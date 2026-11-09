"""a budget period is marked only with an accepted owner-email row."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from auth import Principal
from routes import budgets


def _owner():
    return Principal(username="admin", user_id=1, tenant_id=1,
                     role="super-admin")


def _budget(notified_at=None):
    return {
        "id": 9,
        "scope_type": "tenant",
        "scope_id": "1",
        "period": "daily",
        "limit_usd": Decimal("20.00"),
        "current_spend_usd": Decimal("20.25"),
        "period_reset_at": datetime(2026, 9, 21, tzinfo=timezone.utc),
        "hard_limit": True,
        "enabled": True,
        "notified_at": notified_at,
    }


_DEFAULT = object()


class FakeCursor:
    def __init__(self, *, budget=None, owner=_DEFAULT, fail_insert=False):
        self.budget = _budget() if budget is None else budget
        self.owner = ({"id": 1, "tenant_id": 1, "name": "Owner",
                       "address": "owner@example.test"}
                      if owner is _DEFAULT else owner)
        self.fail_insert = fail_insert
        self.calls = []
        self._next = None

    def execute(self, sql, args=None):
        compact = " ".join(sql.split())
        self.calls.append((compact, args))
        if "FROM sn_config.budgets" in compact and "FOR UPDATE" in compact:
            self._next = self.budget
        elif "FROM sn_config.users u" in compact:
            self._next = self.owner
        elif "INSERT INTO sn_core.messages" in compact:
            if self.fail_insert:
                raise RuntimeError("outbox unavailable")
            self._next = {"id": 77}
        elif "UPDATE sn_config.budgets" in compact:
            self._next = {"notified_at": datetime.now(timezone.utc)}
        else:
            self._next = None

    def fetchone(self):
        value, self._next = self._next, None
        return value


def _install_fake(monkeypatch, fake):
    @contextmanager
    def fake_cursor(commit=False):
        assert commit is True
        yield fake

    monkeypatch.setattr(budgets, "cursor", fake_cursor)
    monkeypatch.setattr(budgets, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(budgets, "_EMAIL_SENDER", "alerts@example.test")


def _call():
    return budgets.notify_budget_exhaustion(
        9, budgets.BudgetNotificationRequest(channel="email"), _owner())


def test_queues_before_marking_and_returns_message_identity(monkeypatch):
    fake = FakeCursor()
    _install_fake(monkeypatch, fake)

    result = _call()

    statements = [sql for sql, _ in fake.calls]
    lock = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql)
    insert = next(i for i, sql in enumerate(statements)
                  if "INSERT INTO sn_core.messages" in sql)
    mark = next(i for i, sql in enumerate(statements)
                if "UPDATE sn_config.budgets" in sql)
    assert lock < insert < mark
    assert result["status"] == "queued"
    assert result["message_id"] == 77
    assert result["external_id"].startswith("<")

    insert_args = fake.calls[insert][1]
    body = insert_args[4]
    assert "Scope: node (tenant 1)" in body
    assert "Limit: $20.00" in body
    assert "Spend: $20.25" in body
    assert "Resets: 2026-09-21" in body
    assert "Raise the limit" in body


def test_missing_sender_never_opens_transaction_or_marks(monkeypatch):
    fake = FakeCursor()
    _install_fake(monkeypatch, fake)
    monkeypatch.setattr(budgets, "_EMAIL_SENDER", None)

    with pytest.raises(HTTPException) as exc:
        _call()

    assert exc.value.status_code == 503
    assert fake.calls == []


def test_missing_owner_channel_does_not_mark(monkeypatch):
    fake = FakeCursor(owner=None)
    _install_fake(monkeypatch, fake)

    with pytest.raises(HTTPException) as exc:
        _call()

    assert exc.value.status_code == 503
    assert not any("UPDATE sn_config.budgets" in sql for sql, _ in fake.calls)


def test_failed_outbox_insert_does_not_mark(monkeypatch):
    fake = FakeCursor(fail_insert=True)
    _install_fake(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="outbox unavailable"):
        _call()

    assert not any("UPDATE sn_config.budgets" in sql for sql, _ in fake.calls)


def test_existing_marker_returns_without_second_message(monkeypatch):
    fake = FakeCursor(budget=_budget(datetime.now(timezone.utc)))
    _install_fake(monkeypatch, fake)

    result = _call()

    assert result == {"status": "already_notified", "budget_id": 9}
    assert not any("INSERT INTO sn_core.messages" in sql for sql, _ in fake.calls)
