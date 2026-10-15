"""The MCP/HTTP surface against a live server on a random port: auth, JSON-RPC conformance, the seven
tools plus the model tools, error shapes, GET /mcp 405, request caps and the /v1/wait long-poll."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from bridge import config, mcp_http
from fakes import Bridge, token_for


class McpClient:
    """A minimal streamable-HTTP JSON-RPC client (the official-SDK proof is the smoke script)."""

    def __init__(self, base, agent):
        self.base = base
        self.header = {"Authorization": f"Bearer {token_for(agent)[1]}",
                       "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        self.seq = 0

    def call(self, method, params=None, raw_headers=None):
        self.seq += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.seq, "method": method,
                           "params": params or {}}).encode()
        request = urllib.request.Request(self.base + "/mcp", data=body,
                                         headers=raw_headers or self.header, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or self.header, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


class TestMcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bridge = Bridge(cls.tmp.name, limits={"max_active_tasks": 16})   # many tests share one bridge
        cls.bridge.make_project("demo")
        cls.server = mcp_http.BridgeServer(("127.0.0.1", 0), cls.bridge.cfg, cls.bridge.engine,
                                           mcp_http.Authenticator(config.load_tokens(
                                               cls.bridge.cfg.tokens_file, cls.bridge.cfg.forgejo_identities)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.bridge.stop()
        cls.tmp.cleanup()

    def test_initialize_echoes_a_supported_protocol_version(self):
        client = McpClient(self.base, "alpha")
        status, reply = client.call("initialize", {"protocolVersion": "2025-03-26",
                                                   "capabilities": {}, "clientInfo": {"name": "t"}})
        self.assertEqual(status, 200)
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        status, reply = client.call("initialize", {"protocolVersion": "1900-01-01"})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "omp-bridge")

    def test_notifications_initialized_returns_202(self):
        client = McpClient(self.base, "alpha")
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()
        request = urllib.request.Request(self.base + "/mcp", data=body,
                                         headers=client.header, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(response.read(), b"")

    def test_ping_and_tools_list_schemas(self):
        client = McpClient(self.base, "alpha")
        _, reply = client.call("ping")
        self.assertEqual(reply["result"], {})
        _, reply = client.call("tools/list")
        names = {tool["name"] for tool in reply["result"]["tools"]}
        self.assertEqual(names, {"submit_task", "send_feedback", "task_status", "task_result",
                                 "cancel_task", "close_task", "list_tasks", "list_models",
                                 "set_default_model", "create_project", "list_projects"})
        submit = next(tool for tool in reply["result"]["tools"] if tool["name"] == "submit_task")
        self.assertIn("done_criteria", submit["inputSchema"]["required"])
        self.assertEqual(submit["inputSchema"]["properties"]["mode"]["default"], "batch")

    def test_get_mcp_is_405(self):
        status, _ = McpClient(self.base, "alpha").get("/mcp")
        self.assertEqual(status, 405)

    def test_auth_failures(self):
        no_header = urllib.request.Request(self.base + "/mcp", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(no_header, timeout=10)
        self.assertEqual(caught.exception.code, 401)
        digest, _ = token_for("alpha")
        bad = McpClient(self.base, "alpha")
        status, _ = bad.call("ping", raw_headers={"Authorization": "Bearer wrong",
                                                  "Content-Type": "application/json"})
        self.assertEqual(status, 401)
        # the token digest itself is never accepted as a bearer value
        status, _ = bad.call("ping", raw_headers={"Authorization": f"Bearer {digest}",
                                                  "Content-Type": "application/json"})
        self.assertEqual(status, 401)

    def test_unknown_method_and_tool_errors(self):
        client = McpClient(self.base, "alpha")
        _, reply = client.call("bogus/method")
        self.assertEqual(reply["error"]["code"], -32601)
        _, reply = client.call("tools/call", {"name": "no_such_tool", "arguments": {}})
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(payload["code"], "unknown_tool")

    def test_submit_status_result_close_flow(self):
        self.bridge.set_omp(action="write:out.txt:mcp")
        client = McpClient(self.base, "alpha")
        _, reply = client.call("tools/call", {"name": "submit_task", "arguments": {
            "project": "demo", "spec": "create out.txt",
            "done_criteria": {"checks": [{"name": "verify", "argv": ["test", "-f", "out.txt"]}]}}})
        self.assertNotIn("isError", reply["result"])
        submitted = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(submitted["status"], "running")
        self.assertEqual(submitted["branch"], f"omp/{submitted['task_id']}")
        task = self.bridge.wait_status(submitted["task_id"], {"awaiting_review"})
        _, reply = client.call("tools/call", {"name": "task_status", "arguments":
                                              {"task_id": submitted["task_id"]}})
        status = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(status["status"], "awaiting_review")
        self.assertEqual(status["worker_status"], "done")
        by_name = {check["name"]: check for check in status["checks"]}   # the bridge adds a built-in "git identity" check to every round
        self.assertTrue(by_name["verify"]["ok"])
        self.assertTrue(by_name["git identity"]["ok"])
        self.assertIn("evidence_dir", status)
        _, reply = client.call("tools/call", {"name": "task_result", "arguments":
                                              {"task_id": submitted["task_id"]}})
        result = json.loads(reply["result"]["content"][0]["text"])
        self.assertIn("diff_stat", result["git"])
        self.assertIn("omp-bridge-status", result["last_message"])
        _, reply = client.call("tools/call", {"name": "close_task", "arguments":
                                              {"task_id": submitted["task_id"]}})
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["status"], "closed")

    def test_submit_validation_errors(self):
        client = McpClient(self.base, "alpha")
        _, reply = client.call("tools/call", {"name": "submit_task", "arguments": {
            "project": "Bad Name", "spec": "x", "done_criteria": {"checks": []}}})
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload["code"], "invalid_request")
        _, reply = client.call("tools/call", {"name": "submit_task", "arguments": {
            "project": "demo", "spec": "x", "done_criteria": {"checks": [{"name": "c", "argv": []}]}}})
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload["code"], "invalid_request")
        _, reply = client.call("tools/call", {"name": "submit_task", "arguments": {
            "project": "demo", "spec": "x", "done_criteria": {"checks": []}, "model": "nope"}})
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload["code"], "unknown_model")
        self.assertIn("local", payload["message"])

    def test_per_agent_isolation_over_mcp(self):
        client = McpClient(self.base, "alpha")
        other = McpClient(self.base, "charlie")
        self.bridge.set_omp(action="write:out.txt:iso")
        _, reply = client.call("tools/call", {"name": "submit_task", "arguments": {
            "project": "demo", "spec": "s", "done_criteria": {"checks": []}}})
        task_id = json.loads(reply["result"]["content"][0]["text"])["task_id"]
        _, reply = other.call("tools/call", {"name": "task_status", "arguments": {"task_id": task_id}})
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload["code"], "not_found")
        _, reply = other.call("tools/call", {"name": "list_tasks", "arguments": {}})
        tasks = json.loads(reply["result"]["content"][0]["text"])["tasks"]
        self.assertEqual(tasks, [])
        _, reply = client.call("tools/call", {"name": "list_tasks", "arguments": {}})
        tasks = json.loads(reply["result"]["content"][0]["text"])["tasks"]
        self.assertEqual([t["task_id"] for t in tasks], [task_id])
        self.assertIn("revision", tasks[0])

    def test_model_tools(self):
        client = McpClient(self.base, "alpha")
        _, reply = client.call("tools/call", {"name": "list_models", "arguments": {}})
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload["default"], "local")
        self.assertIsNone(payload["your_default"])
        self.assertNotIn("base_url", json.dumps(payload))
        self.assertNotIn("api_key_env", json.dumps(payload))
        _, reply = client.call("tools/call", {"name": "set_default_model", "arguments": {"alias": "mock"}})
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["your_default"], "mock")
        _, reply = client.call("tools/call", {"name": "set_default_model", "arguments": {"alias": "nope"}})
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["code"], "unknown_model")

    def test_request_size_cap(self):
        _, token = token_for("alpha")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "list_tasks", "arguments": {"pad": "x" * (2 * 1024 * 1024)}}}
                          ).encode()
        request = urllib.request.Request(self.base + "/mcp", data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        # The server answers 413 without draining the oversized body, so a loaded client may see the connection reset
        # (broken pipe) before it reads the reply. Both outcomes mean the cap held; a 2xx never may.
        try:
            urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as caught:
            self.assertEqual(caught.code, 413)
        except (urllib.error.URLError, ConnectionError, BrokenPipeError, TimeoutError):
            pass
        else:
            self.fail("an oversized request was accepted")

    def test_wait_endpoint_long_poll(self):
        self.bridge.set_omp(action="write:out.txt:wait")
        client = McpClient(self.base, "alpha")
        _, reply = client.call("tools/call", {"name": "submit_task", "arguments": {
            "project": "demo", "spec": "s", "done_criteria": {"checks": []}}})
        task_id = json.loads(reply["result"]["content"][0]["text"])["task_id"]
        task = self.bridge.wait_status(task_id, {"awaiting_review"})
        status, body = client.get(f"/v1/wait/{task_id}?after=0&timeout=5")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "awaiting_review")
        self.assertGreater(body["revision"], 0)
        # after the current revision and no change: the full timeout elapses, same body shape
        import time as time_module
        start = time_module.monotonic()
        status, body = client.get(f"/v1/wait/{task_id}?after={task['revision']}&timeout=1")
        self.assertGreaterEqual(time_module.monotonic() - start, 1)
        self.assertEqual(body["status"], "awaiting_review")
        status, _ = client.get(f"/v1/wait/{task_id}", headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_no_secret_values_anywhere_in_replies(self):
        client = McpClient(self.base, "alpha")
        _, reply = client.call("tools/call", {"name": "list_models", "arguments": {}})
        self.assertNotIn("LOCAL_RELAY_CLIENT_TOKEN", json.dumps(reply))
        self.assertNotIn("token-alpha", json.dumps(reply))
        self.assertNotIn(token_for("alpha")[1], json.dumps(reply))


if __name__ == "__main__":
    unittest.main()
