"""The batch back end: one `openshell sandbox exec` per round, running omp headless in the worker.

Per round exactly one exec: the omp argv with the prompt on stdin, stdout streamed as JSONL events to a
local round file that the engine uploads to the task's evidence directory. The session id comes from the
first {"type":"session", ...} header event and the round's last assistant message from the last
message_end with role "assistant"; both are derived defensively because the event shapes are only as
stable as the installed omp (observed on 18.10.4, targeted 18.9.2 in the worker image).
"""
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .shell import worker_argv

log = logging.getLogger("bridge.batch")

ENV_PREFIX = ["env", "HOME=/sandbox/.home", "PI_CODING_AGENT_DIR=/sandbox/.omp-agent", "PI_NO_TITLE=1"]
GIT_ENV = ["env", "GIT_OPTIONAL_LOCKS=0"]


@dataclass
class RoundOutcome:
    """What one round produced, however it ended. `error` is an infrastructure failure, not a worker verdict."""
    events_path: str = ""
    session_id: Optional[str] = None
    session_file: Optional[str] = None
    last_message: Optional[str] = None
    stop_reason: Optional[str] = None
    timed_out: bool = False
    exit_code: Optional[int] = None
    error: Optional[str] = None
    events: list = field(default_factory=list)   # parsed events, capped in memory


MAX_EVENTS_KEPT = 5000
EVENT_LINE_CAP = 1024 * 1024


def build_argv(cfg, task, alias, continuing, git_env=()):
    argv = [*ENV_PREFIX, *git_env, "omp", "-p", "--mode", "json", "--yolo", "--no-title",
            "--max-time", str(cfg.round_max_time_s), "--session-dir", f"/sandbox/.omp-sessions/{task['task_id']}",
            "--model", alias.model]
    if continuing:
        # VERIFIED on 18.9.2: --continue with the per-task session dir resumes the same history,
        # including across a model change; --resume by id was not tested and is not used.
        argv += ["--continue"]
    argv += cfg.context_flags()
    argv += ["--cwd", task["worktree"]]
    return argv


def summarize_events(events):
    """(session_id, last assistant text, stop_reason) from a parsed event stream. Absence-tolerant:
    every value is None when the stream does not carry it."""
    session_id, last_text, stop_reason = None, None, None
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "session" and session_id is None and isinstance(event.get("id"), str):
            session_id = event["id"]
        elif kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = "".join(part.get("text", "") for part in message.get("content") or []
                               if isinstance(part, dict) and part.get("type") == "text")
                if text:
                    last_text = text
                stop_reason = message.get("stopReason") or stop_reason
        elif kind == "agent_end" and last_text is None:
            for message in event.get("messages") or []:
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = "".join(part.get("text", "") for part in message.get("content") or []
                                   if isinstance(part, dict) and part.get("type") == "text")
                    if text:
                        last_text = text
    return session_id, last_text, stop_reason


def provider_error(events):
    """The model provider's own error text from a JSONL event stream, or None. omp reports a failed model call as an assistant
    message with stopReason "error" and an errorMessage, and as auto_retry_end.finalError when it gives up retrying; both carry the
    provider's words (PROBED: a hosted provider's 429 with its reset time). The last one seen wins."""
    found = None
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "auto_retry_end" and event.get("success") is False and isinstance(event.get("finalError"), str):
            found = event["finalError"]
        message = event.get("message") if event.get("type") in ("message_end", "turn_end") else None
        if isinstance(message, dict) and message.get("stopReason") == "error" and isinstance(message.get("errorMessage"), str):
            found = message["errorMessage"]
    return found.strip()[:400] if found else None

class BatchRunner:
    """One batch round. `run` blocks for the whole round; `cancel` kills the exec from another thread."""

    def __init__(self, shell, cfg, task, alias, continuing, prompt, events_path, git_env=()):
        self.shell, self.cfg, self.task, self.alias = shell, cfg, task, alias
        self.continuing, self.prompt, self.events_path = continuing, prompt, events_path
        self.git_env = tuple(git_env)
        self._exec = None
        self._cancelled = threading.Event()

    def _pidfile(self):
        return f"/sandbox/rpc/{self.task['task_id']}.batch.pid"

    def cancel(self):
        self._cancelled.set()
        if self._exec is not None:
            self._exec.kill()
        self.shell.kill_remote(self._pidfile())

    def run(self):
        argv = build_argv(self.cfg, self.task, self.alias, self.continuing, self.git_env)
        outcome = RoundOutcome(events_path=self.events_path)
        deadline = time.monotonic() + self.cfg.round_max_time_s + self.cfg.exec_grace_s
        try:
            self._exec = self.shell.stream(worker_argv(argv, self._pidfile()), workdir="/sandbox",
                                           stdin_bytes=self.prompt.encode("utf-8"))
        except Exception as error:  # the CLI itself could not start: the task cannot run
            outcome.error = f"exec_start: {error}"
            return outcome
        events = []
        timed_out = False
        with open(self.events_path, "w", encoding="utf-8") as capture:
            while True:
                if self._cancelled.is_set():
                    outcome.error = "cancelled"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._exec.kill()
                    self.shell.kill_remote(self._pidfile())
                    outcome.error = "round_timeout"
                    break
                try:
                    line = self._exec.lines.get(timeout=min(5.0, remaining))
                except queue.Empty:
                    if self._exec.dead:
                        break
                    continue
                if line is None:  # EOF
                    break
                capture.write(line if line.endswith("\n") else line + "\n")
                line = line.strip()
                if not line or len(line) > EVENT_LINE_CAP:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                events.append(event)
                if len(events) > MAX_EVENTS_KEPT:
                    events.pop(0)
        outcome.exit_code = self._exec.wait(timeout=30)
        if outcome.exit_code is None:
            self._exec.kill()
            self.shell.kill_remote(self._pidfile())
            outcome.error = outcome.error or "exec did not exit after EOF"
        outcome.timed_out = timed_out
        outcome.events = events
        outcome.session_id, outcome.last_message, outcome.stop_reason = summarize_events(events)
        failed_call = provider_error(events) if outcome.last_message is None else None
        if not outcome.error and failed_call:
            outcome.error = f"model provider error (omp exit {outcome.exit_code}): {failed_call}"
        elif not outcome.error and outcome.exit_code not in (0, None) and outcome.last_message is None:
            outcome.error = f"omp exited {outcome.exit_code}: {self._exec.stderr_tail.decode('utf-8', 'replace')[-300:]}"
        log.info("task=%s round finished via=batch model=%s exit=%s timed_out=%s session=%s last_message=%s",
                 self.task["task_id"], self.alias.alias, outcome.exit_code, outcome.timed_out,
                 outcome.session_id, "present" if outcome.last_message else "absent")
        return outcome
