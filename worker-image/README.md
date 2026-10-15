# worker-image

The container image the persistent coding worker runs in: **omp 18.9.2** plus the toolchains a coding task needs, all installed at build
time because the worker sandbox has no network. Contains no credentials.

This is built and run with **rootless Podman** under **OpenShell v0.1.2**: the image is built into the Podman store of the user that
owns the sandbox gateway, and that gateway is configured with `image_pull_policy = never`, so a tag you built locally is what runs.

| File | Purpose |
|---|---|
| `Dockerfile` | The layered image. Layers go from least to most volatile so a rebuild reuses the expensive ones. |
| `build.sh` | Builds into the current user's rootless Podman store, with a disk guard. `NO_CACHE=1` forces a full rebuild. |
| `requirements.in`, `requirements.lock` | Python tools; the lock lists every package with its sha256 and pip runs with `--require-hashes`. |
| `npm/package.json`, `npm/package-lock.json` | JavaScript/TypeScript tools; `npm ci` refuses a tree that differs from the lock. |
| `policy.example.yaml` | An example OpenShell sandbox policy: Landlock `hard_requirement`, read-only system paths, writable `/tmp` and `/sandbox`, uid 1000, no network rules. |
| `provider-profile.example.yaml` | An example provider profile: one inference endpoint, one request shape, bound to the omp executable and the processes it spawns. |

```bash
bash build.sh . localhost/omp-worker:18.9.2-1
# then create the sandbox from that tag with your policy and an attached provider (see ../docs/architecture.md)
```

## What the image contains

- **Rust 1.99.0** from the official standalone tarballs (rustc, cargo, clippy, rustfmt, rust-analyzer, rust-src; std for
  x86_64-unknown-linux-gnu, x86_64-unknown-linux-musl and wasm32-unknown-unknown). No rustup; everything is under `/opt/rust`.
- **System**: git, gcc/g++, make, cmake, clang/lld, musl-tools, pkg-config, ripgrep, fd, jq, sqlite3 and common `-dev` libraries.
- **JavaScript/TypeScript**: node 24, npm, typescript, typescript-language-server, pyright, eslint, prettier, vitest, tsx, pnpm, yarn, bun.
- **Python** (a venv): ruff, mypy, pytest and its plugins, hypothesis, uv, and the common build backends.
- **omp** 18.9.2, with its native addons extracted at build time (omp extracts them on the first real session, which a read-only,
  offline sandbox cannot do).

Every external input is pinned so a rebuild later gets the same bytes: the base image by its index digest, apt by a fixed Debian
snapshot instant, pip and npm by lock files with hashes, omp, Bun and each Rust component by SHA-256. The apt pin depends on
snapshot.debian.org staying reachable.

## Offline convention

The worker cannot fetch anything. Package managers are pointed at a shared cache in the mounted workspace
(`/sandbox/workspace/.deps`) and set to fail fast offline. A task whose dependencies were not vendored or cached there fails at the
package-manager step, and the bridge reports that back to the delegating agent. For Rust, vendor the crates in the project
(`cargo vendor`) and commit a `.cargo/config.toml` pointing at them.

## What was observed with this image

OBSERVED on the live system (2026-10): a persistent sandbox created from it ran omp against hosted and self-hosted models; a Rust
project was created, built and run offline with `cargo` 1.99.0; and the worker's own log showed `rust-analyzer` answering LSP
diagnostics and hover requests. NOT measured: build reproducibility across machines, cold-start time under load, and sizing for several
concurrent Rust builds (the worker shares one sandbox's CPU and memory across `max_active_tasks`).

## Limits, stated plainly

Several omp task processes run at once inside this ONE sandbox, each with its own git worktree. Worktrees prevent collisions; they are
not a security boundary: every task shares the sandbox, its policy and uid 1000, and can read every other task's files. Hard isolation
between tasks would need separate sandboxes.
