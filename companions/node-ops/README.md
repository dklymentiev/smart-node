# node-ops -- cross-cutting node housekeeping

Tools that keep the node from filling up with its own exhaust, and that belong to
no single companion. Installed by `core-api.sh` into `/opt/smart-node/ops`, the
same directory as the gateway's session reaper, so an operator has one place to
look rather than one per companion.

The split between here and `companions/gateway-ops/` is by ownership, not by
taste: the session reaper kills processes the GATEWAY spawns, so it ships with
the gateway. What is here is about the node as a whole.

## retention.py

Prunes per-wake agent logs under `/srv/agents/<slug>/logs` older than
`SN_LOG_RETENTION_DAYS` (30), and measures journal growth. Run daily by
`smart-node-retention.timer`. Each sweep records one `node.retention_swept`
event carrying what it removed and the journal's size, so growth is noticed by
the node and not by a person reading `top` on a bad morning.

**It never touches `sn_core.events`, and must not be extended to.** That table is
append-only by trigger (`events_no_update_delete`), which is a decision rather
than an oversight: it is the only record of what the node did. Bounding it means
one of two measures:

- writing less, which is where the volume actually was. An eligibility poll that
  finds nothing is no longer journalled at all: on archive-node those were 8965 of 8986
  `tool.invoked` rows over 48 hours, against 21 for every write the node made.
- partitioning by `ts` and detaching old partitions, which deletes no row and
  never fires the trigger, but requires the primary key to carry the partition
  key and therefore costs the self-referencing `parent_event_id` causality link.

The second is an ADR-level call and is tracked as its own task. `retention.py`
only ever reads that table.

## A note on the window

A 30-day window does not clear a backlog younger than 30 days. When this shipped,
archive-node held ~22000 wake logs all dated from 2026-08-04 onward, so the first sweep
removed none of them; it caps growth, and the pile ages out from early September.
What actually stopped the pile growing was retiring the per-agent cron:
one file per wake, and the wakes fell by roughly two orders of magnitude.

## health-checker/health-probe.py

Probes every agent's granted tools and channels and upserts the result into
`sn_core.agent_health_checks`. Runs every 15 minutes from
`/etc/cron.d/smart-node-health-probe`.

**`sn_core.agent_health_checks` is the authoritative source for agent health
state on this node.** The panel reads it; the probe writes it. No other source
of truth for grant reachability exists.

Status values per grant:

- `ok` -- the grant was exercised and responded correctly
- `fail` -- the grant is broken or unreachable (error_detail explains why)
- `untestable` -- the grant cannot be safely probed from outside (mutating API
  calls, stdio tools that must run as the agent's OS user, etc.)

The probe is idempotent: re-running overwrites the previous result. It starts
no agent sessions and sends no messages. An on-demand run is available from the
panel (Refresh health button) via `POST /api/agents/health/refresh`, which
delegates to `admin-runner run_health_probe`.
