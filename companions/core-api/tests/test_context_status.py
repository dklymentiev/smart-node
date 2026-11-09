"""Contract tests for the canonical context-status read model."""

from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from auth import Principal
from routes import context_status


def row(**changes):
    base = {
        "session_id": "s-new",
        "agent": "alex",
        "conversation": "alex:desk:d-1",
        "desk_id": "d-1",
        "is_opaque": True,
        "ledger_type": "array",
        "latest_request": {
            "input_tokens": 1000,
            "cache_read_tokens": 62000,
            "cache_write_tokens": 2000,
            "output_tokens": 999999,
        },
        "threshold_scope": "agent",
        "threshold_tokens": 120000,
        "policy_enabled": True,
        "last_activity_at": "now",
        "reset_event_id": None,
        "reset_at": None,
        "reset_payload": None,
    }
    base.update(changes)
    return base


def test_latest_request_formula_excludes_output_and_cumulative_usage():
    got = context_status._serialize(row())
    assert got["context_tokens"] == 65000
    assert got["percent"] == 54
    assert got["threshold_scope"] == "agent"


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"ledger_type": "array", "latest_request": None}, (None, None, True)),
        ({"threshold_tokens": 0}, (65000, None, False)),
        ({"threshold_tokens": None, "policy_enabled": None}, (65000, None, False)),
        ({"policy_enabled": False}, (65000, None, False)),
    ],
)
def test_empty_and_disabled_states_do_not_fabricate_percent(changes, expected):
    got = context_status._serialize(row(**changes))
    assert (got["context_tokens"], got["percent"], got["enabled"]) == expected


@pytest.mark.parametrize(
    "ledger_type, request_value",
    [("object", {}), ("array", "bad"), ("array", {"input_tokens": -1})],
)
def test_malformed_ledger_fails_loudly(ledger_type, request_value):
    with pytest.raises(context_status.ContextStatusReadError):
        context_status._context_tokens(ledger_type, request_value)


def test_read_failure_is_structured_503(monkeypatch):
    @contextmanager
    def broken_cursor():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(context_status, "cursor", broken_cursor)
    principal = Principal("owner", 1, 1, "super-admin")
    with pytest.raises(HTTPException) as caught:
        context_status.context_status(scope="active", desk_id=None, principal=principal)
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "context_status_unavailable"


def test_active_scope_is_node_admin_only():
    principal = Principal("member", 2, 1, "member")
    with pytest.raises(HTTPException) as caught:
        context_status.context_status(scope="active", desk_id=None, principal=principal)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "context_status_forbidden"


def test_transparent_active_session_disables_handoff_gauge():
    got = context_status._serialize(row(is_opaque=False))
    assert got["context_tokens"] == 65000
    assert got["enabled"] is False
    assert got["percent"] is None


def test_active_query_excludes_every_terminal_state():
    assert context_status._ACTIVE_STATUSES == ("active", "streaming", "idle_released")
    for terminal in ("completed", "cancelled", "failed", "reaped"):
        assert terminal not in context_status._ACTIVE_STATUSES
    assert "DISTINCT ON" in context_status._ACTIVE_SQL
    assert "usage->'requests'->-1" in context_status._ACTIVE_SQL
