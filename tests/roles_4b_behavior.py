"""Assertions shared by fresh and update portions of roles-4b.test.sh."""
import os
import pwd
import uuid
import hashlib

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

def query(sql, args=()):
    with psycopg2.connect(dbname="smartnode", user="smartnode") as con:
        with con.cursor() as cur:
            cur.execute(sql, args)
            if cur.description:
                return cur.fetchone()[0]


# 1.12.51 adds agents.stop (super-admin and admin), workflows.write, memory.read
# and access.read (super-admin each): 136 + 4 permissions, 289 + 5 grants.
# Then agents.memory.read/forget and admin.agent_memory (super-admin, admin) and
# reminders.read/write (super-admin, admin, user): 140 + 5, 294 + 12.
assert query("SELECT count(*) FROM sn_config.permissions") == 146
assert query("SELECT count(*) FROM sn_config.role_permissions") == 306
assert query("SELECT count(*) FROM sn_config.permissions WHERE name IN ('knowledge.read', 'knowledge.write', 'projects.read', 'projects.write', 'projects.members.write', 'projects.reprovision', 'projects.archive', 'users.read', 'messages.read', 'messages.send', 'desks.read', 'attachments.read') AND object_axis = 'none'") == 12
assert query("SELECT count(*) FROM sn_core.actor_roles WHERE role='super-admin'") == 1
assert query("SELECT count(*) FROM sn_config.capability_default_role") == 2
# The seed carries no operator: no component actor holds a role or an account.
assert query("SELECT count(*) FROM sn_core.actors a WHERE a.kind='component' AND (EXISTS (SELECT 1 FROM sn_core.actor_roles r WHERE r.actor_id=a.id) OR EXISTS (SELECT 1 FROM sn_config.users u WHERE u.actor_id=a.id))") == 0
assert query("SELECT count(*) FROM sn_config.users WHERE email='claude'") == 0
assert query("SELECT count(*) FROM sn_config.actor_credentials") == 0

import auth
from auth import Principal, get_principal
from permissions import check_permission, requires, validate_routes
from routes.agents import _ensure_agent_identity
from routes import actor_credentials

# The operator used below is node configuration, so the owner registers it.
owner_actor = query("SELECT actor_id FROM sn_core.actor_roles WHERE role='super-admin'")
credentials_app = FastAPI()
credentials_app.include_router(actor_credentials.router)
credentials_app.dependency_overrides[get_principal] = lambda: Principal(
    "admin", 1, 1, "super-admin", actor_id=owner_actor, strength="session"
)
with TestClient(credentials_app) as client:
    response = client.post("/api/operators", json={"username": "testoperator"})
    assert response.status_code == 201, response.text
    fixture_token = response.json()["token"]
operator_actor = "component:testoperator"
operator_credentials = f"/api/actors/{operator_actor}/credentials"
assert query("SELECT count(*) FROM sn_core.actor_roles WHERE actor_id=%s AND role='admin'", (operator_actor,)) == 1
assert query("SELECT count(*) FROM sn_config.users WHERE email='testoperator' AND actor_id=%s AND active", (operator_actor,)) == 1
query("INSERT INTO sn_core.actors(id,display_name,type,layer,kind,relation) VALUES ('external:test','External','user','human','human','external')")
try:
    query("INSERT INTO sn_config.actor_credentials(actor_id,cred_type,cred_value,scope) VALUES ('external:test','bearer_token','sha256:test','core-api')")
except psycopg2.Error:
    pass
else:
    raise AssertionError("external actor accepted an administrative credential")
fixture_digest = "sha256:" + hashlib.sha256(fixture_token.encode()).hexdigest()
assert query("SELECT cred_value FROM sn_config.actor_credentials WHERE actor_id=%s AND cred_type='bearer_token'", (operator_actor,)) == fixture_digest
try:
    query("INSERT INTO sn_config.actor_credentials(actor_id,cred_type,cred_value,scope) VALUES ((SELECT actor_id FROM sn_core.actor_roles WHERE role='super-admin'),'bearer_token',%s,'admin')", (fixture_digest,))
except psycopg2.Error:
    pass
else:
    raise AssertionError("one bearer proof identified two actors")
try:
    query("UPDATE sn_core.actors SET relation='external' WHERE id=%s", (operator_actor,))
except psycopg2.Error:
    pass
else:
    raise AssertionError("credentialed operator became external")
assert query("SELECT count(*) FROM sn_core.actor_roles WHERE role='admin' AND actor_id IN ('agent:testjohn','agent:testmax')") == 2
assert query("SELECT count(*) FROM sn_core.actor_roles WHERE actor_id='agent:testplain'") == 0
assert query("SELECT actor_id FROM sn_config.users WHERE email='testjohn'") == "agent:testjohn"
assert "ON DELETE RESTRICT" in query("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='actor_roles_actor_id_fkey'")


socket_user = pwd.getpwuid(os.getuid()).pw_name
query("INSERT INTO sn_config.actor_credentials(actor_id,cred_type,cred_value,scope) VALUES (%s,'os_user',%s,'socket')", (operator_actor, socket_user))
original_peer_credentials = auth.peer_credentials
auth.peer_credentials = lambda request: (1, os.getuid(), os.getgid())
try:
    assert auth.peer_operator_identity(Request({"type": "http", "method": "GET", "path": "/api", "headers": []})) == "testoperator"
    query("UPDATE sn_config.agents SET os_user=%s WHERE slug='testjohn'", (socket_user,))
    socket_request = Request({"type": "http", "method": "GET", "path": "/api", "headers": [(b"remote-user", b"admin")]})
    assert get_principal(socket_request, "admin").actor_id == "agent:testjohn"
    query("UPDATE sn_config.agents SET os_user=NULL WHERE slug='testjohn'")
finally:
    auth.peer_credentials = original_peer_credentials
def bearer_request(token):
    return Request({"type": "http", "method": "GET", "path": "/api", "headers": [
        (b"authorization", ("Bearer " + token).encode()), (b"remote-user", b"admin")
    ], "client": ("127.0.0.1", 12345)})
operator = get_principal(bearer_request(fixture_token), "admin")
assert operator.actor_id == operator_actor and operator.strength == "proved"

def tcp_request(method, headers=()):
    return Request({"type": "http", "method": method, "path": "/api/agents", "headers": [
        (b"remote-user", b"admin"), *headers
    ], "client": ("127.0.0.1", 12345)})

assert get_principal(tcp_request("GET"), "admin").actor_kind == "human"
try:
    get_principal(tcp_request("POST"), "admin")
except HTTPException as exc:
    assert exc.status_code == 403 and "asserted Remote-User" in exc.detail
else:
    raise AssertionError("human TCP assertion was admitted to a mutation")
assert query("SELECT count(*) FROM sn_core.events WHERE event_type='auth.human_tcp_assertion_denied'") == 1
try:
    auth.get_actor(tcp_request("POST"), "admin")
except HTTPException as exc:
    assert exc.status_code == 403
else:
    raise AssertionError("direct get_actor route admitted asserted human mutation")
assert query("SELECT count(*) FROM sn_core.events WHERE event_type='auth.human_tcp_assertion_denied'") == 2
try:
    get_principal(Request({"type": "http", "method": "POST", "path": "/api/agents",
                           "headers": [(b"remote-user", b"testoperator")],
                           "client": ("127.0.0.1", 12345)}), "testoperator")
except HTTPException as exc:
    assert exc.status_code == 403
else:
    raise AssertionError("bare operator name bypassed the bearer-token requirement")
os.environ["SN_PROXY_AUTH_SECRET"] = "test-only-session-secret"
assert get_principal(tcp_request("POST", [(b"x-sn-proxy-auth", b"test-only-session-secret")]), "admin").strength == "session"
assert get_principal(Request({"type": "http", "method": "POST", "path": "/api/agents", "headers": [
    (b"authorization", ("Bearer " + fixture_token).encode())
], "client": ("127.0.0.1", 12345)}), "admin").actor_id == operator_actor
os.environ["BUDGET_NOTIFY_ACTOR"] = "admin"
os.environ["BUDGET_NOTIFY_CHANNEL"] = "email"
os.environ["SN_GATEWAY_CORE_API_SECRET"] = "test-only-gateway-proof"
def gateway_request(path, secret="test-only-gateway-proof", method="POST"):
    return Request({"type": "http", "method": method, "path": path, "headers": [
        (b"x-sn-service", b"gateway"),
        (b"x-sn-service-auth", secret.encode()),
        (b"x-sn-on-behalf-of", b"admin"),
    ], "client": ("127.0.0.1", 12345)})
for path in ("/api/messages/send", "/api/budgets/7/notify-exhaustion",
             "/api/limits/notify-turn-cutoff"):
    gateway = get_principal(gateway_request(path))
    assert gateway.actor_id == "user:admin" and gateway.acting_service == "gateway"
    assert gateway.strength == "proved"
for request in (gateway_request("/api/agents"),
                gateway_request("/api/messages/send", "wrong-proof"),
                gateway_request("/api/messages/send", method="GET")):
    try:
        get_principal(request)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("gateway owner proof exceeded its notice allowlist")
from routes.messages import MessageOriginate, gateway_warning_allowed
assert gateway_warning_allowed(MessageOriginate(
    to="admin", channel="email", body="Budget pre-warning: spend is near the cap."))
assert not gateway_warning_allowed(MessageOriginate(
    to="outside@example.test", channel="email", body="Budget pre-warning: fake"))
assert not gateway_warning_allowed(MessageOriginate(
    to="admin", channel="email", body="Budget pre-warning: fake", cc=["outside@example.test"]))
try:
    get_principal(bearer_request("invalid-token"), "admin")
except HTTPException as exc:
    assert exc.status_code == 403
else:
    raise AssertionError("invalid bearer fell through to Remote-User: admin")

with psycopg2.connect(dbname="smartnode", user="smartnode") as con:
    with con.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("INSERT INTO sn_config.agents(tenant_id,slug,title,capability) VALUES (1,'newconfig','New Config','configurator'),(1,'newplain','New Plain','')")
        _ensure_agent_identity(cur, "newconfig", "New Config", 1)
        _ensure_agent_identity(cur, "newplain", "New Plain", 1)
assert query("SELECT role FROM sn_core.actor_roles WHERE actor_id='agent:newconfig'") == "admin"
assert query("SELECT count(*) FROM sn_core.actor_roles WHERE actor_id='agent:newplain'") == 0

desk_id = str(uuid.uuid4())
query("INSERT INTO sn_core.desks(id, tenant_id, owner_user_id) VALUES (%s,1,(SELECT id FROM sn_config.users WHERE email='admin'))", (desk_id,))

owner = Principal("admin", 1, 1, "agent", actor_id=owner_actor, actor_kind="human")
john = Principal("testjohn", 2, 1, "agent", actor_id="agent:testjohn", actor_kind="agent")
plain = Principal("testplain", 3, 1, "super-admin", actor_id="agent:testplain", actor_kind="agent")
assert check_permission(john, "agents.create").allowed
denied = check_permission(plain, "agents.create")
assert not denied.allowed and "user" in denied.reason and "agents.create" in denied.reason
assert not check_permission(john, "desks.update", f"desk:{desk_id}").allowed
assert check_permission(owner, "desks.update", f"desk:{desk_id}").axis_bypass
query("INSERT INTO sn_core.desk_participants(desk_id, actor_id) VALUES (%s,'agent:testjohn')", (desk_id,))
assert check_permission(john, "desks.update", f"desk:{desk_id}").allowed
assert query("SELECT count(*) FROM sn_config.permissions WHERE name IN ('desks.join', 'desks.leave')") == 0

app = FastAPI()
@app.post("/toy/create", dependencies=[Depends(requires("agents.create"))])
def create():
    return {"ok": True}
app.dependency_overrides[get_principal] = lambda: plain
with TestClient(app) as client:
    response = client.post("/toy/create")
    assert response.status_code == 403, response.text
    assert "agents.create" in response.text and "user" in response.text
app.dependency_overrides[get_principal] = lambda: john
with TestClient(app) as client:
    assert client.post("/toy/create").status_code == 200

for permission, object_fetcher, expected in [
    ("not.in.catalogue", None, "absent"),
    ("desks.update", None, "requires object"),
]:
    toy = FastAPI()
    toy.add_api_route("/toy/check", lambda: {}, dependencies=[Depends(requires(permission, object_fetcher))])
    try:
        validate_routes(toy)
    except RuntimeError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"startup admitted {permission}")

# If a dynamic route escaped the startup scan, a missing permission is a
# server catalogue error rather than a misleading role denial.
drifted = FastAPI()
drifted.add_api_route("/toy/drifted", lambda: {}, dependencies=[Depends(requires("not.in.catalogue"))])
drifted.dependency_overrides[get_principal] = lambda: owner
with TestClient(drifted) as client:
    assert client.get("/toy/drifted").status_code == 500

unmarked = FastAPI()
unmarked.add_api_route("/toy/unmarked", lambda: {})
try:
    validate_routes(unmarked)
except RuntimeError as exc:
    assert "no permission declared" in str(exc)
else:
    raise AssertionError("startup admitted an undeclared route")

# Event shape and owner notifications use existing sn_core.events columns.
query("INSERT INTO sn_config.user_channels(user_id,tenant_id,channel_slug,address,enabled) VALUES ((SELECT id FROM sn_config.users WHERE email='admin'),1,'telegram','123',true)")
query("INSERT INTO sn_config.user_channels(user_id,tenant_id,channel_slug,address,enabled) VALUES ((SELECT id FROM sn_config.users WHERE email='admin'),1,'web-chat','web:owner',true)")
query("INSERT INTO sn_core.events(actor,event_type,entity_type,entity_id,payload) VALUES ('agent:testjohn','desk.admin_joined','desk',%s,'{\"reason\":\"incident\"}'::jsonb)", (desk_id,))
assert query("SELECT count(*) FROM sn_core.messages WHERE metadata->>'source'='desk.admin_joined'") == 2
assert query("SELECT count(*) FROM sn_core.messages WHERE channel_type='web-chat' AND thread_id=%s", (f"desk:{desk_id}",)) == 1

# A broken notification queue cannot undo the join event. The trigger catches
# the failed enqueue separately for each channel.
query("ALTER TABLE sn_core.messages ADD CONSTRAINT test_no_join_notice CHECK (metadata->>'source' IS DISTINCT FROM 'desk.admin_joined') NOT VALID")
query("INSERT INTO sn_core.events(actor,event_type,entity_type,entity_id,payload) VALUES ('agent:testjohn','desk.admin_joined','desk',%s,'{\"reason\":\"queue down\"}'::jsonb)", (desk_id,))
assert query("SELECT count(*) FROM sn_core.events WHERE event_type='desk.admin_joined' AND payload->>'reason'='queue down'") == 1
query("ALTER TABLE sn_core.messages DROP CONSTRAINT test_no_join_notice")

# A bearer credential is a role grant: only a session-proved super-admin can
# issue, rotate, or revoke it. No plaintext is stored or written to events.
credentials_app.dependency_overrides[get_principal] = lambda: Principal(
    "testjohn", 2, 1, "agent", actor_id="agent:testjohn", strength="session"
)
with TestClient(credentials_app) as client:
    response = client.post(
        operator_credentials,
        json={"cred_type": "bearer_token"},
    )
    assert response.status_code == 403, response.text

credentials_app.dependency_overrides[get_principal] = lambda: Principal(
    "admin", 1, 1, "super-admin", actor_id=owner_actor, strength="proved"
)
os.environ["SN_PROXY_AUTH_SECRET"] = "test-only-session-secret"
with TestClient(credentials_app) as client:
    response = client.post(
        operator_credentials,
        json={"cred_type": "bearer_token"},
    )
    assert response.status_code == 403, response.text

credentials_app.dependency_overrides[get_principal] = lambda: Principal(
    "admin", 1, 1, "super-admin", actor_id=owner_actor, strength="session"
)
with TestClient(credentials_app) as client:
    response = client.delete(operator_credentials + "/bearer_token")
    assert response.status_code == 204, response.text
    response = client.post(
        operator_credentials,
        json={"cred_type": "bearer_token"},
    )
    assert response.status_code == 201, response.text
    first_token = response.json()["token"]
    assert query("SELECT cred_value FROM sn_config.actor_credentials WHERE actor_id=%s AND cred_type='bearer_token'", (operator_actor,)) == "sha256:" + hashlib.sha256(first_token.encode()).hexdigest()
    assert query("SELECT count(*) FROM sn_core.events WHERE event_type LIKE 'actor.credential.%%' AND payload::text LIKE %s", ("%" + first_token + "%",)) == 0
    assert get_principal(bearer_request(first_token), "admin").actor_id == operator_actor
    response = client.post(operator_credentials + "/bearer_token/rotate")
    assert response.status_code == 200, response.text
    second_token = response.json()["token"]
    assert second_token != first_token
    try:
        get_principal(bearer_request(first_token), "admin")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("rotated bearer token still works")
    assert get_principal(bearer_request(second_token), "admin").actor_id == operator_actor
    assert client.delete(operator_credentials + "/bearer_token").status_code == 204
    try:
        get_principal(bearer_request(second_token), "admin")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("revoked bearer token still works")

print("PASS: 145/306, capability roles, two axes, startup refusal, notification")
