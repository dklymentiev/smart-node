"""The sops-era file is imported into the store once: leaves become rows,
bookkeeping and old snapshots do not, a ref already in the store keeps its
value, and the journal entry is what stops a second import."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from lib import secret_store, secret_store_import as imp

FILE = {
    "sops": {"age": "..."},
    "_snaps": {"gitlab_token": {"1700000000": "old"}},
    "replicate": {"api_key": "r-key"},
    "agent-d": {"portal": {"login": "u", "password": "p"}},
    "gitlab": {"token": "t"},
    "bad key!": "x",
    "empty": None,
}


def test_leaves_skip_bookkeeping_and_flatten_namespaces():
    assert sorted(imp.leaves(FILE)) == [
        ("agent-d.portal.login", "u"), ("agent-d.portal.password", "p"),
        ("bad key!", "x"), ("gitlab.token", "t"), ("replicate.api_key", "r-key"),
    ]


@pytest.fixture
def world(monkeypatch, tmp_path):
    path = tmp_path / "integrations.sops.yaml"
    path.write_text("encrypted")
    state = {"events": [], "stored": {}, "imported_before": False, "statements": []}

    class Cur:
        def execute(self, sql, params=None):
            state["statements"].append(" ".join(sql.split()))
            self._last = (" ".join(sql.split()), params)

        def fetchone(self):
            sql, params = self._last
            if "FROM sn_core.events" in sql:
                return {"x": 1} if state["imported_before"] else None
            if sql.startswith("SELECT 1 FROM sn_config.secrets"):
                return {"x": 1} if params[0] in state["stored"] else None
            return None

    @contextmanager
    def fake_cursor(commit=False):
        yield Cur()

    def fake_set(cur, ref, value, actor, provider=None, note=None):
        state["stored"][ref] = (value, actor, note)
        return False

    monkeypatch.setattr(imp, "cursor", fake_cursor)
    monkeypatch.setattr(imp, "emit", lambda cur, **ev: state["events"].append(ev))
    monkeypatch.setattr(secret_store, "set_value", fake_set)
    monkeypatch.setattr(secret_store, "exists", lambda cur, ref: ref in state["stored"])
    state["path"] = str(path)
    return state


def test_a_first_start_imports_the_file_and_journals_it(world):
    world["stored"]["gitlab.token"] = ("already-here", "user:alex", None)
    out = imp.import_file(world["path"], decrypt=lambda p: FILE)
    assert sorted(out["imported"]) == ["agent-d.portal.login", "agent-d.portal.password", "replicate.api_key"]
    assert out["kept"] == ["gitlab.token"] and out["invalid"] == ["bad key!"]
    assert world["stored"]["gitlab.token"][0] == "already-here"       # the store's value wins
    assert world["stored"]["replicate.api_key"] == ("r-key", "component:core-api", "imported from the sops file")
    ev = world["events"][0]
    assert ev["event_type"] == "secret.store.imported" and ev["entity_id"] == world["path"]
    assert ev["payload"]["imported"] == 3 and ev["payload"]["invalid"] == ["bad key!"]


def test_a_second_start_leaves_the_file_alone(world):
    world["imported_before"] = True
    assert imp.import_file(world["path"], decrypt=lambda p: FILE) is None
    assert world["stored"] == {} and world["events"] == []


def test_no_file_means_nothing_to_do(world, tmp_path):
    assert imp.import_file(str(tmp_path / "absent.yaml"), decrypt=lambda p: FILE) is None


def test_an_unreadable_file_is_logged_and_retried_next_start(world):
    def boom(path):
        raise RuntimeError("sops decrypt failed")

    assert imp.import_file(world["path"], decrypt=boom) is None
    assert world["events"] == []          # no marker: the next start tries again
