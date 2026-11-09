"""The owner's read of the node memory (routes/memory.py, the panel's Memory page).

mesh is replaced by a fake that answers as it does (a cross-workspace list says
workspace "*", a document asked from another workspace is 404). Pinned here,
against a throwaway database built from the tree's bootstrap files:

1. memory.read is the owner's: an agent is refused every route.
2. Workspaces: each with its count and its agent, the agents with no documents
   too, an agent with its own workspace name mapped to it.
3. The "all" listing asks each workspace and merges by date, so every row says
   whose it is; paging and the type filter reach mesh.
4. Search names the workspaces it searches (every one with documents unless
   chosen), and passes the type and dates on.
5. One document: the full text from its workspace; a wrong workspace is 404,
   a malformed name or id 400; mesh down is 502.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals

DOCS = {
    "dev": [
        {"guid": "doc_d1", "content": "# Build notes\nthe tetris build works", "tags": ["type:handoff"], "created_at": "2026-10-14T10:00:00Z"},
        {"guid": "doc_d2", "content": "deploy checklist", "tags": ["type:knowledge"], "created_at": "2026-10-12T10:00:00Z"},
        {"guid": "doc_d3", "content": "# Session handoff\n\nAgent: dev\n\n# Handoff: the release notes draft\n- Channel: web-chat\nmore", "tags": ["type:handoff"], "created_at": "2026-10-11T10:00:00Z"},
    ],
    "ops-space": [
        {"guid": "doc_o1", "content": "tetris on the server", "filename": "Server note", "tags": ["type:note"], "created_at": "2026-10-15T09:00:00Z"},
    ],
    "shared": [
        {"guid": "doc_s1", "content": "who to ask for approvals", "tags": ["type:knowledge"], "created_at": "2026-10-13T09:00:00Z"},
    ],
}


class FakeMesh:
    def __init__(self):
        self.calls = []
        self.down = False

    def __call__(self, method, path, workspace, body=None, timeout=10):
        self.calls.append((method, path, workspace, body))
        if self.down:
            raise HTTPException(502, "memory is not reachable: refused")
        if path == "/admin/workspaces":
            return [{"workspace": ws, "documents": len(d), "last_activity": max(x["created_at"] for x in d)}
                    for ws, d in DOCS.items()]
        if path.startswith("/tags"):
            seen = {}
            for d in DOCS.values():
                for x in d:
                    for t in x["tags"]:
                        if t.startswith("type:"):
                            seen[t] = seen.get(t, 0) + 1
            return {"tags": [{"tag": t, "count": n} for t, n in seen.items()]}
        if path.startswith("/?"):
            q = dict(p.split("=", 1) for p in path[2:].split("&"))
            rows = [dict(x, workspace="*") for d in DOCS.values() for x in d] if workspace == "*" \
                else [dict(x, workspace=workspace) for x in DOCS.get(workspace, [])]
            if "tag" in q:
                rows = [x for x in rows if q["tag"] in x["tags"]]
            rows.sort(key=lambda x: x["created_at"], reverse=True)
            return rows[:int(q["limit"])]
        if path == "/search":
            out = []
            for ws in body["workspaces"]:
                for x in DOCS.get(ws, []):
                    if body["query"] in x["content"] and all(t in x["tags"] for t in body.get("tags", [])):
                        out.append(dict(x, workspace=ws, similarity_score=0.8))
            return {"results": out[:body["limit"]], "total_count": len(out)}
        guid = path.lstrip("/")
        for x in DOCS.get(workspace, []):
            if x["guid"] == guid:
                return dict(x, workspace=workspace)
        raise HTTPException(404, "no such document in that workspace")


@pytest.fixture(scope="module")
def node():
    import auth
    from routes import memory

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        tenant = people["owner"].tenant_id
        sql("INSERT INTO sn_config.agents (slug, title, enabled, tenant_id) VALUES "
            f"('dev', 'Dev', true, {tenant}), ('ops', 'Ops', true, {tenant}), ('quiet', 'Quiet', true, {tenant})")
        sql("UPDATE sn_config.agents SET mesh_workspace = 'ops-space' WHERE slug = 'ops'")
        app = FastAPI()
        app.include_router(memory.router)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name):
            who["p"] = people[name]
            return client

        yield as_


@pytest.fixture
def mesh(monkeypatch):
    from routes import memory
    fake = FakeMesh()
    monkeypatch.setattr(memory, "_mesh", fake)
    return fake


ROUTES = [("GET", "/api/memory/workspaces", None), ("GET", "/api/memory/types", None),
          ("GET", "/api/memory/documents", None), ("POST", "/api/memory/search", {"query": "x"}),
          ("GET", "/api/memory/documents/doc_d1?workspace=dev", None)]


def test_only_the_owner_reads_memory(node, mesh):
    for method, path, body in ROUTES:
        r = node("admin").request(method, path, json=body)
        assert r.status_code == 403, (path, r.status_code, r.text)
        r = node("owner").request(method, path, json=body)
        assert r.status_code == 200, (path, r.status_code, r.text)


def test_workspaces_name_their_agents_and_keep_the_empty_ones(node, mesh):
    body = node("owner").get("/api/memory/workspaces").json()
    by = {w["workspace"]: w for w in body["workspaces"]}
    assert by["dev"]["documents"] == 3 and by["dev"]["agent"] == {"slug": "dev", "title": "Dev"}
    assert by["ops-space"]["agent"]["slug"] == "ops", "an agent's own workspace name maps to it"
    assert by["shared"]["agent"] is None
    assert by["quiet"]["documents"] == 0, "an agent with no documents is listed"
    assert body["total"] == 5
    assert [w["workspace"] for w in body["workspaces"]][0] == "dev", "most documents first"


def test_the_all_listing_says_whose_each_row_is(node, mesh):
    body = node("owner").get("/api/memory/documents?limit=3").json()
    assert [d["guid"] for d in body["documents"]] == ["doc_o1", "doc_d1", "doc_s1"], "merged by date"
    assert [d["workspace"] for d in body["documents"]] == ["ops-space", "dev", "shared"]
    assert "*" not in {c[2] for c in mesh.calls if c[1].startswith("/?")}, "never listed as *"
    assert body["more"] is True
    assert body["documents"][1]["title"] == "Build notes" and body["documents"][0]["title"] == "Server note"
    nxt = node("owner").get("/api/memory/documents?limit=3&offset=3").json()
    assert [d["guid"] for d in nxt["documents"]] == ["doc_d2", "doc_d3"] and nxt["more"] is False
    assert nxt["documents"][1]["title"] == "the release notes draft", "a handoff is named by its subject"
    assert nxt["documents"][1]["preview"] == "more", "the preview skips the headings and the bookkeeping lines"


def test_one_workspace_and_a_type(node, mesh):
    body = node("owner").get("/api/memory/documents?workspace=dev&type=knowledge").json()
    assert [d["guid"] for d in body["documents"]] == ["doc_d2"]
    assert body["documents"][0]["type"] == "knowledge"
    assert node("owner").get("/api/memory/documents?workspace=Bad%20Name").status_code == 400
    assert node("owner").get("/api/memory/documents?type=x%3By").status_code == 400


def test_search_names_the_workspaces_it_searches(node, mesh):
    body = node("owner").post("/api/memory/search", json={"query": "tetris"}).json()
    assert {(r["guid"], r["workspace"]) for r in body["results"]} == {("doc_d1", "dev"), ("doc_o1", "ops-space")}
    call = [c for c in mesh.calls if c[1] == "/search"][-1]
    assert set(call[3]["workspaces"]) == {"dev", "ops-space", "shared"}, "every workspace with documents"
    body = node("owner").post("/api/memory/search", json={"query": "tetris", "workspaces": ["dev"],
                                                          "type": "handoff", "date_from": "2026-10-01"}).json()
    assert [r["guid"] for r in body["results"]] == ["doc_d1"] and body["results"][0]["score"] == 0.8
    call = [c for c in mesh.calls if c[1] == "/search"][-1]
    assert call[3]["workspaces"] == {"dev": 1.0} and call[3]["tags"] == ["type:handoff"]
    assert call[3]["date_from"] == "2026-10-01"
    assert node("owner").post("/api/memory/search", json={"query": ""}).status_code == 422


def test_one_document_in_full_from_its_own_workspace(node, mesh):
    d = node("owner").get("/api/memory/documents/doc_d1?workspace=dev").json()
    assert d["content"].startswith("# Build notes") and d["workspace"] == "dev" and "preview" not in d
    assert node("owner").get("/api/memory/documents/doc_d1?workspace=shared").status_code == 404
    assert node("owner").get("/api/memory/documents/doc_d1").status_code == 422
    assert node("owner").get("/api/memory/documents/doc%2Fx?workspace=dev").status_code in (400, 404)


def test_mesh_down_is_a_bad_gateway(node, mesh):
    mesh.down = True
    assert node("owner").get("/api/memory/workspaces").status_code == 502
    assert node("owner").get("/api/memory/documents").status_code == 502
