"""Fast tier: deterministic tools executed BEFORE the gateway, no LLM loop.

The runtime splits an agent's tools into two tiers. The reasoning tier (brin -> model)
is for judgment and commits. The fast tier is for cheap, read-only / simple-internal
operations that need no model to EXECUTE -- only, at most, a model to DECIDE which tool
and to PHRASE the result. For fixed status queries even the phrasing is deterministic.

v1 (this module) implements ONE fast tool on ONE channel, as the agreed narrow start:

    check_messages, on telegram

WHY this matters even though Phase 1 already injects [internal mail] into every prompt:
on telegram the bound agent may be an opaque CLI (a small model via claude-cli), so a
trivial "any new messages?" still spins up a full CLI agent turn (seconds, a process).
The fast tier answers that from the bus in ~200ms with zero gateway and zero LLM.

The DECISION (is this a check-inbox request?) is made by the existing Gemini-Flash
classifier (IntentService), NOT a hardcoded keyword list -- the model decides, we only
act on its verdict. The EXECUTION reuses the bus inbox (identity-scoped by Remote-User,
so reading one's own inbox needs no extra grant). Every fast call emits auditable events
so it is auditable and shows up in the [tool calls] ledger like any other tool.
"""

import logging
import os

import httpx

from services.ledger_service import fetch_internal_mail, _snippet
from core.service_proof import service_headers as _service_headers

logger = logging.getLogger("brin.fast_tier")

CORE_API_URL = os.environ.get("CORE_API_URL", "")

# Fast tier is opt-in per channel. DISABLED for now (empty set): the check_inbox
# classifier misfired on normal questions (e.g. "who handles that here") and the
# fast path dumped the agent's raw INTERNAL coordination inbox to the end-user --
# which the agent must never reveal. Internal mail is already surfaced to the agent
# via the [internal mail] prompt section (Phase 1) and relayed by the reasoning tier.
# Re-enable only with a redesign that never shows raw internal mail to the user.
_FAST_CHANNELS = frozenset()


async def _emit(actor: str, event_type: str, entity_id: str, payload: dict) -> None:
    """Best-effort audit event so a fast-tier call is auditable + ledger-visible.
    Never raises -- telemetry must not break a user reply."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            await c.post(
                f"{CORE_API_URL}/api/events",
                headers=_service_headers(),
                json={"actor": actor, "event_type": event_type,
                      "entity_type": "tool", "entity_id": entity_id, "payload": payload},
            )
    except Exception as e:
        logger.warning("fast-tier event emit failed (%s): %s", event_type, e)


def _render_inbox_reply(inbox: dict) -> str:
    """Turn the bus inbox into a short, deterministic user-facing status line.

    Echoes message CONTENT verbatim (the valuable part, language-agnostic) under a
    minimal English label. No LLM. Shows new INBOUND messages -- what a 'do I have
    messages?' query is actually asking.
    """
    convo = inbox.get("conversation") or []
    new_ids = set(inbox.get("new_message_ids") or [])
    new_msgs = [m for m in convo if m.get("id") in new_ids
                and m.get("direction") == "inbound"]
    if not new_msgs:
        return "No new messages."
    lines = ["New messages (%d):" % len(new_msgs)]
    for m in new_msgs:
        lines.append("- %s: %s" % (m.get("sender") or "?", _snippet(m.get("content"), 200)))
    return "\n".join(lines)


async def try_fast_path(message: str, channel: str, agent_slug: str,
                        intent_service) -> dict | None:
    """If this turn is a fast-tier hit, execute it deterministically and return a
    response dict {content, specialist, fast_tier}. Otherwise return None so the
    caller proceeds to the normal router -> gateway path.

    None (fall through) when: channel not fast-enabled, no agent bound, no classifier,
    the classifier did not call it check_inbox, or any execution error (fail-soft:
    a fast-tier problem must degrade to the normal path, never drop the turn).
    """
    if channel not in _FAST_CHANNELS or not agent_slug or intent_service is None:
        return None
    try:
        result = await intent_service.classify(message)
        intent = getattr(result, "intent", None)
    except Exception as e:
        logger.warning("fast-tier classify failed: %s; normal path", e)
        return None
    if intent != "check_inbox":
        return None

    actor = f"agent:{agent_slug}"
    await _emit(actor, "tool.invoked", "fast_check_messages", {"name": "fast_check_messages"})
    try:
        inbox = await fetch_internal_mail(agent_slug)
    except Exception as e:
        inbox = None
        logger.warning("fast-tier check_messages errored for %s: %s", agent_slug, e)
    if inbox is None:
        # Could NOT read the mailbox (failed fetch / unresolved principal). Do not
        # claim "no messages" -- that would be a false statement. Emit a terminal
        # for honesty and fall through to the normal agent path.
        await _emit(actor, "tool.failed", "fast_check_messages", {"error": "inbox fetch failed"})
        logger.info("fast-tier check_messages could not read inbox for %s; normal path", agent_slug)
        return None
    reply = _render_inbox_reply(inbox)
    await _emit(actor, "tool.succeeded", "fast_check_messages",
                {"name": "fast_check_messages", "result": reply})
    logger.info("fast-tier check_messages served for %s (no gateway)", agent_slug)
    return {"content": reply, "specialist": "_fast", "fast_tier": "check_messages"}
