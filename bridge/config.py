"""The bridge's host configuration: one TOML file, read once, with every secret referenced by name only.

Nothing in this module (or the package) ever sees a credential value: model providers are named by the
environment variable the OpenShell provider injects inside the worker sandbox, and agent tokens are sha256
hashes checked by mcp_http.
"""
import re
import tomllib
from dataclasses import dataclass, field


class ConfigError(Exception):
    """The bridge cannot start or accept work: the configuration is missing, unreadable or invalid."""


AGENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")   # an agent name: what the operator lists under [identities.forgejo.<agent>] and in the tokens file
STATUS_FINAL = ("closed", "cancelled")
STATUS_ACTIVE = ("queued", "running", "awaiting_review", "needs_info", "blocked")
API_VALUES = ("openai-completions", "openai-responses", "openai-codex-responses", "azure-openai-responses",
              "anthropic-messages", "bedrock-converse-stream", "google-generative-ai", "google-gemini-cli",
              "google-vertex", "typesafe", "openrouter-decisions", "openai-decisions", "workers-ai-systemone")

_DEFAULTS = {"round_max_time_s": 2700, "total_wall_s": 14400, "max_rounds": 6, "max_active_tasks": 2,
             "spec_max_bytes": 65536, "max_checks": 20, "wait_timeout_cap_s": 3300, "check_timeout_s": 300,
             "exec_grace_s": 30, "request_max_bytes": 1048576}


@dataclass(frozen=True)
class ModelAlias:
    """One operator-approved model. `model` is the exact string passed to omp --model; the bridge never
    lets an agent supply any of these fields, only the alias name."""
    alias: str
    model: str                 # exact omp --model selector, "provider/modelId" form
    provider: str              # models.yml provider id (also the --model prefix)
    model_id: str              # the modelId part, for rpc set_model
    base_url: str
    api: str                   # omp models.yml api type (see omp://models.md allowed values)
    api_key_env: str           # NAME of the env var the provider injects in the sandbox; never a value
    description: str = ""
    enabled: bool = True
    context_window: int = 131072
    max_tokens: int = 8192


@dataclass(frozen=True)
class BridgeConfig:
    listen_host: str
    listen_port: int
    state_dir: str             # sqlite + local scratch; also the systemd ReadWritePath
    tokens_file: str           # lines "<agent> sha256=<hex>"
    openshell_binary: str
    openshell_gateway: str
    sandbox: str               # worker sandbox name, "omp-worker"
    workspace_host: str        # where the workspace lives on the HOST; inside any sandbox it is /sandbox/workspace
    default_base_branch: str
    model_default: str
    aliases: dict              # alias -> ModelAlias (only the operator's tables; enabled or not)
    round_max_time_s: int
    total_wall_s: int
    max_rounds: int
    max_active_tasks: int
    spec_max_bytes: int
    max_checks: int
    wait_timeout_cap_s: int
    check_timeout_s: int
    exec_grace_s: int          # the kill timer waits this long past --max-time before cutting the exec off
    request_max_bytes: int
    remove_merged_worktrees: bool = False   # [cleanup] opt-in: drop a closed task's worktree once merged
    agents_file: str = "/sandbox/workspace/AGENTS.md"   # [workspace] agents_file: appended to every round's system prompt; "" disables
    forgejo_hosts: tuple = ("git.example.com",)
    github_hosts: tuple = ("github.com",)
    forgejo_identities: dict = field(default_factory=dict)   # agent -> (name, email)
    github_identity: tuple = ()                              # (name, email)
    project_pins: dict = field(default_factory=dict)         # project -> "forgejo" | "github"

    def context_flags(self):
        """omp loads AGENTS.md only from its working directory, and every task runs in its own worktree (a separate git root), so the
        shared workspace AGENTS.md is never found by itself (PROBED 2026-10-14 on omp 18.9.2: cwd=/sandbox/workspace found it, cwd=a worktree
        did not). --append-system-prompt delivers it to every round without copying anything into a project repo; a missing file is harmless."""
        return ["--append-system-prompt", self.agents_file] if self.agents_file else []

    def identity_for(self, agent, forge):
        """(name, email) a worker round commits as: the delegating agent's own Forgejo identity, or the single public GitHub front
        identity. WHICH forge is the engine's decision (pin, then the origin host, then the default)."""
        return self.github_identity if forge == "github" else self.forgejo_identities[agent]


def _table(data, key, what):
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{what}: [{key}] must be a table")
    return value


def _get(table, key, kind, where):
    value = table.get(key)
    if not isinstance(value, kind) or (kind is str and not value):
        want = "a non-empty string" if kind is str else f"a {kind.__name__}"
        raise ConfigError(f"{where}: {key!r} must be {want}")
    return value


def _get_int(table, key, where, minimum=1):
    value = table.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{where}: {key!r} must be an integer >= {minimum}")
    return value


def _parse_aliases(data):
    """The [models] table: `default` plus one [models.<alias>] table per operator-approved model."""
    models = _table(data, "models", "configuration")
    default = _get(models, "default", str, "[models]")
    aliases = {}
    for name, table in models.items():
        if name == "default":
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"[models.{name}] must be a table")
        where = f"[models.{name}]"
        model = _get(table, "model", str, where)
        if "/" not in model:
            raise ConfigError(f"{where}: model must be in provider/modelId form, got {model!r}")
        provider, model_id = model.split("/", 1)
        if not provider or not model_id:
            raise ConfigError(f"{where}: model must be in provider/modelId form, got {model!r}")
        base_url = _get(table, "base_url", str, where)
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"{where}: base_url must be http(s), got {base_url!r}")
        api = _get(table, "api", str, where)
        if api not in API_VALUES:
            raise ConfigError(f"{where}: api {api!r} is not an allowed value ({', '.join(API_VALUES)})")
        api_key_env = table.get("api_key_env", "")
        if not isinstance(api_key_env, str):
            raise ConfigError(f"{where}: api_key_env must be a string")
        if api_key_env and (not api_key_env.replace("_", "").isalnum() or api_key_env.upper() != api_key_env):
            raise ConfigError(f"{where}: api_key_env must be an UPPER_CASE env var name (or empty for auth: none),"
                              f" got {api_key_env!r}")
        description = table.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(f"{where}: description must be a string")
        enabled = table.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{where}: enabled must be a boolean")
        context_window = table.get("context_window", 131072)
        max_tokens = table.get("max_tokens", 8192)
        for key, value in (("context_window", context_window), ("max_tokens", max_tokens)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ConfigError(f"{where}: {key} must be an integer >= 1")
        aliases[name] = ModelAlias(alias=name, model=model, provider=provider, model_id=model_id, base_url=base_url,
                                   api=api, api_key_env=api_key_env, description=description, enabled=enabled,
                                   context_window=context_window, max_tokens=max_tokens)
    if default not in aliases:
        raise ConfigError(f"[models].default {default!r} names no [models.{default}] table")
    if not aliases[default].enabled:
        raise ConfigError(f"[models].default {default!r} is disabled")
    for name, alias in aliases.items():
        if alias.provider in {a.provider for other, a in aliases.items() if other != name}:
            raise ConfigError(f"[models.{name}]: provider {alias.provider!r} is declared by another alias; "
                              "one provider id per alias keeps models.yml unambiguous")
    return default, aliases


_PROJECT_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")


def _identity(table, where):
    name, email = _get(table, "name", str, where), _get(table, "email", str, where)
    if any(c in name for c in "<>\r\n\t"):
        raise ConfigError(f"{where}: name must not contain <, >, tabs or newlines")
    if not _EMAIL.match(email):
        raise ConfigError(f"{where}: email {email!r} is not a plain address")
    return name, email


def _parse_identities(data):
    """[identities] (forge hosts, one forgejo identity per agent, one github front identity) and the optional [projects.<name>]
    pins. Every agent must have a forgejo identity: a missing one would fail a task late, so the bridge refuses to start instead."""
    section = _table(data, "identities", "configuration")
    hosts = {}
    for key, default in (("forgejo_hosts", ("git.example.com",)), ("github_hosts", ("github.com",))):
        value = section.get(key, list(default))
        if not isinstance(value, list) or not value or not all(isinstance(h, str) and h for h in value):
            raise ConfigError(f"[identities]: {key} must be a non-empty list of host names")
        hosts[key] = tuple(h.lower() for h in value)
    forgejo = _table(section, "forgejo", "[identities]")
    invalid = sorted(agent for agent in forgejo if not AGENT_RE.match(agent))
    if invalid:
        raise ConfigError(f"[identities.forgejo]: invalid agent name(s) {invalid} (lowercase letters, digits and hyphens, starting with a letter)")
    if not forgejo:
        raise ConfigError("[identities.forgejo]: define at least one agent")
    identities = {agent: _identity(_table(forgejo, agent, "[identities.forgejo]"), f"[identities.forgejo.{agent}]")
                  for agent in sorted(forgejo)}
    github = _identity(_table(section, "github", "[identities]"), "[identities.github]")
    pins = {}
    for project, table in (data.get("projects") or {}).items():
        if not _PROJECT_KEY.match(project) or not isinstance(table, dict):
            raise ConfigError(f"[projects.{project}] must be a table named like a project slug")
        forge = table.get("identity")
        if forge not in ("forgejo", "github"):
            raise ConfigError(f'[projects.{project}]: identity must be "forgejo" or "github"')
        pins[project] = forge
    return hosts["forgejo_hosts"], hosts["github_hosts"], identities, github, pins


def load(path):
    """Read and validate bridge.toml. Raises ConfigError for anything missing or mistyped."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from None

    bridge = _table(data, "bridge", path)
    listen_host = _get(bridge, "listen_host", str, "[bridge]")
    listen_port = _get_int(bridge, "listen_port", "[bridge]", minimum=1)
    if listen_port > 65535:
        raise ConfigError("[bridge]: listen_port must be <= 65535")
    state_dir = _get(bridge, "state_dir", str, "[bridge]")
    tokens_file = _get(bridge, "tokens_file", str, "[bridge]")
    default_base_branch = _get(bridge, "default_base_branch", str, "[bridge]")

    shell = _table(data, "openshell", path)
    openshell_binary = _get(shell, "binary", str, "[openshell]")
    openshell_gateway = _get(shell, "gateway", str, "[openshell]")
    sandbox = _get(shell, "sandbox", str, "[openshell]")

    workspace = _table(data, "workspace", path)
    workspace_host = _get(workspace, "host", str, "[workspace]")
    agents_file = workspace.get("agents_file", BridgeConfig.agents_file)
    if not isinstance(agents_file, str) or (agents_file and not agents_file.startswith("/")):
        raise ConfigError('[workspace]: agents_file must be an absolute path ("" disables it)')

    forgejo_hosts, github_hosts, forgejo_identities, github_identity, project_pins = _parse_identities(data)
    model_default, aliases = _parse_aliases(data)

    limits = data.get("limits", {})
    if not isinstance(limits, dict):
        raise ConfigError("[limits] must be a table")

    cleanup = data.get("cleanup", {})
    if not isinstance(cleanup, dict):
        raise ConfigError("[cleanup] must be a table")
    remove_merged = cleanup.get("remove_merged_worktrees", False)
    if not isinstance(remove_merged, bool):
        raise ConfigError("[cleanup]: remove_merged_worktrees must be a boolean")

    def limit(key):
        return _get_int(limits, key, "[limits]", minimum=1) if key in limits else _DEFAULTS[key]

    return BridgeConfig(listen_host=listen_host, listen_port=listen_port, state_dir=state_dir,
                        tokens_file=tokens_file, openshell_binary=openshell_binary,
                        openshell_gateway=openshell_gateway, sandbox=sandbox, workspace_host=workspace_host,
                        default_base_branch=default_base_branch, model_default=model_default, aliases=aliases,
                        round_max_time_s=limit("round_max_time_s"), total_wall_s=limit("total_wall_s"),
                        max_rounds=limit("max_rounds"), max_active_tasks=limit("max_active_tasks"),
                        spec_max_bytes=limit("spec_max_bytes"), max_checks=limit("max_checks"),
                        wait_timeout_cap_s=limit("wait_timeout_cap_s"), check_timeout_s=limit("check_timeout_s"),
                        exec_grace_s=_get_int(limits, "exec_grace_s", "[limits]", minimum=0)
                        if "exec_grace_s" in limits else _DEFAULTS["exec_grace_s"],
                        request_max_bytes=limit("request_max_bytes"),
                        remove_merged_worktrees=remove_merged, agents_file=agents_file,
                        forgejo_hosts=forgejo_hosts, github_hosts=github_hosts, forgejo_identities=forgejo_identities,
                        github_identity=github_identity, project_pins=project_pins)


def load_tokens(path, agents):
    """agent -> sha256 hex digest of its bearer token. `agents` is the set the operator defined under [identities.forgejo]; the file must
    list exactly those agents. Values are never logged or returned elsewhere."""
    tokens = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError as error:
        raise ConfigError(f"cannot read tokens file {path}: {error}") from None
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        try:
            agent, digest = line.split(None, 1)
            scheme, _, hexdigest = digest.partition("=")
        except ValueError:
            raise ConfigError(f"{path}:{number} is not '<agent> sha256=<hex>'") from None
        if scheme != "sha256" or not hexdigest or len(hexdigest) != 64:
            raise ConfigError(f"{path}:{number} is not '<agent> sha256=<64 hex>'")
        try:
            int(hexdigest, 16)
        except ValueError:
            raise ConfigError(f"{path}:{number} has a non-hex digest") from None
        if agent not in agents:
            raise ConfigError(f"{path}:{number}: unknown agent {agent!r}")
        if agent in tokens:
            raise ConfigError(f"{path}:{number}: agent {agent!r} is listed twice")
        tokens[agent] = hexdigest.lower()
    missing = [name for name in sorted(agents) if name not in tokens]
    if missing:
        raise ConfigError(f"{path}: missing tokens for: {', '.join(missing)}")
    return tokens
