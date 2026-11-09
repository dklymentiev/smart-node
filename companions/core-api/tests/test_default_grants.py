"""What every agent is given comes from the registry's default marks.

A tool with sn_core.tools.default_caps is granted to an agent when it is created
and to every agent when core-api starts; an MCP server with default_on is in
every agent's .mcp.json. Before, the installer granted a fixed list to the agents
that existed when it ran, so an agent created between two installs had the
messaging and reminders servers and none of their contracts (2026-10-29).
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from disposable_pg import disposable_database

_NO_AGENTS = os.path.join(tempfile.gettempdir(), "snperm-no-agents")
os.environ.setdefault("SN_AGENTS_ROOT", _NO_AGENTS)
os.environ.setdefault("SN_AGENT_TEMPLATE", os.path.join(_NO_AGENTS, "_template"))


@pytest.fixture(scope="module")
def sql():
    with disposable_database() as sql:
        tenant = int(sql("SELECT min(id) FROM sn_config.tenants"))
        for slug in ("olddog", "newpup"):
            sql("INSERT INTO sn_config.agents (slug, title, enabled, tenant_id) VALUES "
                f"('{slug}', '{slug}', true, {tenant})")
        sql("INSERT INTO sn_core.tools (name, kind, title, description, status, created_by, updated_by) "
            "VALUES ('every_agent_tool', 'api', 't', 'd', 'available', 'test', 'test'), "
            "       ('granted_tool', 'api', 't', 'd', 'available', 'test', 'test')")
        sql("UPDATE sn_core.tools SET default_caps = '{read}' WHERE name = 'every_agent_tool'")
        yield sql


def _granted(sql, slug):
    out = sql("SELECT coalesce(string_agg(t.name, ',' ORDER BY t.name), '') FROM sn_config.agent_tools at "
              "JOIN sn_core.tools t ON t.id = at.tool_id JOIN sn_config.agents a ON a.id = at.agent_id "
              f"WHERE a.slug = '{slug}'")
    return [x for x in out.split(",") if x]


def test_one_agent_gets_what_is_marked_default(sql):
    from db import cursor
    from lib import default_grants
    agent_id = int(sql("SELECT id FROM sn_config.agents WHERE slug = 'newpup'"))
    with cursor(commit=True) as cur:
        default_grants.apply(cur, agent_id)
    assert _granted(sql, "newpup") == ["every_agent_tool"]
    assert _granted(sql, "olddog") == []          # one agent means that one only
    with cursor(commit=True) as cur:
        default_grants.apply(cur)                 # core-api start: every agent
    assert _granted(sql, "olddog") == ["every_agent_tool"]


def test_a_default_server_is_in_every_mcp_json(sql, tmp_path, monkeypatch):
    from db import cursor
    from routes import agents
    sql("INSERT INTO sn_config.mcp_servers (name, slug, transport, config_jsonb, enabled, default_on) VALUES "
        "('e', 'everyone_srv', 'stdio', '{\"command\": \"x\"}', true, true), "
        "('g', 'granted_srv', 'stdio', '{\"command\": \"y\"}', true, false), "
        "('o', 'other_srv', 'stdio', '{\"command\": \"z\"}', true, false)")
    written = {}
    monkeypatch.setattr(agents.agent_fs, "write_mcp_json",
                        lambda slug, rows: written.update({slug: sorted(r["slug"] for r in rows)}))
    with cursor() as cur:
        agents._write_agent_mcp(cur, "newpup", {"granted_srv"})
    assert written["newpup"] == ["everyone_srv", "granted_srv"]
