"""References registry: CRUD + tenant isolation.

References are the first registry built tenant-scoped from the start (DB column
tenant_id + per-tenant unique slug), with the caller's tenant taken from
get_principal. These tests drive the API as different principals via
dependency_overrides and clean up the rows they create.
"""

import pytest

from auth import Principal, get_principal


def _member(tenant_id):
    return Principal(username=f"m{tenant_id}", user_id=-1, tenant_id=tenant_id, role="member")


@pytest.fixture
def as_principal():
    from main import app

    def _set(principal):
        app.dependency_overrides[get_principal] = lambda: principal

    yield _set
    app.dependency_overrides.pop(get_principal, None)


@pytest.fixture(autouse=True)
def cleanup_test_refs():
    """Hard-delete any references created by these tests (slug prefix test-)."""
    yield
    from db import cursor
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_config.agent_references WHERE slug LIKE 'test-%'")


# --------------------------------------------------------------- CRUD (super-admin)

def test_references_crud_lifecycle(client):
    payload = {"key": "test-brand", "title": "Brand voice", "target_kind": "doci",
               "target_id": "abc-123", "required": True}
    r = client.post("/api/references", json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["key"] == "test-brand"
    assert r.json()["created_by"]  # actor recorded from principal

    assert client.get("/api/references/test-brand").status_code == 200

    keys = {x["key"] for x in client.get("/api/references").json()["references"]}
    assert "test-brand" in keys

    r = client.put("/api/references/test-brand",
                   json={"title": "Brand voice v2", "target_kind": "url",
                         "target_id": "https://example.com", "required": False})
    assert r.status_code == 200
    assert r.json()["title"] == "Brand voice v2"
    assert r.json()["target_kind"] == "url"

    assert client.delete("/api/references/test-brand").status_code == 200
    assert client.get("/api/references/test-brand").status_code == 404


def test_references_duplicate_key_conflicts(client):
    base = {"title": "X", "target_kind": "doci", "target_id": "g1"}
    assert client.post("/api/references", json={**base, "key": "test-dup"}).status_code == 201
    assert client.post("/api/references", json={**base, "key": "test-dup"}).status_code == 409


def test_references_bad_key_rejected(client):
    r = client.post("/api/references",
                    json={"key": "Bad Key", "title": "X", "target_kind": "doci", "target_id": "g"})
    assert r.status_code == 422


def test_references_reuse_key_after_delete(client):
    base = {"title": "X", "target_kind": "doci", "target_id": "g1"}
    assert client.post("/api/references", json={**base, "key": "test-reuse"}).status_code == 201
    assert client.delete("/api/references/test-reuse").status_code == 200
    # Soft delete frees the slug -> same key can be created again.
    assert client.post("/api/references", json={**base, "key": "test-reuse"}).status_code == 201


# --------------------------------------------------------------- tenant isolation

def test_references_tenant_isolation(client, as_principal):
    # Tenant 4242 creates a reference.
    as_principal(_member(4242))
    assert client.post("/api/references",
                       json={"key": "test-iso", "title": "Owned", "target_kind": "doci",
                             "target_id": "g1"}).status_code == 201

    # A member of another tenant cannot see or fetch it.
    as_principal(_member(9999))
    keys = {x["key"] for x in client.get("/api/references").json()["references"]}
    assert "test-iso" not in keys
    assert client.get("/api/references/test-iso").status_code == 404

    # ...and cannot delete it (scoped to their own tenant).
    assert client.delete("/api/references/test-iso").status_code == 404

    # The owning tenant still sees it.
    as_principal(_member(4242))
    keys = {x["key"] for x in client.get("/api/references").json()["references"]}
    assert "test-iso" in keys


def test_references_same_key_distinct_tenants(client, as_principal):
    # The per-tenant unique index lets two tenants hold the same key independently.
    as_principal(_member(4242))
    assert client.post("/api/references",
                       json={"key": "test-shared", "title": "T4242", "target_kind": "doci",
                             "target_id": "g-a"}).status_code == 201
    as_principal(_member(9999))
    assert client.post("/api/references",
                       json={"key": "test-shared", "title": "T9999", "target_kind": "doci",
                             "target_id": "g-b"}).status_code == 201
    assert client.get("/api/references/test-shared").json()["title"] == "T9999"
