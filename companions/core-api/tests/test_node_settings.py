"""The node's own settings (routes/node_settings.py, the panel's Settings page).

Against a throwaway database built from the tree's bootstrap files, with the
release and auth account files written to a temp dir:

1. access.read is the owner's: an admin and a user are refused /access;
   /about is for anyone with system.status.read.
2. /about: the tenant's name, the domain, the release file's three fields, and
   the node pause.
3. /access: each sign-in account with the role of the actor it maps to, the
   actors merged into it as its other names, and never anything from the
   account file but the name, role and creation date.
4. Roles: holders (an alias is not a second holder) and permissions with their
   descriptions; super-admin first.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    import auth
    from routes import node_settings

    d = tmp_path_factory.mktemp("node")
    (d / "release").write_text("release=9.9.9\ncommit=abc1234\ninstalled_at=2026-10-13T13:03:08-05:00\nother=x\n")
    (d / "users.json").write_text(json.dumps({
        "admin": {"role": "superadmin", "created": "2026-08-12", "password": "HASH-NEVER-SHOWN"},
        "guest": {"role": "user", "totp": "SECRET-NEVER-SHOWN"},
    }))
    mp = pytest.MonkeyPatch()
    mp.setattr(node_settings, "RELEASE_FILE", str(d / "release"))
    mp.setattr(node_settings, "AUTH_USERS_FILE", str(d / "users.json"))
    mp.setenv("SMART_NODE_DOMAIN", "example.lan")
    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        owner = people["owner"].actor_id
        sql("INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation, canonical_id) VALUES "
            f"('owner-mail@example.com', 'owner-mail@example.com', 'user', 'system', 'human', 'internal', '{owner}')")
        sql("INSERT INTO sn_core.actor_roles (actor_id, role, source) VALUES ('owner-mail@example.com', 'admin', 'migration')")
        if owner != "user:admin":
            sql("INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation, canonical_id) VALUES "
                f"('user:admin', 'user:admin', 'user', 'human', 'human', 'internal', '{owner}') ON CONFLICT (id) DO NOTHING")
        app = FastAPI()
        app.include_router(node_settings.router)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name):
            who["p"] = people[name]
            return client

        yield as_, sql, owner
    mp.undo()


def test_only_the_owner_reads_access(node):
    as_, _, _ = node
    for name in ("admin", "user", "installer"):
        assert as_(name).get("/api/node/access").status_code == 403, name
    assert as_("owner").get("/api/node/access").status_code == 200
    assert as_("user").get("/api/node/about").status_code == 200, "about is system.status.read"


def test_about_says_name_domain_release_and_pause(node):
    as_, sql, _ = node
    b = as_("owner").get("/api/node/about").json()
    assert b["domain"] == "example.lan"
    assert (b["release"], b["commit"], b["installed_at"]) == ("9.9.9", "abc1234", "2026-10-13T13:03:08-05:00")
    assert "other" not in b
    assert b["name"] and b["paused"] is False
    sql("UPDATE sn_config.node_control SET node_paused = true WHERE id = true")
    try:
        assert as_("owner").get("/api/node/about").json()["paused"] is True
    finally:
        sql("UPDATE sn_config.node_control SET node_paused = false WHERE id = true")


def test_logins_carry_the_actor_role_and_nothing_secret(node):
    as_, _, owner = node
    r = as_("owner").get("/api/node/access")
    assert "NEVER-SHOWN" not in r.text
    by = {x["login"]: x for x in r.json()["logins"]}
    assert set(by) == {"admin", "guest"}
    assert by["admin"]["actor"] == owner and by["admin"]["role"] == "super-admin"
    assert "owner-mail@example.com" in by["admin"]["other_names"], "a merged actor is another name"
    assert by["admin"]["auth_role"] == "superadmin"
    assert by["guest"]["actor"] is None and by["guest"]["role"] == "user", "no actor acts as user"


def test_roles_list_holders_and_permissions(node):
    as_, _, owner = node
    roles = as_("owner").get("/api/node/access").json()["roles"]
    assert roles[0]["role"] == "super-admin"
    sa = roles[0]
    assert [h["id"] for h in sa["holders"]] == [owner], "an alias is not a second holder"
    names = {p["name"]: p["description"] for p in sa["permissions"]}
    assert "access.read" in names and names["access.read"], "permissions carry their description"
    by = {r["role"]: r for r in roles}
    assert "access.read" not in {p["name"] for p in by["admin"]["permissions"]}, "access.read is the owner's alone"
    assert "agent:permadmin" in {h["id"] for h in by["admin"]["holders"]}
    assert "owner-mail@example.com" not in {h["id"] for h in by["admin"]["holders"]}, "a merged actor's own row does not count"
