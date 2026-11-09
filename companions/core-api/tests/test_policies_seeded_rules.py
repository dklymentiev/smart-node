"""Tests for seeded policy rules: PATCH and DELETE must reach seeded rules (tenant_id=1).

Seeded rules have tenant_id=1. Before this fix, PATCH and DELETE both filtered
on tenant_id IS NULL, which excludes all seeded rules and returns 404. The fix
adds a `scope` parameter that controls the WHERE clause.

These are integration tests: they run against the real database and use an actual
seeded rule. The seeded rule used here is 'no-slop' (id=6, tenant_id=1, always
present on a fresh node per the seed migration). We restore its original state
after each test that mutates it.

Run inside the backend container:
    docker compose exec -w /app backend python -m pytest tests/test_policies_seeded_rules.py -v
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# A seeded rule that exists on every fresh node (tenant_id=1).
# We restore its enabled state after any test that touches it.
SEEDED_SLUG = "no-slop"


@pytest.fixture(autouse=True)
def restore_seeded_rule():
    """Ensure the seeded 'no-slop' rule is left enabled after each test."""
    yield
    # Restore to enabled=true regardless of what the test did.
    from db import cursor
    with cursor(commit=True) as cur:
        cur.execute(
            "UPDATE sn_config.policies SET enabled = true, archived_at = NULL, "
            "updated_by = 'test-restore', updated_at = now() "
            "WHERE slug = %s AND tenant_id = 1",
            (SEEDED_SLUG,),
        )


# ---- Bug 1: PATCH reaches seeded rule when scope=enterprise -----------------

def test_patch_node_scope_misses_seeded_rule(client):
    """Default scope=node must return 404 for a rule with tenant_id=1."""
    r = client.patch(
        f"/api/policies/{SEEDED_SLUG}",
        json={"enabled": False},
        # scope defaults to 'node' -> tenant_id IS NULL filter
    )
    assert r.status_code == 404, (
        f"expected 404 for seeded rule at scope=node, got {r.status_code}: {r.text}"
    )


def test_patch_enterprise_scope_reaches_seeded_rule(client):
    """scope=enterprise&tenant_id=1 must find and update the seeded rule."""
    r = client.patch(
        f"/api/policies/{SEEDED_SLUG}?scope=enterprise&tenant_id=1",
        json={"enabled": False},
    )
    assert r.status_code == 200, (
        f"scope=enterprise should reach seeded rule, got {r.status_code}: {r.text}"
    )
    assert r.json()["enabled"] is False


def test_patch_enterprise_scope_missing_tenant_id_returns_400(client):
    """scope=enterprise without tenant_id must be rejected."""
    r = client.patch(
        f"/api/policies/{SEEDED_SLUG}?scope=enterprise",
        json={"enabled": False},
    )
    assert r.status_code == 400


# ---- Bug 2: DELETE targets exactly one row (LIMIT 1 via subquery) -----------

def test_delete_enterprise_scope_archives_seeded_rule(client):
    """scope=enterprise&tenant_id=1 must archive the seeded rule, not 404."""
    r = client.delete(
        f"/api/policies/{SEEDED_SLUG}?scope=enterprise&tenant_id=1",
    )
    assert r.status_code == 204, (
        f"scope=enterprise delete should reach seeded rule, got {r.status_code}: {r.text}"
    )
    # Confirm it is now archived.
    from db import cursor
    with cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM sn_config.policies WHERE slug = %s AND tenant_id = 1",
            (SEEDED_SLUG,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["archived_at"] is not None, "row should be archived after delete"


def test_delete_node_scope_misses_seeded_rule(client):
    """Default scope=node must return 404 for a seeded rule (tenant_id=1)."""
    r = client.delete(f"/api/policies/{SEEDED_SLUG}")
    assert r.status_code == 404


# ---- Bug 3: list endpoint exposes collision flag ----------------------------

def test_list_collision_flag_absent_when_no_collision(client):
    """A rule that exists at only one scope must have collision=False."""
    r = client.get("/api/policies")
    assert r.status_code == 200
    data = r.json()
    # no-slop exists only at enterprise scope (tenant_id=1); collision must be False
    # (assuming no node-level rule with the same slug exists on this node).
    seeded = [p for p in data["policies"] if p["slug"] == SEEDED_SLUG]
    assert seeded, f"'{SEEDED_SLUG}' not found in policy list"
    assert seeded[0]["collision"] is False, (
        f"expected collision=False for '{SEEDED_SLUG}' (single scope), "
        f"got {seeded[0]['collision']}"
    )


def test_list_collision_flag_and_collisions_field_on_duplicate(client):
    """When the same slug exists at node AND enterprise scope, collision=True."""
    from db import cursor
    # Create a node-level shadow of the seeded rule.
    with cursor(commit=True) as cur:
        cur.execute(
            "INSERT INTO sn_config.policies "
            "  (tenant_id, agent_id, slug, category, title, body_md, severity, rationale, "
            "   created_by, updated_by) "
            "VALUES (NULL, NULL, %s, 'workflow', 'Test shadow', 'shadow body', "
            "        'warn', 'test reason', 'pytest', 'pytest') "
            "ON CONFLICT DO NOTHING",
            (SEEDED_SLUG,),
        )
    try:
        r = client.get("/api/policies")
        assert r.status_code == 200
        data = r.json()
        matching = [p for p in data["policies"] if p["slug"] == SEEDED_SLUG]
        assert len(matching) == 2, (
            f"expected 2 rows for '{SEEDED_SLUG}' (node + enterprise), got {len(matching)}"
        )
        for p in matching:
            assert p["collision"] is True, (
                f"both rows at slug '{SEEDED_SLUG}' should have collision=True"
            )
        assert SEEDED_SLUG in data["collisions"], (
            f"'{SEEDED_SLUG}' should appear in the collisions summary"
        )
    finally:
        # Remove the shadow node-level rule we created.
        with cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM sn_config.policies "
                "WHERE slug = %s AND tenant_id IS NULL AND agent_id IS NULL",
                (SEEDED_SLUG,),
            )
