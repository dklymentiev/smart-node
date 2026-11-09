"""Unit-level tests for the DB module that don't require a live PostgreSQL.

Pool / transaction behavior is exercised in tests/integration with a real DB.
"""

from __future__ import annotations

import pytest

from planner.db import ConfigError, _resolve_dsn


def test_resolve_dsn_prefers_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://env/db")
    assert _resolve_dsn("postgres://arg/db") == "postgres://arg/db"


def test_resolve_dsn_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://env/db")
    assert _resolve_dsn(None) == "postgres://env/db"


def test_resolve_dsn_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        _resolve_dsn(None)
