"""In-process event emission helper.

Other route modules call emit() within their existing transaction so the
event lands atomically with the action it describes. Bash companions and
external scripts call POST /api/events instead (routes/events.py).

emit() takes a RealDictCursor (db.cursor() context). It runs a single
INSERT, returns the new row {id, ts}, and is safe to call multiple times
in the same transaction.
"""

import json
import logging
from typing import Any

log = logging.getLogger("core-api.events.emit")

# sn_core.events.actor is a FK to sn_core.actors(id). Rather than pre-seed every
# possible actor, we self-register the acting identity on emit so the journal
# is always FK-consistent. The layer is derived from the id prefix
# ("<layer>:<name>" convention); unknown/bare ids fall back to system.
_LAYER_BY_PREFIX = {
    "user": "human",
    "human": "human",
    "agent": "specialist",
    "specialist": "specialist",
    "orchestrator": "orchestrator",
    "guard": "guard",
    "component": "system",
    "system": "system",
    "cron": "system",
    "installer": "system",
}


def _ensure_actor(cur, actor: str) -> None:
    """Idempotently register the actor so events.actor FK always resolves."""
    prefix = actor.split(":", 1)[0] if ":" in actor else actor
    layer = _LAYER_BY_PREFIX.get(prefix, "system")
    cur.execute(
        "INSERT INTO sn_core.actors (id, display_name, type, layer) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (actor, actor, prefix, layer),
    )


def emit(
    cur,
    *,
    actor: str,
    event_type: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
    parent_event_id: int | None = None,
) -> dict:
    _ensure_actor(cur, actor)
    cur.execute(
        """
        INSERT INTO sn_core.events
            (actor, event_type, entity_type, entity_id, payload, parent_event_id)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id, ts
        """,
        (
            actor,
            event_type,
            entity_type,
            entity_id,
            json.dumps(payload or {}),
            parent_event_id,
        ),
    )
    row = cur.fetchone()
    log.debug(
        "emit: actor=%s type=%s entity=%s/%s id=%s",
        actor, event_type, entity_type, entity_id, row["id"],
    )
    return row
