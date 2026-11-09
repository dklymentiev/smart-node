"""The door as the one door to the model: token for credential, model held to
the session's family, every call recorded, a bearer the gateway did not
issue refused."""
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
from fake_db import install  # noqa: E402


SSE = (
    b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":12,'
    b'"cache_read_input_tokens":7}}}\n\n'
    b'event: content_block_start\ndata: {"type":"content_block_start","content_block":'
    b'{"type":"tool_use","name":"Bash"}}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
    b'"usage":{"output_tokens":5}}\n\n'
)


class Upstream:
    """A fake api.anthropic.com that remembers what it was asked."""

    def __init__(self, status=200, sse=True):
        self.requests = []
        self.status = status
        self.sse = sse

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.sse:
            return httpx.Response(self.status, headers={"content-type": "text/event-stream"},
                                  stream=httpx.ByteStream(SSE))
        body = {"usage": {"input_tokens": 3, "output_tokens": 4}, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "pong"}]}
        return httpx.Response(self.status, json=body)


@pytest.fixture
def door(monkeypatch):
    up = Upstream()
    recorded = []
    monkeypatch.setattr(model_door, "_get_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(up.handler)))
    monkeypatch.setattr(model_door, "record_call", recorded.append)
    routes = {"claude-cli": relay.Route("claude-cli", relay.DEFAULT_UPSTREAM,
                                        {"CLAUDE_CODE_OAUTH_TOKEN": "node-oauth-secret"})}
    monkeypatch.setattr(relay, "_load_route", lambda provider: routes[provider])
    relay.forget_routes()
    store = install(monkeypatch, model_tokens)
    monkeypatch.setattr(model_door, "_decide", lambda agent, session_id: None)
    model_door.forget_policy()
    app = FastAPI()
    app.include_router(relay.router)
    client = TestClient(app)
    yield client, up, recorded, store


def _messages(client, token, model="claude-opus-4-7", session="11111111-1111-1111-1111-111111111111"):
    return client.post("/anthropic/v1/messages",
                       headers={"authorization": "Bearer " + token, "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
                                "x-claude-code-session-id": session},
                       content=json.dumps({"model": model, "stream": True, "messages": []}))


def test_token_is_swapped_for_the_node_credential_and_the_call_recorded(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("11111111-1111-1111-1111-111111111111", "agent-b", "agent-b", "opus")
    r = _messages(client, token)
    assert r.status_code == 200
    assert r.content == SSE
    sent = up.requests[0]
    assert sent.headers["authorization"] == "Bearer node-oauth-secret"
    assert "oauth-2025-04-20" in sent.headers["anthropic-beta"]
    assert "fine-grained-tool-streaming-2025-05-14" in sent.headers["anthropic-beta"]
    assert sent.url.host == relay.UPSTREAM_HOST and sent.url.path == "/v1/messages"
    rec = recorded[0]
    assert rec["auth"] == "token" and rec["agent"] == "agent-b" and rec["outcome"] == "ok"
    assert rec["session_id"] == "11111111-1111-1111-1111-111111111111"
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read"]) == (12, 5, 7)
    assert rec["stop_reason"] == "tool_use" and rec["tool_use"] == ["Bash"]


def test_a_bearer_this_gateway_did_not_issue_is_refused(door):
    client, up, recorded, store = door
    for bearer in ("some-other-credential", "sk-ant-a-real-key"):
        r = _messages(client, bearer)
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "authentication_error"
    assert up.requests == []
    assert [x["auth"] for x in recorded] == ["unknown", "unknown"]
    # A foreign bearer is not even looked up.
    assert store.statements == []


def test_the_session_is_held_to_its_model_family_plus_haiku(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s1", "agent-b", "agent-b", "sonnet")
    assert _messages(client, token, model="claude-haiku-4-5").status_code == 200
    assert _messages(client, token, model="claude-sonnet-4-6").status_code == 200
    refused = _messages(client, token, model="claude-opus-4-7")
    assert refused.status_code == 403
    assert refused.json()["error"]["type"] == "permission_error"
    assert len(up.requests) == 2
    assert [r["outcome"] for r in recorded][-1] == "refused:model"


def test_a_revoked_or_reissued_token_opens_nothing(door):
    client, up, recorded, store = door
    first = model_tokens.issue("s1", "agent-b", "agent-b", "opus")
    second = model_tokens.issue("s1", "agent-b", "agent-b", "opus")   # respawn of the same session
    model_tokens.revoke_session("s1")
    model_tokens.forget()                   # the door's cache would hold it for CACHE_SECONDS
    assert model_tokens.live_count() == 0
    assert _messages(client, first).status_code == 401
    assert _messages(client, second).status_code == 401
    assert up.requests == []


def test_a_door_restart_keeps_a_running_agents_token(door, monkeypatch):
    """The token lives in the database, not in the door's memory."""
    client, up, recorded, store = door
    token = model_tokens.issue("s9", "agent-b", "agent-b", "opus")
    model_tokens.forget()                   # a fresh door process knows nothing
    assert _messages(client, token).status_code == 200
    assert up.requests[0].headers["authorization"] == "Bearer node-oauth-secret"


def test_a_token_store_that_is_down_answers_503_not_passthru(door):
    client, up, recorded, store = door
    token = model_tokens.issue("s5", "agent-b", "agent-b", "opus")
    model_tokens.forget()
    store.fail = True
    r = _messages(client, token)
    assert r.status_code == 503 and not up.requests      # our token never reaches the provider
    assert r.json()["error"]["type"] == "api_error"
    assert recorded[-1]["outcome"] == "refused:token-store"


def test_without_a_node_credential_the_token_call_is_refused(door, monkeypatch):
    client, up, recorded, _ = door
    monkeypatch.setattr(relay, "_load_route", lambda provider: relay.Route(provider, relay.DEFAULT_UPSTREAM))
    relay.forget_routes()
    token = model_tokens.issue("s2", "agent-b", "agent-b", "opus")
    r = _messages(client, token)
    assert r.status_code == 403 and not up.requests
    assert recorded[-1]["outcome"] == "refused:no-credential"


def test_a_whole_response_is_tallied_too(door, monkeypatch):
    client, up, recorded, _ = door
    up.sse = False
    token = model_tokens.issue("s3", "agent-b", "agent-b", "opus")
    r = client.post("/anthropic/v1/messages", headers={"authorization": "Bearer " + token},
                    content=json.dumps({"model": "claude-opus-4-7", "messages": []}))
    assert r.status_code == 200 and r.json()["content"][0]["text"] == "pong"
    rec = recorded[-1]
    assert (rec["input_tokens"], rec["output_tokens"], rec["stop_reason"]) == (3, 4, "end_turn")
    assert rec["session_id"] == "s3"      # from the token when the CLI sends no header


def test_upstream_errors_are_relayed_and_recorded(door):
    client, up, recorded, _ = door
    up.status, up.sse = 529, False
    token = model_tokens.issue("s4", "agent-b", "agent-b", "opus")
    r = _messages(client, token)
    assert r.status_code == 529
    assert recorded[-1]["outcome"] == "upstream_529" and recorded[-1]["status"] == 529


def test_model_family():
    assert relay.model_family("claude-opus-4-7") == "opus"
    assert relay.model_family("claude-cli/sonnet") == "sonnet"
    assert relay.model_family("haiku") == "haiku"
    assert relay.model_family(None) == ""



# ---------------------------------------------------------------------------
# The route: where a provider's calls go, and with what. The token names the
# cli_providers row its process runs under; the row's env_vars decide.
# ---------------------------------------------------------------------------
def _routes(monkeypatch, **routes):
    table = {"claude-cli": relay.Route("claude-cli", relay.DEFAULT_UPSTREAM,
                                       {"CLAUDE_CODE_OAUTH_TOKEN": "node-oauth-secret"})}
    table.update(routes)
    monkeypatch.setattr(relay, "_load_route", lambda provider: table[provider])
    relay.forget_routes()


def test_a_provider_with_its_own_upstream_is_called_there_without_any_credential(door, monkeypatch):
    client, up, recorded, _ = door
    _routes(monkeypatch, **{"local-cli": relay.Route("local-cli", "http://127.0.0.1:11434")})
    token = model_tokens.issue("s6", "agent-b", "agent-b", "qwen2.5:0.5b", provider="local-cli")
    r = _messages(client, token, model="qwen2.5:0.5b")
    assert r.status_code == 200
    sent = up.requests[0]
    assert str(sent.url) == "http://127.0.0.1:11434/v1/messages"
    assert sent.headers["host"] == "127.0.0.1:11434"
    assert "authorization" not in sent.headers and "x-api-key" not in sent.headers
    assert "oauth-2025-04-20" not in sent.headers.get("anthropic-beta", "")
    assert recorded[0]["provider"] == "local-cli" and recorded[0]["outcome"] == "ok"


def test_the_nodes_own_processes_still_go_to_anthropic_with_its_credential(door):
    client, up, recorded, _ = door
    token = model_tokens.issue("s7", "agent-b", "agent-b", "opus")      # no provider: claude-cli
    assert _messages(client, token).status_code == 200
    assert up.requests[0].url.host == relay.UPSTREAM_HOST
    assert up.requests[0].headers["authorization"] == "Bearer node-oauth-secret"
    assert recorded[0]["provider"] == "claude-cli"


def test_a_provider_on_the_default_upstream_without_a_credential_is_refused(door, monkeypatch):
    client, up, recorded, _ = door
    _routes(monkeypatch, **{"second-cli": relay.Route("second-cli", relay.DEFAULT_UPSTREAM)})
    token = model_tokens.issue("s8", "agent-b", "agent-b", "opus", provider="second-cli")
    r = _messages(client, token)
    assert r.status_code == 403 and not up.requests
    assert recorded[-1]["outcome"] == "refused:no-credential"
    assert "second-cli" in r.json()["error"]["message"]


def test_a_provider_with_its_own_credential_uses_it(door, monkeypatch):
    client, up, recorded, _ = door
    _routes(monkeypatch, **{"second-cli": relay.Route("second-cli", relay.DEFAULT_UPSTREAM,
                                                      {"ANTHROPIC_API_KEY": "second-key"})})
    token = model_tokens.issue("s8", "agent-b", "agent-b", "opus", provider="second-cli")
    assert _messages(client, token).status_code == 200
    sent = up.requests[0]
    assert sent.headers["x-api-key"] == "second-key" and "authorization" not in sent.headers


def test_upstream_of_a_providers_base_url():
    assert relay.upstream_of(None) == relay.DEFAULT_UPSTREAM
    assert relay.upstream_of("") == relay.DEFAULT_UPSTREAM
    assert relay.upstream_of("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert relay.upstream_of("https://models.example/prefix") == "https://models.example/prefix"
    assert relay.upstream_of("not a url") == relay.DEFAULT_UPSTREAM
    # a node that pointed the CLI at the door by this variable before the relay did it itself
    assert relay.upstream_of(relay.DOOR_URL + "/anthropic") == relay.DEFAULT_UPSTREAM


def test_the_route_is_read_from_the_providers_row(monkeypatch):
    rows = {"local-cli": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:11434",
                          "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen2.5:0.5b"},
            "second-cli": {"ANTHROPIC_API_KEY": "secret:gateway/second-key"}}
    monkeypatch.setattr(relay.db, "query",
                        lambda sql, args, **kw: [{"env_vars": rows[args[0]]}] if args[0] in rows else [])
    monkeypatch.setattr(relay.secrets_store, "resolve_env",
                        lambda env: {k: ("second-key" if v == "secret:gateway/second-key" else v)
                                     for k, v in env.items()})
    for key in relay.CREDENTIAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    local = relay._load_route("local-cli")
    assert (local.base_url, local.credential, local.is_default) == ("http://127.0.0.1:11434", {}, False)
    second = relay._load_route("second-cli")
    assert second.credential == {"ANTHROPIC_API_KEY": "second-key"} and second.is_default
    unknown = relay._load_route("nobody-cli")
    assert unknown.is_default and unknown.credential == {}


def test_flint_on_anthropic_sends_its_token_as_x_api_key(door, monkeypatch):
    client, up, recorded, _ = door
    monkeypatch.setattr(relay, "_load_route", lambda provider: relay.Route(
        provider, relay.DEFAULT_UPSTREAM, {"ANTHROPIC_API_KEY": "node-api-key"}))
    relay.forget_routes()
    token = model_tokens.issue("s8", "agent-b", "agent-b", "claude-sonnet-4-6", provider="flint-cli")
    r = client.post("/anthropic/v1/messages", headers={"x-api-key": token},
                    content=json.dumps({"model": "claude-sonnet-4-6", "stream": True, "messages": []}))
    assert r.status_code == 200
    sent = up.requests[0]
    assert sent.headers["x-api-key"] == "node-api-key" and "authorization" not in sent.headers
    assert recorded[0]["auth"] == "token" and recorded[0]["provider"] == "flint-cli"
