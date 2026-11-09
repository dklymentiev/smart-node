"""The daily digest is the owner's choice: off until switched on, offered only
where mail is set up.

Run inside the backend container against the migrated DB.
"""

import importlib.util
import os
import sys

import pytest


@pytest.fixture
def db():
    from db import init_pool, cursor
    init_pool()
    with cursor() as cur:
        cur.execute("SELECT daily_digest FROM sn_config.node_control WHERE id = true")
        was = cur.fetchone()["daily_digest"]
    yield cursor
    with cursor(commit=True) as cur:
        cur.execute("UPDATE sn_config.node_control SET daily_digest = %s WHERE id = true", (was,))


def _digest():
    ops = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "node-ops")
    sys.path.insert(0, ops)   # as on the node: it imports its sibling _events_classify
    spec = importlib.util.spec_from_file_location("event_digest", os.path.join(ops, "event-digest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_about_says_whether_mail_is_there_and_the_digest_is_on(db, monkeypatch):
    from routes.node_settings import about
    monkeypatch.delenv("SN_NOTIFY_SENDER", raising=False)
    a = about()
    assert a["mail"] is False and a["daily_digest"] in (True, False)
    monkeypatch.setenv("SN_NOTIFY_SENDER", "node@example.com")
    assert about()["mail"] is True


def test_the_switch_is_saved(db):
    from routes.node_settings import DigestInput, about, set_digest
    set_digest(DigestInput(on=True))
    assert about()["daily_digest"] is True
    set_digest(DigestInput(on=False))
    assert about()["daily_digest"] is False


def test_off_means_the_digest_does_nothing(monkeypatch):
    d = _digest()
    calls = []
    monkeypatch.setattr(d, "_psql", lambda sql, label="query": calls.append(label) or "f")
    assert d.main() == 0
    assert calls == ["switch"]
