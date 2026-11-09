"""The secrets MCP write tools, end to end on the socket.

A sysadmin agent asked to hand a credential to a colleague used to answer that
granting is an operator action: core-api already admitted it (capability
sysadmin is an approved secrets writer, role admin holds secrets.write and
agents.grants.write), but the MCP server offered resolve_secret only.

Here the real secrets and agents routers run under uvicorn on a unix socket,
against a throwaway database built from the tree's bootstrap files, and the MCP
tools are called the way an agent session calls them. The kernel names the
caller (SO_PEERCRED: this test's own uid, mapped to an agent row), so nothing in
the identity or permission path is replaced.

1. The sysadmin stores a secret with set_secret and grants it to a colleague
   with grant_secret_ref; the colleague's grants gain the ref and the read-only
   `secrets` tool, and its other grants stay.
2. The same calls from an agent with an ordinary capability are refused by
   core-api with 403; the MCP tools carry no policy of their own.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

# The agents router reads both at import; the fixture points it at its own dir.
_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))

from disposable_pg import disposable_database  # noqa: E402

import mcp_secrets  # noqa: E402
from lib import secret_store  # noqa: E402

CALLER = "sysadmin"
COLLEAGUE = "dev"


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    import uvicorn
    from fastapi import FastAPI

    import routes.agents as agents_routes
    from routes import agents, secrets

    work = tmp_path_factory.mktemp("node")
    key = work / "node-secret.key"
    key.write_text("k" * 64 + "\n")
    agents_root = work / "agents"
    (agents_root / COLLEAGUE).mkdir(parents=True)
    (agents_root / COLLEAGUE / "grants.json").write_text(json.dumps(
        {"grants": {"doci": "rw-"}, "secret_refs": []}))
    (agents_root / CALLER).mkdir()
    sock = str(work / "core-api.sock")

    mp = pytest.MonkeyPatch()
    mp.setattr(secret_store, "KEY_FILE", str(key))
    mp.setenv("SN_AGENTS_ROOT", str(agents_root))
    mp.setattr(agents_routes, "AGENTS_ROOT", agents_root)
    mp.setattr(mcp_secrets, "CORE_API_SOCKET", sock)
    mp.setattr(mcp_secrets, "CORE_API_URL", "")
    mp.setenv("SN_AGENT_SLUG", CALLER)

    with disposable_database() as sql:
        sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        # The agent this process is, to the kernel: our own OS user owns it.
        # Its identity rows come from the same helper agent creation uses, so
        # the role is the one capability sysadmin gets on a real node.
        sql(f"""
            INSERT INTO sn_core.actors (id, display_name, type, layer, kind, relation)
            VALUES ('agent:{CALLER}', 'Sysadmin', 'agent', 'specialist', 'agent', 'internal')
            ON CONFLICT (id) DO NOTHING;
            INSERT INTO sn_config.agents (tenant_id, slug, title, capability, os_user, actor_id)
            VALUES (1, '{CALLER}', 'Sysadmin', 'sysadmin', '{getpass.getuser()}',
                    'agent:{CALLER}');
            INSERT INTO sn_core.tools (name, kind, title, status, created_by, updated_by)
            VALUES ('secrets', 'mcp', 'Secrets', 'available', 'seed', 'seed'),
                   ('secrets_resolve', 'api', 'Secrets contract', 'available', 'seed', 'seed'),
                   ('doci', 'mcp', 'Documents', 'available', 'seed', 'seed')
            ON CONFLICT (name) DO NOTHING;
        """)
        from db import cursor
        with cursor(commit=True) as cur:
            agents._ensure_agent_identity(cur, CALLER, "Sysadmin", 1)
        assert sql(f"SELECT role FROM sn_core.actor_roles "
                   f"WHERE actor_id = 'agent:{CALLER}'") == "admin"
        app = FastAPI()
        app.include_router(secrets.router, prefix="/api/secrets")
        app.include_router(agents.router)
        server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="warning",
                                               lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started:
            if time.time() > deadline:
                pytest.fail("cannot run: core-api did not start on its socket")
            time.sleep(0.05)
        try:
            yield sql, agents_root
        finally:
            server.should_exit = True
            thread.join(10)
    mp.undo()


def _run(coro):
    return json.loads(asyncio.run(coro))


def test_the_sysadmin_stores_a_secret_and_grants_it(node):
    sql, agents_root = node
    out = _run(mcp_secrets.set_secret("google.gemini_api_key", "AIza-not-printed"))
    assert out.get("ref") == "google.gemini_api_key", out
    assert out.get("actor") == CALLER
    assert "AIza-not-printed" not in json.dumps(out)
    assert sql("SELECT count(*) FROM sn_config.secrets "
               "WHERE ref = 'google.gemini_api_key'") == "1"

    out = _run(mcp_secrets.grant_secret_ref(" Dev", "google.gemini_api_key"))
    assert out == {"ok": True, "agent": COLLEAGUE, "ref": "google.gemini_api_key",
                   "secret_refs": ["google.gemini_api_key"]}, out
    grants = json.loads((agents_root / COLLEAGUE / "grants.json").read_text())
    assert grants["secret_refs"] == ["google.gemini_api_key"]
    assert grants["grants"]["secrets"] == "r--"
    # A merge: what the colleague held before is still there.
    assert grants["grants"]["doci"] == "rw-"

    # A second ref adds to the allowlist rather than replacing it.
    _run(mcp_secrets.grant_secret_ref(COLLEAGUE, "gitlab.*"))
    grants = json.loads((agents_root / COLLEAGUE / "grants.json").read_text())
    assert grants["secret_refs"] == ["gitlab.*", "google.gemini_api_key"]


def test_an_ordinary_agent_is_refused_by_core_api(node):
    sql, agents_root = node
    before = (agents_root / COLLEAGUE / "grants.json").read_text()
    sql(f"UPDATE sn_config.agents SET capability = 'developer' WHERE slug = '{CALLER}';"
        f"DELETE FROM sn_core.actor_roles WHERE actor_id = 'agent:{CALLER}'")
    try:
        out = _run(mcp_secrets.set_secret("google.other_key", "v"))
        assert out.get("error") == "HTTP 403", out
        assert sql("SELECT count(*) FROM sn_config.secrets "
                   "WHERE ref = 'google.other_key'") == "0"
        out = _run(mcp_secrets.grant_secret_ref(COLLEAGUE, "google.other_key"))
        assert out.get("error") == "HTTP 403", out
        assert (agents_root / COLLEAGUE / "grants.json").read_text() == before
    finally:
        sql(f"UPDATE sn_config.agents SET capability = 'sysadmin' WHERE slug = '{CALLER}';"
            f"INSERT INTO sn_core.actor_roles (actor_id, role, source) "
            f"VALUES ('agent:{CALLER}', 'admin', 'class_default') "
            f"ON CONFLICT (actor_id) DO NOTHING")
