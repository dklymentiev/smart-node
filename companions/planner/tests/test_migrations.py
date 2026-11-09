"""Unit-level tests for the migration discovery logic.

DB-backed tests live in tests/integration and require PLANNER_TEST_DSN.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from planner.migrations import MIGRATION_FILENAME_RE, MigrationError, discover


def _write(tmp_path: Path, name: str, body: str = "SELECT 1;") -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_filename_regex_accepts_canonical_names() -> None:
    assert MIGRATION_FILENAME_RE.match("0001_initial.sql")
    assert MIGRATION_FILENAME_RE.match("0042_add_index_on_tasks.sql")


def test_filename_regex_rejects_non_canonical() -> None:
    assert not MIGRATION_FILENAME_RE.match("1_initial.sql")
    assert not MIGRATION_FILENAME_RE.match("0001-initial.sql")
    assert not MIGRATION_FILENAME_RE.match("0001_Initial.sql")
    assert not MIGRATION_FILENAME_RE.match("initial.sql")


def test_discover_returns_sorted(tmp_path: Path) -> None:
    _write(tmp_path, "0002_second.sql")
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "0010_tenth.sql")

    out = discover(tmp_path)

    assert [m.version for m in out] == [1, 2, 10]
    assert [m.name for m in out] == ["0001_first", "0002_second", "0010_tenth"]


def test_discover_ignores_non_sql_files(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.sql")
    (tmp_path / "README.md").write_text("notes")
    (tmp_path / "0002_skip.txt").write_text("not a migration")

    out = discover(tmp_path)
    assert [m.version for m in out] == [1]


def test_discover_rejects_malformed_sql_files(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "001_bad.sql")

    with pytest.raises(MigrationError, match="does not match"):
        discover(tmp_path)


def test_discover_rejects_duplicate_version(tmp_path: Path) -> None:
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "0001_other.sql")

    with pytest.raises(MigrationError, match="duplicate migration version 1"):
        discover(tmp_path)


def test_discover_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="does not exist"):
        discover(tmp_path / "nope")


def test_migration_sql_reads_from_disk(tmp_path: Path) -> None:
    p = _write(tmp_path, "0001_first.sql", "CREATE TABLE foo();")
    [m] = discover(tmp_path)
    assert m.path == p
    assert m.sql == "CREATE TABLE foo();"
