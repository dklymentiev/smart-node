"""The one secret store: what the library writes and reads, over a fake
cursor that answers the exact statements it issues. No database; the key is a
file in a temporary directory."""
from __future__ import annotations

import pytest

from lib import secret_store


class Cursor:
    """Remembers statements; answers fetchone/fetchall/rowcount from a script."""

    def __init__(self, answers=None):
        self.executed = []
        self.answers = list(answers or [])
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        if self.answers and self.answers[0] == "ROWCOUNT1":
            self.answers.pop(0)
            self.rowcount = 1
        else:
            self.rowcount = 0

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []


@pytest.fixture(autouse=True)
def key_file(monkeypatch, tmp_path):
    path = tmp_path / "node-secret.key"
    path.write_text("k" * 64 + "\n")
    monkeypatch.setattr(secret_store, "KEY_FILE", str(path))
    return path


def test_both_ref_spellings_are_valid_and_nothing_else():
    assert secret_store.valid_ref("replicate.api_key")
    assert secret_store.valid_ref("gateway/gemini-key")
    assert secret_store.valid_ref("gateway/claude-cli-token")
    assert secret_store.valid_ref("dana.claude_web.password")
    assert not secret_store.valid_ref("bad ref!")
    assert not secret_store.valid_ref("")
    assert not secret_store.valid_ref(".leading")
    assert not secret_store.valid_ref("a." + ".".join(["b"] * 6))
    assert secret_store.namespace_of("gateway/gemini-key") == "gateway"
    assert secret_store.namespace_of("replicate.api_key") == "replicate"


def test_set_encrypts_in_sql_and_keeps_the_previous_value_as_a_version():
    cur = Cursor(["ROWCOUNT1"])              # the version insert copied one row: a replace
    replaced = secret_store.set_value(cur, "replicate.api_key", "s3cret", "user:alex", note="n")
    assert replaced is True
    version_sql, version_params = cur.executed[0]
    assert version_sql.startswith("INSERT INTO sn_config.secret_versions")
    assert version_params == ("user:alex", "replicate.api_key")
    upsert_sql, upsert_params = cur.executed[1]
    assert "pgp_sym_encrypt(%s, %s)" in upsert_sql and "ON CONFLICT (ref) DO UPDATE" in upsert_sql
    assert upsert_params[:4] == ("replicate.api_key", "s3cret", "k" * 64, "replicate")
    assert "s3cret" not in upsert_sql          # the value is a bind parameter, never statement text


def test_set_of_a_new_ref_is_not_a_replace():
    cur = Cursor()
    assert secret_store.set_value(cur, "gateway/gemini-key", "v", "user:alex", provider="gemini") is False


def test_set_refuses_a_bad_ref_before_touching_the_store():
    with pytest.raises(ValueError):
        secret_store.set_value(Cursor(), "bad ref!", "v", "x")


def test_get_decrypts_with_the_key_and_decodes_bytes():
    cur = Cursor([{"val": memoryview(b"plain")}])
    assert secret_store.get_value(cur, "replicate.api_key") == "plain"
    sql, params = cur.executed[0]
    assert "pgp_sym_decrypt(value_enc, %s)" in sql and params == ("k" * 64, "replicate.api_key")
    assert secret_store.get_value(Cursor([None]), "replicate.api_key") is None


def test_get_namespace_returns_the_leaves_under_a_prefix():
    cur = Cursor([[{"ref": "google.client_id", "val": "cid"}, {"ref": "google.refresh_token", "val": b"rt"}]])
    assert secret_store.get_namespace(cur, "google") == {"client_id": "cid", "refresh_token": "rt"}
    sql, params = cur.executed[0]
    assert params == ("k" * 64, "google.%")


def test_delete_keeps_the_last_value_as_a_version():
    cur = Cursor(["ROWCOUNT1", "ROWCOUNT1"])
    assert secret_store.delete_value(cur, "replicate.api_key", "os:root") is True
    assert cur.executed[0][0].startswith("INSERT INTO sn_config.secret_versions")
    assert "'deleted'" in cur.executed[0][0]
    assert cur.executed[1][0].startswith("DELETE FROM sn_config.secrets")


def test_used_by_names_cli_env_vars_and_api_providers():
    cur = Cursor([
        [{"slug": "claude-cli", "env_vars": {"CLAUDE_CODE_OAUTH_TOKEN": "secret:gateway/claude-cli-token",
                                             "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}}],
        [{"slug": "gemini"}, {"slug": "openrouter"}],
    ])
    out = secret_store.used_by(cur, ["gateway/claude-cli-token", "gateway/gemini-key", "replicate.api_key"])
    assert out["gateway/claude-cli-token"] == ["cli provider claude-cli (CLAUDE_CODE_OAUTH_TOKEN)"]
    assert out["gateway/gemini-key"] == ["provider gemini"]
    assert out["replicate.api_key"] == []


def test_readers_are_agents_with_the_grant_and_the_ref_or_its_namespace():
    grants = {"agent-d": ("r--", ["agent-d.portal.*", "nyws.vip.*"]),
              "agent-l": ("r--", ["google.gemini_api_key"]),
              "agent-m": ("rw-", []),
              "agent-t": ("", ["google.gemini_api_key"])}       # ref listed but no read grant
    out = secret_store.readers(grants, ["google.gemini_api_key", "agent-d.portal.password", "gateway/gemini-key"])
    assert out == {"google.gemini_api_key": ["agent-l"], "agent-d.portal.password": ["agent-d"],
                   "gateway/gemini-key": []}


def test_a_missing_or_empty_key_file_is_a_named_unavailability(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_store, "KEY_FILE", str(tmp_path / "absent.key"))
    with pytest.raises(secret_store.StoreUnavailable):
        secret_store.get_value(Cursor(), "replicate.api_key")
    empty = tmp_path / "empty.key"
    empty.write_text("\n")
    monkeypatch.setattr(secret_store, "KEY_FILE", str(empty))
    with pytest.raises(secret_store.StoreUnavailable):
        secret_store.set_value(Cursor(), "replicate.api_key", "v", "x")
