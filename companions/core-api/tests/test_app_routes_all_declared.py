"""The whole application passes the strict startup scan.

Asked of the real `main:app`, not of one router at a time, against a database
built from the tree's bootstrap files: every API route declares a permission,
every declared name is a catalogue row, and every permission with an object
axis is declared with an object. This is the check the service runs when it
starts, so a route added without a declaration fails here before it can stop
a node.
"""

from __future__ import annotations

import os
import tempfile

from fastapi.routing import APIRoute

from permission_db import permission_db  # noqa: F401

# Some routers read these at import. Nothing here writes an agent directory.
_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))

# Answered without a permission by design: the liveness probes.
OPEN = {"/api", "/api/health"}


def _api_routes():
    import main

    return [r for r in main.app.routes if isinstance(r, APIRoute) and r.path not in OPEN]


def test_the_real_application_passes_the_strict_scan(permission_db):
    import main
    from permissions import validate_routes

    validate_routes(main.app, strict_undeclared=True)


def test_every_route_of_the_real_application_carries_a_declaration(permission_db):
    from permissions import route_permission

    routes = _api_routes()
    undeclared = sorted(f"{sorted(r.methods)} {r.path}" for r in routes
                        if route_permission(r) is None)
    assert undeclared == [], f"routes with no permission declared: {undeclared}"
    # The scan must be looking at the application and not at an empty shell.
    assert len(routes) > 200, len(routes)


def test_the_service_starts_with_the_strict_scan():
    """The startup call is the strict one: an undeclared route stops the
    service when it starts instead of answering 403 one request at a time."""
    import inspect

    import main

    source = inspect.getsource(main.lifespan)
    assert "validate_routes(app, strict_undeclared=True)" in source
    assert "strict_undeclared=False" not in source


def _installer_service_list() -> dict[str, list[str]]:
    """The permission list as the installer writes it."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "core-api.sh").read_text(encoding="utf-8")
    m = re.search(r'^SERVICE_PERMISSIONS_DEFAULT="\$\{SMART_NODE_SERVICE_PERMISSIONS:-([^}]*)\}"',
                  text, re.M)
    assert m, "the installer no longer declares the service permission list"
    out = {}
    for clause in m.group(1).split(";"):
        service, _, names = clause.partition(":")
        out[service] = [n for n in names.split(",") if n]
    return out


def test_a_handler_a_service_can_reach_copes_with_no_account(permission_db):
    """A listed service is admitted with no account, and the permission check
    then hands the route None. Every handler that takes its caller from the
    check, on a permission the installer lists for a service, must say so in
    its signature: a handler that expects an account would fail on the first
    call a service makes to it."""
    import inspect

    from fastapi import params
    from permissions import route_permission

    listed = {n for names in _installer_service_list().values() for n in names}
    unsafe = []
    for route in _api_routes():
        declared = route_permission(route)
        if declared is None or declared[0] not in listed:
            continue
        for name, param in inspect.signature(route.endpoint).parameters.items():
            default = param.default
            if not isinstance(default, params.Depends):
                continue
            if not getattr(default.dependency, "_permission_name", None):
                continue
            if "None" not in str(param.annotation):
                unsafe.append(f"{route.path} ({declared[0]}): {name}: {param.annotation}")
    assert unsafe == [], unsafe


def test_every_permission_the_installer_lists_is_one_a_service_can_be_admitted_to(permission_db):
    listed = sorted({n for names in _installer_service_list().values() for n in names})
    with permission_db() as cur:
        cur.execute("SELECT name FROM sn_config.permissions "
                    "WHERE name = ANY(%s) AND object_axis = 'none'", (listed,))
        seeded = sorted(r["name"] for r in cur.fetchall())
    assert seeded == listed


SECRET = "s" * 64

# What the dispatcher, its runner and the gateway ask core-api as themselves.
# Before the routes were declared these took no identity at all.
SERVICE_CALLS = [
    ("brin", "GET", "/api/agents/with-pending-work"),
    ("brin", "GET", "/api/agents/state"),
    ("brin", "GET", "/api/agents/no-such-agent"),
    ("brin", "GET", "/api/agents/no-such-agent/tools"),
    ("brin", "GET", "/api/agents/no-such-agent/system-prompt"),
    ("brin", "POST", "/api/agents/no-such-agent/runtime"),
    ("brin", "GET", "/api/mcp/servers"),
    ("brin", "GET", "/api/budgets/check-warnings"),
    ("brin", "GET", "/api/users/resolve-identity?identity=nobody"),
    ("brin", "GET", "/api/users/cross-channel-history?identity=nobody"),
    ("gateway", "GET", "/api/agents/no-such-agent/system-prompt"),
    ("core-api", "GET", "/api/agents/no-such-agent/system-prompt"),
    ("core-api", "POST", "/api/agents/no-such-agent/regenerate"),
]


def _ask(method, path, headers):
    import asyncio

    import httpx
    import main

    async def _go():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 50000),
                                        raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://core-api") as c:
            return await c.request(method, path, headers=headers,
                                   json={"action": "release"} if method == "POST" else None)

    return asyncio.run(_go())


def test_node_services_reach_what_they_ask_core_api_as_themselves(permission_db, monkeypatch):
    """On the real application, with the list the installer writes: each call
    a service makes as itself gets the handler's answer, and the same call
    under a typed service name is refused."""
    import pytest

    monkeypatch.setenv("SN_SERVICE_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SN_SERVICE_PERMISSIONS", ";".join(
        f"{svc}:{','.join(names)}" for svc, names in _installer_service_list().items()))
    refused, typed_admitted = [], []
    for service, method, path in SERVICE_CALLS:
        r = _ask(method, path, {"X-SN-Service": service, "X-SN-Service-Auth": SECRET})
        if r.status_code in (401, 403, 500, 503):
            refused.append(f"{service} {method} {path}: {r.status_code} {r.text[:120]}")
        r = _ask(method, path, {"Remote-User": service})
        if r.status_code != 403:
            typed_admitted.append(f"{service} {method} {path}: {r.status_code}")
    assert refused == [], refused
    assert typed_admitted == [], typed_admitted
    # A service is admitted to its own list and to nobody else's.
    r = _ask("GET", "/api/agents/with-pending-work",
             {"X-SN-Service": "voice", "X-SN-Service-Auth": SECRET})
    assert r.status_code == 403, r.text
    r = _ask("POST", "/api/agents/no-such-agent/runtime",
             {"X-SN-Service": "gateway", "X-SN-Service-Auth": SECRET})
    assert r.status_code == 403, r.text
