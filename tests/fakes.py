"""Helpers: a whole bridge running against the fake openshell + fake omp on a temp dir. Standard library
only, mirroring the dispatcher's test style."""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))

from bridge import config  # noqa: E402
from bridge.engine import Engine  # noqa: E402
from bridge.shell import OpenShell  # noqa: E402
from bridge.store import Store  # noqa: E402

AGENTS = ("alpha", "bravo", "charlie", "delta", "echo")

ALIASES = {
    "local": {"model": "local/model-small",
              "base_url": "http://host.openshell.internal:18770/v1", "api": "openai-completions",
              "api_key_env": "LOCAL_RELAY_CLIENT_TOKEN", "description": "local relay", "enabled": True},
    "hosted": {"model": "hosted/hosted-model", "base_url": "http://host.openshell.internal:18765/v1",
             "api": "anthropic-messages", "api_key_env": "HOSTED_RELAY_CLIENT_TOKEN", "description": "hosted relay",
             "enabled": False},
    "mock": {"model": "mock/m-a", "base_url": "http://127.0.0.1:8099/v1", "api": "openai-completions",
             "api_key_env": "", "description": "loopback mock for proofs", "enabled": True},
}

GIT = ["git", "-c", "user.name=tester", "-c", "user.email=tester@example.com", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null"]


def git(repo, *args, check=True):
    return subprocess.run([*GIT, *args], cwd=repo, capture_output=True, check=check)


FORGEJO_IDENTITIES = {
    "alpha": ("Alpha", "alpha@noreply.git.example.com"),
    "bravo": ("Bravo", "bravo@noreply.git.example.com"),
    "charlie": ("Charlie", "charlie@noreply.git.example.com"),
    "delta": ("Delta", "delta@noreply.git.example.com"),
    "echo": ("Echo", "echo@noreply.git.example.com"),
}
GITHUB_IDENTITY = ("Public Front", "front-account@users.noreply.github.com")


def write_toml(path, aliases=None, default="local", limits=None, cleanup=False, agents_file=None, pins=None):
    aliases = ALIASES if aliases is None else aliases
    limits = limits or {}
    lines = ["[bridge]",
             'listen_host = "127.0.0.1"',
             "listen_port = 18780",
             f'state_dir = "{path.parent}/state"',
             f'tokens_file = "{path.parent}/tokens"',
             'default_base_branch = "main"',
             "[openshell]",
             'binary = "openshell"',
             'gateway = "realm2"',
             'sandbox = "omp-worker"',
             "[workspace]",
             'host = "/srv/workspace"',
             *([f"agents_file = {json.dumps(agents_file)}"] if agents_file is not None else []),
             "[identities]",
             'forgejo_hosts = ["git.example.com"]',
             'github_hosts = ["github.com"]',
             *[line for agent, (name, email) in FORGEJO_IDENTITIES.items()
               for line in (f"[identities.forgejo.{agent}]", f"name = {json.dumps(name)}", f"email = {json.dumps(email)}")],
             "[identities.github]",
             f"name = {json.dumps(GITHUB_IDENTITY[0])}",
             f"email = {json.dumps(GITHUB_IDENTITY[1])}",
             "[models]",
             f'default = "{default}"']
    for name, table in aliases.items():
        lines.append(f"[models.{name}]")
        for key, value in table.items():
            lines.append(f"{key} = {json.dumps(value)}")
    if limits:
        lines.append("[limits]")
        for key, value in limits.items():
            lines.append(f"{key} = {json.dumps(value)}")
    if cleanup:
        lines += ["[cleanup]", "remove_merged_worktrees = true"]
    for project, forge in (pins or {}).items():
        lines += [f"[projects.{project}]", f'identity = "{forge}"']
    path.write_text("\n".join(lines) + "\n")
    return path


def write_tokens(path):
    lines = []
    for agent in AGENTS:
        digest = hashlib.sha256(f"token-{agent}".encode()).hexdigest()
        lines.append(f"{agent} sha256={digest}")
    path.write_text("\n".join(lines) + "\n")
    return path


def token_for(agent):
    return hashlib.sha256(f"token-{agent}".encode()).hexdigest(), f"token-{agent}"


class Bridge:
    """One bridge instance on a temp dir. `fs` is the fake sandbox root (the /sandbox mapping)."""

    def __init__(self, tmp, aliases=None, default="local", limits=None, cleanup=False, env_extra=None, pins=None):
        self.tmp = pathlib.Path(tmp)
        self.fake = self.tmp / "fake"
        self.fs = self.fake / "sandbox"
        self.bin = self.tmp / "bin"
        for directory in (self.fs / "workspace", self.bin, self.tmp / "state"):
            directory.mkdir(parents=True, exist_ok=True)
        for name, source in (("openshell", "fake_openshell.py"), ("omp", "fake_omp.py")):
            link = self.bin / name
            link.symlink_to(HERE / source)
        self.toml = write_toml(self.tmp / "bridge.toml", aliases, default, limits, cleanup, pins=pins)
        self.tokens = write_tokens(self.tmp / "tokens")
        self.cfg = config.load(str(self.toml))
        self.store = Store(str(self.tmp / "state" / "bridge.db"))
        env = {**os.environ,
               "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
               "FAKE_DIR": str(self.fake),
               "FAKE_GATEWAY": "realm2",
               "FAKE_OMP_LOG": str(self.tmp / "omp-log.jsonl"),
               "FAKE_OMP_ACTION": "none",
               "FAKE_OMP_STATUS": "done"}
        env.update(env_extra or {})
        self.env = env
        self.shell = OpenShell("openshell", "realm2", env=env, sandbox="omp-worker")
        self.engine = Engine(self.cfg, self.store, self.shell)

    # ---- workspace -----------------------------------------------------------

    def make_project(self, name, files=None, branch="main"):
        """A real git repo at /sandbox/workspace/<name> with one commit on `branch`."""
        repo = self.fs / "workspace" / name
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q", "-b", branch, ".")
        for rel, content in (files or {"README.md": f"# {name}\n"}).items():
            target = repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "initial")
        return repo

    def mapped(self, sandbox_path):
        """A /sandbox/<path> as the fake host sees it (the fake fs root stands in for /sandbox)."""
        return self.fs / sandbox_path.removeprefix("/sandbox").lstrip("/")

    # ---- driving -------------------------------------------------------------

    def submit(self, agent="alpha", project="demo", spec="do the thing", checks=None, mode="batch",
               base_branch=None, model=None):
        criteria = {"checks": checks if checks is not None else
                    [{"name": "verify", "argv": ["test", "-f", "out.txt"]}], "review": ["keep it small"]}
        return self.engine.submit(agent, project, spec, criteria, mode, base_branch, model)

    def wait_status(self, task_id, statuses, timeout=30):
        deadline = time.time() + timeout
        task = None
        while time.time() < deadline:
            task = self.store.get_task(task_id)
            if task and task["status"] in statuses:
                return task
            time.sleep(0.1)
        raise AssertionError(f"task {task_id} did not reach {statuses}; now {task and task['status']}")

    def set_omp(self, action=None, status=None):
        # the shell holds a copy of the env: mutate THAT, it is what exec children inherit
        if action is not None:
            self.shell.env["FAKE_OMP_ACTION"] = action
        if status is not None:
            self.shell.env["FAKE_OMP_STATUS"] = status

    def omp_log(self):
        path = self.tmp / "omp-log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def stop(self):
        self.engine.stop()
        self.store.close()
