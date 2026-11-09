# Changelog -- smart-node-admin-runner

Semver. Privileged operations service (node trust anchor).

## [0.4.0] - 2026-08-31

### Added
- `lxc_node_provision`: creates a test LXC container from a
  configurable base image, runs the Smart Node installer at the requested
  version, and records the container name in the test-node registry
  (`/var/lib/admin-runner/test-nodes.txt` by default). Three hard guards: (1)
  refuses if the container name matches the live node hostname, (2) idempotent
  on an existing container (reports state, returns 0), (3) credential isolation
  check runs before `lxc launch` -- every value in an optional seed manifest is
  checked against `/etc/smart-node/*.env` and the operation is refused before
  any container is created if a match is found. Disconnected mode (no seed
  manifest) is the safe default.

- `lxc_node_destroy`: stops and deletes a test container and
  removes it from the registry. Always confirmed (dangerous=True). Two guards:
  (1) refuses if name matches live node hostname, (2) refuses if name is not in
  the test-node registry -- prevents accidental deletion of non-test containers.

- `lxc_node_status`: read-only -- `lxc info` state plus the
  `/health` endpoint response (fetched inside the container via `lxc exec`).
  No confirmation required.

- Four new optional env knobs (all with sensible defaults):
  `SN_ADMIN_LXC_BASE_IMAGE`, `SN_ADMIN_LXC_NETWORK`,
  `SN_ADMIN_INSTALLER_URL_BASE`, `SN_ADMIN_TEST_NODE_REGISTRY`.

### Notes
- Host-level prerequisite (one-time operator action, NOT this release): the LXD
  unix socket must be bind-mounted into the admin-runner environment and the
  service user must be a member of the `lxd` group on the host. Without this
  the `lxc` commands fail at runtime, not at import time.
- Tests: `tests/test_lxc_ops.py` (68 cases).

## [0.3.0] - 2026-08-11

### Added
- `ensure_project`: creates a project's unix group and
  `/srv/projects/<slug>` at 2770 setgid group-owned, and puts its members in the
  group. Modelled directly on `_ENSURE_USER_SH` -- a short idempotent bash
  template run through `sudo -n` with a strictly validated argument list. Same
  class of privilege as the agent-user provisioner, no new component and no new
  sudoers rule.

  setgid is the whole mechanism: without it the directory works on the first day
  and silently stops working on the second, when a second member writes a file
  the first cannot read. The shared root stays 0755 root:root -- everyone may
  traverse it, nobody may create a project by making a directory.

  Members are passed as separate argv entries, never interpolated into the
  script text. It never REMOVES a member: revoking access needs an explicit path
  and until that exists a member dropped from the registry row still holds the
  group. Stated here rather than discovered later.

### Notes
- Tests: `tests/test_ensure_project.py` (32 cases) -- the first tests this
  component has ever had. They found three real defects in the op while being
  written.

## [0.2.1] - 2026-08-11

### Added
- `ensure_agent_users` now creates and owns `work/` alongside `logs/` and `state/`.
 Until this an agent could write nowhere but `/tmp` -- measured, not  assumed -- which is why file work was impossible on a node. `/srv/projects` is
  the shared area and does not cover it: an agent with no project still needs
  somewhere of its own. Config files in the agent directory stay owned by the
  provisioner, so the agent still cannot write its own `CLAUDE.md`.

## [0.2.0] - 2026-06-15

### Added
- `materialize_crons` op: install a validated cron.d body to
  `/etc/cron.d/smart-node-agents`. Fail-closed lint (every job line must
  route through `cron-runner.sh`), pre-write backup to
  `/var/backups/smart-node/cron/` + a `last-good` pointer, atomic
  tmp+rename install, prune to last 10. No-op when the body already
  matches the live file (keeps the reconcile timer from churning backups).
- `rollback_crons` op: restore the live crontab from `last-good`
  (default) or a named timestamped backup.

Both ops are owner-gated + audited like every op; they are `dangerous=False`
because their narrowness (one fixed target, strict validation, reversible)
is the safety boundary -- callable by the reconcile timer and panel without
a confirm round-trip.

## [0.1.0]
- Initial: node_status, run_command, service_control, claude_login_*.
