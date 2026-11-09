"""The door in the OpenAI format: Flint's token for the provider key, every
chat call recorded, nothing served beyond chat and the model list, and no
bearer the gateway did not issue let through."""
import json
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import model_tokens  # noqa: E402
import model_door  # noqa: E402
import openai_relay as relay  # noqa: E402
from fake_db import install  # noqa: E402


SSE = (
    b'data: {"choices":[{"delta":{"role":"assistant","content":"hi"}}]}\n\n'
    b': OPENROUTER PROCESSING\n\n'
    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"read_file",'
    b'"arguments":""}}]}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
    b'"usage":{"prompt_tokens":21,"completion_tokens":6,"prompt_tokens_details":{"cached_tokens":9}}}\n\n'
    b'data: [DONE]\n\n'
)


class Upstream:
    """A fake OpenRouter that remembers what it was asked."""

    def __init__(self):
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fixture/model"}]})
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=httpx.ByteStream(SSE))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "pong"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4}})


@pytest.fixture
def door(monkeypatch):
    up = Upstream()
    recorded = []
    monkeypatch.setattr(model_door, "_get_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(up.handler)))
    monkeypatch.setattr(model_door, "record_call", recorded.append)
    routes = {"flint-cli": relay.Route("flint-cli", "https://openrouter.ai/api/v1",
                                       "provider-key-secret"),
              "no-key": None}
    monkeypatch.setattr(relay, "_load_route", lambda provider: routes.get(provider))
    relay.forget_routes()
    store = install(monkeypatch, model_tokens)
    monkeypatch.setattr(model_door, "_decide", lambda agent, session_id: None)
    model_door.forget_policy()
    app = FastAPI()
    app.include_router(relay.router)
    yield TestClient(app), up, recorded, store


def _chat(client, token, stream=True):
    return client.post("/openai/v1/chat/completions",
                       headers={"authorization": "Bearer " + token, "http-referer": "https://example.com"},
                       content=json.dumps({"model": "fixture/model", "stream": stream, "messages": []}))


def test_token_is_swapped_for_the_provider_key_and_the_call_recorded(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s1", "agent-b", "agent-b", "fixture/model", provider="flint-cli")
    r = _chat(client, token)
    assert r.status_code == 200 and r.content == SSE
    sent = up.requests[0]
    assert sent.headers["authorization"] == "Bearer provider-key-secret"
    assert sent.headers["http-referer"] == "https://example.com"
    assert sent.url.host == "openrouter.ai" and sent.url.path == "/api/v1/chat/completions"
    rec = recorded[0]
    assert (rec["auth"], rec["agent"], rec["provider"], rec["session_id"]) == ("token", "agent-b", "flint-cli", "s1")
    assert (rec["model"], rec["outcome"], rec["status"]) == ("fixture/model", "ok", 200)
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read"]) == (21, 6, 9)
    assert rec["stop_reason"] == "tool_calls" and rec["tool_use"] == ["read_file"]


def test_a_whole_answer_is_recorded_too(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s2", "agent-b", "agent-b", "fixture/model", provider="flint-cli")
    r = _chat(client, token, stream=False)
    assert r.json()["choices"][0]["message"]["content"] == "pong"
    rec = recorded[0]
    assert (rec["input_tokens"], rec["output_tokens"], rec["stop_reason"]) == (3, 4, "stop")


def test_the_model_list_is_forwarded_and_not_recorded(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s3", "agent-b", "agent-b", "fixture/model", provider="flint-cli")
    r = client.get("/openai/v1/models", headers={"authorization": "Bearer " + token})
    assert r.json()["data"][0]["id"] == "fixture/model"
    assert up.requests[0].headers["authorization"] == "Bearer provider-key-secret"
    assert recorded == []


def test_a_bearer_the_gateway_did_not_issue_never_reaches_the_provider(door):
    client, up, recorded, _ = door
    for bearer in ("sk-or-a-real-provider-key", "sn-revoked-or-made-up"):
        r = _chat(client, bearer)
        assert r.status_code == 401
        assert r.json()["error"]["message"].startswith("gateway:")
    assert up.requests == []
    assert [x["outcome"] for x in recorded] == ["refused:token", "refused:token"]


def test_a_revoked_token_opens_nothing(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s4", "agent-b", "agent-b", "fixture/model", provider="flint-cli")
    model_tokens.revoke_session("s4")
    model_tokens.forget()
    assert _chat(client, token).status_code == 401
    assert up.requests == []


def test_only_chat_and_the_model_list_are_served(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s5", "agent-b", "agent-b", "fixture/model", provider="flint-cli")
    auth = {"authorization": "Bearer " + token}
    assert client.get("/openai/v1/credits", headers=auth).status_code == 404
    assert client.get("/openai/v1/auth/key", headers=auth).status_code == 404
    assert client.post("/openai/v1/embeddings", headers=auth, content=b"{}").status_code == 404
    assert client.get("/openai/v1/chat/completions", headers=auth).status_code == 404
    assert up.requests == []


def test_a_provider_without_a_key_is_refused(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s6", "agent-b", "agent-b", "fixture/model", provider="no-key")
    r = _chat(client, token)
    assert r.status_code == 403 and up.requests == []
    assert recorded[0]["outcome"] == "refused:no-credential"


def test_a_token_store_that_is_down_answers_503(door):
    client, up, recorded, store = door
    token = model_tokens.issue("s7", "agent-b", "agent-b", "fixture/model", provider="flint-cli")
    model_tokens.forget()
    store.fail = True
    assert _chat(client, token).status_code == 503
    assert up.requests == []


def test_the_route_reads_the_provider_row(monkeypatch):
    rows = {"flint-cli": {"FLINT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "secret:gateway/k"},
            "flint-openai": {"FLINT_PROVIDER": "openai", "OPENAI_API_KEY": "secret:gateway/o"},
            "flint-anthropic": {"FLINT_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "secret:gateway/a"},
            "flint-nokey": {"FLINT_PROVIDER": "openrouter"}}
    monkeypatch.setattr(relay.db, "query",
                        lambda sql, args, **kw: [{"env_vars": rows[args[0]]}] if args[0] in rows else [])
    monkeypatch.setattr(relay.secrets_store, "resolve_env",
                        lambda env: {k: ("resolved-" + v[7:] if v.startswith("secret:") else v)
                                     for k, v in env.items()})
    r = relay._load_route("flint-cli")
    assert (r.base_url, r.key) == ("https://openrouter.ai/api/v1", "resolved-gateway/k")
    r = relay._load_route("flint-openai")
    assert (r.base_url, r.key) == ("https://api.openai.com/v1", "resolved-gateway/o")
    assert relay._load_route("flint-anthropic") is None      # that one goes to /anthropic
    assert relay._load_route("flint-nokey") is None
    assert relay._load_route("missing") is None


def test_the_row_can_name_another_openai_compatible_upstream(monkeypatch):
    rows = {"flint-nvidia": {"FLINT_PROVIDER": "openai", "OPENAI_API_KEY": "secret:gateway/nvidia-key",
                             "FLINT_BASE_URL": "https://integrate.api.nvidia.com/v1/"},
            "flint-loop": {"FLINT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "k",
                           "FLINT_BASE_URL": relay.relay_base_url()},
            "flint-bad": {"FLINT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "k",
                          "FLINT_BASE_URL": "file:///etc/passwd"}}
    monkeypatch.setattr(relay.db, "query", lambda sql, args, **kw: [{"env_vars": rows[args[0]]}])
    monkeypatch.setattr(relay.secrets_store, "resolve_env",
                        lambda env: {k: ("resolved-" + v[7:] if v.startswith("secret:") else v)
                                     for k, v in env.items()})
    r = relay._load_route("flint-nvidia")
    assert (r.base_url, r.key, r.host) == ("https://integrate.api.nvidia.com/v1",
                                           "resolved-gateway/nvidia-key", "integrate.api.nvidia.com")
    # the door's own address would send its calls back to itself
    assert relay._load_route("flint-loop").base_url == "https://openrouter.ai/api/v1"
    assert relay._load_route("flint-bad").base_url == "https://openrouter.ai/api/v1"


def test_a_call_goes_to_the_rows_upstream(door, monkeypatch):
    client, up, recorded, _ = door
    monkeypatch.setattr(relay, "_load_route", lambda provider: relay.Route(
        provider, "https://integrate.api.nvidia.com/v1", "nvapi-test-only"))
    relay.forget_routes()
    token = model_tokens.issue("s8", "agent-b", "agent-b", "nvidia/model", provider="flint-cli")
    assert _chat(client, token).status_code == 200
    sent = up.requests[0]
    assert str(sent.url) == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer nvapi-test-only"


def test_a_custom_address_may_take_no_key(monkeypatch):
    rows = {"own-none": {"FLINT_PROVIDER": "openai", "OPENAI_API_KEY": "none",
                         "FLINT_BASE_URL": "http://192.0.2.7:8080/v1"},
            "own-absent": {"FLINT_PROVIDER": "openai", "FLINT_BASE_URL": "http://192.0.2.7:8080/v1"},
            "own-key": {"FLINT_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "k",
                        "FLINT_BASE_URL": "https://router.example.com/api/v1"},
            "default-none": {"FLINT_PROVIDER": "openai", "OPENAI_API_KEY": "NONE"}}
    monkeypatch.setattr(relay.db, "query",
                        lambda _sql, args, **kw: [{"env_vars": rows[args[0]]}])
    monkeypatch.setattr(relay.secrets_store, "resolve_env", lambda env: dict(env))
    assert relay._load_route("own-none").key == ""
    assert relay._load_route("own-absent").base_url == "http://192.0.2.7:8080/v1"
    assert relay._load_route("own-absent").key == ""
    r = relay._load_route("own-key")
    assert (r.base_url, r.key) == ("https://router.example.com/api/v1", "k")
    assert relay._load_route("default-none") is None


def test_no_key_sends_no_credential_and_never_our_token(door, monkeypatch):
    client, up, _, _ = door
    monkeypatch.setattr(relay, "_load_route", lambda provider: relay.Route(
        provider, "http://192.0.2.7:8080/v1", ""))
    relay.forget_routes()
    token = model_tokens.issue("s9", "agent-b", "agent-b", "local/model", provider="flint-cli")
    assert _chat(client, token).status_code == 200
    sent = up.requests[0]
    assert str(sent.url) == "http://192.0.2.7:8080/v1/chat/completions"
    assert "authorization" not in sent.headers
