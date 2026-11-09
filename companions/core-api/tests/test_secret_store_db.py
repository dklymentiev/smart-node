"""The secret store against a real database built from the tree's bootstrap
files (test_secret_store.py fakes the cursor, so it cannot see a constraint).

1. A new secret saved with no note (the panel sends none) is stored, with the
   note empty: note is NOT NULL, and the store used to pass NULL, so every
   secret created from the panel failed with a 500.
2. Replacing a value with no note keeps the note it had; a note given
   replaces it. The value reads back decrypted.
"""

from __future__ import annotations

import pytest

from disposable_pg import disposable_database
from lib import secret_store


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    key = tmp_path_factory.mktemp("key") / "node-secret.key"
    key.write_text("k" * 64 + "\n")
    mp = pytest.MonkeyPatch()
    mp.setattr(secret_store, "KEY_FILE", str(key))
    with disposable_database() as sql:
        # The install creates pgcrypto (B01); the bootstrap SQL assumes it.
        sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        from db import cursor
        yield cursor, sql
    mp.undo()


def test_a_new_secret_without_a_note_is_stored(store):
    cursor, sql = store
    with cursor(commit=True) as cur:
        assert secret_store.set_value(cur, "uitest/no-note", "v1", "user:owner") is False
    assert sql("SELECT note = '' FROM sn_config.secrets WHERE ref = 'uitest/no-note'") == "t"
    with cursor() as cur:
        assert secret_store.get_value(cur, "uitest/no-note") == "v1"


def test_a_replace_keeps_the_note_unless_one_is_given(store):
    cursor, sql = store
    with cursor(commit=True) as cur:
        secret_store.set_value(cur, "uitest/noted", "v1", "user:owner", note="the bot token")
        assert secret_store.set_value(cur, "uitest/noted", "v2", "user:owner") is True
    assert sql("SELECT note FROM sn_config.secrets WHERE ref = 'uitest/noted'") == "the bot token"
    with cursor(commit=True) as cur:
        secret_store.set_value(cur, "uitest/noted", "v3", "user:owner", note="rotated")
    assert sql("SELECT note FROM sn_config.secrets WHERE ref = 'uitest/noted'") == "rotated"
    with cursor() as cur:
        assert secret_store.get_value(cur, "uitest/noted") == "v3"
