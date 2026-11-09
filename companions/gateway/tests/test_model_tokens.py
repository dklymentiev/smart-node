"""Tokens of agent processes: minted by the runtime, checked by the door, kept
as digests in the database so neither process has to remember them."""
import os

os.environ.setdefault("SMARTNODE_DB_DSN", "dbname=test-only")

import pytest  # noqa: E402

import model_tokens  # noqa: E402
from fake_db import install  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    return install(monkeypatch, model_tokens)


def test_the_token_is_never_stored_only_its_digest(store):
    token = model_tokens.issue("s1", "agent-b", "agent-b", "opus")
    assert token.startswith(model_tokens.PREFIX) and len(token) > 40
    assert store.rows[0]["digest"] == model_tokens.digest_of(token)
    assert token not in str(store.rows)
    assert model_tokens.lookup(token) == {"session_id": "s1", "agent": "agent-b",
                                         "os_user": "agent-b", "model": "opus", "provider": None}


def test_a_session_holds_one_live_token(store):
    first = model_tokens.issue("s1", "agent-b", "agent-b", "opus")
    second = model_tokens.issue("s1", "agent-b", "agent-b", "opus")   # respawn
    model_tokens.forget()
    assert model_tokens.lookup(first) is None
    assert model_tokens.lookup(second)["session_id"] == "s1"
    assert [r["revoke_reason"] for r in store.rows if r["revoked_at"]] == ["reissued"]


def test_revoke_session_and_revoke_all(store):
    a = model_tokens.issue("s1", "agent-a", "agent-a", "opus")
    b = model_tokens.issue("s2", "agent-b", "agent-b", "sonnet")
    model_tokens.revoke_session("s1")
    model_tokens.forget()
    assert model_tokens.lookup(a) is None and model_tokens.lookup(b) is not None
    assert model_tokens.revoke_all("runtime started") == 1
    model_tokens.forget()
    assert model_tokens.lookup(b) is None
    assert model_tokens.live_count() == 0


def test_a_foreign_bearer_costs_no_database_read(store):
    assert model_tokens.lookup("sk-ant-oat01-something") is None
    assert model_tokens.lookup("") is None
    assert store.statements == []


def test_the_cache_serves_a_turn_from_one_read(store):
    token = model_tokens.issue("s1", "agent-b", "agent-b", "opus")
    reads_before = len(store.statements)
    for _ in range(5):
        assert model_tokens.lookup(token)["agent"] == "agent-b"
    assert len(store.statements) == reads_before + 1


def test_revoke_session_never_raises(store):
    store.fail = True
    model_tokens.revoke_session("s1")      # logged, not raised: this runs on every reclaim path


def test_issue_fails_loudly_when_the_store_is_down(store):
    store.fail = True
    with pytest.raises(RuntimeError):
        model_tokens.issue("s1", "agent-b", "agent-b", "opus")


def test_the_token_names_the_provider_its_process_runs_under(store):
    own = model_tokens.issue("s1", "agent-b", "agent-b", "opus")
    other = model_tokens.issue("s2", "agent-b", "agent-b", "qwen2.5:0.5b", provider="local-cli")
    assert model_tokens.lookup(own)["provider"] is None            # claude-cli, the node's own
    assert model_tokens.lookup(other)["provider"] == "local-cli"
