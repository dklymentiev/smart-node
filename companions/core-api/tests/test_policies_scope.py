"""Policies registry: tenant scoping now comes from get_principal (not a
tenant_id query param). A member sees/edits only their own tenant's policies;
a cross-tenant super-admin sees all. Rows created here use the test- slug
prefix and are hard-deleted afterwards.
"""

import pytest

from auth import Principal, get_principal


def _member(tenant_id):
    return Principal(username=f"m{tenant_id}", user_id=-1, tenant_id=tenant_id, role="member")


def _superadmin():
    return Principal(username="s", user_id=-1, tenant_id=1, role="super-admin")


@pytest.fixture
def as_principal():
    from main import app

    def _set(principal):
        app.dependency_overrides[get_principal] = lambda: principal

    yield _set
    app.dependency_overrides.pop(get_principal, None)


@pytest.fixture(autouse=True)
def cleanup_test_policies():
    yield
    from db import cursor
    with cursor(commit=True) as cur:
        cur.execute("DELETE FROM sn_config.policies WHERE slug LIKE 'test-%'")


def _policy(slug):
    return {"slug": slug, "category": "workflow", "title": "T",
            "body_md": "rule body", "severity": "warn", "enabled": True}


def test_policies_tenant_isolation(client, as_principal):
    as_principal(_member(4242))
    assert client.post("/api/policies", json=_policy("test-pol")).status_code == 201

    as_principal(_member(9999))
    slugs = {p["slug"] for p in client.get("/api/policies").json()["policies"]}
    assert "test-pol" not in slugs
    assert client.get("/api/policies/test-pol").status_code == 404
    assert client.delete("/api/policies/test-pol").status_code == 404

    as_principal(_member(4242))
    slugs = {p["slug"] for p in client.get("/api/policies").json()["policies"]}
    assert "test-pol" in slugs


def test_policies_superadmin_sees_all_tenants(client, as_principal):
    as_principal(_member(4242))
    assert client.post("/api/policies", json=_policy("test-pol-x")).status_code == 201

    # cross-tenant role lists across tenants -> sees the tenant-4242 row.
    as_principal(_superadmin())
    slugs = {p["slug"] for p in client.get("/api/policies").json()["policies"]}
    assert "test-pol-x" in slugs


def test_policies_create_ignores_payload_tenant(client, as_principal):
    # payload.tenant_id must not override the principal's tenant.
    as_principal(_member(4242))
    body = {**_policy("test-pol-t"), "tenant_id": 1}
    assert client.post("/api/policies", json=body).status_code == 201
    # A different tenant must not see it, proving it landed in 4242, not 1.
    as_principal(_member(7777))
    slugs = {p["slug"] for p in client.get("/api/policies").json()["policies"]}
    assert "test-pol-t" not in slugs
