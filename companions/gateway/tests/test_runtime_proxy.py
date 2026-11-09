"""The door forwards the runtime's traffic as it arrived and streams the
answer back; a runtime that is down is said to be down."""
import json
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import runtime_proxy  # noqa: E402


class Runtime:
    """A fake runtime that remembers what it was asked."""

    def __init__(self):
        self.requests = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        if request.url.path.endswith("/messages") and request.method == "POST":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=httpx.ByteStream(b"data: {\"a\":1}\n\ndata: {\"b\":2}\n\n"))
        return httpx.Response(201, json={"echo": request.url.path, "method": request.method,
                                         "q": request.url.query.decode(),
                                         "body": request.content.decode() or None,
                                         "forwarded_for": request.headers.get("x-forwarded-for")})


@pytest.fixture
def door(monkeypatch):
    rt = Runtime()
    monkeypatch.setattr(runtime_proxy, "_get_client", lambda: httpx.AsyncClient(
        base_url="http://runtime.test", transport=httpx.MockTransport(rt.handler)))
    app = FastAPI()

    @app.middleware("http")
    async def remember_ip(request: Request, call_next):
        request.state.client_ip = "10.9.8.7"
        return await call_next(request)

    @app.post("/api/v1/chat")
    async def chat(req: Request):
        return await runtime_proxy.forward(req, "/api/v1/chat")

    app.include_router(runtime_proxy.router)
    return TestClient(app), rt


def test_everything_under_sessions_goes_to_the_runtime_as_it_arrived(door):
    client, rt = door
    r = client.post("/api/v1/sessions/v2?x=1", json={"agent_slug": "agent-b"})
    assert r.status_code == 201
    body = r.json()
    assert body["echo"] == "/api/v1/sessions/v2" and body["method"] == "POST" and body["q"] == "x=1"
    assert json.loads(body["body"]) == {"agent_slug": "agent-b"}
    assert body["forwarded_for"] == "10.9.8.7"
    assert client.get("/api/v1/sessions").json()["echo"] == "/api/v1/sessions"
    assert client.delete("/api/v1/sessions/abc/v2").json()["method"] == "DELETE"
    assert client.get("/api/v1/sessions/desks-in-progress").json()["echo"] == "/api/v1/sessions/desks-in-progress"


def test_a_stream_is_passed_through_whole(door):
    client, rt = door
    r = client.post("/api/v1/sessions/s1/messages", json={"content": "hi"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == b"data: {\"a\":1}\n\ndata: {\"b\":2}\n\n"


def test_a_cli_chat_is_forwarded_to_the_runtime_chat(door):
    client, rt = door
    r = client.post("/api/v1/chat", json={"model": "claude-cli/sonnet", "messages": []})
    assert r.status_code == 201 and r.json()["echo"] == "/api/v1/chat"
    assert rt.requests[-1].headers.get("host") == "runtime.test"   # not the door's host header


def test_a_runtime_that_is_down_is_reported_as_503(door):
    client, rt = door
    rt.down = True
    r = client.post("/api/v1/sessions/v2", json={})
    assert r.status_code == 503
    assert "runtime unavailable" in r.json()["detail"]
