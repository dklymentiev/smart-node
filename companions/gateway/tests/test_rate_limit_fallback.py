"""The Anthropic account's 429 is the node's limit: with a fallback model configured the call that
met it goes again through OpenRouter, the breaker stays open for the next ones, and without a model
nothing changes."""
import json
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import anthropic_relay as relay  # noqa: E402
import model_door  # noqa: E402
import model_tokens  # noqa: E402
import secrets_store  # noqa: E402
from fake_db import install  # noqa: E402

SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":2}}}\n\n'
       b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
       b'"usage":{"output_tokens":1}}\n\n')
LIMIT = {"type": "error", "error": {"type": "rate_limit_error", "message": "limit"}}
SESSION = "11111111-1111-1111-1111-111111111111"


class Upstreams:
    """Anthropic answers 429 while `limited`; OpenRouter answers a stream. Every request is kept."""

    def __init__(self):
        self.limited = True
        self.anthropic, self.openrouter = [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            self.openrouter.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=httpx.ByteStream(SSE))
        self.anthropic.append(request)
        if self.limited:
            return httpx.Response(429, headers={"retry-after": "120"}, json=LIMIT)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=httpx.ByteStream(SSE))


@pytest.fixture
def door(monkeypatch):
    up = Upstreams()
    recorded = []
    monkeypatch.setattr(model_door, "_get_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(up.handler)))
    monkeypatch.setattr(model_door, "record_call", recorded.append)
    routes = {"claude-cli": relay.Route("claude-cli", relay.DEFAULT_UPSTREAM,
                                        {"CLAUDE_CODE_OAUTH_TOKEN": "node-oauth-secret"})}
    monkeypatch.setattr(relay, "_load_route", lambda provider: routes[provider])
    relay.forget_routes()
    relay.reset_fallback()
    install(monkeypatch, model_tokens)
    monkeypatch.setattr(model_door, "_decide", lambda agent, session_id: None)
    model_door.forget_policy()
    monkeypatch.setattr(relay, "FALLBACK_PROVIDER", "openrouter")
    monkeypatch.setattr(relay, "FALLBACK_MODEL", "vendor/fallback-model")
    monkeypatch.setattr(secrets_store, "get_provider_key", lambda provider: "or-key-secret")
    app = FastAPI()
    app.include_router(relay.router)
    yield TestClient(app), up, recorded
    relay.reset_fallback()


def _call(client):
    token = model_tokens.issue(SESSION, "agent-b", "agent-b", "opus")
    return client.post("/anthropic/v1/messages",
                       headers={"authorization": "Bearer " + token,
                                "anthropic-beta": "oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14",
                                "x-claude-code-session-id": SESSION},
                       content=json.dumps({"model": "claude-opus-4-7", "stream": True, "messages": []}))


def test_a_limit_sends_the_call_again_through_openrouter_with_only_the_model_changed(door):
    client, up, recorded = door
    r = _call(client)
    assert r.status_code == 200 and r.content == SSE
    assert len(up.anthropic) == 1 and len(up.openrouter) == 1
    sent = up.openrouter[0]
    assert str(sent.url) == "https://openrouter.ai/api/v1/messages"
    assert json.loads(sent.content) == {"model": "vendor/fallback-model", "stream": True, "messages": []}
    assert sent.headers["x-api-key"] == "or-key-secret"
    assert "authorization" not in sent.headers
    assert sent.headers["anthropic-beta"] == "fine-grained-tool-streaming-2025-05-14"
    assert "node-oauth-secret" not in str(sent.headers)
    assert recorded[-1]["model"] == "vendor/fallback-model"
    assert recorded[-1]["provider"] == "openrouter"
    assert recorded[-1]["outcome"] == "ok"


def test_the_breaker_stays_open_so_the_next_call_does_not_ask_anthropic(door):
    client, up, _ = door
    _call(client)
    up.limited = False                      # Anthropic is back, but the cooldown has not run out
    r = _call(client)
    assert r.status_code == 200
    assert len(up.anthropic) == 1           # only the first call ever reached it
    assert len(up.openrouter) == 2


def test_the_cooldown_ends_and_anthropic_is_asked_again(door):
    client, up, _ = door
    _call(client)
    relay.reset_fallback()
    up.limited = False
    r = _call(client)
    assert r.status_code == 200
    assert len(up.anthropic) == 2 and len(up.openrouter) == 1


def test_without_a_fallback_model_the_limit_is_relayed_as_it_is(door, monkeypatch):
    client, up, recorded = door
    monkeypatch.setattr(relay, "FALLBACK_MODEL", "")
    r = _call(client)
    assert r.status_code == 429
    assert up.openrouter == []
    assert recorded[-1]["outcome"] == "upstream_429"


def test_a_missing_openrouter_key_ends_the_call_as_a_named_502(door, monkeypatch):
    client, up, recorded = door
    monkeypatch.setattr(secrets_store, "get_provider_key", lambda provider: None)
    r = _call(client)
    assert r.status_code == 502
    assert up.openrouter == []
    assert recorded[-1]["outcome"] == "fallback_error"


def test_an_open_breaker_with_no_key_is_a_503_not_a_call_to_anthropic(door, monkeypatch):
    client, up, recorded = door
    relay.open_fallback()
    monkeypatch.setattr(secrets_store, "get_provider_key", lambda provider: None)
    r = _call(client)
    assert r.status_code == 503
    assert up.anthropic == [] and up.openrouter == []
    assert recorded[-1]["outcome"] == "fallback_unavailable"
