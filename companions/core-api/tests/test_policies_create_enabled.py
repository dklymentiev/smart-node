"""A rule written from the panel (routes/policies.py create and update).

Against a throwaway database built from the tree's bootstrap files:

1. Create keeps the Enabled box: a rule created off stays off (the route used
   to drop the field, so every new rule went straight into agents' prompts).
   Left out, a rule is on, as before.
2. The reason is required on create; an edit changes the rule with PATCH and
   keeps the reason unless a new one is given.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals

RULE = {"category": "workflow", "title": "T", "body_md": "rule body", "severity": "info",
        "rationale": "2026-10-15: the panel's Enabled box was ignored"}


@pytest.fixture(scope="module")
def owner():
    import auth
    from routes import policies

    with disposable_database() as sql:
        people = principals(sql)
        # The install adds the column in seed-policies.py, not in the bootstrap files.
        sql("ALTER TABLE sn_config.policies ADD COLUMN IF NOT EXISTS rationale TEXT")
        app = FastAPI()
        app.include_router(policies.router, prefix="/api/policies")
        app.dependency_overrides[auth.get_principal] = lambda: people["owner"]
        yield TestClient(app, raise_server_exceptions=False), sql


def test_a_rule_created_off_stays_off(owner):
    client, sql = owner
    r = client.post("/api/policies", json={**RULE, "slug": "test-off", "enabled": False})
    assert r.status_code == 201, r.text
    assert r.json()["enabled"] is False
    assert sql("SELECT enabled FROM sn_config.policies WHERE slug = 'test-off'") == "f"
    r = client.post("/api/policies", json={**RULE, "slug": "test-on"})
    assert r.status_code == 201 and r.json()["enabled"] is True, r.text


def test_the_reason_is_required_and_an_edit_keeps_it(owner):
    client, sql = owner
    no_reason = {k: v for k, v in RULE.items() if k != "rationale"}
    assert client.post("/api/policies", json={**no_reason, "slug": "test-noreason"}).status_code == 422
    r = client.patch("/api/policies/test-off", json={"title": "T2", "enabled": False})
    assert r.status_code == 200, r.text
    assert sql("SELECT title || '|' || enabled || '|' || rationale FROM sn_config.policies WHERE slug = 'test-off'") \
        == "T2|false|" + RULE["rationale"]
