"""Budget enforcement for spending limits, warnings and period resets.

Two responsibilities:

  1. check_budget(tenant_id, agent_id=None, metadata_key=None)
     Called BEFORE every backend.send(). Returns (allowed, reason, status).
     Sums all applicable budget rows (tenant + optional agent + optional metadata-key).
     Auto period-reset on first read after period_reset_at.
     HARD over-limit blocks immediately; SOFT logs a warning and emits an event.

  2. record_spend(tenant_id, agent_id=None, metadata_key=None, *, cost_usd)
     Called AFTER backend.send() with whatever the response usage reported.
     Increments current_spend_usd on all applicable budgets in a single UPDATE.
     Emits a budget.soft_limit_exceeded event if spend just crossed a soft limit.

Auto period-reset:
  When current row's period_reset_at <= now(), reset current_spend_usd=0
  and bump period_reset_at to the next boundary based on period (daily/weekly/monthly).
  This happens lazily on first check_budget call after the boundary.

Scope resolution:
  budget.scope_type='tenant'              -> always considered (if scope_id matches tenant)
  budget.scope_type='agent'               -> considered when session.agent_id matches scope_id
  budget.scope_type='session_metadata_key' -> considered when session.metadata has key=scope_id (any value)
"""
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Tuple

import db
from supervisor import resolve_from_rows, check_limit, record_event, MissingLimit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- helpers

def _next_period_reset(period: str, now: datetime) -> datetime:
    """Compute the next reset boundary for a given period string."""
    if period == "daily":
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return base + timedelta(days=1)
    if period == "weekly":
        # Reset on Monday 00:00
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_to_monday = (7 - base.weekday()) % 7
        if days_to_monday == 0:
            days_to_monday = 7
        return base + timedelta(days=days_to_monday)
    if period == "monthly":
        # First of next month
        if now.month == 12:
            return now.replace(year=now.year + 1, month=1, day=1,
                               hour=0, minute=0, second=0, microsecond=0)
        return now.replace(month=now.month + 1, day=1,
                           hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown period: {period!r}")


def _maybe_reset(row: Dict[str, Any]) -> Dict[str, Any]:
    """If the row's period has elapsed, atomically reset current_spend_usd and
    advance period_reset_at. Returns the (possibly updated) row dict."""
    reset_at = row.get("period_reset_at")
    if not reset_at:
        return row
    now = datetime.now(reset_at.tzinfo)
    if reset_at > now:
        return row

    new_reset = _next_period_reset(row["period"], now)
    # Atomic: only reset if not already reset by another process.
    # Also clear the budget notification flags so the new period starts fresh.
    updated = db.query(
        """UPDATE sn_config.budgets
              SET current_spend_usd = 0,
                  period_reset_at   = %s,
                  notified_at       = NULL,
                  warned_at         = NULL
            WHERE id = %s
              AND period_reset_at <= now()
            RETURNING current_spend_usd, period_reset_at""",
        (new_reset, row["id"]),
        fetch=True, as_dict=True,
    )
    if updated:
        row = dict(row)
        row["current_spend_usd"] = updated[0]["current_spend_usd"]
        row["period_reset_at"] = updated[0]["period_reset_at"]
        row["notified_at"] = None
        row["warned_at"] = None
        logger.info("budget id=%s period=%s reset -> next %s",
                    row["id"], row["period"], new_reset)
    return row


def _applicable_budgets(tenant_id: int,
                        agent_id: Optional[int],
                        metadata: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pull all enabled budget rows that apply to this session's scope mix.

    A budget applies if:
      scope_type='tenant'               and scope_id = str(tenant_id)
      scope_type='agent'                and scope_id = str(agent_id)        (agent_id given)
      scope_type='session_metadata_key' and scope_id in metadata.keys()     (metadata given)
    """
    agent_slug = None
    if agent_id is not None:
        agent_rows = db.query("SELECT slug FROM sn_config.agents WHERE id=%s",
                              (agent_id,), fetch=True, as_dict=True) or []
        agent_slug = agent_rows[0]["slug"] if agent_rows else None
    profiles = db.query(
        "SELECT code, scope_type, scope_id, period, value, action, "
        "warn_at_pct, notify FROM sn_config.limit_profiles "
        "WHERE code IN ('spend_usd','node_spend_usd')",
        fetch=True, as_dict=True,
    ) or []
    # The meter is created for every effective profile, including a period that
    # exists only as an agent override. A new agent inherits default rows on its
    # first turn. A new node daily limit gets a daily meter before checking.
    def ensure_meter(scope, scope_id, profile):
        period = {"day": "daily", "week": "weekly", "month": "monthly"}[profile["period"]]
        reset_sql = {"day": "date_trunc('day',now()) + interval '1 day'",
                     "week": "date_trunc('week',now()) + interval '1 week'",
                     "month": "date_trunc('month',now()) + interval '1 month'"}[profile["period"]]
        db.query(
            "INSERT INTO sn_config.budgets "
            "(scope_type,scope_id,period,limit_usd,period_reset_at,hard_limit,enabled) "
            f"VALUES (%s,%s,%s,%s,{reset_sql},%s,true) "
            "ON CONFLICT (scope_type,scope_id,period) DO NOTHING",
            (scope, scope_id, period, profile["value"], profile["action"] != "warn"),
        )

    for period in ("day", "week", "month"):
        try:
            ensure_meter("tenant", str(tenant_id),
                         resolve_from_rows(profiles, "node_spend_usd", period=period))
        except MissingLimit:
            pass
        if agent_slug:
            try:
                ensure_meter("agent", str(agent_id),
                             resolve_from_rows(profiles, "spend_usd",
                                               agent_slug=agent_slug, period=period))
            except MissingLimit:
                pass
    clauses = ["enabled = true"]
    args: List[Any] = []

    scope_or = ["(scope_type = 'tenant' AND scope_id = %s)"]
    args.append(str(tenant_id))

    if agent_id is not None:
        scope_or.append("(scope_type = 'agent' AND scope_id = %s)")
        args.append(str(agent_id))

    if metadata:
        keys = [k for k in metadata.keys() if k]
        if keys:
            placeholders = ", ".join(["%s"] * len(keys))
            scope_or.append(
                f"(scope_type = 'session_metadata_key' AND scope_id IN ({placeholders}))"
            )
            args.extend(keys)

    clauses.append("(" + " OR ".join(scope_or) + ")")
    sql = (
        "SELECT id, scope_type, scope_id, period, limit_usd, current_spend_usd, "
        "       period_reset_at, hard_limit, enabled, "
        "       notified_at, COALESCE(warn_at_pct, 90) AS warn_at_pct, warned_at "
        "  FROM sn_config.budgets "
        " WHERE " + " AND ".join(clauses) + " "
        " ORDER BY scope_type, scope_id, period"
    )
    rows = db.query(sql, tuple(args), fetch=True, as_dict=True) or []
    # Apply period reset lazily, then replace all policy fields from the one
    # registry. Legacy budget columns remain a usage-ledger compatibility shape.
    out = []
    for r in rows:
        r = _maybe_reset(r)
        period = {"daily": "day", "weekly": "week", "monthly": "month"}[r["period"]]
        try:
            if r["scope_type"] == "tenant":
                profile = resolve_from_rows(profiles, "node_spend_usd", period=period)
            elif r["scope_type"] == "agent":
                profile = resolve_from_rows(profiles, "spend_usd",
                                            agent_slug=agent_slug, period=period)
            else:
                profile = resolve_from_rows(profiles, "spend_usd",
                                            metadata_key=r["scope_id"], period=period)
        except MissingLimit:
            # A disabled or deleted profile removes the limit; its historical
            # meter stays in the ledger but cannot enforce a stale value.
            continue
        r = dict(r)
        r["limit_usd"] = profile["value"]
        r["hard_limit"] = profile["action"] != "warn"
        r["warn_at_pct"] = profile["warn_at_pct"]
        r["action"] = profile["action"]
        r["notify"] = profile["notify"]
        r["profile"] = profile
        r["agent_slug"] = agent_slug if r["scope_type"] == "agent" else None
        out.append(r)
    return out


# ---------------------------------------------------------------------- public

def check_budget(tenant_id: int,
                 agent_id: Optional[int] = None,
                 metadata: Optional[Dict[str, Any]] = None
                 ) -> Tuple[bool, Optional[str], List[Dict[str, Any]]]:
    """Pre-send budget check.

    Returns:
        allowed:  False if any HARD budget is already over its limit.
        reason:   human-readable string when allowed=False; None otherwise.
        status:   list of {scope_type, scope_id, period, limit, current, pct, hard, over}
                  for all applicable budgets -- caller can surface in headers/logs.
    """
    budgets = _applicable_budgets(tenant_id, agent_id, metadata)
    status: List[Dict[str, Any]] = []
    blocking_reasons: List[str] = []

    for b in budgets:
        limit = float(b["limit_usd"])
        current = float(b["current_spend_usd"])
        decision = check_limit(None, b["profile"]["code"], used=current,
                               profile=b["profile"])
        over = current >= limit
        hard = not decision["allowed"] if over else b["action"] != "warn"
        reset_at = b.get("period_reset_at")
        item: Dict[str, Any] = {
            "id": b["id"],
            "scope_type": b["scope_type"],
            "scope_id": b["scope_id"],
            "period": b["period"],
            "limit_usd": limit,
            "current_spend_usd": current,
            "pct": (current / limit * 100.0) if limit > 0 else 0.0,
            "hard": hard,
            "over": over,
        }
        # include reset_at in status so the 402 body can carry it.
        if over and hard and reset_at is not None:
            item["reset_at"] = reset_at.isoformat()
        status.append(item)
        if over and hard:
            blocking_reasons.append(
                f"{b['scope_type']}={b['scope_id']} {b['period']} budget exhausted "
                f"(${current:.4f} >= ${limit:.2f})"
            )

    if blocking_reasons:
        return False, "; ".join(blocking_reasons), status
    return True, None, status


def _record_budget_event(b: Dict[str, Any], after: float) -> None:
    """Claim a period event with the shared supervisor and journal it once."""
    try:
        from psycopg2.extras import RealDictCursor
        decision = check_limit(None, b["profile"]["code"], used=after,
                               profile=b["profile"])
        with db.with_transaction() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                def emit(tx, **event):
                    actor = event["actor"]
                    tx.execute("INSERT INTO sn_core.actors (id, display_name, type, layer) "
                               "VALUES (%s, %s, 'component', 'system') ON CONFLICT DO NOTHING",
                               (actor, actor))
                    tx.execute("INSERT INTO sn_core.events "
                               "(actor, event_type, entity_type, entity_id, payload) "
                               "VALUES (%s, %s, %s, %s, %s::jsonb)",
                               (actor, event["event_type"], event["entity_type"],
                                event["entity_id"], json.dumps(event["payload"])))

                record_event(cur, decision, actor="component:gateway",
                             agent_slug=b["agent_slug"], emit=emit,
                             context={"budget_id": b["id"]})
    except Exception as exc:
        logger.warning("budget limit event failed: %s", exc)


def record_spend(tenant_id: int,
                 cost_usd: float,
                 agent_id: Optional[int] = None,
                 metadata: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Post-send: increment current_spend_usd on all applicable budgets.

    No-op when cost_usd is 0 or negative.

    After the increment, checks each updated row: if spend just crossed a soft
    limit for the first time this call, emits a budget.soft_limit_exceeded event
    to sn_core.events. Also checks if spend crossed the hard-limit
    pre-warning threshold (warn_at_pct) and warned_at is still NULL, atomically
    claims warned_at and returns the budget info so the caller can notify the owner.

    Returns: list of budget dicts for which a pre-warning was newly claimed.
    Caller is responsible for sending the owner notification.
    """
    if cost_usd is None or cost_usd <= 0:
        return []

    budgets = _applicable_budgets(tenant_id, agent_id, metadata)
    if not budgets:
        return []

    # Capture pre-spend state so we can detect crossings.
    pre_spend = {b["id"]: float(b["current_spend_usd"]) for b in budgets}
    ids = [b["id"] for b in budgets]

    db.query(
        """UPDATE sn_config.budgets
              SET current_spend_usd = current_spend_usd + %s
            WHERE id = ANY(%s)""",
        (Decimal(str(cost_usd)), ids),
    )
    logger.info("budget.record_spend tenant=%s agent=%s cost=$%.6f rows=%s",
                tenant_id, agent_id, cost_usd, ids)

    warnings_to_send: List[Dict[str, Any]] = []

    # Emit a soft-limit alert if spend just crossed the limit on a non-hard budget.
    # Also check the configured pre-warning for hard-limit budgets.
    for b in budgets:
        limit = float(b["limit_usd"])
        if limit <= 0:
            continue
        before = pre_spend[b["id"]]
        after = before + cost_usd

        if before < limit <= after:
            _record_budget_event(b, after)

        if b["hard_limit"]:
            # pre-warning at configurable threshold.
            # Only warn if below the hard limit (above it is exhaustion, not a pre-warning).
            # Also do not pre-warn if the period has already been notified as exhausted.
            warn_pct = int(b.get("warn_at_pct") or 90)
            threshold = limit * warn_pct / 100.0
            if (before < threshold <= after
                    and b.get("warned_at") is None
                    and b.get("notified_at") is None):
                # Atomically claim the warning for this period.
                rows = db.query(
                    """UPDATE sn_config.budgets
                          SET warned_at = now()
                        WHERE id = %s
                          AND warned_at IS NULL
                          AND notified_at IS NULL
                        RETURNING id, period, limit_usd, current_spend_usd,
                                  period_reset_at, warn_at_pct""",
                    (b["id"],),
                    fetch=True, as_dict=True,
                )
                if rows:
                    r = rows[0]
                    reset_at = r["period_reset_at"]
                    warnings_to_send.append({
                        "id": r["id"],
                        "period": r["period"],
                        "limit_usd": limit,
                        "current_spend_usd": float(r["current_spend_usd"]),
                        "warn_at_pct": warn_pct,
                        "reset_at": reset_at.isoformat() if reset_at else None,
                    })
                    logger.info(
                        "budget pre-warning claimed id=%s period=%s spend=%.4f limit=%.2f",
                        b["id"], b["period"], after, limit,
                    )
            continue  # hard limits handled above; no soft-limit event

    return warnings_to_send
