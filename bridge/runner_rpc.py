"""The session (rpc) back end, shaped by what a real `openshell sandbox exec` on 18.9.2 actually does
(a live probe, VERIFIED on omp 18.9.2): an exec does NOT stream — stdin goes in as
one blob at EOF and no output returns until then, so an interactive rpc child can never work. What works
is an in-sandbox daemon (bridge/assets/rpcd.sh, uploaded to /sandbox/rpc/rpcd.sh): omp --mode rpc reads a
FIFO held open read-write, appends frames to out.jsonl, and survives the exec that started it. The bridge
sends one frame per short exec (`cat > <dir>/in`) and polls `tail -c +<offset+1> <dir>/out.jsonl` about
every 0.7 s, consuming only complete lines. Liveness is `kill -0 $(cat <dir>/pid)`; a dead daemon is
restarted with `--resume <sessionFile>` from the last get_state.

Frames are the custom JSONL protocol from omp://rpc.md, not JSON-RPC 2.0. rpc_chunk frames are handled
only when protocol v2 was negotiated. Everything shape-sensitive is defensive: a missing ready frame,
a dead pid or a truncated tail is an error, not a crash.
"""
import base64
import json
import logging
import time
from typing import Optional

from .runner_batch import ENV_PREFIX, RoundOutcome, summarize_events

log = logging.getLogger("bridge.rpc")

READY_TIMEOUT_S = 60
REQUEST_TIMEOUT_S = 60
POLL_S = 0.7
SEND_TIMEOUT_S = 30
MAX_EVENTS_KEPT = 5000
MAX_REASSEMBLED = 64 * 1024 * 1024

DAEMON_SCRIPT = "/sandbox/rpc/rpcd.sh"

# The daemon path, task dir and omp args all arrive as positional parameters so every path stays a
# discrete argv element (the test fake maps /sandbox paths by argv; the real sandbox needs no mapping).
START_SH = 'DAEMON="$1"; shift; (setsid nohup sh "$DAEMON" "$@" >/dev/null 2>&1 </dev/null &)'
ALIVE_SH = 'kill -0 "$(cat "$1/pid" 2>/dev/null)" 2>/dev/null'
KILL_SH = 'kill "$(cat "$1/pid" 2>/dev/null)" 2>/dev/null; rm -f "$1/pid"'


class RpcError(Exception):
    """The rpc daemon or the protocol failed. `code` is machine-readable."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class _ChunkDecoder:
    """Reassembles one uninterrupted rpc_chunk sequence into its logical JSON object (protocol v2)."""

    def __init__(self, max_bytes=MAX_REASSEMBLED):
        self.max_bytes = max_bytes
        self.chunk_id: Optional[str] = None
        self.parts: list = []
        self.count: Optional[int] = None
        self.byte_length: Optional[int] = None

    def reset(self):
        self.chunk_id, self.parts, self.count, self.byte_length = None, [], None, None

    def feed(self, frame):
        chunk_id = frame.get("chunkId")
        try:
            index, count, byte_length = int(frame["index"]), int(frame["count"]), int(frame["byteLength"])
            data = base64.b64decode(frame["data"], validate=True)
        except (KeyError, TypeError, ValueError):
            self.reset()
            raise RpcError("rpc_chunk", "rpc_chunk frame is malformed")
        if self.chunk_id is None:
            if index != 0:
                self.reset()
                raise RpcError("rpc_chunk", "rpc_chunk sequence does not start at index 0")
            self.chunk_id, self.count, self.byte_length = chunk_id, count, byte_length
            self.parts = []
        elif chunk_id != self.chunk_id or count != self.count or byte_length != self.byte_length:
            self.reset()
            raise RpcError("rpc_chunk", "interleaved or inconsistent rpc_chunk sequence")
        if index != len(self.parts):
            self.reset()
            raise RpcError("rpc_chunk", "rpc_chunk sequence is interrupted")
        self.parts.append(data)
        if self.count is None or self.byte_length is None:
            self.reset()
            raise RpcError("rpc_chunk", "rpc_chunk state is inconsistent")
        if sum(len(part) for part in self.parts) > self.max_bytes or self.byte_length > self.max_bytes:
            self.reset()
            raise RpcError("rpc_chunk", "rpc_chunk sequence exceeds the reassembly limit")
        if len(self.parts) < self.count:
            return None
        payload = b"".join(self.parts)
        self.reset()
        if len(payload) != self.byte_length:
            raise RpcError("rpc_chunk", "rpc_chunk byteLength mismatch")
        try:
            return json.loads(payload.decode("utf-8", "strict"))
        except (ValueError, UnicodeDecodeError):
            raise RpcError("rpc_chunk", "reassembled rpc_chunk is not valid JSON") from None


class RpcRunner:
    """One daemon dir per task under /sandbox/rpc/<task_id>. Poll-driven; no interactive child."""

    def __init__(self, shell, cfg, task, alias, session_file=None, git_env=()):
        self.shell, self.cfg, self.task, self.alias = shell, cfg, task, alias
        self.dir = f"/sandbox/rpc/{task['task_id']}"
        self.session_file = session_file
        self.git_env = tuple(git_env)   # the daemon is started detached with these variables; every git command omp runs inherits them
        self.session_id = None
        self._offset = 0
        self._frames = []
        self._messages = []           # message_end + agent_end, for summarization and evidence
        self._pending = {}            # request id -> {"response": frame or None}
        self._prompt_wait = None      # {"id", "result", "settled": bool}
        self._protocol = 1
        self._chunks = None
        self._request_seq = 0
        self.dead = True              # becomes False once the daemon answers

    # ----------------------------------------------------------------- exec helpers

    def _exec(self, argv, *, stdin=None, timeout=60):
        return self.shell.exec(argv, workdir="/sandbox", stdin=stdin, timeout=timeout)

    def _sh(self, script, *args, stdin=None, timeout=60, env=()):
        from .shell import worker_argv
        prefix = ["env", *env] if env else []
        done = self._exec(worker_argv([*prefix, "sh", "-c", script, "sh", *args]), stdin=stdin, timeout=timeout)
        return done

    def _omp_args(self):
        # Only the FLAGS: assets/rpcd.sh supplies `env HOME=... omp --mode rpc --no-ui --yolo --no-title` itself. PROBED live
        # 2026-10-14: passing the whole command here produced `omp --mode rpc ... env ... omp --mode rpc ...` and the
        # daemon died before `ready` (the unit tests' fake omp tolerated stray arguments and hid it).
        args = [
                "--session-dir", f"/sandbox/.omp-sessions/{self.task['task_id']}",
                "--model", self.alias.model, *self.cfg.context_flags(), "--cwd", self.task["worktree"]]
        if self.session_file:
            args += ["--resume", self.session_file]
        return args

    # ----------------------------------------------------------------- lifecycle

    def start(self):
        """Start the daemon (resuming the saved session when one is known) and wait for ready."""
        self._sh(START_SH, DAEMON_SCRIPT, self.dir, *self._omp_args(), env=self.git_env)
        self._offset, self._frames = 0, []
        ready = self._until(lambda f: f.get("type") == "ready", READY_TIMEOUT_S)
        if ready is None:
            self.dead = True
            raise RpcError("ready_timeout", "no ready frame from the rpc daemon")
        self.dead = False
        versions = ready.get("supportedProtocolVersions") or []
        if 2 in versions:
            try:
                self._request({"type": "negotiate_protocol", "protocolVersion": 2},
                              command="negotiate_protocol")
                self._protocol = 2
                self._chunks = _ChunkDecoder(int(ready.get("maxReassembledFrameBytes") or MAX_REASSEMBLED))
                log.info("task=%s rpc protocol v2 negotiated", self.task["task_id"])
            except RpcError as error:
                log.warning("task=%s protocol v2 negotiation failed, staying on v1: %s",
                            self.task["task_id"], error.message)
        self._load_state()
        log.info("task=%s rpc daemon ready model=%s session_file=%s", self.task["task_id"],
                 self.alias.alias, self.session_file)

    def _load_state(self):
        state = self._request({"type": "get_state"}, command="get_state")
        data = state.get("data") or {}
        self.session_file = data.get("sessionFile") or self.session_file
        self.session_id = data.get("sessionId") or self.session_id

    def ensure_alive(self):
        """Liveness check plus restart-with-resume on a dead or missing daemon."""
        done = self._sh(ALIVE_SH, self.dir)
        if done.returncode == 0 and not self.dead:
            return
        log.warning("task=%s rpc daemon is down; restarting with --resume", self.task["task_id"])
        self.dead = True
        self.start()   # _omp_args picks up the recorded session_file

    def cancel(self):
        """Stop the daemon: abort whatever it is doing, then kill the omp pid. Best effort."""
        try:
            self.abort()
        except Exception:
            pass
        try:
            self._sh(KILL_SH, self.dir)
        except Exception:
            pass
        self.dead = True

    close = cancel

    # ----------------------------------------------------------------- poll pump

    def _pump_once(self):
        """One poll: fetch the tail after the last consumed byte, consume only complete lines."""
        try:
            done = self._sh('tail -c +"$1" "$2/out.jsonl" 2>/dev/null', str(self._offset + 1), self.dir,
                            timeout=30)
        except Exception as error:
            log.debug("task=%s tail poll failed: %s", self.task["task_id"], error)
            return
        if done.returncode not in (0, 1) or not done.stdout:
            return
        blob = done.stdout
        cut = blob.rfind(b"\n")
        if cut < 0:
            return
        complete = blob[:cut + 1]
        self._offset += len(complete)
        for raw in complete.decode("utf-8", "replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if isinstance(frame, dict):
                self._dispatch(frame)

    def _until(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        index = 0
        while time.monotonic() < deadline:
            self._pump_once()
            while index < len(self._frames):
                frame = self._frames[index]
                index += 1
                if predicate(frame):
                    return frame
            if self._daemon_gone():
                return None
            time.sleep(POLL_S)
        return None

    def _daemon_gone(self):
        if self.dead:
            return False    # caller owns liveness during startup
        try:
            done = self._sh(ALIVE_SH, self.dir)
            return done.returncode != 0
        except Exception:
            return True

    def _dispatch(self, frame):
        kind = frame.get("type")
        if kind == "response":
            slot = self._pending.get(frame.get("id"))
            if slot is not None:
                slot["response"] = frame
        elif kind == "prompt_result":
            wait = self._prompt_wait
            if wait is not None and frame.get("id") == wait["id"]:
                wait["result"] = frame
                if frame.get("sessionSettled"):
                    wait["settled"] = True
        elif kind == "session_settled":
            if self._prompt_wait is not None:
                self._prompt_wait["settled"] = True
        elif kind in ("message_end", "agent_end"):
            self._messages.append(frame)
            if len(self._messages) > MAX_EVENTS_KEPT:
                self._messages.pop(0)
        elif kind == "rpc_chunk":
            if self._chunks is None:
                log.warning("task=%s rpc_chunk on protocol v1: ignored", self.task["task_id"])
                return
            try:
                complete = self._chunks.feed(frame)
            except RpcError as error:
                log.warning("task=%s %s", self.task["task_id"], error.message)
                return
            if complete is not None:
                self._dispatch(complete)
                return
            return
        self._frames.append(frame)

    # ----------------------------------------------------------------- requests

    def _next_id(self, prefix):
        self._request_seq += 1
        return f"{prefix}-{self._request_seq}"

    def send(self, frame):
        """One frame to the daemon's FIFO through a short exec whose stdin is the JSON line."""
        line = json.dumps(frame) + "\n"
        done = self._sh('cat > "$1/in"', self.dir, stdin=line.encode("utf-8"), timeout=SEND_TIMEOUT_S)
        if done.returncode != 0:
            raise RpcError("send", done.stderr.decode("utf-8", "replace").strip()[-200:] or "cat failed")

    def _request(self, payload, command, timeout=REQUEST_TIMEOUT_S):
        if self.dead:
            raise RpcError("daemon_dead", "the rpc daemon is not running")
        request_id = self._next_id(command)
        slot = {"response": None}
        self._pending[request_id] = slot
        try:
            self.send({**payload, "id": request_id})
            end = time.monotonic() + timeout
            while slot["response"] is None and time.monotonic() < end:
                self._pump_once()
                if slot["response"] is None:
                    if self._daemon_gone():
                        raise RpcError("daemon_dead", "the rpc daemon died")
                    time.sleep(POLL_S)
        finally:
            self._pending.pop(request_id, None)
        response = slot["response"]
        if response is None:
            raise RpcError("request_timeout", f"no response to {command}")
        if not response.get("success", False):
            raise RpcError(response.get("code") or "request_failed",
                           f"{command}: {response.get('error') or 'failed'}")
        return response

    # ----------------------------------------------------------------- rounds

    def _summarize(self):
        events = [{"type": "message_end", "message": f["message"]} for f in self._messages
                  if f.get("type") == "message_end"]
        events += [f for f in self._messages if f.get("type") == "agent_end"]
        return summarize_events(events)

    def round(self, message, events_path, follow_up=False):
        """Send `prompt` for EVERY round on the idle daemon; the round ends at the prompt_result for our request plus session
        quiescence. PROBED live 2026-10-14: the rpc `follow_up` command only queues a message for a BUSY agent, and on an
        idle daemon it yields no prompt_result, so a later round that used it waited until round_timeout despite a good
        verdict. (`follow_up` is accepted for call compatibility and ignored.) Returns a RoundOutcome."""
        self.ensure_alive()
        outcome = RoundOutcome(events_path=events_path)
        command = "prompt"
        request_id = self._next_id(command)
        self._prompt_wait = {"id": request_id, "result": None, "settled": False}
        started = len(self._messages)
        try:
            self.send({"id": request_id, "type": command, "message": message})
        except RpcError as error:
            self._prompt_wait = None
            outcome.error = f"send: {error.message}"
            return outcome
        deadline = time.monotonic() + self.cfg.round_max_time_s + self.cfg.exec_grace_s
        timed_out = False
        while self._prompt_wait["result"] is None:
            if time.monotonic() > deadline:
                timed_out = True
                outcome.error = "round_timeout"
                break
            self._pump_once()
            if self._prompt_wait["result"] is None:
                if self._daemon_gone():
                    outcome.error = "daemon_dead"
                    break
                time.sleep(POLL_S)
        wait = self._prompt_wait
        result = wait["result"] or {}
        self._prompt_wait = None
        if not outcome.error and result.get("status") not in ("completed", "aborted", None):
            outcome.error = f"prompt: {result.get('status')}"
        if not outcome.error and result.get("agentInvoked", True) and not wait["settled"]:
            end = time.monotonic() + 30
            while not wait["settled"] and time.monotonic() < end and not self._daemon_gone():
                self._pump_once()
                if not wait["settled"]:
                    time.sleep(POLL_S)
            if not wait["settled"]:
                outcome.error = "no_session_settled"
        outcome.timed_out = timed_out
        outcome.session_id, outcome.last_message, outcome.stop_reason = self._summarize()
        outcome.session_file = self.session_file
        with open(events_path, "w", encoding="utf-8") as capture:
            for frame in self._messages[max(0, started - 1):]:
                capture.write(json.dumps(frame) + "\n")
        log.info("task=%s round finished via=rpc model=%s status=%s session_file=%s last_message=%s",
                 self.task["task_id"], self.alias.alias, (result or {}).get("status"),
                 self.session_file, "present" if outcome.last_message else "absent")
        return outcome

    def steer(self, message):
        """Mid-run steering: fire and forget, valid only while a round is live."""
        self.ensure_alive()
        self.send({"type": "steer", "message": message})

    def abort(self):
        try:
            if not self.dead:
                self.send({"type": "abort"})
        except RpcError:
            pass

    def set_model(self, alias):
        """Switch the model from the next round on (rpc set_model with provider/modelId)."""
        self._request({"type": "set_model", "provider": alias.provider, "modelId": alias.model_id},
                      command="set_model")
        self.alias = alias
        log.info("task=%s rpc set_model %s (%s)", self.task["task_id"], alias.alias, alias.model)
