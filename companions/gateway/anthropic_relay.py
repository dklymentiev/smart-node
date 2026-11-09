"""The gateway as the one door to the model for CLI agents.

Claude Code in an agent's process talks to the Anthropic Messages API itself.
Until now that traffic left the node from the agent's process with the node's
own credential in its environment, and the gateway, the component declared as
the single door to a model, never saw it. This module closes that gap:

  * At spawn the gateway mints a per-process token and hands it to the
    credential wrapper over the stdin frame the wrapper already uses, with
    ANTHROPIC_BASE_URL pointing at this gateway. The node credential stays out
    of the agent's process; the token is worth nothing outside this node and
    dies with the process.
  * /anthropic/* forwards the call to the Anthropic API unchanged (streaming
    kept), swapping the token for the node credential on the way out, and
    holds the process to the model family its session was created with (plus
    haiku, which Claude Code uses for its own side calls).
  * Every call is recorded in sn_core.model_calls with the gateway session id
    Claude Code sends as x-claude-code-session-id, so "what is this agent
    doing, and is it doing anything" has one answer per agent and per call.

A bearer this gateway did not issue is refused (401). Forwarding it was for
the processes an earlier gateway had started with the node credential in
their environment; none of those is left.

Where a call goes is the provider's to say. The token names the cli_providers
row its process runs under (claude-cli unless the agent's model names another
row that runs the Claude Code binary, see backends.claude_cli_upstreams), and
that row's env_vars hold the upstream, ANTHROPIC_BASE_URL as the CLI itself
would read it, and the credential for it. Unset, the upstream is the Anthropic
API and the credential is required. Set to another Anthropic-compatible server,
a local model server on a test node or another account, the call goes there;
with no credential in the row nothing is sent as one, and our token never is.

Pauses and budgets are not decided here. The release's turn-level limits keep
doing that; this module is the transport and the record.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import db
import model_door
import model_tokens
import secrets_store
from config import DOOR_URL
from model_door import Refused

logger = logging.getLogger("gateway.anthropic_relay")
router = APIRouter(prefix="/anthropic", tags=["anthropic-relay"])

UPSTREAM_HOST = os.environ.get("ANTHROPIC_UPSTREAM_HOST", "api.anthropic.com")
DEFAULT_UPSTREAM = "https://" + UPSTREAM_HOST
CLI_PROVIDER = "claude-cli"
CREDENTIAL_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
OAUTH_BETA = "oauth-2025-04-20"
SIDE_FAMILIES = ("haiku",)
MESSAGES_PATH = "v1/messages"


# A 429 from the Anthropic account is the node's limit, not one agent's. When a fallback model is
# configured, the first 429 opens one breaker for the node and the call that met it is sent again
# through OpenRouter's Anthropic-format endpoint with only the model changed; Claude Code and its
# tool loop go on. Calls that arrive while the breaker is open go there directly. No model, no
# fallback. The model, provider and cooldown are node settings.
FALLBACK_PROVIDER = os.getenv("SN_ANTHROPIC_FALLBACK_PROVIDER", "openrouter").strip()
FALLBACK_MODEL = os.getenv("SN_ANTHROPIC_FALLBACK_MODEL", "").strip()
FALLBACK_COOLDOWN_SECONDS = max(60, int(os.getenv("SN_ANTHROPIC_FALLBACK_COOLDOWN_SECONDS", "18000")))
_FALLBACK_UNTIL = 0.0
_FALLBACK_LOCK = threading.Lock()


def fallback_active() -> bool:
    with _FALLBACK_LOCK:
        return bool(FALLBACK_MODEL and _FALLBACK_UNTIL > time.monotonic())


def open_fallback(retry_after: Optional[str] = None) -> int:
    """Open the node-wide breaker. Returns the cooldown in seconds."""
    global _FALLBACK_UNTIL
    duration = FALLBACK_COOLDOWN_SECONDS
    try:
        if retry_after:
            duration = max(60, int(float(retry_after)))
    except (TypeError, ValueError):
        pass
    with _FALLBACK_LOCK:
        _FALLBACK_UNTIL = max(_FALLBACK_UNTIL, time.monotonic() + duration)
    return duration


def reset_fallback() -> None:
    global _FALLBACK_UNTIL
    with _FALLBACK_LOCK:
        _FALLBACK_UNTIL = 0.0


def _fallback_target(url: str, headers: dict, body: bytes) -> tuple:
    """(url, headers, body) of the same call on the fallback: the model replaced, the OAuth
    credential and its beta flag left behind, the provider's key put in."""
    if FALLBACK_PROVIDER != "openrouter":
        raise RuntimeError("unsupported Anthropic fallback provider: " + FALLBACK_PROVIDER)
    key = secrets_store.get_provider_key(FALLBACK_PROVIDER)
    if not key:
        raise RuntimeError("the OpenRouter key is not in the secret store")
    payload = json.loads(body)
    payload["model"] = FALLBACK_MODEL
    out = dict(headers)
    out["host"] = "openrouter.ai"
    out.pop("authorization", None)
    out["x-api-key"] = key
    betas = [b.strip() for b in (out.get("anthropic-beta") or "").split(",")
             if b.strip() and b.strip() != OAUTH_BETA]
    if betas:
        out["anthropic-beta"] = ",".join(betas)
    else:
        out.pop("anthropic-beta", None)
    path = urlsplit(url).path
    return ("https://openrouter.ai/api" + path, out,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def relay_base_url() -> str:
    """Where a CLI process must send its model calls: the door."""
    return DOOR_URL + "/anthropic"


# ---------------------------------------------------------------------------
# The route of a provider: where its processes' model calls go and what they
# go with. cli_providers.env_vars, secret: refs resolved from the store:
# ANTHROPIC_BASE_URL is the upstream, as the CLI itself would read it (the
# CLI appends /v1/messages); CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY is
# the credential, exactly what the wrapper used to put into the agent's
# environment. Cached a minute per provider; the process environment is the
# fallback for a node that still configures the node's own key there.
# ---------------------------------------------------------------------------
ROUTE_TTL = 60.0


@dataclass
class Route:
    provider: str
    base_url: str                       # scheme://host[:port][/prefix], no trailing slash
    credential: dict = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    @property
    def is_default(self) -> bool:
        return self.base_url == DEFAULT_UPSTREAM


_ROUTES: dict = {}                      # provider -> (Route, monotonic time)


def upstream_of(base_url: Optional[str]) -> str:
    """The upstream a provider's ANTHROPIC_BASE_URL names. Empty is the
    Anthropic API. So is an address of this door: a node that routed the CLI
    through the door by that variable before the relay did it itself would
    otherwise send the door's calls back to the door."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return DEFAULT_UPSTREAM
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        logger.warning("ANTHROPIC_BASE_URL %r is not an http(s) URL; using %s", base, DEFAULT_UPSTREAM)
        return DEFAULT_UPSTREAM
    if parts.netloc == urlsplit(DOOR_URL).netloc:
        return DEFAULT_UPSTREAM
    return base


def _load_route(provider: str) -> Route:
    rows = db.query("SELECT env_vars FROM sn_config.cli_providers WHERE slug = %s",
                    (provider,), fetch=True, as_dict=True)
    env = secrets_store.resolve_env((rows[0].get("env_vars") or {}) if rows else {})
    cred = {k: env[k] for k in CREDENTIAL_KEYS if env.get(k)}
    if not cred and provider == CLI_PROVIDER:
        cred = {k: os.environ[k] for k in CREDENTIAL_KEYS if os.environ.get(k)}
    return Route(provider, upstream_of(env.get("ANTHROPIC_BASE_URL")), cred)


async def route_for(provider: Optional[str]) -> Route:
    provider = provider or CLI_PROVIDER
    hit = _ROUTES.get(provider)
    if hit and time.monotonic() - hit[1] < ROUTE_TTL:
        return hit[0]
    try:
        route = await asyncio.to_thread(_load_route, provider)
        _ROUTES[provider] = (route, time.monotonic())
    except Exception as e:  # noqa: BLE001
        logger.error("route of provider %s unreadable, keeping the last copy: %s", provider, e)
        if hit:
            _ROUTES[provider] = (hit[0], time.monotonic() - ROUTE_TTL + 10.0)
            return hit[0]
        route = Route(provider, DEFAULT_UPSTREAM)      # refused below: no credential
    return route


def forget_routes() -> None:
    """Drop the route cache (tests, and after a provider row changes)."""
    _ROUTES.clear()


def model_family(model: Optional[str]) -> str:
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return m


# ---------------------------------------------------------------------------
# The record: usage, stop reason and tool_use names, read off the response as
# it streams past, never kept whole.
# ---------------------------------------------------------------------------
class Tally:
    def __init__(self):
        self.usage: dict = {}
        self.stop_reason: Optional[str] = None
        self.tools: list = []
        self.progress = 0          # events that carry the answer forward (not ping)
        self._buf = b""
        self._whole = b""

    def feed_sse(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n\n" in self._buf:
            block, self._buf = self._buf.split(b"\n\n", 1)
            for line in block.split(b"\n"):
                if line.startswith(b"data:"):
                    try:
                        self.event(json.loads(line[5:].strip()))
                    except ValueError:
                        pass

    def feed_whole(self, chunk: bytes) -> None:
        if len(self._whole) < 4_000_000:
            self._whole += chunk

    def event(self, ev: dict) -> None:
        t = ev.get("type")
        if t and t != "ping":
            self.progress += 1
        if t == "message_start":
            self.usage.update((ev.get("message") or {}).get("usage") or {})
        elif t == "content_block_start":
            blk = ev.get("content_block") or {}
            if blk.get("type") in ("tool_use", "server_tool_use"):
                self.tools.append(blk.get("name"))
        elif t == "message_delta":
            self.usage.update(ev.get("usage") or {})
            self.stop_reason = (ev.get("delta") or {}).get("stop_reason") or self.stop_reason
        elif t == "error":
            self.stop_reason = "error:" + str((ev.get("error") or {}).get("type"))

    def finish_whole(self) -> None:
        if not self._whole:
            return
        try:
            msg = json.loads(self._whole)
        except ValueError:
            return
        self.usage.update(msg.get("usage") or {})
        self.stop_reason = msg.get("stop_reason")
        for blk in msg.get("content") or []:
            if blk.get("type") in ("tool_use", "server_tool_use"):
                self.tools.append(blk.get("name"))

    def summary(self) -> dict:
        u = self.usage
        return {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                "cache_read": u.get("cache_read_input_tokens"),
                "cache_write": u.get("cache_creation_input_tokens"),
                "stop_reason": self.stop_reason, "tool_use": self.tools}


async def authorize(headers: dict, rec: dict, path: str) -> Route:
    """Decide the mode and the route and, for a gateway token, swap the
    credential in. Raises Refused when the call may not go upstream."""
    auth = headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not bearer and (headers.get("x-api-key") or "").startswith(model_tokens.PREFIX):
        # Flint on an Anthropic provider sends its key as x-api-key, and its
        # key is our token (backends.cli_claude._relay_flint).
        bearer = headers.pop("x-api-key")
    # A store that is down is a 503 (Refused), not passthru: that would send
    # our token to the provider as a credential.
    info = await model_door.caller_of(bearer)
    if info is None:
        rec["auth"] = "unknown"
        raise Refused("token", "this door takes only a token the gateway issued", 401)
    model_door.admit(rec, info)
    rec["provider"] = rec.get("provider") or CLI_PROVIDER
    if info.get("model") and rec.get("model") and path.startswith(MESSAGES_PATH):
        assigned = model_family(info["model"])
        if model_family(rec["model"]) not in (assigned,) + SIDE_FAMILIES:
            raise Refused("model", "model %s is not allowed for this session (assigned %s)"
                          % (rec["model"], info["model"]))
    route = await route_for(rec["provider"])
    cred = route.credential
    if cred.get("CLAUDE_CODE_OAUTH_TOKEN"):
        headers["authorization"] = "Bearer " + cred["CLAUDE_CODE_OAUTH_TOKEN"]
        headers.pop("x-api-key", None)
        betas = [b.strip() for b in (headers.get("anthropic-beta") or "").split(",") if b.strip()]
        if OAUTH_BETA not in betas:
            betas.append(OAUTH_BETA)
        headers["anthropic-beta"] = ",".join(betas)
    elif cred.get("ANTHROPIC_API_KEY"):
        headers.pop("authorization", None)
        headers["x-api-key"] = cred["ANTHROPIC_API_KEY"]
    elif route.is_default:
        raise Refused("no-credential",
                      "the node has no Anthropic credential configured" if route.provider == CLI_PROVIDER
                      else "provider %s has no Anthropic credential configured" % route.provider)
    else:
        # An upstream of the provider's own that wants no credential (a local
        # model server). Nothing is sent as one, and our token least of all.
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
    return route


def error_body(status: int, why: str, message: str) -> dict:
    """An error in the Anthropic format, which Claude Code shows and retries by."""
    kind = {401: "authentication_error", 402: "billing_error",
            403: "permission_error"}.get(status, "api_error")
    return {"type": "error", "error": {"type": kind, "message": message}}


@router.api_route("/{path:path}", methods=["POST", "GET", "DELETE"])
async def relay(path: str, request: Request):
    body = await request.body()
    rec = model_door.new_record(request, request.headers.get("x-claude-code-session-id") or None)
    is_messages = path.startswith(MESSAGES_PATH)
    if body and is_messages:
        try:
            rec["model"] = json.loads(body).get("model")
        except (ValueError, AttributeError):
            pass
    headers = model_door.forward_headers(request)
    try:
        route = await authorize(headers, rec, path)
        if is_messages:
            await model_door.check(rec)
    except Refused as refused:
        return await model_door.refuse(rec, refused, error_body)
    headers["host"] = route.host
    url = f"{route.base_url}/{path}"
    # Only the node's own Claude, only a model call: a gateway-token call on the CLI provider.
    managed = (bool(FALLBACK_MODEL) and is_messages and rec.get("auth") == "token"
               and rec.get("provider") == CLI_PROVIDER)
    on_limit = None
    if managed and fallback_active():
        try:
            url, headers, body = _fallback_target(url, headers, body)
        except Exception as e:  # noqa: BLE001
            logger.error("the open fallback is unavailable (%s): %s", rec["id"], e)
            rec.update(outcome="fallback_unavailable", status=503)
            await model_door._finish(rec)
            return JSONResponse(status_code=503,
                                content=error_body(503, "fallback", "gateway: the fallback is unavailable"))
        rec["model"], rec["provider"] = FALLBACK_MODEL, FALLBACK_PROVIDER
        logger.warning("fallback open: request=%s agent=%s model=%s", rec["id"], rec.get("agent"),
                       FALLBACK_MODEL)
    elif managed:
        first = (url, dict(headers), body)

        async def on_limit(refusal_headers):
            cooldown = open_fallback(refusal_headers.get("retry-after"))
            target = _fallback_target(*first)
            rec["model"], rec["provider"] = FALLBACK_MODEL, FALLBACK_PROVIDER
            logger.warning("Anthropic limit: fallback opened for %ss; request=%s agent=%s model=%s",
                           cooldown, rec["id"], rec.get("agent"), FALLBACK_MODEL)
            return target
    return await model_door.forward(request, url, headers, body, rec, Tally(), error_body,
                                    metered=is_messages, on_limit=on_limit)


# The gateway's own Anthropic calls (providers/anthropic.py) pass the door too.
FORMAT = model_door.Format(
    "anthropic",
    is_model_call=lambda r: r.method == "POST" and r.url.path.endswith("/messages"),
    model_of=model_door.body_model,
    tally=Tally,
)
