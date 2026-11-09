"""Budget management endpoints (sn_config.budgets).

Provides write access to budget rows so the owner can update limits without
touching the database directly. Every write emits a budget event so there is an
audit trail of who changed what and when.

  GET    /api/budgets              list all budgets (cross-tenant) or own (tenant)
  POST   /api/budgets              create a new budget row for a scope
  PATCH  /api/budgets/{id}         update limit_usd, period, hard_limit, enabled

No DELETE: budgets are disabled (enabled=false) rather than removed, because the
history of past spend still makes sense against the row.
"""

import email.utils
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from psycopg2.extras import Json

from auth import Principal, get_principal
from db import cursor
from events_emit import emit
from permissions import requires

log = logging.getLogger("core-api.budgets")

router = APIRouter(prefix="/api/budgets", tags=["budgets"])

SCOPE_TYPES = ("tenant", "agent", "session_metadata_key")
PERIODS = ("daily", "weekly", "monthly")

SELECT_COLS = (
    "id, scope_type, scope_id, period, limit_usd, current_spend_usd, "
    "period_reset_at, hard_limit, enabled"
)

# A notification is an outbound email, so its sender must be a mailbox the
# communication companion can actually authenticate as.  communication.sh
# publishes this value only after it has matched the address to installed
# credentials.  There is deliberately no invented fallback.
_EMAIL_SENDER = os.environ.get("SN_NOTIFY_SENDER") or None
_NODE_DOMAIN = os.environ.get("SMART_NODE_DOMAIN") or "smart-node.local"


def _row_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "scope_type": r["scope_type"],
        "scope_id": r["scope_id"],
        "period": r["period"],
        "limit_usd": float(r["limit_usd"]),
        "current_spend_usd": float(r["current_spend_usd"]),
        "period_reset_at": r["period_reset_at"],
        "hard_limit": r["hard_limit"],
        "enabled": r["enabled"],
    }


def _profile_key(cur, row):
    if row["scope_type"] == "tenant":
        return "node_spend_usd", "node", "*"
    if row["scope_type"] == "session_metadata_key":
        return "spend_usd", "metadata", row["scope_id"]
    cur.execute("SELECT slug FROM sn_config.agents WHERE id::text=%s", (row["scope_id"],))
    agent = cur.fetchone()
    if not agent:
        raise HTTPException(409, "budget refers to an unknown agent")
    return "spend_usd", "agent", agent["slug"]


def _sync_profile(cur, row, actor, *, old_period=None):
    """Legacy budget writes update the registry in the same transaction."""
    code, scope, scope_id = _profile_key(cur, row)
    period = {"daily": "day", "weekly": "week", "monthly": "month"}[row["period"]]
    if old_period and old_period != row["period"]:
        cur.execute("DELETE FROM sn_config.limit_profiles WHERE code=%s AND scope_type=%s "
                    "AND scope_id=%s AND period=%s",
                    (code, scope, scope_id,
                     {"daily": "day", "weekly": "week", "monthly": "month"}[old_period]))
    if row["enabled"]:
        cur.execute(
            """INSERT INTO sn_config.limit_profiles
                     (code,scope_type,scope_id,period,value,action,warn_at_pct,notify)
               VALUES (%s,%s,%s,%s,%s,%s,90,true)
               ON CONFLICT (code,scope_type,scope_id,period) DO UPDATE
                 SET value=EXCLUDED.value, action=EXCLUDED.action, updated_at=now()""",
            (code, scope, scope_id, period, row["limit_usd"],
             "pause" if row["hard_limit"] else "warn"),
        )
    else:
        cur.execute("DELETE FROM sn_config.limit_profiles WHERE code=%s AND scope_type=%s "
                    "AND scope_id=%s AND period=%s", (code, scope, scope_id, period))
    emit(cur, actor=f"user:{actor}", event_type="profile_changed",
         entity_type="limit", entity_id=code,
         payload={"scope": scope, "scope_id": scope_id, "period": period,
                  "value": float(row["limit_usd"]) if row["enabled"] else None})


class BudgetNotificationRequest(BaseModel):
    channel: str

    @field_validator("channel")
    @classmethod
    def _channel(cls, value):
        value = (value or "").strip().lower()
        if value != "email":
            raise ValueError("budget exhaustion notifications require channel=email")
        return value


def _budget_scope_text(cur, row) -> str:
    """Human-readable scope without pretending an id is an agent name."""
    if row["scope_type"] == "tenant":
        return f"node (tenant {row['scope_id']})"
    if row["scope_type"] == "agent":
        cur.execute(
            "SELECT slug, title FROM sn_config.agents WHERE id::text = %s",
            (row["scope_id"],),
        )
        agent = cur.fetchone()
        if agent:
            label = agent["title"] or agent["slug"]
            return f"agent {label} ({agent['slug']})"
        return f"agent id {row['scope_id']}"
    if row["scope_type"] == "session_metadata_key":
        return f"sessions with metadata key {row['scope_id']}"
    return f"{row['scope_type']} {row['scope_id']}"


@router.post("/{budget_id}/notify-exhaustion", response_model=dict,
             dependencies=[Depends(requires("budgets.notify_exhaustion"))])
def notify_budget_exhaustion(
    budget_id: int,
    body: BudgetNotificationRequest,
    principal: Principal = Depends(get_principal),
):
    """Atomically queue one owner email and mark one exhausted budget period.

    The budget row is locked until the outbound row and notified_at update are
    committed together.  A missing sender/channel or a failed INSERT therefore
    leaves notified_at NULL, and a concurrent 402 sees either the same lock or
    the committed marker rather than enqueueing a duplicate.
    """
    if principal.role != "super-admin":
        raise HTTPException(403, "only the node owner may originate budget alerts")
    if not _EMAIL_SENDER:
        raise HTTPException(
            503,
            "SN_NOTIFY_SENDER is unset: refusing to mark the budget notified "
            "without a real configured mailbox",
        )

    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_core, sn_config, public")
        cur.execute(
            """SELECT id, scope_type, scope_id, period, limit_usd,
                      current_spend_usd, period_reset_at, hard_limit, enabled,
                      notified_at
                 FROM sn_config.budgets
                WHERE id = %s
                FOR UPDATE""",
            (budget_id,),
        )
        budget = cur.fetchone()
        if budget is None:
            raise HTTPException(404, f"budget {budget_id} not found")
        cur.execute("SELECT limit_usd, hard_limit FROM sn_config.budget_status WHERE id=%s",
                    (budget_id,))
        policy = cur.fetchone()
        if policy is None:
            raise HTTPException(409, "budget has no active registry profile")
        budget = dict(budget)
        budget.update(policy)
        if budget["notified_at"] is not None:
            return {"status": "already_notified", "budget_id": budget_id}
        if (not budget["enabled"] or not budget["hard_limit"]
                or budget["current_spend_usd"] < budget["limit_usd"]):
            raise HTTPException(409, f"budget {budget_id} is not an exhausted hard limit")

        # The recipient is the node owner, not the actor's guessed address.
        # user_channels is the source of truth and permits the owner to change
        # mail without changing this system path.
        cur.execute(
            """SELECT u.id, u.tenant_id, u.name, uc.address
                 FROM sn_config.users u
                 JOIN sn_config.user_channels uc ON uc.user_id = u.id
                WHERE u.role = 'super-admin'
                  AND u.active = true
                  AND u.archived_at IS NULL
                  AND uc.channel_slug = 'email'
                  AND uc.enabled = true
                ORDER BY u.id
                LIMIT 1"""
        )
        owner = cur.fetchone()
        if owner is None or not owner["address"]:
            raise HTTPException(
                503,
                "node owner has no enabled email channel; budget remains unnotified",
            )

        scope = _budget_scope_text(cur, budget)
        reset_at = budget["period_reset_at"]
        reset_text = reset_at.isoformat() if reset_at else "not configured"
        text = (
            "Smart Node stopped agent turns because a budget limit was reached.\n\n"
            f"Scope: {scope}\n"
            f"Period: {budget['period']}\n"
            f"Limit: ${budget['limit_usd']:.2f}\n"
            f"Spend: ${budget['current_spend_usd']:.2f}\n"
            f"Resets: {reset_text}\n\n"
            "Raise the limit in the node panel under Limits to "
            "resume sooner. Otherwise turns resume after the reset time."
        )
        external_id = email.utils.make_msgid(domain=_NODE_DOMAIN)
        metadata = Json({
            "subject": f"Smart Node budget limit reached: {scope}",
            "source": "budget-exhaustion",
            "from_name": "Smart Node",
            "budget_id": budget_id,
        })

        # Order is load-bearing: queue first, mark second, one transaction.
        # Any exception rolls both back.  The FOR UPDATE above serialises two
        # simultaneous gateway 402s, preserving one message per row/period.
        cur.execute(
            """INSERT INTO sn_core.messages
                      (channel_type, channel_id, external_id, direction, sender,
                       recipient, content, content_type, metadata, tenant_id)
               VALUES ('email', %s, %s, 'outbound', %s, %s, %s,
                       'text/plain', %s, %s)
            RETURNING id""",
            (_EMAIL_SENDER, external_id, _EMAIL_SENDER, owner["address"],
             text, metadata, owner["tenant_id"]),
        )
        message_id = cur.fetchone()["id"]
        cur.execute(
            """UPDATE sn_config.budgets
                  SET notified_at = now()
                WHERE id = %s
                  AND notified_at IS NULL
            RETURNING notified_at""",
            (budget_id,),
        )
        marked = cur.fetchone()
        if marked is None:
            # Defensive: the row lock should make this impossible.  Raising
            # rolls the queued message back instead of losing the invariant.
            raise HTTPException(409, "budget notification was claimed concurrently")
        emit(
            cur,
            actor="component:core-api",
            event_type="budget.exhaustion_notification_queued",
            entity_type="budget",
            entity_id=str(budget_id),
            payload={"message_id": message_id, "channel": body.channel,
                     "scope_type": budget["scope_type"],
                     "scope_id": budget["scope_id"]},
        )

    return {
        "status": "queued",
        "budget_id": budget_id,
        "message_id": message_id,
        "external_id": external_id,
        "channel": body.channel,
    }


@router.get("/check-warnings",
            dependencies=[Depends(requires("budgets.read"))])
def check_spend_warnings():
    """check and claim pre-warning thresholds for hard-limit budgets.

    Called by the Runner after each successful agent wake. Atomically claims
    warned_at for any hard-limit budget where:
      - current_spend_usd / limit_usd >= warn_at_pct / 100  (threshold crossed)
      - warned_at IS NULL  (not yet notified this period)
      - notified_at IS NULL  (period not yet exhausted -- exhaustion wins)
      - enabled = true

    Returns {warnings: [{id, period, limit_usd, current_spend_usd,
                          warn_at_pct, reset_at}]}.

    No principal auth: called by the Runner from the internal network.
    First caller wins (atomic CAS on warned_at). Safe to call on every successful
    wake; the CAS prevents duplicate sends even if called concurrently.
    """
    with cursor(commit=True) as cur:
        cur.execute("SET LOCAL search_path TO sn_config, sn_core, public")
        cur.execute(
            """UPDATE sn_config.budgets b
                  SET warned_at = now()
                 FROM sn_config.budget_status s
                WHERE s.id=b.id AND s.hard_limit=true
                  AND s.limit_usd > 0
                  AND s.current_spend_usd / s.limit_usd >= s.warn_at_pct / 100.0
                  AND b.warned_at IS NULL AND b.notified_at IS NULL
                RETURNING b.id, b.period, s.limit_usd, b.current_spend_usd,
                          s.warn_at_pct, b.period_reset_at"""
        )
        rows = cur.fetchall() or []
    warnings = []
    for r in rows:
        reset_ts = r["period_reset_at"]
        warnings.append({
            "id": r["id"],
            "period": r["period"],
            "limit_usd": float(r["limit_usd"]),
            "current_spend_usd": float(r["current_spend_usd"]),
            "warn_at_pct": int(r["warn_at_pct"]),
            "reset_at": reset_ts.isoformat() if reset_ts else None,
        })
    return {"warnings": warnings}


@router.get("", dependencies=[Depends(requires("budgets.read"))])
def list_budgets(principal: Principal = Depends(get_principal)):
    """List budget rows. Super-admin sees all; tenant principal sees own tenant row only."""
    with cursor() as cur:
        if principal.cross_tenant:
            cur.execute(
                f"SELECT {SELECT_COLS} FROM sn_config.budget_status ORDER BY scope_type, scope_id, period"
            )
        else:
            cur.execute(
                f"SELECT {SELECT_COLS} FROM sn_config.budget_status "
                "WHERE scope_type = 'tenant' AND scope_id = %s "
                "ORDER BY period",
                (str(principal.tenant_id),),
            )
        rows = cur.fetchall() or []
    return {"budgets": [_row_to_dict(r) for r in rows]}


class BudgetCreate(BaseModel):
    scope_type: str
    scope_id: str = Field(min_length=1, max_length=200)
    period: str
    limit_usd: float = Field(gt=0)
    hard_limit: bool = False
    enabled: bool = True

    @field_validator("scope_type")
    @classmethod
    def _scope(cls, v):
        if v not in SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {SCOPE_TYPES}")
        return v

    @field_validator("period")
    @classmethod
    def _period(cls, v):
        if v not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}")
        return v


@router.post("", status_code=201,
             dependencies=[Depends(requires("budgets.create"))])
def create_budget(payload: BudgetCreate, principal: Principal = Depends(get_principal)):
    """Create a new budget row for a scope/period combination."""
    if principal.role != "super-admin":
        raise HTTPException(403, "only the node owner may change limits")
    with cursor(commit=True) as cur:
        # Check for existing row at this scope+period (unique key)
        cur.execute(
            "SELECT id FROM sn_config.budgets "
            "WHERE scope_type = %s AND scope_id = %s AND period = %s",
            (payload.scope_type, payload.scope_id, payload.period),
        )
        if cur.fetchone():
            raise HTTPException(
                409,
                f"budget already exists for {payload.scope_type}/{payload.scope_id}/{payload.period}",
            )
        # period_reset_at: start of next period from now
        if payload.period == "daily":
            reset_sql = "date_trunc('day', now()) + interval '1 day'"
        elif payload.period == "weekly":
            reset_sql = "date_trunc('week', now()) + interval '1 week'"
        else:  # monthly
            reset_sql = "date_trunc('month', now()) + interval '1 month'"

        cur.execute(
            f"""
            INSERT INTO sn_config.budgets
              (scope_type, scope_id, period, limit_usd, hard_limit, enabled, period_reset_at)
            VALUES (%s, %s, %s, %s, %s, %s, {reset_sql})
            RETURNING {SELECT_COLS}
            """,
            (
                payload.scope_type, payload.scope_id, payload.period,
                payload.limit_usd, payload.hard_limit, payload.enabled,
            ),
        )
        row = cur.fetchone()
        _sync_profile(cur, row, principal.username or "panel")
        emit(
            cur,
            actor=f"user:{principal.username or 'panel'}",
            event_type="budget.created",
            entity_type="budget",
            entity_id=f"{payload.scope_type}/{payload.scope_id}/{payload.period}",
            payload={
                "limit_usd": payload.limit_usd,
                "hard_limit": payload.hard_limit,
                "period": payload.period,
            },
        )
    return _row_to_dict(row)


class BudgetUpdate(BaseModel):
    limit_usd: float | None = Field(default=None, gt=0)
    period: str | None = None
    hard_limit: bool | None = None
    enabled: bool | None = None

    @field_validator("period")
    @classmethod
    def _period(cls, v):
        if v is not None and v not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}")
        return v


@router.patch("/{budget_id}",
              dependencies=[Depends(requires("budgets.update"))])
def update_budget(
    budget_id: int,
    payload: BudgetUpdate,
    principal: Principal = Depends(get_principal),
):
    """Update limit_usd, period, hard_limit, or enabled on an existing budget row.

    Emits a budget.updated event so every change has an author and a timestamp.
    Only the node owner may change a budget policy.
    """
    sets, params = [], []
    for col, val in (
        ("limit_usd", payload.limit_usd),
        ("period", payload.period),
        ("hard_limit", payload.hard_limit),
        ("enabled", payload.enabled),
    ):
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val)
    if not sets:
        raise HTTPException(400, "no fields to update")

    params.append(budget_id)

    with cursor(commit=True) as cur:
        # Fetch the row first so we can enforce tenant scoping and record the change.
        cur.execute(
            f"SELECT {SELECT_COLS} FROM sn_config.budgets WHERE id = %s",
            (budget_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            raise HTTPException(404, f"budget id={budget_id} not found")

        if principal.role != "super-admin":
            raise HTTPException(403, "only the node owner may change limits")

        cur.execute(
            f"UPDATE sn_config.budgets SET {', '.join(sets)} WHERE id = %s "
            f"RETURNING {SELECT_COLS}",
            tuple(params),
        )
        row = cur.fetchone()
        _sync_profile(cur, row, principal.username or "panel", old_period=existing["period"])

        changed = {
            col: getattr(payload, col)
            for col in ("limit_usd", "period", "hard_limit", "enabled")
            if getattr(payload, col) is not None
        }
        emit(
            cur,
            actor=f"user:{principal.username or 'panel'}",
            event_type="budget.updated",
            entity_type="budget",
            entity_id=f"{existing['scope_type']}/{existing['scope_id']}/{existing['period']}",
            payload={"id": budget_id, "changed": changed},
        )
    return _row_to_dict(row)
