#!/usr/bin/env python3
"""Run the whole delegation loop on your laptop, with no sandbox runtime and no model.

It starts the real bridge (real MCP server, real task engine, real git worktrees) against a *fake* sandbox CLI and a *fake* omp, both
shipped in tests/. An "agent" then talks to it over MCP exactly as a real chat agent would:

    list tools -> create_project -> submit_task -> task_status (checks) -> task_result -> close_task

Nothing here proves anything about a real model or a real sandbox; it shows the contract and the evidence the bridge produces.

    python3 examples/quickstart.py
"""
import json
import pathlib
import sys
import tempfile
import threading
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fakes import Bridge, token_for  # noqa: E402  (tests/fakes.py also puts the repo root on sys.path)
from bridge import config, mcp_http  # noqa: E402


def rpc(base, agent, method, params=None, _ids=[0]):
    _ids[0] += 1
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": _ids[0], "method": method, "params": params or {}}).encode(),
        headers={"Authorization": f"Bearer {token_for(agent)[1]}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def tool(base, agent, tool_name, /, **arguments):
    reply = rpc(base, agent, "tools/call", {"name": tool_name, "arguments": arguments})
    return json.loads(reply["result"]["content"][0]["text"])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        bridge = Bridge(tmp)
        server = mcp_http.BridgeServer(("127.0.0.1", 0), bridge.cfg, bridge.engine,
                                       mcp_http.Authenticator(config.load_tokens(bridge.cfg.tokens_file,
                                                                                  bridge.cfg.forgejo_identities)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        agent = "alpha"
        try:
            names = [t["name"] for t in rpc(base, agent, "tools/list")["result"]["tools"]]
            print("1. tools the agent sees:", ", ".join(names))

            project = tool(base, agent, "create_project", name="demo", description="quickstart project")
            print("2. create_project ->", json.dumps(project)[:160])

            bridge.set_omp(action="commit:hello.txt:hello from the fake worker")   # what the fake omp does in its round
            submitted = tool(base, agent, "submit_task", project="demo", spec="Create hello.txt containing a greeting.",
                             done_criteria={"checks": [{"name": "hello.txt exists", "argv": ["test", "-f", "hello.txt"]}]})
            print("3. submit_task ->", submitted["task_id"], "on branch", submitted["branch"])

            bridge.wait_status(submitted["task_id"], {"awaiting_review"})
            status = tool(base, agent, "task_status", task_id=submitted["task_id"])
            print("4. task_status ->", status["status"], "| worker said:", status["worker_status"])
            for check in status["checks"]:
                print("     check:", ("PASS" if check["ok"] else "FAIL"), check["name"])

            result = tool(base, agent, "task_result", task_id=submitted["task_id"])
            print("5. task_result -> commits:", result["git"]["commits"], "| diff:", result["git"]["diff_stat"].strip().replace("\n", " | "))

            evidence = bridge.mapped("/sandbox/workspace/.omp-bridge") / submitted["task_id"]
            print("6. evidence written for the human reviewer:", ", ".join(sorted(p.name for p in evidence.iterdir())))

            print("7. close_task ->", tool(base, agent, "close_task", task_id=submitted["task_id"])["status"])
            other = tool(base, "charlie", "list_tasks")
            print("8. another agent's list_tasks is empty (isolation):", other.get("tasks") == [] or other)
        finally:
            server.shutdown()
            server.server_close()
            bridge.stop()


if __name__ == "__main__":
    main()
