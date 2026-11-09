# Seed manifest

A seed manifest is a JSON file passed to `install-on.sh` via `--seed-manifest`
at install time. It specifies, for each external channel and credential, either
a concrete value or the word `"disconnected"`.

node-provision (admin-runner) uses a seed manifest to enforce credential
isolation when standing up a test node. The manifest is the API boundary between
the provisioner and the installer that makes isolation enforceable rather than
conventional.

## Format

```json
{
  "telegram":   "disconnected" | { "token": "...", "channel_id": "..." },
  "email":      "disconnected" | { "host": "...", "account": "...", "password": "...",
                                   "imap_host": "...", "smtp_host": "..." },
  "voice":      "disconnected" | { "gemini_key": "..." },
  "anthropic":  "disconnected" | { "key": "..." } | { "secret_ref": "dotted.ref" },
  "openrouter": "disconnected" | { "key": "..." } | { "secret_ref": "dotted.ref" },
  "node_fqdn":  "test-node.local",
  "no_vpn":     true,
  "budgets": [
    {
      "scope_type": "tenant",
      "scope_id":   "1",
      "period":     "daily",
      "limit_usd":  5.00,
      "hard_limit": true
    }
  ]
}
```

All fields are optional. Absent fields leave the corresponding CLI flags
unchanged. Present fields override CLI flags; the manifest wins.

## Field reference

| Field | Effect when "disconnected" | Effect when an object |
|---|---|---|
| `telegram` | TG_TOKEN and TG_CHANNEL_ID are cleared; communication companion is skipped | Sets the bot token and channel ID |
| `email` | MAIL_HOST and all mail variables are cleared; communication companion is skipped | Sets the mailserver and credentials |
| `voice` | GEMINI_KEY is cleared; voice companion is not installed | Sets the Gemini API key |
| `anthropic` | ANTHROPIC_KEY is cleared; gateway installs degraded | `{ "key": "..." }` sets the key directly; `{ "secret_ref": "dotted.ref" }` resolves the key from this node's secret store at install time (see below) |
| `openrouter` | OPENROUTER_KEY is cleared; gateway installs degraded | `{ "key": "..." }` sets the key directly; `{ "secret_ref": "dotted.ref" }` resolves the key from this node's secret store at install time |
| `node_fqdn` | N/A | Overrides the node FQDN (only when `--node-fqdn` was not given on the CLI) |
| `no_vpn` | N/A | `true` skips WireGuard (same as `--no-vpn`) |
| `budgets` | N/A | Array of budget rows to insert into `sn_config.budgets` on the target node after Phase 3. See below. |

## secret_ref form

`{ "secret_ref": "dotted.ref" }` tells the installer to resolve the credential
from the PROVISIONING node's secret store (`GET /api/secrets/resolve?ref=<ref>`)
at install time. The resolved value is passed directly to the installer as a shell
variable; it is never written to a file or transmitted over the network as
plaintext.

This is the form to use when the credential must be kept out of the manifest file
itself. The secret must exist in the provisioning node's
store before the install runs:

```bash
# Store the stand key once (run on the provisioning node):
sudo curl -s --unix-socket /run/smart-node/api/core-api.sock \
  -X POST http://localhost/api/secrets \
  -H "Content-Type: application/json" \
  -d '{"ref": "anthropic.stand_key", "value": "sk-ant-..."}'
```

The installer dies with a clear message if the ref is absent or the secret store
is unreachable.

## budgets field

An optional array of budget rows to seed on the target node after Phase 3 (DB up).
Each row maps to a row in `sn_config.budgets`:

| Key | Required | Description |
|---|---|---|
| `scope_type` | yes | `tenant`, `agent`, or `metadata_key` |
| `scope_id` | yes | Identifier for that scope (e.g. `"1"` for the default tenant) |
| `period` | yes | `daily`, `weekly`, or `monthly` |
| `limit_usd` | yes | Spend limit in USD |
| `hard_limit` | no | `true` refuses new sessions when the limit is reached; `false` (default) only alerts |

The INSERT uses `ON CONFLICT ... DO UPDATE`, so re-running with the same manifest
updates the limits rather than failing. `enabled` is always set to `true`.

## Disconnected mode

All channels `"disconnected"` is the standard for test node acceptance testing.
The node runs all internal services (planner, doci, mesh, core-api, brin,
admin-runner) without any external adapters.

Sample disconnected manifest (`docs/seed-manifests/disconnected.json`):

```json
{
  "telegram":   "disconnected",
  "email":      "disconnected",
  "voice":      "disconnected",
  "anthropic":  "disconnected",
  "openrouter": "disconnected"
}
```

## Acceptance stand manifest

For the acceptance stand, use `docs/seed-manifests/stand.json`. It resolves the
Anthropic key by reference (never a literal), leaves all other channels
disconnected, and seeds a daily hard budget of $5.00 for the test tenant:

```json
{
  "telegram":   "disconnected",
  "email":      "disconnected",
  "voice":      "disconnected",
  "anthropic":  { "secret_ref": "anthropic.stand_key" },
  "openrouter": "disconnected",
  "budgets": [
    { "scope_type": "tenant", "scope_id": "1",
      "period": "daily", "limit_usd": 5.00, "hard_limit": true }
  ]
}
```

Before running the stand install, store the stand key on the provisioning node:

```bash
sudo curl -s --unix-socket /run/smart-node/api/core-api.sock \
  -X POST http://localhost/api/secrets \
  -H "Content-Type: application/json" \
  -d '{"ref": "anthropic.stand_key", "value": "sk-ant-..."}'
```

Calling the installer in disconnected mode:

```bash
install-on.sh \
  --provider lxd-local \
  --name test-node \
  --ip <node-ip> \
  --ssh-key /path/to/key.pub \
  --all \
  --no-vpn \
  --setup-via-token \
  --seed-manifest /path/to/docs/seed-manifests/disconnected.json
```

## Acceptance criteria

A node installed with a fully disconnected manifest:

- Has the planner reachable (HTTP 200 on the planner health endpoint)
- Can write and retrieve a document via doci
- Has at least one agent start without error
- Carries no live credential in its secret store

These are the checks in the seed-manifest acceptance run.

## Notes

`--seed-manifest` is valid in all modes (default, bootstrap-only, all).
The manifest is processed before the companion-credential validation, so the
existing flag-level checks remain correct: a disconnected manifest leaves the
credential variables empty, which passes the existing validation.

node-provision hard-codes a disconnected manifest for test node provisioning.
A manifest with live credentials requires explicit operator approval.
