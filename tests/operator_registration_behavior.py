"""Checks for operator-registration.test.sh.

Every request goes through the real identity resolver: a session is the
loopback proxy's proof header, a bearer caller is an Authorization header.
Each check reports on its own line so one refusal does not hide the others.
"""
import hashlib
import logging
import os
import sys
import traceback

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

SESSION_SECRET = "test-only-session-secret"
os.environ["SN_PROXY_AUTH_SECRET"] = SESSION_SECRET


def query(sql, args=()):
    with psycopg2.connect(dbname="smartnode", user="smartnode") as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, args)
            if not cur.description:
                return None
            rows = cur.fetchall()
            return rows[0][next(iter(rows[0]))] if len(rows) == 1 and len(rows[0]) == 1 else rows


def digest(token):
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def footprint(username):
    """Rows the route could have written for this name, in any table."""
    actor = "component:" + username
    return (
        query("SELECT count(*) FROM sn_core.actors WHERE id=%s", (actor,)),
        query("SELECT count(*) FROM sn_config.users WHERE email=%s", (username,)),
        query("SELECT count(*) FROM sn_core.actor_roles WHERE actor_id=%s", (actor,)),
        query("SELECT count(*) FROM sn_config.actor_credentials WHERE actor_id=%s", (actor,)),
    )


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def fresh_seed_holds_no_operator():
    # Runs first, before anything below registers one.
    assert query(
        "SELECT count(*) FROM sn_core.actors a WHERE a.kind='component' AND ("
        " EXISTS (SELECT 1 FROM sn_core.actor_roles r WHERE r.actor_id=a.id) OR"
        " EXISTS (SELECT 1 FROM sn_config.users u WHERE u.actor_id=a.id))"
    ) == 0, "seed created a component actor with a role or an account"
    assert query("SELECT count(*) FROM sn_config.users WHERE email='claude'") == 0, \
        "seed created a 'claude' account"
    assert query("SELECT count(*) FROM sn_config.users WHERE role='admin'") == 0, \
        "seed created an admin account"
    assert query("SELECT count(*) FROM sn_config.actor_credentials") == 0


from auth import Principal, get_principal  # noqa: E402
from permissions import requires, validate_routes  # noqa: E402
from routes import actor_credentials  # noqa: E402
from routes import agents as agents_routes  # noqa: E402
from routes.agents import _ensure_agent_identity  # noqa: E402

app = FastAPI()
app.include_router(actor_credentials.router)
app.include_router(agents_routes.router)

# Agent creation ends in a privileged runner call and a planner call that only
# a real node can answer. Record them here; a refused create reaches neither.
SIDE_EFFECTS = []
agents_routes.runner_op = lambda *args, **kw: SIDE_EFFECTS.append(args) or {"ok": False}
agents_routes._register_planner_actor = lambda *args: SIDE_EFFECTS.append(args)


@app.get("/probe", dependencies=[Depends(requires("agents.read"))])
def probe(principal: Principal = Depends(get_principal)):
    return {"actor_id": principal.actor_id, "username": principal.username,
            "strength": principal.strength}


class Loopback:
    """Present requests as arriving from the reverse proxy on loopback."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=("127.0.0.1", 50000))
        await self.inner(scope, receive, send)


client = TestClient(Loopback(app), raise_server_exceptions=False)


def session(user="admin"):
    return {"Remote-User": user, "X-SN-Proxy-Auth": SESSION_SECRET}


def bearer(token):
    return {"Authorization": "Bearer " + token}


STATE = {}


@check
def route_passes_startup_validation():
    assert "/api/operators" in {r.path for r in app.routes}, "route is not registered"
    # Strict form over the credential routes alone: the agent routes mounted
    # above for the name checks are not part of this declaration.
    declared = FastAPI()
    declared.include_router(actor_credentials.router)
    validate_routes(declared)
    import main
    logging.getLogger("httpx").setLevel(logging.WARNING)  # main turns INFO on
    assert "/api/operators" in {r.path for r in main.app.routes}
    validate_routes(main.app, strict_undeclared=False)


@check
def owner_session_registers_operator_and_gets_token():
    owner = query("SELECT actor_id FROM sn_core.actor_roles WHERE role='super-admin'")
    response = client.post("/api/operators", headers=session(),
                           json={"username": "opsdesk", "display_name": "Ops desk"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["actor_id"] == "component:opsdesk" and body["username"] == "opsdesk"
    token = body["token"]
    STATE["token"] = token
    actor = query("SELECT kind, relation, display_name FROM sn_core.actors WHERE id='component:opsdesk'")[0]
    assert (actor["kind"], actor["relation"], actor["display_name"]) == ("component", "internal", "Ops desk")
    user = query("SELECT role, active, actor_id, tenant_id FROM sn_config.users WHERE email='opsdesk'")[0]
    assert (user["role"], user["active"], user["actor_id"], user["tenant_id"]) == ("admin", True, "component:opsdesk", 1)
    role = query("SELECT role, source, granted_by FROM sn_core.actor_roles WHERE actor_id='component:opsdesk'")[0]
    assert (role["role"], role["source"], role["granted_by"]) == ("admin", "owner_grant", owner)
    cred = query("SELECT cred_type, cred_value, scope FROM sn_config.actor_credentials WHERE actor_id='component:opsdesk'")[0]
    assert (cred["cred_type"], cred["cred_value"], cred["scope"]) == ("bearer_token", digest(token), "admin")
    assert query("SELECT count(*) FROM sn_core.events WHERE payload::text LIKE %s", ("%" + token + "%",)) == 0, \
        "plaintext token reached the event log"
    assert query("SELECT count(*) FROM sn_core.events WHERE event_type='actor.operator.registered'"
                 " AND entity_id='component:opsdesk' AND actor=%s", (owner,)) == 1


@check
def issued_token_authenticates_as_admin_operator():
    assert "token" in STATE, "no token was issued"
    response = client.get("/probe", headers=bearer(STATE["token"]))
    assert response.status_code == 200, response.text
    assert response.json() == {"actor_id": "component:opsdesk", "username": "opsdesk",
                               "strength": "proved"}, response.text
    granted = query("SELECT payload->>'role' AS role FROM sn_core.events"
                    " WHERE event_type='auth.permission_granted' AND entity_id='agents.read'"
                    " AND actor='component:opsdesk'")
    assert granted == "admin", granted
    assert client.get("/probe", headers=bearer(STATE["token"] + "x")).status_code == 403


@check
def duplicate_username_is_refused_without_extra_rows():
    before = footprint("opsdesk")
    assert before == (1, 1, 1, 1), before
    stored = query("SELECT cred_value FROM sn_config.actor_credentials WHERE actor_id='component:opsdesk'")
    response = client.post("/api/operators", headers=session(), json={"username": "opsdesk"})
    assert response.status_code == 409, response.text
    assert "token" not in response.json()
    assert footprint("opsdesk") == before
    assert query("SELECT cred_value FROM sn_config.actor_credentials WHERE actor_id='component:opsdesk'") == stored
    assert query("SELECT count(*) FROM sn_core.events WHERE event_type='actor.operator.registered'") == 1


def agent_footprint(slug):
    """Everything an agent create could have written under this name."""
    return (
        query("SELECT count(*) FROM sn_config.agents WHERE slug=%s", (slug,)),
        query("SELECT count(*) FROM sn_config.specialists WHERE slug=%s", (slug,)),
        query("SELECT count(*) FROM sn_core.actors WHERE id=%s", ("agent:" + slug,)),
        query("SELECT role, active, actor_id FROM sn_config.users WHERE email=%s", (slug,)),
        query("SELECT count(*) FROM sn_core.events WHERE entity_type='agent' AND entity_id=%s", (slug,)),
        os.path.exists(os.path.join(os.environ["SN_AGENTS_ROOT"], slug)),
    )


@check
def agent_slug_naming_an_existing_account_is_refused():
    # The registered operator, then the owner's own account: either name as an
    # agent slug would hand the agent that account's role.
    assert footprint("opsdesk") == (1, 1, 1, 1), footprint("opsdesk")
    for slug in ("opsdesk", "admin"):
        before = agent_footprint(slug)
        response = client.post("/api/agents", headers=session(), json={
            "slug": slug, "title": "Impostor", "specialist_body": "You are an impostor."})
        assert response.status_code == 409, f"{slug}: {response.status_code} {response.text}"
        assert agent_footprint(slug) == before, f"{slug}: {agent_footprint(slug)}"
    assert SIDE_EFFECTS == [], SIDE_EFFECTS


@check
def free_or_own_agent_slug_is_not_refused_by_the_name_check():
    # A slug nobody holds, and one whose leftover account is an agent's own.
    # Both must get past the name check. This harness has no agent template,
    # so directory provisioning is where such a create stops here.
    query("INSERT INTO sn_config.users (tenant_id, email, role, active) VALUES (1, 'formeragent', 'agent', false)")
    for slug in ("freshagent", "formeragent"):
        response = client.post("/api/agents", headers=session(), json={"slug": slug, "title": "Agent"})
        assert response.status_code == 500 and "provision directory" in response.text,             f"{slug}: {response.status_code} {response.text}"


@check
def name_of_existing_account_agent_or_component_is_refused():
    # An agent row with no account of its own: the shape older nodes carry.
    query("INSERT INTO sn_config.agents(tenant_id,slug,title,capability) VALUES (1,'loneagent','Lone Agent','')")
    query("INSERT INTO sn_core.actors(id,display_name,type,layer,kind,relation)"
          " VALUES ('component:relay','Relay','component','system','component','internal')")
    for name, expected in [("admin", (0, 1, 0, 0)), ("loneagent", (0, 0, 0, 0)), ("relay", (1, 0, 0, 0))]:
        response = client.post("/api/operators", headers=session(), json={"username": name})
        assert response.status_code == 409, f"{name}: {response.status_code} {response.text}"
        assert footprint(name) == expected, f"{name}: {footprint(name)}"
    assert query("SELECT actor_id FROM sn_config.users WHERE email='admin'") == \
        query("SELECT actor_id FROM sn_core.actor_roles WHERE role='super-admin'")


@check
def caller_without_roles_grant_is_refused():
    with psycopg2.connect(dbname="smartnode", user="smartnode") as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("INSERT INTO sn_config.agents(tenant_id,slug,title,capability)"
                        " VALUES (1,'deskadmin','Desk Admin','configurator')")
            _ensure_agent_identity(cur, "deskadmin", "Desk Admin", 1)
    assert query("SELECT role FROM sn_core.actor_roles WHERE actor_id='agent:deskadmin'") == "admin"
    response = client.post("/api/operators", headers=session("deskadmin"), json={"username": "second"})
    assert response.status_code == 403, response.text
    assert "roles.grant" in response.text, response.text
    assert footprint("second") == (0, 0, 0, 0)


@check
def bearer_caller_is_refused_by_session_requirement():
    assert "token" in STATE, "no token was issued"
    # The operator's own token: an operator cannot mint another operator.
    response = client.post("/api/operators", headers=bearer(STATE["token"]), json={"username": "third"})
    assert response.status_code == 403, response.text
    # A bearer token of the owner holds roles.grant, so only the session
    # requirement stands between it and the route. The proxy proof sent
    # alongside must not lift a bearer caller to a session.
    owner = query("SELECT actor_id FROM sn_core.actor_roles WHERE role='super-admin'")
    owner_token = "test-only-owner-bearer"
    query("INSERT INTO sn_config.actor_credentials(actor_id,cred_type,cred_value,scope)"
          " VALUES (%s,'bearer_token',%s,'admin')", (owner, digest(owner_token)))
    try:
        for headers in (bearer(owner_token), {**bearer(owner_token), **session()}):
            response = client.post("/api/operators", headers=headers, json={"username": "third"})
            assert response.status_code == 403, response.text
            assert "signed-in session" in response.text, response.text
            granted = query("SELECT count(*) FROM sn_core.events WHERE event_type='auth.permission_granted'"
                            " AND entity_id='roles.grant' AND actor=%s AND payload->>'role'='super-admin'", (owner,))
            assert granted >= 1, "owner bearer did not reach the session requirement"
    finally:
        query("DELETE FROM sn_config.actor_credentials WHERE actor_id=%s", (owner,))
    assert footprint("third") == (0, 0, 0, 0)


@check
def invalid_username_is_refused():
    actors = query("SELECT count(*) FROM sn_core.actors")
    users = query("SELECT count(*) FROM sn_config.users")
    for name in ["Opsdesk", "9lives", "ops desk", "ops:desk", "ops.desk", "", " opsdesk",
                 "opsdesk\n", "-ops", "a" * 33, "agent:deskadmin"]:
        response = client.post("/api/operators", headers=session(), json={"username": name})
        assert response.status_code == 422, f"{name!r}: {response.status_code} {response.text}"
    for payload in [{}, {"username": None}, {"username": "fourth", "display_name": ""}]:
        response = client.post("/api/operators", headers=session(), json=payload)
        assert response.status_code == 422, f"{payload!r}: {response.status_code} {response.text}"
    assert query("SELECT count(*) FROM sn_core.actors") == actors
    assert query("SELECT count(*) FROM sn_config.users") == users


@check
def failure_after_first_insert_leaves_no_rows():
    # The credential is the last row written. Refusing it must take the actor,
    # the account and the role back out with it.
    query("ALTER TABLE sn_config.actor_credentials ADD CONSTRAINT test_refuse_credential"
          " CHECK (actor_id <> 'component:halfway') NOT VALID")
    try:
        response = client.post("/api/operators", headers=session(), json={"username": "halfway"})
        assert response.status_code == 500, response.text
    finally:
        query("ALTER TABLE sn_config.actor_credentials DROP CONSTRAINT test_refuse_credential")
    assert footprint("halfway") == (0, 0, 0, 0), footprint("halfway")
    assert query("SELECT count(*) FROM sn_core.events WHERE entity_id='component:halfway'") == 0


failed = 0
for fn in CHECKS:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- report every check, then fail
        failed += 1
        last = traceback.extract_tb(exc.__traceback__)[-1]
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else last.line
        print(f"FAIL: {fn.__name__}: {type(exc).__name__}: {detail} (line {last.lineno})")
    else:
        print(f"PASS: {fn.__name__}")
if failed:
    print(f"FAIL: {failed} of {len(CHECKS)} operator registration checks failed")
    sys.exit(1)
print(f"PASS: {len(CHECKS)} operator registration checks")
