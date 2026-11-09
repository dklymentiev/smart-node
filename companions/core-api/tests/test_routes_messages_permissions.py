"""The permission chokepoint on routes/messages.py.

Asked of the real router, the real `requires()` and a database built from the
tree's bootstrap files:

1. Every handler carries the catalogue's permission.
2. A role that lacks the permission is refused before the handler, and the
   403 names both the role and the permission.
3. A role that holds it reaches the handler.
4. `messages.deliver_desk` has a desk object axis: the role grant alone is not
   enough, the caller must also be a participant of the desk the message is
   addressed to. The owner is admitted without one.
5. The mailbox verbs are decided on the role axis only; the handler keeps the
   scope it had.
6. The receiver callbacks and the routing lookup are held by no role: a node
   service that proves itself and is listed is admitted with no account, and
   everybody else is refused by name.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals

P = "/api/messages"

# (permission, passes an object)
DECLARED = {
    ("GET", P + "/inbox"): ("messages.inbox.read", False),
    ("GET", P + "/snapshot"): ("messages.inbox.read", False),
    ("POST", P + "/read_thread"): ("messages.inbox.write", False),
    ("POST", P + "/dismiss"): ("messages.inbox.write", False),
    ("PATCH", P + "/{message_id}/state"): ("messages.inbox.write", False),
    ("GET", P + "/{message_id}/chat-history"): ("messages.inbox.read", False),
    ("POST", P + "/{mid}/deliver-desk"): ("messages.deliver_desk", True),
    # The mailbox verbs: no object, the handler scopes them.
    ("GET", P): ("messages.read", False),
    ("GET", P + "/histogram"): ("messages.read", False),
    ("GET", P + "/by-contact"): ("messages.read", False),
    ("POST", P + "/search"): ("messages.read", False),
    ("POST", P + "/reply"): ("messages.send", False),
    ("POST", P + "/send"): ("messages.send", False),
    # Callbacks of the node's own services: admitted by trusted origin.
    ("GET", P + "/resolve-recipient"): ("internal.messages.routing", False),
    ("POST", P + "/inbound"): ("internal.messages.inbound", False),
    ("POST", P + "/record-voice"): ("internal.messages.voice.write", False),
    ("POST", P + "/voice-call-ended"): ("internal.messages.voice.write", False),
    ("POST", P + "/record-voice-turns"): ("internal.messages.voice.write", False),
    ("POST", P + "/record-webchat"): ("internal.messages.voice.write", False),
}
UNDECLARED: set = set()

SECRET = "s" * 64
PROXY_SECRET = "p" * 64
SERVICES = ("brin:internal.messages.inbound,internal.messages.routing,"
            "internal.messages.voice.write;voice:internal.messages.voice.write")


def test_messages_router_declares_catalogue_permissions():
    """Structural: each declared handler carries exactly the catalogue's name."""
    from routes import messages
    from permissions import route_permission

    seen = {}
    for route in messages.router.routes:
        for method in route.methods - {"HEAD"}:
            seen[(method, route.path)] = route_permission(route)
    assert set(seen) == set(DECLARED) | UNDECLARED, (
        "the router and the catalogue list different handlers: "
        f"{sorted(set(seen) ^ (set(DECLARED) | UNDECLARED))}"
    )
    declared = {k: v for k, v in seen.items() if v is not None}
    assert declared == DECLARED, (
        "handler-to-permission mapping diverged from the catalogue: "
        f"{sorted(set(declared.items()) ^ set(DECLARED.items()))}"
    )


@pytest.fixture(scope="module")
def node():
    """The real messages router over a throwaway database, with two desks and
    one message addressed to each. `admin` participates in the first desk only."""
    import auth
    from routes import messages

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        tenant = people["owner"].tenant_id
        desks = {"joined": str(uuid.uuid4()), "foreign": str(uuid.uuid4())}
        mids = {}
        for name, desk in desks.items():
            sql("INSERT INTO sn_core.desks (id, tenant_id, owner_user_id) VALUES "
                f"('{desk}', {tenant}, (SELECT min(id) FROM sn_config.users))")
            mids[name] = int(sql(
                "SET search_path TO sn_core, sn_config, public; "
                "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, "
                " thread_id, direction, sender, recipient, content, content_type, tenant_id) "
                f"VALUES ('internal', 'desk:{desk}', '<{name}@test>', '<{name}@test>', "
                f" 'inbound', 'system', 'desk:{desk}', 'hello', 'text/plain', {tenant}) "
                "RETURNING id"))
        mids["mailbox"] = int(sql(
            "SET search_path TO sn_core, sn_config, public; "
            "INSERT INTO sn_core.messages (channel_type, channel_id, external_id, "
            " thread_id, direction, sender, recipient, content, content_type, tenant_id) "
            "VALUES ('internal', 'permuser', '<m@test>', '<m@test>', 'inbound', "
            f" 'system', 'permuser', 'hello', 'text/plain', {tenant}) RETURNING id"))
        sql("INSERT INTO sn_core.desk_participants (desk_id, actor_id) VALUES "
            f"('{desks['joined']}', 'agent:permadmin')")

        app = FastAPI()
        app.include_router(messages.router)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name: str) -> TestClient:
            who["p"] = people[name]
            return client

        yield app, as_, desks, mids


def test_every_declared_name_is_in_the_seeded_catalogue(node):
    """The startup scan admits the router: every declared name exists, and the
    one with an object axis passes an object."""
    from permissions import validate_routes

    validate_routes(node[0], strict_undeclared=True)


def test_role_without_the_permission_is_refused_and_named(node):
    _, as_, _, mids = node
    # `user` holds no deliver right; the refusal is on the role, not the desk.
    r = as_("user").post(f"{P}/{mids['joined']}/deliver-desk")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail and "'messages.deliver_desk'" in detail, detail


def test_role_with_the_permission_reaches_the_handler(node):
    _, as_, _, _ = node
    # The inbox permissions are held by every role, the role-less actor included.
    r = as_("user").get(P + "/inbox")
    assert r.status_code == 200, r.text
    r = as_("user").get(P + "/snapshot")
    assert r.status_code == 200, r.text
    # Past the gate the handler's own ownership rule answers: not this caller's
    # message, so its 404.
    r = as_("admin").post(P + "/dismiss", json={"message_id": 999999})
    assert r.status_code == 404, r.text


def test_desk_object_allowed_with_the_grant(node):
    _, as_, _, mids = node
    r = as_("admin").post(f"{P}/{mids['joined']}/deliver-desk")
    assert r.status_code == 200, r.text
    assert r.json()["decision"] in ("deliver", "deny", "noop"), r.text


def test_desk_object_refused_without_the_grant(node):
    _, as_, desks, mids = node
    # Same role, same permission, a desk this admin is not a participant of.
    r = as_("admin").post(f"{P}/{mids['foreign']}/deliver-desk")
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'messages.deliver_desk'" in detail and "'desk'" in detail, detail
    assert desks["foreign"] in detail, detail
    # An admin who participates nowhere is refused on the first desk too.
    r = as_("other").post(f"{P}/{mids['joined']}/deliver-desk")
    assert r.status_code == 403 and "'desk'" in r.json()["detail"], r.text


def test_desk_object_is_not_asked_of_the_owner(node):
    _, as_, _, mids = node
    r = as_("owner").post(f"{P}/{mids['foreign']}/deliver-desk")
    assert r.status_code == 200, r.text


def test_deliver_desk_object_keeps_the_handler_answers(node):
    """The object is read from the message, so the two answers the handler
    gave for a message it cannot place are given before the check, unchanged."""
    _, as_, _, mids = node
    r = as_("admin").post(f"{P}/999999/deliver-desk")
    assert r.status_code == 404 and "message not found" in r.text, r.text
    r = as_("admin").post(f"{P}/{mids['mailbox']}/deliver-desk")
    assert r.status_code == 400 and "not a desk message" in r.text, r.text


# ---------------------------------------------------------------------------
# the mailbox verbs: role axis only
# ---------------------------------------------------------------------------

def test_mailbox_reads_are_decided_on_the_role_alone(node):
    _, as_, _, _ = node
    # No desk is named and none is asked for: the role holds messages.read.
    for name in ("user", "admin", "owner"):
        r = as_(name).get(P)
        assert r.status_code == 200, (name, r.text)
        assert as_(name).get(P + "/histogram").status_code == 200, name
    # Past the gate the handler's own validation answers.
    assert as_("user").get(P + "?limit=0").status_code == 400


def test_mailbox_verbs_refuse_a_role_without_the_permission_by_name(node):
    _, as_, _, _ = node
    r = as_("installer").get(P)
    assert r.status_code == 403, r.text
    assert "'installer'" in r.json()["detail"] and "'messages.read'" in r.json()["detail"]
    r = as_("installer").post(P + "/send", json={"to": "permuser", "body": "x"})
    assert r.status_code == 403, r.text
    assert "'installer'" in r.json()["detail"] and "'messages.send'" in r.json()["detail"]


def test_mailbox_send_reaches_the_handler_for_a_role_that_holds_it(node):
    _, as_, _, _ = node
    r = as_("user").post(P + "/send", json={"to": "permuser", "body": "  "})
    assert r.status_code == 400, r.text
    r = as_("user").post(P + "/reply", json={"to_message_id": 999999, "body": "  "})
    assert r.status_code == 400 and "empty" in r.text, r.text


# ---------------------------------------------------------------------------
# the callbacks: trusted origin
#
# Sent with real headers from loopback and with the identity stand-in removed,
# so the identity and the origin are both resolved by the real code.
# ---------------------------------------------------------------------------

def _send(app, method, path, headers=None, **kwargs):
    import httpx

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as c:
            return await c.request(method, path, headers=headers or {}, **kwargs)

    return asyncio.run(_go())


def _as_service(name, secret=SECRET):
    return {"X-SN-Service": name, "X-SN-Service-Auth": secret}


@pytest.fixture
def services(node, monkeypatch):
    """The same app with the identity stand-in removed: these calls are
    resolved by the real identity code from what the request carries."""
    import auth

    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", SERVICES)
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", PROXY_SECRET)
    app = node[0]
    stand_in = app.dependency_overrides.pop(auth.get_principal)
    yield app
    app.dependency_overrides[auth.get_principal] = stand_in


def test_routing_lookup_is_refused_without_a_trusted_origin(services):
    q = "?channel_type=internal&recipient=permuser"
    r = _send(services, "GET", P + "/resolve-recipient" + q)
    assert r.status_code == 403, r.text
    assert "'internal.messages.routing'" in r.json()["detail"]
    assert "no trusted origin" in r.json()["detail"]
    # A typed name is not an origin, whoever it names short of the owner.
    r = _send(services, "GET", P + "/resolve-recipient" + q, {"Remote-User": "runner"})
    assert r.status_code == 403, r.text
    r = _send(services, "GET", P + "/resolve-recipient" + q, _as_service("brin", "w" * 64))
    assert r.status_code == 403, r.text


def test_routing_lookup_is_answered_for_the_listed_service(services):
    r = _send(services, "GET", P + "/resolve-recipient?channel_type=internal&recipient=nobody",
              _as_service("brin"))
    assert r.status_code == 200, r.text
    assert r.json()["agent"] is None
    # voice proves itself too, and is not listed for routing.
    r = _send(services, "GET", P + "/resolve-recipient?channel_type=internal&recipient=nobody",
              _as_service("voice"))
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("path, body", [
    ("/record-voice", {"identity": "", "turns": []}),
    ("/record-voice-turns", {"identity": "", "session_id": "", "turns": []}),
    ("/record-webchat", {"identity": "", "turns": []}),
    ("/voice-call-ended", {"call": ""}),
])
def test_voice_and_webchat_callbacks_are_trusted_origin_only(services, path, body):
    r = _send(services, "POST", P + path, json=body)
    assert r.status_code == 403, r.text
    assert "'internal.messages.voice.write'" in r.json()["detail"]
    for service in ("voice", "brin"):
        r = _send(services, "POST", P + path, _as_service(service), json=body)
        # Past the gate: the handler's own answer for an empty report.
        assert r.status_code in (200, 400), (service, r.text)
        assert "internal.messages.voice.write" not in r.text


def test_inbound_from_a_service_lands_in_the_recipient_agents_tenant(services, node):
    import db

    with db.cursor(commit=True) as cur:
        cur.execute("INSERT INTO sn_config.tenants (slug, name) VALUES ('second', 'second') RETURNING id")
        tenant = cur.fetchone()["id"]
        cur.execute("INSERT INTO sn_config.agents (tenant_id, slug, title, capability) "
                    "VALUES (%s, 'inboundagent', 'Inbound', '')", (tenant,))
    body = {"recipient": "inboundagent", "content": "you have work"}

    r = _send(services, "POST", P + "/inbound", json=body)
    assert r.status_code == 403 and "'internal.messages.inbound'" in r.text, r.text
    r = _send(services, "POST", P + "/inbound", _as_service("voice"), json=body)
    assert r.status_code == 403, r.text

    r = _send(services, "POST", P + "/inbound", _as_service("brin"), json=body)
    assert r.status_code == 200, r.text
    with db.cursor() as cur:
        cur.execute("SELECT tenant_id FROM sn_core.messages WHERE id = %s", (r.json()["id"],))
        assert cur.fetchone()["tenant_id"] == tenant

    # No account and no agent to file it under: refused, not filed under a guess.
    r = _send(services, "POST", P + "/inbound", _as_service("brin"),
              json={"recipient": "nobody-here", "content": "x"})
    assert r.status_code == 400 and "not an agent" in r.text, r.text


def test_inbound_from_the_owner_keeps_the_owners_tenant(services):
    import db

    # The owner signed in through the proxy: the account, by its role.
    r = _send(services, "POST", P + "/inbound",
              {"Remote-User": "admin", "X-SN-Proxy-Auth": PROXY_SECRET},
              json={"recipient": "nobody-here", "content": "x"})
    assert r.status_code == 200, r.text
    with db.cursor() as cur:
        cur.execute("SELECT m.tenant_id, u.tenant_id AS owner_tenant FROM sn_core.messages m, "
                    "sn_config.users u WHERE m.id = %s AND u.email = 'admin'", (r.json()["id"],))
        row = cur.fetchone()
    assert row["tenant_id"] == row["owner_tenant"]
