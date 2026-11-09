"""The gateway's own model calls pass the door: a provider client sends
through model_door.Transport, and every model call is kept live and recorded
in sn_core.model_calls with the caller the code named."""
import asyncio
import json
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import httpx  # noqa: E402
import pytest  # noqa: E402

import anthropic_relay  # noqa: E402
import model_door  # noqa: E402
import openai_relay  # noqa: E402
from models import Message  # noqa: E402
from providers.anthropic import AnthropicProvider  # noqa: E402
from providers.gemini import GeminiProvider  # noqa: E402
from providers.openrouter import OpenRouterProvider  # noqa: E402


@pytest.fixture
def door(monkeypatch):
    log = {"recorded": [], "live": []}
    monkeypatch.setattr(model_door, "record_call", log["recorded"].append)
    monkeypatch.setattr(model_door, "live_start", lambda rec: log["live"].append(("start", rec["id"])))
    monkeypatch.setattr(model_door, "live_touch", lambda rid, n: None)
    monkeypatch.setattr(model_door, "live_end", lambda rid: log["live"].append(("end", rid)))
    monkeypatch.setattr(model_door, "_decide", lambda agent, session_id: None)
    model_door.forget_policy()
    return log


def _wire(provider, fmt, handler):
    seen = []

    def remember(request):
        seen.append(request)
        return handler(request)
    provider._client = httpx.AsyncClient(transport=model_door.Transport(
        provider.door_name, fmt, httpx.MockTransport(remember)))
    return seen


def _run(coro):
    return asyncio.run(coro)


def test_every_provider_client_is_built_on_the_door():
    for provider in (OpenRouterProvider(api_key="k"), GeminiProvider(api_key="k"), AnthropicProvider(api_key="k")):
        client = _run(provider.get_client())
        assert isinstance(client._transport, model_door.Transport), provider
        assert client._transport.provider == provider.door_name


def test_an_openrouter_call_is_recorded_for_the_agent_that_made_it(door):
    p = OpenRouterProvider(api_key="provider-key")
    seen = _wire(p, openai_relay.FORMAT, lambda r: httpx.Response(200, json={
        "id": "x", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "read_mail", "arguments": "{}"}}]}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 12}}}))

    async def turn():
        with model_door.calling(agent="agent-b", session_id="s1", service="brin"):
            return await p.chat([Message(role="user", content="hi")], model="vendor/model")
    out = _run(turn())
    assert out["tool_calls"][0]["function"]["name"] == "read_mail"
    assert seen[0].headers["authorization"] == "Bearer provider-key"
    assert seen[0].headers["accept-encoding"] == "identity"
    rec = door["recorded"][0]
    assert (rec["auth"], rec["provider"], rec["agent"], rec["session_id"]) == ("internal", "openrouter", "agent-b", "s1")
    assert (rec["model"], rec["status"], rec["outcome"], rec["user_agent"]) == ("vendor/model", 200, "ok", "gateway:brin")
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read"]) == (30, 5, 12)
    assert rec["stop_reason"] == "tool_calls" and rec["tool_use"] == ["read_mail"]
    assert [k for k, _ in door["live"]] == ["start", "end"]


def test_a_streamed_call_is_recorded_when_the_stream_ends(door):
    p = OpenRouterProvider(api_key="k")
    sse = (b'data: {"choices":[{"delta":{"content":"po"}}]}\n\n'
           b'data: {"choices":[{"delta":{"content":"ng"},"finish_reason":"stop"}],'
           b'"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
    _wire(p, openai_relay.FORMAT, lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(sse)))

    async def turn():
        model_door.call_for("agent-b", "s2", "brin")
        return [c async for c in p.chat_stream([Message(role="user", content="hi")], model="m")]
    chunks = _run(turn())
    assert "".join(c["chunk"] for c in chunks) == "pong"
    rec = door["recorded"][0]
    assert (rec["agent"], rec["input_tokens"], rec["output_tokens"], rec["stop_reason"]) == ("agent-b", 4, 2, "stop")


def test_a_gemini_call_is_read_in_its_own_format(door):
    p = GeminiProvider(api_key="k")
    seen = _wire(p, p.door_format(), lambda r: httpx.Response(200, json={
        "candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": "ok"}, {"functionCall": {"name": "planner_list", "args": {}}}]}}],
        "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 3, "cachedContentTokenCount": 8}}))
    out = _run(p.chat([Message(role="user", content="hi")], model="gemini-2.5-flash"))
    assert out["content"] == "ok"
    assert seen[0].url.path.endswith("/models/gemini-2.5-flash:generateContent")
    rec = door["recorded"][0]
    assert (rec["provider"], rec["model"], rec["agent"]) == ("gemini", "gemini-2.5-flash", None)
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read"]) == (40, 3, 8)
    assert rec["stop_reason"] == "STOP" and rec["tool_use"] == ["planner_list"]


def test_an_anthropic_call_is_recorded(door):
    p = AnthropicProvider(api_key="k")
    _wire(p, anthropic_relay.FORMAT, lambda r: httpx.Response(200, json={
        "id": "m", "stop_reason": "end_turn", "content": [{"type": "text", "text": "hey"}],
        "usage": {"input_tokens": 7, "output_tokens": 1}}))
    assert _run(p.chat([Message(role="user", content="hi")], model="claude-haiku-4-5"))["content"] == "hey"
    rec = door["recorded"][0]
    assert (rec["provider"], rec["model"], rec["input_tokens"], rec["stop_reason"]) == (
        "anthropic", "claude-haiku-4-5", 7, "end_turn")


def test_an_upstream_error_is_recorded_and_raised_as_before(door):
    p = OpenRouterProvider(api_key="k")
    _wire(p, openai_relay.FORMAT, lambda r: httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(httpx.HTTPStatusError):
        _run(p.chat([Message(role="user", content="hi")], model="m"))
    rec = door["recorded"][0]
    assert (rec["status"], rec["outcome"]) == (429, "upstream_429")


def test_an_unreachable_upstream_is_recorded_as_an_error(door):
    p = OpenRouterProvider(api_key="k")

    def down(request):
        raise httpx.ConnectError("no route")
    _wire(p, openai_relay.FORMAT, down)
    with pytest.raises(httpx.ConnectError):
        _run(p.chat([Message(role="user", content="hi")], model="m"))
    assert door["recorded"][0]["outcome"] == "error" and door["recorded"][0]["status"] == 502
    assert [k for k, _ in door["live"]] == ["start", "end"]


def test_a_call_that_is_not_a_model_call_is_not_recorded(door):
    p = OpenRouterProvider(api_key="k")
    _wire(p, openai_relay.FORMAT, lambda r: httpx.Response(200, json={"data": []}))

    async def models():
        client = await p.get_client()
        return (await client.get("https://openrouter.ai/api/v1/models")).json()
    assert _run(models()) == {"data": []}
    assert door["recorded"] == [] and door["live"] == []


def test_calling_names_the_caller_only_inside_its_block(door):
    p = OpenRouterProvider(api_key="k")
    _wire(p, openai_relay.FORMAT, lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}))

    async def two():
        with model_door.calling(agent="agent-b", session_id="s9"):
            await p.chat([Message(role="user", content="a")], model="m")
        await p.chat([Message(role="user", content="b")], model="m")
    _run(two())
    assert [r["agent"] for r in door["recorded"]] == ["agent-b", None]
