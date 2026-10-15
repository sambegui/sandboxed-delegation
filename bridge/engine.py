"""The bridge's state machine: task creation (worktree + branch via exec), rounds through the batch or
rpc back end, the done_criteria check runner, hard stops, cancel, restart recovery and the long-poll
waiters. All state changes go through the sqlite store with a monotonically increasing revision, and
every change wakes the /v1/wait waiters.

Everything the bridge does to the workspace — every git command, every check, every evidence write —
runs inside the worker sandbox through `openshell sandbox exec`, never with host-side git on the host
path, so owners and absolute paths stay consistent for the sandbox, the cells and the desktop.
"""
import json
import logging
import os
import re
import threading
import time

from . import identity, models, runner_batch, runner_rpc
from .models import ModelSyncer
from .shell import ShellError, worker_argv

log = logging.getLogger("bridge.engine")

GIT = ["env", "GIT_OPTIONAL_LOCKS=0", "git", "-c", "safe.directory=*"]
GITCONFIG = ("[safe]\n\tdirectory = *\n[user]\n\tname = omp-worker\n\temail = omp-worker@localhost\n"
             "[core]\n\tsharedRepository = group\n")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
STATUS_BLOCK_RE = re.compile(r"```omp-bridge-status\s*\n(.*?)```", re.DOTALL)
OUTPUT_TAIL_CAP = 4096
RESULT_MESSAGE_CAP = 16 * 1024
INDEX_LOCK_RETRIES = 3


class EngineError(Exception):
    """A business-rule failure worth returning to the caller. `code` is machine-readable."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def parse_status_block(last_message):
    """The worker's final fenced block: {status, summary, questions}. Absent or invalid -> unclear."""
    if not last_message:
        return None
    blocks = STATUS_BLOCK_RE.findall(last_message)
    if not blocks:
        return None
    try:
        block = json.loads(blocks[-1])
    except ValueError:
        return None
    if not isinstance(block, dict) or block.get("status") not in ("done", "needs_info", "blocked"):
        return None
    questions = block.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    return {"status": block["status"],
            "summary": block.get("summary") if isinstance(block.get("summary"), str) else "",
            "questions": [q for q in questions if isinstance(q, str)]}


class Engine:
    def __init__(self, cfg, store, shell):
        self.cfg, self.store, self.shell = cfg, store, shell
        self.model_syncer = ModelSyncer(cfg, store, shell)
        self._cond = threading.Condition()
        self._project_locks = {}
        self._project_locks_guard = threading.Lock()
        self._runners = {}          # task_id -> BatchRunner | RpcRunner (live rounds only)
        self._runners_lock = threading.Lock()
        self._round_threads = []
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ small helpers

    def _project_lock(self, project):
        with self._project_locks_guard:
            return self._project_locks.setdefault(project, threading.Lock())

    def _evidence_dir(self, task_id):
        return f"/sandbox/workspace/.omp-bridge/{task_id}"

    def _worktree_path(self, project, task_id):
        return f"/sandbox/workspace/.worktrees/{project}/{task_id}"

    def _exec(self, argv, *, workdir="/sandbox", stdin=None, timeout=120):
        done = self.shell.exec(worker_argv(argv), workdir=workdir, stdin=stdin, timeout=timeout)
        return done

    def _exec_checked(self, argv, *, workdir="/sandbox", stdin=None, timeout=120, what="exec"):
        done = self._exec(argv, workdir=workdir, stdin=stdin, timeout=timeout)
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()[-300:]
            raise ShellError("exec_failed", f"{what} exited {done.returncode}: {detail}")
        return done

    def _write_evidence(self, task_id, name, content):
        """One evidence file inside the sandbox, via tee with the content on stdin. Never a shell string."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        self._exec_checked(["mkdir", "-p", self._evidence_dir(task_id)], what="mkdir evidence")
        self._exec_checked(["tee", f"{self._evidence_dir(task_id)}/{name}"], stdin=content, what=f"write {name}")

    def _public_checks(self, task_id, round_n):
        return [{"name": row["name"], "ok": bool(row["ok"]), "exit_code": row["exit_code"]}
                for row in self.store.get_checks(task_id, round_n)]

    def _status_view(self, task):
        latest = self.store.last_round(task["task_id"])
        round_n = latest["n"] if latest else 0
        return {"task_id": task["task_id"], "status": task["status"], "revision": task["revision"],
                "round": round_n,
                "max_rounds": task["max_rounds"], "mode": task["mode"], "worker_status": task["worker_status"],
                "questions": json.loads(task["questions"]) if task["questions"] else [],
                "summary": task["summary"] or "", "branch": task["branch"], "worktree": task["worktree"],
                "model": task["model"], "models_used": json.loads(task["models_used"] or "[]"),
                "updated_at": task["updated_at"],
                "limits": {"max_rounds": task["max_rounds"], "round_max_time_s": self.cfg.round_max_time_s,
                           "total_wall_s": self.cfg.total_wall_s, "max_active_tasks": self.cfg.max_active_tasks,
                           "spec_max_bytes": self.cfg.spec_max_bytes},
                "evidence_dir": self._evidence_dir(task["task_id"]),
                "error": task["error"],
                "checks": self._public_checks(task["task_id"], round_n) if latest else []}

    def _notify(self):
        with self._cond:
            self._cond.notify_all()

    def _bump(self, task_id, status, **fields):
        task = self.store.bump(task_id, status, **fields)
        self._write_status(task)
        self._notify()
        return task

    def _write_status(self, task):
        try:
            view = dict(task)
            view["done_criteria"] = json.loads(task["done_criteria"])
            view["checks"] = self.store.get_checks(task["task_id"])
            self._write_evidence(task["task_id"], "status.json",
                                 json.dumps(view, indent=1, default=str) + "\n")
        except ShellError as error:
            log.warning("task=%s status.json write failed: %s", task["task_id"], error)

    # ------------------------------------------------------------------ readiness

    def _ensure_ready(self):
        """models.yml and the rpc daemon script in place (hash-tracked), workspace tree present, and a
        worker home with a gitconfig: projects created on the desktop are owned by an unmapped uid, so
        git refuses them without safe.directory, and shared repos need the group-writable core mode."""
        try:
            self.model_syncer.ensure()
            self._upload_daemon()
            self._exec_checked(["mkdir", "-p", "/sandbox/workspace", "/sandbox/.omp-sessions",
                                "/sandbox/workspace/.worktrees", "/sandbox/workspace/.omp-bridge",
                                "/sandbox/rpc", "/sandbox/.home"],
                               what="mkdir workspace")
            self._exec_checked(["tee", "/sandbox/.home/.gitconfig"], stdin=GITCONFIG.encode("utf-8"),
                               what="write gitconfig")
        except (ShellError, Exception) as error:
            raise EngineError("worker_unavailable", f"the worker sandbox is not ready: {error}") from None

    def _upload_daemon(self):
        """rpcd.sh, uploaded when its content changed (hash in sqlite, like models.yml)."""
        import hashlib
        import pathlib
        asset = pathlib.Path(__file__).resolve().parent / "assets" / "rpcd.sh"
        content = asset.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if self.store.kv_get("rpcd_sha256") == digest:
            return
        import tempfile
        with tempfile.NamedTemporaryFile("wb", suffix=".rpcd.sh", delete=False) as handle:
            handle.write(content)
            local = handle.name
        try:
            self.shell.upload(local, "/sandbox/rpc/rpcd.sh")
        finally:
            os.unlink(local)
        self.store.kv_set("rpcd_sha256", digest)
        log.info("rpcd.sh uploaded (sha256=%s)", digest[:12])

    # ------------------------------------------------------------------ submit

    def submit(self, agent, project, spec, done_criteria, mode, base_branch, requested_model):
        if not PROJECT_RE.match(project):
            raise EngineError("invalid_request",
                              f"project {project!r} must match {PROJECT_RE.pattern}")
        if len(spec.encode("utf-8")) > self.cfg.spec_max_bytes:
            raise EngineError("invalid_request", f"spec exceeds {self.cfg.spec_max_bytes} bytes")
        checks = done_criteria.get("checks", [])
        if len(checks) > self.cfg.max_checks:
            raise EngineError("invalid_request",
                              f"done_criteria.checks has {len(checks)} entries; the limit is {self.cfg.max_checks}")
        for check in checks:
            argv = check.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
                raise EngineError("invalid_request", f"check {check.get('name')!r} needs a non-empty argv list")
        alias = models.resolve(self.cfg.aliases, self.store.agent_default_model(agent),
                               self.cfg.model_default, requested_model)
        if self.store.count_active() >= self.cfg.max_active_tasks:
            raise EngineError("too_many_active",
                              f"max_active_tasks is {self.cfg.max_active_tasks}; try again when a task finishes")

        # the public name is "session"; "rpc" is accepted as an alias. PROBED 2026-10-14: comparing against "rpc" while the MCP
        # schema says "session" made every session task silently run as a batch round.
        if mode in ("session", "rpc"):
            mode = "session"
        elif mode != "batch":
            raise EngineError("invalid_request", f"mode {mode!r} must be batch or session")
        base_branch = base_branch or self.cfg.default_base_branch
        task_id = self.store.new_task_id()
        branch, worktree = f"omp/{task_id}", self._worktree_path(project, task_id)
        self.store.create_task(task_id, agent, project, spec, done_criteria, mode, base_branch, branch, worktree,
                               "", self.cfg.max_rounds, alias.alias)
        task = self.store.get_task(task_id)
        self._write_status(task)
        try:
            self._ensure_ready()
            base_commit = self._create_worktree(task, project, base_branch, worktree, branch)
        except EngineError as error:
            self._bump(task_id, "failed", error=f"setup: {error.message}")
            raise
        except ShellError as error:
            self._bump(task_id, "failed", error=f"setup: {error}")
            raise EngineError("worker_unavailable", f"could not prepare the worker: {error}") from None
        except Exception as error:
            self._bump(task_id, "failed", error=f"setup: {type(error).__name__}: {error}")
            raise EngineError("worker_unavailable", f"could not prepare the worker: {error}") from None
        try:
            forge, source = self._resolve_forge(project)
        except EngineError as error:
            self._bump(task_id, "failed", error=f"setup: {error.message}")
            raise
        self.store.kv_set(f"forge:{task_id}", forge)
        self.store.set_fields(task_id, base_commit=base_commit)
        self._write_evidence(task_id, "spec.md", spec)
        name, email = self.cfg.identity_for(agent, forge)
        self._write_evidence(task_id, "identity.json", json.dumps(
            {"forge": forge, "source": source, "author_and_committer": f"{name} <{email}>"}) + "\n")
        self._bump(task_id, "queued")
        self._start_round(task_id, feedback=None)
        return self._status_view(self.store.get_task(task_id))

    def _create_worktree(self, task, project, base_branch, worktree, branch):
        """Inside the per-project lock: verify the repo and base, pin prune behaviour, add the locked
        worktree on its own branch. Retries past a foreign index.lock; never touches the primary tree."""
        repo = f"/sandbox/workspace/{project}"
        with self._project_lock(project):
            try:
                done = self._exec(GIT + ["rev-parse", "--verify", f"{base_branch}^{{commit}}"],
                                  workdir=repo, timeout=60)
            except ShellError as error:
                raise EngineError("invalid_request", f"project {project!r}: {error}") from None
            if done.returncode != 0:
                raise EngineError("invalid_request",
                                  f"project {project!r} has no base branch {base_branch!r}: "
                                  + done.stderr.decode("utf-8", "replace").strip()[-200:])
            base_commit = done.stdout.decode("utf-8", "replace").strip().splitlines()[0]
            try:
                self._exec_checked(GIT + ["config", "gc.worktreePruneExpire", "never"], workdir=repo,
                                   what="config gc.worktreePruneExpire")
            except ShellError as error:
                log.warning("project=%s could not pin gc.worktreePruneExpire: %s", project, error)
            self._exec_checked(["mkdir", "-p", f"/sandbox/workspace/.worktrees/{project}"],
                               what="mkdir worktrees")
            for attempt in range(INDEX_LOCK_RETRIES):
                done = self._exec(GIT + ["worktree", "add", "--lock",
                                         "--reason", f"omp-bridge task {task['task_id']}", "-b", branch,
                                         worktree, base_commit],
                                  workdir=repo, timeout=120)
                if done.returncode == 0:
                    return base_commit
                if b"index.lock" in done.stderr and attempt + 1 < INDEX_LOCK_RETRIES:
                    log.info("project=%s index.lock held, retrying worktree add", project)
                    time.sleep(1)
                    continue
                raise ShellError("worktree_add", done.stderr.decode("utf-8", "replace").strip()[-300:])
        raise ShellError("worktree_add", "unreachable")

    # ------------------------------------------------------------------ prompts

    def _instruction_block(self):
        return (
            "\n\n## How you report back\n"
            "End your final message with exactly one fenced block like this:\n"
            "```omp-bridge-status\n"
            '{"status":"done","summary":"one line","questions":[]}\n'
            "```\n"
            "- status is done (the done_criteria checks should pass), needs_info (you must ask the "
            "delegating agent something; put the questions in questions), or blocked (you cannot proceed; "
            "say why in summary).\n"
            "- If the block is missing or invalid, the round counts as unclear: say what happened in summary.\n"
            "Constraints: no network access; do not leave this worktree; do not create other worktrees; do "
            "not touch the primary checkout or its branch; never commit to the base branch. Make the "
            "changes and run the checks yourself; leave the branch state clean enough to review.\n")

    def _build_prompt(self, task, feedback, prior_checks):
        if task["round"] == 0:
            prompt = task["spec"]
            review = json.loads(task["done_criteria"]).get("review") or []
            if review:
                prompt += "\n\n## Review focus\n" + "\n".join(f"- {line}" for line in review)
            return prompt + self._instruction_block()
        prompt = f"## Feedback from the delegating agent\n{feedback}\n"
        failures = [check for check in prior_checks if not check["ok"]]
        if failures:
            prompt += "\n## Failed checks from your last round (fix these first)\n"
            for check in failures:
                prompt += f"- {check['name']} (exit {check['exit_code']}): {check['output_tail'][-400:]}\n"
        return prompt + self._instruction_block()

    # ------------------------------------------------------------------ rounds

    def _start_round(self, task_id, feedback):
        task = self.store.get_task(task_id)
        prior = self.store.last_round(task_id)
        round_n = (prior["n"] if prior else 0) + 1
        # the task's own model (chosen at submit or on feedback) is the round's model
        alias = models.resolve(self.cfg.aliases, None, self.cfg.model_default, task["model"])
        prompt = self._build_prompt(task, feedback,
                                    self.store.get_checks(task_id, prior["n"]) if prior else [])
        if round_n == 1:
            self.store.set_fields(task_id, started_at=time.time())
        self.store.set_fields(task_id, round=round_n)
        self._bump(task_id, "running", error=None)
        thread = threading.Thread(target=self._run_round, args=(task_id, round_n, alias, prompt),
                                  daemon=True, name=f"round-{task_id}-{round_n}")
        self._round_threads.append(thread)
        thread.start()

    def _resolve_forge(self, project):
        """Which forge this project's work is going to: ("forgejo" | "github", source). A pin in bridge.toml wins. Otherwise the `origin`
        host of the main checkout is inferred and REMEMBERED (trust on first use): worktrees share the repository's config, so a worker
        could edit remote.origin.url, and a changed answer is refused until the operator pins the project. No remote or an unknown host means
        forgejo (the configured Forgejo host is the artifact plane)."""
        pinned = self.cfg.project_pins.get(project)
        if pinned:
            return pinned, "pinned"
        url = None
        try:
            done = self._exec(GIT + ["config", "--get", "remote.origin.url"], workdir=f"/sandbox/workspace/{project}", timeout=30)
            if done.returncode == 0:
                url = done.stdout.decode("utf-8", "replace").strip() or None
        except ShellError:
            url = None
        inferred = identity.classify(url, self.cfg.forgejo_hosts, self.cfg.github_hosts)
        forge, source = (inferred, "inferred") if inferred else ("forgejo", "default")
        seen = self.store.kv_get(f"forge-seen:{project}")
        if seen is None:
            self.store.kv_set(f"forge-seen:{project}", forge)
        elif seen != forge:
            raise EngineError("identity_changed",
                              f"project {project!r} was first seen as {seen} but its origin now points to {forge}; check the remote, then "
                              f"pin the project in bridge.toml ([projects.{project}] identity = \"forgejo\" or \"github\")")
        return forge, source

    def _forge_of(self, task):
        forge = self.store.kv_get(f"forge:{task['task_id']}")
        if forge is None:   # a task created before identities existed: decide now by the same rules
            forge = self._resolve_forge(task["project"])[0]
            self.store.kv_set(f"forge:{task['task_id']}", forge)
        return forge

    def _git_env(self, task):
        name, email = self.cfg.identity_for(task["agent"], self._forge_of(task))
        return identity.env_for(name, email)

    def _record_context(self, task_id, round_n):
        """Evidence for one operator question: did the worker round get the shared AGENTS.md? omp's own logs do not store the system
        prompt, so the bridge records what it passed (the flag) and whether the file existed when the round started."""
        flags = self.cfg.context_flags()
        path = flags[1] if flags else None
        present = None
        if path:
            try:
                present = self._exec(["test", "-f", path], timeout=30).returncode == 0
            except ShellError:
                present = None
        record = {"append_system_prompt": path, "file_present_at_round_start": present}
        log.info("task=%s round=%s context=%s", task_id, round_n, record)
        try:
            self._write_evidence(task_id, f"round-{round_n}-context.json", json.dumps(record) + "\n")
        except ShellError as error:
            log.warning("task=%s round=%s context evidence write failed: %s", task_id, round_n, error)

    def _run_round(self, task_id, round_n, alias, prompt):
        self._record_context(task_id, round_n)
        task = self.store.get_task(task_id)
        events_path = os.path.join(self.cfg.state_dir, "tmp", f"{task_id}-r{round_n}.jsonl")
        os.makedirs(os.path.dirname(events_path), exist_ok=True)
        session_file = None
        prior = self.store.round_by_n(task_id, round_n - 1) if round_n > 1 else None
        if prior and prior.get("session_file"):
            session_file = prior["session_file"]
        has_session = bool(prior and (prior.get("session_id") or prior.get("session_file")))
        if round_n > 1 and not has_session:
            # PROBED: a round killed early (service restart, timeout) may never have persisted an omp session, and
            # `--continue` then exits 1. Start fresh and carry the spec, which later-round prompts do not repeat.
            prompt = task["spec"] + "\n\n" + prompt
        outcome = None
        error = None
        try:
            if task["mode"] == "session":
                outcome = self._rpc_round(task, alias, prompt, events_path, round_n, session_file)
            else:
                runner = runner_batch.BatchRunner(self.shell, self.cfg, task, alias, round_n > 1 and has_session, prompt,
                                                  events_path, git_env=self._git_env(task))
                with self._runners_lock:
                    self._runners[task_id] = runner
                outcome = runner.run()
        except Exception as error_:  # infrastructure failure: the round did not produce a verdict
            error = f"{type(error_).__name__}: {error_}"
            log.exception("task=%s round=%s crashed", task_id, round_n)
        finally:
            if task["mode"] != "session":   # the rpc daemon stays for the task's next round
                with self._runners_lock:
                    self._runners.pop(task_id, None)
        self._finish_round(task_id, round_n, alias, prompt, outcome, error, events_path)

    def _rpc_round(self, task, alias, prompt, events_path, round_n, session_file):
        with self._runners_lock:
            runner = self._runners.get(task["task_id"])
        if runner is None or runner.dead:
            runner = runner_rpc.RpcRunner(self.shell, self.cfg, task, alias, session_file=session_file,
                                          git_env=self._git_env(task))
            with self._runners_lock:
                self._runners[task["task_id"]] = runner
            runner.start()   # fresh daemon, resuming the recorded session file when there is one
        elif round_n > 1 and runner.alias.alias != alias.alias:
            runner.set_model(alias)
        outcome = runner.round(prompt, events_path, follow_up=(round_n > 1))
        return outcome

    def _finish_round(self, task_id, round_n, alias, prompt, outcome, error, events_path):
        task = self.store.get_task(task_id)
        if task is None or task["status"] in ("cancelled", "closed"):
            return
        models_used = json.loads(task["models_used"] or "[]")
        if alias.alias not in models_used:
            models_used.append(alias.alias)
        round_pk = self.store.start_round(task_id, round_n, alias.alias,
                                          outcome.session_id if outcome else None, len(prompt.encode("utf-8")))
        if outcome is None:
            self.store.end_round(round_pk, None, None, None)
            self._bump(task_id, "failed", error=error, models_used=models_used)
            self._write_result(task_id, round_n, None, [])
            self._retire_runner(task_id)
            return
        block = parse_status_block(outcome.last_message)
        worker_status = block["status"] if block else "unclear"
        summary = block["summary"] if block else (outcome.error or "")
        questions = block["questions"] if block and block["status"] == "needs_info" else None
        last_message = (outcome.last_message or "")[:RESULT_MESSAGE_CAP] or None
        self.store.end_round(round_pk, worker_status, outcome.stop_reason, last_message, outcome.session_id,
                             outcome.session_file)
        try:
            with open(events_path, "rb") as events_file:
                self._write_evidence(task_id, f"round-{round_n}.jsonl", events_file.read())
        except ShellError as write_error:
            log.warning("task=%s round file upload failed: %s", task_id, write_error)
        checks = self._run_checks(task, round_n)
        self.store.put_checks(task_id, round_n, checks)
        try:
            self._write_evidence(task_id, f"round-{round_n}-checks.json", json.dumps(checks, indent=1) + "\n")
            self._write_result(task_id, round_n, outcome, checks)
        except ShellError as write_error:
            log.warning("task=%s evidence write failed: %s", task_id, write_error)

        if outcome.error:
            self._bump(task_id, "failed", round=round_n, models_used=models_used, worker_status=worker_status,
                       summary=summary, questions=questions, stop_reason=outcome.stop_reason, error=outcome.error)
            self._retire_runner(task_id)
            return
        over_wall = task["started_at"] and (time.time() - task["started_at"]) > self.cfg.total_wall_s
        if over_wall:
            self._bump(task_id, "needs_human", round=round_n, models_used=models_used, worker_status=worker_status,
                       summary=summary, questions=questions, stop_reason=outcome.stop_reason,
                       error="total_wall_s exceeded")
            self._retire_runner(task_id)
            return
        if worker_status != "done" and round_n >= task["max_rounds"]:
            self._bump(task_id, "needs_human", round=round_n, models_used=models_used, worker_status=worker_status,
                       summary=summary, questions=questions, stop_reason=outcome.stop_reason,
                       error=f"max_rounds ({task['max_rounds']}) reached")
            self._retire_runner(task_id)
            return
        status = {"done": "awaiting_review", "needs_info": "needs_info", "blocked": "blocked",
                  "unclear": "awaiting_review"}[worker_status]
        self._bump(task_id, status, round=round_n, models_used=models_used, worker_status=worker_status,
                   summary=summary, questions=questions, stop_reason=outcome.stop_reason, error=None)
        if status != "running":
            self._retire_runner(task_id)

    def _retire_runner(self, task_id):
        """The task left running: the rpc child (if any) is done for now; the next round reopens the session."""
        with self._runners_lock:
            runner = self._runners.pop(task_id, None)
        if isinstance(runner, runner_rpc.RpcRunner):
            try:
                runner.close()
            except Exception:
                log.debug("task=%s rpc runner close failed", task_id, exc_info=True)

    def _write_result(self, task_id, round_n, outcome, checks):
        lines = [f"# Round {round_n}", ""]
        if outcome is not None:
            if outcome.error:
                lines += [f"infrastructure error: {outcome.error}", ""]
            if outcome.last_message:
                lines += ["## Last assistant message", "", outcome.last_message[:RESULT_MESSAGE_CAP], ""]
        failed = [c for c in checks if not c["ok"]]
        lines += ["## Checks", ""]
        lines += [f"- {c['name']}: {'ok' if c['ok'] else 'FAILED (exit ' + str(c['exit_code']) + ')'}" for c in checks] or ["(none)"]
        if failed:
            lines += ["", "## Failed check output", ""]
            for check in failed:
                lines += [f"### {check['name']}", "", check["output_tail"], ""]
        self._write_evidence(task_id, "result.md", "\n".join(lines) + "\n")

    def _run_checks(self, task, round_n):
        """The objective gate: every done_criteria check runs inside the worker, in the task's worktree,
        with its own timeout. Output tails are stored, not whole transcripts."""
        criteria = json.loads(task["done_criteria"])
        results = []
        for check in criteria.get("checks", []):
            name = check.get("name", "check")
            argv = list(check["argv"])
            cwd = check.get("cwd") or "."
            timeout = int(check.get("timeout_s") or self.cfg.check_timeout_s)
            workdir = task["worktree"] if cwd in (".", "") else f"{task['worktree']}/{cwd}"
            try:
                done = self._exec(["env", "HOME=/sandbox/.home", *argv], workdir=workdir, timeout=timeout)
                output = (done.stdout + b"\n" + done.stderr)[-OUTPUT_TAIL_CAP:].decode("utf-8", "replace")
                results.append({"name": name, "ok": done.returncode == 0, "exit_code": done.returncode,
                                "output_tail": output})
            except ShellError as error:
                results.append({"name": name, "ok": False, "exit_code": -1, "output_tail": str(error)[:OUTPUT_TAIL_CAP]})
        results.append(self._identity_check(task))
        log.info("task=%s round=%s checks: %s", task["task_id"], round_n,
                 ", ".join(f"{r['name']}:{'ok' if r['ok'] else 'fail'}" for r in results) or "none")
        return results

    def _identity_check(self, task):
        """Every commit this task added must carry the identity the bridge stamped. The environment covers the worker's own git calls; an
        explicit `--author` flag or `-c user.*` can still override it, so the bridge VERIFIES instead of trusting. Never rewrites history:
        a failure goes back to the delegating agent as a failed check and the worker fixes it in the next round."""
        try:
            name, email = self.cfg.identity_for(task["agent"], self._forge_of(task))
            done = self._exec(GIT + ["log", "--format=%h%x09%an%x09%ae%x09%cn%x09%ce", f"{task['base_commit']}..HEAD"],
                              workdir=task["worktree"], timeout=60)
        except (ShellError, EngineError) as error:
            message = getattr(error, "message", None) or str(error)
            return {"name": "git identity", "ok": False, "exit_code": -1, "output_tail": message[:OUTPUT_TAIL_CAP]}
        if done.returncode != 0:
            return {"name": "git identity", "ok": False, "exit_code": done.returncode,
                    "output_tail": done.stderr.decode("utf-8", "replace")[-OUTPUT_TAIL_CAP:]}
        bad = identity.offenders(done.stdout.decode("utf-8", "replace").splitlines(), name, email)
        if bad:
            output = f"expected {name} <{email}> on every commit; wrong:\n" + "\n".join(bad)
        else:
            output = f"every commit is {name} <{email}>"
        return {"name": "git identity", "ok": not bad, "exit_code": 1 if bad else 0, "output_tail": output[:OUTPUT_TAIL_CAP]}

    # ------------------------------------------------------------------ feedback / steer

    def feedback(self, agent, task_id, text, how, model=None):
        task = self.store.get_task(task_id, agent)
        if task is None:
            raise EngineError("not_found", f"no task {task_id!r}")
        if how == "steer":
            if task["mode"] != "session" or task["status"] != "running":
                raise EngineError("steer_unsupported", "steer is valid only for mode session while a round is running")
            if model is not None:
                raise EngineError("steer_unsupported", "a model change cannot ride a steer; use next_round")
            with self._runners_lock:
                runner = self._runners.get(task_id)
            deadline = time.monotonic() + 10
            while (runner is None or getattr(runner, "dead", True)) and time.monotonic() < deadline:
                time.sleep(0.1)
                with self._runners_lock:
                    runner = self._runners.get(task_id)
            if runner is None or getattr(runner, "dead", True):
                raise EngineError("steer_unsupported", "no live rpc runner to steer")
            runner.steer(text)
            return self._status_view(task)
        if task["status"] not in ("awaiting_review", "needs_info", "blocked", "failed"):
            raise EngineError("invalid_state",
                              f"feedback needs awaiting_review, needs_info, blocked or failed; got {task['status']}")
        chosen = task["model"]
        if model is not None:
            alias = models.resolve(self.cfg.aliases, None, self.cfg.model_default, model)
            chosen = alias.alias
        latest = self.store.last_round(task_id)
        if (latest["n"] if latest else 0) >= task["max_rounds"]:
            # the hard stop does not depend on what the worker claims: no round beyond max_rounds, ever
            self._bump(task_id, "needs_human", error=f"max_rounds ({task['max_rounds']}) reached")
            raise EngineError("max_rounds", f"the task already used its {task['max_rounds']} rounds; it needs a human")
        self._bump(task_id, "running", model=chosen)
        self._start_round(task_id, feedback=text)
        return self._status_view(self.store.get_task(task_id))

    # ------------------------------------------------------------------ cancel / close

    def cancel(self, agent, task_id):
        task = self.store.get_task(task_id, agent)
        if task is None:
            raise EngineError("not_found", f"no task {task_id!r}")
        if task["status"] in ("closed", "cancelled"):
            return self._status_view(task)
        with self._runners_lock:
            runner = self._runners.pop(task_id, None)
        if runner is not None:
            runner.cancel()
        return self._status_view(self._bump(task_id, "cancelled"))

    def close(self, agent, task_id):
        task = self.store.get_task(task_id, agent)
        if task is None:
            raise EngineError("not_found", f"no task {task_id!r}")
        if task["status"] in ("closed", "cancelled"):
            return self._status_view(task)
        self._maybe_remove_worktree(task)
        return self._status_view(self._bump(task_id, "closed"))

    def _maybe_remove_worktree(self, task):
        """Only when the operator opted in AND the branch reached the base: unlock then remove the
        worktree. The branch and the evidence directory always stay."""
        if not self.cfg.remove_merged_worktrees:
            return
        repo = f"/sandbox/workspace/{task['project']}"
        try:
            done = self._exec(GIT + ["merge-base", "--is-ancestor", task["branch"], task["base_branch"]],
                              workdir=repo, timeout=60)
            if done.returncode != 0:
                return
            self._exec_checked(GIT + ["worktree", "unlock", task["worktree"]], workdir=repo, what="worktree unlock")
            # --force: the worker leaves untracked files behind; the branch and evidence dir stay
            self._exec_checked(GIT + ["worktree", "remove", "--force", task["worktree"]], workdir=repo,
                               what="worktree remove")
            log.info("task=%s merged worktree removed", task["task_id"])
        except ShellError as error:
            log.warning("task=%s merged worktree cleanup failed: %s", task["task_id"], error)

    # ------------------------------------------------------------------ reads

    def status(self, agent, task_id):
        task = self.store.get_task(task_id, agent)
        if task is None:
            raise EngineError("not_found", f"no task {task_id!r}")
        return self._status_view(task)

    def result(self, agent, task_id, round_n=None):
        view = self.status(agent, task_id)
        task = self.store.get_task(task_id, agent)
        if round_n is None:
            latest = self.store.last_round(task_id)
            round_n = latest["n"] if latest else 0
        round_row = self.store.round_by_n(task_id, round_n)
        checks = self.store.get_checks(task_id, round_n)
        view.update({"round": round_n,
                     "last_message": (round_row["last_message"] or "")[:RESULT_MESSAGE_CAP] if round_row else "",
                     "checks": [{**self._check_view(c)} for c in checks],
                     "git": self._git_summary(task)})
        return view

    @staticmethod
    def _check_view(row):
        return {"name": row["name"], "ok": bool(row["ok"]), "exit_code": row["exit_code"],
                "output_tail": row["output_tail"]}

    def _git_summary(self, task):
        repo = task["worktree"]
        summary = {"diff_stat": "", "commits": []}
        try:
            done = self._exec(GIT + ["diff", "--stat", task["base_commit"]], workdir=repo, timeout=60)
            summary["diff_stat"] = done.stdout.decode("utf-8", "replace")[-4000:]
            done = self._exec(GIT + ["log", "--oneline", f"{task['base_commit']}..HEAD"], workdir=repo, timeout=60)
            summary["commits"] = done.stdout.decode("utf-8", "replace").strip().splitlines()[:50]
        except ShellError as error:
            summary["error"] = str(error)
        return summary

    def list_tasks(self, agent):
        return [self._status_view(task) for task in self.store.list_tasks(agent)]

    # ------------------------------------------------------------------ projects

    def create_project(self, agent, name, description=""):
        """A new git repository in the shared workspace: branch main, one seed commit (README.md) made as the CREATING agent's git
        identity. It only ever creates a path that does not exist yet, so it cannot touch an existing project; the per-project lock
        serialises two agents racing for one name. A brand-new project has no remote, so it commits as the agent's Forgejo identity
        unless the operator pinned that name to github in bridge.toml."""
        if not isinstance(name, str) or not PROJECT_RE.match(name):
            raise EngineError("invalid_request", f"project name {name!r} must match {PROJECT_RE.pattern} (a lowercase slug)")
        description = (description or "").strip()
        if len(description) > 500 or "\x00" in description:
            raise EngineError("invalid_request", "description must be at most 500 characters")
        path = f"/sandbox/workspace/{name}"
        forge = self.cfg.project_pins.get(name) or "forgejo"
        ident_name, ident_email = self.cfg.identity_for(agent, forge)
        git_env = ["env", "GIT_OPTIONAL_LOCKS=0", *identity.env_for(ident_name, ident_email)]
        readme = f"# {name}\n\n{description}\n" if description else f"# {name}\n"
        with self._project_lock(name):
            self._ensure_ready()
            try:
                taken = self._exec(["sh", "-c", 'test -e "$1" || test -L "$1"', "sh", path], timeout=30).returncode == 0
            except ShellError as error:
                raise EngineError("worker_unavailable", f"could not reach the worker: {error}") from None
            if taken:
                raise EngineError("already_exists", f"project {name!r} already exists; use it with submit_task or pick another name")
            try:
                self._exec_checked(GIT + ["init", "-q", "-b", "main", path], what="git init")
                self._exec_checked(GIT + ["-C", path, "config", "core.sharedRepository", "group"], what="git config")
                self._exec_checked(["tee", f"{path}/README.md"], stdin=readme.encode("utf-8"), what="write README.md")
                self._exec_checked([*git_env, "git", "-c", "safe.directory=*", "-C", path, "add", "README.md"], what="git add")
                self._exec_checked([*git_env, "git", "-c", "safe.directory=*", "-C", path, "commit", "-q", "-m",
                                    f"Initial commit (project created through omp_bridge by {agent})"], what="git commit")
                head = self._exec_checked(GIT + ["-C", path, "rev-parse", "--short", "HEAD"], what="git rev-parse")
            except ShellError as error:
                # we proved the path did not exist a moment ago, under the project lock: removing it only removes our own half-made repo
                try:
                    self._exec(["rm", "-rf", "--", path], timeout=60)
                except ShellError:
                    log.warning("project=%s half-created repository could not be removed", name)
                raise EngineError("worker_unavailable", f"could not create the project: {error}") from None
            self.store.kv_set(f"forge-seen:{name}", forge)
            self.store.kv_set(f"project-owner:{name}", agent)
        commit = head.stdout.decode("utf-8", "replace").strip()
        log.info("project=%s created by %s as %s <%s> (%s)", name, agent, ident_name, ident_email, commit)
        return {"project": name, "path": path, "branch": "main", "commit": commit,
                "committed_as": f"{ident_name} <{ident_email}>", "created_by": agent}

    def list_projects(self, agent):
        """Every project (a git repository directly under the workspace) that submit_task can use: all agents see the same list, because
        the workspace files are shared. Only names that match the project rule are listed."""
        script = ('for d in "$1"/*/; do [ -d "${d}.git" ] || continue; n=$(basename "$d"); '
                  'printf "%s\\t%s\\n" "$n" "$(git -c safe.directory="*" -C "$d" log -1 --format="%h%x09%cI%x09%s" main 2>/dev/null)"; done')
        try:
            done = self._exec(["sh", "-c", script, "sh", "/sandbox/workspace"], timeout=60)
        except ShellError as error:
            raise EngineError("worker_unavailable", f"could not reach the worker: {error}") from None
        if done.returncode != 0:
            raise EngineError("worker_unavailable", "listing the workspace failed: " + done.stderr.decode("utf-8", "replace")[-200:])
        projects = []
        for line in done.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\t")
            if not parts or not PROJECT_RE.match(parts[0]):
                continue
            name = parts[0]
            forge = self.cfg.project_pins.get(name) or self.store.kv_get(f"forge-seen:{name}") or "forgejo"
            ident_name, ident_email = self.cfg.identity_for(agent, forge)
            last = {"hash": parts[1], "date": parts[2], "subject": "\t".join(parts[3:])} if len(parts) >= 4 and parts[1] else None
            projects.append({"name": name, "last_commit_on_main": last,
                             "created_by": self.store.kv_get(f"project-owner:{name}"),
                             "commits_as_you": f"{ident_name} <{ident_email}>"})
        return sorted(projects, key=lambda p: p["name"])

    # ------------------------------------------------------------------ models

    def list_models(self, agent):
        return {"default": self.cfg.model_default, "your_default": self.store.agent_default_model(agent),
                "models": [{"alias": name, "description": alias.description, "enabled": alias.enabled}
                           for name, alias in sorted(self.cfg.aliases.items())]}

    def set_default_model(self, agent, alias_name):
        models.resolve(self.cfg.aliases, None, self.cfg.model_default, alias_name)
        self.store.set_agent_default_model(agent, alias_name)
        return {"your_default": alias_name}

    # ------------------------------------------------------------------ wait + recovery

    def wait(self, agent, task_id, after, timeout):
        task = self.store.get_task(task_id, agent)
        if task is None:
            raise EngineError("not_found", f"no task {task_id!r}")
        deadline = time.monotonic() + min(timeout, self.cfg.wait_timeout_cap_s)
        with self._cond:
            while True:
                task = self.store.get_task(task_id, agent)
                if task["revision"] > after and task["status"] not in ("queued", "running"):
                    return {"task_id": task_id, "revision": task["revision"], "status": task["status"],
                            "round": task["round"]}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"task_id": task_id, "revision": task["revision"], "status": task["status"],
                            "round": task["round"]}
                self._cond.wait(timeout=min(remaining, 5.0))

    def recover(self):
        """Restart recovery: a round that was live when the service died becomes failed (resumable)."""
        for task in self.store.live_tasks():
            self._bump(task["task_id"], "failed", error="service_restart")
            log.warning("task=%s recovered as failed (service_restart)", task["task_id"])
        try:
            self.model_syncer.refresh()
        except Exception:
            log.warning("models.yml refresh at startup failed; will retry on the next task", exc_info=True)

    def stop(self):
        self._stopping.set()
        with self._runners_lock:
            runners = list(self._runners.values())
            self._runners.clear()
        for runner in runners:
            try:
                runner.cancel()
            except Exception:
                pass
        deadline = time.monotonic() + 10
        for thread in self._round_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        self._notify()
