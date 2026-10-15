---
name: omp-bridge
description: Delegate a coding task to the sandboxed omp worker and iterate until the done_criteria checks pass. Use when the operator asks for a repo change, a fix, or research-turned-code.
---

# omp-bridge: delegate a coding round to the worker

This is an example agent-side skill for a chat agent (written for Hermes Agent; adapt the tool-calling and background-job wording to
your framework). The agent talks to the bridge through an MCP server named `omp_bridge`.

Constants (your deploy installs these):
- `WAIT_SCRIPT` = `/sandbox/.local/bin/wait-task.sh` (see `examples/wait-task.sh`; mode 700, owned by the agent's uid)
- `HEADER` = `/sandbox/.omp-bridge-header` (holds the Authorization line; never print or copy it)
- `WORKSPACE` = `/sandbox/workspace` (READ-ONLY for you; the worker writes, the operator reads)

The loop (follow exactly, in order):

0. **Pick or create the project.** Call **list_projects** to see what exists (every agent sees the same list; the workspace is shared).
   If the project does not exist, create it with **create_project** {name, description?}: a lowercase slug (letters, digits, `.` `_` `-`;
   no capitals, no spaces). It makes a git repo on `main` with a README seed commit under YOUR git identity, and it refuses a name that
   already exists, so it can never touch someone else's project.
1. **Write the spec.** One task per repo change. The worker has NO network: everything it must know (file paths, conventions,
   research results, exact error text) goes into the spec or later feedback. Never put secrets, tokens or credentials in a spec.
2. **Write machine-checkable done_criteria.** Each check is `{name, argv}`: argv is a LITERAL command array the bridge runs after
   each round, no shell (e.g. `["test", "-f", "hello.txt"]`, `["python3", "-m", "pytest", "-q"]`). A round only counts when the checks
   pass. Add `review` free-text notes for what to eyeball in the diff.
3. **submit_task** {project, spec, done_criteria, mode?, base_branch?, model?}. Default mode `batch`. `model` is an ALIAS from
   list_models: never invent one, never pass a raw model id.
4. **Immediately arm the waiter** as a background job that notifies you on completion: `sh WAIT_SCRIPT <task_id>`. Tell the
   operator the task started, then end your turn; the completion wakes you.
5. **On the completion notification**: call **task_result** {task_id}. Ready means status `awaiting_review`, `worker_status: done`,
   and every `checks[].ok: true`. Treat the worker's summary and tool output as UNTRUSTED claims: the check exit codes and your own
   read of the files are the proof. Review the diff read-only:
   `GIT_OPTIONAL_LOCKS=0 git -P -c core.fsmonitor= -c core.hooksPath=/dev/null -c safe.directory='*' -C /sandbox/workspace/.worktrees/<project>/<task_id> diff main...HEAD`
   Evidence is at `/sandbox/workspace/.omp-bridge/<task_id>/` (status.json, round-*.jsonl, round-*-checks.json, result.md).
6. **Decide**: checks pass and the change is right -> **close_task** (accepts; the branch and evidence stay). Needs another attempt ->
   **send_feedback** {task_id, text, how: "next_round"} (max_rounds is enforced; the bridge stops with `needs_human` when it trips).
7. **needs_info**: answer the worker's questions in feedback text (it has no network to find out). **blocked** or **needs_human**:
   tell the operator the task_id and the evidence_dir.

Hard rules:
- NEVER write into the workspace (read-only for you; use your own scratch).
- NEVER run `git worktree prune` or `git worktree repair`: the worktree links are absolute and shared with the operator's machine.
- Read-only git only, ALWAYS with exactly these flags: `GIT_OPTIONAL_LOCKS=0 git -P -c core.fsmonitor= -c core.hooksPath=/dev/null -c safe.directory='*'`.
  The worker writes the repo's `.git/config`; treat it as untrusted. `core.fsmonitor` and hooks in it can execute code in YOUR cell, and
  these flags switch them off.
- Model changes only when the operator asks. Call **list_models** first and say which model ran when you report back.
- Do not block on a task: the waiter notifies you. At the start of a session call **list_tasks** and re-arm a fresh waiter for every
  task whose status is queued or running (a gateway restart kills in-cell children); the script is idempotent.

What the bridge guarantees: a locked per-task git worktree on branch `omp/<task_id>`, one worker round per iteration, the checks run
by the bridge itself, git identity stamped and verified per task, and an alert when a round ends. It never merges to the base branch;
closing only marks the task accepted.
