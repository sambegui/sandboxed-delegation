# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** through GitHub's private vulnerability reporting: open this repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue and do not post exploit details in discussions.

Include what you found, how to reproduce it, and what you believe the impact is. This is a small project maintained on a best-effort
basis: expect an acknowledgement when someone has had a chance to look, not a guaranteed response time.

## Scope

In scope: the bridge (`bridge/`), the example configuration and scripts (`examples/`), and the worker image build (`worker-image/`).
Examples of things worth reporting: authentication or per-agent isolation bypass, a way for an agent or the worker to name a model,
URL, credential or identity it was not given, command or argument injection through task fields, evidence or path traversal, and
leaks of tokens or credentials into logs, responses or evidence.

Out of scope: vulnerabilities in Hermes Agent, OpenShell, omp, Podman or the models themselves (report those upstream), and the known
limits already documented in [docs/trust-model.md](docs/trust-model.md) (for example that all tasks share one worker sandbox and uid).

## Supported versions

There are no releases. Only the default branch receives fixes.

## Before you deploy

This is a reference implementation proven on a single self-hosted setup. Read [docs/trust-model.md](docs/trust-model.md) first, keep the
bridge bound to loopback, keep tokens and provider credentials out of the repository, and treat everything the worker writes as
untrusted input.
