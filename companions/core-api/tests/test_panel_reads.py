"""Coverage for the live panel read endpoints (the ones that replaced the static
/data/*.json snapshots) and the tenant-scoping authorization layer.

Two kinds of test here:
  * get_principal -- called directly to exercise the real DB resolution + 403 path
    (TestClient always looks 'untrusted' so it can't present arbitrary identities).
  * endpoint shape + tenant scoping -- via TestClient, using dependency_overrides
    to stand in different principals.
"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from auth import Principal, get_principal


# --------------------------------------------------------------------------- helpers

def _request(remote_user=None, ip="127.0.0.1"):
    headers = []
    if remote_user is not None:
        headers.append((b"remote-user", remote_user.encode()))
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": headers, "client": (ip, 12345), "scheme": "http",
        "server": ("test", 80),
    }
    return Request(scope)


def _member(tenant_id):
    return Principal(username="m", user_id=-1, tenant_id=tenant_id, role="member")


def _superadmin():
    return Principal(username="s", user_id=-1, tenant_id=1, role="super-admin")


@pytest.fixture
def as_principal():
    """Stand in a given Principal for the body of a test, then restore."""
    from main import app

    def _set(principal):
        app.dependency_overrides[get_principal] = lambda: principal

    yield _set
    app.dependency_overrides.pop(get_principal, None)


@pytest.fixture
def as_owner():
    """Call as the node's owner: the account whose actor holds the super-admin
    role row. A bare TestClient presents no identity, and the system and cron
    routes now need a caller whose role holds their permission."""
    from db import cursor
    from main import app

    with cursor() as cur:
        cur.execute(
            "SELECT u.id, u.email, u.tenant_id, u.actor_id FROM sn_config.users u "
            "JOIN sn_core.actor_roles r ON r.actor_id = u.actor_id "
            "WHERE r.role = 'super-admin' AND u.active")
        row = cur.fetchone()
    assert row is not None, "this database has no owner account with a role row"
    app.dependency_overrides[get_principal] = lambda: Principal(
        username=row["email"], user_id=row["id"], tenant_id=row["tenant_id"],
        role="super-admin", actor_id=row["actor_id"], actor_kind="human")
    yield
    app.dependency_overrides.pop(get_principal, None)


# An admin of one tenant: a real actor with a real admin role row. It holds
# the permission to render the crontab and is not cross-tenant.
TENANT_ADMIN_ACTOR = "user:panel-reads-tenant-admin"


@pytest.fixture
def tenant_admin():
    from db import cursor

    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation) "
            "VALUES (%s, 'tenant admin', 'user', 'human', 'human', 'internal') "
            "ON CONFLICT (id) DO NOTHING", (TENANT_ADMIN_ACTOR,))
        cur.execute(
            "INSERT INTO sn_core.actor_roles (actor_id, role, source) "
            "VALUES (%s, 'admin', 'owner_grant') ON CONFLICT (actor_id) DO NOTHING",
            (TENANT_ADMIN_ACTOR,))
    return Principal(username="m", user_id=-1, tenant_id=1, role="member",
                     actor_id=TENANT_ADMIN_ACTOR, actor_kind="human")


# --------------------------------------------------------- get_principal (real auth)

def test_principal_unknown_identity_is_rejected():
    # Trusted source (loopback) but an identity with no sn_config.users row -> 403.
    with pytest.raises(HTTPException) as ei:
        get_principal(_request(remote_user="nobody@nowhere"), "nobody@nowhere")
    assert ei.value.status_code == 403


def test_principal_resolves_seeded_account():
    # 'anonymous' is the local-stack stand-in seeded as a tenant-1 super-admin.
    p = get_principal(_request(remote_user="anonymous"), "anonymous")
    assert p.tenant_id == 1
    assert p.role == "super-admin"
    assert p.cross_tenant is True


def test_principal_member_is_not_cross_tenant():
    assert _member(1).cross_tenant is False


# --------------------------------------------------------- endpoint shapes

def test_ai_status_shape(client):
    r = client.get("/api/ai/status")
    assert r.status_code == 200
    b = r.json()
    for k in ("summary", "providers", "budgets", "sessions", "generated_at"):
        assert k in b
    assert isinstance(b["providers"], list)
    assert isinstance(b["sessions"], list)
    for session in b["sessions"]:
        assert "requests" in session
        assert isinstance(session["requests"], list)
    # Gateway-runtime fields are reported honestly, not fabricated.
    assert b["summary"]["status"] == "unknown"
    assert b["summary"]["cache_hit_pct"] is None


def test_messages_shape(client, as_owner):
    r = client.get("/api/messages")
    assert r.status_code == 200
    assert isinstance(r.json()["messages"], list)


def test_messages_limit_validation(client, as_owner):
    assert client.get("/api/messages?limit=0").status_code == 400
    assert client.get("/api/messages?limit=99999").status_code == 400


def test_overview_shape(client, as_owner):
    r = client.get("/api/system/overview")
    assert r.status_code == 200
    b = r.json()
    for k in ("uptime_seconds", "load", "mem", "disk", "containers", "events_24h"):
        assert k in b
    assert isinstance(b["events_24h"], int)


def test_services_shape(client, as_owner):
    # Served from the host collector's services.json. On a host with
    # no collector the answer is an honest 503, not an empty 200.
    r = client.get("/api/system/services")
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        b = r.json()
        assert "summary" in b and "companions" in b and "collector" in b
        assert b["summary"]["companions_total"] == len(b["companions"])


def test_crons_panel_shape(client, as_owner):
    r = client.get("/api/crons/panel")
    assert r.status_code == 200
    b = r.json()
    assert "timers" in b and "crontabs" in b and "summary" in b
    assert b["summary"]["crontab_entries"] == len(b["crontabs"])


# --------------------------------------------------------- tenant scoping

def test_ai_status_tenant_scope(client, as_principal):
    # super-admin sees the golden-seeded session; a member of an empty tenant sees none.
    as_principal(_superadmin())
    assert len(client.get("/api/ai/status").json()["sessions"]) >= 1

    as_principal(_member(999999))
    scoped = client.get("/api/ai/status").json()
    assert scoped["sessions"] == []
    assert scoped["summary"]["active_sessions"] == 0


def test_events_tenant_scope(client, as_principal):
    as_principal(_superadmin())
    assert len(client.get("/api/events").json()["events"]) >= 1

    as_principal(_member(999999))
    assert client.get("/api/events").json()["events"] == []


def test_messages_tenant_scope(client, as_principal):
    # An empty tenant must never see another tenant's messages.
    as_principal(_member(999999))
    assert client.get("/api/messages").json()["messages"] == []


# --------------------------------------------------------- crontab render (node action)

def test_crons_render_requires_cross_tenant(client, as_principal, tenant_admin):
    # Projecting the node crontab is node-wide -> an admin of one tenant holds
    # the permission and is still rejected, by the handler's own rule.
    as_principal(tenant_admin)
    r = client.post("/api/crons/render")
    assert r.status_code == 403
    assert "cross-tenant" in r.json()["detail"]


def test_crons_render_is_refused_without_a_role(client, as_principal):
    # A plain member has no role row; the permission layer refuses it by name.
    as_principal(_member(1))
    r = client.post("/api/crons/render")
    assert r.status_code == 403
    assert "'user'" in r.json()["detail"] and "'crons.render'" in r.json()["detail"]


def test_crons_render_superadmin_ok(client, as_principal):
    as_principal(_superadmin())
    r = client.post("/api/crons/render")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "path" in body and isinstance(body["jobs"], int)
