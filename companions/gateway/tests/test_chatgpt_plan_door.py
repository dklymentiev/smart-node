"""The door in the OpenAI format when the owner's ChatGPT plan is the upstream: Flint keeps speaking
Chat Completions, the call leaves as a Responses request with the plan's token, and the answer comes
back as chat chunks, recorded like any other model call."""
import asyncio
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


def sse(*events):
    return b"".join(b"event: " + n.encode() + b"\ndata: " + json.dumps(d).encode() + b"\n\n" for n, d in events)


TEXT = sse(("response.created", {"response": {}}),
           ("response.output_text.delta", {"delta": "Hel"}),
           ("response.output_text.delta", {"delta": "lo"}),
           ("response.completed", {"response": {"usage": {"input_tokens": 21, "output_tokens": 6,
                                                          "input_tokens_details": {"cached_tokens": 9}}}}))
TOOL = sse(("response.output_item.added", {"output_index": 0, "item": {"type": "function_call", "call_id": "c1", "name": "read_file"}}),
           ("response.function_call_arguments.delta", {"output_index": 0, "delta": "{}"}),
           ("response.completed", {"response": {"usage": {"input_tokens": 5, "output_tokens": 2}}}))


class Upstream:
    """A fake ChatGPT plan upstream that remembers what it was asked."""

    def __init__(self):
        self.requests = []
        self.answer = TEXT
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"slug": "gpt-a", "visibility": "list"}, {"slug": "x", "visibility": "hide"}]})
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"message": "usage limit", "type": "limit", "code": "subscription_sharing_usage_limit_exceeded"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(self.answer))


@pytest.fixture
def door(monkeypatch):
    up = Upstream()
    recorded = []
    monkeypatch.setattr(model_door, "_get_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(up.handler)))
    monkeypatch.setattr(model_door, "record_call", recorded.append)
    routes = {"flint-cli": relay.Route("flint-cli", relay.CHATGPT_BASE, "", chatgpt=True)}
    monkeypatch.setattr(relay, "_load_route", lambda provider: routes.get(provider))
    relay.forget_routes()

    async def token():
        return "PLAN-TOKEN"
    monkeypatch.setattr(relay, "plan_token", token)
    store = install(monkeypatch, model_tokens)
    monkeypatch.setattr(model_door, "_decide", lambda agent, session_id: None)
    model_door.forget_policy()
    app = FastAPI()
    app.include_router(relay.router)
    yield TestClient(app), up, recorded, monkeypatch


def _token():
    return model_tokens.issue("s1", "agent-b", "agent-b", "gpt-a", provider="flint-cli")


def _chat(client, stream=True, **extra):
    body = {"model": "gpt-a", "stream": stream, "temperature": 0.3, "messages": [
        {"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]}
    body.update(extra)
    return client.post("/openai/v1/chat/completions", headers={"authorization": "Bearer " + _token()}, content=json.dumps(body))


def _chunks(raw):
    out, done = [], False
    for part in raw.decode().split("\n\n"):
        if part.startswith("data: "):
            if part[6:] == "[DONE]":
                done = True
            else:
                out.append(json.loads(part[6:]))
    return out, done


def test_the_call_leaves_as_a_responses_request_with_the_plans_token(door):
    client, up, recorded, _ = door
    r = _chat(client)
    assert r.status_code == 200
    sent = up.requests[0]
    assert sent.url.host == "api.openai.com" and sent.url.path == "/v1/responses"
    assert sent.headers["authorization"] == "Bearer PLAN-TOKEN"
    body = json.loads(sent.content)
    assert body["store"] is False and body["stream"] is True and body["instructions"] == "Be brief."
    assert body["input"] == [{"role": "user", "content": "hi"}] and "temperature" not in body and "messages" not in body
    assert "gateway-issued" not in sent.headers.get("authorization", "")


def test_the_answer_comes_back_as_chat_chunks_and_is_recorded(door):
    client, up, recorded, _ = door
    r = _chat(client)
    assert r.headers["content-type"].startswith("text/event-stream")
    chunks, done = _chunks(r.content)
    assert done and "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices")) == "Hello"
    rec = recorded[0]
    assert (rec["auth"], rec["agent"], rec["provider"], rec["session_id"]) == ("token", "agent-b", "flint-cli", "s1")
    assert (rec["outcome"], rec["status"], rec["input_tokens"], rec["output_tokens"], rec["cache_read"]) == ("ok", 200, 21, 6, 9)
    assert rec["stop_reason"] == "stop"


def test_a_tool_call_is_recorded_by_name(door):
    client, up, recorded, _ = door
    up.answer = TOOL
    r = _chat(client)
    chunks, _ = _chunks(r.content)
    calls = [t for c in chunks if c.get("choices") for t in (c["choices"][0]["delta"].get("tool_calls") or [])]
    assert calls[0]["function"]["name"] == "read_file" and calls[0]["id"] == "c1"
    assert recorded[0]["stop_reason"] == "tool_calls" and recorded[0]["tool_use"] == ["read_file"]


def test_a_request_that_did_not_stream_gets_one_completion(door):
    client, up, recorded, _ = door
    r = _chat(client, stream=False)
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["choices"][0]["message"]["content"] == "Hello"
    assert json.loads(up.requests[0].content)["stream"] is True
    assert (recorded[0]["input_tokens"], recorded[0]["output_tokens"], recorded[0]["stop_reason"]) == (21, 6, "stop")


def test_an_upstream_limit_keeps_its_status_and_the_error_shape(door):
    client, up, recorded, _ = door
    up.status = 429
    r = _chat(client)
    assert r.status_code == 429 and r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]["code"] == "subscription_sharing_usage_limit_exceeded" and "usage limit" in r.json()["error"]["message"]
    assert recorded[0]["outcome"] == "upstream_429"


def test_the_model_list_is_translated_and_not_recorded(door):
    client, up, recorded, _ = door
    r = client.get("/openai/v1/models", headers={"authorization": "Bearer " + _token()})
    assert r.json() == {"object": "list", "data": [{"id": "gpt-a", "object": "model", "owned_by": "openai"}]}
    assert up.requests[0].headers["authorization"] == "Bearer PLAN-TOKEN" and recorded == []


def test_nothing_else_is_served_for_the_plan(door):
    client, up, recorded, _ = door
    r = client.get("/openai/v1/models/gpt-a", headers={"authorization": "Bearer " + _token()})
    assert r.status_code == 404 and up.requests == []


def test_a_plan_that_is_not_connected_is_refused_clearly_and_nothing_is_sent(door):
    client, up, recorded, monkeypatch = door

    async def none():
        raise model_door.Refused("no-credential", "the ChatGPT plan is not connected on this node; the owner connects it in the AI settings")
    monkeypatch.setattr(relay, "plan_token", none)
    r = _chat(client)
    assert r.status_code == 403 and "not connected" in r.json()["error"]["message"] and up.requests == []
    assert recorded[0]["outcome"] == "refused:no-credential"


def test_a_body_that_is_not_json_is_a_bad_request(door):
    client, up, recorded, _ = door
    r = client.post("/openai/v1/chat/completions", headers={"authorization": "Bearer " + _token()}, content=b"{nope")
    assert r.status_code == 400 and up.requests == []


def test_the_gateway_keeps_the_token_until_shortly_before_it_ends(monkeypatch):
    calls = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(relay.time, "time", lambda: clock["t"])

    def fetch():
        calls.append(1)
        return {"access_token": "T%d" % len(calls), "expires_at": 1000 + 3600}
    monkeypatch.setattr(relay, "_fetch_plan_token", fetch)
    relay.forget_plan_token()
    run = asyncio.run
    assert run(relay.plan_token()) == "T1" and run(relay.plan_token()) == "T1" and len(calls) == 1
    clock["t"] = 1000 + 3600 - 60          # inside the margin: ask core-api again
    assert run(relay.plan_token()) == "T2" and len(calls) == 2
    relay.forget_plan_token()
