"""The door's rules on every model call: a stopped agent and a spent budget
get no call, through any entrance, and the turn ends, not the session."""
import asyncio
import json
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import anthropic_relay  # noqa: E402
import model_door  # noqa: E402
import model_tokens  # noqa: E402
import openai_relay  # noqa: E402
from fake_db import install  # noqa: E402
from models import Message  # noqa: E402
from providers.openrouter import OpenRouterProvider  # noqa: E402

STOPPED = model_door.Refused("agent_stopped", "agent agent-b is stopped; enable it to continue")
SPENT = model_door.Refused("budget", "budget exhausted: agent=7 daily budget exhausted", 402)


@pytest.fixture
def door(monkeypatch):
    up, recorded, rules, asked = [], [], {"decision": None}, []

    def handler(request):
        up.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"usage": {}, "stop_reason": "end_turn", "content": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    def decide(agent, session_id):
        asked.append((agent, session_id))
        return rules["decision"]
    monkeypatch.setattr(model_door, "_get_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(model_door, "record_call", recorded.append)
    monkeypatch.setattr(model_door, "live_start", lambda rec: None)
    monkeypatch.setattr(model_door, "live_end", lambda rid: None)
    monkeypatch.setattr(model_door, "_decide", decide)
    model_door.forget_policy()
    monkeypatch.setattr(anthropic_relay, "_load_route", lambda p: anthropic_relay.Route(
        p, anthropic_relay.DEFAULT_UPSTREAM, {"CLAUDE_CODE_OAUTH_TOKEN": "node-oauth"}))
    anthropic_relay.forget_routes()
    monkeypatch.setattr(openai_relay, "_load_route", lambda p: openai_relay.Route(
        p, "https://openrouter.ai/api/v1", "provider-key"))
    openai_relay.forget_routes()
    install(monkeypatch, model_tokens)
    app = FastAPI()
    app.include_router(anthropic_relay.router)
    app.include_router(openai_relay.router)
    return {"client": TestClient(app), "up": up, "recorded": recorded, "rules": rules,
            "asked": asked, "handler": handler}


def _claude(d, session="s1"):
    token = model_tokens.issue(session, "agent-b", "agent-b", "opus")
    return d["client"].post("/anthropic/v1/messages",
                            headers={"authorization": "Bearer " + token, "x-claude-code-session-id": session},
                            content=json.dumps({"model": "claude-opus-4-7", "messages": []}))


def _flint(d, session="s2"):
    token = model_tokens.issue(session, "agent-b", "agent-b", "m", provider="flint-cli")
    return d["client"].post("/openai/v1/chat/completions", headers={"authorization": "Bearer " + token},
                            content=json.dumps({"model": "m", "messages": []}))


def test_a_stopped_agent_gets_no_call_through_either_entrance(door):
    door["rules"]["decision"] = STOPPED
    r = _claude(door)
    assert r.status_code == 403 and r.json()["error"]["type"] == "permission_error"
    assert "is stopped" in r.json()["error"]["message"]
    r = _flint(door)
    assert r.status_code == 403 and r.json()["error"]["type"] == "agent_stopped"
    assert door["up"] == []
    assert [x["outcome"] for x in door["recorded"]] == ["refused:agent_stopped"] * 2


def test_a_spent_budget_is_a_402(door):
    door["rules"]["decision"] = SPENT
    r = _claude(door)
    assert r.status_code == 402 and r.json()["error"]["type"] == "billing_error"
    assert _flint(door).status_code == 402
    assert door["up"] == []


def test_the_rules_are_asked_for_the_caller_the_token_names(door):
    _claude(door, session="sess-a")
    assert door["asked"] == [("agent-b", "sess-a")]
    assert len(door["up"]) == 1


def test_a_decision_holds_for_a_while_then_is_asked_again(door, monkeypatch):
    for _ in range(3):
        _claude(door)
    assert len(door["asked"]) == 1
    monkeypatch.setattr(model_door, "POLICY_TTL", 0.0)
    _claude(door)
    assert len(door["asked"]) == 2


def test_the_model_list_is_not_held_to_the_rules(door):
    door["rules"]["decision"] = STOPPED
    token = model_tokens.issue("s3", "agent-b", "agent-b", "m", provider="flint-cli")
    r = door["client"].get("/openai/v1/models", headers={"authorization": "Bearer " + token})
    assert r.status_code == 200 and door["asked"] == []


def test_rules_that_cannot_be_read_let_the_call_go(door, monkeypatch):
    def broken(agent, session_id):
        raise RuntimeError("database down")
    monkeypatch.setattr(model_door, "_decide", broken)
    assert _claude(door).status_code == 200


def test_the_gateways_own_call_is_held_to_the_same_rules(door):
    door["rules"]["decision"] = SPENT
    p = OpenRouterProvider(api_key="k")
    p._client = httpx.AsyncClient(transport=model_door.Transport(
        "openrouter", openai_relay.FORMAT, httpx.MockTransport(door["handler"])))

    async def turn():
        with model_door.calling(agent="agent-b", session_id="s4"):
            await p.chat([Message(role="user", content="hi")], model="m")
    with pytest.raises(model_door.Refused) as refused:
        asyncio.run(turn())
    assert refused.value.status == 402
    assert door["up"] == [] and door["asked"] == [("agent-b", "s4")]
    assert door["recorded"][0]["outcome"] == "refused:budget"


# ---------------------------------------------------------------------------
# _decide itself, against the rows it reads.
# ---------------------------------------------------------------------------
class Rows:
    def __init__(self, sessions=None, agents=None):
        self.sessions, self.agents, self.budget_args = sessions or {}, agents or {}, []

    def query(self, sql, args, **kw):
        if "FROM sn_core.ai_sessions" in sql:
            return [self.sessions[args[0]]] if args[0] in self.sessions else []
        if "WHERE id = %s" in sql:
            return [a for a in self.agents.values() if a["id"] == args[0]]
        if "WHERE slug = %s" in sql:
            return [self.agents[args[0]]] if args[0] in self.agents else []
        raise AssertionError(sql)


@pytest.fixture
def rows(monkeypatch):
    r = Rows(sessions={"11111111-2222-3333-4444-555555555555": {"tenant_id": 1, "agent_id": 7, "metadata": {"desk": "d1"}}},
             agents={"agent-b": {"id": 7, "slug": "agent-b", "tenant_id": 1, "enabled": True}})
    monkeypatch.setattr(model_door.db, "query", r.query)
    r.allowed = True

    def check_budget(**kw):
        r.budget_args.append(kw)
        return (r.allowed, None if r.allowed else "agent=7 daily budget exhausted", [])
    monkeypatch.setattr(model_door.budget, "check_budget", check_budget)
    return r


def test_decide_reads_the_session_and_its_agent(rows):
    assert model_door._decide("agent-b", "11111111-2222-3333-4444-555555555555") is None
    assert rows.budget_args == [{"tenant_id": 1, "agent_id": 7, "metadata": {"desk": "d1"}}]


def test_decide_stops_a_disabled_agent_before_any_budget(rows):
    rows.agents["agent-b"]["enabled"] = False
    refused = model_door._decide("agent-b", "11111111-2222-3333-4444-555555555555")
    assert refused.why == "agent_stopped" and refused.status == 403
    assert rows.budget_args == []


def test_decide_finds_the_agent_by_name_without_a_session_row(rows):
    # /api/v1/chat names a conversation id, not a gateway session.
    assert model_door._decide("agent-b", "brin-conversation-9") is None
    assert rows.budget_args[0]["agent_id"] == 7


def test_decide_refuses_a_spent_budget(rows):
    rows.allowed = False
    refused = model_door._decide("agent-b", "11111111-2222-3333-4444-555555555555")
    assert refused.why == "budget" and refused.status == 402
    assert "daily budget exhausted" in refused.message


def test_decide_without_a_caller_still_holds_the_node_budget(rows):
    assert model_door._decide(None, None) is None
    assert rows.budget_args == [{"tenant_id": 1, "agent_id": None, "metadata": {}}]


def test_the_gateways_own_callers_get_the_shapes_they_know():
    status, detail = STOPPED.http_error()
    assert status == 409 and detail["error"] == "agent_stopped"
    spent = model_door.Refused("budget", "budget exhausted: x", 402, detail={
        "reset_at": "2026-10-20T00:00:00", "limit": 5.0, "spent": 5.1, "budgets": []})
    status, detail = spent.http_error()
    assert status == 402 and detail["error"] == "budget_exhausted"
    assert (detail["reset_at"], detail["limit"], detail["spent"]) == ("2026-10-20T00:00:00", 5.0, 5.1)


def test_decide_names_the_spent_budget(rows, monkeypatch):
    item = {"over": True, "hard": True, "reset_at": "2026-10-20T00:00:00",
            "limit_usd": 5.0, "current_spend_usd": 5.2}
    monkeypatch.setattr(model_door.budget, "check_budget", lambda **kw: (False, "spent", [item]))
    refused = model_door._decide("agent-b", None)
    assert refused.detail["limit"] == 5.0 and refused.detail["spent"] == 5.2
