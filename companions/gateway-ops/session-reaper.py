#!/usr/bin/env python3
"""Reap idle / orphaned agent CLI sessions, by LAST ACTIVITY, never by age.

A `claude --print` agent process is killed only if its session's
sn_core.ai_sessions.last_activity_at is older than IDLE_MIN minutes -- so a
session that is actively working (recent activity, however long the turn) is
NEVER killed. The process's --session-id IS the ai_sessions.id, so the signal
is per-session (the gateway's own reaper only walks its in-memory registry and
misses orphans -- sessions dropped from the registry while the process lived).

Every kill is recorded as a `session.reaped` event for audit, and the session
row is closed (status=reaped, completed_at=now).

ORPHAN SWEEP: A second sweep runs after the idle-reap loop. Sessions
with status=active and no corresponding live process and last_activity_at older
than ORPHAN_MIN minutes (default 60) are cancelled. These are sessions whose
process vanished on its own (gateway restart, host reboot, turn crash) and
would otherwise stay status=active forever. The sweep records a
`session.cancelled` event and closes the row.

Runs as root (can kill any PID); reads the DB via the core-api db role over the
local socket. Scheduled by smart-node-session-reaper.timer.

PROVENANCE: written on reference-node in June 2026 and living in no repository
until 2026-08-13, when it was copied here VERBATIM. On 2026-08-12 five leaked
`claude --print` children were killed by hand across the fleet: four on archive-node,
which does not have this, alive since Aug 8 with ninety minutes of CPU each, and
one on reference-node, which does. A small sample and not proof, but consistent with this
being why reference-node's pile stays bounded.

THIS IS THE SWEEP, NOT THE FIX. The leak is the gateway owning no
lifetime for what it spawns. A reaper running every five minutes HIDES an
unbounded leak; it does not close it. Shipping this must not be read as closing


WHAT "IDLE" MEANS HERE, read before trusting it:
  - The clock is `sn_core.ai_sessions.last_activity_at`, which the gateway bumps
    in session_service on EVERY message row it writes -- assistant turns and tool
    results included. A session that is working bumps it continuously; it is not
    stamped once at the start. That is what makes the paragraph above true.
  - CAN IT KILL A LIVE SESSION? Not one whose turn the gateway is running. This
    used to say a Runner wake could not reach IDLE_MIN because the Runner caps
    the call at 310s. That stopped being true when turns began to outlive the
    request that started them: a CLI turn writes its one message row at the END,
    so a twenty-minute turn had a fifteen-minute-old clock and was killed here
    while it worked. The gateway now stamps last_activity_at for every turn in
    flight on each of its sweeps (backends/cli_claude.py, _touch_session), once
    a minute against this script's fifteen, and the turn's registered ceiling is
    what bounds a turn. What this still reaps is a process with no turn: a chat
    session silent for fifteen minutes, or a child whose gateway is gone.
  - A process whose session id is NOT in the table is SPARED, not killed.
    Unknown means unknown, and the conservative direction is to leave it running
    and let it show up as a leak.
  - Kill is SIGTERM, five seconds of grace, then SIGKILL for stragglers only.
  - Every kill writes a `session.reaped` event carrying the pid, the measured
    idle minutes and the threshold, and closes the session row (status=reaped).
    The actor row is registered once up front so the events FK always resolves.
"""
import os, glob, re, signal, subprocess, time, json

IDLE_MIN = float(os.environ.get("SESSION_REAPER_IDLE_MIN", "15"))
ORPHAN_MIN = float(os.environ.get("SESSION_REAPER_ORPHAN_MIN", "60"))


def psql(sql, label=None):
    """Run ONE statement. Errors are PRINTED, never swallowed.

    This helper used to return r.stdout and drop stderr on the floor, which is
    how the audit half of this script stayed broken from June to August: the
    status UPDATE violated a CHECK constraint on every reap, psql failed, nothing
    was printed, and the timer reported success.

    Callers pass one statement each. They used to pass the audit INSERT and the
    status UPDATE as a single string, and psql runs a multi-statement -c inside
    ONE transaction -- so the failing UPDATE rolled the event back with it. The
    result was exactly inverted: a session that was open when it was reaped
    recorded nothing, while an already-closed session recorded an event, because
    then the UPDATE matched zero rows and raised nothing."""
    r = subprocess.run(
        ["sudo", "-u", "smart-node-core-api", "psql", "-U", "smartnode", "-d", "smartnode",
         "-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        print("session-reaper: %s FAILED: %s"
              % (label or "query", (r.stderr or "").strip().replace("\n", " ")))
    return r.stdout


# 1. live claude agent processes -> their --session-id (== ai_sessions.id)
procs = {}
for d in glob.glob("/proc/[0-9]*"):
    pid = d.rsplit("/", 1)[-1]
    try:
        cl = open(f"{d}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        continue
    if "claude --print" not in cl:
        continue
    m = re.search(r"--session-id ([0-9a-fA-F-]{36})", cl)
    if m:
        procs[m.group(1)] = int(pid)

if not procs:
    print("session-reaper: no claude agent sessions live")

# 2. idle minutes per session, in one query (only relevant when there are live procs)
idle = {}
if procs:
    ids = ",".join("'%s'" % s for s in procs)
    rows = psql("SELECT id, EXTRACT(EPOCH FROM (now()-last_activity_at))/60 "
                f"FROM sn_core.ai_sessions WHERE id::text IN ({ids})")
    for line in rows.splitlines():
        if "|" in line:
            sid, mins = line.split("|", 1)
            try:
                idle[sid] = float(mins)
            except ValueError:
                pass

# 3. reap only the idle ones; spare active (recent activity) and unknown sessions
reaped = []
for sid, pid in procs.items():
    m = idle.get(sid)
    if m is None or m < IDLE_MIN:
        continue  # unknown -> conservative; recent -> actively working, NEVER kill
    try:
        os.kill(pid, signal.SIGTERM)
        reaped.append((sid, pid, round(m, 1)))
    except ProcessLookupError:
        pass

# grace, then SIGKILL any straggler
if reaped:
    time.sleep(5)
    for sid, pid, m in reaped:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

# 4. Orphan sweep: sessions status=active with no live process and stale activity.
# These are sessions whose process vanished on its own (gateway restart, host
# reboot, turn crash). They have no PID to measure idleness against, so the
# idle-reap loop above never touches them. Without this sweep they stay
# status=active forever. Migration 032 cleared the backlog; this keeps
# future ones from accumulating.
rows = psql(
    "SELECT id::text FROM sn_core.ai_sessions "
    "WHERE status='active' AND completed_at IS NULL "
    f"AND EXTRACT(EPOCH FROM (now()-last_activity_at))/60 > {ORPHAN_MIN}",
    label="orphan sweep query")
orphan_ids = [
    sid for sid in (line.strip() for line in rows.splitlines() if line.strip())
    if sid not in procs  # a live process may have a stale last_activity_at; leave it
]

# 5. Audit + close. Register the actor ONCE before any event inserts (FK).
if reaped or orphan_ids:
    psql("INSERT INTO sn_core.actors (id,display_name,type,layer) "
         "VALUES ('component:session-reaper','component:session-reaper','component','system') "
         "ON CONFLICT (id) DO NOTHING", label="actor register")

# Idle-reap audit + close.
# SEPARATE statements, deliberately. Together in one -c they are one
# transaction, and for two months the UPDATE failed a CHECK constraint and
# silently took the audit event down with it. The event is the thing we most
# want to survive: a kill nobody can see is worse than a status column that
# is behind. 'reaped' became a legal status in migration 025.
for sid, pid, m in reaped:
    payload = json.dumps({"pid": pid, "idle_minutes": m, "threshold_minutes": IDLE_MIN,
                          "reason": "idle past threshold"})
    psql("INSERT INTO sn_core.events (actor,event_type,entity_type,entity_id,payload) "
         f"VALUES ('component:session-reaper','session.reaped','ai_session','{sid}','{payload}'::jsonb)",
         label=f"audit event for {sid[:8]}")
    psql("UPDATE sn_core.ai_sessions SET status='reaped', completed_at=now() "
         f"WHERE id::text='{sid}' AND completed_at IS NULL",
         label=f"close session {sid[:8]}")

# Orphan audit + close (status='cancelled', matching migration 032 convention).
for sid in orphan_ids:
    payload = json.dumps({"threshold_minutes": ORPHAN_MIN,
                          "reason": "orphaned: no live process and stale activity"})
    psql("INSERT INTO sn_core.events (actor,event_type,entity_type,entity_id,payload) "
         f"VALUES ('component:session-reaper','session.cancelled','ai_session','{sid}','{payload}'::jsonb)",
         label=f"audit event for orphan {sid[:8]}")
    psql("UPDATE sn_core.ai_sessions SET status='cancelled', completed_at=now() "
         f"WHERE id::text='{sid}' AND status='active' AND completed_at IS NULL",
         label=f"cancel orphan {sid[:8]}")

print(
    f"session-reaper: {len(procs)} live, "
    f"reaped {len(reaped)} (idle>={IDLE_MIN}m): "
    + ", ".join(f"{s[:8]}/{p}/{m}m" for s, p, m in reaped)
    + f" | orphans cancelled {len(orphan_ids)} (stale>{ORPHAN_MIN}m): "
    + ", ".join(s[:8] for s in orphan_ids)
)
