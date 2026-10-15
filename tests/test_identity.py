"""Git identity of worker commits: the bridge decides it per task (forgejo agent identity, or the GitHub front identity), stamps it into
the worker's environment, and verifies every commit afterwards, because an explicit --author can still override the environment."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge import config, identity
from bridge.engine import EngineError
from fakes import FORGEJO_IDENTITIES, GITHUB_IDENTITY, Bridge, write_toml

BRAVO = "Bravo <bravo@noreply.git.example.com>"
FRONT = "Public Front <front-account@users.noreply.github.com>"


def set_origin(bridge, project, url):
    subprocess.run(["git", "-C", str(bridge.fs / "workspace" / project), "remote", "add", "origin", url], check=True)


def commit_authors(bridge, task):
    out = subprocess.run(["git", "-C", str(bridge.mapped(task["worktree"])), "log", "--format=%an <%ae>|%cn <%ce>",
                          f"{task['base_commit']}..HEAD"], capture_output=True, text=True, check=True).stdout
    return [line.split("|") for line in out.splitlines()]


class ClassifyTests(unittest.TestCase):
    HOSTS = (("git.example.com",), ("github.com",))

    def test_every_remote_url_shape(self):
        cases = {
            "git@git.example.com:owner/app.git": "forgejo",
            "ssh://git@git.example.com:2222/owner/app.git": "forgejo",
            "https://git.example.com/owner/app": "forgejo",
            "https://user:token@git.example.com/owner/app": "forgejo",
            "git@github.com:front-account/app.git": "github",
            "https://github.com/front-account/app": "github",
            "HTTPS://GitHub.COM/x/y": "github",
            "git@gitlab.com:x/y.git": None,
            "/srv/mirrors/app.git": None,
            "": None,
            None: None,
        }
        for url, want in cases.items():
            self.assertEqual(identity.classify(url, *self.HOSTS), want, url)

    def test_lookalike_hosts_are_not_trusted(self):
        for url in ("https://git.example.com.evil.example/x", "https://notgithub.com/x", "git@evilgithub.com:x/y.git"):
            self.assertIsNone(identity.classify(url, *self.HOSTS), url)

    def test_env_for_sets_all_four_variables(self):
        self.assertEqual(identity.env_for("A B", "a@b.c"),
                         ["GIT_AUTHOR_NAME=A B", "GIT_AUTHOR_EMAIL=a@b.c", "GIT_COMMITTER_NAME=A B", "GIT_COMMITTER_EMAIL=a@b.c"])

    def test_offenders_catch_a_wrong_author_or_committer(self):
        lines = ["aaa\tBravo\tz@x\tBravo\tz@x", "bbb\tMallory\tm@x\tBravo\tz@x", "ccc\tBravo\tz@x\tomp-worker\to@x", "garbage"]
        bad = identity.offenders(lines, "Bravo", "z@x")
        self.assertEqual([b.split(":")[0] for b in bad], ["bbb", "ccc"])


class ConfigTests(unittest.TestCase):
    def load(self, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            return config.load(str(write_toml(pathlib.Path(tmp) / "bridge.toml", **kw)))

    def test_identities_load_for_every_agent(self):
        cfg = self.load()
        self.assertEqual(cfg.identity_for("charlie", "forgejo"), FORGEJO_IDENTITIES["charlie"])
        self.assertEqual(cfg.identity_for("charlie", "github"), GITHUB_IDENTITY)
        self.assertEqual(cfg.identity_for("alpha", "github"), GITHUB_IDENTITY)   # one public front for every agent

    def test_an_invalid_agent_name_stops_the_bridge_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_toml(pathlib.Path(tmp) / "bridge.toml")
            text = path.read_text().replace("[identities.forgejo.delta]", "[identities.forgejo.Not_Valid]")
            path.write_text(text)
            with self.assertRaises(config.ConfigError):
                config.load(str(path))

    def test_bad_email_and_bad_pin_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_toml(pathlib.Path(tmp) / "bridge.toml")
            path.write_text(path.read_text().replace("alpha@noreply.git.example.com", "not an address"))
            with self.assertRaises(config.ConfigError):
                config.load(str(path))
            with self.assertRaises(config.ConfigError):
                self.load(pins={"demo": "gitlab"})

    def test_pins_load(self):
        self.assertEqual(self.load(pins={"demo": "github"}).project_pins, {"demo": "github"})


class EngineTests(unittest.TestCase):
    def submit_and_wait(self, bridge, project, action, agent="bravo"):
        bridge.set_omp(action=action)
        view = bridge.submit(agent=agent, project=project)
        task = bridge.wait_status(view["task_id"], {"awaiting_review", "failed", "needs_human"})
        return view, task

    def test_forgejo_origin_commits_as_the_delegating_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            set_origin(bridge, "demo", "git@git.example.com:owner/demo.git")
            view, task = self.submit_and_wait(bridge, "demo", "commit:a.txt:a")
            self.assertEqual(commit_authors(bridge, task), [[BRAVO, BRAVO]])
            checks = {c["name"]: c["ok"] for c in bridge.engine.status("bravo", view["task_id"])["checks"]}
            self.assertTrue(checks["git identity"])
            evidence = json.loads((bridge.mapped("/sandbox/workspace/.omp-bridge") / view["task_id"] / "identity.json").read_text())
            self.assertEqual(evidence["forge"], "forgejo")
            self.assertEqual(evidence["source"], "inferred")
            bridge.stop()

    def test_github_origin_commits_as_the_public_front_for_any_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            set_origin(bridge, "demo", "https://github.com/front-account/demo")
            _, task = self.submit_and_wait(bridge, "demo", "commit:a.txt:a", agent="echo")
            self.assertEqual(commit_authors(bridge, task), [[FRONT, FRONT]])
            bridge.stop()

    def test_the_pin_beats_the_origin_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp, pins={"demo": "github"})
            bridge.make_project("demo")
            set_origin(bridge, "demo", "git@git.example.com:owner/demo.git")   # a Forgejo copy the operator sends upstream by hand
            view, task = self.submit_and_wait(bridge, "demo", "commit:a.txt:a")
            self.assertEqual(commit_authors(bridge, task), [[FRONT, FRONT]])
            evidence = json.loads((bridge.mapped("/sandbox/workspace/.omp-bridge") / view["task_id"] / "identity.json").read_text())
            self.assertEqual(evidence["source"], "pinned")
            bridge.stop()

    def test_no_remote_defaults_to_forgejo_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            view, task = self.submit_and_wait(bridge, "demo", "commit:a.txt:a")
            self.assertEqual(commit_authors(bridge, task), [[BRAVO, BRAVO]])
            evidence = json.loads((bridge.mapped("/sandbox/workspace/.omp-bridge") / view["task_id"] / "identity.json").read_text())
            self.assertEqual((evidence["forge"], evidence["source"]), ("forgejo", "default"))
            bridge.stop()

    def test_an_explicit_author_flag_is_caught_by_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            view, _ = self.submit_and_wait(bridge, "demo", "commit-as:a.txt:a")
            checks = {c["name"]: c for c in bridge.engine.status("bravo", view["task_id"])["checks"]}
            self.assertFalse(checks["git identity"]["ok"])
            detail = bridge.store.get_checks(view["task_id"])
            wrong = [c for c in detail if c["name"] == "git identity"][0]["output_tail"]
            self.assertIn("Mallory", wrong)
            self.assertIn(BRAVO, wrong)
            bridge.stop()

    def test_a_changed_origin_is_refused_until_the_project_is_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("demo")
            set_origin(bridge, "demo", "git@git.example.com:owner/demo.git")
            self.submit_and_wait(bridge, "demo", "commit:a.txt:a")
            subprocess.run(["git", "-C", str(bridge.fs / "workspace" / "demo"), "remote", "set-url", "origin",
                            "https://github.com/someone/demo"], check=True)
            with self.assertRaises(EngineError) as raised:
                bridge.submit(agent="bravo", project="demo")
            self.assertEqual(raised.exception.code, "identity_changed")
            bridge.stop()


if __name__ == "__main__":
    unittest.main()
