"""The door to the model in the OpenAI format, for Flint and any agent that
speaks it.

Claude Code calls the Anthropic Messages API and passes the door at
/anthropic/*. Flint calls an OpenAI-compatible chat API (OpenRouter by
default), and until now it got the provider key into its own process and
called the provider itself. This module gives it the same door:

  * At spawn the runtime mints the process a token (model_tokens) and hands
    it to Flint in place of the provider key, with FLINT_BASE_URL pointing at
    /openai/v1 here. The provider key stays in the gateway.
  * /openai/v1/chat/completions forwards the call to the provider the token's
    cli_providers row names (FLINT_PROVIDER; its FLINT_BASE_URL, when set, is
    the upstream: another OpenAI-compatible service such as NVIDIA's),
    streaming kept, with the provider key in place of the token. The model list is forwarded too, since
    Flint reads prices and limits from it. Nothing else is: a path the agent
    needs and this door does not serve is a change to this list, not a
    passthrough.
  * Every chat call is recorded in sn_core.model_calls and kept in
    sn_core.model_calls_live while it runs, the same rows /anthropic/* writes.

Unlike /anthropic/*, a bearer this gateway did not issue is refused, not
passed through: no Flint process ever held the provider key under this door,
so there is no earlier process to keep working, and a provider key sent here
by mistake must not reach the provider from the node.

A row may name its own address (a router, a local model server). Such an address
may take no key: an absent key or the word "none" sends no credential at all, and
the caller's token is never sent. The provider's own default address always needs
its key.

Pauses and budgets are not decided here, as in anthropic_relay.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import chatgpt_responses
import db
import model_door
import secrets_store
from core_api_proof import service_headers
from config import DOOR_URL
from model_door import Refused

logger = logging.getLogger("gateway.openai_relay")
router = APIRouter(prefix="/openai/v1", tags=["openai-relay"])

# FLINT_PROVIDER -> (upstream base, the env_vars name of its key). Flint's own
# defaults, config/providers.json.
UPSTREAMS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}
CHAT_PATH = "chat/completions"
CHATGPT_BASE = "https://api.openai.com/v1"      # where a ChatGPT plan's sign-in is accepted
CHATGPT_UPSTREAM = "chatgpt"            # the provider row's SN_FLINT_UPSTREAM that sends calls there


def relay_base_url() -> str:
    """Where a Flint process must send its model calls: the door."""
    return DOOR_URL + "/openai/v1"


def _allowed(method: str, path: str) -> bool:
    if path == CHAT_PATH:
        return method == "POST"
    return method == "GET" and (path == "models" or path.startswith("models/"))


@dataclass
class Route:
    provider: str
    base_url: str
    key: str
    chatgpt: bool = False      # the owner's ChatGPT plan: no key, a token from core-api per call

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc


ROUTE_TTL = 60.0
_ROUTES: dict = {}                      # provider -> (Route or None, monotonic time)


def upstream_of(base_url: Optional[str], default: str) -> str:
    """The upstream a provider row's FLINT_BASE_URL names: where Flint itself
    would call without the gateway (another OpenAI-compatible service, NVIDIA
    or a local server). Unset, not http(s), or an address of this door, it is
    the provider's own default; the door must not send its calls to itself."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return default
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        logger.warning("FLINT_BASE_URL %r is not an http(s) URL; using %s", base, default)
        return default
    if parts.netloc == urlsplit(DOOR_URL).netloc:
        return default
    return base


def _load_route(provider: str) -> Optional[Route]:
    rows = db.query("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s",
                    (provider,), fetch=True, as_dict=True)
    env = secrets_store.resolve_env((rows[0].get("env_vars") or {}) if rows else {})
    if (env.get("SN_FLINT_UPSTREAM") or "") == CHATGPT_UPSTREAM:
        return Route(provider, CHATGPT_BASE, "", chatgpt=True)
    upstream = UPSTREAMS.get(env.get("FLINT_PROVIDER") or "openrouter")
    if not upstream:
        return None
    base = upstream_of(env.get("FLINT_BASE_URL"), upstream[0])
    key = env.get(upstream[1]) or ""
    if key.lower() == "none":
        key = ""
    if not key and base == upstream[0]:
        return None
    return Route(provider, base, key)


async def route_for(provider: str) -> Optional[Route]:
    hit = _ROUTES.get(provider)
    if hit and time.monotonic() - hit[1] < ROUTE_TTL:
        return hit[0]
    try:
        route = await asyncio.to_thread(_load_route, provider)
    except Exception as e:  # noqa: BLE001
        logger.error("route of provider %s unreadable: %s", provider, e)
        return hit[0] if hit else None
    _ROUTES[provider] = (route, time.monotonic())
    return route


def forget_routes() -> None:
    """Drop the route cache (tests, and after a provider row changes)."""
    _ROUTES.clear()


class Tally:
    """Usage, finish reason and tool names, read off the answer as it passes.
    OpenAI streams `data: {chunk}` lines ending with `data: [DONE]`; the usage
    comes in the last chunk (OpenRouter always sends it)."""

    def __init__(self):
        self.usage: dict = {}
        self.stop_reason: Optional[str] = None
        self.tools: list = []
        self.progress = 0
        self._buf = b""
        self._whole = b""

    def feed_sse(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
                try:
                    self.chunk(json.loads(line[5:].strip()))
                except ValueError:
                    pass

    def chunk(self, ev: dict) -> None:
        if ev.get("usage"):
            self.usage.update(ev["usage"])
        for choice in ev.get("choices") or []:
            self.progress += 1
            for call in (choice.get("delta") or {}).get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                if name:
                    self.tools.append(name)
            if choice.get("finish_reason"):
                self.stop_reason = choice["finish_reason"]
        if ev.get("error"):
            self.stop_reason = "error:" + str((ev["error"] or {}).get("code"))

    def feed_whole(self, chunk: bytes) -> None:
        if len(self._whole) < 4_000_000:
            self._whole += chunk

    def finish_whole(self) -> None:
        if not self._whole:
            return
        try:
            msg = json.loads(self._whole)
        except ValueError:
            return
        self.usage.update(msg.get("usage") or {})
        for choice in msg.get("choices") or []:
            self.stop_reason = choice.get("finish_reason") or self.stop_reason
            for call in (choice.get("message") or {}).get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                if name:
                    self.tools.append(name)

    def summary(self) -> dict:
        u = self.usage
        return {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens"),
                "cache_read": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                "cache_write": None, "stop_reason": self.stop_reason, "tool_use": self.tools}


def error_body(status: int, why: str, message: str) -> dict:
    """An error in the OpenAI format."""
    return {"error": {"message": message, "type": why, "code": status}}


_PLAN_TOKEN = {"value": "", "until": 0.0}
_PLAN_LOCK = asyncio.Lock()
PLAN_TOKEN_MARGIN = 90.0


def _fetch_plan_token() -> dict:
    import httpx
    r = httpx.post(os.environ.get("CORE_API_URL", "") + "/api/gateway/chatgpt/token", headers=service_headers(), timeout=45.0)
    if r.status_code == 404:
        raise Refused("no-credential", "the ChatGPT plan is not connected on this node; the owner connects it in the AI settings")
    if r.status_code == 401:
        raise Refused("chatgpt-signin", "the ChatGPT sign-in has ended; the owner has to sign in again in the AI settings")
    if r.status_code != 200:
        raise Refused("token-store", "the ChatGPT plan token could not be fetched (core-api answered %d), retry" % r.status_code, 503)
    return r.json()


async def plan_token() -> str:
    """The plan's access token. core-api owns it (it refreshes under one lock); the gateway keeps the
    one it was given until shortly before it ends."""
    if _PLAN_TOKEN["value"] and time.time() < _PLAN_TOKEN["until"] - PLAN_TOKEN_MARGIN:
        return _PLAN_TOKEN["value"]
    async with _PLAN_LOCK:
        if _PLAN_TOKEN["value"] and time.time() < _PLAN_TOKEN["until"] - PLAN_TOKEN_MARGIN:
            return _PLAN_TOKEN["value"]
        try:
            got = await asyncio.to_thread(_fetch_plan_token)
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("plan token unreachable: %s", e)
            raise Refused("token-store", "core-api is unreachable, retry", 503)
        _PLAN_TOKEN.update(value=got["access_token"], until=float(got["expires_at"]))
        return _PLAN_TOKEN["value"]


def forget_plan_token() -> None:
    _PLAN_TOKEN.update(value="", until=0.0)


async def _relay_plan(request: Request, path: str, body: bytes, rec: dict):
    """A call whose upstream is the owner's ChatGPT plan: Responses API, streamed, nothing stored.
    The caller keeps speaking Chat Completions; chatgpt_responses.Translator turns the answer back."""
    is_chat = path == CHAT_PATH
    if not is_chat and path != "models":
        return JSONResponse(status_code=404, content=error_body(
            404, "not_found", "gateway: /%s is not served for the ChatGPT plan" % path))
    token = await plan_token()
    headers = model_door.forward_headers(request)
    headers.pop("authorization", None)
    headers.pop("content-length", None)
    headers["authorization"] = "Bearer " + token
    headers["host"] = "api.openai.com"
    if not is_chat:
        return await model_door.forward(
            request, CHATGPT_BASE + "/models", headers, b"", rec, Tally(), error_body, metered=False,
            translate=lambda status: chatgpt_responses.Translator(status, "", False, kind="models"))
    try:
        chat = json.loads(body)
        sent, left_out = chatgpt_responses.to_responses(chat)
    except (ValueError, AttributeError):
        raise Refused("bad-request", "the request is not a chat completion body", 400)
    if left_out:
        logger.info("chatgpt plan: left out %s (session=%s)", ",".join(left_out), rec.get("session_id"))
    headers["content-type"] = "application/json"
    model, stream_out = chat.get("model") or "", bool(chat.get("stream"))
    return await model_door.forward(
        request, CHATGPT_BASE + "/responses", headers, json.dumps(sent).encode(), rec, Tally(), error_body, metered=True,
        translate=lambda status: chatgpt_responses.Translator(status, model, stream_out))


@router.api_route("/{path:path}", methods=["POST", "GET"])
async def relay(path: str, request: Request):
    if not _allowed(request.method, path):
        return JSONResponse(status_code=404, content=error_body(
            404, "not_found", "gateway: %s /%s is not served here" % (request.method, path)))
    body = await request.body()
    is_chat = path == CHAT_PATH
    rec = model_door.new_record(request)
    if body and is_chat:
        try:
            rec["model"] = json.loads(body).get("model")
        except (ValueError, AttributeError):
            pass
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        info = await model_door.caller_of(bearer)
        if info is None:
            rec["auth"] = "unknown"
            raise Refused("token", "this door takes only a token the gateway issued", 401)
        model_door.admit(rec, info)
        if is_chat:
            await model_door.check(rec)
        route = await route_for(info.get("provider") or "")
        if route is None:
            raise Refused("no-credential",
                          "provider %s has no upstream key configured" % info.get("provider"))
    except Refused as refused:
        return await model_door.refuse(rec, refused, error_body)
    if route.chatgpt:
        try:
            return await _relay_plan(request, path, body, rec)
        except Refused as refused:
            return await model_door.refuse(rec, refused, error_body)
    headers = model_door.forward_headers(request)
    headers.pop("authorization", None)
    if route.key:
        headers["authorization"] = "Bearer " + route.key
    headers["host"] = route.host
    return await model_door.forward(request, f"{route.base_url}/{path}", headers, body, rec,
                                    Tally(), error_body, metered=is_chat)


# The gateway's own calls in this format (providers/openrouter.py) pass the door too.
FORMAT = model_door.Format(
    "openai",
    is_model_call=lambda r: r.method == "POST" and r.url.path.endswith("/chat/completions"),
    model_of=model_door.body_model,
    tally=Tally,
)
