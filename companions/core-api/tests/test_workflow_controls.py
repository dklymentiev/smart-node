"""The owner's workflow controls (routes/workflows.py, the panel's Workflows page).

Against a throwaway database built from the tree's bootstrap files:

1. workflows.write is the owner's: an admin and a user are refused both routes.
2. Off: the status is kept, the run route refuses the flow (409), the agents
   granted it are recomposed; on again, the same. Only "available" and "off";
   an unknown flow is 404.
3. Agents: the set given becomes the grant; an agent taken off is remembered,
   given back is forgotten; an unknown agent is 422 and changes nothing; the
   agents added and removed are recomposed.
4. The import door and the registrar grant a flow to the agents in its blocks,
   but not to one the owner took off it.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals

FLOW = """schema_version: "3.4.0"
name: demo-flow
team: team-demo
description: a demo
blocks:
  - name: draft
    specialist: dev
  - name: review
    specialist: ops
    depends_on: [draft]
"""


@pytest.fixture(scope="module")
def node():
    import auth
    from routes import workflows

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        tenant = people["owner"].tenant_id
        sql("INSERT INTO sn_config.agents (slug, title, enabled, tenant_id) VALUES "
            f"('dev', 'Dev', true, {tenant}), ('ops', 'Ops', true, {tenant}), ('qa', 'QA', true, {tenant})")
        sql("INSERT INTO sn_core.workflows (name, title, status, steps) VALUES ('demo-flow', 'Demo', 'available', '[]')")
        sql("INSERT INTO sn_config.agent_workflows (agent_id, workflow_id) "
            "SELECT a.id, w.id FROM sn_config.agents a, sn_core.workflows w "
            "WHERE a.slug IN ('dev', 'ops') AND w.name = 'demo-flow'")
        app = FastAPI()
        app.include_router(workflows.router, prefix="/api/workflows")
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name):
            who["p"] = people[name]
            return client

        yield as_, sql


@pytest.fixture
def recomposed(monkeypatch):
    from routes import workflows
    seen = []
    monkeypatch.setattr(workflows, "_recompose", lambda cur, slugs: seen.append(sorted(set(slugs))))
    return seen


def granted(sql):
    return sorted(sql("SELECT a.slug FROM sn_config.agent_workflows aw JOIN sn_config.agents a ON a.id = aw.agent_id "
                      "JOIN sn_core.workflows w ON w.id = aw.workflow_id WHERE w.name = 'demo-flow'").split())


def revoked(sql):
    return sorted(sql("SELECT a.slug FROM sn_config.agent_workflow_revokes r JOIN sn_config.agents a ON a.id = r.agent_id "
                      "JOIN sn_core.workflows w ON w.id = r.workflow_id WHERE w.name = 'demo-flow'").split())


def test_only_the_owner_changes_a_workflow(node, recomposed):
    as_, sql = node
    for name in ("admin", "user"):
        assert as_(name).patch("/api/workflows/demo-flow", json={"status": "off"}).status_code == 403, name
        assert as_(name).put("/api/workflows/demo-flow/agents", json={"agents": []}).status_code == 403, name
    assert sql("SELECT status FROM sn_core.workflows WHERE name = 'demo-flow'") == "available"
    assert granted(sql) == ["dev", "ops"] and recomposed == []


def test_off_refuses_a_run_and_on_allows_it_again(node, recomposed):
    as_, sql = node
    owner = as_("owner")
    r = owner.patch("/api/workflows/demo-flow", json={"status": "off"})
    assert r.status_code == 200 and r.json()["changed"] is True, r.text
    assert sql("SELECT status FROM sn_core.workflows WHERE name = 'demo-flow'") == "off"
    assert recomposed == [["dev", "ops"]], "the agents granted it learn it is off"
    run = owner.post("/api/workflows/demo-flow/run", json={})
    assert run.status_code == 409 and "off" in run.text, run.text
    assert owner.patch("/api/workflows/demo-flow", json={"status": "off"}).json()["changed"] is False
    assert len(recomposed) == 1, "no change, no recompose"
    assert owner.patch("/api/workflows/demo-flow", json={"status": "broken"}).status_code == 422
    assert owner.patch("/api/workflows/no-such", json={"status": "off"}).status_code == 404
    assert owner.patch("/api/workflows/demo-flow", json={"status": "available"}).status_code == 200
    assert sql("SELECT status FROM sn_core.workflows WHERE name = 'demo-flow'") == "available"
    assert sql("SELECT count(*) FROM sn_core.events WHERE event_type = 'workflow.status_changed'") == "2"


def test_agents_taken_off_are_remembered_and_given_back_forgotten(node, recomposed):
    as_, sql = node
    owner = as_("owner")
    r = owner.put("/api/workflows/demo-flow/agents", json={"agents": ["dev", "qa"]})
    assert r.status_code == 200, r.text
    assert (r.json()["added"], r.json()["removed"]) == (["qa"], ["ops"])
    assert granted(sql) == ["dev", "qa"] and revoked(sql) == ["ops"]
    assert recomposed == [["ops", "qa"]]
    bad = owner.put("/api/workflows/demo-flow/agents", json={"agents": ["dev", "nobody"]})
    assert bad.status_code == 422 and granted(sql) == ["dev", "qa"], "an unknown agent changes nothing"
    owner.put("/api/workflows/demo-flow/agents", json={"agents": ["dev", "ops", "qa"]})
    assert granted(sql) == ["dev", "ops", "qa"] and revoked(sql) == [], "given back, forgotten"
    owner.put("/api/workflows/demo-flow/agents", json={"agents": ["dev", "qa"]})
    assert revoked(sql) == ["ops"]


def test_the_import_door_does_not_grant_back_a_revoked_agent(node, recomposed, tmp_path):
    as_, sql = node
    import yaml
    from db import cursor
    from routes import workflows
    assert revoked(sql) == ["ops"]
    with cursor(commit=True) as cur:
        workflows._materialize("demo-flow", yaml.safe_load(FLOW), cur, path=str(tmp_path / "demo-flow.yaml"))
    assert granted(sql) == ["dev", "qa"], "ops stays off; dev, named in the blocks, keeps it"


def test_the_registrar_does_not_grant_back_a_revoked_agent(node, recomposed, tmp_path, monkeypatch):
    as_, sql = node
    assert revoked(sql) == ["ops"]
    d = tmp_path / "flows" / "demo-flow"
    d.mkdir(parents=True)
    (d / "demo-flow.yaml").write_text(FLOW)
    monkeypatch.setenv("SN_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("SN_AUTHORED_FLOWS_DIR", str(tmp_path / "authored"))
    sql("DELETE FROM sn_config.agent_workflows WHERE agent_id IN (SELECT id FROM sn_config.agents WHERE slug = 'dev')")
    path = pathlib.Path(__file__).resolve().parents[1] / "companions" / "register-workflows.py"
    spec = importlib.util.spec_from_file_location("register_workflows_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()
    assert granted(sql) == ["dev", "qa"], "dev is granted again from the blocks, ops is not"
    assert sql("SELECT status FROM sn_core.workflows WHERE name = 'demo-flow'") == "available"
    owner = as_("owner")
    owner.patch("/api/workflows/demo-flow", json={"status": "off"})
    mod.main()
    assert sql("SELECT status FROM sn_core.workflows WHERE name = 'demo-flow'") == "off", "an update keeps it off"
