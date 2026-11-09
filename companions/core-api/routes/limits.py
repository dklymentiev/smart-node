"""Owner controls for the durable supervisor limit registry."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import Principal
from db import cursor
from events_emit import emit
from lib.supervisor import resolve_profile, MissingLimit, turn_limit_phrase
from lib import owner_alert
from datetime import datetime
from permissions import requires

router = APIRouter(prefix="/api/limits", tags=["limits"])


def _owner(principal: Principal | None) -> None:
    # None is a node service admitted by its origin: it is not the owner either.
    if principal is None or principal.role != "super-admin":
        raise HTTPException(403, "only the node owner may change limits")


def _row(row) -> dict:
    out = dict(row)
    if "value" in out:
        out["value"] = float(out["value"])
    return out


class ProfileInput(BaseModel):
    value: float = Field(gt=0)
    action: str
    warn_at_pct: int = Field(ge=1, le=100)
    notify: bool


@router.get("/turn-bound", dependencies=[Depends(requires("limits.read"))])
def turn_bound():
    """Upper bound for outer HTTP waits; the gateway enforces each agent's row."""
    with cursor() as cur:
        cur.execute("SELECT max(value) AS seconds FROM sn_config.limit_profiles "
                    "WHERE code='turn_seconds' AND period='turn'")
        row = cur.fetchone()
        if not row or row["seconds"] is None:
            raise HTTPException(503, "turn limit registry is empty")
        return {"seconds": float(row["seconds"])}


def _check_profile(code: str, period: str, body: ProfileInput) -> None:
    if body.action == "ask_owner":
        raise HTTPException(422, "ask_owner is unavailable until HITL is implemented")
    if body.action not in ("refuse", "pause", "warn"):
        raise HTTPException(422, "unknown limit action")
    if code == "turn_seconds" and body.action not in ("pause", "refuse"):
        raise HTTPException(422, "turn length accepts only pause or refuse")
    if code == "turn_seconds" and period != "turn":
        raise HTTPException(422, "turn length uses period=turn")
    if code == "wakes_unattended" and period not in ("hour", "day"):
        raise HTTPException(422, "wake cap uses hour or day")
    if code in ("spend_usd", "node_spend_usd") and period not in ("day", "week", "month"):
        raise HTTPException(422, "spending uses day, week or month")


def _usage(cur, code: str, period: str, slug: str | None) -> float:
    if code == "wakes_unattended" and slug:
        window = {"hour": "1 hour", "day": "1 day"}[period]
        cur.execute(
            "SELECT count(*) AS n FROM sn_core.events WHERE event_type='agent.woken' "
            "AND entity_id=%s AND ts >= now() - %s::interval", (slug, window))
        return float(cur.fetchone()["n"])
    if code in ("spend_usd", "node_spend_usd"):
        if code == "spend_usd" and slug:
            cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s", (slug,))
            agent = cur.fetchone()
            if not agent:
                raise HTTPException(404, "agent not found")
            scope, scope_id = "agent", str(agent["id"])
        else:
            scope, scope_id = "tenant", "1"
        legacy_period = {"day": "daily", "week": "weekly", "month": "monthly"}[period]
        cur.execute("SELECT current_spend_usd FROM sn_config.budgets "
                    "WHERE scope_type=%s AND scope_id=%s AND period=%s AND enabled",
                    (scope, scope_id, legacy_period))
        row = cur.fetchone()
        return float(row["current_spend_usd"]) if row else 0.0
    if code == "turn_seconds" and slug:
        # A turn is its own period. Show the latest observed duration, not a
        # fabricated daily cumulative number.
        cur.execute(
            "SELECT (payload->>'held_seconds')::numeric AS seconds "
            "FROM sn_core.events WHERE event_type='agent.turn_settled' "
            "AND entity_id=%s AND ts >= date_trunc('day', now()) "
            "ORDER BY ts DESC LIMIT 1", (slug,))
        row = cur.fetchone()
        return float(row["seconds"]) if row and row["seconds"] is not None else 0.0
    return 0.0


@router.get("")
def list_limits(principal: Principal | None = Depends(requires("limits.read"))):
    _owner(principal)
    with cursor() as cur:
        cur.execute("SELECT node_paused, changed_at, changed_by FROM sn_config.node_control WHERE id=true")
        control = cur.fetchone()
        cur.execute("SELECT code, label, unit, control_point, description "
                    "FROM sn_config.limit_defs ORDER BY code")
        defs = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT code, scope_type, scope_id, period, value, action, "
                    "warn_at_pct, notify FROM sn_config.limit_profiles ORDER BY code, period, scope_type")
        profiles = [_row(r) for r in cur.fetchall()]
        # Keep the editor visible when a node has never had an agent spending
        # default. This is only a display row; saving it creates the profile.
        if not any(p["code"] == "spend_usd" and p["scope_type"] == "default"
                   and p["period"] == "month" for p in profiles):
            profiles.append({"code": "spend_usd", "scope_type": "default",
                             "scope_id": "*", "period": "month", "value": None,
                             "action": "pause", "warn_at_pct": 80,
                             "notify": True, "missing": True})
        for p in profiles:
            if p["scope_type"] in ("node", "default"):
                p["usage"] = _usage(cur, p["code"], p["period"], None)
        return {"node_paused": bool(control and control["node_paused"]),
                "pause_changed_at": control["changed_at"] if control else None,
                "definitions": defs, "profiles": profiles}


@router.get("/agents/{slug}")
def agent_limits(slug: str,
                 principal: Principal | None = Depends(requires("limits.read"))):
    _owner(principal)
    with cursor() as cur:
        cur.execute("SELECT slug FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        if cur.fetchone() is None:
            raise HTTPException(404, "agent not found")
        cur.execute("SELECT DISTINCT code, period FROM sn_config.limit_profiles "
                    "WHERE scope_type='default' OR (scope_type='agent' AND scope_id=%s) "
                    "ORDER BY code, period", (slug,))
        pairs = {(r["code"], r["period"]) for r in cur.fetchall()}
        pairs.add(("spend_usd", "month"))
        rows = []
        for code, period in sorted(pairs):
            try:
                default = resolve_profile(cur, code, period=period)
            except MissingLimit:
                default = None
            try:
                effective = resolve_profile(cur, code, agent_slug=slug, period=period)
            except MissingLimit:
                effective = None
            rows.append({"code": code, "period": period,
                         "default": _row(default) if default else None,
                         "effective": _row(effective) if effective else None,
                         "override": _row(effective) if effective and effective["scope_type"] == "agent" else None,
                         "usage": _usage(cur, code, period, slug)})
        return {"agent": slug, "limits": rows}


def _save(cur, code, scope, scope_id, period, body, actor):
    _check_profile(code, period, body)
    cur.execute("SELECT 1 FROM sn_config.limit_defs WHERE code=%s", (code,))
    if cur.fetchone() is None:
        raise HTTPException(404, "limit code not found")
    cur.execute(
        """INSERT INTO sn_config.limit_profiles
             (code, scope_type, scope_id, period, value, action, warn_at_pct, notify)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (code, scope_type, scope_id, period) DO UPDATE
              SET value=EXCLUDED.value, action=EXCLUDED.action,
                  warn_at_pct=EXCLUDED.warn_at_pct, notify=EXCLUDED.notify,
                  updated_at=now()
           RETURNING code, scope_type, scope_id, period, value, action, warn_at_pct, notify""",
        (code, scope, scope_id, period, body.value, body.action, body.warn_at_pct, body.notify))
    row = _row(cur.fetchone())
    emit(cur, actor=f"user:{actor}", event_type="profile_changed",
         entity_type="limit", entity_id=code,
         payload={"scope": scope, "scope_id": scope_id, "period": period,
                  "value": body.value, "action": body.action})
    return row


@router.put("/defaults/{code}/{period}")
def set_default(code: str, period: str, body: ProfileInput,
                principal: Principal = Depends(requires("limits.write"))):
    _owner(principal)
    with cursor(commit=True) as cur:
        return _save(cur, code, "default" if code != "node_spend_usd" else "node",
                     "*", period, body, principal.username)


@router.put("/agents/{slug}/{code}/{period}")
def set_override(slug: str, code: str, period: str, body: ProfileInput,
                 principal: Principal = Depends(requires("limits.write"))):
    _owner(principal)
    if code == "node_spend_usd":
        raise HTTPException(422, "node total cannot have an agent override")
    with cursor(commit=True) as cur:
        cur.execute("SELECT 1 FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (slug,))
        if cur.fetchone() is None:
            raise HTTPException(404, "agent not found")
        return _save(cur, code, "agent", slug, period, body, principal.username)


@router.delete("/agents/{slug}/{code}/{period}")
def clear_override(slug: str, code: str, period: str,
                   principal: Principal = Depends(requires("limits.write"))):
    _owner(principal)
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_config.limit_profiles WHERE code=%s AND period=%s "
                    "AND scope_type='agent' AND scope_id=%s RETURNING code", (code, period, slug))
        if cur.fetchone() is None:
            raise HTTPException(404, "override not found")
        emit(cur, actor=f"user:{principal.username}", event_type="profile_changed",
             entity_type="limit", entity_id=code,
             payload={"scope": "agent", "scope_id": slug, "period": period,
                      "override": None})
        try:
            return {"effective": _row(resolve_profile(cur, code, agent_slug=slug, period=period))}
        except MissingLimit:
            return {"effective": None}


class PauseInput(BaseModel):
    paused: bool


@router.put("/pause")
def set_pause(body: PauseInput, principal: Principal = Depends(requires("limits.write"))):
    _owner(principal)
    with cursor(commit=True) as cur:
        cur.execute("UPDATE sn_config.node_control SET node_paused=%s, "
                    "changed_at=now(), changed_by=%s WHERE id=true "
                    "AND node_paused IS DISTINCT FROM %s RETURNING node_paused",
                    (body.paused, principal.username, body.paused))
        if cur.fetchone():
            emit(cur, actor=f"user:{principal.username}",
                 event_type="node_paused" if body.paused else "node_unpaused",
                 entity_type="node", entity_id="node", payload={"paused": body.paused})
        return {"node_paused": body.paused}


class TurnCutoffNotice(BaseModel):
    agent: str | None = None
    scope_type: str
    scope_id: str
    seconds: float = Field(gt=0)
    period_start: datetime


@router.post("/notify-turn-cutoff")
def notify_turn_cutoff(body: TurnCutoffNotice,
                       principal: Principal = Depends(requires("limits.write"))):
    """Queue one owner alert after the gateway journals a cut turn.

    The gateway is already configured with the owner service identity for
    budget notifications. A failed send leaves notified_at NULL for retry.
    """
    _owner(principal)
    scope = body.scope_type
    scope_id = body.scope_id
    if scope not in ("agent", "default") or (scope == "default" and scope_id != "*"):
        raise HTTPException(422, "invalid turn cutoff scope")
    with cursor(commit=True) as cur:
        cur.execute(
            "SELECT notified_at FROM sn_core.limit_notices "
            "WHERE code='turn_seconds' AND scope_type=%s AND scope_id=%s "
            "AND period_start=%s AND kind='trip' FOR UPDATE",
            (scope, scope_id, body.period_start),
        )
        notice = cur.fetchone()
        if notice is None:
            raise HTTPException(404, "turn cutoff event not found")
        if notice["notified_at"] is not None:
            return {"status": "already_notified"}
        text = (f"The turn for {body.agent or 'the node'} was stopped by the "
                f"{turn_limit_phrase(body.seconds)} limit. The reason and limit are in the event log.")
        message = owner_alert.queue_owner_email(
            cur, subject=f"Smart Node: turn limit for {body.agent or 'node'}",
            text=text, source="limit-supervisor",
            extra={"agent": body.agent, "limit_seconds": body.seconds},
        )
        cur.execute("UPDATE sn_core.limit_notices SET notified_at=now() "
                    "WHERE code='turn_seconds' AND scope_type=%s AND scope_id=%s "
                    "AND period_start=%s AND kind='trip'",
                    (scope, scope_id, body.period_start))
        return {"status": "queued", "message_id": message["message_id"]}
