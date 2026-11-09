# Smart Node bootstrap stages (Layer C, Phase 1)

The Layer C **bootstrap** phase: the linear, CLI-driven stages that take
a Layer-A-hardened host up to the operator login screen. One bash script
per stage, prefixed `B01-` .. `B06-`, plus a shared `lib.sh` they all
source.

The scripts in this directory and the stage descriptions below are the public
contract for the bootstrap phase.

## Layout

```
bootstrap/
├── README.md          # this file
├── lib.sh             # shared helpers; installed to /usr/local/lib/smart-node/lib.sh
├── cron-runner.sh     # B01-core sidecar; installed to /usr/local/lib/smart-node/cron-runner.sh
├── B01-core.sh        # built  -- foundation: group, user, dirs, lib, base pkgs
├── B02-persistence.sh # built  -- managed PostgreSQL + pgvector + core DB
├── B02-schema.sql     # B02-persistence sidecar -- core event-log + actor DDL
├── B03-routing.sh     # built  -- Docker + Traefik reverse proxy + TLS
├── B04-secrets-slice.sh # built  -- sops + age secrets binaries
├── B05-identity.sh    # not built
└── B06-web-interface.sh  # not built
```

After bootstrap, Phase 2 installs **companions** (gateway, memory,
documents, tracking, agent-runtimes, ...) web-driven from the B06 UI.
Companions are named, not numbered, and live outside this directory.

## Pre-condition

The target host must have passed Layer A (host configuration). The orchestrator
uses `provision/provision-lxd-local.sh` for local LXD provisioning and then runs
the provider-independent `configure/configure-host.sh` over SSH.

After Layer A: hardened Debian/Ubuntu host with admin user + key SSH,
ufw + fail2ban + unattended-upgrades. Stage B01 picks up from there.

## How to run (manual; the orchestrator owns this normally)

For an LXD container:

```bash
# Push lib + cron sidecar + stage B01.
lxc file push bootstrap/lib.sh "$NAME"/usr/local/lib/smart-node/lib.sh \
    --mode=0644 --create-dirs
lxc file push bootstrap/cron-runner.sh "$NAME"/tmp/cron-runner.sh --mode=0755
lxc file push bootstrap/B01-core.sh "$NAME"/tmp/B01-core.sh --mode=0755
# the grant travels next to B01. Its --verify check 21 measures a
# present agent-sysadmin against it and fails when it is missing.
lxc file push provision/sudoers/agent-max.sudoers "$NAME"/tmp/agent-max.sudoers

# Run.
lxc exec "$NAME" -- bash /tmp/B01-core.sh

# Verify (idempotent).
lxc exec "$NAME" -- bash /tmp/B01-core.sh --verify

# Status (read state file, no re-check).
lxc exec "$NAME" -- bash /tmp/B01-core.sh --status
```

For an SSH-reachable host:

```bash
scp bootstrap/lib.sh         "$HOST":/tmp/lib.sh
scp bootstrap/cron-runner.sh "$HOST":/tmp/cron-runner.sh
scp bootstrap/B01-core.sh    "$HOST":/tmp/B01-core.sh
scp provision/sudoers/agent-max.sudoers "$HOST":/tmp/agent-max.sudoers
ssh "$HOST" sudo bash -c '
    install -d -m 0755 /usr/local/lib/smart-node
    install -m 0644 /tmp/lib.sh /usr/local/lib/smart-node/lib.sh
    bash /tmp/B01-core.sh
'
```

The orchestrator (`smart-node/orchestrator/install-on.sh`) owns this
push + run step end-to-end (Layer P -> Layer A -> B01 -> B02 -> B03).

## Contract every stage script obeys

Per `installer-stages.md`:

```
./BNN-stage.sh              # full run: probe_before -> intent -> probe_after -> mark state
./BNN-stage.sh --verify     # only probe_after; idempotent; exit 0 on full pass, N (count) on fail
./BNN-stage.sh --status     # read /var/lib/smart-node/stages/BNN.state; exit 0=done 1=failed 2=missing
./BNN-stage.sh --help       # this docstring
```

State file at `/var/lib/smart-node/stages/BNN.state`:

```
status=done|failed|skipped|in_progress
timestamp=2026-05-14T13:00:00-05:00
lib_version=0.7.1
version=BNN-name@X.Y.Z
checks_passed=18/18
reason=...           # only on failed
```

Report appended at `/etc/smart-node/install-report.md`. Each stage gets
exactly one section, idempotent: re-running doesn't duplicate.

## `lib.sh` API

| Function | Purpose |
|---|---|
| `log MSG...` | timestamped stdout |
| `die MSG...` | log to stderr + exit 1 |
| `require_root` | abort if not running as uid 0 |
| `verify_check NAME CMD...` | run CMD; count in `VERIFY_TOTAL`; on fail increment `VERIFY_ERRORS`, print `[VERIFY-FAIL]`; always returns 0 (safe under `set -e`) |
| `stage_mark ID STATUS KEY=VAL...` | atomic write of state file |
| `stage_status ID` | print state file; exit 0=done 1=failed 2=missing |
| `require_stage ID` | die unless stage ID has status=done |
| `report_section TITLE BODY` | append `## TITLE` block to install-report (no-op if section exists) |
| `create_service_user SHORT` | ensure system user `smart-node-SHORT` + owned dirs + Postgres ident map; echoes the username |
| `grant_pg_docker_access DB ROLE` | open the smart-node Docker network to one B02 DB/role -- `listen_addresses` += bridge gateway, scoped `pg_hba` scram line, scoped `ufw` rule, restart/reload; idempotent, multi-caller safe |
| `log_init STAGE_ID` | point the per-stage log at `/var/log/smart-node/STAGE_ID.log`; `log()`/`die()` then tee there |
| `manifest_record TYPE VALUE` | append a created artifact (group/user/dir/file/package/systemd-unit/pg-ident/pg-cluster/docker-network/docker-container/ufw-rule) to `manifest.jsonl` |
| `register_mcp NAME FILE` | install an MCP shim into `/etc/smart-node/mcp.d/NAME.json` |
| `unregister_mcp NAME` | remove an MCP shim |
| `register_companion NAME VERSION DEPS [STATE]` | write `/etc/smart-node/companions.d/NAME.json` (read by the B06 web UI) |
| `unregister_companion NAME` | remove a companion manifest |
| `register_cron NAME SCHEDULE COMMAND [USER]` | write `/etc/cron.d/smart-node-NAME` (job runs through `cron-runner.sh`) |
| `unregister_cron NAME` | remove a cron.d job file |
| `register_route NAME FILE` | install a Traefik dynamic-route fragment into `/etc/smart-node/traefik/dynamic/NAME.yml` |
| `unregister_route NAME` | remove a Traefik dynamic-route fragment |
| `register_hook EVENT NAME FILE` | install a hook script + enable it for a lifecycle `EVENT` |
| `unregister_hook NAME` | remove a hook script and all its enabled links |
| `run_hooks EVENT [ARG...]` | run enabled hooks for `EVENT`; returns the failed-hook count |
| `log_event ACTOR ETYPE [ENTYPE] [ENID] [PAYLOAD]` | record an event -- 3-tier fallback: Postgres -> buffer file -> stderr; never aborts the caller |
| `flush_event_buffer` | drain the pre-DB event buffer into the `events` table |

Caller convention for verify blocks:

```bash
VERIFY_ERRORS=0
VERIFY_TOTAL=0
verify_check "check name" some_cmd arg1 arg2
verify_check "another"    bash -c 'inline test'
if (( VERIFY_ERRORS > 0 )); then ... fi
```

`create_service_user` is the shared convention for every companion (B02+)
and any custom service account. It is **defined** at B01 but only
**called** later, once a companion needs a user. When Postgres is not yet
installed it queues the ident mapping in
`/var/lib/smart-node/stages/pg-ident.pending` for a later persistence
stage to apply.

## Stage B01 -- core

Builds the skeleton every later stage assumes:

- umbrella group `smart-node` (every service account's primary group)
- system user `smart-node` (UID < 1000, nologin, group `smart-node`)
- dirs:
  - `/var/lib/smart-node/{stages,data,tmp}` (smart-node:smart-node, 750)
  - `/etc/smart-node/` (root:smart-node, 750)
  - `/usr/local/lib/smart-node/` (root:root, 755)
  - `/var/log/smart-node/` (smart-node:smart-node, 750)
  - `/opt/smart-node/` (root:smart-node, 2775 -- native component installs)
  - `/etc/smart-node/{mcp.d,companions.d}/` (root:smart-node, 2750 -- registries)
  - `/etc/smart-node/hooks/{available,enabled}/` (root:smart-node, 2750 -- lifecycle hooks)
  - `/var/log/smart-node/cron/` (root:smart-node, 2775 -- cron job run logs)
- `lib.sh` at `/usr/local/lib/smart-node/lib.sh`
- `cron-runner.sh` at `/usr/local/lib/smart-node/cron-runner.sh`
- base apt packages: `jq git python3 python3-venv python3-pip
  build-essential rsync ca-certificates cron`
- cron service enabled (so `/etc/cron.d/smart-node-*` jobs fire)
- `/etc/smart-node/install-report.md` (Stage B01 section)
- `/var/lib/smart-node/stages/B01.state`
- `/var/lib/smart-node/manifest.jsonl` (artifacts B01 created)
- `/var/log/smart-node/B01.log` (this stage's run log)

Stage B01 is **idempotent**: every step is a check-or-create. Running
it twice on a healthy host is a no-op. Verify runs 18 checks.

## Stage B02 -- persistence

Installs and fully owns the Smart Node data engine. Smart Node is an
**appliance**: it assumes a clean, dedicated host and owns the machine,
so B02 runs in **managed mode** -- it installs its own PostgreSQL. There
is no shared / external-DB mode; a pre-existing PostgreSQL is an abort
condition, not a co-tenant (`probe_before` guard).

Builds:

- the **PGDG apt repository** (`apt.postgresql.org`) + signing key --
  pgvector is not packaged in Debian (base or backports); PGDG is the
  PostgreSQL project's official apt channel
- apt packages from PGDG: `postgresql-common`, `postgresql-NN`,
  `postgresql-NN-pgvector` (`NN` = `SMART_NODE_PG_MAJOR`, default `16`)
- a dedicated cluster `NN/smartnode` with the data directory relocated
  under the Smart Node tree: `/opt/smart-node/postgres`
  (`postgres:postgres 0700`, set at `pg_createcluster` time)
- systemd unit `postgresql@NN-smartnode` (enabled + started)
- peer auth: `pg_hba.conf` `local all all` -> `peer map=smartnode`;
  `pg_ident.conf` gets the `smartnode` map (`root` -> `smartnode` role)
  plus every line `create_service_user` queued into
  `stages/pg-ident.pending`
- core database `smartnode` owned by role `smartnode`
- schema `smartnode_ext` with the `vector` extension
- core schema in `smartnode`, applied from the `B02-schema.sql` sidecar:
  `events` (immutable, append-only log -- UPDATE/DELETE blocked by a
  trigger) + `actors` (registry, `layer` CHECK constraint), seeded with
  the `system:installer` actor
- `/etc/smart-node/persistence.env` -- the Postgres connection contract
  later stages and `lib.sh log_event()` read
- `Stage B02 -- persistence` report section, `B02.state`

Every object carries the `smartnode` prefix (database, role, cluster,
schema `smartnode_ext`, ident map). B02 is idempotent; verify runs 24
checks.

The `events` + `actors` schema is core kernel schema, NOT a Phase-2
companion -- every bootstrap action must be attributable to an actor
from the start. `lib.sh log_event()` is the helper stages call to emit events.

## Stage B03 -- routing

Installs and fully owns the routing layer -- the reverse proxy that gives
every Smart Node web service a domain and HTTPS. Per the approved
decision, **Docker is installed here**, not
deferred to the optional Layer A A7 step: the Phase 2 companions are
Docker containers, and the `smart-node` Docker network is their shared
routing fabric.

Builds:

- the **Docker apt repository** (`download.docker.com`) + signing key,
  and Docker Engine (`docker-ce`, `docker-ce-cli`, `containerd.io`,
  `docker-buildx-plugin`, `docker-compose-plugin`)
- the **`smart-node` Docker network** -- the routing fabric every
  Docker-companion attaches to (routing by container name, no host port
  mapping)
- `/etc/smart-node/traefik/` -- the Traefik config tree:
  - `traefik.yml` -- static config (entrypoints, providers, ACME)
  - `dynamic/` -- the file-provider directory; companions drop route
    fragments here via `lib.sh register_route` (`2750 root:smart-node`)
  - `certs/` -- the self-signed bootstrap certificate
- `/var/lib/smart-node/traefik/acme.json` -- ACME cert storage (`0600`)
- the **`smart-node-traefik` container** -- ports 80 (-> 443 redirect)
  and 443, the docker-provider (label routing) + file-provider,
  `--restart unless-stopped`
- `ufw allow 80/tcp` + `443/tcp` -- Layer A opens only SSH; Layer C owns
  the product's ports
- `/etc/smart-node/routing.env` -- the routing contract (network name,
  dynamic dir, domain, TLS resolver) later stages + companions read
- `Stage B03 -- routing` report section, `B03.state`

**TLS.** With a node domain (`SMART_NODE_DOMAIN`, threaded from
`install-on.sh --node-domain`) Traefik issues a real Let's Encrypt
certificate via the ACME HTTP-01 challenge. Without one, only the
self-signed bootstrap cert is configured -- the panel is still reachable
over HTTPS, and real ACME is enabled later from the B06 web interface.
The self-signed cert is also the file-provider **default certificate**,
so IP access and the dashboard never hit a Traefik throwaway cert.

The Traefik dashboard is built (`api.dashboard`) but **not exposed**
(`api.insecure=false`, no router) -- B05 identity adds an internal,
authenticated route to `api@internal`.

B03 depends on B01 (`require_stage B01`); in the linear bootstrap order
it runs after B02. Idempotent -- the Traefik container is recreated from
current config on a re-run. Verify runs 22 checks.

## Stage B04 -- secrets

Installs the secrets layer -- `sops` + `age` binaries + a per-node age
key + an empty sops-encrypted `integrations.sops.yaml`. From B04 onward,
B05 identity, the B06 web interface and the Phase 2 companions read
secrets via `sops -d` (or `sops exec-env`) against the encrypted file.
No containers, no DB, no Postgres role for B04 -- the layer is pure
file + binary on the host. This completes the Infisical excision.

Builds:

- `/usr/local/bin/sops` -- pinned sops binary (verified by sha256)
- `/usr/local/bin/age`, `/usr/local/bin/age-keygen` -- via apt (Debian
  12 main, age 1.1.1)
- `/etc/smart-node/age-key.txt` -- private age key, 0600 root
- `/etc/smart-node/age-recipient.txt` -- public age recipient, 0644
- `/etc/smart-node/.sops.yaml` -- sops config (pins recipient for the
  encrypted file)
- `/etc/smart-node/integrations.sops.yaml` -- empty sops-encrypted YAML
  (placeholder for external integration tokens added per-deployment)
- `Stage B04 -- secrets` report section, `B04.state`

The previous Infisical-based B04 was orphaned dead weight (~5 min
install + 700 MB on disk + 2 idle containers, never read by any
companion at runtime). It is removed entirely; the migration to
sops+age replaces the old secrets layer, and `bootstrap/B04-secrets.sh`
(the old script) is deleted in this commit. The current script is
`bootstrap/B04-secrets-slice.sh`.

B04 depends on B01 (`require_stage B01`). Idempotent -- an existing age
key is preserved on re-run, never regenerated. `uninstall-from.sh`
clears the four installed files; the age key is left in place (the
operator must `shred` it if rotating). Verify runs 8 checks.

## Artifact manifest + logging

Every stage records what it touches, so the install is observable and
cleanly reversible:

- **Manifest** -- `/var/lib/smart-node/manifest.jsonl`, append-only, one
  JSON object per host artifact (`{"type":...,"value":...,"stage":...,"ts":...}`).
  Each artifact-creating helper calls `manifest_record TYPE VALUE`
  (idempotent: a `type`+`value` pair already present is not re-appended).
  Types: `group user dir file package systemd-unit pg-ident pg-cluster
  docker-network docker-container ufw-rule`.
  `orchestrator/uninstall-from.sh` replays the manifest in reverse
  instead of using a hardcoded removal list -- so a new stage that
  records its artifacts is automatically uninstallable. Packages are
  recorded only when the stage actually installed them (not pre-existing
  base-image packages).
  Some types need a real handler, not `rm`: **`pg-cluster`** (B02) is
  torn down by `pg_dropcluster --stop`, **`docker-container`** /
  **`docker-network`** (B03) by `docker rm -f` / `docker network rm`,
  **`ufw-rule`** (B03) by `ufw delete`. `uninstall-from.sh` runs all of
  these -- and the jq-driven file/dir/user/group replay -- before any
  package purge (the purge can remove `jq` and `docker` themselves).

- **Per-stage log** -- `log_init STAGE_ID` points the log at
  `/var/log/smart-node/STAGE_ID.log`; `log()` and `die()` tee every line
  there in addition to stdout. The first lines of B01 (before
  `ensure_dirs` creates `/var/log/smart-node`) are stdout-only and tee
  in once the dir exists.

## Registries (mcp.d / companions.d)

Two file-based registries B01-core creates, `cron.d/`-style -- additive,
no daemon, no DB. Companion installers (Phase 2) populate them:

- **`/etc/smart-node/mcp.d/`** -- one JSON shim per MCP server.
  `register_mcp NAME FILE` drops it in; agent runtimes glob the dir at
  startup. `unregister_mcp NAME` removes it.
- **`/etc/smart-node/companions.d/`** -- one JSON manifest per installed
  companion: `{name, version, state, deps, ts}`. `register_companion`
  writes it; the **B06 web interface reads these to build companion
  cards** (label, version, install state, dependency gating).

Both dirs are `2750 root:smart-node` (setgid -- files dropped by
companion installers inherit the umbrella group). Files registered this
way are recorded in the manifest, so uninstall removes them.

## Cron jobs (cron.d convention)

A third file-based registry, same `cron.d/`-style as the registries
above -- additive, no daemon, no DB. At bootstrap **the cron.d file IS
the registry**; a DB-backed `cron_jobs` table is a later (B02+ /
companion) concern.

- **`cron-runner.sh`** -- a wrapper B01-core installs to
  `/usr/local/lib/smart-node/cron-runner.sh`. Every Smart Node cron job
  runs its command *through* this wrapper, which adds:
  - a **per-job lock** (`/run/lock/smart-node-cron-NAME.lock`, `flock -n`)
    -- if the previous tick of the same job is still running, this tick
    exits cleanly instead of stacking;
  - a **per-job run log** at `/var/log/smart-node/cron/NAME.log`
    (START/END + exit code + duration + captured output, size-capped at
    5 MiB with one `.1` rotation).
- **`register_cron NAME SCHEDULE COMMAND [USER]`** -- writes
  `/etc/cron.d/smart-node-NAME`: a `0644 root:root` cron.d file whose
  single job line invokes `cron-runner.sh NAME COMMAND`. `SCHEDULE` is a
  5-field cron spec; `USER` defaults to `root`. `unregister_cron NAME`
  removes the file. cron rescans `/etc/cron.d` every minute, so no
  service reload is needed.

The registered cron.d files are recorded in the manifest, so uninstall
removes them. `cron` is a B01 base package and the cron service is
enabled by B01-core.

## Lifecycle hooks

A small extension point so a stage or companion can run extra scripts at
defined moments of an install/uninstall. These are **install-lifecycle
hooks** -- not Claude Code-style agent hooks (`UserPromptSubmit`,
`PostToolUse`); agent hooks belong to the `agent-runtimes` companion.

B01-core creates `/etc/smart-node/hooks/` with two subdirs, the
`available`/`enabled` split (same idea as Apache `mods-available` /
`mods-enabled`):

- **`hooks/available/`** -- hook scripts live here, inert until enabled.
- **`hooks/enabled/`** -- a symlink per (event, hook): the link name is
  `EVENT.NAME`, the target is `../available/NAME`. The link name decides
  *when* the hook fires, so one script can be enabled for several events.

Lifecycle events: `pre-install`, `post-install`, `pre-uninstall`.

`lib.sh` provides:

- `register_hook EVENT NAME FILE` -- install `FILE` as
  `hooks/available/NAME` (`0750`) and symlink it into `hooks/enabled/`
  for `EVENT`. Re-callable to enable the same hook for another event.
- `unregister_hook NAME` -- remove the script and every enabled link.
- `run_hooks EVENT [ARG...]` -- the dispatcher. Runs every enabled hook
  for `EVENT` in sorted order, each with `SMART_NODE_HOOK_EVENT` exported
  and `ARG...` passed through. Returns the count of hooks that failed,
  so a caller can abort on a non-zero `pre-install` run.

Hook scripts and links are recorded in the manifest, so uninstall
removes them. B01-core only builds the infrastructure; no hooks ship
enabled by default.

## Event log

From B02 onward every meaningful step is journalled in the core `events`
table. `lib.sh` provides `log_event ACTOR EVENT_TYPE [ENTITY_TYPE]
[ENTITY_ID] [PAYLOAD]`, resilient by design -- it must never abort its
caller, so it degrades through three tiers:

1. **PostgreSQL** -- insert into `events`, once the persistence contract
   `/etc/smart-node/persistence.env` exists and the DB accepts the row.
2. **Buffer file** -- a JSON line appended to
   `/var/lib/smart-node/stages/events.buffer`, for events emitted before
   Postgres is up (B01, the orchestrator) or while it is unreachable.
3. **stderr** -- last resort, if even buffering fails.

`flush_event_buffer` drains tier 2 into the table; B02-persistence calls
it after the core schema is applied, preserving each buffered event's
original timestamp. `ACTOR` must be an id present in the `actors` table
(B02 seeds `system:installer`).

## Cross-refs

- Full installation sequence: `../orchestrator/install-on.sh`
- Local LXD provisioning: `../provision/provision-lxd-local.sh`
- Provider-independent host configuration: `../configure/configure-host.sh`
