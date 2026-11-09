"""Authorization properties for the secrets store.

The wire fixture proves the kernel/socket path.  These cases pin the policy
decisions cheaply so later route edits cannot reopen one door while fixing
another.
"""

import asyncio
import json
import sys
import types
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import routes.secrets as secrets

# core-api's runtime does not install the optional MCP SDK; the secrets server
# runs in an integration venv. Stub only its decorator surface so transport
# selection can still be unit-tested with core-api's normal test dependencies.
class _TestMcpServer:
    def __init__(self, name):
        self.name = name

    def tool(self):
        return lambda fn: fn


if "mcp.server" not in sys.modules:
    _mcp = types.ModuleType("mcp")
    _server = types.ModuleType("mcp.server")
    _server.MCPServer = _TestMcpServer
    _mcp.server = _server
    sys.modules["mcp"] = _mcp
    sys.modules["mcp.server"] = _server
import mcp_secrets


class Request:
    def __init__(self, host="127.0.0.1", headers=None):
        self.client = type("Client", (), {"host": host})() if host else None
        self.headers = headers or {}


@pytest.fixture
def denial_ledger(monkeypatch):
    events = []

    @contextmanager
    def fake_cursor(*, commit=False):
        yield object()

    monkeypatch.setattr(secrets, "cursor", fake_cursor)
    monkeypatch.setattr(
        secrets, "emit",
        lambda cur, **event: events.append(event),
    )
    return events


def socket(monkeypatch, *, slug=None, os_user="agent-alex", uid=1001):
    monkeypatch.setattr(secrets, "peer_credentials", lambda request: (9, uid, uid))
    monkeypatch.setattr(secrets, "peer_identity", lambda request: slug)
    monkeypatch.setattr(secrets, "_peer_os_user", lambda request: os_user)
    monkeypatch.setattr(secrets, "identity_strength", lambda request: "proved" if slug else "asserted")


def tcp(monkeypatch, *, strength="asserted", actor="anonymous"):
    monkeypatch.setattr(secrets, "peer_credentials", lambda request: None)
    monkeypatch.setattr(secrets, "peer_identity", lambda request: None)
    monkeypatch.setattr(secrets, "identity_strength", lambda request: strength)
    monkeypatch.setattr(secrets, "get_actor", lambda request: actor)


def test_non_privileged_agent_cannot_write(monkeypatch, denial_ledger):
    socket(monkeypatch, slug="alex")
    capability(monkeypatch, {"alex": "secretary"})
    with pytest.raises(HTTPException) as exc:
        secrets._operator_actor(Request(host=None), "set", "gitlab.token")
    assert exc.value.status_code == 403
    assert denial_ledger[0]["event_type"] == "secret.access_denied"
    assert denial_ledger[0]["entity_id"] == "gitlab.token"


def capability(monkeypatch, table):
    # raising=False: on a tree without _agent_capability the case must fail on
    # the decision it asserts, not on a missing attribute.
    monkeypatch.setattr(secrets, "_agent_capability",
                        lambda slug: table.get(slug), raising=False)


# the writers are recognised by capability. the slugs were renamed
# (sysadmin -> max, foreman -> john) and the slug list silently locked both out.
@pytest.mark.parametrize(
    ("slug", "cap"),
    [("max", "sysadmin"), ("john", "configurator"),
     ("sysadmin", "sysadmin"), ("foreman", "configurator")],
)
def test_owner_designated_secret_agents_can_write(monkeypatch, slug, cap):
    socket(monkeypatch, slug=slug, os_user=f"agent-{slug}")
    capability(monkeypatch, {slug: cap})
    assert secrets._operator_actor(Request(host=None), "set", "mailbox.owner") == slug


def test_writer_slug_without_writer_capability_is_refused(monkeypatch, denial_ledger):
    # A slug is a name, not a role: an agent called "sysadmin" that holds the
    # developer capability gets nothing from its name.
    socket(monkeypatch, slug="sysadmin", os_user="agent-sysadmin")
    capability(monkeypatch, {"sysadmin": "developer"})
    with pytest.raises(HTTPException) as exc:
        secrets._operator_actor(Request(host=None), "set", "gitlab.token")
    assert exc.value.status_code == 403
    assert denial_ledger[0]["event_type"] == "secret.access_denied"


def test_root_socket_peer_is_a_named_operator(monkeypatch):
    socket(monkeypatch, slug=None, os_user="root", uid=0)
    assert secrets._operator_actor(Request(host=None), "set", "gitlab.token") == "os:root"


def test_anonymous_loopback_is_not_an_operator(monkeypatch, denial_ledger):
    tcp(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        secrets._operator_actor(Request(), "resolve", "gitlab.token")
    assert exc.value.status_code == 403
    assert denial_ledger[0]["payload"]["operation"] == "resolve"


def test_authenticated_owner_session_is_operator(monkeypatch):
    tcp(monkeypatch, strength="session", actor="owner@example.com")
    assert secrets._operator_actor(Request(), "set", "gitlab.token") == "owner@example.com"


@pytest.mark.parametrize(
    ("ref", "rules", "allowed"),
    [
        ("integration-agent.google_ads.customer_id", ["integration-agent.google_ads.*"], True),
        ("integration-agent.google_ads", ["integration-agent.google_ads.*"], False),
        ("gitlab.token", ["gitlab.token"], True),
        ("github.token", ["gitlab.token"], False),
        ("gitlab.token", [], False),
        ("gitlab.token", ["*"], False),
    ],
)
def test_secret_ref_grants_are_exact_or_dotted_namespaces(ref, rules, allowed):
    assert secrets._secret_ref_allowed(ref, rules) is allowed


def test_agent_resolve_requires_both_tool_and_ref(monkeypatch, denial_ledger):
    socket(monkeypatch, slug="integration-agent")
    monkeypatch.setattr(secrets, "_trusted", lambda request: True)
    monkeypatch.setattr(secrets, "_agent_secret_grant",
                        lambda slug: ("r--", ["integration-agent.google_ads.*"]))
    monkeypatch.setattr(secrets, "_read_value",
                        lambda ref: "123" if ref == "integration-agent.google_ads.customer_id" else None)
    result = secrets.resolve_secret("integration-agent.google_ads.customer_id", Request(host=None))
    assert result["value"] == "123"

    with pytest.raises(HTTPException) as exc:
        secrets.resolve_secret("gitlab.token", Request(host=None))
    assert exc.value.status_code == 403
    assert denial_ledger[-1]["event_type"] == "secret.access_denied"


def test_secrets_mcp_prefers_socket_when_both_addresses_are_exported(monkeypatch):
    """Production exports a TCP URL too; it must never shadow SO_PEERCRED."""
    socket_transport = object()
    seen = {}

    class Response:
        status_code = 200
        content = b"yes"
        text = ""

        @staticmethod
        def json():
            return {"ref": "gitlab.token", "value": "not-printed"}

    class Client:
        def __init__(self, *, timeout, transport):
            seen["transport"] = transport

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, *, headers, params):
            seen["url"] = url
            return Response()

    monkeypatch.setattr(mcp_secrets.os.path, "exists", lambda path: True)
    monkeypatch.setattr(mcp_secrets.httpx, "AsyncHTTPTransport",
                        lambda *, uds: socket_transport)
    monkeypatch.setattr(mcp_secrets.httpx, "AsyncClient", Client)
    monkeypatch.setattr(mcp_secrets, "CORE_API_URL", "http://127.0.0.1:8030")

    result = json.loads(asyncio.run(
        mcp_secrets._call("GET", "/resolve", params={"ref": "gitlab.token"})))
    assert seen["transport"] is socket_transport
    assert seen["url"].startswith("http://localhost/")
    assert result["value"] == "not-printed"


def test_legacy_writer_env_warns_at_startup(monkeypatch, caplog):
    monkeypatch.setenv("SN_SECRETS_AGENT_WRITERS", "sysadmin,foreman")
    with caplog.at_level("WARNING", logger="core-api.secrets"):
        assert secrets.warn_legacy_writer_env() is True
    assert "SN_SECRETS_WRITER_CAPABILITIES" in caplog.text


def test_no_warning_without_legacy_writer_env(monkeypatch, caplog):
    monkeypatch.delenv("SN_SECRETS_AGENT_WRITERS", raising=False)
    with caplog.at_level("WARNING", logger="core-api.secrets"):
        assert secrets.warn_legacy_writer_env() is False
    assert caplog.text == ""
