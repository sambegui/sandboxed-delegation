# Trust model

Who is trusted with what, which controls exist, and what remains. Labels: **VERIFIED** (a test in this repo), **OBSERVED** (seen on the
live system), **INFERRED**, **UNVERIFIED**, **NOT BUILT**.

## Who is trusted

| Party | Trusted to | NOT trusted to |
|---|---|---|
| **Operator (human)** | write the policy, the model allowlist and the tokens; review and merge | (assumed honest and careful) |
| **Bridge** | decide identity, run checks, enforce hard stops, hold agent token digests | hold provider credentials (it does not) |
| **Chat agent** | call its own tools and read the workspace | write the workspace, see other agents' tasks, name a model id, URL or credential |
| **Worker (model + omp)** | edit files in its task's worktree | be honest about its own results, stay inside its worktree, keep the repository's `.git` intact |
| **Everything the worker writes** | nothing: treat it as untrusted input to the reviewer's machine and to the agents | |

## Controls

| Control | What it does | Evidence |
|---|---|---|
| Sandbox policy, no network | The worker's filesystem is read-only except `/tmp` and `/sandbox`; Landlock is `hard_requirement`; network is denied except one provider endpoint bound to the omp executable | OBSERVED: denial of other egress, including direct IP and raw sockets, in a proof run of the image |
| Credentials stay outside | Providers inject credentials at the sandbox boundary; the sandbox holds a placeholder. `bridge.toml` names env vars, never values | VERIFIED: tests assert env var names, never values, reach agents or logs |
| Per-agent tokens | The bridge stores only sha256 digests, compares in constant time, and an agent sees only its own tasks | VERIFIED: `tests/test_mcp.py` |
| Model allowlist | Agents pick an alias; unknown or disabled aliases are refused | VERIFIED: `tests/test_models.py`, `tests/test_mcp.py` |
| Checks run by the bridge | The agent's `done_criteria` run as literal argv in the worktree; the worker's claim is advisory | VERIFIED: `tests/test_engine.py` |
| Identity stamped and verified | Environment identity per task plus a per-round `git identity` check | VERIFIED: `tests/test_identity.py` |
| Hard stops | `max_rounds`, per-round and total time, `max_active_tasks`, spec size, request size | VERIFIED for rounds, time and active tasks (`tests/test_engine.py`); the spec-size and request-size limits are enforced in code but have no test (UNVERIFIED) |
| Read-only workspace for agents | The agents' sandboxes mount the workspace read-only | OBSERVED |
| Loopback bind | The bridge listens on `127.0.0.1` only | OBSERVED |

## Residual risks, most important first

1. **The host that runs the gateways is the real prize.** Whoever has root there controls every sandbox. Do not rely on network trust
   alone: require a password (or hardware key) for privilege, and pin SSH access by source in your network policy. OBSERVED in the
   live setup: the first finding of a risk review was a trusted-network SSH login with passwordless sudo.
2. **The worker can rewrite any repository's `.git`.** A bad or compromised round can damage history, and there is no off-worker copy
   unless you make one. NOT BUILT here: scheduled off-worker backups (for example `git bundle` of every repository plus the database and
   evidence, kept where the worker cannot write) and pushing branches to a forge as the real backup.
3. **Checks run inside the worker**, so a worker that games its environment can fool them. Human review of the diff is the real gate.
4. **All tasks share one sandbox and one uid.** Worktrees prevent collisions, not malice. Hard isolation needs separate sandboxes.
5. **Running or reviewing agent-written code on your own machine** executes hooks and config the worker wrote. Use read-only git with
   `core.hooksPath` and `core.fsmonitor` neutralized, publish from a clean clone, and build and run agent code in the worker or a VM.
6. **Agents can edit their own skills** after tasks (OBSERVED), so the rules you gave them can drift. NOT BUILT: a scheduled hash check
   that reports, and never auto-reverts, changes to the skill files.
7. **Model endpoints on loopback accept any local request** (OBSERVED for one relay), so any local process can spend the quota. Keep
   them loopback-only.
8. **Code goes to whichever models you allow.** A hosted model is a third party. NOT BUILT: a per-project allowlist so a sensitive
   project can be limited to a self-hosted model.
9. **Prompt injection.** Specs, feedback and repository files are text a model reads; the workspace rules tell the worker to treat file
   text as data. How well a given model obeys that is UNVERIFIED.

## What this design is not

It is not a defense against a malicious operator, a compromised host, or a malicious model that finds a sandbox escape. It narrows what a
well-meaning but fallible agent and worker can do, and makes what they did visible.
