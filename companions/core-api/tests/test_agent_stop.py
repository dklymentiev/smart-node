"""Stopping an agent: disabled first, processes ended second, journalled
honestly. The database and the gateway are replaced; the route logic is real.

Runs without a database:
    python -m pytest tests/test_agent_stop.py --noconftest
"""
from __future__ import annotations

import os
import tempfile
import urllib.error
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))

from routes import agents  # noqa: E402


class Cursor:
    """Remembers every statement; answers the one SELECT the route makes."""

    def __init__(self, agent_row):
        self.agent_row = agent_row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.agent_row


@pytest.fixture
def node(monkeypatch):
    state = {"cursor": Cursor({"enabled": True}), "events": [], "gateway": []}

    @contextmanager
    def fake_cursor(*, commit=False):
        yield state["cursor"]

    def fake_gateway(method, path, body=None, timeout=10.0):
        state["gateway"].append((method, path, body))
        answer = state.get("answer")
        if isinstance(answer, Exception):
            raise answer
        return answer or (200, {"agent": body["agent"], "interrupted": ["s1"], "closed": ["s1", "s2"]})

    monkeypatch.setattr(agents, "cursor", fake_cursor)
    monkeypatch.setattr(agents, "_gateway", fake_gateway)
    monkeypatch.setattr(agents, "emit", lambda cur, **event: state["events"].append(event))
    return state


def _owner():
    return agents.Principal(username="alex", user_id=1, tenant_id=1, role="admin")


def test_stop_disables_the_agent_then_ends_its_processes_and_journals_it(node):
    out = agents.agent_stop("agent-b", agents.StopRequest(reason="runaway"), _owner())
    assert out == {"ok": True, "agent": "agent-b", "enabled": False, "was_enabled": True,
                   "interrupted": ["s1"], "closed": ["s1", "s2"], "gateway": "ok"}
    sqls = [s for s, _ in node["cursor"].executed]
    assert any(s.startswith("UPDATE sn_config.agents SET enabled=false") for s in sqls)
    assert node["gateway"] == [("POST", "/api/v1/sessions/agent-stop",
                                {"agent": "agent-b", "requested_by": "user:alex", "reason": "runaway"})]
    ev = node["events"][0]
    assert ev["event_type"] == "agent.stopped" and ev["actor"] == "user:alex" and ev["entity_id"] == "agent-b"
    assert ev["payload"]["closed"] == ["s1", "s2"] and ev["payload"]["reason"] == "runaway"


def test_a_gateway_that_does_not_answer_still_leaves_the_agent_disabled(node):
    node["answer"] = urllib.error.URLError("connection refused")
    out = agents.agent_stop("agent-b", None, _owner())
    assert out["ok"] is False and out["enabled"] is False
    assert out["gateway"].startswith("unreachable") and out["closed"] == []
    assert any(s.startswith("UPDATE sn_config.agents SET enabled=false") for s, _ in node["cursor"].executed)
    assert node["events"][0]["payload"]["gateway"].startswith("unreachable")


def test_a_gateway_error_status_is_reported_not_hidden(node):
    node["answer"] = (503, {"detail": "runtime unavailable"})
    out = agents.agent_stop("agent-b", None, _owner())
    assert out["ok"] is False and out["gateway"] == "http 503"


def test_an_unknown_agent_is_404_and_nothing_is_called(node):
    node["cursor"] = Cursor(None)
    with pytest.raises(HTTPException) as e:
        agents.agent_stop("ghost", None, _owner())
    assert e.value.status_code == 404
    assert node["gateway"] == [] and node["events"] == []


def test_the_process_table_is_the_runtimes_answer(node):
    node["answer"] = (200, {"processes": [{"session_id": "s1", "agent": "agent-b"}], "count": 1})
    assert agents.agents_processes("agent-b")["count"] == 1
    assert node["gateway"][-1] == ("GET", "/api/v1/sessions/processes?agent=agent-b", None)
    node["answer"] = urllib.error.URLError("connection refused")
    with pytest.raises(HTTPException) as e:
        agents.agents_processes(None)
    assert e.value.status_code == 503
