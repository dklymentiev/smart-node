"""The permission chokepoint on the route files whose permissions are role-only.

One table per route file, asked of the real routers, the real `requires()`
and a database built from the tree's bootstrap files:

1. Structural: every handler either declares exactly the catalogue's
   permission or is listed as undeclared with the reason. A handler in
   neither list fails the test.
2. Deny: a caller whose role lacks the permission is refused before the
   handler, and the 403 names both the role and the permission.
3. Allow: a caller whose role holds it gets the handler's own answer.

An undeclared route is refused by the default-deny middleware of the app.
"""

from __future__ import annotations

import importlib
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from disposable_pg import disposable_database, principals

# Some routers read these at import. No case here writes an agent directory.
_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))

# Why a route carries no declaration.
NO_ROW = "the catalogue has no row for this handler"

# file -> mount:      (router attribute, prefix it is included under)
#         declared:   (method, path) -> permission
#         undeclared: (method, path) -> (catalogue permission or None, why)
#         deny:       (caller, role named, method, path, body, permission)
#         allow:      (caller, method, path, body, status, text in the answer)
FILES: dict[str, dict] = {}

FILES["work"] = {
    "mount": [("router", "/api/work")],
    "declared": {
        ("GET", "/api/work/now"): "work.read",
        ("GET", "/api/work/map"): "work.read",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "GET", "/api/work/now", None, "work.read"),
        ("user", "user", "GET", "/api/work/map", None, "work.read"),
    ],
    "allow": [
        ("admin", "GET", "/api/work/now", None, 200, "schema_version"),
        ("owner", "GET", "/api/work/map?window=1h", None, 400, "window_not_allowed"),
    ],
}

FILES["limits"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/limits/turn-bound"): "limits.read",
        ("GET", "/api/limits"): "limits.read",
        ("GET", "/api/limits/agents/{slug}"): "limits.read",
        ("PUT", "/api/limits/defaults/{code}/{period}"): "limits.write",
        ("PUT", "/api/limits/agents/{slug}/{code}/{period}"): "limits.write",
        ("DELETE", "/api/limits/agents/{slug}/{code}/{period}"): "limits.write",
        ("PUT", "/api/limits/pause"): "limits.write",
        ("POST", "/api/limits/notify-turn-cutoff"): "limits.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "GET", "/api/limits/turn-bound", None, "limits.read"),
        ("user", "user", "GET", "/api/limits", None, "limits.read"),
        ("user", "user", "PUT", "/api/limits/pause", {"paused": True}, "limits.write"),
    ],
    # Past the gate the handler's own rule still stands: the registry is the
    # owner's, so an admin is admitted by role and answered by the handler.
    "allow": [
        ("admin", "GET", "/api/limits", None, 403, "only the node owner"),
        ("admin", "PUT", "/api/limits/pause", {"paused": True}, 403, "only the node owner"),
        ("owner", "GET", "/api/limits", None, 200, "profiles"),
    ],
}

FILES["workflows"] = {
    "mount": [("router", "/api/workflows")],
    "declared": {
        ("GET", "/api/workflows"): "workflows.read",
        ("GET", "/api/workflows/{name}"): "workflows.read",
        ("POST", "/api/workflows/{name}/run"): "workflows.run",
        ("POST", "/api/workflows/import"): "workflows.import",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/workflows/import", {}, "workflows.import"),
        ("user", "user", "POST", "/api/workflows/no-such-flow/run", {}, "workflows.run"),
    ],
    "allow": [
        ("user", "GET", "/api/workflows", None, 200, "workflows"),
        ("admin", "GET", "/api/workflows/no-such-flow", None, 404, "no-such-flow"),
    ],
}

FILES["users"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/users/resolve-identity"): "users.read",
        ("GET", "/api/users/cross-channel-history"): "users.read",
        ("GET", "/api/users/{user_id}/notify-channels"): "users.read",
        ("PUT", "/api/users/{user_id}/notify-channels/{channel}"): "users.channels.write",
    },
    "undeclared": {},
    # Every role a person or an agent can hold has both permissions; the
    # installer component is the one seeded role without them.
    "deny": [
        ("installer", "installer", "GET", "/api/users/resolve-identity?identity=nobody",
         None, "users.read"),
        ("installer", "installer", "GET", "/api/users/999999/notify-channels", None,
         "users.read"),
        ("installer", "installer", "PUT", "/api/users/999999/notify-channels/email",
         {"address": "a@example.test"}, "users.channels.write"),
    ],
    "allow": [
        ("user", "GET", "/api/users/resolve-identity?identity=nobody", None, 200, "found"),
        ("user", "GET", "/api/users/999999/notify-channels", None, 404, "user not found"),
        ("user", "PUT", "/api/users/999999/notify-channels/email",
         {"address": "a@example.test"}, 404, "user not found"),
    ],
}

FILES["tools"] = {
    "mount": [("router", "/api/tools")],
    "declared": {
        ("GET", "/api/tools"): "tools.read",
        ("GET", "/api/tools/{name}"): "tools.read",
        ("POST", "/api/tools"): "tools.write",
        ("PUT", "/api/tools/{name}"): "tools.write",
        ("DELETE", "/api/tools/{name}"): "tools.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/tools", {}, "tools.write"),
        ("user", "user", "DELETE", "/api/tools/no-such-tool", None, "tools.write"),
    ],
    "allow": [
        ("user", "GET", "/api/tools", None, 200, "tools"),
        ("admin", "DELETE", "/api/tools/no-such-tool", None, 404, "no-such-tool"),
    ],
}

FILES["test_nodes"] = {
    "mount": [("router", "")],
    "declared": {
        ("POST", "/api/test-nodes/provision"): "test_nodes.provision",
        ("POST", "/api/test-nodes/destroy"): "test_nodes.destroy",
        ("POST", "/api/test-nodes/status"): "test_nodes.read",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/test-nodes/status", {"name": "tn-x"}, "test_nodes.read"),
        ("admin", "admin", "POST", "/api/test-nodes/destroy", {}, "test_nodes.destroy"),
    ],
    # Past the permission the route's own rule still stands: an admin-role
    # agent is not a cross-tenant principal, and is told so by the route.
    "allow": [
        ("admin", "POST", "/api/test-nodes/status", {"name": "tn-x"}, 403,
         "require a cross-tenant"),
    ],
}

FILES["specialists"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/specialists/library"): "specialists.read",
        ("GET", "/api/specialists"): "specialists.read",
        ("GET", "/api/specialists/{slug}"): "specialists.read",
        ("POST", "/api/specialists"): "specialists.write",
        ("PUT", "/api/specialists/{slug}"): "specialists.write",
        ("DELETE", "/api/specialists/{slug}"): "specialists.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/specialists", {}, "specialists.write"),
        ("user", "user", "DELETE", "/api/specialists/no-such-one", None, "specialists.write"),
    ],
    "allow": [
        ("user", "GET", "/api/specialists", None, 200, "specialists"),
        ("admin", "DELETE", "/api/specialists/no-such-one", None, 404, "no-such-one"),
    ],
}

FILES["settings"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/settings"): "settings.read",
        ("PUT", "/api/settings"): "settings.write",
    },
    "undeclared": {},
    "deny": [
        ("admin", "admin", "GET", "/api/settings", None, "settings.read"),
        ("admin", "admin", "PUT", "/api/settings", {}, "settings.write"),
    ],
    "allow": [
        ("owner", "GET", "/api/settings", None, 200, "preamble_md"),
    ],
}

FILES["sessions"] = {
    "mount": [("agent_sessions_router", ""), ("sessions_router", "")],
    "declared": {
        ("GET", "/api/agents/{slug}/sessions"): "agents.sessions.read",
        ("GET", "/api/sessions/{session_id}"): "sessions.read",
        ("GET", "/api/sessions"): "sessions.read",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "GET", "/api/sessions", None, "sessions.read"),
        ("user", "user", "GET", "/api/sessions/00000000-0000-0000-0000-000000000000", None,
         "sessions.read"),
    ],
    "allow": [
        ("admin", "GET", "/api/sessions", None, 200, "sessions"),
        ("user", "GET", "/api/agents/no-such-agent/sessions", None, 404, "no-such-agent"),
    ],
}

FILES["session_context"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/session-context-limits"): "session_context.read",
        ("PUT", "/api/session-context-limits/global"): "session_context.write",
        ("PUT", "/api/session-context-limits/agents/{agent_slug}"): "session_context.write",
        ("DELETE", "/api/session-context-limits/agents/{agent_slug}"): "session_context.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "GET", "/api/session-context-limits", None, "session_context.read"),
        ("admin", "admin", "PUT", "/api/session-context-limits/global", {},
         "session_context.write"),
    ],
    "allow": [
        ("admin", "GET", "/api/session-context-limits", None, 200, "global"),
    ],
}

FILES["secrets"] = {
    "mount": [("router", "/api/secrets")],
    "declared": {
        ("GET", "/api/secrets/refs"): "secrets.refs.read",
        ("POST", "/api/secrets"): "secrets.write",
        ("DELETE", "/api/secrets/{ref:path}"): "secrets.write",
        ("GET", "/api/secrets/resolve"): "secrets.read",
        ("GET", "/api/secrets/requests"): "secrets.refs.read",
        ("GET", "/api/secrets/snapshots/{ref}"): "secrets.snapshots.read",
        ("GET", "/api/secrets/snapshots/{ref}/{ts}"): "secrets.snapshots.read",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/secrets", {}, "secrets.write"),
        ("user", "user", "GET", "/api/secrets/snapshots/some.ref", None,
         "secrets.snapshots.read"),
    ],
    # Past the permission the store's own boundary still stands: a caller
    # with no session proof and no socket identity is refused by the route,
    # in the route's words, before anything is decrypted. The admin tier
    # holds the write and snapshot permissions and meets that boundary too.
    "allow": [
        ("admin", "GET", "/api/secrets/snapshots/some.ref", None, 403, "secret access denied"),
        ("admin", "DELETE", "/api/secrets/some.ref", None, 403, "secret access denied"),
        ("user", "GET", "/api/secrets/refs", None, 403, "secret access denied"),
        ("user", "GET", "/api/secrets/requests", None, 403, "secret access denied"),
        ("owner", "GET", "/api/secrets/resolve?ref=some.ref", None, 403, "secret access denied"),
    ],
}

FILES["runtimes"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/runtimes"): "runtimes.read",
        ("GET", "/api/runtimes/live"): "runtimes.read",
        ("GET", "/api/runtimes/{slug}"): "runtimes.read",
        ("POST", "/api/runtimes"): "runtimes.write",
        ("PUT", "/api/runtimes/{slug}"): "runtimes.write",
        ("DELETE", "/api/runtimes/{slug}"): "runtimes.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/runtimes", {}, "runtimes.write"),
        ("user", "user", "DELETE", "/api/runtimes/no-such-runtime", None, "runtimes.write"),
    ],
    "allow": [
        ("user", "GET", "/api/runtimes", None, 200, "runtimes"),
        ("admin", "DELETE", "/api/runtimes/no-such-runtime", None, 404, "no-such-runtime"),
    ],
}

FILES["references"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/references"): "references.read",
        ("GET", "/api/references/{key}"): "references.read",
        ("POST", "/api/references"): "references.write",
        ("PUT", "/api/references/{key}"): "references.write",
        ("DELETE", "/api/references/{key}"): "references.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/references", {}, "references.write"),
        ("user", "user", "DELETE", "/api/references/no-such-key", None, "references.write"),
    ],
    "allow": [
        ("user", "GET", "/api/references", None, 200, "references"),
        ("admin", "DELETE", "/api/references/no-such-key", None, 404, "no-such-key"),
    ],
}

FILES["provision"] = {
    "mount": [("router", "/api/provision")],
    "declared": {
        ("GET", "/api/provision/params"): "provision.read",
        ("PUT", "/api/provision/params"): "provision.write",
        ("POST", "/api/provision/params/{param_key}/retire"): "provision.write",
        ("POST", "/api/provision/project"): "provision.write",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "GET", "/api/provision/params", None, "provision.read"),
        ("user", "user", "POST", "/api/provision/project", None, "provision.write"),
    ],
    # Past the permission the route's own origin guard still stands, and this
    # client is neither loopback nor a trusted network.
    "allow": [
        ("admin", "GET", "/api/provision/params", None, 403, "provision requires a loopback"),
    ],
}

FILES["projects"] = {
    "mount": [("router", "")],
    "declared": {
        ("GET", "/api/projects"): "projects.read",
        ("GET", "/api/projects/{slug}"): "projects.read",
        ("POST", "/api/projects"): "projects.write",
        ("PATCH", "/api/projects/{slug}"): "projects.write",
        ("POST", "/api/projects/{slug}/members"): "projects.members.write",
        ("POST", "/api/projects/{slug}/reprovision"): "projects.reprovision",
        ("DELETE", "/api/projects/{slug}"): "projects.archive",
    },
    "undeclared": {},
    "deny": [
        ("user", "user", "POST", "/api/projects", {}, "projects.write"),
        ("user", "user", "PATCH", "/api/projects/no-such-project", {}, "projects.write"),
        ("user", "user", "POST", "/api/projects/no-such-project/members", {},
         "projects.members.write"),
        ("user", "user", "POST", "/api/projects/no-such-project/reprovision", None,
         "projects.reprovision"),
        ("user", "user", "DELETE", "/api/projects/no-such-project", None, "projects.archive"),
    ],
    "allow": [
        ("user", "GET", "/api/projects", None, 200, "projects"),
        ("admin", "GET", "/api/projects/no-such-project", None, 404, "no-such-project"),
    ],
}


def _mount(app: FastAPI, name: str) -> None:
    module = importlib.import_module("routes." + name)
    for attr, prefix in FILES[name]["mount"]:
        app.include_router(getattr(module, attr), prefix=prefix)


@pytest.mark.parametrize("name", sorted(FILES))
def test_router_declares_catalogue_permissions(name):
    """Structural: each handler is declared as the catalogue says, or listed."""
    from permissions import route_permission

    app = FastAPI()
    _mount(app, name)
    seen = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods - {"HEAD"}:
                seen[(method, route.path)] = route_permission(route)
    spec = FILES[name]
    listed = set(spec["declared"]) | set(spec["undeclared"])
    assert set(seen) == listed, (
        f"routes/{name}.py and the table list different handlers: "
        f"{sorted(set(seen) ^ listed)}"
    )
    assert not any(v[1] for v in seen.values() if v), "no permission here has an object axis"
    declared = {k: v[0] for k, v in seen.items() if v is not None}
    assert declared == spec["declared"], (
        "handler-to-permission mapping diverged from the catalogue: "
        f"{sorted(set(declared.items()) ^ set(spec['declared'].items()))}"
    )


@pytest.mark.parametrize("name", sorted(n for n in FILES if FILES[n]["declared"]))
def test_file_has_a_real_deny_and_a_real_allow(name):
    assert FILES[name]["deny"] and FILES[name]["allow"], name


@pytest.fixture(scope="module")
def node():
    """Every router of the table over one throwaway database."""
    import auth

    with disposable_database() as sql:
        who = {"p": None}
        people = principals(sql)
        app = FastAPI()
        for name in FILES:
            _mount(app, name)
        app.dependency_overrides[auth.get_principal] = lambda: who["p"]
        client = TestClient(app, raise_server_exceptions=False)

        def as_(name: str) -> TestClient:
            who["p"] = people[name]
            return client

        yield app, as_, sql


def test_every_declared_name_is_in_the_seeded_catalogue(node):
    """The startup scan admits what is declared; no object is owed."""
    from permissions import validate_routes

    validate_routes(node[0], strict_undeclared=False)
    names = sorted({p for spec in FILES.values() for p in spec["declared"].values()})
    quoted = ", ".join("'%s'" % n for n in names) or "''"
    with_axis = node[2](
        "SELECT name FROM sn_config.permissions "
        f"WHERE name IN ({quoted}) AND object_axis <> 'none'")
    assert with_axis == "", f"declared without an object, seeded with an axis: {with_axis}"


def _cases(kind):
    at = 1 if kind == "allow" else 2  # where the method sits in the tuple
    return [pytest.param(name, case, id=f"{name}:{case[at]} {case[at + 1]}")
            for name in sorted(FILES) for case in FILES[name][kind]]


@pytest.mark.parametrize("name,case", _cases("deny"))
def test_role_without_the_permission_is_refused_and_named(node, name, case):
    caller, role, method, path, body, permission = case
    assert FILES[name]["declared"], name
    r = node[1](caller).request(method, path, json=body)
    assert r.status_code == 403, f"{caller} {method} {path}: {r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert f"'{role}'" in detail and f"'{permission}'" in detail, detail


@pytest.mark.parametrize("name,case", _cases("allow"))
def test_role_with_the_permission_reaches_the_handler(node, name, case):
    caller, method, path, body, status, needle = case
    r = node[1](caller).request(method, path, json=body)
    assert r.status_code == status, f"{caller} {method} {path}: {r.status_code} {r.text}"
    assert needle in r.text, r.text
    assert "lacks permission" not in r.text, r.text
