"""Unit-level tests for the installer.

End-to-end install (``python -m planner.install`` against a real DB) is
exercised by the integration suite.
"""

from __future__ import annotations

from planner.install import _migrations_dir, _redact


def test_redact_hides_password() -> None:
    assert _redact("postgresql://u:secret@host/db") == "postgresql://u:***@host/db"


def test_redact_passes_through_when_no_credentials() -> None:
    assert _redact("postgresql://host/db") == "postgresql://host/db"


def test_redact_passes_through_when_user_only() -> None:
    assert _redact("postgresql://u@host/db") == "postgresql://u@host/db"


def test_redact_handles_non_dsn_gracefully() -> None:
    assert _redact("not-a-dsn") == "not-a-dsn"


def test_migrations_dir_default_resolves_to_repo_root() -> None:
    out = _migrations_dir()
    assert out.name == "migrations"
    # Same parent as src/
    assert (out.parent / "src").is_dir()


def test_migrations_dir_honors_env(monkeypatch) -> None:
    monkeypatch.setenv("PLANNER_MIGRATIONS_DIR", "/custom/path")
    assert str(_migrations_dir()) == "/custom/path"
