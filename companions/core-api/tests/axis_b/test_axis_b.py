"""Axis B regressions without a database or a running node.

Run through tests/axis-b-core-api.test.sh. The fakes model the rows changed by
each route, while the real route and prompt composer execute.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from routes import agents, mcp
from schemas.agent import AgentCreate, AgentUpdate
from schemas.mcp import MCPToolsBulk


@contextmanager
def _using(cur):
    yield cur


def _request():
    return Request({"type": "http", "method": "PUT", "path": "/", "headers": []})


class _CreateCursor:
    def __init__(self):
        self.slugs = ["alex", "john", "max"]
        self.one = None
        self.many = []

    def execute(self, sql, params=None):
        if "INSERT INTO sn_config.agents" in sql:
            self.slugs.append("pam")
            self.one = {"id": 4}
        elif "LEFT JOIN sn_config.specialists" in sql:
            self.one = {"id": 4, "slug": "pam", "title": "Pam", "policy_categories": [],
                        "mesh_workspace": None, "spec_body": "You are Pam.", "spec_slug": "sysadmin"}
        elif "SELECT slug FROM sn_config.agents" in sql:
            self.many = [{"slug": slug} for slug in self.slugs]
        elif "FROM sn_config.users WHERE email" in sql:
            # The slug-is-free check ahead of creation: no account holds it.
            self.one = None
        elif "SELECT avatar FROM sn_config.agents" in sql:
            # A new agent takes the next free face.
            self.many = []
        elif sql.startswith("UPDATE sn_config.agents SET avatar"):
            pass
        elif "INSERT INTO sn_config.agent_tools" in sql:
            pass
        elif "FROM sn_config.agent_tools at" in sql:
            self.one = None
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def test_create_updates_every_existing_team_prompt(monkeypatch, tmp_path):
    cur = _CreateCursor()
    composed = []
    events = []
    def compose(_cur, slug):
        team = [f"- {other} -- colleague" for other in cur.slugs if other != slug]
        path = tmp_path / slug / "CLAUDE.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(agents.agent_fs.compose_claude_md("Role", team=team))
        composed.append(slug)
        return True
    for slug in cur.slugs:
        compose(cur, slug)
        assert (tmp_path / slug / "CLAUDE.md").read_text().count("-- colleague") == 2
    composed.clear()
    monkeypatch.setattr(agents, "cursor", lambda **kw: _using(cur))
    monkeypatch.setattr(agents, "_resolve_create_tenant", lambda *a: 1)
    monkeypatch.setattr(agents, "_canonical_model", lambda _c, model, *_a, **_k: model)
    monkeypatch.setattr(agents, "_ensure_agent_identity", lambda *a: None)
    monkeypatch.setattr(agents, "_compose_and_write_claude", compose)
    monkeypatch.setattr(agents, "_row_to_read", lambda *_a: SimpleNamespace(model_dump=lambda **_k: {}))
    monkeypatch.setattr(agents, "runner_op", lambda *_a: {"ok": True, "result": {"failed": 0, "ensured": 1, "agents": [{"slug": "pam", "os_user": "agent-pam"}]}})
    monkeypatch.setattr(agents, "_register_planner_actor", lambda *a: None)
    monkeypatch.setattr(agents, "emit", lambda _c, **event: events.append(event))
    monkeypatch.setattr(agents.agent_fs, "provision_agent_dir", lambda **_k: None)
    monkeypatch.setattr(agents.agent_fs, "remove_agent_dir", lambda *_a: None)

    agents.create_agent(AgentCreate(slug="pam", title="Pam"), actor="user:owner")

    assert set(composed) == {"alex", "john", "max", "pam"}
    # Each colleague once. The new agent twice: its own prompt inside the transaction,
    # so a message right after the commit finds it, then with the roster.
    assert sorted(composed) == ["alex", "john", "max", "pam", "pam"]
    for slug in cur.slugs:
        assert (tmp_path / slug / "CLAUDE.md").read_text().count("-- colleague") == 3
    assert any(e["event_type"] == "agent.roster_changed" for e in events)


class _UpdateCursor:
    def __init__(self):
        self.one = None
        self.many = []

    def execute(self, sql, params=None):
        if "SELECT id, version, specialist_id, os_user FROM sn_config.agents" in sql:
            self.one = {"id": 3, "version": "0.1.0", "specialist_id": None,
                        "os_user": "agent-max"}
        elif "UPDATE sn_config.agents SET" in sql or "INSERT INTO sn_config.changelog_entries" in sql:
            pass
        elif "LEFT JOIN sn_config.specialists" in sql:
            self.one = {"id": 3, "title": "Maxim", "model": "claude-cli/sonnet",
                        "role": "sysadmin", "description": ""}
        elif "SELECT slug FROM sn_config.agents" in sql:
            self.many = [{"slug": s} for s in ("alex", "john", "max")]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def test_renaming_agent_updates_colleagues_team_prompt(monkeypatch):
    cur = _UpdateCursor()
    composed = []
    monkeypatch.setattr(agents, "cursor", lambda **kw: _using(cur))
    monkeypatch.setattr(agents, "_row_to_read", lambda *_a: SimpleNamespace(model_dump=lambda **_k: {}))
    monkeypatch.setattr(agents, "_perms_from_row", lambda *_a: {})
    monkeypatch.setattr(agents, "_compose_and_write_claude", lambda _c, slug: composed.append(slug) or True)
    monkeypatch.setattr(agents, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(agents.agent_fs, "append_changelog", lambda *a: None)
    monkeypatch.setattr(agents.agent_fs, "rerender_config", lambda *a, **kw: None)

    agents.update_agent("max", AgentUpdate(title="Maxim"))
    assert set(composed) == {"alex", "john", "max"}
    assert len(composed) == 3


class _KnowledgeCursor:
    def __init__(self, knowledge=None):
        self.one = None
        self.many = []
        self.knowledge = knowledge or [{"kind": "text", "title": "Owner rule",
                                        "content": "Always confirm costs."}]

    def execute(self, sql, params=None):
        if "FROM sn_config.agents a LEFT JOIN" in sql:
            self.one = {"id": 3, "title": "Max", "policy_categories": [], "mesh_workspace": None,
                        "spec_body": "You are Max.", "spec_slug": "sysadmin"}
        elif "FROM sn_config.agent_knowledge" in sql:
            self.many = self.knowledge
        elif "FROM sn_config.agent_tools at" in sql:
            # Whether the agent holds a tool: this one holds none of those asked about.
            self.one = None
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def test_text_knowledge_reaches_composed_prompt(monkeypatch):
    cur = _KnowledgeCursor()
    for name in ("_agent_tool_contracts", "_agent_policies", "_team_directory", "_agent_documents",
                 "_agent_workflows", "_agent_contacts", "_agent_channels"):
        monkeypatch.setattr(agents, name, lambda *_a: [])
    monkeypatch.setattr(agents.agent_fs, "people_directory", lambda *_a: [])

    body = agents._compose_body(cur, "max")

    assert "# Knowledge" in body
    assert "Always confirm costs." in body


def test_link_knowledge_names_read_path(monkeypatch):
    cur = _KnowledgeCursor([{"kind": "link", "title": "Expense policy", "content": None}])
    for name in ("_agent_tool_contracts", "_agent_policies", "_team_directory", "_agent_documents",
                 "_agent_workflows", "_agent_contacts", "_agent_channels"):
        monkeypatch.setattr(agents, name, lambda *_a: [])
    monkeypatch.setattr(agents.agent_fs, "people_directory", lambda *_a: [])
    body = agents._compose_body(cur, "max")
    assert "## Expense policy" in body
    assert "Documents you can read" in body


class _GrantCursor:
    def __init__(self, catalogue, rows=None):
        self.catalogue = catalogue
        self.rows = dict(rows or {})
        self.one = None
        self.many = []

    def execute(self, sql, params=None):
        if "SELECT name, id FROM sn_core.tools" in sql:
            self.many = [{"name": name, "id": ident} for name, ident in self.catalogue.items()]
        elif "DELETE FROM sn_config.agent_tools" in sql:
            self.rows.clear()
        elif "INSERT INTO sn_config.agent_tools" in sql:
            tool_id = params[1]
            name = next(name for name, ident in self.catalogue.items() if ident == tool_id)
            self.rows[name] = params[2]
        elif "LEFT JOIN sn_config.specialists" in sql:
            self.one = {"id": 7, "title": "Max", "tenant_id": 1, "model": "claude-cli/sonnet",
                        "role": "sysadmin", "description": ""}
        elif "FROM sn_config.mcp_servers" in sql:
            self.many = []
        elif "JOIN sn_core.tools" in sql:
            self.many = [{"name": name, "granted_caps": caps} for name, caps in self.rows.items()]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def _grant_route(monkeypatch, tmp_path, catalogue, rows=None, old=None):
    cur = _GrantCursor(catalogue, rows)
    events = []
    root = tmp_path / "max"
    root.mkdir()
    if old is not None:
        (root / "grants.json").write_text(json.dumps(old))
    monkeypatch.setattr(agents, "cursor", lambda **kw: _using(cur))
    monkeypatch.setattr(agents, "_resolve_agent_dir", lambda _slug: root)
    monkeypatch.setattr(agents, "_ensure_agent_identity", lambda *a: None)
    monkeypatch.setattr(agents, "_perms_from_row", lambda *a: {})
    monkeypatch.setattr(agents, "_compose_and_write_claude", lambda *a: True)
    monkeypatch.setattr(agents, "get_actor", lambda *a: "user:owner")
    monkeypatch.setattr(agents, "emit", lambda _c, **event: events.append(event))
    monkeypatch.setattr(agents.agent_fs, "rerender_config", lambda *a, **kw: None)
    monkeypatch.setattr(agents.agent_fs, "write_mcp_json", lambda *a, **kw: None)
    return cur, root, events


@pytest.mark.parametrize("name", ["screenbox", "analytics", "google-ads",
                                  "laptop-operator", "operator"])
def test_unknown_grant_is_rejected_and_audited(monkeypatch, tmp_path, name):
    cur, root, events = _grant_route(monkeypatch, tmp_path, {"mesh": 1})
    with pytest.raises(HTTPException) as error:
        agents.put_grants("max", agents.GrantsPayload(grants={name: "r--"}), _request(), merge=False)
    assert error.value.status_code == 400
    assert name in str(error.value.detail)
    assert not (root / "grants.json").exists()
    assert any(e["event_type"] == "grant.rejected" and e["payload"]["tool"] == name for e in events)


def test_secrets_grant_preserves_its_prompt_contract(monkeypatch, tmp_path):
    cur, _root, events = _grant_route(
        monkeypatch, tmp_path, {"secrets": 1, "secrets_resolve": 2},
        {"secrets_resolve": ["read"]},
        {"grants": {"secrets": "r--"}, "secret_refs": []},
    )
    monkeypatch.setattr("routes.secrets._operator_actor", lambda *a: "user:owner")
    agents.put_grants("max", agents.GrantsPayload(grants={"secrets": "r--"}), _request(), merge=False)
    assert "secrets" in cur.rows
    assert "secrets_resolve" in cur.rows
    assert not any(e["event_type"] == "grant.revoked" for e in events)


def test_database_failure_restores_grants_file(monkeypatch, tmp_path):
    cur, root, _events = _grant_route(
        monkeypatch, tmp_path, {"mesh": 1}, {"mesh": ["read"]},
        {"grants": {"mesh": "r--"}, "secret_refs": []},
    )
    path = root / "grants.json"
    previous = path.read_bytes()
    execute = cur.execute

    def fail_delete(sql, params=None):
        if "DELETE FROM sn_config.agent_tools" in sql:
            raise RuntimeError("database write failed")
        execute(sql, params)

    monkeypatch.setattr(cur, "execute", fail_delete)
    with pytest.raises(RuntimeError, match="database write failed"):
        agents.put_grants("max", agents.GrantsPayload(grants={"mesh": "rw-"}),
                          _request(), merge=False)
    assert path.read_bytes() == previous


class _ArchiveCursor:
    def __init__(self):
        self.one = None
        self.many = []
        self.budget_enabled = True
        self.budget_scope = None

    def execute(self, sql, params=None):
        if "UPDATE sn_config.agents" in sql:
            self.one = {"id": 9, "specialist_id": None}
        elif "UPDATE sn_config.budgets" in sql:
            self.budget_scope = params[0]
            self.budget_enabled = False
            self.many = [{"id": 21}]
        elif "SELECT slug FROM sn_config.agents" in sql:
            self.many = [{"slug": "alex"}, {"slug": "john"}]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


def test_archive_disables_agent_budget(monkeypatch):
    cur = _ArchiveCursor()
    events = []
    monkeypatch.setattr(agents, "cursor", lambda **kw: _using(cur))
    monkeypatch.setattr(agents, "emit", lambda _c, **event: events.append(event))
    composed = []
    monkeypatch.setattr(agents, "_compose_and_write_claude", lambda _c, slug: composed.append(slug) or True)
    monkeypatch.setattr(agents.agent_fs, "archive_agent_dir", lambda *a: None)
    agents.delete_agent("max")
    assert cur.budget_enabled is False
    assert cur.budget_scope == "9"
    assert composed == ["alex", "john"]
    assert any(e["event_type"] == "agent.roster_changed" for e in events)
    assert any(e["event_type"] == "budget.disabled" and e["payload"]["reason"] == "agent-archived" for e in events)


class _McpCursor:
    def __init__(self):
        self.one = None
        self.names = []

    def execute(self, sql, params=None):
        if "SELECT id FROM sn_config.mcp_servers" in sql:
            self.one = {"id": 5}
        elif "INSERT INTO sn_core.tools" in sql:
            self.names.append(params[0])
        elif "UPDATE sn_config.mcp_servers" in sql:
            pass
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.one


def test_mcp_discovery_publishes_umbrella_grant(monkeypatch):
    cur = _McpCursor()
    monkeypatch.setattr(mcp, "cursor", lambda **kw: _using(cur))
    monkeypatch.setattr(mcp, "get_actor", lambda *a: "user:owner")
    monkeypatch.setattr(mcp, "emit", lambda *a, **kw: None)
    mcp.bulk_upsert_tools("screenbox", MCPToolsBulk(tools=[]), _request())
    assert "screenbox" in cur.names


def test_get_grants_shows_rows_granted_only_in_the_database(monkeypatch, tmp_path):
    # seed-tools grants messaging and its rows straight into agent_tools, so
    # grants.json can be empty while the agent holds them. A page that read the
    # file, added one switch and saved the whole set back took all of them away
    # (2026-10-30: Twine lost eleven tools when Mesh was switched on).
    (tmp_path / "grants.json").write_text(json.dumps({"grants": {"mesh": "rw-"}, "secret_refs": []}))
    monkeypatch.setattr(agents, "_resolve_agent_dir", lambda slug: tmp_path)
    monkeypatch.setattr(agents, "_db_grant_modes", lambda slug: {"messaging": "r--", "mesh": "r--"})
    got = agents.get_grants("max")["grants"]
    assert got["messaging"] == "r--"
    assert got["mesh"] == "rw-"


class _PinCursor:
    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return {"id": 7}


def test_an_uploaded_file_is_pinned_so_the_agent_always_reads_it(monkeypatch):
    # "Always keeps in mind": an unpinned file reached the prompt as a title and
    # "search your memory" (or "ask the owner" without the memory grant), so
    # the agent did not see it
    from routes import knowledge
    seen = {}
    monkeypatch.setattr(knowledge, "cursor", lambda commit=False: _using(_PinCursor()))
    monkeypatch.setattr(knowledge, "_kinds_with_availability", lambda cur: [{"kind": "file", "available": True}])
    monkeypatch.setattr(knowledge, "_get_agent", lambda cur, slug: {"id": 1, "tenant_id": 1, "mesh_workspace": "agent-max"})
    monkeypatch.setattr(knowledge, "_snapshot_content", lambda payload: "# Prices\n\nTea 3 USD")
    monkeypatch.setattr(knowledge, "_mesh_add", lambda ws, content, tags, filename: seen.setdefault("tags", tags) and "g1")
    monkeypatch.setattr(knowledge, "_compose_and_write_claude", lambda cur, slug: True)
    got = knowledge.add_knowledge("max", knowledge.KnowledgeCreate(kind="file", filename="prices.md", file_b64="eA=="),
                                  principal=SimpleNamespace(username="admin"))
    assert got["delivered"] is True
    assert "pin" in seen["tags"]


def test_a_provider_that_does_not_answer_is_not_saved(monkeypatch):
    # The first-run page lets no one in before the AI answers:
    # Connect asks the address once; a refused key says so and writes nothing.
    import io
    import urllib.error
    from routes import gateway_secrets as gs
    opened = []
    monkeypatch.setattr(gs, "_require_session_owner", lambda request: SimpleNamespace(username="admin"))
    monkeypatch.setattr(gs, "cursor", lambda commit=False: opened.append(1) or _using(None))
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))
    monkeypatch.setattr(gs.urllib.request, "urlopen", refuse)
    payload = gs.CustomProvider(base_url="https://router.example.com", model="m1", api_key="k", runtime="flint")
    with pytest.raises(HTTPException) as e:
        gs.add_custom_provider(payload, _request())
    assert e.value.status_code == 400 and "did not accept the key" in e.value.detail
    assert opened == []


def test_remembers_says_where_the_memory_is_kept(monkeypatch):
    # the agent page labels the list: a runtime's own memory, the node's, or an
    # external store, so the answer names it
    monkeypatch.setattr(agents, "runner_op", lambda *a, **k: {"result": {"items": []}})
    monkeypatch.setattr(agents, "_memory_agent", lambda slug: {"runtime": "flint-cli", "os_user": "agent-max"})
    got = agents.get_agent_remembers("max", principal=type("P", (), {"username": "admin"})())
    assert (got["store"], got["runtime"]) == ("runtime", "Flint")
    monkeypatch.setattr(agents, "_memory_agent", lambda slug: {"runtime": "claude-cli", "os_user": "agent-max"})
    got = agents.get_agent_remembers("max", principal=type("P", (), {"username": "admin"})())
    assert (got["supported"], got["store"], got["runtime"]) == (False, None, "Claude Code")


def test_an_address_that_does_not_resolve_is_said_in_plain_words(monkeypatch):
    # The first-run page showed "[Errno -2] Name or service not known" (tester
    # 2026-10-30): a person reads a reason, not an error number.
    import socket
    import urllib.error
    from routes import gateway_secrets as gs
    monkeypatch.setattr(gs, "_require_session_owner", lambda request: SimpleNamespace(username="admin"))
    def no_name(req, timeout=None):
        raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(gs.urllib.request, "urlopen", no_name)
    payload = gs.CustomProvider(base_url="https://router.invalid", model="m1", api_key="k", runtime="flint")
    with pytest.raises(HTTPException) as e:
        gs.add_custom_provider(payload, _request())
    assert e.value.detail == "there is no such address: the name does not resolve"


def test_an_api_root_is_kept_when_the_address_names_its_version():
    # One rule for every OpenAI-format address: presets need no code of their own.
    from routes import gateway_secrets as gs
    assert gs._openai_root("https://api.openai.com") == "https://api.openai.com/v1"
    assert gs._openai_root("https://openrouter.ai/api") == "https://openrouter.ai/api/v1"
    assert gs._openai_root("https://router.example.com/v1") == "https://router.example.com/v1"
    assert (gs._openai_root("https://generativelanguage.googleapis.com/v1beta/openai")
            == "https://generativelanguage.googleapis.com/v1beta/openai")


def test_the_models_of_an_address_are_listed_for_the_key(monkeypatch):
    # A person picks a model from what the provider offers instead of knowing its name.
    import io
    import json as _json
    from routes import gateway_secrets as gs
    monkeypatch.setattr(gs, "_require_session_owner", lambda request: SimpleNamespace(username="admin"))
    seen = {}
    def answer(req, timeout=None):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        r = io.BytesIO(_json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}]}).encode())
        r.__enter__ = lambda *a: r
        r.__exit__ = lambda *a: False
        return r
    monkeypatch.setattr(gs.urllib.request, "urlopen", answer)
    out = gs.provider_models(gs.ProviderModels(base_url="https://openrouter.ai/api", api_key="k", runtime="flint"),
                             _request())
    assert out == {"models": ["a-model", "b-model"]}
    assert seen == {"url": "https://openrouter.ai/api/v1/models", "auth": "Bearer k"}


def test_an_agent_is_told_which_modules_this_node_has(monkeypatch, tmp_path):
    # Told only what the release can do, an agent offered email and workflows on
    # a node with neither (tester 2026-10-30). The modules are chosen at install,
    # so the prompt says what is installed here and what is not, and local or not.
    from lib import agent_fs
    man, comp = tmp_path / "manifests", tmp_path / "companions.d"
    man.mkdir(); comp.mkdir()
    for name, title, provides, core in [("studio", "Studio", "the web workspace", True),
                                         ("flint", "Flint", "an agent runtime", False),
                                         ("communication", "Channels", "email, Telegram", False),
                                         ("memory", "Memory", "long-term memory (Mesh)", False)]:
        (man / f"{name}.manifest.json").write_text(
            '{"name": "%s", "title": "%s", "provides": "%s"%s}' % (name, title, provides, ', "core": true' if core else ""))
    for name in ("studio", "flint"):
        (comp / f"{name}.json").write_text("{}")
    monkeypatch.setattr(agent_fs, "MANIFESTS_DIR", str(man))
    monkeypatch.setattr(agent_fs, "COMPANIONS_DIR", str(comp))
    monkeypatch.setenv("SMART_NODE_DOMAIN", "sn.localhost")
    facts = agent_fs.node_facts()
    assert "Installed here: Flint (an agent runtime)." in facts
    assert "Not installed here: Channels (email, Telegram); Memory (long-term memory)." in facts
    assert "Studio" not in facts and "Mesh" not in facts
    assert "This node is local" in facts
