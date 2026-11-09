"""The runtime: starts without a door, revokes what a predecessor minted,
answers its own health, and refuses what is not the local CLI's."""
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import model_tokens  # noqa: E402
import runtime_main  # noqa: E402
from fake_db import install  # noqa: E402


@pytest.fixture
def runtime(monkeypatch):
    store = install(monkeypatch, model_tokens)
    model_tokens.issue("left-over", "agent-x", "agent-x", "opus")   # a predecessor's token
    # The test client's address is "testclient", not loopback.
    monkeypatch.setattr(runtime_main, "ALLOWED_IPS", ["testclient", "127.0.0.1"])
    with TestClient(runtime_main.app) as client:
        yield client, store


def test_start_revokes_a_predecessors_tokens_and_health_answers(runtime):
    client, store = runtime
    assert store.live() == []
    assert store.rows[0]["revoke_reason"] == "runtime started"
    h = client.get("/api/v1/health").json()
    assert h["status"] in ("ok", "unavailable")
    assert h["active_sessions"] == 0 and "live_children" in h


def test_a_cloud_model_is_refused_not_guessed(runtime):
    client, _ = runtime
    r = client.post("/api/v1/chat", json={"service": "test", "model": "openrouter/openai/gpt-5",
                                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert "runs the local CLI only" in r.json()["detail"]


def test_a_non_loopback_caller_is_refused(runtime, monkeypatch):
    client, _ = runtime
    monkeypatch.setattr(runtime_main, "ALLOWED_IPS", ["10.0.0."])
    assert client.get("/api/v1/health").status_code == 403


def test_the_v2_session_routes_are_not_taken_for_a_v1_session_id(runtime):
    """Registration order: the v1 GET /api/v1/sessions/{session_id} must come
    after the v2 router, or "processes", "v2" and "agent-activity" are session
    ids and the runner, the panel and the process table all get 400."""
    client, _ = runtime
    for path in ("/api/v1/sessions/processes", "/api/v1/sessions/v2",
                 "/api/v1/sessions/agent-activity", "/api/v1/sessions/desks-in-progress"):
        r = client.get(path)
        assert "Invalid session ID format" not in r.text, path
    r = client.get("/api/v1/sessions/not-a-session-id")
    assert r.status_code == 400 and "Invalid session ID format" in r.text
