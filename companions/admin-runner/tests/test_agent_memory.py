"""agent_memory: list and forget what a Flint agent remembers about people.

The script is run here directly (the op runs it as the agent via sudo) against a
store with Flint's own schema: forgetting must remove the entry from both its
table and the search index, and must leave how the agent works (patterns) alone.
"""
import json
import sqlite3
import subprocess
import sys

import pytest

import operations
from operations import _AGENT_MEMORY_PY, _agent_memory


def _store(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE facts (id TEXT PRIMARY KEY, category TEXT, content TEXT NOT NULL,
            source TEXT, confidence REAL, created_at TEXT, project TEXT, pinned INTEGER);
        CREATE TABLE user_model (id TEXT PRIMARY KEY, trait TEXT NOT NULL, value TEXT,
            evidence TEXT, confidence REAL, updated_at TEXT);
        CREATE TABLE patterns (id TEXT PRIMARY KEY, ts TEXT NOT NULL);
        CREATE VIRTUAL TABLE memory_fts USING fts5(layer, entry_id, text);
        INSERT INTO facts (id, content, created_at) VALUES ('fact-1', 'Name is Alex', '2026-10-28 19:45:23');
        INSERT INTO user_model (id, trait, value, updated_at) VALUES ('trait-language', 'language', 'ru', '2026-10-28 20:58:40');
        INSERT INTO patterns VALUES ('p1', 'x');
        INSERT INTO memory_fts VALUES ('facts', 'fact-1', 'Name is Alex'),
            ('user_model', 'trait-language', 'ru'), ('patterns', 'p1', 'x');
    """)
    con.commit()
    con.close()


def _run(action, db, entry="-"):
    out = subprocess.run([sys.executable, "-I", "-c", _AGENT_MEMORY_PY, action, str(db), entry],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_list_shows_facts_and_traits(tmp_path):
    db = tmp_path / "index.sqlite"
    _store(db)
    items = _run("list", db)["items"]
    assert {(i["id"], i["kind"]) for i in items} == {("fact-1", "fact"), ("trait-language", "trait")}
    assert any(i["text"] == "language: ru" for i in items)


def test_forget_one_removes_it_from_the_search_index_too(tmp_path):
    db = tmp_path / "index.sqlite"
    _store(db)
    assert _run("forget", db, "fact-1")["removed"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM memory_fts WHERE entry_id='fact-1'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM user_model").fetchone()[0] == 1


def test_forget_all_keeps_how_the_agent_works(tmp_path):
    db = tmp_path / "index.sqlite"
    _store(db)
    assert _run("forget_all", db)["removed"] == 2
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM patterns").fetchone()[0] == 1
    assert con.execute("SELECT layer FROM memory_fts").fetchall() == [("patterns",)]


def test_no_store_yet_is_empty(tmp_path):
    assert _run("list", tmp_path / "missing.sqlite") == {"items": [], "removed": 0}


@pytest.mark.parametrize("args", [
    {"os_user": "root", "action": "list"},
    {"os_user": "agent-x;rm", "action": "list"},
    {"os_user": "agent-x", "action": "drop"},
    {"os_user": "agent-x", "action": "forget", "id": "a b"},
])
def test_refuses_bad_arguments(args, monkeypatch):
    monkeypatch.setattr(operations.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ValueError):
        _agent_memory(args)
