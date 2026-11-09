"""MessageWatcher -- background poller that wakes the owning agent when a new
inbound message is recorded in the registry.

This is the dispatch link of the communication layer: producers record inbound
rows into sn_core.messages (channel adapters for external mail/chat; the
core-api /inbound webhook for internal system messages); this watcher notices
new inbound and wakes the owning agent through brin's own /api/message. Because
the wake passes agent=<slug>, the gateway runs in agent-driven mode and resolves
the agent's backend from the DB -- so a CLI agent runs with its tools.

Routing:
- channel_type == 'system' -> an internal doorbell addressed to an agent slug
  (recipient IS the slug). Wake that agent with the message content as the
  prompt, then mark the doorbell processed -- no agent closes a system message,
  so the watcher consumes it (keeps the table clean + restart-safe).
- otherwise (email/chat) -> wake the default agent (secretary) with a generic
  "check your inbox" nudge; the secretary owns the new->processed transition via
  comm. This preserves the verified email path unchanged.

De-dup is an in-process set of already-dispatched ids (mirrors ReinWatcher).
Wake is event-driven, so a woken agent never spends an LLM call on an empty
tick (the earlier change acceptance criterion, met by construction).
"""

import asyncio
import logging
import os

import httpx
from core.service_proof import service_headers as _service_headers

from core.owner_proof import owner_headers as _owner_headers
from core.owner_proof import owner_read_headers as _owner_read_headers

logger = logging.getLogger("brin.message_watcher")

_POLL_INTERVAL = int(os.environ.get("MESSAGE_POLL_INTERVAL", "10"))
# Ceiling for the failure back-off below: still checks twice a minute at worst,
# so a node that recovers is not left waiting, and a node that does not recover
# is not writing a traceback every ten seconds.
_MAX_BACKOFF = int(os.environ.get("MESSAGE_POLL_MAX_BACKOFF", "30"))
_CORE_API_URL = os.environ.get("CORE_API_URL", "").rstrip("/")
_BRIN_SELF_URL = os.environ.get("BRIN_SELF_URL", "http://127.0.0.1:8101").rstrip("/")
_API_KEY = os.environ.get("BRIN_API_KEY", "")
_DEFAULT_AGENT = os.environ.get("BRIN_DEFAULT_AGENT", "secretary")
# Identity for the core-api read/patch (the CLI tools fail closed without one).
# The CONFIGURED value only: it may name an agent this node does not have.
# Resolved against reality by _actor(); never used directly.
_CONFIGURED_ACTOR = os.environ.get("MESSAGE_WATCHER_ACTOR", _DEFAULT_AGENT)

# WHO THIS WATCHER CALLS AS. Not an agent, and that was the whole bug.
# It reads the WHOLE inbound queue in order to route it, which is a node-level
# job; an agent slug here is the watcher borrowing a name that is not its own.
# Since the earlier change identity hardening core-api refuses exactly that on the TCP
# door -- "an agent identity is accepted only over the core-api unix socket,
# where the kernel establishes it" -- so every poll answered 403, every ten
# seconds, on every node, for a whole release cycle.
#
# The socket is NOT the answer here, and that is the trap: mcp_messaging.py fixed
# the same 403 by moving to the socket, but it is spawned as the AGENT's own OS
# user, so SO_PEERCRED names an agent row. This service runs as
# smart-node-dispatcher, which has no agent row, and on the socket the
# Remote-User header is ignored entirely rather than outranked -- it answers
# "no active user for identity 'unknown-peer'". Neither door will carry an agent
# identity on this watcher's behalf.
#
# So the watcher calls as the node owner. The name alone is enough for its
# reads; its two mutations (the desk delivery decision, closing a system
# message) carry the service proof, which core-api accepts on those routes
# only. Both come from node configuration: see core/owner_proof.py.

# None = not resolved yet, False = asked and there is nobody. Different things.
_RESOLVED_ACTOR = None
_ACTOR_COMPLAINED = False


async def _actor(client):
    """Which agent this watcher polls as, decided against the node rather than
    read from a literal.

    The code default is `secretary`, an agent no node we ship has, so core-api
    refused EVERY poll with 403 and inbound routing stopped -- silently, because
    the 403s land in a log nobody reads while the service stays active and every
    check passes. Writing it at install time cannot work either: the dispatcher
    installs ~96 seconds before the first agent exists.

    So: the configured agent if the node actually has it, else the node's first
    enabled agent, else nobody -- said once, not every ten seconds forever.
    """
    global _RESOLVED_ACTOR, _ACTOR_COMPLAINED
    if _RESOLVED_ACTOR is not None:
        return _RESOLVED_ACTOR or None
    try:
        r = await client.get(f"{_CORE_API_URL}/api/agents",
                             headers=_owner_read_headers())
        r.raise_for_status()
        agents = (r.json() or {}).get("agents") or []
    except Exception as e:
        # Transient. Do NOT cache a failure as "nobody".
        logger.warning("MessageWatcher: cannot resolve an actor yet (%s)", e)
        return None

    slugs = [a.get("slug") for a in agents if a.get("slug")]
    enabled = [a.get("slug") for a in agents
               if a.get("slug") and a.get("enabled") is not False]
    if _CONFIGURED_ACTOR in slugs:
        _RESOLVED_ACTOR = _CONFIGURED_ACTOR
    elif enabled:
        _RESOLVED_ACTOR = enabled[0]
        logger.warning("MessageWatcher: configured actor %r is not an agent on this "
                       "node; polling as %r instead", _CONFIGURED_ACTOR, _RESOLVED_ACTOR)
    else:
        # Not cached: the first agent arrives minutes after this service starts,
        # and a cached "nobody" left every relay into a chat 'new' until a restart
        # (a reminder answered, never shown; fresh node 2026-10-30).
        if not _ACTOR_COMPLAINED:
            _ACTOR_COMPLAINED = True
            logger.error("MessageWatcher: this node has no agents, so inbound "
                         "messages cannot be routed to anyone. Saying this once.")
    return _RESOLVED_ACTOR or None

_WAKE_PROMPT = (
    "You have new inbound messages. Run your loop: check your inbox and handle "
    "each new message following your tool contracts. Be concise."
)

class MessageWatcher:
    """Polls core-api for new inbound messages and wakes the owning agent."""

    def __init__(self):
        self._seen: set[int] = set()
        self._held: set[int] = set()     # waiting for a stopped agent, said once
        self._poll_task = None
        self._running = False
        self._wake_seq = 0

    async def start(self):
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("MessageWatcher started (interval=%ss, core=%s, default_agent=%s)",
                    _POLL_INTERVAL, _CORE_API_URL, _DEFAULT_AGENT)

    async def stop(self):
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("MessageWatcher stopped")

    async def _poll_loop(self):
        # A poll that fails usually fails for a reason that will not clear by
        # itself (a refused identity, a door that moved), so the honest shape is
        # loud once and quiet after, not an ERROR every _POLL_INTERVAL forever.
        # The previous shape wrote 130k tracebacks into a 429 MB log on one node
        # and 51k into a 901 MB log on another while the service sat there
        # 'active' and every check passed: nobody reads a file that size, so the
        # node was mute and looked healthy. Back off to a ceiling, keep saying it
        # at a readable rate, and say plainly when it starts working again.
        fails = 0
        while self._running:
            try:
                delay = _POLL_INTERVAL
                if fails:
                    delay = min(_POLL_INTERVAL * (2 ** min(fails, 5)), _MAX_BACKOFF)
                await asyncio.sleep(delay)
                await self._check_messages()
                if fails:
                    logger.info("MessageWatcher: polling again after %s consecutive "
                                "failures", fails)
                    fails = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                fails += 1
                if fails == 1:
                    logger.exception("MessageWatcher poll error")
                elif fails in (2, 5, 10) or fails % 60 == 0:
                    logger.error("MessageWatcher: poll still failing (%s consecutive) "
                                 "-- %s: %s", fails, type(e).__name__, e)

    async def _check_messages(self):
        msgs = await self._new_inbound()
        fresh = [m for m in msgs if m["id"] not in self._seen]
        # v1 (simple, from-simple-to-complex): one message at a time, in order, no
        # batching. The wake rule is permission-driven -- wake the recipient iff it
        # is permitted to read the channel the message arrived on (resolved by core).
        for m in fresh:
            await self._route_one(m)

    # Definitive denials -> record + dead-letter. Anything else (core hiccup) is a
    # transient failure: leave the message 'new' and retry next tick.
    _TERMINAL = ("no-binding", "not-permitted", "recipient-archived",
                 "no-agent", "no-recipient")

    async def _route_one(self, m):
        chan = m.get("channel_type") or "system"
        recipient = m.get("recipient") or ""
        # A message to desk:<id> is already delivered: the desk history reads it
        # by recipient, whatever its status, and there is no agent to wake. It
        # was resolved like an agent and logged as routing_denied/no-agent on
        # every relay into a desk (sn-4680). Close it quietly.
        # writing a row into a desk is intent, not delivery. core-api
        # decides (the desk's own agent, the system relay or a person: delivered;
        # any other agent: blocked + message.routing_denied) and the desk history
        # shows delivered rows only. A failed call leaves the row 'new' for the
        # next tick rather than guessing.
        if recipient.startswith("desk:"):
            if await self._deliver_desk(m["id"]):
                self._seen.add(m["id"])
            return
        target, reason = await self._resolve(chan, recipient)
        if not target and reason == "recipient-stopped":
            # The agent exists and is stopped: the message waits, 'new', and is
            # handled when the agent is started again. Not seen, so every poll
            # re-resolves it and notices the start. Said once (core-api keeps
            # the once across restarts; the set only spares it the call).
            if m["id"] not in self._held:
                logger.info("MessageWatcher: id=%s on %s for %r HELD (recipient stopped)",
                            m["id"], chan, recipient)
                if await self._held_notice(m["id"]):
                    self._held.add(m["id"])
            return
        self._held.discard(m["id"])
        if not target:
            if reason in self._TERMINAL:
                logger.info("MessageWatcher: id=%s on %s for %r DENIED (%s)",
                            m["id"], chan, recipient, reason)
                await self._emit_denied(m["id"], recipient, chan, reason)
                self._seen.add(m["id"])
                await self._mark_processed(m["id"])     # dead-letter -> out of 'new'
            else:
                logger.warning("MessageWatcher: id=%s resolve transient (%s) -- retry",
                               m["id"], reason)
            return
        # permitted: the intake-only rule -- MessageWatcher is now Intake: it does NOT launch. It leaves
        # the message 'new' for the Runner (smart-node-runner polls free agents with pending work
        # and launches them). Denials/dead-letters are handled above. Mark seen to avoid re-logging.
        logger.info("MessageWatcher(Intake): id=%s on %s permitted for %s -> leave for Runner",
                    m["id"], chan, target)
        self._seen.add(m["id"])

    async def _resolve(self, channel_type, recipient):
        """Ask core which agent is permitted to read this (channel, recipient).
        Returns (agent_slug | None, reason)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    "%s/api/messages/resolve-recipient" % _CORE_API_URL,
                    headers=_service_headers(),
                    params={"channel_type": channel_type or "",
                            "recipient": recipient or ""})
            if r.status_code == 200:
                d = r.json()
                return d.get("agent"), d.get("reason") or "unknown"
            return None, "resolve-error-%s" % r.status_code
        except Exception:
            logger.exception("MessageWatcher: resolve failed for %s/%s",
                             channel_type, recipient)
            return None, "resolve-exception"

    async def _emit_denied(self, msg_id, recipient, channel_type, reason):
        """Record a routing denial in the event log (audit of refused wakes)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    "%s/api/events" % _CORE_API_URL,
                    headers=_service_headers(),
                    json={"actor": "component:message-watcher",
                          "event_type": "message.routing_denied",
                          "entity_type": "message",
                          "entity_id": str(msg_id),
                          "payload": {"recipient": recipient, "channel": channel_type,
                                      "reason": reason}})
        except Exception:
            logger.debug("MessageWatcher: routing_denied emit failed (non-fatal)")

    async def _new_inbound(self):
        url = "%s/api/messages?direction=inbound&status=new&limit=100" % _CORE_API_URL
        async with httpx.AsyncClient(timeout=10.0) as client:
            actor = await _actor(client)
            if not actor:
                # Nobody to wake yet. Not an identity problem any more (we call as
                # the owner); this is the node having no agent to route to at all.
                return []
            r = await client.get(url, headers=_owner_read_headers())
            r.raise_for_status()
            data = r.json()
        out = []
        for m in data.get("messages", []):
            if "id" in m:
                out.append({
                    "id": m["id"],
                    "channel_type": m.get("channel_type"),
                    "recipient": m.get("recipient"),
                    "sender": m.get("sender"),
                    "content": m.get("content"),
                })
        return out

    async def _deliver_desk(self, mid):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    "%s/api/messages/%s/deliver-desk" % (_CORE_API_URL, mid),
                    headers=_owner_headers())
            if r.status_code == 200:
                d = r.json()
                logger.info("MessageWatcher: desk message id=%s -> %s (%s)",
                            mid, d.get("decision"), d.get("reason"))
                return True
            logger.warning("MessageWatcher: deliver-desk id=%s answered %s -- retry",
                           mid, r.status_code)
        except Exception:
            logger.exception("MessageWatcher: deliver-desk failed for id=%s", mid)
        return False

    async def _held_notice(self, mid):
        """Tell core-api this message waits for a stopped agent (idempotent there)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post("%s/api/messages/%s/held" % (_CORE_API_URL, mid),
                                      headers=_service_headers())
            if r.status_code == 200:
                return True
            logger.warning("MessageWatcher: held notice for %s answered %s: %s",
                           mid, r.status_code, r.text[:200])
        except Exception:
            logger.exception("MessageWatcher: held notice for %s failed", mid)
        return False

    async def _mark_processed(self, mid):
        """Consume a system doorbell -- no agent closes a system message."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                actor = await _actor(client)
                if not actor:
                    return
                r = await client.patch(
                    "%s/api/messages/%s/state" % (_CORE_API_URL, mid),
                    headers={**_owner_headers(), "Content-Type": "application/json"},
                    json={"status": "processed"})
            if r.status_code != 200:
                # Left 'new', the doorbell is re-read on every poll: say why.
                logger.error("MessageWatcher: marking system msg %s processed "
                             "answered %s: %s", mid, r.status_code, r.text[:200])
        except Exception:
            logger.exception("MessageWatcher: failed to mark system msg %s processed", mid)
