"""Which git identity a worker round commits under. The BRIDGE decides it, per task, from where the work is going; neither the worker
nor the project's own git config gets a say.

- forgejo: the delegating agent's own Forgejo identity (each agent can have its own).
- github:  one public front identity for every agent, so no per-agent address ever reaches a public repo.

Decision order for a project: an explicit pin in bridge.toml ([projects.<name>] identity) wins; otherwise the `origin` host is inferred
and remembered (trust on first use, see Engine._resolve_forge); no remote or an unknown host defaults to forgejo.
"""
import re

FORGES = ("forgejo", "github")

_SCP = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)")        # git@host:owner/repo.git
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/\s]*@)?([^:/\s]+)", re.IGNORECASE)   # https://host/..., ssh://git@host:22/...


def host_of(url):
    """The host of a git remote URL in scp-like, ssh:// or https:// form, lower-cased; None when it cannot be read."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    match = _URL.match(url) or _SCP.match(url)
    return match.group(1).lower() if match else None


def classify(url, forgejo_hosts, github_hosts):
    """'forgejo', 'github', or None when the URL is missing or its host is not one of the configured forge hosts."""
    host = host_of(url)
    if host in {h.lower() for h in forgejo_hosts}:
        return "forgejo"
    if host in {h.lower() for h in github_hosts}:
        return "github"
    return None


def env_for(name, email):
    """The four variables that make EVERY git command in the worker process commit as this identity. Environment beats both the
    repository and the global git config; only an explicit `--author` flag can still override the author (the bridge checks for it)."""
    return [f"GIT_AUTHOR_NAME={name}", f"GIT_AUTHOR_EMAIL={email}",
            f"GIT_COMMITTER_NAME={name}", f"GIT_COMMITTER_EMAIL={email}"]


def offenders(log_lines, name, email):
    """Commits from a `git log --format=%h%x09%an%x09%ae%x09%cn%x09%ce` listing whose author or committer is not (name, email).
    Returns 'hash: author <email> / committer <email>' strings; empty means every commit carries the expected identity."""
    bad = []
    for line in log_lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 5:
            continue
        sha, an, ae, cn, ce = parts
        if (an, ae, cn, ce) != (name, email, name, email):
            bad.append(f"{sha}: author {an} <{ae}> / committer {cn} <{ce}>")
    return bad
