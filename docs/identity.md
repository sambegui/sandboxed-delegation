# Git identity

**The bridge decides which identity a round commits as, and verifies it afterwards. Neither the worker nor the project's own git config
gets a say.** Implemented in `bridge/identity.py` and the engine; tests in `tests/test_identity.py`.

## Rule

| Where the work is going | Identity every commit carries |
|---|---|
| a Forgejo repository (a host in `forgejo_hosts`) | the delegating agent's own identity |
| a GitHub repository (a host in `github_hosts`) | one public front identity for every agent |

The agents are the tables the operator defines under `[identities.forgejo.<agent>]`, and the tokens file must list exactly those
agents. The GitHub front identity exists so that no per-agent address ever reaches a public repository.

Decision order per project: an explicit `[projects.<name>]` pin in `bridge.toml` wins; otherwise the `origin` host is inferred once and
remembered (trust on first use); no remote, or an unknown host, defaults to the Forgejo identity. A project whose origin later changes is
refused as `identity_changed` instead of silently switching, because a worker can edit `remote.origin.url`.

## How it is enforced

1. The bridge exports `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` into the worker process, so author equals committer.
   Environment beats both the repository config and the global config. Only an explicit `--author` flag can still override the author.
2. A built-in `git identity` check runs after every round over the task's new commits and fails the round if any commit carries another
   name or address. History is never rewritten.
3. Each round writes `identity.json` into the evidence folder: the forge kind, whether it came from a pin, the origin or the default,
   and the identity used.

OBSERVED live: a project created through `create_project` was seeded as the creating agent, and a delegated task's commit carried the same
identity; the built-in check passed.

## At merge time

Per-agent commits live on the task branch. A squash merge collapses them into one commit authored by whoever merges. On GitHub, a squash
adds a `Co-authored-by` line for each branch author, which would publish a per-agent address. If you publish to a public host, gate the
push: refuse unless every commit is the front identity and no internal address appears in the author, committer, message or trailers.
(The live setup did this in its desktop push helper; that helper is not part of this repository.)

UNVERIFIED: whether your forge links the no-reply addresses to accounts, and whether it keeps `Co-authored-by` trailers on squash.
