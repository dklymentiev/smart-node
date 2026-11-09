"""Permissions admitted by where a call comes from, and handler-scoped ones.

Measured on the real chain in a throwaway database built from the bootstrap
files: the real `requires()`, the real identity resolution, the seeded
catalogue. Nothing in the check or in identity is replaced.

What has to hold:

  * an internal permission is held by no role, so an account without the
    owner's role is refused, and the refusal names the permission and says no
    trusted origin was presented;
  * a node service that proves itself and is listed for the permission is
    admitted with no account at all, and the event says which service;
  * a proved service that is not listed, a wrong credential, and a missing
    setting each refuse, and none of them admits;
  * an overlay permission is also held by the admin tier;
  * a handler-scoped permission passes the startup scan without an object and
    is decided on the role axis, with the event saying the handler scopes it;
  * a unix-socket peer is admitted only when the kernel-named uid belongs to
    the configured group.
"""

from __future__ import annotations

import asyncio
import grp
import os
import pwd
import tempfile
import threading
import time

import pytest
from fastapi import Depends, FastAPI

from permission_db import (  # noqa: F401
    ADMIN, OWNER, PLAIN, PROXY_SECRET, permission_db, session)

SECRET = "s" * 64
ALLOWLIST = ("brin:internal.events.write,internal.messages.routing;"
             "voice:internal.events.write")


def _app() -> FastAPI:
    from permissions import requires

    app = FastAPI()

    @app.post("/toy/event")
    def _event(principal=Depends(requires("internal.events.write"))) -> dict:
        return {"account": principal.username if principal else None}

    @app.get("/toy/routing", dependencies=[Depends(requires("internal.messages.routing"))])
    def _routing() -> dict:
        return {"ok": True}

    @app.post("/toy/notify", dependencies=[Depends(requires("notifications.notify"))])
    def _notify() -> dict:
        return {"ok": True}

    @app.post("/toy/cron-run")
    def _cron_run(principal=Depends(requires("crons.runs.write"))) -> dict:
        return {"account": principal.username if principal else None}

    @app.post("/toy/create-agent")
    def _create_agent(principal=Depends(requires("agents.create"))) -> dict:
        return {"account": principal.username if principal else None}

    @app.patch("/toy/desk/{desk_id}",
               dependencies=[Depends(requires("desks.update", object=lambda r: "desk:toy"))])
    def _desk() -> dict:
        return {"ok": True}

    @app.get("/toy/secret", dependencies=[Depends(requires("secrets.read"))])
    def _secret() -> dict:
        return {"ok": True}

    @app.get("/toy/messages", dependencies=[Depends(requires("messages.read"))])
    def _messages() -> dict:
        return {"ok": True}

    return app


def _send(app, method: str, path: str, headers: dict | None = None, **kwargs):
    """One request from loopback over TCP, with exactly the headers given."""
    import httpx

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as client:
            return await client.request(method, path, headers=headers or {}, **kwargs)

    return asyncio.run(_go())


def _service(name: str, secret: str = SECRET) -> dict:
    return {"X-SN-Service": name, "X-SN-Service-Auth": secret}


def _last_event(cursor, permission: str) -> dict:
    with cursor() as cur:
        cur.execute(
            "SELECT actor, event_type, payload FROM sn_core.events "
            "WHERE entity_type = 'permission' AND entity_id = %s "
            "ORDER BY id DESC LIMIT 1",
            (permission,),
        )
        return cur.fetchone()


@pytest.fixture
def node(permission_db, monkeypatch):
    """A node with the service credential and the service list configured."""
    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", ALLOWLIST)
    monkeypatch.delenv("SN_COMPONENTS_GROUP", raising=False)
    monkeypatch.delenv("SN_OPERATOR_ACTOR", raising=False)
    return permission_db


# ---------------------------------------------------------------------------
# the seed
# ---------------------------------------------------------------------------

def test_internal_permissions_are_seeded_and_held_by_no_role(node):
    internal = ["internal.events.write", "internal.messages.inbound",
                "internal.messages.voice.write", "internal.messages.routing"]
    with node() as cur:
        cur.execute("SELECT name, object_axis FROM sn_config.permissions "
                    "WHERE name = ANY(%s)", (internal,))
        rows = {r["name"]: r["object_axis"] for r in cur.fetchall()}
        cur.execute("SELECT count(*) AS n FROM sn_config.role_permissions "
                    "WHERE permission = ANY(%s)", (internal,))
        held = cur.fetchone()["n"]
    assert rows == {name: "none" for name in internal}
    assert held == 0


def test_overlay_permissions_are_held_by_the_admin_tier_only(node):
    overlay = ["notifications.notify", "crons.runs.write", "budgets.notify_exhaustion"]
    with node() as cur:
        cur.execute("SELECT permission, role FROM sn_config.role_permissions "
                    "WHERE permission = ANY(%s)", (overlay,))
        pairs = {(r["permission"], r["role"]) for r in cur.fetchall()}
    assert pairs == {(p, r) for p in overlay for r in ("admin", "super-admin")}


# ---------------------------------------------------------------------------
# no trusted origin
# ---------------------------------------------------------------------------

def test_plain_account_without_trusted_origin_is_refused_by_name(node):
    r = _send(_app(), "POST", "/toy/event", session(PLAIN))
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'internal.events.write'" in detail and "no trusted origin" in detail

    event = _last_event(node, "internal.events.write")
    assert event["event_type"] == "auth.permission_denied"
    # A signed-in person: the proxy's proof is real and opens no internal
    # permission, and neither other origin was offered.
    assert event["payload"]["trusted_origin"] == {
        "service": "not presented",
        "peer": "not a unix-socket peer",
        "proxy": "the proxy is not listed for this permission",
    }


def test_admin_does_not_hold_an_internal_permission(node):
    r = _send(_app(), "POST", "/toy/event", session(ADMIN))
    assert r.status_code == 403, r.text
    assert "'admin'" in r.json()["detail"]


def test_caller_with_no_account_and_no_origin_is_refused(node):
    r = _send(_app(), "POST", "/toy/event")
    assert r.status_code == 403, r.text
    assert "no trusted origin" in r.json()["detail"]
    assert _last_event(node, "internal.events.write")["actor"] == "system:unidentified-caller"


# ---------------------------------------------------------------------------
# channel A: a node service
# ---------------------------------------------------------------------------

def test_listed_service_is_admitted_without_an_account(node):
    r = _send(_app(), "POST", "/toy/event", _service("brin"))
    assert r.status_code == 200, r.text
    assert r.json() == {"account": None}

    event = _last_event(node, "internal.events.write")
    assert event["event_type"] == "auth.permission_granted"
    assert event["actor"] == "component:brin"
    payload = event["payload"]
    assert payload["admitted_by"] == "trusted_origin"
    assert payload["channel"] == "service"
    assert payload["principal"] == "brin"


def test_service_not_listed_for_the_permission_is_refused(node):
    # voice is a real, proved service; it is listed for events, not for routing.
    assert _send(_app(), "POST", "/toy/event", _service("voice")).status_code == 200
    r = _send(_app(), "GET", "/toy/routing", _service("voice"))
    assert r.status_code == 403, r.text
    assert "'internal.messages.routing'" in r.json()["detail"]
    tried = _last_event(node, "internal.messages.routing")["payload"]["trusted_origin"]
    assert tried["service"] == "'voice' is not listed for this permission"


def test_unlisted_service_name_is_refused(node):
    r = _send(_app(), "POST", "/toy/event", _service("somebody-else"))
    assert r.status_code == 403, r.text


def test_wrong_secret_is_refused(node):
    r = _send(_app(), "POST", "/toy/event", _service("brin", "w" * 64))
    assert r.status_code == 403, r.text
    tried = _last_event(node, "internal.events.write")["payload"]["trusted_origin"]
    assert tried["service"] == "proof not verified"


def test_secret_without_a_service_name_is_refused(node):
    r = _send(_app(), "POST", "/toy/event", {"X-SN-Service-Auth": SECRET})
    assert r.status_code == 403, r.text


def test_unset_service_list_admits_nobody_and_names_the_setting(node, monkeypatch):
    monkeypatch.delenv("SN_SERVICE_PERMISSIONS")
    r = _send(_app(), "POST", "/toy/event", _service("brin"))
    assert r.status_code == 503, r.text
    assert "SN_SERVICE_PERMISSIONS" in r.json()["detail"]
    assert _last_event(node, "internal.events.write")["event_type"] == "auth.permission_denied"


def test_unset_service_secret_admits_nobody_and_names_the_setting(node, monkeypatch):
    monkeypatch.delenv("SN_SERVICE_AUTH_SECRET")
    r = _send(_app(), "POST", "/toy/event", _service("brin", ""))
    assert r.status_code == 503, r.text
    assert "SN_SERVICE_AUTH_SECRET" in r.json()["detail"]


def test_service_list_has_no_wildcard(node, monkeypatch):
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", "brin:*")
    assert _send(_app(), "POST", "/toy/event", _service("brin")).status_code == 403


# ---------------------------------------------------------------------------
# channel C: the proxy proof
# ---------------------------------------------------------------------------

def test_proxy_proof_alone_admits_no_internal_permission(node, monkeypatch):
    monkeypatch.setenv("SN_PROXY_AUTH_SECRET", PROXY_SECRET)
    r = _send(_app(), "POST", "/toy/event", {"X-SN-Proxy-Auth": PROXY_SECRET})
    assert r.status_code == 403, r.text
    tried = _last_event(node, "internal.events.write")["payload"]["trusted_origin"]
    assert tried["proxy"] == "the proxy is not listed for this permission"

    r = _send(_app(), "POST", "/toy/event", {"X-SN-Proxy-Auth": "x" * 64})
    assert r.status_code == 403, r.text
    tried = _last_event(node, "internal.events.write")["payload"]["trusted_origin"]
    assert tried["proxy"] == "proof not verified"


# ---------------------------------------------------------------------------
# the role axis still decides first
# ---------------------------------------------------------------------------

def test_owner_is_admitted_to_an_internal_permission(node):
    r = _send(_app(), "POST", "/toy/event", session(OWNER))
    assert r.status_code == 200, r.text
    assert r.json() == {"account": OWNER}
    payload = _last_event(node, "internal.events.write")["payload"]
    assert payload["reason"] == "super-admin" and "channel" not in payload


def test_overlay_permission_is_admitted_by_role_for_the_admin_tier(node):
    assert _send(_app(), "POST", "/toy/notify", session(ADMIN)).status_code == 200
    payload = _last_event(node, "notifications.notify")["payload"]
    assert payload["role"] == "admin" and "channel" not in payload

    assert _send(_app(), "POST", "/toy/notify", session(OWNER)).status_code == 200

    r = _send(_app(), "POST", "/toy/notify", session(PLAIN))
    assert r.status_code == 403, r.text
    assert "'notifications.notify'" in r.json()["detail"]


# ---------------------------------------------------------------------------
# handler-scoped permissions
# ---------------------------------------------------------------------------

def test_messages_read_needs_no_object_and_is_decided_on_the_role_axis(node):
    from permissions import validate_routes

    app = _app()
    validate_routes(app, strict_undeclared=True)

    r = _send(app, "GET", "/toy/messages", session(PLAIN))
    assert r.status_code == 200, r.text
    payload = _last_event(node, "messages.read")["payload"]
    assert payload["object_axis"] == "none"
    assert payload["object_axis_deferred"] == "handler"
    assert payload["reason"] == "role grant (no object axis)"


def test_messages_send_is_seeded_without_an_object_axis(node):
    with node() as cur:
        cur.execute("SELECT name, object_axis FROM sn_config.permissions "
                    "WHERE name IN ('messages.read', 'messages.send')")
        assert {r["name"]: r["object_axis"] for r in cur.fetchall()} == {
            "messages.read": "none", "messages.send": "none"}


def test_trusted_origin_permission_takes_no_object():
    from permissions import requires

    with pytest.raises(ValueError):
        requires("internal.events.write", object=lambda request: "x")


# ---------------------------------------------------------------------------
# channel B: a unix-socket peer
#
# A real socket and a real server, so the uid below is what the kernel reported
# for this test process and not a value the test supplied. The group is one
# this account really belongs to. What this cannot reach is the node's own
# service group and the mode of the node's socket; those need an installed node.
# ---------------------------------------------------------------------------

def _serve_on_socket(app):
    import uvicorn

    work = tempfile.mkdtemp(prefix="sn-uds-")
    path = os.path.join(work, "api.sock")
    server = uvicorn.Server(uvicorn.Config(app, uds=path, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not (server.started and os.path.exists(path)):
        if time.time() > deadline:
            raise RuntimeError("the unix-socket server did not start")
        time.sleep(0.05)
    try:
        yield path
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.rmdir(work)


@pytest.fixture
def socket_app(node):
    yield from _serve_on_socket(_app())


@pytest.fixture
def socket_installer_routes(node):
    """The real runtime and provision routers on a real unix socket."""
    app = _routers("runtimes")
    app.include_router(__import__("routes.provision", fromlist=["router"]).router,
                       prefix="/api/provision")
    yield from _serve_on_socket(app)


def _send_uds(path: str, method: str, url: str, headers: dict | None = None, **kwargs):
    import httpx

    with httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url="http://core-api") as c:
        return c.request(method, url, headers=headers or {}, **kwargs)


def _own_group() -> str:
    return grp.getgrgid(pwd.getpwuid(os.getuid()).pw_gid).gr_name


def _foreign_group() -> str:
    me = pwd.getpwuid(os.getuid())
    for entry in grp.getgrall():
        if entry.gr_gid != me.pw_gid and me.pw_name not in entry.gr_mem:
            return entry.gr_name
    pytest.skip("this account belongs to every group on the machine")


def test_socket_peer_in_the_components_group_is_admitted(socket_app, node, monkeypatch):
    monkeypatch.setenv("SN_COMPONENTS_GROUP", _own_group())
    # The owner's name is typed here on purpose: on the socket it is ignored.
    r = _send_uds(socket_app, "POST", "/toy/event", session(OWNER))
    assert r.status_code == 200, r.text
    assert r.json() == {"account": None}
    payload = _last_event(node, "internal.events.write")["payload"]
    assert payload["channel"] == "peer"
    assert payload["principal"] == f"uid={os.getuid()}"


def test_socket_peer_outside_the_components_group_is_refused(socket_app, node, monkeypatch):
    group = _foreign_group()
    monkeypatch.setenv("SN_COMPONENTS_GROUP", group)
    r = _send_uds(socket_app, "POST", "/toy/event")
    assert r.status_code == 403, r.text
    tried = _last_event(node, "internal.events.write")["payload"]["trusted_origin"]
    assert tried["peer"] == f"uid={os.getuid()} is not in group '{group}'"


def test_socket_peer_with_no_group_configured_is_refused_naming_the_setting(socket_app, node):
    r = _send_uds(socket_app, "POST", "/toy/event")
    assert r.status_code == 503, r.text
    assert "SN_COMPONENTS_GROUP" in r.json()["detail"]


def test_operator_account_on_the_socket_reports_a_cron_run_as_the_owner(socket_app, node, monkeypatch):
    """The cron wrapper's path: the node's root account on the socket, no header
    and no credential. The kernel names the account, the node maps its operator
    account to the owner, and the owner's role admits the run report. Here the
    operator account is this test's own, which is as close as a test can stand
    to being root."""
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("SN_OPERATOR_USERS", me)
    monkeypatch.setenv("SN_OPERATOR_ACTOR", OWNER)
    r = _send_uds(socket_app, "POST", "/toy/cron-run")
    assert r.status_code == 200, r.text
    assert r.json() == {"account": OWNER}
    payload = _last_event(node, "crons.runs.write")["payload"]
    assert payload["role"] == "super-admin" and "channel" not in payload


def test_other_account_on_the_socket_cannot_report_a_cron_run(socket_app, node, monkeypatch):
    monkeypatch.setenv("SN_OPERATOR_USERS", "root")
    monkeypatch.setenv("SN_COMPONENTS_GROUP", _foreign_group())
    r = _send_uds(socket_app, "POST", "/toy/cron-run")
    assert r.status_code == 403, r.text
    assert "'crons.runs.write'" in r.json()["detail"]


# ---------------------------------------------------------------------------
# a node service on a permission outside the trusted-origin names
#
# The list decides: a proved service is admitted to exactly what is listed for
# it. `agents.create` is an ordinary admin permission; the list below gives it
# to one service for the length of a test.
# ---------------------------------------------------------------------------

@pytest.fixture
def listed(node, monkeypatch):
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS",
                       "brin:agents.create,desks.update;voice:internal.events.write")
    return node


def test_service_is_admitted_to_an_ordinary_permission_listed_for_it(listed):
    r = _send(_app(), "POST", "/toy/create-agent", _service("brin"))
    assert r.status_code == 200, r.text
    assert r.json() == {"account": None}
    event = _last_event(listed, "agents.create")
    assert event["event_type"] == "auth.permission_granted"
    assert event["actor"] == "component:brin"
    assert event["payload"]["channel"] == "service"
    assert event["payload"]["principal"] == "brin"


def test_service_is_refused_an_ordinary_permission_not_listed_for_it(listed):
    r = _send(_app(), "POST", "/toy/create-agent", _service("voice"))
    assert r.status_code == 403, r.text
    assert "'agents.create'" in r.json()["detail"]
    tried = _last_event(listed, "agents.create")["payload"]["trusted_origin"]
    assert tried == {"service": "'voice' is not listed for this permission"}

    r = _send(_app(), "POST", "/toy/create-agent", _service("brin", "w" * 64))
    assert r.status_code == 403, r.text


def test_socket_peer_and_proxy_are_not_asked_for_an_ordinary_permission(socket_app, listed,
                                                                         monkeypatch):
    """Being a node component on the socket admits the internal callbacks and
    nothing else: an ordinary permission is refused by identity as before."""
    monkeypatch.setenv("SN_COMPONENTS_GROUP", _own_group())
    assert _send_uds(socket_app, "POST", "/toy/event").status_code == 200
    r = _send_uds(socket_app, "POST", "/toy/create-agent")
    assert r.status_code == 403, r.text
    assert "unknown-peer" in r.json()["detail"]


# ---------------------------------------------------------------------------
# a declared secret operator on the socket
#
# The service MCPs run as their own OS accounts and read their credentials
# from the store per call. They are no account on the role axis, so the gate
# has to admit them by the installer's list or the handler's rule is never
# asked. The OS account below is this test process's own, named by the kernel.
# ---------------------------------------------------------------------------

def _own_account() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def test_declared_secret_operator_on_the_socket_reads_a_secret(socket_app, node, monkeypatch):
    monkeypatch.setenv("SN_SECRETS_OPERATOR_USERS", f"root,{_own_account()}")
    r = _send_uds(socket_app, "GET", "/toy/secret")
    assert r.status_code == 200, r.text
    payload = _last_event(node, "secrets.read")["payload"]
    assert payload["channel"] == "secret-operator"
    assert payload["principal"] == f"os:{_own_account()}"


def test_undeclared_socket_peer_is_refused_a_secret(socket_app, node, monkeypatch):
    if _own_account() == "root":
        pytest.skip("root is the operator and holds an account")
    monkeypatch.setenv("SN_SECRETS_OPERATOR_USERS", "root,some-other-service")
    r = _send_uds(socket_app, "GET", "/toy/secret")
    assert r.status_code == 403, r.text
    assert "unknown-peer" in r.json()["detail"]


def test_unset_secret_operator_list_admits_nobody(socket_app, node, monkeypatch):
    monkeypatch.delenv("SN_SECRETS_OPERATOR_USERS", raising=False)
    assert _send_uds(socket_app, "GET", "/toy/secret").status_code == 403


def test_declared_secret_operator_is_admitted_to_nothing_else(socket_app, node, monkeypatch):
    monkeypatch.setenv("SN_SECRETS_OPERATOR_USERS", _own_account())
    r = _send_uds(socket_app, "POST", "/toy/create-agent")
    assert r.status_code == 403, r.text


def test_secret_operator_list_means_nothing_over_tcp(node, monkeypatch):
    monkeypatch.setenv("SN_SECRETS_OPERATOR_USERS", _own_account())
    r = _send(_app(), "GET", "/toy/secret")
    assert r.status_code == 403, r.text


def test_ordinary_permission_with_no_proof_keeps_the_answers_it_had(listed):
    # No account: identity's own refusal, untouched.
    r = _send(_app(), "POST", "/toy/create-agent")
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "no active user for identity 'anonymous'"
    # An account whose role lacks it: the matrix's refusal, untouched.
    r = _send(_app(), "POST", "/toy/create-agent", session(PLAIN))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "role 'user' lacks permission 'agents.create'"
    assert "trusted_origin" not in _last_event(listed, "agents.create")["payload"]
    # And the role that holds it.
    r = _send(_app(), "POST", "/toy/create-agent", session(ADMIN))
    assert r.status_code == 200 and r.json() == {"account": ADMIN}, r.text


def test_service_acting_for_someone_is_measured_on_that_identity(listed, monkeypatch):
    """A delegation is not the service acting as itself. The list must not
    hand an agent, through the service that carries it, what the agent's own
    role does not hold."""
    monkeypatch.setenv("SN_SERVICE_AGENTS", "brin:*")
    with listed(commit=True) as cur:
        cur.execute("INSERT INTO sn_config.agents (tenant_id, slug, title, capability) "
                    "VALUES (1, 'delegated', 'Delegated', '') ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
                    "VALUES ('agent:delegated', 'delegated', 'agent', 'specialist', 'agent', "
                    "'internal') ON CONFLICT (id) DO NOTHING")
        cur.execute("INSERT INTO sn_config.users (tenant_id, email, role, active, actor_id) "
                    "VALUES (1, 'delegated', 'agent', true, 'agent:delegated') "
                    "ON CONFLICT (tenant_id, email) DO NOTHING")
    headers = {**_service("brin"), "X-SN-On-Behalf-Of": "delegated"}
    r = _send(_app(), "POST", "/toy/create-agent", headers)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "role 'user' lacks permission 'agents.create'"
    tried = _last_event(listed, "agents.create")["payload"]["trusted_origin"]
    assert tried["service"].startswith("acting for another identity")
    # The same holds on a trusted-origin permission.
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", ALLOWLIST)
    r = _send(_app(), "POST", "/toy/event", headers)
    assert r.status_code == 403, r.text


def test_service_is_never_admitted_to_an_object_axis_permission(listed):
    r = _send(_app(), "PATCH", "/toy/desk/toy", _service("brin"))
    assert r.status_code == 403, r.text
    assert "'desks.update'" in r.json()["detail"]
    tried = _last_event(listed, "desks.update")["payload"]["trusted_origin"]
    assert "object axis 'desk'" in tried["service"]


# ---------------------------------------------------------------------------
# the real routers a node service reaches by its listing
# ---------------------------------------------------------------------------

def _routers(*names) -> FastAPI:
    import importlib

    app = FastAPI()
    for name in names:
        app.include_router(importlib.import_module("routes." + name).router)
    return app


def test_brin_reads_the_turn_bound_by_its_listing_and_nothing_more_of_limits(node, monkeypatch):
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", "brin:limits.read;voice:internal.events.write")
    app = _routers("limits")

    r = _send(app, "GET", "/api/limits/turn-bound")
    assert r.status_code == 403, r.text
    r = _send(app, "GET", "/api/limits/turn-bound", _service("voice"))
    assert r.status_code == 403 and "'limits.read'" in r.text, r.text

    r = _send(app, "GET", "/api/limits/turn-bound", _service("brin"))
    # Past the gate: the registry's own answer, a bound or an empty registry.
    assert r.status_code in (200, 503), r.text
    assert "limits.read" not in r.text
    assert _last_event(node, "limits.read")["payload"]["principal"] == "brin"

    # The listing admits the service to the permission, not to the owner's
    # registry: the handler still answers that only the owner reads it.
    r = _send(app, "GET", "/api/limits", _service("brin"))
    assert r.status_code == 403 and "only the node owner" in r.text, r.text
    # And writing is not listed at all.
    r = _send(app, "PUT", "/api/limits/pause", _service("brin"), json={"paused": True})
    assert r.status_code == 403 and "'limits.write'" in r.text, r.text


def test_brin_ensures_a_desk_and_reads_the_workflow_list_by_its_listing(node, monkeypatch):
    import uuid

    monkeypatch.setenv("SN_SERVICE_PERMISSIONS",
                       "brin:desks.create,workflows.read;voice:internal.events.write")
    app = _routers("desks")
    app.include_router(__import__("routes.workflows", fromlist=["router"]).router,
                       prefix="/api/workflows")
    desk = str(uuid.uuid4())
    body = {"studio_user": "owner", "agent_slug": None, "title": "from the bus"}

    # A typed name is what the callers used to send, and it is no account.
    r = _send(app, "PUT", f"/api/desks/{desk}/ensure", {"Remote-User": "brin"}, json=body)
    assert r.status_code == 403, r.text
    r = _send(app, "GET", "/api/workflows", {"Remote-User": "runner"})
    assert r.status_code == 403, r.text
    r = _send(app, "PUT", f"/api/desks/{desk}/ensure", _service("voice"), json=body)
    assert r.status_code == 403 and "'desks.create'" in r.text, r.text

    r = _send(app, "PUT", f"/api/desks/{desk}/ensure", _service("brin"), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["desk"]["id"] == desk
    assert _last_event(node, "desks.create")["payload"]["principal"] == "brin"

    r = _send(app, "GET", "/api/workflows", _service("brin"))
    assert r.status_code == 200 and "workflows" in r.json(), r.text

    # The listing opens the permission, not an account: creating a desk needs
    # an owner to create it for, and the handler asks identity for one.
    r = _send(app, "POST", "/api/desks", _service("brin"), json={"title": "x"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# the installer's calls: the node's operator account on the socket
#
# The installer runs as root and sends no credential. On the socket the kernel
# names the account, the node maps its operator account to the owner, and the
# owner's role admits. The operator account here is this test's own.
# ---------------------------------------------------------------------------

RUNTIME = {"slug": "installer-test-runtime", "label": "Installer test", "kind": "other"}


def test_installer_on_the_socket_registers_a_runtime_as_the_owner(socket_installer_routes, node,
                                                                 monkeypatch):
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("SN_OPERATOR_USERS", me)
    monkeypatch.setenv("SN_OPERATOR_ACTOR", OWNER)

    # The name the installer used to type is ignored on the socket.
    r = _send_uds(socket_installer_routes, "POST", "/api/runtimes",
                  {"Remote-User": "installer:installer-test-runtime"}, json=RUNTIME)
    assert r.status_code in (200, 201), r.text
    assert _last_event(node, "runtimes.write")["payload"]["role"] == "super-admin"
    with node() as cur:
        cur.execute("SELECT actor FROM sn_core.events WHERE entity_type = 'runtime' "
                    "AND entity_id = %s ORDER BY id DESC LIMIT 1", (RUNTIME["slug"],))
        # Attributed to the account the kernel proved, not to an unnamed peer.
        assert cur.fetchone()["actor"] == OWNER

    r = _send_uds(socket_installer_routes, "GET", "/api/provision/params")
    assert r.status_code == 200 and "params" in r.json(), r.text

    r = _send_uds(socket_installer_routes, "DELETE", f"/api/runtimes/{RUNTIME['slug']}?hard=true")
    assert r.status_code == 200, r.text


def test_other_account_on_the_socket_is_not_the_installer(socket_installer_routes, node,
                                                          monkeypatch):
    monkeypatch.setenv("SN_OPERATOR_USERS", "root")
    r = _send_uds(socket_installer_routes, "POST", "/api/runtimes", json=RUNTIME)
    assert r.status_code == 403, r.text
    r = _send_uds(socket_installer_routes, "GET", "/api/provision/params")
    assert r.status_code == 403, r.text


def test_typed_installer_name_over_tcp_is_refused(node):
    r = _send(_routers("runtimes"), "POST", "/api/runtimes",
              {"Remote-User": "installer:installer-test-runtime"}, json=RUNTIME)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# the setup window
#
# The real setup router, with the token file and the operator store pointed at
# a temp directory. The window is open while the one-time token is on disk.
# ---------------------------------------------------------------------------

@pytest.fixture
def setup_node(node, monkeypatch, tmp_path):
    from routes import setup

    token_file = tmp_path / "setup-token"
    users_file = tmp_path / "users.json"
    monkeypatch.setattr(setup, "SETUP_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(setup, "AUTH_USERS_FILE", str(users_file))
    app = FastAPI()
    app.include_router(setup.router)
    return app, token_file, users_file


def test_setup_route_declares_its_permission_and_no_role_holds_it(setup_node, node):
    from permissions import route_permission, validate_routes

    app = setup_node[0]
    declared = {r.path: route_permission(r) for r in app.routes if hasattr(r, "dependant")}
    assert declared["/api/setup/complete"] == ("setup.complete", False)
    validate_routes(app, strict_undeclared=True)
    with node() as cur:
        cur.execute("SELECT count(*) AS n FROM sn_config.role_permissions "
                    "WHERE permission = 'setup.complete'")
        assert cur.fetchone()["n"] == 0


def test_closed_setup_window_refuses_everyone_with_the_answer_it_always_gave(setup_node, node):
    app, _token_file, _ = setup_node
    body = {"token": "anything", "username": "admin", "password": "long-enough-pw"}
    for headers in ({}, session(OWNER), _service("brin")):
        r = _send(app, "POST", "/api/setup/complete", headers, json=body)
        assert r.status_code == 409, (headers, r.text)
        assert r.json()["detail"] == "setup is not available (already completed?)"
    event = _last_event(node, "setup.complete")
    assert event["event_type"] == "auth.permission_denied"
    assert event["payload"]["trusted_origin"] == {
        "setup": "window closed: no setup token on this node"}


def test_open_setup_window_admits_the_call_and_the_handler_compares_the_token(setup_node, node):
    app, token_file, users_file = setup_node
    token_file.write_text("the-one-time-token\n")

    r = _send(app, "POST", "/api/setup/complete",
              json={"token": "a-wrong-token", "username": "admin", "password": "long-enough-pw"})
    assert r.status_code == 403 and r.json()["detail"] == "invalid setup token", r.text
    event = _last_event(node, "setup.complete")
    assert event["event_type"] == "auth.permission_granted"
    assert event["payload"]["channel"] == "setup"
    assert event["payload"]["window"] == "open"
    assert token_file.exists() and not users_file.exists()

    # Past the token the handler's own validation still answers.
    r = _send(app, "POST", "/api/setup/complete",
              json={"token": "the-one-time-token", "username": "admin", "password": "short"})
    assert r.status_code == 400 and "password" in r.text, r.text

    # An operator that already exists is refused by the handler, token or not.
    users_file.write_text('{"admin": {"password": "x"}}')
    r = _send(app, "POST", "/api/setup/complete",
              json={"token": "the-one-time-token", "username": "admin", "password": "long-enough-pw"})
    assert r.status_code == 409 and "already" in r.text, r.text


def test_setup_window_admits_nothing_but_setup(setup_node, node):
    _app_, token_file, _ = setup_node
    token_file.write_text("the-one-time-token\n")
    r = _send(_app(), "POST", "/toy/event")
    assert r.status_code == 403, r.text
    r = _send(_app(), "POST", "/toy/create-agent")
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# with the TCP guard: a person, a service acting for the owner, a missing owner
# ---------------------------------------------------------------------------

def test_typed_owner_name_over_tcp_changes_nothing_and_reads_nothing_internal(node):
    """A name in a header over TCP is not the owner for anything that changes
    state. The guard refuses it inside identity, and the permission check must
    not find another way in for it."""
    for path in ("/toy/event", "/toy/notify", "/toy/create-agent", "/toy/cron-run"):
        r = _send(_app(), "POST", path, {"Remote-User": OWNER})
        assert r.status_code == 403, (path, r.text)
        assert "asserted Remote-User" in r.json()["detail"], (path, r.text)


def _owner_proof(service: str) -> dict:
    return {"Remote-User": OWNER, "X-SN-Service": service, "X-SN-Service-Auth": SECRET,
            "X-SN-On-Behalf-Of": OWNER}


def _delegations(cursor, path: str) -> int:
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM sn_core.events "
                    "WHERE event_type = 'identity.delegated_owner' AND payload->>'path' = %s",
                    (path,))
        return cur.fetchone()["n"]


def test_service_acting_for_the_owner_is_admitted_once_and_as_the_owner(node, monkeypatch):
    """The planner raises its notice for the owner, on the one route listed for
    it. Identity makes that call the owner's; the permission check admits it on
    the owner's role and by nothing else, and the journal says so once."""
    from auth import Principal, get_principal

    monkeypatch.setenv("SN_OPERATOR_ACTOR", OWNER)
    app = FastAPI()

    # Both shapes a route has: the check alone, and the check plus a handler
    # that resolves the caller again for its own use.
    @app.post("/api/notify")
    def _notify(principal=Depends(requires_notify()),
                again: Principal = Depends(get_principal)) -> dict:
        return {"account": principal.username, "again": again.username,
                "service": again.acting_service}

    before = _delegations(node, "/api/notify")
    r = _send(app, "POST", "/api/notify", _owner_proof("planner"))
    assert r.status_code == 200, r.text
    assert r.json() == {"account": OWNER, "again": OWNER, "service": "planner"}
    assert _delegations(node, "/api/notify") == before + 1

    payload = _last_event(node, "notifications.notify")["payload"]
    assert payload["role"] == "super-admin" and "channel" not in payload


def requires_notify():
    from permissions import requires

    return requires("notifications.notify")


def test_owner_proof_outside_its_listed_routes_is_not_rescued_by_the_service_list(node, monkeypatch):
    """brin is listed for the event write as ITSELF. Naming the owner on that
    route is a delegation outside its list: identity refuses it, and the
    service list must not admit the same request a second way."""
    monkeypatch.setenv("SN_OPERATOR_ACTOR", OWNER)
    r = _send(_app(), "POST", "/toy/event", _owner_proof("brin"))
    assert r.status_code == 403, r.text
    tried = _last_event(node, "internal.events.write")["payload"]["trusted_origin"]
    assert tried["service"].startswith("acting for another identity")
    # As itself it is admitted, and as nobody's account.
    r = _send(_app(), "POST", "/toy/event", _service("brin"))
    assert r.status_code == 200 and r.json() == {"account": None}, r.text


def test_missing_owner_setting_is_answered_503_and_named_not_turned_into_a_refusal(node):
    """With no owner configured, identity cannot say whom a proved service
    acts for. That is the node's fault and it says which setting; the
    permission check must not swallow it into a 403 about the caller."""
    app = FastAPI()

    @app.post("/api/notify", dependencies=[Depends(requires_notify())])
    def _notify() -> dict:
        return {"ok": True}

    r = _send(app, "POST", "/api/notify",
              {"X-SN-Service": "planner", "X-SN-Service-Auth": SECRET,
               "X-SN-On-Behalf-Of": OWNER, "Remote-User": OWNER})
    assert r.status_code == 503, r.text
    assert "SN_OPERATOR_ACTOR" in r.json()["detail"]


def test_gateway_as_itself_is_not_judged_as_an_owner_notice(node, monkeypatch):
    """The gateway has two calls under one name: an owner notice, proved with
    its own credential and naming the owner, and a read as itself, proved with
    the service credential and naming nobody. Identity used to take every
    request named gateway for a notice and judge its proof as one, so the read
    was refused, or answered 503 on a node where the notice credential was
    absent. A call that names nobody is not a delegation."""
    monkeypatch.delenv("SN_GATEWAY_CORE_API_SECRET", raising=False)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", "gateway:agents.create")
    r = _send(_app(), "POST", "/toy/create-agent", _service("gateway"))
    assert r.status_code == 200 and r.json() == {"account": None}, r.text
    assert _last_event(node, "agents.create")["payload"]["principal"] == "gateway"

    # Naming the owner is still a notice, still judged by the notice proof.
    r = _send(_app(), "POST", "/toy/create-agent",
              {**_service("gateway"), "X-SN-On-Behalf-Of": OWNER})
    assert r.status_code == 503 and "gateway core-api proof" in r.text, r.text
    monkeypatch.setenv("SN_GATEWAY_CORE_API_SECRET", "g" * 64)
    r = _send(_app(), "POST", "/toy/create-agent",
              {**_service("gateway"), "X-SN-On-Behalf-Of": OWNER})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# the publish door: the declaration in front, the handler's own rules behind
# ---------------------------------------------------------------------------

def test_publishing_a_flow_is_admitted_by_role_and_keeps_the_handlers_rules(node, monkeypatch,
                                                                            tmp_path):
    from permission_db import call
    from routes import workflows

    app = FastAPI()
    app.include_router(workflows.router, prefix="/api/workflows")
    agents_root = tmp_path / "agents"
    (agents_root / ADMIN).mkdir(parents=True)
    monkeypatch.setattr(workflows, "_AGENTS_ROOT", str(agents_root))
    monkeypatch.setattr(workflows, "_AGENT_HOMES", str(tmp_path / "homes"))
    elsewhere = str(tmp_path / "someone-else" / "flow")
    own = str(agents_root / ADMIN / "flow")

    # The role decides first: an account with no role cannot publish.
    r = call(app, "POST", "/api/workflows/import", PLAIN, json={"name": "f", "source": own})
    assert r.status_code == 403, r.text
    assert "'user'" in r.json()["detail"] and "'workflows.import'" in r.json()["detail"]

    # An admin-role author is admitted, and the handler's rule still stands:
    # an agent publishes from its own directory and from nowhere else.
    r = call(app, "POST", "/api/workflows/import", ADMIN, json={"name": "f", "source": elsewhere})
    assert r.status_code == 403, r.text
    assert "its own directory" in r.json()["detail"]

    # From its own directory the call reaches the checks of the flow itself,
    # which refuse a directory that holds no flow.
    r = call(app, "POST", "/api/workflows/import", ADMIN, json={"name": "f", "source": own})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["message"] == "not published"

    # A typed name publishes nothing.
    r = call(app, "POST", "/api/workflows/import", ADMIN, proved=False,
             json={"name": "f", "source": own})
    assert r.status_code == 403, r.text
