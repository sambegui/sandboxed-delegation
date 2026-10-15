"""The one way the bridge runs another program (the openshell CLI): a small vendored procs-like helper,
so this package stays standalone and does not import the dispatcher.

The child gets its own session, so a timeout or a cancel kills the whole process group: the openshell CLI
together with the exec stream it holds. A streaming run copies stdin from a buffer, delivers stdout by
lines to a queue, and keeps only a capped stderr tail. Nothing here knows about tokens or provider keys:
the CLI's environment is the service user's ordinary one, and the only secrets anywhere near it live
inside the sandbox, injected by the provider.
"""
import os
import queue
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import IO

STDERR_CAP = 64 * 1024


def worker_argv(argv, pidfile=None):
    """Every argv run inside the worker: umask 002 first so files stay group-writable for the shared host
    group, then exec the real command. Always argv form: the shell strings are fixed literals and the
    command arrives as "$@", never built from user text. With `pidfile` the shell records its pid first;
    `exec` keeps that pid for the real command, so a cancel can kill the worker-side process (killing only
    the local openshell CLI does NOT stop it: PROBED 2026-10-14, the omp kept running in the sandbox)."""
    if pidfile:
        return ["sh", "-c", 'umask 002; pf="$1"; shift; echo $$ > "$pf"; exec "$@"', "sh", pidfile, *argv]
    return ["sh", "-c", 'umask 002; exec "$@"', "sh", *argv]


class ProcTimeout(Exception):
    """The program did not finish in time and was killed with its whole process group."""


class ProcStartError(Exception):
    """The program could not be started at all (missing binary, bad argv)."""


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: bytes


def run(args, *, env, cwd=None, stdin=None, timeout=300) -> Completed:
    """Run `args` and wait. `env` is the complete environment; `stdin` is bytes or None (then /dev/null)."""
    try:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                                   start_new_session=True)
    except OSError as error:
        raise ProcStartError(f"cannot run {args[0]}: {error}") from None
    try:
        out, err = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(process)
        raise ProcTimeout(f"{args[0]} did not finish within {timeout} s") from None
    return Completed(process.returncode, out, err)


def kill_group(process):
    """SIGKILL the child's process group; the CLI and whatever it spawned die together. Best effort."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


class StreamExec:
    """A running child with a writer thread for stdin, a reader thread turning stdout into lines, and a
    stderr tail. Used both for one-shot batch rounds (stdin closed right away) and for the long-lived
    rpc child (stdin stays open across rounds)."""

    def __init__(self, args, *, env, workdir=None, stdin_bytes=None):
        self.args = args
        self.lines = queue.Queue()
        self.stderr_tail = b""
        self.returncode = None
        self._stderr = []
        self._stderr_bytes = 0
        self._done = threading.Event()
        try:
            self.process = subprocess.Popen(args, env=env, cwd=workdir, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, stdin=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            raise ProcStartError(f"cannot run {args[0]}: {error}") from None
        self._stdin_lock = threading.Lock()
        stdout, stderr_pipe, stdin = self.process.stdout, self.process.stderr, self.process.stdin
        assert stdout is not None and stderr_pipe is not None and stdin is not None
        self._stdout: IO[bytes] = stdout
        self._stderr_pipe: IO[bytes] = stderr_pipe
        self._stdin: IO[bytes] = stdin
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        if stdin_bytes is not None:
            self.write(stdin_bytes)
            self.close_stdin()

    def _read_stdout(self):
        try:
            for raw in iter(self._stdout.readline, b""):
                self.lines.put(raw.decode("utf-8", "replace"))
        finally:
            self.lines.put(None)  # EOF marker

    def _read_stderr(self):
        while True:
            chunk = self._stderr_pipe.read(4096)
            if not chunk:
                break
            self._stderr.append(chunk)
            self._stderr_bytes += len(chunk)
            if self._stderr_bytes > STDERR_CAP:
                self._stderr = [b"".join(self._stderr)[-STDERR_CAP // 2:]]
                self._stderr_bytes = STDERR_CAP // 2
        self.stderr_tail = b"".join(self._stderr)

    def write(self, data):
        with self._stdin_lock:
            self._stdin.write(data)
            self._stdin.flush()

    def write_line(self, text):
        self.write(text.encode("utf-8") + b"\n")

    def close_stdin(self):
        with self._stdin_lock:
            try:
                self._stdin.close()
            except (OSError, ValueError):
                pass

    def poll(self):
        self.returncode = self.process.poll()
        if self.returncode is not None:
            self._done.set()
        return self.returncode

    def wait(self, timeout=None):
        try:
            self.returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self._done.set()
        return self.returncode

    def kill(self):
        kill_group(self.process)
        self.returncode = self.process.returncode
        self._done.set()

    @property
    def dead(self):
        return self.poll() is not None


class OpenShell:
    """The `openshell` CLI for one gateway, exactly the calls the bridge needs."""

    def __init__(self, binary, gateway, env=None, sandbox="omp-worker"):
        self.binary, self.gateway = binary, gateway
        self.env = dict(env if env is not None else os.environ)
        self.sandbox = sandbox

    def _argv(self, *args):
        return [self.binary, "-g", self.gateway, *args]

    def run(self, *args, timeout=120, stdin=None):
        """A plain call that must succeed; the stderr tail becomes the error text."""
        try:
            done = run(self._argv(*args), env=self.env, stdin=stdin, timeout=timeout)
        except ProcTimeout:
            raise ShellError("timeout", f"openshell {' '.join(args[:2])} exceeded {timeout} s") from None
        except ProcStartError as error:
            raise ShellError("cli", str(error)) from None
        if done.returncode != 0:
            raise ShellError("cli", done.stderr.decode("utf-8", "replace").strip()[-300:] or
                             f"openshell exited {done.returncode}")
        return done

    def exec(self, argv, *, timeout=120, stdin=None, workdir="/sandbox"):
        """One command in the worker sandbox; returns its Completed result (caller judges the code)."""
        try:
            return run(self._argv("sandbox", "exec", "-n", self.sandbox, "--no-tty", "--no-login-shell", "--workdir",
                                  workdir, "--", *argv),
                       env=self.env, stdin=stdin, timeout=timeout + 30)
        except ProcTimeout:
            raise ShellError("timeout", f"exec {' '.join(argv[:2])} exceeded {timeout} s") from None
        except ProcStartError as error:
            raise ShellError("cli", str(error)) from None

    def stream(self, argv, *, workdir="/sandbox", stdin_bytes=None):
        """A streaming exec in the worker sandbox: the caller reads lines and finishes or kills it."""
        try:
            return StreamExec(self._argv("sandbox", "exec", "-n", self.sandbox, "--no-tty", "--no-login-shell",
                                         "--workdir", workdir, "--", *argv),
                              env=self.env, stdin_bytes=stdin_bytes)
        except ProcStartError as error:
            raise ShellError("cli", str(error)) from None

    def upload(self, local, remote):
        """One local file to a path under /sandbox in the worker."""
        self.run("sandbox", "upload", self.sandbox, local, remote, timeout=300)

    def kill_remote(self, pidfile):
        """Stop the worker-side process whose pid is in `pidfile` (children first, then it, then SIGKILL after
        2 s) and remove the file. Best effort: a dead process or a missing file is fine."""
        script = ('p="$(cat "$1" 2>/dev/null)"; [ -n "$p" ] && { pkill -TERM -P "$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; '
                  'sleep 2; pkill -KILL -P "$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null; }; rm -f "$1"; exit 0')
        try:
            self.exec(["sh", "-c", script, "sh", pidfile], timeout=30)
        except ShellError:
            pass


class ShellError(Exception):
    """An openshell call failed. `code` is a stable machine-readable reason."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail
