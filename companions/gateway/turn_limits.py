"""Gateway adapter for the shared supervisor registry."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import db
from supervisor import resolve_profile, check_limit, record_event, turn_limit_phrase


def node_turn_bound_seconds() -> float:
    """The bound for legacy v1 CLI sessions, which have no agent identity."""
    rows = db.query("SELECT max(value) AS seconds FROM sn_config.limit_profiles "
                    "WHERE code='turn_seconds' AND period='turn'",
                    fetch=True, as_dict=True) or []
    if not rows or rows[0]["seconds"] is None:
        raise RuntimeError("turn limit registry is empty")
    return float(rows[0]["seconds"])


def agent_turn_limit_seconds(agent_slug: str | None) -> float:
    """The ceiling for a stateless chat call, which names an agent but has no
    session row. Same registry row a CLI turn of that agent is held to."""
    from psycopg2.extras import RealDictCursor
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            profile = resolve_profile(cur, "turn_seconds", agent_slug=agent_slug,
                                      period="turn")
            return float(profile["value"])


def turn_limit_seconds(session_id: str) -> float:
    """Read the effective ceiling at turn start; edits affect the next turn."""
    from psycopg2.extras import RealDictCursor
    with db.get_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT a.slug FROM sn_core.ai_sessions s "
                "LEFT JOIN sn_config.agents a ON a.id=s.agent_id WHERE s.id=%s",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown session {session_id}")
            profile = resolve_profile(cur, "turn_seconds", agent_slug=row["slug"],
                                      period="turn")
            return float(profile["value"])


def record_turn_cutoff(session_id: str, seconds: float) -> dict:
    """Atomically journal a cut turn; return the notice key for owner delivery."""
    from psycopg2.extras import RealDictCursor
    with db.with_transaction() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT a.slug FROM sn_core.ai_sessions s "
                "LEFT JOIN sn_config.agents a ON a.id=s.agent_id WHERE s.id=%s",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"unknown session {session_id}")
            agent = row["slug"]
            profile = resolve_profile(cur, "turn_seconds", agent_slug=agent, period="turn")
            # The cutoff belongs to the ceiling captured when this turn began.
            # An owner edit mid-turn applies to the next turn, so the journal
            # must preserve the value that actually stopped this one.
            profile = dict(profile)
            profile["value"] = seconds
            decision = check_limit(cur, "turn_seconds", used=seconds, profile=profile)
            # The configured warning threshold is advisory. The registered
            # ceiling itself is hard, including when the row says warn.
            decision["action"] = "pause" if profile["action"] == "pause" else "refuse"

            def emit(tx, **event):
                actor = event["actor"]
                tx.execute("INSERT INTO sn_core.actors (id, display_name, type, layer) "
                           "VALUES (%s, %s, 'component', 'system') ON CONFLICT DO NOTHING",
                           (actor, actor))
                import json
                tx.execute(
                    "INSERT INTO sn_core.events (actor, event_type, entity_type, entity_id, payload) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb)",
                    (actor, event["event_type"], event["entity_type"],
                     event["entity_id"], json.dumps(event["payload"])),
                )

            record_event(cur, decision, actor="component:gateway", agent_slug=agent,
                         emit=emit, context={"session_id": session_id},
                         when=datetime.now(timezone.utc) - timedelta(seconds=seconds))
            return {"agent": agent, "seconds": seconds, "notify": bool(profile["notify"]),
                    "scope_type": decision["notice_scope_type"],
                    "scope_id": decision["notice_scope_id"],
                    "period_start": decision["period_start"]}
