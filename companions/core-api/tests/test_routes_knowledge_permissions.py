"""Role-axis chokepoint on routes/knowledge.py.

Per the 4a catalogue the handler-to-permission mapping is:

- GET    /api/knowledge/kinds                       list_kinds        -> knowledge.read
- GET    /api/agents/{slug}/knowledge               list_knowledge    -> knowledge.read
- POST   /api/agents/{slug}/knowledge               add_knowledge     -> knowledge.write
- DELETE /api/agents/{slug}/knowledge/{kid}         delete_knowledge  -> knowledge.write

Operator ruling on this task: knowledge.* is seeded with object_axis='none'
in 1.12.49; the per-agent scoping stays in the handler. Declared accordingly
with no object fetcher.
"""

from __future__ import annotations

import os

# knowledge.py imports from routes.agents which pulls lib.agent_fs; that module
# resolves two filesystem roots at import time and raises KeyError if they
# are not set. The chokepoint test does not touch the filesystem, so stub
# values are enough.
os.environ.setdefault("SN_AGENTS_ROOT", "/tmp/sn-agents-root-stub")
os.environ.setdefault("SN_AGENT_TEMPLATE", "/tmp/sn-agent-template-stub")

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient


EXPECTED_KINDS = {
    ("GET", "/api/knowledge/kinds"): "knowledge.read",
}

EXPECTED_AGENTS = {
    ("GET",    "/api/agents/{slug}/knowledge"):        "knowledge.read",
    ("POST",   "/api/agents/{slug}/knowledge"):        "knowledge.write",
    ("DELETE", "/api/agents/{slug}/knowledge/{kid}"):  "knowledge.write",
}


def test_knowledge_routers_declare_catalogue_permissions():
    """Both routers in this module carry the catalogue permissions."""
    from routes import knowledge
    from permissions import route_permission

    def _collect(router):
        seen: dict[tuple[str, str], str] = {}
        for route in router.routes:
            for method in route.methods:
                if method == "HEAD":
                    continue
                declared = route_permission(route)
                assert declared is not None, (
                    f"{method} {route.path} has no requires() permission declaration"
                )
                seen[(method, route.path)] = declared[0]
                assert declared[1] is False, (
                    f"{method} {route.path} declares an object fetcher but the "
                    "catalogue places knowledge.* on object_axis='none' in 1.12.49"
                )
        return seen

    kinds_seen = _collect(knowledge.kinds_router)
    agents_seen = _collect(knowledge.agent_knowledge_router)
    assert kinds_seen == EXPECTED_KINDS, (
        f"kinds_router mapping diverged; got {sorted(kinds_seen.items())}"
    )
    assert agents_seen == EXPECTED_AGENTS, (
        f"agent_knowledge_router mapping diverged; got {sorted(agents_seen.items())}"
    )


class _StubCur:
    def execute(self, *a, **k):
        return None
    def fetchall(self):
        return []
    def fetchone(self):
        return None


@contextmanager
def _stub_cursor(*a, **k):
    yield _StubCur()


def _build_app(monkeypatch, decision_for, role="user"):
    import permissions
    import auth
    from routes import knowledge

    monkeypatch.setattr(permissions, "check_permission", decision_for)
    monkeypatch.setattr(permissions, "cursor", _stub_cursor)
    monkeypatch.setattr(knowledge, "cursor", _stub_cursor)

    app = FastAPI()
    app.include_router(knowledge.kinds_router)
    app.include_router(knowledge.agent_knowledge_router)

    def _principal():
        return auth.Principal(
            username="test", user_id=0, tenant_id=1, role=role,
            actor_id="agent:test", strength="proved",
        )
    app.dependency_overrides[auth.get_principal] = _principal
    return app


def test_write_is_refused_when_role_axis_denies(monkeypatch):
    """DELETE knowledge.write is refused by the chokepoint; 403 names role AND permission."""
    import permissions

    def deny(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=False,
            reason=f"role 'user' lacks permission '{name}'",
        )

    app = _build_app(monkeypatch, deny)
    r = TestClient(app, raise_server_exceptions=False).delete(
        "/api/agents/some-agent/knowledge/42"
    )
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "'user'" in detail
    assert "'knowledge.write'" in detail


def test_read_is_reached_when_role_axis_allows(monkeypatch):
    """GET /api/knowledge/kinds admitted; handler returns the catalogue."""
    import permissions

    def allow(principal, name, object_id=None):
        return permissions.PermissionDecision(
            permission=name, role="user", axis="none", object_id=object_id,
            allowed=True, reason="role grant (no object axis)",
        )

    app = _build_app(monkeypatch, allow)
    r = TestClient(app, raise_server_exceptions=False).get("/api/knowledge/kinds")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "kinds" in body
    assert isinstance(body["kinds"], list)
    # KIND_CATALOG has four entries; availability depends on integrations status,
    # which the stub reports as empty, so none require an integration will be
    # available only if 'requires' is None. Only the shape is asserted here.
    assert len(body["kinds"]) == 4
