"""The bridge's public surface: one ThreadingHTTPServer on 127.0.0.1:18780 (reached by the Hermes cells
as http://host.openshell.internal:18780), two routes:

- POST /mcp: JSON-RPC 2.0 over plain application/json (no SSE), the seven work tools plus list_models
  and set_default_model. Proven against the official `mcp` Python SDK streamable-HTTP client.
- GET /v1/wait/<task_id>?after=<revision>&timeout=<seconds>: long-poll for the Hermes waiter script.

Auth: `Authorization: Bearer <token>`; the token's sha256 is compared constant-time against
/etc/omp-bridge/tokens (one line per agent). Token values never appear in logs, responses or errors.
"""
import hashlib
import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import EngineError
from .models import ModelError

log = logging.getLogger("bridge.http")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
JSONRPC_PARSE_ERROR, JSONRPC_INVALID_REQUEST, JSONRPC_METHOD_NOT_FOUND, JSONRPC_INVALID_PARAMS, \
    JSONRPC_INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

CHECK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "argv"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "argv": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "cwd": {"type": "string"},
        "timeout_s": {"type": "integer", "minimum": 1},
    },
}

TOOLS = [
    {"name": "submit_task",
     "description": "Delegate one coding task to the omp worker. Creates a locked git worktree on branch "
                    "omp/<task_id> and runs the first round immediately.",
     "inputSchema": {
         "type": "object", "additionalProperties": False,
         "required": ["project", "spec", "done_criteria"],
         "properties": {
             "project": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"},
             "spec": {"type": "string"},
             "done_criteria": {
                 "type": "object", "additionalProperties": False,
                 "required": ["checks"],
                 "properties": {
                     "checks": {"type": "array", "maxItems": 20, "items": CHECK_SCHEMA},
                     "review": {"type": "array", "items": {"type": "string"}},
                 },
             },
             "mode": {"type": "string", "enum": ["batch", "session"], "default": "batch"},
             "base_branch": {"type": "string"},
             "model": {"type": "string", "description": "an alias from list_models; never a raw model id"},
         },
     }},
    {"name": "send_feedback",
     "description": "Send feedback after a round: next_round starts a new round (optionally with a new "
                    "model alias applied from that round on); steer only works on a running session-mode "
                    "round and cannot carry a model change.",
     "inputSchema": {
         "type": "object", "additionalProperties": False,
         "required": ["task_id", "text"],
         "properties": {
             "task_id": {"type": "string"},
             "text": {"type": "string"},
             "how": {"type": "string", "enum": ["next_round", "steer"], "default": "next_round"},
             "model": {"type": "string"},
         },
     }},
    {"name": "task_status",
     "description": "One task's status, checks of its latest round, limits and evidence dir.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["task_id"],
                     "properties": {"task_id": {"type": "string"}}}},
    {"name": "task_result",
     "description": "task_status plus the capped last assistant message, check output tails, and the "
                    "branch's diff stat and commits since the base.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["task_id"],
                     "properties": {"task_id": {"type": "string"}, "round": {"type": "integer"}}}},
    {"name": "cancel_task",
     "description": "Kill the running round (and the rpc daemon) and mark the task cancelled.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["task_id"],
                     "properties": {"task_id": {"type": "string"}}}},
    {"name": "close_task",
     "description": "Accept the work: mark the task closed. The branch and evidence stay; optional "
                    "merged-worktree cleanup may remove the worktree.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["task_id"],
                     "properties": {"task_id": {"type": "string"}}}},
    {"name": "list_tasks",
     "description": "Your tasks only, newest first (revision included so waiters can be re-armed).",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "list_models",
     "description": "The operator's model allowlist: alias, description, enabled. base_url and env var "
                    "names are never exposed.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "set_default_model",
     "description": "Set your per-agent default model alias (other agents are unaffected).",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["alias"],
                     "properties": {"alias": {"type": "string"}}}},
    {"name": "create_project",
     "description": "Create a new project in the shared workspace: a git repository on branch main with one seed commit (a README), "
                    "committed as your own git identity. The name is a lowercase slug (letters, digits, . _ -). It never touches an "
                    "existing project; submit_task needs the project to exist.",
     "inputSchema": {
         "type": "object", "additionalProperties": False, "required": ["name"],
         "properties": {
             "name": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"},
             "description": {"type": "string", "maxLength": 500,
                             "description": "one or two lines, written into the README"},
         },
     }},
    {"name": "list_projects",
     "description": "Every project in the shared workspace (all agents see the same list): name, last commit on main, "
                    "who created it when it was made through create_project, and which git identity its commits use.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
]

WAIT_RE = re.compile(r"^/v1/wait/([A-Za-z0-9._-]+)$")


class Authenticator:
    """Bearer token -> agent name, by sha256, constant-time. Values are never logged."""

    def __init__(self, tokens):
        self._tokens = tokens  # agent -> sha256 hex

    def authenticate(self, header):
        if not header or not header.startswith("Bearer "):
            return None
        value = header[len("Bearer "):].strip()
        if not value:
            return None
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        for agent, expected in self._tokens.items():
            if hmac.compare_digest(digest, expected):
                return agent
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "omp-bridge/1"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- plumbing

    @property
    def bridge(self):
        return self.server.bridge  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 (BaseHTTPRequestHandler's own name)
        log.debug("http %s", format % args)

    def _send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return b""
        if length > self.bridge.cfg.request_max_bytes:
            return None
        return self.rfile.read(length)

    def _agent(self):
        agent = self.bridge.authenticator.authenticate(self.headers.get("Authorization"))
        if agent is None:
            self._send_json(401, {"error": "unauthorized"})
            log.warning("rejected request without a valid bearer token (path=%s)", self.path)
            return None
        return agent

    # ---------------------------------------------------------------- routes

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            self._send_json(405, {"error": "method not allowed: use POST"})
            return
        match = WAIT_RE.match(parsed.path)
        if match:
            self._handle_wait(match.group(1), parse_qs(parsed.query))
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return
        agent = self._agent()
        if agent is None:
            return
        body = self._read_body()
        if body is None:
            self._send_json(413, {"error": f"request exceeds {self.bridge.cfg.request_max_bytes} bytes"})
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except ValueError:
            self._send_json(200, self._rpc_error(None, JSONRPC_PARSE_ERROR, "parse error"))
            return
        if not isinstance(request, dict):
            self._send_json(200, self._rpc_error(None, JSONRPC_INVALID_REQUEST, "not an object"))
            return
        self._dispatch(agent, request)

    # ---------------------------------------------------------------- json-rpc

    @staticmethod
    def _rpc_result(request_id, result):
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _rpc_error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _dispatch(self, agent, request):
        request_id, method = request.get("id"), request.get("method")
        if not isinstance(method, str):
            self._send_json(200, self._rpc_error(request_id, JSONRPC_INVALID_REQUEST, "method is required"))
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            params = request.get("params") or {}
            offered = params.get("protocolVersion")
            version = offered if offered in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            self._send_json(200, self._rpc_result(request_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "omp-bridge", "version": "1.0.0"},
            }))
            return
        if method == "ping":
            self._send_json(200, self._rpc_result(request_id, {}))
            return
        if method == "tools/list":
            self._send_json(200, self._rpc_result(request_id, {"tools": TOOLS}))
            return
        if method == "tools/call":
            self._tools_call(agent, request_id, request.get("params") or {})
            return
        self._send_json(200, self._rpc_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"unknown method {method!r}"))

    def _tools_call(self, agent, request_id, params):
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            self._send_json(200, self._rpc_error(request_id, JSONRPC_INVALID_PARAMS, "bad tools/call params"))
            return
        if name not in {tool["name"] for tool in TOOLS}:
            self._send_json(200, self._rpc_result(request_id, self._tool_error("unknown_tool", f"no tool {name!r}")))
            return
        schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
        valid, problem = self._validate(arguments, schema)
        if not valid:
            self._send_json(200, self._rpc_result(request_id, self._tool_error("invalid_request", problem)))
            return
        engine = self.bridge.engine
        try:
            result = self._invoke(engine, agent, name, arguments)
        except EngineError as error:
            log.info("agent=%s tool=%s error=%s", agent, name, error.code)
            result = self._tool_error(error.code, error.message)
        except ModelError as error:
            log.info("agent=%s tool=%s error=%s", agent, name, error.code)
            result = self._tool_error(error.code, error.message)
        except Exception:
            log.exception("agent=%s tool=%s crashed", agent, name)
            result = self._tool_error("internal_error", "the bridge hit an internal error; see its logs")
        else:
            result = {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        self._send_json(200, self._rpc_result(request_id, result))

    @staticmethod
    def _tool_error(code, message):
        return {"content": [{"type": "text", "text": json.dumps({"code": code, "message": message})}],
                "isError": True}

    @staticmethod
    def _validate(arguments, schema):
        """The subset of JSON Schema the tool schemas use: required, types, enum, arrays, patterns."""
        properties = schema.get("properties", {})
        for key, value in arguments.items():
            if key not in properties:
                return False, f"unknown argument {key!r}"
            spec = properties[key]
            expected = spec.get("type")
            kinds = {"string": str, "integer": int, "array": list, "object": dict, "boolean": bool}
            if expected in kinds and not isinstance(value, kinds[expected]):
                return False, f"{key!r} must be {expected}"
            if expected == "integer" and isinstance(value, bool):
                return False, f"{key!r} must be an integer"
            if "enum" in spec and value not in spec["enum"]:
                return False, f"{key!r} must be one of {spec['enum']}"
            if expected == "string" and "pattern" in spec and not re.match(spec["pattern"], value):
                return False, f"{key!r} must match {spec['pattern']}"
            if expected == "array" and "maxItems" in spec and len(value) > spec["maxItems"]:
                return False, f"{key!r} allows at most {spec['maxItems']} items"
            if expected == "array" and "items" in spec:
                for item in value:
                    ok, problem = Handler._validate(item, {**spec["items"], "properties": spec["items"].get("properties", {})})
                    if not ok:
                        return False, f"{key!r}: {problem}"
        for key in schema.get("required", []):
            if key not in arguments:
                return False, f"missing required argument {key!r}"
        return True, ""

    @staticmethod
    def _invoke(engine, agent, name, args):
        if name == "submit_task":
            return engine.submit(agent, args["project"], args["spec"], args["done_criteria"],
                                 args.get("mode", "batch"), args.get("base_branch"), args.get("model"))
        if name == "send_feedback":
            return engine.feedback(agent, args["task_id"], args["text"], args.get("how", "next_round"),
                                   args.get("model"))
        if name == "task_status":
            return engine.status(agent, args["task_id"])
        if name == "task_result":
            return engine.result(agent, args["task_id"], args.get("round"))
        if name == "cancel_task":
            return engine.cancel(agent, args["task_id"])
        if name == "close_task":
            return engine.close(agent, args["task_id"])
        if name == "list_tasks":
            return {"tasks": engine.list_tasks(agent)}
        if name == "list_models":
            return engine.list_models(agent)
        if name == "set_default_model":
            return engine.set_default_model(agent, args["alias"])
        if name == "create_project":
            return engine.create_project(agent, args["name"], args.get("description", ""))
        if name == "list_projects":
            return {"projects": engine.list_projects(agent)}
        raise EngineError("unknown_tool", f"no tool {name!r}")

    # ---------------------------------------------------------------- wait

    def _handle_wait(self, task_id, query):
        agent = self._agent()
        if agent is None:
            return
        try:
            after = int((query.get("after") or ["0"])[0])
            timeout = int((query.get("timeout") or ["0"])[0])
        except ValueError:
            self._send_json(400, {"error": "after and timeout must be integers"})
            return
        try:
            result = self.bridge.engine.wait(agent, task_id, after, timeout)
        except EngineError as error:
            self._send_json(404 if error.code == "not_found" else 400,
                            {"error": error.code, "message": error.message})
            return
        self._send_json(200, result)


class BridgeServer(ThreadingHTTPServer):
    def __init__(self, address, cfg, engine, authenticator):
        super().__init__(address, Handler)
        self.cfg, self._engine, self.authenticator = cfg, engine, authenticator
        self.bridge = self  # Handler.bridge expects the aggregate; keep one name
        self.engine = engine
        self.daemon_threads = True


def serve(cfg, engine, tokens):
    server = BridgeServer((cfg.listen_host, cfg.listen_port), cfg, engine, Authenticator(tokens))
    log.info("listening on %s:%s", cfg.listen_host, cfg.listen_port)
    return server
