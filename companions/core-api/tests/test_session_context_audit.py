"""Threshold writes keep the complete config.changed audit contract."""

from contextlib import contextmanager

import pytest

from auth import Principal
from routes import session_context


class FakeCursor:
    def __init__(self):
        self.rows = [
            {"threshold_tokens": 100000, "enabled": True},
            {
                "id": 5,
                "scope_type": "global",
                "scope_id": None,
                "threshold_tokens": 120000,
                "enabled": False,
                "updated_at": "now",
            },
        ]

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self.rows.pop(0)


@pytest.mark.parametrize("scope_type, scope_id", [("global", None), ("agent", "7")])
def test_upsert_audits_old_new_actor_reason_for_every_scope(monkeypatch, scope_type, scope_id):
    cur = FakeCursor()
    cur.rows[1]["scope_type"] = scope_type
    cur.rows[1]["scope_id"] = scope_id

    @contextmanager
    def fake_cursor(commit=False):
        assert commit is True
        yield cur

    events = []
    monkeypatch.setattr(session_context, "cursor", fake_cursor)
    monkeypatch.setattr(session_context, "emit", lambda _cur, **event: events.append(event))
    principal = Principal("owner", 1, 1, "super-admin", strength="session")
    payload = session_context.ContextLimitUpdate(
        threshold_tokens=120000, enabled=False, reason="capacity policy review",
    )

    session_context._upsert(scope_type, scope_id, payload, principal)

    audit = events[0]
    assert audit["event_type"] == "config.changed"
    assert audit["payload"] == {
        "scope": {"type": scope_type, "id": scope_id},
        "old": {"threshold_tokens": 100000, "enabled": True},
        "new": {"threshold_tokens": 120000, "enabled": False},
        "actor": "user:owner",
        "reason": "capacity policy review",
    }
