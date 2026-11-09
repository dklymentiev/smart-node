"""The one door to the model: what every model call of an agent passes,
whatever API format the agent speaks.

An agent's process holds a token of the gateway, never a provider key. Each
format the agents speak has a thin entrance of its own (anthropic_relay for
Claude Code, openai_relay for Flint): it reads the model name off the request
in its format, knows where its provider's calls go and how its provider
reports usage. Everything else is here, once:

  * who calls: the token is looked up (model_tokens) and becomes the caller,
    with its agent, session and provider row;
  * the call itself: forwarded upstream with streaming kept, the answer
    passed back as it arrives;
  * the rules: an agent that is stopped, or whose budget is spent, gets no
    model call, checked on every call (check), not only when a turn starts;
  * the record: one row per call in sn_core.model_calls, and one row in
    sn_core.model_calls_live while the call runs.

An entrance never forwards, meters or records on its own; a new format is a
new entrance, not a second door.

The gateway's own model calls (/api/v1/chat, the HTTP session backends) pass
the same door from the inside: their providers' HTTP client sends through
`Transport`, which keeps each model call live and records it in the same rows,
with the caller the code that made the call names (`calling`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

import body_capture
import budget
import db
import model_tokens

logger = logging.getLogger("gateway.model_door")

_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
        "upgrade", "proxy-authorization", "proxy-authenticate", "content-length",
        "accept-encoding"}
_RESP_DROP = _HOP | {"content-encoding"}


class Refused(Exception):
    """The call may not go upstream: (why, message for the agent, status).
    `detail` is what the gateway's own callers are told (http_error)."""

    def __init__(self, why: str, message: str, status: int = 403, detail: Optional[dict] = None):
        super().__init__(message)
        self.why, self.message, self.status = why, message, status
        self.detail = detail or {}

    def http_error(self) -> tuple:
        """(status, detail) for the gateway's own HTTP callers, in the shapes a
        turn refused at its start already has (sessions v2): 409 agent_stopped
        and 402 budget_exhausted, which brin tells the person."""
        if self.why == "agent_stopped":
            return 409, {"error": "agent_stopped", "reason": self.message, **self.detail}
        if self.why == "budget":
            return 402, {"error": "budget_exhausted", "reason": self.message, **self.detail}
        return self.status, {"error": self.why, "reason": self.message}


# An entrance's error answer, in its own format: (status, why, message) -> body.
ErrorBody = Callable[[int, str, str], dict]


def new_record(request: Request, session_id: Optional[str] = None) -> dict:
    return {"id": uuid.uuid4().hex[:12],
            "user_agent": (request.headers.get("user-agent") or "")[:40],
            "session_id": session_id}


def forward_headers(request: Request) -> dict:
    """The request's headers without the hop-by-hop ones, names lowercased."""
    return {k.lower(): v for k, v in request.headers.items() if k.lower() not in _HOP}


async def caller_of(token: str) -> Optional[dict]:
    """The live token's record (session_id, agent, os_user, model, provider),
    or None for a bearer this gateway did not issue. A token store that cannot
    be read is a 503, which the agents retry: never a guess either way."""
    if not token or not token.startswith(model_tokens.PREFIX):
        return None
    try:
        return await asyncio.to_thread(model_tokens.lookup, token)
    except Exception as e:  # noqa: BLE001
        logger.error("token store unreachable: %s", e)
        raise Refused("token-store", "the token store is unreachable, retry", 503)


def admit(rec: dict, info: dict) -> None:
    """Put the caller the token names on the record."""
    rec["auth"] = "token"
    rec["agent"] = info.get("agent")
    rec["os_user"] = info.get("os_user")
    rec["provider"] = info.get("provider")
    if not rec.get("session_id"):
        rec["session_id"] = info.get("session_id")


# ---------------------------------------------------------------------------
# The rules, on every call. A turn is checked when it starts (sessions v2);
# a long turn is many calls, and the agent stopped or the budget spent in the
# middle of it must end it at the next call, not after it.
# ---------------------------------------------------------------------------
POLICY_TTL = 10.0                       # a decision holds this long per caller
_POLICY: dict = {}                      # (agent, session_id) -> (Refused or None, monotonic time)


def _is_uuid(value: Optional[str]) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _decide(agent: Optional[str], session_id: Optional[str]) -> Optional[Refused]:
    tenant, agent_id, metadata, enabled = 1, None, {}, True
    sess = None
    if _is_uuid(session_id):
        # A gateway session; /api/v1/chat names a conversation id instead.
        rows = db.query("SELECT tenant_id, agent_id, metadata FROM sn_core.ai_sessions "
                        "WHERE id = %s::uuid", (session_id,), fetch=True, as_dict=True)
        sess = rows[0] if rows else None
    if sess:
        tenant, agent_id, metadata = sess["tenant_id"] or 1, sess["agent_id"], sess["metadata"] or {}
        if agent_id:
            rows = db.query("SELECT slug, enabled FROM sn_config.agents WHERE id = %s",
                            (agent_id,), fetch=True, as_dict=True)
            if rows:
                agent, enabled = rows[0]["slug"], bool(rows[0]["enabled"])
    elif agent:
        rows = db.query("SELECT id, tenant_id, enabled FROM sn_config.agents WHERE slug = %s",
                        (agent,), fetch=True, as_dict=True)
        if rows:
            agent_id, tenant, enabled = rows[0]["id"], rows[0]["tenant_id"] or 1, bool(rows[0]["enabled"])
    if not enabled:
        return Refused("agent_stopped", "agent %s is stopped; enable it to continue" % agent,
                       detail={"agent": agent})
    allowed, reason, status = budget.check_budget(tenant_id=int(tenant), agent_id=agent_id,
                                                  metadata=metadata if isinstance(metadata, dict) else {})
    if not allowed:
        first = next((b for b in status or [] if b.get("over") and b.get("hard")), {})
        return Refused("budget", "budget exhausted: %s" % reason, 402, detail={
            "budgets": status, "reset_at": first.get("reset_at"),
            "limit": first.get("limit_usd"), "spent": first.get("current_spend_usd")})
    return None


async def check(rec: dict) -> None:
    """Raise Refused when the caller may make no model call now. A store that
    cannot be read is not the agent's fault: the call goes, and the log says
    why."""
    key = (rec.get("agent"), rec.get("session_id"))
    hit = _POLICY.get(key)
    if hit and time.monotonic() - hit[1] < POLICY_TTL:
        decision = hit[0]
    else:
        try:
            decision = await asyncio.to_thread(_decide, *key)
        except Exception as e:  # noqa: BLE001
            logger.warning("rules unreadable for agent=%s session=%s, call allowed: %s",
                           key[0], key[1], e)
            return
        if len(_POLICY) > 4096:
            _POLICY.clear()
        _POLICY[key] = (decision, time.monotonic())
    if decision is not None:
        raise decision


def forget_policy() -> None:
    """Drop the decisions (tests, and after a stop or a budget change)."""
    _POLICY.clear()


# ---------------------------------------------------------------------------
# The record. Never raises: a record that could not be written must not fail
# the call it describes.
# ---------------------------------------------------------------------------
def record_call(rec: dict) -> None:
    try:
        db.query(
            "INSERT INTO sn_core.model_calls (rid, session_id, agent, os_user, model, auth, outcome, "
            " status, input_tokens, output_tokens, cache_read, cache_write, stop_reason, tool_use, "
            " duration_ms, user_agent, provider) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (rec.get("id"), rec.get("session_id"), rec.get("agent"), rec.get("os_user"),
             rec.get("model"), rec.get("auth"), rec.get("outcome"), rec.get("status"),
             rec.get("input_tokens"), rec.get("output_tokens"), rec.get("cache_read"),
             rec.get("cache_write"), rec.get("stop_reason"), json.dumps(rec.get("tool_use") or []),
             rec.get("duration_ms"), rec.get("user_agent"), rec.get("provider")))
    except Exception as e:  # noqa: BLE001
        logger.error("model call %s not recorded: %s", rec.get("id"), e)


async def _finish(rec: dict) -> None:
    await asyncio.to_thread(record_call, rec)


# ---------------------------------------------------------------------------
# Calls in flight: sn_core.model_calls_live. Every write here is best effort
# and never raises; the call it describes must not fail because of it.
# ---------------------------------------------------------------------------
LIVE_TOUCH_SEC = 5.0
_LIVE_TASKS: set = set()


def _touch_later(rid: str, nbytes: int) -> None:
    """Stamp a byte without making the stream wait for the database. A touch
    that lands after the call's row is deleted updates nothing."""
    task = asyncio.ensure_future(asyncio.to_thread(live_touch, rid, nbytes))
    _LIVE_TASKS.add(task)
    task.add_done_callback(_LIVE_TASKS.discard)


def _live(sql: str, args: tuple) -> None:
    try:
        db.query(sql, args)
    except Exception as e:  # noqa: BLE001
        logger.warning("model_calls_live not written: %s", e)


def live_start(rec: dict) -> None:
    _live("INSERT INTO sn_core.model_calls_live (rid, session_id, agent, model, provider) "
          "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (rid) DO NOTHING",
          (rec.get("id"), rec.get("session_id"), rec.get("agent"), rec.get("model"),
           rec.get("provider")))


def live_touch(rid: str, nbytes: int) -> None:
    _live("UPDATE sn_core.model_calls_live SET last_event_at = now(), bytes = %s WHERE rid = %s",
          (nbytes, rid))


def live_end(rid: str) -> None:
    _live("DELETE FROM sn_core.model_calls_live WHERE rid = %s", (rid,))


def live_clear() -> None:
    """At door start: no call of a previous door is still in flight."""
    _live("DELETE FROM sn_core.model_calls_live", ())


# ---------------------------------------------------------------------------
# The call.
# ---------------------------------------------------------------------------
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


async def refuse(rec: dict, refused: Refused, error: ErrorBody) -> JSONResponse:
    rec["outcome"] = "refused:" + refused.why
    rec["status"] = refused.status
    logger.warning("refused %s session=%s agent=%s model=%s: %s", refused.why,
                   rec.get("session_id"), rec.get("agent"), rec.get("model"), refused.message)
    await _finish(rec)
    return JSONResponse(status_code=refused.status,
                        content=error(refused.status, refused.why, "gateway: " + refused.message))


async def forward(request: Request, url: str, headers: dict, body: bytes, rec: dict,
                  tally, error: ErrorBody, metered: bool = True, on_limit=None, translate=None):
    """Send the call upstream and pass the answer back as it arrives.

    `tally` is the entrance's reader of its provider's answer (usage, stop
    reason, tool names; see anthropic_relay.Tally). A metered call (a model
    call, not a model list) is kept live while it runs and recorded when it
    ends, whatever way it ends.

    `translate`, when the upstream speaks another format than the caller, is called with the upstream's
    status once it has answered and returns an object with feed(bytes) -> bytes, close() -> bytes, `streaming`
    and `media_type`; what the caller receives is what that object produces (chatgpt_responses.Translator).

    `on_limit`, when the entrance has somewhere else to send a call the provider refused with
    429, is awaited with the refusal's headers and returns the (url, headers, body) to send
    instead, once. It also puts the new model and provider on `rec`. A failure of it ends the
    call as a 502 with outcome fallback_error."""
    if request.url.query:
        url += "?" + request.url.query
    headers["accept-encoding"] = "identity"
    t0 = time.monotonic()
    client = _get_client()
    capture = body_capture.begin(rec, request.method, url, headers, body) if metered else None
    if metered:
        await asyncio.to_thread(live_start, rec)
    try:
        upstream = await client.send(
            client.build_request(request.method, url, headers=headers, content=body or None),
            stream=True)
    except Exception as e:  # noqa: BLE001
        logger.error("upstream unreachable (%s): %s", rec["id"], e)
        if metered:
            await asyncio.to_thread(live_end, rec["id"])
            rec.update(outcome="error", status=502, duration_ms=int((time.monotonic() - t0) * 1000))
            body_capture.note(capture, "finish", rec["outcome"], 502, rec["duration_ms"])
            await _finish(rec)
        return JSONResponse(status_code=502, content=error(502, "upstream", "gateway: " + str(e)[:200]))

    if on_limit is not None and upstream.status_code == 429:
        try:
            limited = await upstream.aread()
            url, headers, body = await on_limit(upstream.headers)
            await upstream.aclose()
            if request.url.query:
                url += "?" + request.url.query
            headers["accept-encoding"] = "identity"
            body_capture.note(capture, "fallback", url, body, 429, limited)
            upstream = await client.send(
                client.build_request(request.method, url, headers=headers, content=body or None),
                stream=True)
        except Exception as e:  # noqa: BLE001
            await upstream.aclose()
            logger.error("fallback after a provider limit failed (%s): %s", rec["id"], e)
            if metered:
                await asyncio.to_thread(live_end, rec["id"])
                rec.update(outcome="fallback_error", status=502,
                           duration_ms=int((time.monotonic() - t0) * 1000))
                body_capture.note(capture, "finish", rec["outcome"], 502, rec["duration_ms"])
                await _finish(rec)
            return JSONResponse(status_code=502, content=error(
                502, "fallback", "gateway: the provider limit was reached and the fallback failed"))

    rec["status"] = upstream.status_code
    body_capture.note(capture, "answer", upstream.status_code, upstream.headers)
    tr = translate(upstream.status_code) if translate is not None else None
    streaming = tr.streaming if tr is not None else "text/event-stream" in (upstream.headers.get("content-type") or "")
    dropped = _RESP_DROP | {"content-type", "content-length"} if tr is not None else _RESP_DROP
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in dropped}
    err = bytearray()

    async def upstream_chunks():
        # A response whose body arrived whole (a transport that does not stream)
        # is served the same way.
        if upstream.is_stream_consumed:
            yield upstream.content
            return
        async for chunk in upstream.aiter_raw():
            yield chunk

    async def chunks():
        if tr is None:
            async for chunk in upstream_chunks():
                yield chunk
            return
        async for chunk in upstream_chunks():
            piece = tr.feed(chunk)
            if piece:
                yield piece
        piece = tr.close()
        if piece:
            yield piece

    async def body_out():
        # Progress is stamped, not bytes: a stream that only pings is a model
        # that says nothing. The first event is stamped at once (it tells
        # "nothing yet" from "started and went quiet"), later ones at most
        # every LIVE_TOUCH_SEC. A whole (non-SSE) body is progress as it comes.
        nbytes, touched, seen = 0, None, 0
        try:
            async for chunk in chunks():
                nbytes += len(chunk)
                body_capture.note(capture, "chunk", chunk)
                if metered:
                    if streaming:
                        tally.feed_sse(chunk)
                    moved = tally.progress > seen if streaming else True
                    if moved and (touched is None or time.monotonic() - touched >= LIVE_TOUCH_SEC):
                        touched, seen = time.monotonic(), tally.progress
                        _touch_later(rec["id"], nbytes)
                    if not streaming and upstream.status_code >= 400:
                        if len(err) < 300:
                            err.extend(chunk[:300 - len(err)])
                    elif not streaming:
                        tally.feed_whole(chunk)
                yield chunk
            rec["outcome"] = "ok" if upstream.status_code < 400 else "upstream_%d" % upstream.status_code
        except Exception as e:  # noqa: BLE001
            rec["outcome"] = "error"
            logger.error("relay interrupted (%s): %s", rec["id"], e)
            raise
        finally:
            await upstream.aclose()
            if metered:
                rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
                await asyncio.to_thread(live_end, rec["id"])
                if not streaming:
                    tally.finish_whole()
                rec.update(tally.summary())
                if err:
                    logger.warning("upstream %s for %s session=%s: %s", upstream.status_code,
                                   rec["id"], rec.get("session_id"), bytes(err).decode(errors="replace"))
                body_capture.note(capture, "finish", rec.get("outcome", "error"), rec.get("status"),
                                  rec.get("duration_ms"))
                await _finish(rec)

    return StreamingResponse(body_out(), status_code=upstream.status_code, headers=out_headers,
                             media_type=tr.media_type if tr is not None else upstream.headers.get("content-type"))


# ---------------------------------------------------------------------------
# The gateway's own calls. A provider client (providers/*) sends through
# Transport; the code that starts a call says who it is for with calling().
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Format:
    """What the door needs to know of an API format to keep its record:
    which requests are model calls, the model a call names, and the reader
    of the answer (a Tally: feed_sse, feed_whole, finish_whole, summary,
    progress)."""
    name: str
    is_model_call: Callable[[httpx.Request], bool]
    model_of: Callable[[httpx.Request], Optional[str]]
    tally: Callable[[], object]


def body_model(request: httpx.Request) -> Optional[str]:
    """The model named in a JSON body, as the Anthropic and OpenAI formats do."""
    try:
        return json.loads(request.content or b"{}").get("model")
    except (ValueError, AttributeError, httpx.RequestNotRead):
        return None


_CALLER: ContextVar[dict] = ContextVar("model_door_caller", default={})


def call_for(agent: Optional[str] = None, session_id: Optional[str] = None,
             service: Optional[str] = None) -> None:
    """The gateway's model calls from here to the end of the current task (a
    request handler: each request runs in its own context) are for this
    agent and session."""
    _CALLER.set({"agent": agent, "session_id": session_id, "service": service})


@contextmanager
def calling(agent: Optional[str] = None, session_id: Optional[str] = None,
            service: Optional[str] = None):
    """The gateway's model calls inside this block are for this agent and
    session (and the calling service, kept as the record's user_agent)."""
    reset = _CALLER.set({"agent": agent, "session_id": session_id, "service": service})
    try:
        yield
    finally:
        _CALLER.reset(reset)


class _MeteredStream(httpx.AsyncByteStream):
    """The answer's bytes, read by the format's tally on their way to the
    provider client; the record is written once, when the client is done."""

    def __init__(self, inner, rec: dict, tally, streaming: bool, t0: float, capture=None):
        self._inner, self._rec, self._tally = inner, rec, tally
        self._streaming, self._t0, self._done = streaming, t0, False
        self._capture = capture

    async def __aiter__(self):
        nbytes, touched, seen = 0, None, 0
        try:
            async for chunk in self._inner:
                nbytes += len(chunk)
                body_capture.note(self._capture, "chunk", chunk)
                if self._streaming:
                    self._tally.feed_sse(chunk)
                else:
                    self._tally.feed_whole(chunk)
                moved = self._tally.progress > seen if self._streaming else True
                if moved and (touched is None or time.monotonic() - touched >= LIVE_TOUCH_SEC):
                    touched, seen = time.monotonic(), self._tally.progress
                    _touch_later(self._rec["id"], nbytes)
                yield chunk
            status = self._rec.get("status") or 500
            self._rec["outcome"] = "ok" if status < 400 else "upstream_%d" % status
        except Exception:
            self._rec["outcome"] = "error"
            raise

    async def aclose(self):
        try:
            await self._inner.aclose()
        finally:
            await self._end()

    async def _end(self):
        if self._done:
            return
        self._done = True
        rec = self._rec
        rec.setdefault("outcome", "error")       # closed before the answer was read whole
        rec["duration_ms"] = int((time.monotonic() - self._t0) * 1000)
        if not self._streaming:
            self._tally.finish_whole()
        rec.update(self._tally.summary())
        body_capture.note(self._capture, "finish", rec.get("outcome"), rec.get("status"),
                          rec.get("duration_ms"))
        await asyncio.to_thread(live_end, rec["id"])
        await _finish(rec)


class Transport(httpx.AsyncBaseTransport):
    """The HTTP transport of a provider client inside the gateway. A model
    call is kept live and recorded like an agent's; anything else (a model
    list, a health probe) goes out untouched."""

    def __init__(self, provider: str, fmt: Format, inner: Optional[httpx.AsyncBaseTransport] = None):
        self.provider, self.fmt = provider, fmt
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.fmt.is_model_call(request):
            return await self._inner.handle_async_request(request)
        who = _CALLER.get()
        rec = {"id": uuid.uuid4().hex[:12], "auth": "internal", "provider": self.provider,
               "agent": who.get("agent"), "session_id": who.get("session_id"),
               "user_agent": ("gateway:" + who["service"])[:40] if who.get("service") else "gateway",
               "model": self.fmt.model_of(request)}
        try:
            await check(rec)
        except Refused as refused:
            rec.update(outcome="refused:" + refused.why, status=refused.status)
            await _finish(rec)
            raise
        # The tally reads the answer as it comes, so it must come unencoded.
        request.headers["accept-encoding"] = "identity"
        t0 = time.monotonic()
        try:
            sent = request.content
        except httpx.RequestNotRead:
            sent = b""
        capture = body_capture.begin(rec, request.method, str(request.url), dict(request.headers), sent)
        await asyncio.to_thread(live_start, rec)
        try:
            response = await self._inner.handle_async_request(request)
        except Exception as e:
            await asyncio.to_thread(live_end, rec["id"])
            rec.update(outcome="error", status=502, duration_ms=int((time.monotonic() - t0) * 1000))
            body_capture.note(capture, "finish", rec["outcome"], 502, rec["duration_ms"])
            logger.error("upstream unreachable (%s): %s", rec["id"], e)
            await _finish(rec)
            raise
        rec["status"] = response.status_code
        body_capture.note(capture, "answer", response.status_code, response.headers)
        streaming = "text/event-stream" in (response.headers.get("content-type") or "")
        return httpx.Response(
            status_code=response.status_code, headers=response.headers,
            stream=_MeteredStream(response.stream, rec, self.fmt.tally(), streaming, t0, capture),
            extensions=response.extensions, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()
