"""Endpoint coverage for every core-api route.

Strategy: integration tests against the live DB via TestClient. Each test that
creates a row cleans it up (delete / archive) so the suite is repeatable.
Slugs are uniquified per test to avoid colliding with seeded golden data.

Secrets endpoints need sops+age+an encrypted store, which is NOT configured in
the local stack. Those tests assert validation behaviour and the documented
"store unavailable" path rather than faking a keystore.
"""

import pytest


@pytest.fixture
def as_owner():
    """Call as the node's owner: the account whose actor holds the super-admin
    role row. A bare TestClient presents no identity, and the system and cron
    routes now need a caller whose role holds their permission."""
    from auth import Principal, get_principal
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


# --------------------------------------------------------------------------- system

def test_system_status(client, as_owner):
    r = client.get("/api/system/status")
    assert r.status_code == 200
    body = r.json()
    assert body["db"]["reachable"] is True
    assert "registry" in body


# --------------------------------------------------------------------------- events

def test_events_list(client):
    r = client.get("/api/events")
    assert r.status_code == 200
    assert isinstance(r.json()["events"], list)


def test_events_create_ok(client):
    # actor user:panel is seeded as a sn_core.actors row (FK target).
    r = client.post("/api/events", json={
        "actor": "user:panel",
        "event_type": "test.event",
        "entity_type": "test",
        "entity_id": "x",
        "payload": {"k": 1},
    })
    assert r.status_code == 201
    body = r.json()
    assert "id" in body and "ts" in body


def test_events_create_validation(client):
    r = client.post("/api/events", json={"actor": "", "event_type": "x"})
    assert r.status_code == 422


def test_events_autoregisters_new_actor(client, uid):
    # A never-seen actor must self-register (no events_actor_fkey violation).
    actor = f"agent:{uid}"
    r = client.post("/api/events", json={"actor": actor, "event_type": "test.autoreg"})
    assert r.status_code == 201


# --------------------------------------------------------------------------- agents

def test_agents_full_lifecycle(client, uid):
    slug = uid
    # create
    r = client.post("/api/agents", json={
        "slug": slug, "title": "Test Agent", "role": "tester",
        "model": "sonnet", "specialist_body": "# Test\n\nrole text\n",
    })
    assert r.status_code == 201, r.text
    try:
        # get
        r = client.get(f"/api/agents/{slug}")
        assert r.status_code == 200
        assert r.json()["slug"] == slug
        # get with body
        r = client.get(f"/api/agents/{slug}?body=true")
        assert r.status_code == 200
        assert "role text" in (r.json().get("body_md") or "")
        # list contains it
        r = client.get("/api/agents")
        assert any(a["slug"] == slug for a in r.json()["agents"])
        # update
        r = client.put(f"/api/agents/{slug}", json={"role": "updated-role"})
        assert r.status_code == 200
        assert r.json()["role"] == "updated-role"
        # grants: empty by default
        r = client.get(f"/api/agents/{slug}/grants")
        assert r.status_code == 200
        assert r.json()["grants"] == {}
        # grants: set
        r = client.put(f"/api/agents/{slug}/grants", json={"grants": {"mesh-cli": "rw-"}})
        assert r.status_code == 200
        r = client.get(f"/api/agents/{slug}/grants")
        assert r.json()["grants"]["mesh-cli"] == "rw-"
        # system prompt
        r = client.get(f"/api/agents/{slug}/system-prompt")
        assert r.status_code == 200
        r = client.get(f"/api/agents/{slug}/system-prompt?fmt=json")
        assert r.status_code == 200
    finally:
        r = client.delete(f"/api/agents/{slug}")
        assert r.status_code == 204


def test_agent_update_rerenders_srv(client, uid):
    """Updating the DB row must re-render the /srv data projection."""
    import os
    slug = uid
    r = client.post("/api/agents", json={
        "slug": slug, "title": "RT Agent", "role": "before", "model": "sonnet",
        "specialist_body": "# RT\n",
    })
    assert r.status_code == 201, r.text
    try:
        yaml_path = f"/srv/agents/{slug}/agent.yaml"
        assert os.path.isfile(yaml_path)
        before = open(yaml_path).read()
        assert "role: before" in before and "model: sonnet" in before
        # update role + model
        r = client.put(f"/api/agents/{slug}", json={"role": "after", "model": "opus"})
        assert r.status_code == 200
        after = open(yaml_path).read()
        assert "role: after" in after and "model: opus" in after
    finally:
        assert client.delete(f"/api/agents/{slug}").status_code == 204


def test_agent_permissions_persist_and_render(client, uid):
    """Access/permission fields round-trip through the DB and into agent.yaml."""
    import os
    slug = uid
    r = client.post("/api/agents", json={
        "slug": slug, "title": "Perm Agent", "model": "sonnet",
        "specialist_body": "# Perm\n",
        "os_user": f"agent-{slug}",
        "schedule": "*/10 * * * *",
        "os_groups": ["agents", "docker"],
        "fs_read": ["/srv/shared"],
        "fs_write": [f"/srv/agents/{slug}"],
        "forbidden_fs": ["/etc"],
        "forbidden_behavior": ["send_email", "delete_files"],
    })
    assert r.status_code == 201, r.text
    try:
        a = client.get(f"/api/agents/{slug}").json()
        assert a["os_user"] == f"agent-{slug}"
        assert a["schedule"] == "*/10 * * * *"
        assert a["os_groups"] == ["agents", "docker"]
        assert a["forbidden_behavior"] == ["send_email", "delete_files"]
        assert a["fs_write"] == [f"/srv/agents/{slug}"]

        y = open(f"/srv/agents/{slug}/agent.yaml").read()
        assert f"os_user: agent-{slug}" in y
        assert "send_email" in y and "delete_files" in y
        assert "directories:" in y

        # Update a permission field -> re-rendered.
        r = client.put(f"/api/agents/{slug}", json={"forbidden_behavior": ["modify_firewall"]})
        assert r.status_code == 200
        assert client.get(f"/api/agents/{slug}").json()["forbidden_behavior"] == ["modify_firewall"]
        assert "modify_firewall" in open(f"/srv/agents/{slug}/agent.yaml").read()
    finally:
        assert client.delete(f"/api/agents/{slug}").status_code == 204


def test_agent_grants_render_into_agent_yaml(client, uid):
    """Setting tool grants re-renders agent.yaml tools[]."""
    slug = uid
    assert client.post("/api/agents", json={
        "slug": slug, "title": "Tooled", "specialist_body": "# T\n",
    }).status_code == 201
    try:
        r = client.put(f"/api/agents/{slug}/grants", json={"grants": {"planner": "rw-", "fetch": "r--"}})
        assert r.status_code == 200
        y = open(f"/srv/agents/{slug}/agent.yaml").read()
        assert "planner" in y and "fetch" in y
    finally:
        assert client.delete(f"/api/agents/{slug}").status_code == 204


def _specialist_slugs(client):
    d = client.get("/api/specialists").json()
    xs = d if isinstance(d, list) else d.get("specialists", [])
    return {s["slug"] for s in xs}


def test_delete_agent_archives_inline_specialist(client, uid):
    slug = uid
    r = client.post("/api/agents", json={
        "slug": slug, "title": "Inline", "specialist_body": "# body\n", "model": "sonnet",
    })
    assert r.status_code == 201
    assert slug in _specialist_slugs(client)   # inline specialist created with same slug
    assert client.delete(f"/api/agents/{slug}").status_code == 204
    assert slug not in _specialist_slugs(client)   # archived alongside the agent


def test_delete_agent_keeps_shared_specialist(client, uid):
    sp = uid + "sp"
    ag = uid + "ag"
    # shared specialist (different slug from the agent)
    assert client.post("/api/specialists", json={"slug": sp, "name": "Shared"}).status_code == 201
    try:
        r = client.post("/api/agents", json={"slug": ag, "title": "A", "specialist_slug": sp})
        assert r.status_code == 201
        assert client.delete(f"/api/agents/{ag}").status_code == 204
        assert sp in _specialist_slugs(client)   # shared specialist survives
    finally:
        client.delete(f"/api/specialists/{sp}")


def test_agents_bad_slug(client):
    r = client.post("/api/agents", json={"slug": "Bad Slug", "title": "x"})
    assert r.status_code in (400, 422)


def test_agents_get_missing(client, uid):
    r = client.get(f"/api/agents/{uid}")
    assert r.status_code == 404


# --------------------------------------------------------------------------- specialists

def test_specialists_lifecycle(client, uid):
    slug = uid
    r = client.post("/api/specialists", json={"slug": slug, "name": "Spec", "body_md": "body"})
    assert r.status_code == 201, r.text
    try:
        assert client.get("/api/specialists/library").status_code == 200
        assert client.get("/api/specialists").status_code == 200
        r = client.get(f"/api/specialists/{slug}")
        assert r.status_code == 200 and r.json()["slug"] == slug
        r = client.put(f"/api/specialists/{slug}", json={"name": "Spec2"})
        assert r.status_code == 200
    finally:
        assert client.delete(f"/api/specialists/{slug}").status_code == 204


# --------------------------------------------------------------------------- tools

def test_tools_lifecycle(client, uid):
    name = uid
    r = client.post("/api/tools", json={"name": name, "kind": "builtin", "title": "T"})
    assert r.status_code == 201, r.text
    try:
        assert client.get("/api/tools").status_code == 200
        r = client.get(f"/api/tools/{name}")
        assert r.status_code == 200 and r.json()["name"] == name
        r = client.put(f"/api/tools/{name}", json={"kind": "builtin", "title": "T2"})
        assert r.status_code == 200 and r.json()["title"] == "T2"
    finally:
        assert client.delete(f"/api/tools/{name}").status_code == 200


def test_tool_labels_say_what_a_tool_does(client, uid, as_owner):
    """A chat shows "doing" while an agent uses a tool, read from the registry;
    a later edit that leaves it out keeps it."""
    r = client.post("/api/tools", json={"name": uid, "kind": "builtin", "title": "T", "doing": "Doing T"})
    assert r.status_code == 201, r.text
    try:
        assert r.json()["doing"] == "Doing T"
        labels = client.get("/api/tools/labels").json()["labels"]
        assert labels[uid] == {"kind": "builtin", "title": "T", "doing": "Doing T"}
        r = client.put(f"/api/tools/{uid}", json={"kind": "builtin", "title": "T2"})
        assert r.status_code == 200 and r.json()["doing"] == "Doing T"
    finally:
        assert client.delete(f"/api/tools/{uid}").status_code == 200


def test_tools_bad_name(client):
    r = client.post("/api/tools", json={"name": "X", "kind": "builtin", "title": "T"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- integrations

def test_integrations_lifecycle(client, uid):
    slug = uid
    r = client.post("/api/integrations", json={"slug": slug, "label": "Int"})
    assert r.status_code in (200, 201), r.text
    try:
        assert client.get("/api/integrations").status_code == 200
        r = client.get(f"/api/integrations/{slug}")
        assert r.status_code == 200 and r.json()["slug"] == slug
        assert client.get(f"/api/integrations/{slug}/tools").status_code == 200
        r = client.put(f"/api/integrations/{slug}", json={"label": "Int2"})
        assert r.status_code == 200 and r.json()["label"] == "Int2"
    finally:
        assert client.delete(f"/api/integrations/{slug}").status_code == 200


# --------------------------------------------------------------------------- runtimes

def test_runtimes_lifecycle(client, uid):
    slug = uid
    r = client.post("/api/runtimes", json={"slug": slug, "label": "RT", "kind": "other"})
    assert r.status_code in (200, 201), r.text
    try:
        assert client.get("/api/runtimes").status_code == 200
        r = client.get(f"/api/runtimes/{slug}")
        assert r.status_code == 200 and r.json()["slug"] == slug
        r = client.put(f"/api/runtimes/{slug}", json={"label": "RT2", "kind": "other"})
        assert r.status_code == 200 and r.json()["label"] == "RT2"
    finally:
        assert client.delete(f"/api/runtimes/{slug}").status_code == 200


# --------------------------------------------------------------------------- policies

def test_policies_lifecycle(client, uid):
    slug = uid
    r = client.post("/api/policies", json={
        "slug": slug, "category": "safety", "title": "P", "body_md": "rule",
    })
    assert r.status_code == 201, r.text
    try:
        assert client.get("/api/policies").status_code == 200
        r = client.get(f"/api/policies/{slug}")
        assert r.status_code == 200 and r.json()["slug"] == slug
        r = client.put(f"/api/policies/{slug}", json={
            "category": "safety", "title": "P2", "body_md": "rule2",
        })
        assert r.status_code == 200 and r.json()["title"] == "P2"
    finally:
        assert client.delete(f"/api/policies/{slug}").status_code == 200


# --------------------------------------------------------------------------- crons

def _run_report(path: str, body: dict):
    """A cron run report, sent the way the route resolves its caller.

    The run-report routes are admitted by trusted origin and resolve identity
    themselves, so the principal override the other calls use does not reach
    them. This names the owner from loopback, and the real identity code and
    the owner's real role row decide.
    """
    import asyncio

    import httpx
    from db import cursor
    from main import app

    with cursor() as cur:
        cur.execute(
            "SELECT u.email FROM sn_config.users u "
            "JOIN sn_core.actor_roles r ON r.actor_id = u.actor_id "
            "WHERE r.role = 'super-admin' AND u.active")
        owner = cur.fetchone()["email"]

    async def _go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as c:
            return await c.post(path, json=body, headers={"Remote-User": owner})

    return asyncio.run(_go())


def test_crons_lifecycle(client, uid, as_owner):
    slug = "job-" + uid
    r = client.post("/api/crons", json={
        "slug": slug, "schedule": "*/5 * * * *", "command": "echo hi", "description": "test cron",
    })
    assert r.status_code in (200, 201), r.text
    body = r.json()
    cron_id = body["id"]
    assert body["slug"] == slug
    try:
        assert client.get("/api/crons").status_code == 200
        r = client.get(f"/api/crons/{cron_id}")
        assert r.status_code == 200 and r.json()["id"] == cron_id
        r = client.put(f"/api/crons/{cron_id}", json={"enabled": False})
        assert r.status_code == 200
        # runs by id
        r = _run_report(f"/api/crons/{cron_id}/runs", {
            "started_at": "2026-05-29T00:00:00Z", "exit_code": 0, "output_snippet": "ok",
        })
        assert r.status_code == 201, r.text
        assert client.get(f"/api/crons/{cron_id}/runs").status_code == 200
        # runs by job slug (cron-runner path) resolves to the same cron
        r = _run_report(f"/api/crons/by-job/{slug}/runs", {
            "started_at": "2026-05-29T00:00:00Z", "exit_code": 0,
        })
        assert r.status_code == 201, r.text
        assert r.json()["cron_id"] == cron_id
    finally:
        assert client.delete(f"/api/crons/{cron_id}").status_code == 200


def test_cron_slug_autoderived(client, as_owner):
    r = client.post("/api/crons", json={"schedule": "0 2 * * *", "command": "echo derive"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    try:
        assert body["slug"] == f"cron-{body['id']}"   # derived when omitted
    finally:
        client.delete(f"/api/crons/{body['id']}")


# --------------------------------------------------------------------------- mcp

def test_mcp_lifecycle(client, uid):
    slug = uid
    r = client.post("/api/mcp/servers", json={
        "slug": slug, "name": "MCP", "transport": "stdio",
        "config": {"command": "echo", "args": []},
    })
    assert r.status_code in (200, 201), r.text
    try:
        assert client.get("/api/mcp/servers").status_code == 200
        r = client.get(f"/api/mcp/servers/{slug}")
        assert r.status_code == 200 and r.json()["slug"] == slug
        r = client.put(f"/api/mcp/servers/{slug}", json={"name": "MCP2"})
        assert r.status_code == 200 and r.json()["name"] == "MCP2"
        # bulk tools upsert
        r = client.post(f"/api/mcp/servers/{slug}/tools", json={
            "tools": [{"name": "do_thing", "description": "x"}],
        })
        assert r.status_code in (200, 201), r.text
        assert client.get(f"/api/mcp/servers/{slug}/tools").status_code == 200
    finally:
        assert client.delete(f"/api/mcp/servers/{slug}").status_code == 200


# --------------------------------------------------------------------------- secrets (store not configured locally)

def test_secrets_refs_need_an_operator(client):
    # The store is the database now; what gates the listing is identity. An
    # anonymous TCP client is refused in the route's words, not with a traceback.
    r = client.get("/api/secrets/refs")
    assert r.status_code == 403
    assert r.json()["detail"] == "secret access denied"


def test_secrets_set_validation(client):
    # Bad ref pattern is rejected by pydantic before sops is ever called.
    r = client.post("/api/secrets", json={"ref": "bad ref!", "value": "x"})
    assert r.status_code == 422


def test_secrets_resolve_invalid_ref(client):
    r = client.get("/api/secrets/resolve", params={"ref": "bad ref!"})
    # 400 invalid ref (trusted testclient) or 403 (untrusted) -- both are defined paths.
    assert r.status_code in (400, 403)
