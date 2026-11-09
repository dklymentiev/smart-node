"""The gateway reads provider keys from the one store and journals a load once
per value, not per cached use."""
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402

import secrets_store as store  # noqa: E402


@pytest.fixture
def fake_db(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("k" * 64)
    monkeypatch.setattr(store, "_KEY_FILE", str(key))
    state = {"values": {}, "statements": []}

    def query(sql, args=None, fetch=False, as_dict=False):
        s = " ".join(sql.split())
        state["statements"].append((s, args))
        if s.startswith("SELECT pgp_sym_decrypt"):
            assert "sn_config.secrets" in s and args[0] == "k" * 64
            ref = args[1]
            return [{"val": state["values"][ref]}] if ref in state["values"] else []
        return None

    monkeypatch.setattr(store.db, "query", query)
    store._KEY_CACHE.clear()
    store._SEEN.clear()
    return state


def test_resolve_reads_the_one_store_and_journals_the_first_load(fake_db):
    fake_db["values"]["gateway/gemini-key"] = memoryview(b"gk")
    assert store.resolve_value("secret:gateway/gemini-key") == "gk"
    assert store.resolve_value("plain") == "plain"
    reads = [a for s, a in fake_db["statements"] if s.startswith("INSERT INTO sn_core.events")]
    assert len(reads) == 1 and reads[0][0] == "gateway/gemini-key"
    # the same value again: no second journal line
    store.resolve_value("secret:gateway/gemini-key")
    assert len([a for s, a in fake_db["statements"] if s.startswith("INSERT INTO sn_core.events")]) == 1
    # a rotation is journalled again
    fake_db["values"]["gateway/gemini-key"] = "gk2"
    assert store.resolve_value("secret:gateway/gemini-key") == "gk2"
    assert len([a for s, a in fake_db["statements"] if s.startswith("INSERT INTO sn_core.events")]) == 2


def test_a_missing_ref_fails_loud_and_a_provider_key_is_cached(fake_db):
    with pytest.raises(RuntimeError):
        store.resolve_value("secret:gateway/absent-key")
    fake_db["values"]["gateway/openrouter-key"] = "ok"
    assert store.get_provider_key("openrouter") == "ok"
    before = len(fake_db["statements"])
    assert store.get_provider_key("openrouter") == "ok"
    assert len(fake_db["statements"]) == before            # served from the cache
    assert store.get_provider_key("nothere") is None
