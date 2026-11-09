"""Shared limit resolution and decisions for core-api and the gateway.

This file is installed unchanged into both Python services. The database is the
only source of values; callers supply the measured usage and their local event
and notification adapters. No process environment participates in decisions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal


class MissingLimit(RuntimeError):
    pass


def turn_limit_phrase(seconds: float) -> str:
    """How a turn ceiling is named to a person: '10-minute' or '97-second'.

    The ceiling is stored in seconds and the owner may set any number. Dividing
    by sixty printed '0.116667-minute' for a 7 second ceiling. Whole minutes are
    said as minutes, everything else as seconds, so no fraction is ever shown.
    """
    whole = int(round(seconds))
    if whole >= 60 and whole % 60 == 0:
        return f"{whole // 60}-minute"
    return f"{whole}-second"


def resolve_profile(cur, code: str, *, agent_slug: str | None = None,
                    period: str | None = None) -> dict:
    """Return the effective row: agent override, node default, or node total.

    Each period is a separate row. An omitted period is only legal when the
    code has one period (turn_seconds); callers of wakes and money pass it.
    """
    cur.execute(
        """SELECT code, scope_type, scope_id, period, value, action,
                  warn_at_pct, notify
             FROM sn_config.limit_profiles
            WHERE code = %s AND (%s::text IS NULL OR period = %s)
              AND (scope_type = 'default' AND scope_id = '*'
                   OR scope_type = 'node' AND scope_id = '*'
                   OR scope_type = 'agent' AND scope_id = %s)
            ORDER BY CASE scope_type WHEN 'agent' THEN 0
                                     WHEN 'default' THEN 1 ELSE 2 END""",
        (code, period, period, agent_slug),
    )
    return resolve_from_rows(cur.fetchall(), code, agent_slug=agent_slug, period=period)


def resolve_from_rows(rows, code: str, *, agent_slug: str | None = None,
                      period: str | None = None,
                      metadata_key: str | None = None) -> dict:
    """Use the same precedence when a caller already fetched several profiles."""
    eligible = [dict(r) for r in rows if r["code"] == code
                and (period is None or r["period"] == period)
                and ((r["scope_type"] == "agent" and r["scope_id"] == agent_slug)
                     or (r["scope_type"] == "metadata" and r["scope_id"] == metadata_key)
                     or (r["scope_type"] in ("default", "node") and r["scope_id"] == "*"))]
    rank = {"agent": 0, "metadata": 0, "default": 1, "node": 2}
    eligible.sort(key=lambda r: rank.get(r["scope_type"], 9))
    allowed = {"node"} if code == "node_spend_usd" else (
        {"metadata"} if metadata_key else
        {"agent", "default"} if agent_slug else {"default"})
    for row in eligible:
        if row["scope_type"] in allowed:
            return row
    raise MissingLimit(f"no profile for {code}/{period or '*'}")


def check_limit(cur, code: str, *, used: float | Decimal,
                requested_amount: float | Decimal = 0,
                agent_slug: str | None = None, period: str | None = None,
                profile: dict | None = None) -> dict:
    """Evaluate one measured value. Persistence and side effects are explicit."""
    profile = profile or resolve_profile(cur, code, agent_slug=agent_slug, period=period)
    amount = Decimal(str(used)) + Decimal(str(requested_amount))
    ceiling = Decimal(str(profile["value"]))
    if ceiling <= 0:
        raise ValueError("limit value must be positive")
    crossed = amount >= ceiling
    warned = amount >= ceiling * Decimal(profile["warn_at_pct"]) / 100
    action = profile["action"] if crossed else ("warn" if warned else "allow")
    return {"allowed": action in ("allow", "warn"), "action": action,
            "code": code, "used": float(amount), "limit": float(ceiling),
            "period": profile["period"], "profile": profile}


def period_start(period: str, when: datetime | None = None) -> datetime:
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = when.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "hour":
        return when.replace(minute=0, second=0, microsecond=0)
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "turn":
        return when
    raise ValueError(f"unknown limit period {period}")


def period_end(start: datetime, period: str, limit: float | None = None) -> datetime:
    if period == "hour": return start + timedelta(hours=1)
    if period == "day": return start + timedelta(days=1)
    if period == "week": return start + timedelta(days=7)
    if period == "month":
        return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    if period == "turn": return start + timedelta(seconds=limit or 0)
    raise ValueError(f"unknown limit period {period}")


def record_event(cur, decision: dict, *, actor: str, agent_slug: str | None,
                 emit, context: dict | None = None, when: datetime | None = None) -> bool:
    """Claim one warning/trip per period and write the shared journal event.

    Returns True only for a new claim, so a caller can notify once. The event
    and claim live in the caller's transaction. A turn has its own unique start.
    """
    if decision["action"] == "allow":
        return False
    kind = "trip" if decision["action"] not in ("allow", "warn") else "warn"
    profile = decision["profile"]
    start = period_start(profile["period"], when)
    end = period_end(start, profile["period"], decision["limit"])
    decision["period_start"] = start.isoformat()
    counter_scope = "agent" if agent_slug else profile["scope_type"]
    counter_id = agent_slug or profile["scope_id"]
    decision["notice_scope_type"] = counter_scope
    decision["notice_scope_id"] = counter_id
    cur.execute(
        """INSERT INTO sn_core.limit_notices
                  (code, scope_type, scope_id, period_start, kind)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT DO NOTHING RETURNING code""",
        (decision["code"], counter_scope, counter_id, start, kind),
    )
    if cur.fetchone() is None:
        return False
    emit(cur, actor=actor, event_type="limit_tripped",
         entity_type="agent" if agent_slug else "node", entity_id=agent_slug or "node",
         payload={"code": decision["code"], "actor": actor,
                  "reason": f"{decision['code']} limit reached",
                  "scope": counter_scope, "scope_id": counter_id,
                  "profile_scope": profile["scope_type"],
                  "value": decision["used"], "limit": decision["limit"],
                  "action": decision["action"], "period": profile["period"],
                  "period_start": start.isoformat(),
                  "period_end": end.isoformat(),
                  **(context or {})})
    return True


def notify_owner(cur, decision: dict, *, queue_message, text: str,
                 source: str = "limit-supervisor") -> dict | None:
    """Use the node's owner transport; report failure to the caller for retry."""
    if not decision["profile"]["notify"]:
        return None
    return queue_message(cur, subject=f"Smart Node limit: {decision['code']}",
                         text=text, source=source,
                         extra={"code": decision["code"],
                                "limit": decision["limit"],
                                "value": decision["used"]})
