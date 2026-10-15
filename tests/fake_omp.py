#!/usr/bin/env python3
"""A deterministic fake `omp` for the bridge tests: batch (-p --mode json) and rpc (--mode rpc, driven
through a FIFO by rpcd.sh) with the frame shapes observed on the real 18.9.2. Behaviour is controlled
entirely by env vars set by the test (inherited through the fake openshell):

FAKE_OMP_ACTION   write:<relpath>:<content>  create a file relative to the workdir (default: none)
                  sleep                      hang (cancel/timeout tests)
                  fail                       batch: exit 3 with no events; rpc: prompt_result status error
FAKE_OMP_STATUS   done (default) | needs_info | blocked | unclear   the status block to emit
FAKE_OMP_LOG      path of a jsonl log of every model request (model, n_messages, last_user)
"""
import json
import os
import subprocess
import sys
import time
import uuid

STATUS_BLOCKS = {
    "done": '```omp-bridge-status\n{"status":"done","summary":"implementation complete","questions":[]}\n```',
    "needs_info": '```omp-bridge-status\n{"status":"needs_info","summary":"need clarification",'
                  '"questions":["which file should hold the config?"]}\n```',
    "blocked": '```omp-bridge-status\n{"status":"blocked","summary":"cannot proceed: missing input",'
               '"questions":[]}\n```',
    "unclear": "no status block here",
}


def log_request(model, n_messages, last_user):
    path = os.environ.get("FAKE_OMP_LOG")
    if not path:
        return
    with open(path, "a") as handle:
        handle.write(json.dumps({"model": model, "n_messages": n_messages, "last_user": last_user[:80]}) + "\n")


def prompt_var(prompt, name, default=None):
    """A per-round FAKE_OMP_<name>=<value> line in the prompt wins over the env: the prompt is the one
    thing that is unique to each round, so tests can drive tasks differently without races."""
    marker = f"FAKE_OMP_{name}="
    for line in (prompt or "").splitlines():
        if line.startswith(marker):
            return line[len(marker):].strip()
    return os.environ.get(f"FAKE_OMP_{name}", default)


def do_action(prompt=None):
    action = prompt_var(prompt, "ACTION", "none")
    if action.startswith("write:"):
        _, rel, content = action.split(":", 2)
        target = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w") as handle:
            handle.write(content)
    elif action.startswith(("commit:", "commit-as:")):
        # commit: write a file and commit it with whatever identity the environment gives git (the bridge stamps GIT_* variables);
        # commit-as: the same, but with an explicit --author, the one thing environment variables cannot stop
        kind, rel, content = action.split(":", 2)
        target = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w") as handle:
            handle.write(content)
        extra = ["--author=Mallory <mallory@evil.example>"] if kind == "commit-as" else []
        subprocess.run(["git", "add", "--", rel], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", f"add {rel}", *extra], check=True, capture_output=True)
    elif action == "sleep":
        time.sleep(60)
    return action


def assistant_text(model, n_messages, prompt=None):
    status = prompt_var(prompt, "STATUS", "done")
    return f"model={model} msgs={n_messages} " + STATUS_BLOCKS.get(status, STATUS_BLOCKS["done"])


def parse_args(argv):
    args = {"model": "mock/m-a", "session_dir": "/tmp/sess", "continue": False, "resume": None, "cwd": os.getcwd()}
    i = 0
    while i < len(argv):
        if argv[i] == "--model":
            i += 1
            args["model"] = argv[i]
        elif argv[i] == "--session-dir":
            i += 1
            args["session_dir"] = argv[i]
        elif argv[i] == "--continue":
            args["continue"] = True
        elif argv[i] == "--resume":
            i += 1
            args["resume"] = argv[i]
        elif argv[i] == "--cwd":
            i += 1
            args["cwd"] = argv[i]
        i += 1
    return args


# ------------------------------------------------------------------ batch

def batch(argv):
    if "--version" in argv:
        print("omp/18.9.2-fake")
        return 0
    args = parse_args([a for a in argv if not a.startswith("-") or a in ("--model", "--session-dir", "--cwd")])
    os.makedirs(args["session_dir"], exist_ok=True)
    if os.path.isdir(args["cwd"]):
        os.chdir(args["cwd"])   # real omp starts in --cwd
    prompt = sys.stdin.read()
    do_action(prompt)
    if prompt_var(prompt, "ACTION", "none") == "fail":
        return 3
    model = args["model"]
    # --continue keeps one history per session dir: track the cumulative message count there
    counter = os.path.join(args["session_dir"], "fake-history.json")
    history = 0
    if os.path.exists(counter):
        try:
            history = int(open(counter).read().strip() or 0)
        except ValueError:
            history = 0
    history += 2
    with open(counter, "w") as handle:
        handle.write(str(history))
    log_request(model, history, prompt)
    session = str(uuid.uuid4())
    events = [
        {"type": "session", "version": 3, "id": session, "timestamp": "2026-10-14T00:00:00.000Z",
         "cwd": args["cwd"]},
        {"type": "agent_start"}, {"type": "turn_start"},
        {"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": prompt}],
                                             "attribution": "user", "timestamp": 1}},
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": prompt}],
                                            "attribution": "user", "timestamp": 1}},
        {"type": "message_start", "message": {"role": "assistant", "content": [], "api": "openai-completions",
                                             "provider": model.split("/")[0], "model": model.split("/")[-1],
                                             "usage": {"input": 1, "output": 1, "totalTokens": 2}}},
        {"type": "message_end", "message": {"role": "assistant",
                                            "content": [{"type": "text", "text": assistant_text(model, 2, prompt)}],
                                            "api": "openai-completions", "provider": model.split("/")[0],
                                            "model": model.split("/")[-1],
                                            "usage": {"input": 1, "output": 1, "totalTokens": 2},
                                            "stopReason": "stop", "timestamp": 2}},
        {"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}, "toolResults": []},
        {"type": "agent_end", "messages": [{"role": "assistant",
                                            "content": [{"type": "text", "text": assistant_text(model, 2)}]}],
         "isTerminal": True, "yielded": True},
    ]
    for event in events:
        print(json.dumps(event), flush=True)
    return int(os.environ.get("FAKE_OMP_EXIT", "0"))


# ------------------------------------------------------------------ rpc

class RpcSpeaker:
    def __init__(self, args):
        self.args = args
        self.n = 0
        self.model = args["model"]
        self.session_id = str(uuid.uuid4())
        self.session_file = os.path.join(args["session_dir"], "session-" + self.session_id + ".jsonl")
        self.pending_prompt = None

    def frame(self, obj):
        print(json.dumps(obj), flush=True)

    def run(self):
        self.frame({"type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2],
                    "maxFrameBytes": 1048576, "maxReassembledFrameBytes": 67108864})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                self.frame({"type": "response", "command": "parse", "success": False,
                            "error": "malformed json"})
                continue
            if isinstance(request, dict):
                self.handle(request)

    def respond(self, request, success=True, data=None, error=None):
        frame = {"id": request.get("id"), "type": "response", "command": request.get("type"),
                 "success": success}
        if data is not None:
            frame["data"] = data
        if error is not None:
            frame["error"] = error
        self.frame(frame)

    def handle(self, request):
        kind = request.get("type")
        if kind == "negotiate_protocol":
            self.respond(request, data={"protocolVersion": 2})
        elif kind == "get_state":
            self.respond(request, data={"model": {"id": self.model.split("/")[-1],
                                                  "provider": self.model.split("/")[0]},
                                        "sessionId": self.session_id, "sessionFile": self.session_file,
                                        "isSettled": self.pending_prompt is None})
        elif kind == "open_session":
            self.respond(request, data={"cancelled": False, "resumed": True, "sessionId": self.session_id,
                                        "sessionFile": self.session_file})
        elif kind == "set_model":
            self.model = f"{request['provider']}/{request['modelId']}"
            self.frame({"type": "model_changed", "model": {"id": request["modelId"],
                                                           "provider": request["provider"]}})
            self.respond(request, data={"id": request["modelId"], "provider": request["provider"]})
        elif kind in ("prompt", "follow_up", "abort_and_prompt"):
            self.respond(request)
            self.turn(request)
        elif kind == "steer":
            self.respond(request, data={})
        elif kind == "abort":
            if self.pending_prompt is not None:
                pid = self.pending_prompt
                self.pending_prompt = None
                self.frame({"type": "prompt_result", "id": pid, "agentInvoked": True, "status": "aborted",
                            "sessionSettled": True})
            self.respond(request, data={})
        else:
            self.respond(request, success=False, error=f"unknown command {kind!r}")

    def turn(self, request):
        pid = request.get("id")
        self.pending_prompt = pid
        message = request.get("message", "")
        action = do_action(message)
        self.n += 2
        log_request(self.model, self.n, message)
        self.frame({"type": "agent_start"})
        self.frame({"type": "turn_start"})
        user = {"role": "user", "content": [{"type": "text", "text": message}], "timestamp": 1}
        self.frame({"type": "message_start", "message": user})
        self.frame({"type": "message_end", "message": user})
        failed = action == "fail"
        assistant = {"role": "assistant",
                     "content": [{"type": "text", "text": assistant_text(self.model, self.n, message)}],
                     "api": "openai-completions", "provider": self.model.split("/")[0],
                     "model": self.model.split("/")[-1], "stopReason": "error" if failed else "stop",
                     "timestamp": 2}
        self.frame({"type": "message_start", "message": {**assistant, "content": []}})
        self.frame({"type": "message_end", "message": assistant})
        self.frame({"type": "turn_end", "message": assistant, "toolResults": []})
        self.frame({"type": "agent_end", "messages": [user, assistant], "isTerminal": True, "yielded": True})
        self.pending_prompt = None
        self.frame({"type": "prompt_result", "id": pid, "agentInvoked": True,
                    "status": "error" if failed else "completed", "sessionSettled": True})
        self.frame({"type": "session_settled"})


def rpc(argv):
    args = parse_args([a for a in argv if not a.startswith("-") or a in ("--model", "--session-dir", "--cwd")])
    os.makedirs(args["session_dir"], exist_ok=True)
    if os.path.isdir(args["cwd"]):
        os.chdir(args["cwd"])   # real omp starts in --cwd
    RpcSpeaker(args).run()
    return 0


def main():
    argv = sys.argv[1:]
    if "--mode" in argv and "rpc" in argv[argv.index("--mode") + 1:argv.index("--mode") + 2]:
        return rpc(argv)
    return batch(argv)


if __name__ == "__main__":
    sys.exit(main())
