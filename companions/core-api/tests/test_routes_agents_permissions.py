"""The permission chokepoint on routes/agents.py.

Three facts, asked of the real router, the real `requires()` and a database
built from the tree's bootstrap files:

1. Every handler declares the permission the catalogue assigns to it, and
   every declared name exists in the seeded catalogue (the startup scan).
2. A caller whose role lacks the permission is refused before the handler,
   and the 403 names both the role and the permission.
3. A caller whose role holds it reaches the handler.

None of the agents permissions has an object axis.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals

# The agents router reads both at import. No case here writes an agent
# directory, so a path that is never created is enough.
_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))

P = "/api/agents"
EXPECTED = {
    ("GET", P): "agents.read",
    ("GET", P + "/state"): "agents.read",
    ("GET", P + "/health"): "agents.read",
    ("GET", P + "/processes"): "agents.read",
    ("POST", P + "/{slug}/stop"): "agents.stop",
    ("POST", P + "/health/refresh"): "agents.health.refresh",
    ("POST", P + "/{slug}/runtime"): "agents.runtime.control",
    ("POST", P + "/{slug}/wake"): "agents.wake",
    ("GET", P + "/with-pending-work"): "agents.read",
    ("GET", P + "/{slug}"): "agents.read",
    ("POST", P): "agents.create",
    ("POST", P + "/{slug}/avatar"): "agents.update",
    ("PUT", P + "/{slug}"): "agents.update",
    ("DELETE", P + "/{slug}"): "agents.archive",
    ("GET", P + "/{slug}/grants"): "agents.grants.read",
    ("PUT", P + "/{slug}/grants"): "agents.grants.write",
    ("POST", P + "/{slug}/regenerate"): "agents.regenerate",
    ("GET", P + "/{slug}/system-prompt"): "agents.system_prompt.read",
    ("GET", P + "/{slug}/tools"): "agents.tools.read",
    ("GET", P + "/{slug}/policies"): "agents.policies.read",
    ("PUT", P + "/{slug}/policies/{policy_slug}"): "agents.policies.write",
    ("GET", P + "/{slug}/channels"): "agents.channels.read",
    ("PUT", P + "/{slug}/channels/{channel_slug}"): "agents.channels.bind",
    ("PUT", P + "/{slug}/documents/{guid}/pin"): "agents.documents.pin.write",
    ("GET", P + "/{slug}/documents"): "agents.documents.read",
    ("POST", P + "/{slug}/documents"): "agents.documents.write",
    ("POST", P + "/{slug}/documents/upload"): "agents.documents.write",
    ("GET", P + "/{slug}/documents/{guid}"): "agents.documents.read",
    ("DELETE", P + "/{slug}/documents/{guid}"): "agents.documents.delete",
    ("POST", P + "/me/wakes"): "agents.self.wakes.read",
}


def test_agents_router_declares_catalogue_permissions():
    """Structural: every handler carries exactly the catalogue's name."""
    from routes import agents
    from permissions import route_permission

    seen: dict[tuple[str, str], str | None] = {}
    for route in agents.router.routes:
        for method in route.methods - {"HEAD"}:
            declared = route_permission(route)
            seen[(method, route.path)] = declared[0] if declared else None
            assert declared is None or declared[1] is False, (
                f"{method} {route.path} passes an object, but no agents "
                "permission has an object axis"
            )
    missing = sorted(k for k, v in seen.items() if v is None)
    assert not missing, f"no requires() declaration on: {missing}"
    assert seen == EXPECTED, (
        "handler-to-permission mapping diverged from the catalogue: "
        f"{sorted(set(seen.items()) ^ set(EXPECTED.items()))}"
    )


@pytest.fixture(scope="module")
def node():
    """The real agents router over a throwaway database; `as_(who)` picks the caller."""
    import auth
    from routes import agents

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        app = FastAPI()
        app.include_router(agents.router)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name: str) -> TestClient:
            who["p"] = people[name]
            return client

        yield app, as_


def test_every_declared_name_is_in_the_seeded_catalogue(node):
    """The startup scan admits the router: names exist, no object is owed."""
    from permissions import validate_routes

    app, _ = node
    validate_routes(app, strict_undeclared=True)


def test_role_without_the_permission_is_refused_and_named(node):
    """A role-less actor resolves to `user`, which does not hold the admin-tier
    permissions; the refusal names the missing cell of the matrix."""
    _, as_ = node
    for method, path, permission in (
        ("POST", P + "/health/refresh", "agents.health.refresh"),
        ("GET", P + "/no-such-agent/system-prompt", "agents.system_prompt.read"),
        ("DELETE", P + "/no-such-agent", "agents.archive"),
        ("POST", P + "/no-such-agent/regenerate", "agents.regenerate"),
    ):
        r = as_("user").request(method, path)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text}"
        detail = r.json()["detail"]
        assert "'user'" in detail and f"'{permission}'" in detail, detail


def test_role_with_the_permission_reaches_the_handler(node):
    _, as_ = node
    # `agents.read` is held by every role, the role-less actor included.
    r = as_("user").get(P)
    assert r.status_code == 200, r.text
    assert "agents" in r.json()
    # The same paths the user was refused on answer from the handler for an
    # admin: its own 404 for an agent that does not exist.
    r = as_("admin").get(P + "/no-such-agent/system-prompt")
    assert r.status_code == 404 and "no-such-agent" in r.text, r.text
    r = as_("owner").delete(P + "/no-such-agent")
    assert r.status_code == 404 and "no-such-agent" in r.text, r.text
