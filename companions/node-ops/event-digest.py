#!/usr/bin/env python3
"""Daily digest of hard-failure events from sn_core.events, e-mailed to the owner.

PURPOSE
The tick-side monitor (event-monitor.py) catches hot defects and alerts within
one tick. Quiet-but-recurring faults get drowned by its 30 min dedup window
and 50-event cap per e-mail. The daily digest reads the whole 24h slice,
groups by type and signature, and sends ONE summary e-mail per day: what
broke, how many times, by whom, what repeats, what is new vs the day before.

PAIRING WITH event-monitor.py
Both companions are node-ops. The monitor runs every 5 min and alerts on
tick-size batches; the digest runs daily and alerts on the pattern view.
They share the signal classification.
The classification sets live in companions/node-ops/_events_classify.py
and are imported by both; see that module for the data.

WHY NOT /api/events
Same reason as the monitor: /api/events caps the page at 1000 rows and
would silently drop a day's worth of failures on a bad day. We read
sn_core.events directly over the local psql socket via the core-api DB
role (identical pattern to event-monitor.py and retention.py).

WINDOW
Previous local calendar day in the server's timezone: [00:00 local,
24:00 local). The preceding day is read as a smaller side query to
compute deltas and the "new type today" line. Both ranges are reported
in the e-mail header as local and UTC.

An explicit date can be requested via SN_EVENT_DIGEST_DATE=YYYY-MM-DD
(the local date to summarise); default is "yesterday".

CLEAN DAY
If the window has zero signal events the digest still mails: subject
tagged "clean", body states the range and the volume of non-signal
events, so absence of mail is never confused with a broken digest.
This is the owner's explicit requirement.

STATE
No persistent cursor. The window boundary is derived from the clock
each run; a missed day is not backfilled. systemd Persistent=true on the
timer runs the job once after a reboot that spanned the boundary, which
covers the usual gap case. State the digest DOES keep is a small file for
fallback cooldowns (same shape as monitor), so repeated send failures do
not file one planner task per retry.

SENDING
POST /api/owner-alert on the local core-api socket: the same letter the
budget and turn guards send, from the node's own sender (SN_NOTIFY_SENDER)
to the owner's email channel as Settings has it. The digest names no person
and no mailbox; a node without a sender or an owner without an email channel
answers 409 and the digest takes the fallback below.

FALLBACK (owner unreachable)
- Append the digest body to /var/log/smart-node/event-monitor-alerts.log
  (same log the monitor uses on its own fallback).
- syslog via `logger -p daemon.err`.
- File one planner task per 24h (actor=component:event-monitor; attribution
  record via emitted event event_monitor.alert_failed).
- No retries from the hot path.

OBSERVABILITY
When fallback is taken the digest emits event_monitor.alert_failed with
actor=component:event-monitor. That event type is in NOISE_EXACT (see
_events_classify.py) so the digest cannot flag its own degraded mode on
tomorrow's run.

TIMEZONE
The window is "yesterday local" in the NODE's zone: the host zone the
operator set (/etc/timezone, else the /etc/localtime link), UTC when the
host names none. The timer fires at 09:00 host local time, so the window
and the moment of delivery follow the same zone. Nothing here names a zone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

STATE_FILE = Path(os.environ.get(
    "SN_EVENT_DIGEST_STATE",
    "/var/lib/smart-node/node-ops/state/event-digest.json",
))
ALERT_LOG = Path(os.environ.get(
    "SN_EVENT_DIGEST_ALERT_LOG",
    # Shared with the monitor on purpose: the operator reads one log, not two.
    "/var/log/smart-node/event-monitor-alerts.log",
))
CORE_SOCKET = os.environ.get("CORE_API_UDS", "/run/smart-node/api/core-api.sock")
PSQL_USER = os.environ.get("SN_EVENT_DIGEST_PSQL_USER", "smart-node-core-api")
PSQL_DB_USER = os.environ.get("SN_EVENT_DIGEST_PSQL_DB_USER", "smartnode")

TOP_REPEATS = int(os.environ.get("SN_EVENT_DIGEST_TOP_REPEATS", "10"))
TOP_ACTORS = int(os.environ.get("SN_EVENT_DIGEST_TOP_ACTORS", "15"))
TOP_CHANNELS = int(os.environ.get("SN_EVENT_DIGEST_TOP_CHANNELS", "10"))
TOP_CLEAN_TYPES = int(os.environ.get("SN_EVENT_DIGEST_TOP_CLEAN_TYPES", "3"))
ERROR_TRUNC = int(os.environ.get("SN_EVENT_DIGEST_ERROR_TRUNC", "120"))
FALLBACK_COOLDOWN_SEC = int(os.environ.get("SN_EVENT_DIGEST_FALLBACK_COOLDOWN", "86400"))
DRYRUN = os.environ.get("SN_EVENT_DIGEST_DRYRUN", "").strip().lower() in {"1", "true", "yes"}

# Explicit date override. Takes a YYYY-MM-DD local calendar date; default is
# "yesterday local". The window is [date 00:00 local, date+1 00:00 local).
_date_env = os.environ.get("SN_EVENT_DIGEST_DATE", "").strip()

# The node's zone for "local". ZoneInfo(...) by name, NOT
# datetime.now().astimezone(): the latter returns a frozen fixed-offset tzinfo
# captured at process start, which gives the wrong offset for any target_date
# in a different DST state than "now". ZoneInfo handles DST transitions.

def _host_zone_name() -> str:
    """The zone the host is set to: TZ if the environment sets one, then
    /etc/timezone, then the zone the /etc/localtime link points at, else UTC."""
    env = os.environ.get("TZ", "").strip().lstrip(":")
    if env:
        return env
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


_TZ_NAME = _host_zone_name()
try:
    LOCAL_TZ = ZoneInfo(_TZ_NAME)
except Exception as _e:
    raise RuntimeError(
        f"host zone {_TZ_NAME!r} is not a known zone (ZoneInfo refused): {_e}"
    )

# -----------------------------------------------------------------------------
# CLASSIFICATION -- data lives in _events_classify.py and is shared with
# event-monitor.py so the two tools never disagree on what counts as signal.
# -----------------------------------------------------------------------------

from _events_classify import (
    MONITOR_ACTOR,
    NOISE_EXACT,
    SIGNAL_EXACT,
    SIGNAL_SUFFIX,
    is_signal,
)


# -----------------------------------------------------------------------------
# Journal access -- direct SQL via core-api psql role
# -----------------------------------------------------------------------------

def _psql(sql: str, label: str = "query") -> str:
    """Run a short read-only query as the core-api DB role.

    The digest service runs as root under systemd; `sudo -u <role> psql` is
    the pattern retention.py uses for the same reason. We do not rely on
    /api/events, which caps the page at 1000 and would drop rows on a storm.
    """
    r = subprocess.run(
        ["sudo", "-u", PSQL_USER, "psql", "-U", PSQL_DB_USER, "-d", "smartnode",
         "-At", "-F", "\x1f", "-R", "\x1e", "-v", "ON_ERROR_STOP=1",
         "-c", sql],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"psql {label} failed: {(r.stderr or '').strip()[:400]}"
        )
    return r.stdout


def _parse_rows(out: str, expected_cols: int) -> list[list[str]]:
    rows = []
    if not out:
        return rows
    for raw in out.split("\x1e"):
        raw = raw.rstrip("\n")
        if not raw:
            continue
        parts = raw.split("\x1f")
        if len(parts) != expected_cols:
            continue
        rows.append(parts)
    return rows


# -----------------------------------------------------------------------------
# Window arithmetic
# -----------------------------------------------------------------------------

def _resolve_window(now_local: datetime) -> tuple[date, datetime, datetime]:
    """Return (target_date, local_start, local_end) for the digest window.

    target_date is the local calendar date summarised. By default this is
    "yesterday local in LOCAL_TZ"; SN_EVENT_DIGEST_DATE overrides it.

    local_start/_end are tz-aware datetimes in LOCAL_TZ (ZoneInfo), so
    the %Z abbreviation and the offset reflect the target date's DST
    state, not whatever the system TZ was when the process started.
    """
    if _date_env:
        try:
            target = date.fromisoformat(_date_env)
        except ValueError as e:
            raise RuntimeError(
                f"SN_EVENT_DIGEST_DATE must be YYYY-MM-DD, got {_date_env!r} ({e})"
            )
    else:
        target = (now_local.astimezone(LOCAL_TZ) - timedelta(days=1)).date()
    # Attach LOCAL_TZ directly so DST-transition days still bound the
    # LOCAL calendar day, not a fixed 24h offset. [00:00 local target,
    # 00:00 local target+1) is 23h or 25h long on DST change days.
    local_start = datetime(target.year, target.month, target.day,
                           tzinfo=LOCAL_TZ)
    next_day = target + timedelta(days=1)
    local_end = datetime(next_day.year, next_day.month, next_day.day,
                         tzinfo=LOCAL_TZ)
    return target, local_start, local_end


def _fmt_range(local_start: datetime, local_end: datetime) -> str:
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)
    local_tz = local_start.tzinfo
    return (
        f"local {local_start:%Y-%m-%d %H:%M %Z} .. {local_end:%Y-%m-%d %H:%M %Z}"
        f" (UTC {utc_start:%Y-%m-%d %H:%M} .. {utc_end:%Y-%m-%d %H:%M})"
    )


# -----------------------------------------------------------------------------
# Signal filter in SQL
# -----------------------------------------------------------------------------

def _signal_where() -> str:
    """SQL fragment that matches a signal event. Same semantics as
    is_signal() in _events_classify.py (exact set OR suffix match,
    excluding NOISE_EXACT), expressed in SQL so the filter happens at
    the row scan, not after fetch."""
    exacts = ",".join(f"'{t}'" for t in sorted(SIGNAL_EXACT))
    noise = ",".join(f"'{t}'" for t in sorted(NOISE_EXACT))
    suffix_clauses = " OR ".join(
        f"event_type LIKE '%{suf}'" for suf in SIGNAL_SUFFIX
    )
    return (
        f"((event_type IN ({exacts})) OR ({suffix_clauses})) "
        f"AND event_type NOT IN ({noise})"
    )


def _ts_bounds(local_start: datetime, local_end: datetime) -> tuple[str, str]:
    # psql interprets a bare 'YYYY-MM-DD HH:MM:SS+OO' literal as a timestamptz;
    # the local-time range maps correctly to UTC on both sides.
    return (local_start.isoformat(), local_end.isoformat())


# -----------------------------------------------------------------------------
# Queries
# -----------------------------------------------------------------------------

def _count_total(ts_from: str, ts_to: str) -> int:
    out = _psql(
        f"SELECT COUNT(*) FROM sn_core.events "
        f"WHERE ts >= '{ts_from}' AND ts < '{ts_to}'",
        label="count_total",
    ).strip()
    return int(out) if out else 0


def _counts_by_type(ts_from: str, ts_to: str, signal_only: bool) -> list[tuple[str, int]]:
    where_extra = f" AND {_signal_where()}" if signal_only else ""
    out = _psql(
        f"SELECT event_type, COUNT(*) FROM sn_core.events "
        f"WHERE ts >= '{ts_from}' AND ts < '{ts_to}'{where_extra} "
        f"GROUP BY event_type ORDER BY COUNT(*) DESC, event_type ASC",
        label="counts_by_type",
    )
    rows = _parse_rows(out, 2)
    return [(r[0], int(r[1])) for r in rows]


def _top_repeats(ts_from: str, ts_to: str) -> list[dict]:
    """Top signal signatures. A signature is (event_type, actor, truncated
    error text). We truncate to ERROR_TRUNC chars so near-identical errors
    with a trailing stack trace or timestamp still cluster."""
    out = _psql(
        f"SELECT event_type, actor, "
        f"COALESCE(substring(payload->>'error' FROM 1 FOR {ERROR_TRUNC}), ''), "
        f"COUNT(*), MIN(ts), MAX(ts) "
        f"FROM sn_core.events "
        f"WHERE ts >= '{ts_from}' AND ts < '{ts_to}' AND {_signal_where()} "
        f"GROUP BY event_type, actor, "
        f"COALESCE(substring(payload->>'error' FROM 1 FOR {ERROR_TRUNC}), '') "
        f"ORDER BY COUNT(*) DESC, event_type ASC, actor ASC "
        f"LIMIT {TOP_REPEATS}",
        label="top_repeats",
    )
    rows = _parse_rows(out, 6)
    return [
        {
            "event_type": r[0],
            "actor": r[1],
            "error": r[2],
            "count": int(r[3]),
            "first_ts": r[4],
            "last_ts": r[5],
        }
        for r in rows
    ]


def _affected_actors(ts_from: str, ts_to: str) -> list[tuple[str, int]]:
    out = _psql(
        f"SELECT actor, COUNT(*) FROM sn_core.events "
        f"WHERE ts >= '{ts_from}' AND ts < '{ts_to}' AND {_signal_where()} "
        f"GROUP BY actor ORDER BY COUNT(*) DESC, actor ASC "
        f"LIMIT {TOP_ACTORS}",
        label="affected_actors",
    )
    rows = _parse_rows(out, 2)
    return [(r[0], int(r[1])) for r in rows]


def _affected_channels(ts_from: str, ts_to: str) -> list[tuple[str, int]]:
    # payload->>'channel' is TEXT or NULL. We filter nulls out and keep
    # anything else (telegram/email/voice/web-chat/desk/...). No whitelist:
    # the journal is the source of truth, if a new channel appears it should
    # show up without a code change.
    out = _psql(
        f"SELECT payload->>'channel' AS ch, COUNT(*) FROM sn_core.events "
        f"WHERE ts >= '{ts_from}' AND ts < '{ts_to}' AND {_signal_where()} "
        f"AND payload ? 'channel' AND payload->>'channel' <> '' "
        f"GROUP BY ch ORDER BY COUNT(*) DESC, ch ASC "
        f"LIMIT {TOP_CHANNELS}",
        label="affected_channels",
    )
    rows = _parse_rows(out, 2)
    return [(r[0], int(r[1])) for r in rows]


# -----------------------------------------------------------------------------
# Body composition
# -----------------------------------------------------------------------------

def _digest_body(target: date, local_start: datetime, local_end: datetime,
                 total: int, signal_counts_today: list[tuple[str, int]],
                 signal_counts_yesterday: list[tuple[str, int]],
                 top_repeats: list[dict], actors: list[tuple[str, int]],
                 channels: list[tuple[str, int]],
                 all_counts_today: list[tuple[str, int]]) -> str:
    """Produce the plain-text body of the digest e-mail."""
    today_map = dict(signal_counts_today)
    yesterday_map = dict(signal_counts_yesterday)
    signal_total = sum(today_map.values())

    lines: list[str] = []
    lines.append(f"Daily event digest for {target.isoformat()}.")
    lines.append(_fmt_range(local_start, local_end))
    lines.append(
        f"Events total: {total}. Signal events: {signal_total}."
        f" Affected actors: {len(actors)}."
    )
    lines.append("")

    if signal_total == 0:
        lines.append("No signal events in the window. Clean day.")
        lines.append("")
        if all_counts_today:
            lines.append(f"Top {TOP_CLEAN_TYPES} event type(s) overall for context:")
            for et, c in all_counts_today[:TOP_CLEAN_TYPES]:
                lines.append(f"  {c:>8}  {et}")
        return "\n".join(lines)

    # Counts by signal type, with delta vs the preceding day.
    lines.append("Signal events by type (count today, delta vs previous day):")
    all_types = sorted(set(today_map) | set(yesterday_map),
                       key=lambda t: (-today_map.get(t, 0), t))
    for et in all_types:
        today_n = today_map.get(et, 0)
        yesterday_n = yesterday_map.get(et, 0)
        delta = today_n - yesterday_n
        if today_n == 0 and yesterday_n > 0:
            tag = "gone"
            lines.append(f"  {today_n:>8}  {et}  (was {yesterday_n}, {tag})")
        elif yesterday_n == 0 and today_n > 0:
            lines.append(f"  {today_n:>8}  {et}  (new)")
        else:
            sign = "+" if delta >= 0 else ""
            lines.append(f"  {today_n:>8}  {et}  ({sign}{delta})")
    lines.append("")

    # New types line (today's signal set minus yesterday's).
    new_types = [et for et in today_map if et not in yesterday_map]
    if new_types:
        new_types.sort(key=lambda t: (-today_map[t], t))
        lines.append("New signal types today (not seen yesterday):")
        for et in new_types:
            lines.append(f"  {today_map[et]:>8}  {et}")
        lines.append("")

    # Top repeated signatures.
    if top_repeats:
        lines.append(f"Top {len(top_repeats)} repeated signatures (same type + actor + error):")
        for r in top_repeats:
            err = r["error"].replace("\n", " ")[:ERROR_TRUNC]
            if not err:
                err = "<no error field>"
            lines.append(
                f"  {r['count']:>5}  {r['event_type']}  by {r['actor']}"
            )
            lines.append(f"         {r['first_ts']} .. {r['last_ts']}")
            lines.append(f"         {err}")
        lines.append("")

    # Affected actors.
    if actors:
        lines.append(f"Affected actors (top {min(len(actors), TOP_ACTORS)}):")
        for actor, c in actors:
            lines.append(f"  {c:>6}  {actor}")
        lines.append("")

    # Affected channels.
    if channels:
        lines.append(f"Affected channels (top {min(len(channels), TOP_CHANNELS)}):")
        for ch, c in channels:
            lines.append(f"  {c:>6}  {ch}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# -----------------------------------------------------------------------------
# State (fallback cooldowns only)
# -----------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            s.setdefault("last_fallback_at", 0.0)
            s.setdefault("last_alert_failed", 0.0)
            s.setdefault("last_run_at", 0.0)
            s.setdefault("last_target_date", "")
            return s
        except Exception:
            pass
    return {"last_fallback_at": 0.0, 
            "last_alert_failed": 0.0, "last_run_at": 0.0,
            "last_target_date": ""}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(STATE_FILE)


# -----------------------------------------------------------------------------
# Observability
# -----------------------------------------------------------------------------

def _emit_event(event_type: str, payload: dict) -> None:
    body = json.dumps({
        "actor": MONITOR_ACTOR,
        "event_type": event_type,
        "entity_type": "node",
        "entity_id": "self",
        "payload": payload,
    })
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", "--unix-socket", CORE_SOCKET,
             "-H", "Content-Type: application/json",
             "-d", body, "http://core-api/api/events"],
            capture_output=True, text=True, timeout=5,
        )
        if not (r.stdout or "").startswith("2"):
            print(f"[emit] {event_type} HTTP {r.stdout or '?'}", file=sys.stderr)
    except Exception as e:
        print(f"[emit] {event_type} failed: {e}", file=sys.stderr)


# -----------------------------------------------------------------------------
# Send path
# -----------------------------------------------------------------------------

def _send_digest(subject: str, body: str) -> tuple[bool, int | None, str]:
    """Queue the digest for the owner through core-api. (ok, message id, error)."""
    payload = json.dumps({"subject": subject, "text": body, "source": "event-digest"})
    try:
        r = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", "-X", "POST",
             "--unix-socket", CORE_SOCKET,
             "-H", "Content-Type: application/json",
             "--data-binary", "@-", "http://core-api/api/owner-alert"],
            input=payload, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        return False, None, f"timeout: {e}"
    out, _, code = (r.stdout or "").rpartition("\n")
    if not code.startswith("2"):
        detail = out.strip()
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        return False, None, f"HTTP {code or '?'}: {str(detail)[:300]}"
    try:
        return True, int(json.loads(out)["message_id"]), ""
    except Exception:
        return True, None, ""


def _fallback_write(subject: str, body: str, err: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts}  DIGEST-SEND-FAILED  {err}\n")
        fh.write(f"{ts}  SUBJECT  {subject}\n")
        for line in body.splitlines():
            fh.write(f"{ts}  BODY     {line}\n")
        fh.write(f"{ts}  END\n")
    try:
        subprocess.run(
            ["logger", "-t", "sn-event-digest", "-p", "daemon.err",
             f"daily digest send failed: {err[:200]}"],
            check=False, timeout=5,
        )
    except Exception:
        pass


def _fallback_file_task(subject: str, body: str, state: dict, now: float) -> None:
    if now - float(state.get("last_fallback_at") or 0) < FALLBACK_COOLDOWN_SEC:
        return
    task = {
        "title": "sn-event-digest: daily digest send is failing",
        "description": (
            "Filed by sn-event-digest (actor=component:event-monitor).\n\n"
            "The daily event digest could not queue the summary e-mail. "
            "Falling back to the local alert log and syslog. See"
            f" {ALERT_LOG} for the full digest(s).\n\n"
            f"Last failed subject: {subject}\n\n"
            f"Last failed body:\n{body}\n"
        ),
        "priority": "high",
    }
    body_json = json.dumps(task)
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--unix-socket", CORE_SOCKET,
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", body_json,
             "http://core-api/api/planner/api/v1/tasks"],
            capture_output=True, text=True, timeout=5,
        )
        code = (r.stdout or "").strip()
    except Exception as e:
        code = f"err:{e}"
    state["last_fallback_at"] = now
    with ALERT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}  "
            f"DIGEST-FALLBACK-TASK-FILE  code={code}\n"
        )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    # The owner's choice (Settings); a mail nobody asked for is not sent.
    if _psql("SELECT daily_digest FROM sn_config.node_control WHERE id = true", "switch").strip() != "t":
        return 0
    now = time.time()
    state = _load_state()

    # The server's local timezone is implied by datetime.now() (naive) in
    # concert with the OS clock; use astimezone() to attach the system tz so
    # the ISO strings we hand to psql are timezone-aware.
    now_local = datetime.now().astimezone()
    target, local_start, local_end = _resolve_window(now_local)
    ts_from, ts_to = _ts_bounds(local_start, local_end)

    # Preceding window of same length for delta.
    prev_start = local_start - timedelta(days=1)
    prev_end = local_start
    ts_from_prev, ts_to_prev = _ts_bounds(prev_start, prev_end)

    total_today = _count_total(ts_from, ts_to)
    signal_today = _counts_by_type(ts_from, ts_to, signal_only=True)
    signal_yesterday = _counts_by_type(ts_from_prev, ts_to_prev, signal_only=True)
    repeats = _top_repeats(ts_from, ts_to) if signal_today else []
    actors = _affected_actors(ts_from, ts_to) if signal_today else []
    channels = _affected_channels(ts_from, ts_to) if signal_today else []
    all_today = _counts_by_type(ts_from, ts_to, signal_only=False)

    signal_total = sum(c for _, c in signal_today)
    types_touched = len({t for t, _ in signal_today})

    if signal_total == 0:
        subject = f"[sn-digest] {target.isoformat()} clean"
    else:
        subject = (
            f"[sn-digest] {target.isoformat()}"
            f" {signal_total} signal event(s) across {types_touched} type(s)"
        )

    body = _digest_body(target, local_start, local_end, total_today,
                        signal_today, signal_yesterday, repeats, actors,
                        channels, all_today)

    if DRYRUN:
        # Print body to stdout so operators can rehearse without sending mail.
        # Nothing else happens: no send, no state write, no event emit.
        sys.stdout.write(f"SUBJECT: {subject}\n\n{body}")
        print(
            f"[dryrun] target={target.isoformat()} signal_total={signal_total}"
            f" types_touched={types_touched} total_events={total_today}",
            file=sys.stderr,
        )
        return 0

    ok, msg_id, primary_err = _send_digest(subject, body)
    if ok:
        state["last_run_at"] = now
        state["last_target_date"] = target.isoformat()
        _save_state(state)
        print(
            f"[digest] target={target.isoformat()} signal_total={signal_total}"
            f" types_touched={types_touched} total_events={total_today}"
            f" msg_id={msg_id}",
            file=sys.stderr,
        )
        return 0

    # The owner could not be reached. Second independent path.
    _fallback_write(subject, body, primary_err or "")
    _fallback_file_task(subject, body, state, now)
    if now - float(state.get("last_alert_failed") or 0) >= FALLBACK_COOLDOWN_SEC:
        _emit_event("event_monitor.alert_failed", {
            "component": "event-digest",
            "reason": primary_err[:400] if primary_err else "unknown",
            "signal_total": signal_total,
            "total_events": total_today,
            "target_date": target.isoformat(),
            "cooldown_until": now + FALLBACK_COOLDOWN_SEC,
        })
        state["last_alert_failed"] = now
    state["last_run_at"] = now
    state["last_target_date"] = target.isoformat()
    _save_state(state)
    print(
        f"[digest-fallback] target={target.isoformat()} signal_total={signal_total}"
        f" total_events={total_today} reason={primary_err}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        try:
            ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ALERT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(
                    time.strftime("%Y-%m-%dT%H:%M:%S%z") +
                    f"  DIGEST-CRASH  {type(e).__name__}: {str(e)[:400]}\n"
                )
        except Exception:
            pass
        print(f"[crash] {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
