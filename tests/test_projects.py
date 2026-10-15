"""create_project / list_projects: agents can start and discover projects themselves, with the same name rule submit_task uses, a seed
commit made as the creating agent's git identity, and no way to touch a project that already exists."""
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bridge.engine import EngineError
from fakes import Bridge


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class ProjectTests(unittest.TestCase):
    def test_create_makes_a_main_branch_repo_with_a_readme_seed_commit_as_the_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            result = bridge.engine.create_project("bravo", "my-app", "A tiny demo.")
            repo = bridge.fs / "workspace" / "my-app"
            self.assertEqual(result["branch"], "main")
            self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
            self.assertEqual((repo / "README.md").read_text(), "# my-app\n\nA tiny demo.\n")
            self.assertEqual(git(repo, "log", "--format=%an <%ae>|%cn <%ce>"),
                             "Bravo <bravo@noreply.git.example.com>|Bravo <bravo@noreply.git.example.com>")
            self.assertEqual(git(repo, "rev-list", "--count", "HEAD"), "1")
            self.assertEqual(git(repo, "config", "core.sharedRepository"), "group")
            self.assertEqual(result["created_by"], "bravo")
            bridge.stop()

    def test_a_created_project_works_with_submit_task_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.engine.create_project("echo", "fresh")
            bridge.set_omp(action="commit:out.txt:hi")
            view = bridge.submit(agent="echo", project="fresh")
            task = bridge.wait_status(view["task_id"], {"awaiting_review"})
            checks = {c["name"]: c["ok"] for c in bridge.engine.status("echo", view["task_id"])["checks"]}
            self.assertTrue(checks["verify"] and checks["git identity"])   # the worker committed on the new project as Echo
            self.assertEqual(task["project"], "fresh")
            bridge.stop()

    def test_names_must_be_lowercase_slugs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            for bad in ("AcceptDemo", "has space", "../escape", ".hidden", "", "a" * 65, "semi;colon"):
                with self.assertRaises(EngineError) as raised:
                    bridge.engine.create_project("bravo", bad)
                self.assertEqual(raised.exception.code, "invalid_request", bad)
            self.assertEqual(list((bridge.fs / "workspace").iterdir()), [])
            bridge.stop()

    def test_an_existing_project_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            repo = bridge.make_project("demo", files={"keep.txt": "mine\n"})
            before = git(repo, "rev-parse", "HEAD")
            with self.assertRaises(EngineError) as raised:
                bridge.engine.create_project("bravo", "demo", "overwrite me")
            self.assertEqual(raised.exception.code, "already_exists")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), before)
            self.assertEqual((repo / "keep.txt").read_text(), "mine\n")
            self.assertFalse((repo / "README.md").exists() and "overwrite" in (repo / "README.md").read_text())
            bridge.stop()

    def test_two_agents_racing_for_one_name_get_exactly_one_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            outcomes = []

            def go(agent):
                try:
                    bridge.engine.create_project(agent, "race")
                    outcomes.append("created:" + agent)
                except EngineError as error:
                    outcomes.append(error.code)

            threads = [threading.Thread(target=go, args=(a,)) for a in ("bravo", "charlie", "delta")]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(sorted(o.split(":")[0] for o in outcomes), ["already_exists", "already_exists", "created"])
            self.assertEqual(git(bridge.fs / "workspace" / "race", "rev-list", "--count", "HEAD"), "1")
            bridge.stop()

    def test_a_github_pin_makes_the_seed_commit_the_public_front(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp, pins={"upstream-copy": "github"})
            result = bridge.engine.create_project("charlie", "upstream-copy")
            self.assertEqual(result["committed_as"], "Public Front <front-account@users.noreply.github.com>")
            bridge.stop()

    def test_list_projects_shows_every_repo_for_every_agent_and_who_made_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge = Bridge(tmp)
            bridge.make_project("by-hand")
            bridge.engine.create_project("delta", "by-delta", "x")
            (bridge.fs / "workspace" / "not-a-repo").mkdir()            # a plain folder is not a project
            (bridge.fs / "workspace" / "Upper").mkdir()                 # nor can submit_task use this name
            (bridge.fs / "workspace" / ".worktrees").mkdir(exist_ok=True)   # the bridge's own dot folders are never projects
            listed = {p["name"]: p for p in bridge.engine.list_projects("alpha")}
            self.assertEqual(sorted(listed), ["by-delta", "by-hand"])
            self.assertEqual(listed["by-delta"]["created_by"], "delta")
            self.assertIsNone(listed["by-hand"]["created_by"])
            self.assertEqual(listed["by-delta"]["last_commit_on_main"]["subject"],
                             "Initial commit (project created through omp_bridge by delta)")
            self.assertEqual(listed["by-hand"]["commits_as_you"], "Alpha <alpha@noreply.git.example.com>")
            self.assertEqual(bridge.engine.list_projects("charlie")[0]["commits_as_you"].split(" <")[0], "Charlie")
            bridge.stop()


class ProjectToolsOverMcp(unittest.TestCase):
    def test_tools_exist_and_reject_a_bad_name_at_the_schema(self):
        from bridge import mcp_http
        names = {t["name"]: t for t in mcp_http.TOOLS}
        self.assertEqual(names["create_project"]["inputSchema"]["required"], ["name"])
        self.assertEqual(names["list_projects"]["inputSchema"]["properties"], {})
        ok, _ = mcp_http.Handler._validate({"name": "accept-demo"}, names["create_project"]["inputSchema"])
        bad, why = mcp_http.Handler._validate({"name": "AcceptDemo"}, names["create_project"]["inputSchema"])
        self.assertTrue(ok)
        self.assertFalse(bad, why)
        json.dumps(mcp_http.TOOLS)   # the schemas must stay serialisable


if __name__ == "__main__":
    unittest.main()
