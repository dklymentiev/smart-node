"""The secrets MCP write tools call the right core-api routes and nothing else.

set_secret POSTs the ref and value to /api/secrets; grant_secret_ref reads the
target's grants, then PUTs a merge that adds the ref and the read-only
`secrets` tool without touching any other grant. Policy is not tested here on
purpose: who may write is decided by core-api (routes/secrets.py and the
agents.grants.write permission), and those tests live with the routes.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_secrets  # noqa: E402


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


def _client(monkeypatch, answers):
    """Install a fake httpx client. `answers` maps (METHOD, path) to a response;
    every call is recorded in `seen` in order."""
    seen = []

    class Client:
        def __init__(self, *, timeout, transport):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, *, headers, params, json=None):
            path = url.split("http://localhost", 1)[-1]
            seen.append({"method": method, "path": path, "params": params, "json": json})
            return answers[(method, path)]

    monkeypatch.setattr(mcp_secrets.os.path, "exists", lambda path: True)
    monkeypatch.setattr(mcp_secrets.httpx, "AsyncHTTPTransport", lambda *, uds: object())
    monkeypatch.setattr(mcp_secrets.httpx, "AsyncClient", Client)
    return seen


def test_set_secret_posts_ref_and_value(monkeypatch):
    seen = _client(monkeypatch, {
        ("POST", "/api/secrets"): _Response(201, {"ref": "google.gemini_api_key", "actor": "ops"}),
    })
    out = json.loads(asyncio.run(mcp_secrets.set_secret("google.gemini_api_key", "AIza-not-printed")))
    assert out["ref"] == "google.gemini_api_key"
    assert seen == [{"method": "POST", "path": "/api/secrets", "params": None,
                     "json": {"ref": "google.gemini_api_key", "value": "AIza-not-printed"}}]


def test_grant_secret_ref_merges_ref_and_read_tool(monkeypatch):
    seen = _client(monkeypatch, {
        ("GET", "/api/agents/dev/grants"): _Response(200, {"grants": {"doci": "rw-"}, "secret_refs": []}),
        ("PUT", "/api/agents/dev/grants"): _Response(200, {"ok": True, "secret_refs": ["google.gemini_api_key"]}),
    })
    out = json.loads(asyncio.run(mcp_secrets.grant_secret_ref("Dev ", "google.gemini_api_key")))
    assert out == {"ok": True, "agent": "dev", "ref": "google.gemini_api_key",
                   "secret_refs": ["google.gemini_api_key"]}
    put = seen[1]
    assert put["method"] == "PUT" and put["params"] == {"merge": "true"}
    # Only the secrets tool and the one ref travel: doci is left to the merge.
    assert put["json"] == {"grants": {"secrets": "r--"}, "secret_refs": ["google.gemini_api_key"]}


def test_grant_secret_ref_keeps_a_wider_secrets_mode(monkeypatch):
    seen = _client(monkeypatch, {
        ("GET", "/api/agents/foreman/grants"): _Response(200, {"grants": {"secrets": "rw-"}, "secret_refs": ["a.b"]}),
        ("PUT", "/api/agents/foreman/grants"): _Response(200, {"ok": True, "secret_refs": ["a.b", "c.*"]}),
    })
    asyncio.run(mcp_secrets.grant_secret_ref("foreman", "c.*"))
    assert seen[1]["json"]["grants"] == {"secrets": "rw-"}


def test_write_tools_report_the_api_refusal(monkeypatch):
    _client(monkeypatch, {
        ("POST", "/api/secrets"): _Response(403, {"detail": "agent is not an approved secrets writer"}),
        ("GET", "/api/agents/dev/grants"): _Response(200, {"grants": {}, "secret_refs": []}),
        ("PUT", "/api/agents/dev/grants"): _Response(403, {"detail": "forbidden"}),
    })
    denied = json.loads(asyncio.run(mcp_secrets.set_secret("x.y", "v")))
    assert denied["error"] == "HTTP 403" and "approved secrets writer" in denied["detail"]
    denied = json.loads(asyncio.run(mcp_secrets.grant_secret_ref("dev", "x.y")))
    assert denied["error"] == "HTTP 403"


def test_resolve_still_sends_no_body(monkeypatch):
    seen = _client(monkeypatch, {
        ("GET", "/api/secrets/resolve"): _Response(200, {"ref": "a.b", "value": "not-printed"}),
    })
    out = json.loads(asyncio.run(mcp_secrets.resolve_secret("a.b")))
    assert out["value"] == "not-printed"
    assert seen[0]["json"] is None and seen[0]["params"] == {"ref": "a.b"}
