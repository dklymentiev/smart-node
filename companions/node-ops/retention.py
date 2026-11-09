#!/usr/bin/env python3
"""Prune the per-wake agent logs, and MEASURE what nothing prunes.

Two jobs, deliberately in one place, because the second is what stops the first
from being needed again in silence.

1. PRUNE. Every wake writes a log file under /srv/agents/<slug>/logs and nothing
   has ever removed one. Measured 2026-08-12 on archive-node, an essentially idle node:
   11145 files for foreman and 10664 for alex, one per wake since 2026-08-04.
   An agent's own log directory becomes unreadable BY THE AGENT IT BELONGS TO
   long before it becomes a disk problem.

2. MEASURE. Report journal rows/day and files-per-agent, and record them as one
   event, so growth is noticed by the node rather than by a person reading `top`
   on a bad morning.

WHAT THIS DOES NOT DO, and must not be extended to do: touch sn_core.events. The
journal is append-only by trigger (events_no_update_delete -> events_immutable),
and that is a decision, not an oversight -- it is the only record of what the
node did. Bounding it means either writing less (done for the dominant source:
an eligibility poll that finds nothing is no longer journalled at all) or
partitioning and detaching, which needs the primary key to carry the partition
key and so costs the parent_event_id causality link. That is an ADR-level call
and it is tracked separately. This script only ever READS that table.
"""
import json
import os
import subprocess
import sys
import time

AGENTS_ROOT = os.environ.get("SN_AGENTS_ROOT", "/srv/agents")
RETENTION_DAYS = float(os.environ.get("SN_LOG_RETENTION_DAYS", "30"))
DRY_RUN = os.environ.get("SN_RETENTION_DRY_RUN", "0") == "1"
CORE_SOCKET = os.environ.get("CORE_API_UDS", "/run/smart-node/api/core-api.sock")


def psql(sql, label="query"):
    """Read-only helper. Errors are printed, never swallowed."""
    r = subprocess.run(
        ["sudo", "-u", "smart-node-core-api", "psql", "-U", "smartnode", "-d", "smartnode",
         "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        print("retention: %s FAILED: %s"
              % (label, (r.stderr or "").strip().replace("\n", " ")))
        return ""
    return r.stdout.strip()


def prune_agent_logs():
    """Delete wake logs older than the window. Returns (removed, kept, per_agent)."""
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = kept = 0
    per_agent = {}
    if not os.path.isdir(AGENTS_ROOT):
        return 0, 0, per_agent
    for slug in sorted(os.listdir(AGENTS_ROOT)):
        logdir = os.path.join(AGENTS_ROOT, slug, "logs")
        # realpath + prefix check: this deletes files, so it never follows a
        # symlink out of the tree it is allowed to touch.
        if not os.path.isdir(logdir):
            continue
        if not os.path.realpath(logdir).startswith(os.path.realpath(AGENTS_ROOT) + os.sep):
            print("retention: skipping %s -- resolves outside %s" % (logdir, AGENTS_ROOT))
            continue
        agent_removed = agent_kept = 0
        for name in os.listdir(logdir):
            path = os.path.join(logdir, name)
            try:
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                if os.path.getmtime(path) >= cutoff:
                    agent_kept += 1
                    continue
                if not DRY_RUN:
                    os.unlink(path)
                agent_removed += 1
            except OSError as e:
                print("retention: could not handle %s: %s" % (path, e))
        removed += agent_removed
        kept += agent_kept
        per_agent[slug] = {"removed": agent_removed, "kept": agent_kept}
    return removed, kept, per_agent


def measure_journal():
    """Rows, size and rows/day for sn_core.events. READ ONLY."""
    out = psql("SELECT count(*), pg_total_relation_size('sn_core.events'), "
               "(SELECT count(*) FROM sn_core.events WHERE ts > now()-interval '24 hours') "
               "FROM sn_core.events", label="journal measure")
    try:
        rows, size_bytes, per_day = out.split("|")
        return {"rows": int(rows), "bytes": int(size_bytes), "rows_last_24h": int(per_day)}
    except Exception:
        return {"rows": None, "bytes": None, "rows_last_24h": None}


def emit(payload):
    """One event per sweep. Best effort: a housekeeping run must not fail because
    the journal could not record that it happened."""
    body = json.dumps({"actor": "component:retention", "event_type": "node.retention_swept",
                       "entity_type": "node", "entity_id": "self", "payload": payload})
    r = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
         "--unix-socket", CORE_SOCKET, "-H", "Content-Type: application/json",
         "-d", body, "http://core-api/api/events"],
        capture_output=True, text=True)
    if not r.stdout.startswith("2"):
        print("retention: could not record the sweep (HTTP %s)" % (r.stdout or "?"))


def main():
    removed, kept, per_agent = prune_agent_logs()
    journal = measure_journal()
    payload = {
        "log_retention_days": RETENTION_DAYS,
        "logs_removed": removed,
        "logs_kept": kept,
        "per_agent": per_agent,
        "journal": journal,
        "dry_run": DRY_RUN,
    }
    if not DRY_RUN:
        emit(payload)
    noisiest = sorted(per_agent.items(), key=lambda kv: -kv[1]["kept"])[:3]
    print("retention: %s%d wake log(s) removed, %d kept (window %.0fd)%s"
          % ("[DRY RUN] " if DRY_RUN else "", removed, kept, RETENTION_DAYS,
             "".join("; %s=%d" % (s, v["kept"]) for s, v in noisiest)))
    if journal["rows"] is not None:
        print("retention: journal %d rows, %.1f MB, %d in the last 24h "
              "(append-only -- measured, never trimmed)"
              % (journal["rows"], journal["bytes"] / 1048576.0, journal["rows_last_24h"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
