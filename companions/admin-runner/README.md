# smart-node-admin-runner

The privileged operations service behind the Smart Node **node-admin agent**. It is the
trust anchor: the LLM never holds privilege. The admin agent (running on a cloud model)
emits a tool call; this service enforces the security boundary and performs the action.

## Security model

- **Bound host-local** (loopback / docker0). Anything reaching the socket is already on the host.
- **Proof of the caller.** core-api presents `SN_ADMIN_CLIENT_SECRET` and names the original
  actor in `X-SN-On-Behalf-Of`. A name sent without that proof is refused, whatever network it
  comes from. The actor is resolved against `sn_config.users`.
- **Per-op permission check.** Every operation declares its permission in
  `operations.py:REGISTRY`, and `permissions.require_permission(name)` admits a call only when
  `sn_config.role_permissions` has (effective role, permission). The effective role is read
  from `sn_core.actor_roles` by the user's `actor_id`, matching core-api so a `super-admin`
  there is the same identity here.
- **Full audit.** Every decision -> append-only `sn_core.events`
  (`admin.permission_granted` / `admin.permission_denied` / `admin.op.requested` /
  `admin.op.executed` / `admin.op.failed` / `admin.op.confirm_invalid`). A denial names
  BOTH the role AND the permission so the missing matrix cell is visible in the log.
- **Confirm-on-destructive.** Ops flagged `dangerous` are staged and require the caller to
  re-submit a single-use, TTL'd `confirm_token` before execution.

## Configuration (env, fail-loud)

| var | required | meaning |
|-----|----------|---------|
| `SN_ADMIN_CLIENT_SECRET` | yes | private core-api client proof, minted by `admin-runner.sh` |
| `SN_ADMIN_DB_USER`     | yes | pg role (peer auth via pg_ident) |
| `SN_ADMIN_DB_NAME`     | no  | default `smartnode` |
| `SN_TRUSTED_CIDRS`     | no  | extra trusted source CIDRs |
| `SN_ADMIN_BIND_HOST/PORT` | no | default `127.0.0.1:8041` |
| `SN_ADMIN_CONFIRM_TTL` | no  | confirm token TTL secs, default 300 |
| `SN_ADMIN_OWNER_ROLES` | no  | retained read-only for backward compatibility; not consulted |

Nothing host-specific is baked into the code: identity, db role, paths are all
deployment config. This service runs on any node.

## API

- `GET /healthz` -> liveness.
- `GET /operations` -> list ops (name, summary, dangerous, input_schema, permission). Requires
  `admin.node_status`.
- `POST /operations/{name}` body `{args, confirm_token?}` -> run an op. Requires the op's own
  permission (see `operations.py:REGISTRY`), audited. Dangerous ops return
  `{needs_confirm, confirm_token}` first.

## Operations

Task A ships `node_status` (read-only) as the proof of the gate. `run_command`,
`service_control`, and the `claude` login lifecycle are added in later tasks.

## Run / test

    pip install -r requirements.txt
    uvicorn app:app --host 127.0.0.1 --port 8041    # needs the env vars above
    pytest                                          # DB-free unit tests
