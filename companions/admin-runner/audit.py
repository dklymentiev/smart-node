"""Append-only audit into sn_core.events. Mirrors core-api events_emit.emit:
self-registers the actor so events.actor FK always resolves. Every privileged
action MUST go through here.

Least-privilege note: the runner's pg role has INSERT on events (write-only -- it
cannot read the audit log back) and SELECT+INSERT on actors (SELECT is required for
the idempotent ON CONFLICT registration). No RETURNING on events, so events stays
write-only.
"""
import json

_LAYER_BY_PREFIX = {
    "user": "human", "human": "human", "component": "system", "system": "system",
}


def _ensure_actor(cur, actor: str) -> None:
    prefix = actor.split(":", 1)[0] if ":" in actor else actor
    layer = _LAYER_BY_PREFIX.get(prefix, "system")
    cur.execute(
        "INSERT INTO sn_core.actors (id, display_name, type, layer) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (actor, actor, prefix, layer),
    )


def audit(cur, *, actor: str, event_type: str,
          entity_type: str = None, entity_id: str = None,
          payload: dict = None) -> None:
    _ensure_actor(cur, actor)
    cur.execute(
        "INSERT INTO sn_core.events (actor, event_type, entity_type, entity_id, payload) "
        "VALUES (%s, %s, %s, %s, %s)",
        (actor, event_type, entity_type, entity_id, json.dumps(payload or {})),
    )
