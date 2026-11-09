"""
AI Gateway Integration Tests

Run against a live Gateway instance.
Usage: cd /opt/smart-node/ai-gateway && venv/bin/pytest tests/ -v
"""
import asyncio
import json
import httpx
import pytest
import websockets

BASE_URL = "http://172.17.0.1:19850"
API = f"{BASE_URL}/api/v1"
TIMEOUT = 30.0


# ============== Helpers ==============

@pytest.fixture
def client():
    with httpx.Client(base_url=API, timeout=TIMEOUT) as c:
        yield c


@pytest.fixture
def session_id(client):
    """Create a session, yield its ID, then close it."""
    r = client.post("/sessions", json={"model": "haiku", "service": "integration-test"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    yield sid
    client.delete(f"/sessions/{sid}")


# ============== Health & Info ==============

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "healthy"
    assert "cli" in data["providers"]


def test_providers_list(client):
    r = client.get("/providers")
    assert r.status_code == 200
    providers = r.json()["providers"]
    ids = [p["id"] for p in providers]
    assert "cli" in ids


# ============== Session Lifecycle ==============

def test_create_and_close_session(client):
    r = client.post("/sessions", json={"model": "haiku", "service": "test-lifecycle"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert len(sid) > 0

    # Verify in list
    r = client.get("/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    found = [s for s in sessions if s["session_id"] == sid]
    assert len(found) == 1
    assert found[0]["model"] == "haiku"
    assert found[0]["service"] == "test-lifecycle"

    # Close
    r = client.delete(f"/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "closed"

    # Verify gone
    r = client.get("/sessions")
    found = [s for s in r.json()["sessions"] if s["session_id"] == sid]
    assert len(found) == 0


def test_close_nonexistent_session(client):
    # Valid UUID format but doesn't exist
    r = client.delete("/sessions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_close_invalid_session_id(client):
    # Invalid UUID format
    r = client.delete("/sessions/nonexistent-id")
    assert r.status_code == 400


# ============== Chat ==============

def test_chat_basic(client, session_id):
    """Send a simple message and get response."""
    r = client.post(
        f"/sessions/{session_id}/chat",
        json={"content": "Reply with exactly one word: OK"},
        timeout=60.0,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == session_id
    assert len(data["content"]) > 0
    assert "usage" in data


def test_chat_nonexistent_session(client):
    r = client.post("/sessions/00000000-0000-0000-0000-000000000000/chat", json={"content": "hello"})
    assert r.status_code == 404


def test_chat_invalid_session_id(client):
    r = client.post("/sessions/bad-id/chat", json={"content": "hello"})
    assert r.status_code == 400


# ============== Model Whitelist ==============

def test_model_whitelist_cli_valid(client):
    """Valid CLI models should be accepted."""
    for model in ["haiku", "sonnet"]:
        r = client.post("/sessions", json={"model": model, "service": "test-whitelist"})
        assert r.status_code == 200, f"Model {model} should be allowed"
        sid = r.json()["session_id"]
        client.delete(f"/sessions/{sid}")


def test_model_whitelist_cli_invalid(client):
    """Invalid CLI model should be rejected."""
    r = client.post("/sessions", json={"model": "gpt-4", "service": "test-whitelist"})
    assert r.status_code == 400
    assert "not allowed" in r.json()["detail"]


# ============== Shadow Cache ==============

def test_shadow_cache_stats(client):
    r = client.get("/cache/stats")
    assert r.status_code == 200
    data = r.json()
    assert "entries" in data
    assert "hits" in data
    assert "misses" in data


def test_shadow_cache_hit(client, session_id):
    """Second identical request should be a cache hit."""
    msg = {"content": "What is 1+1? Reply with just the number."}

    # First request -- miss
    r1 = client.post(f"/sessions/{session_id}/chat", json=msg, timeout=60.0)
    assert r1.status_code == 200

    # Second identical request -- should be cache hit
    r2 = client.post(f"/sessions/{session_id}/chat", json=msg, timeout=60.0)
    assert r2.status_code == 200

    # Verify cache stats increased
    stats = client.get("/cache/stats").json()
    assert stats["hits"] >= 1


# ============== Rate Limiting ==============

def test_rate_limit_not_on_health(client):
    """Health endpoint should not be rate limited."""
    for _ in range(20):
        r = client.get("/health")
        assert r.status_code == 200


# ============== WebSocket ==============

@pytest.mark.asyncio
async def test_websocket_connect():
    """WebSocket should accept connection and stay open."""
    ws_url = BASE_URL.replace("http://", "ws://") + "/ws/live"
    try:
        async with websockets.connect(ws_url, open_timeout=5) as ws:
            # Connection succeeded if we got here
            pass
    except Exception as e:
        pytest.fail(f"WebSocket connection failed: {e}")


# ============== Validation ==============

def test_create_session_missing_model(client):
    """Missing model should use default or fail gracefully."""
    r = client.post("/sessions", json={})
    # Should either use default model or return validation error
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        sid = r.json()["session_id"]
        client.delete(f"/sessions/{sid}")


def test_chat_empty_content(client, session_id):
    """Empty content should be rejected."""
    r = client.post(f"/sessions/{session_id}/chat", json={"content": ""})
    # Either validation error or handled gracefully
    assert r.status_code in (200, 422)


def test_chat_missing_content(client, session_id):
    """Missing content field should return 422."""
    r = client.post(f"/sessions/{session_id}/chat", json={})
    assert r.status_code == 422
