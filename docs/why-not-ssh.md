# Why not just give the agent SSH and a coding harness?

A common alternative: give a chat agent an SSH login to a machine and let it run a coding harness there. It is simple and it works. This
page lists what the delegation design does differently and what that costs. **It is not a benchmark.** The two approaches were never
run against each other, so nothing below is a measured advantage.

Evidence labels: **OBSERVED** in the delegation setup's live use; **BY DESIGN** follows from how the pieces are built and can be read
in the code; **NOT MEASURED** has no data behind it.

## Tradeoffs

| | SSH + a harness | Sandboxed delegation | Evidence |
|---|---|---|---|
| **What the agent can touch** | whatever the SSH account can | eleven MCP tools; the workspace is read-only for it | BY DESIGN |
| **Where code runs** | on a real machine, usually with network and credentials | in a policy-enforced sandbox with no network; credentials injected only at approved endpoints | BY DESIGN; sandbox denials OBSERVED |
| **Who says "done"** | the harness or the agent | the bridge runs the agent's checks itself; the worker's claim is advisory | BY DESIGN; VERIFIED in tests |
| **Several agents** | share an account or need one per agent, plus their own isolation | per-agent tokens, per-agent task visibility, per-agent git identity verified per task | VERIFIED in tests |
| **Git authorship** | whatever the harness's config says | decided and checked by the bridge per task | VERIFIED in tests; OBSERVED live |
| **Models** | whatever the harness is configured with | an operator allowlist of aliases; the agent never names a model, URL or key | VERIFIED in tests |
| **Finishing** | the agent polls or blocks on the session | the agent arms a waiter, ends its turn, and the completion wakes it | OBSERVED in a real chat |
| **Audit trail** | shell history, if kept | a spec, round log, check results and identity record per round | BY DESIGN; OBSERVED |
| **Setup effort** | low | high: a sandbox runtime, a worker image, a bridge, providers, agent skills | OBSERVED |
| **Time to first result** | short | NOT MEASURED | NOT MEASURED |
| **Interactivity** | the agent can poke around in real time | a round at a time; the worker has no network, so research and dependencies must arrive through the spec or a vendored cache | BY DESIGN |
| **Output quality, cost, latency** | NOT MEASURED | NOT MEASURED | NOT MEASURED |
| **Blast radius if the worker misbehaves** | the SSH account's reach | one sandbox, one workspace (but it can rewrite any repository in it) | BY DESIGN; limits in [trust-model.md](trust-model.md) |

## When SSH plus a harness is the better choice

- One agent, one trusted machine, and you want results quickly.
- The task needs network access or real credentials that you do not want to plumb through a sandbox.
- You value interactive exploration over a contract.

## When this design earns its cost

- Several agents share one coding capability and you want to keep their work, identities and visibility separate.
- You want the agent to hold a narrow tool interface instead of a shell, and the code to run where it cannot reach your network or
  credentials.
- You want the "is it done?" decision to rest on checks you defined, with evidence a person can review before anything is merged.

## What would turn this page into evidence

Run the same set of tasks through both setups and record setup time, time to first accepted change, failure modes, and what a reviewer
had to fix. Until someone does, treat the table as a description of design differences, not of results.
