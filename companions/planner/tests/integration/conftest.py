"""Integration-test fixtures.

Skipped unless ``PLANNER_TEST_DSN`` is set. Each test gets a clean schema:
the ``conftest`` truncates all planner tables before every test, leaving
the bootstrap ``system`` actor in place.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from planner.migrations import apply_pending


def pytest_collection_modifyitems(config, items):
    if os.environ.get("PLANNER_TEST_DSN"):
        return
    skip_marker = pytest.mark.skip(reason="set PLANNER_TEST_DSN to run integration tests")
    for item in items:
        item.add_marker(skip_marker)


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture(autouse=True)
def _default_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """DD-008: default integration tests to ``apikey`` mode.

    Most integration tests were written against v0.1.0 semantics
    (login screen, X-API-Key header, ``system`` actor). The DD-008
    default of ``dev`` would change those baselines. Tests that
    exercise a different mode override this via their own
    ``monkeypatch.setenv``.
    """
    monkeypatch.setenv("PLANNER_AUTH_MODE", "apikey")


@pytest_asyncio.fixture
async def pool() -> asyncpg.Pool:  # type: ignore[misc]
    dsn = os.environ["PLANNER_TEST_DSN"]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        # Apply migrations (idempotent).
        await apply_pending(pool, MIGRATIONS_DIR)
        # Truncate everything except the seed system actor.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                TRUNCATE
                    task_notifications, task_links, task_tags, task_log,
                    task_commits, task_relations, task_comments,
                    tasks, objectives, projects, api_keys
                RESTART IDENTITY CASCADE
                """
            )
            await conn.execute(
                "DELETE FROM actors WHERE id <> 'system'"
            )
            # Most of the older tests build tasks mid-flight (in progress, with no
            # doer) to test something else; the rule that work needs a doer
            # (migration 0013) is off for them and is switched on, with its own tests,
            # in test_work_needs_doer.py. In production it is always on.
            await conn.execute("ALTER TABLE tasks DISABLE TRIGGER trg_zz_require_doer")
        yield pool
    finally:
        await pool.close()
