"""Executor: runs direct agent calls via AI Gateway or Rein workflows."""

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

import logging

import httpx

from core.logging_config import correlation_id_var
from core.turn_budget import gateway_call_timeout

from core.specialist_loader import SpecialistLoader
from core.tools import ToolsConfig
from core.catalog_tools import CatalogTools, normalise_tool_calls
from core.attachment_delivery import content_for_agent
from core.service_proof import service_headers as _service_headers
from services.ledger_service import build_ledger_sections
from models.actions import DirectAgent, ReinWorkflow
from models.memory import SmartSearchRequest, SmartSearchResult, MemoryContextResult

logger = logging.getLogger("brin.executor")


GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:19850")
REIN_TASKS_DIR = "/srv/agents/tasks"
DEFAULT_MODEL = "sonnet"
CORE_API_URL = os.environ.get("CORE_API_URL", "")
MAX_TOOL_ROUNDS = int(os.environ.get("SN_MAX_TOOL_ROUNDS", "12") or 12)

# Action-claim guard: phrases that assert a delegation/forward was or will be done.
# If the model emits one of these but did NOT actually call send-message this turn,
# the guard re-prompts it once to act-or-retract (kills "I'll forward it to a
# colleague" lies).
_SEND_CLAIM_HINTS = (
    "i'll forward", "i will forward", "forward your request", "forwarded your request",
    "i'll ask", "i will ask", "i'll send", "i'll check with", "i have sent", "request sent",
)
_SEND_GUARD_REPROMPT = (
    "[SYSTEM CHECK] Your verified tool record for this turn shows you did NOT send "
    "a message (send-message / messages_send). But your reply claims you forwarded / "
    "will forward / asked a colleague. You cannot claim an action you did not perform. "
    "Do ONE of these NOW: (a) actually send it with your message tool this turn, then "
    "say it is sent; or "
    "(b) rewrite your reply so it makes no such claim (ask the person what they need, "
    "or just answer). Do it now."
)

# a background turn woken by a colleague's message must end in an answer
# to that colleague. On 2026-09-25 HTTP-model agent read web-agent's question and answered it in
# plain text, and later spent two whole turns hunting for
# task comments she had no tool to read, hit the round cap, and said nothing. Both
# times the text went into a background thread nobody reads, the messages stayed
# new, and the asker told the owner "she is silent". Her prompt already said "if
# you lack a capability, say so and hand it back"; the loop gave her no budget to
# see and no way out, so the loop now does.
_REPLY_TOOLS = ("messages_reply", "messages_send", "send-message", "messages_dismiss")
_BUDGET_WARN_LEFT = 2
_BUDGET_NOTE = (
    "[SYSTEM] %d tool rounds left in this turn. If you cannot get what you need, "
    "answer the sender now with messages_reply: say what you could not do and why."
)
_LAST_ROUND_NOTE = (
    "[SYSTEM] Last round of this turn. Only answering is possible now: reply to the "
    "message you were woken for with messages_reply, with what you have."
)
_BACKGROUND_TEXT_REPROMPT = (
    "[SYSTEM CHECK] This is a background turn: nobody reads the text you just wrote. "
    "If it answers a message, send it now with messages_reply(<that message id>, "
    "<your answer>). If nothing needs an answer, close the message with messages_dismiss."
)
# runner-<slug>-<highest pending message id>-<seq> (brin/runner.py); a slug may
# itself contain dashes, so the two numbers are read from the end.
_RUNNER_REF = re.compile(r"^runner-.+-(\d+)-(\d+)$")

# Session statuses that are permanently closed and must never be restarted or reused.
# Written as an explicit positive set so that any new terminal status must be added
# here intentionally -- a negative list (what is NOT terminal) silently misses new
# statuses. See migration 025 for the full status enum.
_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "reaped"})


def turn_withholds(conversation, asked):
    """The tools one turn goes without. A turn in a chat sees that chat only: the
    inbox is the agent's, and with it another chat's reminder, another person's
    among them, came along with a person's message (2026-10-31). What waits there
    gets a wake of its own."""
    if str(conversation).startswith("desk:"):
        return ["mcp__messaging__inbox_read", "mcp__messaging__inbox_dismiss"]
    return asked


# The tools that start a message to a colleague (the comm tool and the api form
# of the messaging grant).
_SEND_TOOLS = ("send-message", "messages_send")
# Turns nobody is waiting on: the Runner wakes an agent with channel "system";
# internal mail is agent to agent. A question asked from one of them has no
# person to relay the answer back to.
_BACKGROUND_CHANNELS = ("system", "internal")


def _send_origin(tool: str, channel, conversation_id):
    """Where a question passed to a colleague came from, or None.

    The origin is what lets the colleague's answer be relayed back to the
    conversation that asked (inbox to_relay). Only background turns are named,
    and everything else is a turn a person waits on: the Studio posts desk
    chat to brin with NO channel at all, so requiring one (6e09a01) stamped
    nothing on the desk path, the one the owner uses; seen on sn-4680, where
    HTTP-model agent's question to foreman left without an origin and his answer was
    dismissed instead of relayed. "internal" for an unset channel is what brin
    stamped before, and the relay to desk:<id> goes over internal mail anyway.
    """
    if tool not in _SEND_TOOLS or not conversation_id or channel in _BACKGROUND_CHANNELS:
        return None
    return {"channel": channel or "internal", "conversation": conversation_id}

class GatewayWorkingError(Exception):
    """Gateway returned 202: the turn is still running as a background task.

    The response will appear in the session history when it arrives. This is not
    a failure -- do not convert it to _COULD_NOT_ANSWER.
    """


class QueuedTurnCancelledError(Exception):
    """The owner cancelled a desk message before it reached the model."""


class TurnStoppedError(Exception):
    """A person stopped the turn before it finished.

    Not a failure and not an answer. It carries what the agent had said by then,
    which the gateway has already stored marked partial. Raised rather than
    returned so nothing after the turn (the action-claim re-prompt, memory
    writes) treats a stopped turn as a finished one and starts the work again.
    """

    def __init__(self, content: str, stopped_by: str | None):
        self.content = content
        self.stopped_by = stopped_by
        super().__init__(f"turn stopped by {stopped_by or 'unknown'}")


class SessionResetError(Exception):
    """Gateway returned 409 session_reset: that session was handed off and closed.

    The message was NOT delivered. This is not a failure either: the gateway
    wrote the session's context to memory and retired it, so the same
    text has to go to a clean session. Losing it here would mean a person's
    second message disappearing because the agent happened to hit its context
    boundary on the first one.
    """


class AgentStoppedError(Exception):
    """Gateway returned 409 agent_stopped: the owner stopped this agent.

    Not a failure of the turn and not _COULD_NOT_ANSWER: the agent is disabled
    on purpose and runs no turn until it is enabled again. The person is told
    that, by name.
    """

    def __init__(self, agent: str | None, reason: str | None):
        self.agent = agent
        self.reason = reason
        super().__init__(reason or f"agent {agent or '?'} is stopped")


class SessionBusyDeferredError(Exception):
    """Gateway returned 409 for any reason that is not session_reset nor
    agent_stopped: the session already has a turn in flight, or the request
    duplicated a client_turn_id already being handled, or the gateway hit a
    transient on-session race. In every sub-case the person's turn is NOT a
    failure to report: either the gateway stored the inbound message and the
    turn will run when the lock frees (the answer arrives in session history),
    or the identical client_turn_id is already being processed. Raise this so
    main.py returns the same working-ack it uses for GatewayWorkingError
    instead of converting the 409 to _COULD_NOT_ANSWER.
    """

    def __init__(self, session_id: str | None, reason: str | None):
        self.session_id = session_id
        self.reason = reason
        super().__init__(reason or "session busy; turn deferred")


class BudgetExhaustedError(Exception):
    """Gateway returned 402: the node has hit its spending limit for this period.

    Not a failure -- the limit was set deliberately by the owner and will reset
    at a known time. Do not convert to _COULD_NOT_ANSWER; surface the real reason.

    """

    def __init__(self, reset_at: str | None, limit: float | None, spent: float | None,
                 period: str | None = None):
        self.reset_at = reset_at
        self.limit = limit
        self.spent = spent
        self.period = period
        super().__init__(
            f"budget_exhausted: {period or 'period'} limit ${limit} reached "
            f"(spent ${spent}); resets at {reset_at}"
        )



@dataclass
class TurnArtifacts:
    """Response artifacts owned by one turn and passed back to its caller.

    The caller keeps this object while consuming run_direct's text chunks.
    Explicit ownership also works when the generator runs in a child task.
    """
    actions: list = field(default_factory=list)
    memory: Optional[SmartSearchResult] = None
    final_answer: Optional[str] = None


class Executor:
    """Executes routed actions via AI Gateway or Rein."""

    def __init__(self, loader: SpecialistLoader, tools_config: ToolsConfig = None,
                 memory_service=None):
        self.loader = loader
        self.tools_config = tools_config or ToolsConfig()
        self.memory_service = memory_service
        self._sessions: dict[str, str] = {}  # specialist_id -> gateway session_id
        # Phase 2 catalog tool loop (transparent agents): per-agent
        # client-side history + cached assembled system prompt.
        self._tool_history: dict[str, list[dict]] = {}
        self._tool_system: dict[str, str] = {}

    async def run_direct(
        self, action: DirectAgent, model: str = DEFAULT_MODEL, stream: bool = False,
        workspace: str = None, agent_override: str = None, conversation_id: str = None,
        channel: str = None,
        principal: str = None,
        person_identity: str = None,
        artifacts: Optional[TurnArtifacts] = None,
        client_turn_id: str = None,
        wait_seconds: float = None,
        tools: list = None,
        answer_ids: list = None,
        wake_desk: str = None,
    ) -> AsyncIterator[str]:
        """Send message to a single specialist via AI Gateway.

        stream=False (default): uses /chat endpoint, yields full response as single chunk.
        stream=True: uses /chat/stream endpoint, yields SSE chunks.
        workspace: Mesh workspace name for agent-scoped memory search.
        artifacts: caller-owned result container; omitted for text-only callers.
        """
        artifacts = artifacts if artifacts is not None else TurnArtifacts()
        artifacts.actions = []
        artifacts.memory = None
        artifacts.final_answer = None
        specialist = self.loader.get(action.specialist_id)
        system_prompt = specialist.content if specialist else ""

        # Which DB agent runs this turn. When set, the session is created in the
        # gateway's agent-driven mode, which assembles the prompt and resolves
        # the model + backend from core-api and the catalog -- so brin no longer
        # fetches the prompt here. _default_agent=None falls back to the
        # specialist .md body via Mode B.
        import os as _os
        _default_agent = agent_override or _os.environ.get("BRIN_DEFAULT_AGENT")

        # Phase 2: when a DB agent is bound, run brin's catalog tool loop if
        # the agent has tools brin can dispatch (api/builtin) on a transparent
        # backend. Opaque (claude-cli) agents and mcp-only grants fall through
        # to the normal gateway turn below. The loop yields one final chunk
        # (works for streaming and non-streaming callers; token streaming TODO).
        if _default_agent:
            tool_answer = await self._maybe_run_tool_loop(
                _default_agent, action.message, workspace=workspace,
                conversation_id=conversation_id, channel=channel,
                person_identity=person_identity,
                artifacts=artifacts,
            )
            if tool_answer is not None:
                yield tool_answer
                return

        session_id = await self._get_or_create_session(
            action.specialist_id, system_prompt, model, agent_slug=_default_agent,
            conversation_id=conversation_id, channel=channel, tools=tools, answer_ids=answer_ids,
            wake_desk=wake_desk,
        )

        # Memory enrichment (if enabled)
        message_to_send = action.message
        mem_cfg = self.tools_config.get_memory()
        if mem_cfg.enabled and mem_cfg.mode == "auto-inject" and self.memory_service:
            memory_ctx = await self._enrich_with_memory(action.message, workspace=workspace,
                                                      artifacts=artifacts)
            if memory_ctx and memory_ctx.context:
                message_to_send = memory_ctx.context + action.message

        # Caller identity (authenticated channels): name who the agent is
        # speaking with this turn, resolved from the registry via core-api.
        # A Studio desk IS the thread: the Studio passes user_id 'desk:<uuid>'
        # as conversation_id, and that is exactly the thread_id web-chat rows
        # are written with. Naming it keeps another desk with the same person
        # out of this turn, in BOTH blocks that quote the person back.
        _thread = conversation_id if str(conversation_id or "").startswith("desk:") else None
        _thread_channel = "web-chat" if _thread else None

        if principal:
            _cp = await self._caller_identity_prefix(principal)
            _xc = await self._cross_channel_block(
                principal, agent_slug=_default_agent,
                thread=_thread, thread_channel=_thread_channel)
            if _xc:
                message_to_send = _xc + message_to_send
            if _cp:
                message_to_send = _cp + message_to_send

        # durable ledger snapshot ([internal mail] / [tool calls]) for the
        # bound agent. In this path the model is opaque (claude-cli runs its own
        # loop), so we cannot inject mid-loop -- but the start-of-turn snapshot lets
        # it see pending colleague mail and recent tool results without spending a
        # turn to fetch them. Best-effort: empty string when nothing/agentless/error.
        if _default_agent:
            _ledger = await build_ledger_sections(
                _default_agent, person_identity,
                thread=_thread, thread_channel=_thread_channel,
                with_mail=not tools)   # a chat wake (tools withheld) lists no other mail
            if _ledger:
                message_to_send = _ledger + message_to_send

        # Studio stores stable attachment handles in the transcript.  Resolve
        # them only for the model turn, through core-api's approved/access door;
        # raw_content below remains the person's textual message for the UI.
        if _default_agent and _thread:
            message_to_send = await content_for_agent(message_to_send)

        # action.message is the raw user text before any enrichment was prepended.
        # Pass it as raw_content so the gateway stores what was actually typed
        # rather than the scaffolding blocks.
        _raw = action.message if message_to_send != action.message else None

        async def _resolve_clean_session() -> str:
            """Re-resolve after a context handoff: the retired session is terminal,
            so _get_or_create_session skips it and creates a fresh one, which the
            gateway opens with the handoff document injected."""
            return await self._get_or_create_session(
                action.specialist_id, system_prompt, model, agent_slug=_default_agent,
                conversation_id=conversation_id, channel=channel, tools=tools, answer_ids=answer_ids,
            wake_desk=wake_desk,
            )

        # The streaming twin is not covered: it posts to a session-scoped
        # /chat/stream path the gateway does not serve, so nothing reaches a
        # retired session through it. Wiring a retry there would be dead code.
        if stream:
            async for chunk in self._stream_response(session_id, message_to_send,
                                                     raw_content=_raw):
                yield chunk
        else:
            try:
                result = await self._chat_response(session_id, message_to_send,
                                                   raw_content=_raw, artifacts=artifacts,
                                                   client_turn_id=client_turn_id,
                                                   wait_seconds=wait_seconds)
            except SessionResetError:
                session_id = await _resolve_clean_session()
                result = await self._chat_response(session_id, message_to_send,
                                                   raw_content=_raw, artifacts=artifacts,
                                                   client_turn_id=client_turn_id,
                                                   wait_seconds=wait_seconds)
            # COMMAND: extraction + execution (when allowed)
            if action.allow_commands:
                result = await self._process_commands(result)
            else:
                # Strip commands that shouldn't be executed
                blocked = self._extract_commands(result)
                if blocked:
                    logger.info("Commands blocked (allow_commands=False): %s", blocked)
                    result = re.sub(r'\n*COMMAND:.*?(?=\n|$)', '', result).strip()
                    result += "\n\n---\n[Commands are not available on this channel. Use Telegram to run commands.]"
            yield result

    async def _caller_identity_prefix(self, principal: str) -> str:
        """Resolve an authenticated caller identity (Authum Remote-User) to the
        person behind it via core-api, and return a one-line context prefix naming
        them. Empty string when unknown or on any error (non-fatal)."""
        ident = (principal or "").strip()
        if not ident:
            return ""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{CORE_API_URL}/api/users/resolve-identity",
                    headers=_service_headers(),
                    params={"identity": ident},
                )
            if r.status_code == 200:
                d = r.json()
                if d.get("found"):
                    who = d.get("name") or ident
                    tag = "the owner" if d.get("is_owner") else (d.get("role") or "a known user")
                    return (
                        f"[Caller identity: {who} ({tag}), authenticated on this "
                        f"channel. You are speaking with them directly -- treat their "
                        f"requests as direct instructions, not a customer enquiry.]\n\n"
                    )
        except Exception as e:
            logger.warning("caller identity resolve failed: %s", e)
        return ""

    async def _cross_channel_block(self, principal: str,
                                    agent_slug: str = None,
                                    thread: str = None,
                                    thread_channel: str = None) -> str:
        """Previous messages between THIS agent and this person, oldest->newest.

        Scoped to the agent+person pair. Each agent sees
        only its own history with the person; cross-agent disclosure is prevented
        at the endpoint. Empty on unknown/error (non-fatal). agent_slug is required
        for scoped history; without it the endpoint returns 400 and history is
        silently skipped (fail-soft: missing slug must not block the turn)."""
        ident = (principal or "").strip()
        if not ident:
            return ""
        if not agent_slug:
            logger.warning("cross-channel history: no agent_slug, skipping")
            return ""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                _params = {"identity": ident, "agent": agent_slug, "limit": 10}
                if thread and thread_channel:
                    _params["thread"] = thread
                    _params["thread_channel"] = thread_channel
                r = await client.get(
                    f"{CORE_API_URL}/api/users/cross-channel-history",
                    headers=_service_headers(),
                    params=_params,
                )
            if r.status_code == 200:
                msgs = (r.json() or {}).get("messages") or []
                if msgs:
                    lines = []
                    for m in msgs:
                        who = "Them" if m.get("from_person") else "You"
                        ch = m.get("channel") or "?"
                        if ch == "email":
                            party = ("from " + (m.get("from") or "?")) if m.get("from_person") else ("to " + (m.get("to") or "?"))
                            lines.append("[email, %s] %s (%s) | Subject: %s | %s" % (m.get("ago") or "?", who, party, m.get("subject") or "(no subject)", m.get("text") or ""))
                        else:
                            lines.append("[%s, %s] %s: %s" % (ch, m.get("ago") or "?", who, m.get("text") or ""))
                    return (
                        "[The following are your previous exchanges with this person "
                        "across different channels. Use this context to continue "
                        "naturally. Oldest first, each tagged with how long ago it was. "
                        "Do not recap unprompted.]\n"
                        + "\n".join(lines)
                        # The block ends where the person's message begins. Without a
                        # boundary the message ran on from the last quoted line ("You:
                        # Reminder: ..."), the model read "remind me in 1 minute" as
                        # part of the past and answered "your reminder fired"
                        # without setting one (2026-10-30).
                        + "\n[end of previous exchanges]\n\n[Their message now:]\n"
                    )
        except Exception as e:
            logger.warning("cross-channel history failed: %s", e)
        return ""

    async def _enrich_with_memory(self, message: str,
                                   workspace: str = None,
                                   artifacts: Optional[TurnArtifacts] = None) -> Optional[MemoryContextResult]:
        """Search memory and format context for prompt injection. Non-fatal on error."""
        artifacts = artifacts if artifacts is not None else TurnArtifacts()
        cfg = self.tools_config.get_memory()
        try:
            search_result = await self.memory_service.search(
                SmartSearchRequest(query=message, limit=cfg.limit, workspace=workspace)
            )
            if search_result.documents:
                docs = [d for d in search_result.documents
                        if d.similarity_score >= cfg.min_score]
                if docs:
                    search_result.documents = docs
                    search_result.total = len(docs)
                    artifacts.memory = search_result
                    return self.memory_service.format_context(docs, cfg.max_context_length)
                else:
                    # All docs below min_score -- store empty result
                    artifacts.memory = SmartSearchResult(
                        query=message, search_strategy=search_result.search_strategy
                    )
            else:
                artifacts.memory = search_result
        except Exception as e:
            logger.warning("Memory enrichment failed: %s", e)
        return None

    async def _chat_response(self, session_id: str, message: str,
                              raw_content: str = None,
                              artifacts: Optional[TurnArtifacts] = None,
                              client_turn_id: str = None,
                              wait_seconds: float = None) -> str:
        """Non-streaming: POST /messages (session-first v3), return full content.

        message is the enriched form (with caller identity, cross-channel
        history, memory context prepended) that goes to the model. raw_content,
        when provided, is the original user text that gets stored in
        ai_session_messages so the desk shows what the person typed, not the
        scaffolding injected around it.
        """
        artifacts = artifacts if artifacts is not None else TurnArtifacts()
        url = f"{GATEWAY_URL}/api/v1/sessions/{session_id}/messages"
        start = time.time()
        logger.info(
            "Gateway chat request session=%s content=%s",
            session_id, message[:80],
            extra={"action": "gateway_chat_start"},
        )
        body: dict = {"role": "user", "content": message}
        if raw_content is not None:
            body["raw_content"] = raw_content
        if client_turn_id:
            body["client_turn_id"] = client_turn_id
        timeout = gateway_call_timeout()
        if wait_seconds is not None:
            body["wait_seconds"] = wait_seconds
            timeout = max(timeout, float(wait_seconds) + 60)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=body)
            duration_ms = round((time.time() - start) * 1000, 1)
            if r.status_code == 202:
                # 202 means the gateway's sync window expired and the turn
                # is continuing as a background task (SN_SYNC_RESPONSE_TIMEOUT,
                # default 285s). The answer will arrive in session history when
                # the turn completes. This is NOT a failure. Raise a typed
                # exception so the caller can handle it without treating it as
                # an upstream error.
                try:
                    working_msg = r.json().get(
                        "message",
                        "The agent is still working. "
                        "The response will appear in the session history.",
                    )
                except Exception:
                    working_msg = (
                        "The agent is still working. "
                        "The response will appear in the session history."
                    )
                logger.info(
                    "Gateway working (202) session=%s duration=%.0fms",
                    session_id, duration_ms,
                    extra={"action": "gateway_chat_working", "duration_ms": duration_ms},
                )
                raise GatewayWorkingError(working_msg)
            if r.status_code == 402:
                # 402 is BUDGET_EXHAUSTED, not a generic failure.
                # Parse the structured body and raise a typed error so brin can
                # tell the person the real reason and reset time.
                logger.warning(
                    "Gateway 402 (budget_exhausted) session=%s duration=%.0fms",
                    session_id, duration_ms,
                    extra={"action": "gateway_budget_exhausted", "duration_ms": duration_ms},
                )
                try:
                    detail = r.json().get("detail") or {}
                    if detail.get("error") == "budget_exhausted":
                        raise BudgetExhaustedError(
                            reset_at=detail.get("reset_at"),
                            limit=detail.get("limit"),
                            spent=detail.get("spent"),
                            period=next(
                                (b.get("period") for b in (detail.get("budgets") or [])
                                 if b.get("over") and b.get("hard")),
                                None,
                            ),
                        )
                except BudgetExhaustedError:
                    raise
                except Exception:
                    pass
                raise RuntimeError("gateway returned 402 on chat")
            if r.status_code == 409:
                # the session was retired to hand its context to a clean
                # one. Distinguish it from every other 409 (a genuinely busy or
                # broken session) by the structured body the gateway sends, and
                # let the caller re-send on a fresh session.
                try:
                    detail = r.json().get("detail") or {}
                except Exception:
                    detail = {}
                if isinstance(detail, dict) and detail.get("error") == "session_reset":
                    logger.info(
                        "Gateway 409 (session_reset) session=%s: re-sending on a clean session",
                        session_id,
                        extra={"action": "gateway_session_reset"},
                    )
                    raise SessionResetError(detail.get("reason") or "session was reset")
                if isinstance(detail, dict) and detail.get("error") == "agent_stopped":
                    raise AgentStoppedError(detail.get("agent"), detail.get("reason"))
                # Any other 409: the gateway refused this POST because the
                # session already has a turn in flight (transient readline race
                # inside _run_queued_turn_async), a duplicate client_turn_id
                # arrived (the original is already being processed), or the
                # session bounced for a reason that is not final failure. In
                # every sub-case the right answer to the person is "working,
                # the response will appear in session history", not a red
                # "Nothing was sent on your behalf" (which on 2026-11-04 was
                # reaching desks while the real answer was arriving in the
                # same thread). The inbound message is either already stored
                # on the gateway or being handled by the original client_turn_id,
                # so the answer will land on the chat via session history.
                # "session is <status>; cannot send" is a refusal BEFORE the message is stored
                # (sessions_v2_routes): nothing is queued, so it is a failure to report, not a turn
                # that will answer later. Deferring it would show "working" and never reply.
                if isinstance(detail, str) and detail.startswith("session is "):
                    raise RuntimeError("gateway returned 409 on chat: " + detail)
                reason = detail if isinstance(detail, str) else (
                    (detail.get("reason") or detail.get("error") or "session busy")
                    if isinstance(detail, dict) else "session busy"
                )
                logger.info(
                    "Gateway 409 (session busy) session=%s: deferring, turn queued -- %s",
                    session_id, reason,
                    extra={"action": "gateway_session_busy_deferred"},
                )
                raise SessionBusyDeferredError(session_id, str(reason))
            if r.status_code != 200:
                logger.warning(
                    "Gateway error status=%s", r.status_code,
                    extra={"action": "gateway_chat_error", "status": r.status_code, "duration_ms": duration_ms},
                )
                # RAISE, never return. This string is the agent's ANSWER.
                # Returning it made an upstream failure indistinguishable from a
                # reply -- HTTP 200, content non-empty, which is exactly what
                # dispatch checks -- so on 2026-08-04 a gateway 502 was emailed to
                # an outside address carrying internal service names and the path
                # to a secret. The failure path already exists: when _ask_brin
                # raises, dispatch tags the stage, marks the inbound row failed
                # with the reason and writes NO outbound row. It was never
                # reached. The detail stays in the exception (log and row), not in
                # anything a customer can receive.
                raise RuntimeError(
                    "gateway returned %s on chat" % r.status_code)
            data = r.json()
            if data.get("cancelled"):
                raise QueuedTurnCancelledError()
            # New endpoint shape: {message_id, user_message_id, response: {role, content, usage, tool_calls}}
            response = data.get("response") or {}
            content = response.get("content", "")
            if data.get("stopped"):
                logger.info(
                    "Gateway chat stopped by %s len=%s duration=%.0fms",
                    data.get("stopped_by") or "unknown", len(content or ""), duration_ms,
                    extra={"action": "gateway_chat_stopped", "duration_ms": duration_ms},
                )
                raise TurnStoppedError(content or "", data.get("stopped_by"))
            if data.get("failed"):
                # The gateway says the turn did not happen: Claude Code ends a
                # turn it could not do (the provider answering 529 on every
                # retry, "Prompt is too long") with an error result, and the
                # gateway reports it as failed with outcome=failed. Read as an
                # answer it made the Runner log "done" and record nothing: on
                # the trials stand three minutes of 529s left no failure in the
                # journal. Raised here, every caller already handles it -- the
                # Runner counts a failed wake and retries on its backoff, a
                # channel marks the message failed and sends nothing.
                logger.warning(
                    "Gateway turn failed after %.0fms: %s", duration_ms,
                    str(data.get("failed"))[:300],
                    extra={"action": "gateway_chat_failed", "duration_ms": duration_ms},
                )
                raise RuntimeError("gateway turn failed: %s" % str(data.get("failed"))[:300])
            artifacts.actions = response.get("tool_calls") or []
            # Missing final-answer metadata is not permission to expose the
            # full CLI turn. External dispatch requires this explicit field.
            artifacts.final_answer = response.get("final_answer")
            if artifacts.actions:
                logger.info(
                    "agent actions (%d): %s",
                    len(artifacts.actions),
                    "; ".join(
                        "%s %s" % (
                            (tc.get("name") or "?"),
                            json.dumps(tc.get("input") or {}, ensure_ascii=False)[:120],
                        )
                        for tc in artifacts.actions
                    ),
                    extra={"action": "agent_actions"},
                )
            logger.info(
                "Gateway chat ok len=%s duration=%.0fms", len(content), duration_ms,
                extra={"action": "gateway_chat_done", "status": r.status_code, "duration_ms": duration_ms},
            )
            return content

    async def _stream_response(self, session_id: str, message: str,
                               raw_content: str = None) -> AsyncIterator[str]:
        """Streaming: POST /chat/stream, yield SSE chunks.

        Gateway CLI provider sends SSE events in Python dict format (single quotes).
        Events: system (init), assistant (response text), result (final summary).
        raw_content, when provided, is forwarded so the gateway stores the raw
        user text instead of the enriched prompt.
        """
        import ast

        body: dict = {"content": message}
        if raw_content is not None:
            body["raw_content"] = raw_content
        async with httpx.AsyncClient(timeout=gateway_call_timeout()) as client:
            async with client.stream(
                "POST",
                f"{GATEWAY_URL}/api/v1/sessions/{session_id}/chat/stream",
                json=body,
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    # the streaming twin of _chat_response. Yielding the
                    # error made it part of the answer; raising ends the stream as
                    # a failure, which is what every consumer must see.
                    raise RuntimeError(
                        "gateway returned %s on chat stream"
                        % response.status_code)

                yielded = False
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    # Parse event -- Gateway may send JSON or Python repr
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        try:
                            event = ast.literal_eval(data_str)
                        except (ValueError, SyntaxError):
                            continue

                    if not isinstance(event, dict):
                        continue

                    event_type = event.get("type")

                    if event_type == "assistant":
                        msg = event.get("message", {})
                        for block in (msg.get("content") or []):
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if text:
                                    yielded = True
                                    yield text

                    elif event_type == "result" and not yielded:
                        result_text = event.get("result", "")
                        if result_text:
                            yield result_text

    async def run_workflow(self, action: ReinWorkflow) -> dict:
        """Create a Rein task and wait for completion. Returns result dict."""
        task_id = f"task-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        task_dir = Path(REIN_TASKS_DIR) / task_id

        # Create task structure
        (task_dir / "input").mkdir(parents=True, exist_ok=True)
        (task_dir / "output").mkdir(exist_ok=True)
        (task_dir / "state").mkdir(exist_ok=True)

        # Write task.yaml
        task_yaml = (
            f"id: {task_id}\n"
            f"flow: {action.flow_name}\n"
            f"output_dir: ./output\n"
            f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
        )
        (task_dir / "task.yaml").write_text(task_yaml)

        # Write input
        (task_dir / "input" / "question.txt").write_text(action.message)

        # Write input.json if extra data
        if action.input_data:
            (task_dir / "input" / "input.json").write_text(
                json.dumps(action.input_data, ensure_ascii=False)
            )

        # Set status to pending (Rein daemon picks it up)
        (task_dir / "state" / "status").write_text("pending")

        return {
            "task_id": task_id,
            "flow": action.flow_name,
            "status": "pending",
            "task_dir": str(task_dir),
            "message": f"Workflow '{action.flow_name}' queued as {task_id}. "
                       f"Rein daemon will pick it up.",
        }

    async def get_workflow_status(self, task_id: str) -> dict:
        """Check status of a running Rein workflow."""
        task_dir = Path(REIN_TASKS_DIR) / task_id
        if not task_dir.exists():
            return {"task_id": task_id, "status": "not_found"}

        status_file = task_dir / "state" / "status"
        status = status_file.read_text().strip() if status_file.exists() else "unknown"

        result = {"task_id": task_id, "status": status}

        # If complete, read output
        if status == "completed":
            output_dir = task_dir / "output"
            for f in sorted(output_dir.iterdir()):
                if f.name.endswith(".json"):
                    try:
                        result["output"] = json.loads(f.read_text())
                    except json.JSONDecodeError:
                        result["output_raw"] = f.read_text()
                    break

        return result

    # person-facing channels that reserve a CLI slot pool.
    _PERSON_CHANNELS = frozenset({"telegram", "email", "voice", "web", "web-chat"})

    async def _get_or_create_session(
        self, specialist_id: str, system_prompt: str, model: str,
        agent_slug: str = None, conversation_id: str = None,
        channel: str = None, tools: list = None, answer_ids: list = None,
        wake_desk: str = None,
    ) -> str:
        """Get an existing Gateway session or create a new one.

        When agent_slug is set, create via the gateway's agent-driven mode
        (Mode A): the gateway resolves the agent's model + system prompt +
        backend from the DB and models catalog. brin stays provider-agnostic
        and never names a backend (claude-cli / openrouter). Falls back to
        Mode B (raw model + system_prompt) when no agent is bound.

        channel is used to classify person-facing vs background sessions
       : telegram/email/voice/web/web-chat are person-facing; system
        or absent means background. The classification is written into session
        metadata so the gateway can enforce the two-pool CLI slot cap.
        """
        # One specialist may front several agents, so cache per agent when
        # agent-driven; otherwise per specialist.
        cache_key = f"agent:{agent_slug}" if agent_slug else specialist_id
        if conversation_id:
            cache_key = f"{cache_key}@{conversation_id}"
        # A session runs the model it was created with. After the owner
        # changed the agent's model the open chat went on answering with the
        # old one (2026-10-29), so a session on another model is not reused:
        # the conversation gets a new one on the agent's model.
        agent_model = (await self._agent_runtime(agent_slug))[0] if agent_slug else None
        if cache_key in self._sessions:
            sid = self._sessions[cache_key]
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    r = await client.get(
                        f"{GATEWAY_URL}/api/v1/sessions/{sid}/info"
                    )
                    if r.status_code == 200:
                        status = r.json().get("status")
                        if agent_model and r.json().get("model") != agent_model:
                            status = "completed"  # another model: start anew
                        if status in ("active", "streaming", "idle_released"):
                            logger.info(
                                "Reusing session %s for %s", sid, cache_key,
                                extra={"action": "session_reuse"},
                            )
                            return sid
                        # Session exists but is not sendable (e.g. `failed` after a
                        # crashed turn). RESUME it in place instead of spawning a
                        # fresh, context-less session -- this is the fix for the
                        # "agent introduced itself again" reset after a gateway
                        # hiccup. /restart respawns with --resume and reactivates
                        # the session so the next message lands in the same thread.
                        # Terminal sessions (completed, cancelled, reaped) cannot be
                        # restarted; skip them so a fresh session is created instead.
                        if status not in _TERMINAL_STATUSES:
                            try:
                                rr = await client.post(
                                    f"{GATEWAY_URL}/api/v1/sessions/{sid}/restart"
                                )
                                if rr.status_code == 200:
                                    logger.info(
                                        "Resumed session %s for %s (was %s)",
                                        sid, cache_key, status,
                                        extra={"action": "session_resume"},
                                    )
                                    return sid
                                logger.warning(
                                    "Resume of %s returned %s; creating new session",
                                    sid, rr.status_code,
                                )
                            except httpx.RequestError as e:
                                logger.warning(
                                    "Resume of %s failed (%s); creating new session",
                                    sid, e,
                                )
                except httpx.RequestError:
                    pass
                del self._sessions[cache_key]

        # The conversation key the GATEWAY sees. It must carry the agent, because
        # the gateway indexes sessions by conversation alone and its listing does
        # not report which agent a session belongs to. Without this, the durable
        # lookup below asks "is there a live session for this conversation?",
        # gets back whichever agent happened to speak first, and files it under
        # this agent's unique cache key -- so the key stays unique and means
        # nothing. Observed: a customer's mail to one agent was answered by another,
        # in foreman's session, because foreman's wake-cron had spoken first on
        # the shared conversation "default".
        convo_key = f"{agent_slug}:{conversation_id}" if (agent_slug and conversation_id) \
            else conversation_id

        # Durable reuse: the in-memory cache above is per-process and is
        # wiped on a dispatcher restart, which otherwise spawns a brand-new
        # session for a desk that already has one -> the agent "forgets" the
        # conversation (62da9b04 -> 3764ff72). Before creating, look the
        # conversation's existing session up in the gateway DB and reuse/resume it.
        if convo_key:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    lr = await client.get(
                        f"{GATEWAY_URL}/api/v1/sessions/v2",
                        params={"tenant_id": 1, "conversation": convo_key, "limit": 10},
                    )
                    if lr.status_code == 200:
                        for s in (lr.json().get("sessions") or []):
                            st = s.get("status")
                            sid = s.get("session_id")
                            if not sid or st in _TERMINAL_STATUSES:
                                continue
                            if agent_model and s.get("model") != agent_model:
                                continue
                            if st in ("active", "streaming", "idle_released"):
                                self._sessions[cache_key] = sid
                                logger.info(
                                    "Reusing persisted session %s for %s", sid, cache_key,
                                    extra={"action": "session_reuse"},
                                )
                                return sid
                            # exists but not directly sendable -> resume in place
                            rr = await client.post(
                                f"{GATEWAY_URL}/api/v1/sessions/{sid}/restart"
                            )
                            if rr.status_code == 200:
                                self._sessions[cache_key] = sid
                                logger.info(
                                    "Resumed persisted session %s for %s (was %s)",
                                    sid, cache_key, st,
                                    extra={"action": "session_resume"},
                                )
                                return sid
            except httpx.RequestError as e:
                logger.warning(
                    "conversation lookup failed (%s); creating new session", e,
                )

        # classify this session as person-facing or background so the
        # gateway enforces the two-pool CLI slot cap. Written into metadata
        # (not a separate field) so it survives autorespawn unchanged.
        _session_type = (
            "person" if channel in self._PERSON_CHANNELS else "background"
        )

        # tenant_id hardcoded to 1 for v0 single-tenant (per Migration 008b).
        if agent_slug:
            payload = {
                "agent_slug": agent_slug,
                "metadata": {
                    "caller": "brin", "specialist": specialist_id,
                    "conversation": convo_key, "session_type": _session_type,
                    # Tools this one session goes without (a chat wake withholds
                    # the inbox); the gateway passes them as --disallowedTools.
                    **({"withhold_tools": list(tools)} if tools else {}),
                    **({"answer_ids": list(answer_ids)} if answer_ids else {}),
                    # the chat a chat wake works for (the gateway shows it there)
                    **({"wake_desk": wake_desk} if wake_desk else {}),
                },
            }
            logger.info(
                "Creating new session (agent mode) agent=%s session_type=%s",
                agent_slug, _session_type,
                extra={"action": "session_create"},
            )
        else:
            payload = {
                "tenant_id": 1,
                "type": "chat",
                "model": model,
                "system_prompt": system_prompt,
                "metadata": {
                    "caller": "brin", "specialist": specialist_id,
                    "conversation": convo_key, "session_type": _session_type,
                },
            }
            logger.info(
                "Creating new session for %s model=%s session_type=%s",
                specialist_id, model, _session_type,
                extra={"action": "session_create"},
            )
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{GATEWAY_URL}/api/v1/sessions/v2", json=payload)
            r.raise_for_status()
            session_id = r.json()["session_id"]
            self._sessions[cache_key] = session_id
            return session_id

    # --- Phase 2: catalog-driven tool loop (transparent agents) ---

    async def _maybe_run_tool_loop(
        self, agent_slug: str, message: str, workspace: str = None,
        conversation_id: str = None, channel: str = None,
        person_identity: str = None,
        artifacts: Optional[TurnArtifacts] = None,
    ) -> Optional[str]:
        """Run the in-brin catalog tool loop for a bound agent.

        Returns the final answer string, or None to signal "not applicable --
        use the normal gateway turn". None when: no dispatchable (api/builtin)
        grants, a CLI runtime (it handles its own tools), or a lookup fails
        (fail-soft: never block the normal path).
        """
        artifacts = artifacts if artifacts is not None else TurnArtifacts()
        try:
            ct = await CatalogTools.load(agent_slug)
        except Exception as e:
            logger.warning("catalog tools load failed for %s: %s; normal path",
                           agent_slug, e)
            return None

        specs = ct.openai_specs()
        if not specs:
            return None  # nothing brin can dispatch -- let the gateway turn run

        # the agent's model field switches the mode. A CLI runtime runs
        # its own opaque tool loop (via its MCP config), so this loop is only for
        # an HTTP runtime, where brin owns every iteration. Which runtimes are CLI
        # is the registry's answer (sn_config.cli_providers, read by core-api as
        # runtime_kind), not something to infer from the shape of the id.
        model, runtime_kind = await self._agent_runtime(agent_slug)
        if not model or runtime_kind != "http":
            logger.info("agent %s model=%s runtime_kind=%s; skipping tool loop",
                        agent_slug, model, runtime_kind)
            return None

        deferred = ct.deferred_names()
        if deferred:
            logger.info("agent %s non-dispatchable grants (deferred): %s",
                        agent_slug, ", ".join(deferred))

        # Always re-fetch the assembled prompt so role/policy/directory edits take
        # effect immediately (no brin restart). The resolver call is cheap.
        system = await self._assembled_prompt(agent_slug) or ""

        # Memory and dispatched actions belong to this turn's response.
        user_text = message
        mem_cfg = self.tools_config.get_memory()
        if mem_cfg.enabled and mem_cfg.mode == "auto-inject" and self.memory_service:
            mem = await self._enrich_with_memory(message, workspace=workspace,
                                                 artifacts=artifacts)
            if mem and mem.context:
                user_text = mem.context + message

        # durable ledger sections ([internal mail] / [tool calls]). In
        # this orchestrated path brin owns the loop, so the snapshot is fresh per
        # turn; it lets the model act on pending colleague mail / see recent tool
        # state without a separate fetch tool call. Best-effort (empty on error).
        _ledger = await build_ledger_sections(agent_slug, person_identity)
        if _ledger:
            user_text = _ledger + user_text

        # Conversation history loaded fresh from the gateway persisted store
        # (single source of truth; survives brin restarts).
        history = await self._load_persisted_history(conversation_id) if conversation_id else []
        history.append({"role": "user", "content": user_text})

        turn_tools = []  # ground-truth record of tools actually dispatched this turn
        guard_fired = False  # action-claim guard fires at most once
        background = channel in _BACKGROUND_CHANNELS
        text_guard_fired = False  # background-text guard fires at most once
        for round_num in range(MAX_TOOL_ROUNDS + 1):
            round_specs = specs
            if background and round_num == MAX_TOOL_ROUNDS:
                # the last round can only answer, so a turn that ran out
                # still reaches the asker instead of ending mid-search.
                round_specs = [sp for sp in specs
                               if sp["function"]["name"] in _REPLY_TOOLS] or specs
                history.append({"role": "user", "content": _LAST_ROUND_NOTE})
            data = await self._gateway_chat_stateless(
                model, system, history, round_specs,
                session_id=conversation_id, agent=agent_slug, user=conversation_id,
                raw_user_content=message, continues_turn=round_num > 0)
            if not data:
                # this is the answer too. An empty gateway response is a
                # failure, not a reply, and must not be deliverable.
                raise RuntimeError("gateway returned no response in the tool loop")

            raw_calls = data.get("tool_calls") or None
            assistant_msg = {"role": "assistant", "content": data.get("content") or ""}
            if raw_calls:
                assistant_msg["tool_calls"] = raw_calls
            history.append(assistant_msg)

            calls = normalise_tool_calls(raw_calls)
            if not calls:
                answer = (data.get("content") or "").strip()
                if not guard_fired and self._claims_unbacked_send(answer, turn_tools):
                    guard_fired = True
                    logger.info("action-claim guard fired for %s (claimed send, ran none)", agent_slug)
                    history.append({"role": "user", "content": _SEND_GUARD_REPROMPT})
                    continue
                if background and not any(t[0] in _REPLY_TOOLS for t in turn_tools):
                    pending = await self._unanswered(ct, agent_slug, conversation_id)
                    if pending and not text_guard_fired and round_num < MAX_TOOL_ROUNDS:
                        text_guard_fired = True
                        logger.info("background-text guard fired for %s (answered in text, "
                                    "%d message(s) unanswered)", agent_slug, len(pending))
                        history.append({"role": "user", "content": _BACKGROUND_TEXT_REPROMPT})
                        continue
                    if pending:
                        await self._answer_for_agent(ct, agent_slug, conversation_id, pending,
                                                     "text_answer_not_sent", answer)
                await self._persist_tool_truth(conversation_id, turn_tools)
                # HTTP tools execute here: only the terminal, tool-free round
                # is an answer, regardless of text in earlier rounds.
                artifacts.final_answer = answer
                return answer

            for tc in calls:
                logger.info("tool call [round %d] %s(%s)", round_num, tc["name"],
                            json.dumps(tc["params"], ensure_ascii=False)[:200])
                # A question passed to a colleague carries where it came from, so
                # the answer is relayed back to that conversation (inbox to_relay).
                # messages_send is the api form of the messaging tool. A
                # background turn stamps none: it would send the answer back into
                # that background turn.
                origin = _send_origin(tc["name"], channel, conversation_id)
                if origin and isinstance(tc.get("params"), dict):
                    tc["params"].setdefault("origin", origin)
                result = await ct.dispatch(tc["name"], tc["params"])
                artifacts.actions.append({
                    "id": tc["id"], "name": tc["name"], "input": tc.get("params") or {},
                })
                turn_tools.append((tc["name"], tc.get("params"), result))
                history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })
            if background and MAX_TOOL_ROUNDS - round_num == _BUDGET_WARN_LEFT:
                history.append({"role": "user", "content": _BUDGET_NOTE % _BUDGET_WARN_LEFT})

        logger.warning("tool loop hit MAX_TOOL_ROUNDS=%d for %s",
                       MAX_TOOL_ROUNDS, agent_slug)
        await self._persist_tool_truth(conversation_id, turn_tools)
        used = ", ".join(dict.fromkeys(name for name, _p, _r in turn_tools)) or "none"
        if background and not any(t[0] in _REPLY_TOOLS for t in turn_tools):
            pending = await self._unanswered(ct, agent_slug, conversation_id)
            if pending:
                await self._answer_for_agent(
                    ct, agent_slug, conversation_id, pending, "tool_rounds_exhausted",
                    "[automatic note from the node] %s could not answer this in one turn: "
                    "it used all %d tool rounds (tools: %s) without replying. If the answer "
                    "depends on something outside this message, such as a task comment or a "
                    "document, put that content into the message itself."
                    % (agent_slug, MAX_TOOL_ROUNDS, used))
        answer = (
            "I used up this turn on tools without getting to an answer "
            f"({MAX_TOOL_ROUNDS} rounds, tools used: {used}). Ask me again and "
            "I will pick up from here."
        )
        # Into the THREAD, not only back to the caller. For an agent on an HTTP
        # model the provider completion of a tool round carries no text, so what
        # the gateway stores is an empty assistant row; the sentence the person
        # saw came from here and was never written down. The Studio then removed
        # the empty bubble on its next read and the whole exchange vanished from
        # the chat while sitting intact on the message bus.
        await self._persist_answer(conversation_id, answer)
        return answer

    async def _unanswered(self, ct, agent_slug, conversation_id):
        """Messages from colleagues this runner turn was woken for that are still
        waiting: internal, inbound, status new, id up to the one in the turn's ref.
        Empty for any turn that is not a runner wake, and on any error (the guard
        must never break a turn)."""
        m = _RUNNER_REF.match(conversation_id or "")
        if not m:
            return []
        max_id = int(m.group(1))
        try:
            # An api tool answers "[HTTP <code>] <body>" (catalog_tools._dispatch_api).
            raw = await ct.dispatch("messages_inbox", {"view": "new"})
            if not str(raw).startswith("[HTTP 200]"):
                logger.warning("unanswered check for %s: inbox answered %s",
                               agent_slug, str(raw)[:120])
                return []
            inbox = json.loads(str(raw)[len("[HTTP 200]"):])
        except Exception as e:
            logger.warning("unanswered check failed for %s: %s", agent_slug, e)
            return []
        rows = inbox.get("conversation") if isinstance(inbox, dict) else None
        return [(r["id"], r.get("sender")) for r in (rows or [])
                if isinstance(r, dict) and r.get("channel_type") == "internal"
                and r.get("direction") == "inbound" and r.get("status") == "new"
                and r.get("sender") != agent_slug
                and isinstance(r.get("id"), int) and r["id"] <= max_id]

    async def _answer_for_agent(self, ct, agent_slug, conversation_id, pending,
                                reason, body):
        """the turn ended without answering; answer each waiting message
        as the agent, through its own reply tool (so origin and relay work as for
        any reply), and write down that the node had to."""
        delivered = []
        for mid, _sender in pending:
            try:
                res = await ct.dispatch("messages_reply",
                                        {"to_message_id": mid, "body": body})
                if str(res).startswith("[HTTP 2"):
                    delivered.append(mid)
                else:
                    logger.warning("auto-reply to %s for %s refused: %s",
                                   mid, agent_slug, str(res)[:200])
            except Exception as e:
                logger.warning("auto-reply to %s for %s failed: %s", mid, agent_slug, e)
        logger.warning("turn %s of %s ended unanswered (%s); auto-replied to %s of %s",
                       conversation_id, agent_slug, reason, delivered,
                       [p[0] for p in pending])
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                await c.post(f"{CORE_API_URL}/api/events", headers=_service_headers(), json={
                    "actor": "component:brin", "event_type": "agent.turn_unanswered",
                    "entity_type": "agent", "entity_id": agent_slug,
                    "payload": {"conversation": conversation_id, "reason": reason,
                                "message_ids": [p[0] for p in pending],
                                "auto_replied": delivered}})
        except Exception as e:
            logger.warning("turn_unanswered event failed for %s: %s", agent_slug, e)

    def _claims_unbacked_send(self, answer, turn_tools):
        """True when the reply asserts a delegation/forward but no message tool
        was actually dispatched this turn.

        Both forms of sending count (_SEND_TOOLS). Checking only the CLI name
        made the guard fire on an HTTP agent that HAD sent with messages_send:
        on sn-4680 it told HTTP-model agent she had not sent, and she sent foreman the same
        question a second time in the same turn (messages 65 and 66)."""
        if any(t[0] in _SEND_TOOLS for t in turn_tools):
            return False
        a = (answer or "").lower()
        return any(h in a for h in _SEND_CLAIM_HINTS)

    async def _persist_tool_truth(self, conversation_id, turn_tools):
        """Record what tools were ACTUALLY dispatched this turn into the persisted
        conversation, as ground truth the model cannot fake. NONE when no tool ran."""
        if not conversation_id:
            return
        if turn_tools:
            parts = []
            for name, params, result in turn_tools:
                rs = str(result)[:140].replace(chr(10), " ")
                ps = json.dumps(params, ensure_ascii=False)[:90] if params else "{}"
                parts.append("%s(%s) -> %s" % (name, ps, rs))
            summary = "Tools you actually ran this turn: " + " ; ".join(parts)
        else:
            summary = "Tools you actually ran this turn: NONE. You called no tool, so claim no action you did not take."
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    "%s/api/v1/conversations/%s/note" % (GATEWAY_URL, conversation_id),
                    json={"role": "tool", "content": summary})
        except Exception as e:
            logger.warning("tool-truth persist failed for %s: %s", conversation_id, e)

    async def _persist_answer(self, conversation_id, text):
        """Write an answer brin composed itself into the persisted conversation.

        Only for answers the MODEL did not produce. A normal reply is already
        stored by the gateway from the provider completion, and writing it again
        here would show the person everything twice.
        """
        if not conversation_id or not (text or "").strip():
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    "%s/api/v1/conversations/%s/note" % (GATEWAY_URL, conversation_id),
                    json={"role": "assistant", "content": text})
        except Exception as e:
            logger.warning("answer persist failed for %s: %s", conversation_id, e)

    async def _agent_runtime(self, agent_slug: str) -> tuple:
        """The agent's model id and runtime kind ('cli' | 'http') from core-api.

        (None, None) on error: the caller then takes the normal gateway turn.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{CORE_API_URL}/api/agents/{agent_slug}",
                                     headers=_service_headers())
                if r.status_code == 200:
                    a = r.json()
                    return a.get("model"), a.get("runtime_kind")
        except Exception as e:
            logger.warning("agent model fetch failed for %s: %s", agent_slug, e)
        return None, None

    async def _assembled_prompt(self, agent_slug: str) -> Optional[str]:
        """Fetch the assembled system prompt (role + policies + tool contracts)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{CORE_API_URL}/api/agents/{agent_slug}/system-prompt",
                    headers=_service_headers(),
                )
                if r.status_code == 200:
                    return r.text
        except Exception as e:
            logger.warning("system-prompt fetch failed for %s: %s", agent_slug, e)
        return None

    async def _gateway_chat_stateless(self, model: str, system: str,
                                      messages: list, tools: list,
                                      session_id: str = None, agent: str = None,
                                      user: str = None,
                                      raw_user_content: str = None,
                                      continues_turn: bool = False) -> Optional[dict]:
        """Stateless gateway turn via /api/v1/chat with tools.

        `model` is the agent's <runtime>/<model> exactly as stored; the gateway
        picks the provider from the runtime. brin names no provider here:
        it used to pin openrouter, so an agent on any other HTTP runtime was sent
        to OpenRouter under a name OpenRouter does not have.

        raw_user_content, when provided, is forwarded so the gateway stores the
        raw user text (what was typed) rather than the enriched form in messages.

        Returns the raw response dict ({content, tool_calls, usage, ...}) or None.
        """
        body = {
            "service": "brin-tools",
            "model": model,
            "messages": messages,
            "max_tokens": 4096,
        }
        # Conversation attribution so the gateway groups + persists this turn
        # under the right session/agent (without it, sessions are anonymous:
        # agent_id NULL, ungrouped, unmonitorable).
        if session_id:
            body["session_id"] = session_id
        if agent:
            body["agent"] = agent
        if user:
            body["user"] = user
        if system:
            body["system"] = system
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if raw_user_content is not None:
            body["raw_user_content"] = raw_user_content
        if continues_turn:
            # The gateway stored the person's message with the first round.
            body["continues_turn"] = True
        try:
            async with httpx.AsyncClient(timeout=gateway_call_timeout()) as client:
                r = await client.post(f"{GATEWAY_URL}/api/v1/chat", json=body)
                if r.status_code != 200:
                    logger.error("stateless chat %d: %s", r.status_code, r.text[:300])
                    return None
                return r.json()
        except Exception as e:
            logger.error("stateless chat request failed: %s", e)
            return None

    async def _load_persisted_history(self, conversation_id: str) -> list:
        """This conversation user/assistant history from the gateway persisted
        store, so the tool loop has memory across restarts. [] on error."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    "%s/api/v1/conversations/%s/messages" % (GATEWAY_URL, conversation_id))
                if r.status_code == 200:
                    return r.json().get("messages", []) or []
        except Exception as e:
            logger.warning("history load failed for %s: %s", conversation_id, e)
        return []

    # --- COMMAND: extraction + execution ---

    def _extract_commands(self, response: str) -> list[str]:
        """Extract COMMAND: directives from response text."""
        pattern = r"COMMAND:\s*(.+?)(?:\n|$)"
        return re.findall(pattern, response, re.MULTILINE)

    async def _process_commands(self, response: str) -> str:
        """Extract, execute commands, and format response with results."""
        commands = self._extract_commands(response)
        if not commands:
            return response

        MAX_COMMANDS = 3
        if len(commands) > MAX_COMMANDS:
            logger.warning("Too many commands (%d), limiting to %d", len(commands), MAX_COMMANDS)
            commands = commands[:MAX_COMMANDS]

        logger.info("Executing %d command(s)", len(commands))

        match = re.search(r'COMMAND:', response)
        notification_text = response[:match.start()].strip() if match else ""

        command_results = []
        for cmd in commands:
            logger.info("Executing command: %s", cmd)
            result = await self._execute_command(cmd)
            command_results.append(result)
            logger.info("Command result: %s", result[:200])

        final_results = "\n\n".join(command_results)
        return f"{notification_text}\n\n[Results:]\n{final_results}"

    async def _execute_command(self, cmd: str) -> str:
        """Execute a bash command safely with timeout."""
        try:
            dangerous = ["rm -rf", "mkfs", "dd if=/dev/", "> /etc/", "> /root/"]
            if any(d in cmd for d in dangerous):
                return f"[BLOCKED] Command appears dangerous: {cmd}"

            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )

            output = result.stdout if result.returncode == 0 else result.stderr
            output = output.strip() if output else "[Command executed with no output]"

            MAX_OUTPUT = 3000
            if len(output) > MAX_OUTPUT:
                output = output[:MAX_OUTPUT] + f"\n\n[...truncated, total {len(output)} chars]"

            return output

        except subprocess.TimeoutExpired:
            return "[TIMEOUT] Command took too long to execute"
        except Exception as e:
            return f"[ERROR] Failed to execute command: {e}"

    async def close_session(self, specialist_id: str):
        """Close a Gateway session for a specialist."""
        if specialist_id not in self._sessions:
            return
        session_id = self._sessions.pop(specialist_id)
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.delete(f"{GATEWAY_URL}/api/v1/sessions/{session_id}")
            except httpx.RequestError:
                pass
