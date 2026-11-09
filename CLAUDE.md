# Working in this repository

This file is for coding agents and human contributors working from a public
clone. Start with `README.md`, then read `CONTRIBUTING.md` and `SECURITY.md`
before changing code.

## Repository shape

- `orchestrator/install-on.sh` is the installation entry point.
- `bootstrap/` contains the six core stages.
- `companions/` contains pinned component snapshots and their installers. Read
  the component's `VENDORED` and `NOTICE.md` before changing a vendored tree.
- `tests/*.test.sh` are source-level regression checks.
- `tests/e2e/` requires a running disposable node; do not aim it at a machine
  that contains data you care about.
- `docs/node/` is installed into the node for its agents to read. Keep it in
  sync with behavior changes.

## Before proposing a change

1. Make the smallest source change that fixes the behavior. Do not repair a
   running node by hand and leave the source divergent.
2. Add or update a test that fails without the change.
3. Run `tests/run-source.sh` and the affected component's own test suite.
4. Run `git diff --check` and syntax-check changed shell and Python files.
5. Update public documentation when commands, prerequisites, supported
   providers, or limitations change.

Never add credentials, private hostnames, real customer data, or machine-local
paths. Use neutral fixtures such as `example-node`, `example.com`, and reserved
documentation addresses.
