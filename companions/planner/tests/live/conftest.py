"""Live test harness: real Postgres + real uvicorn subprocess + real CLI/MCP.

Skipped unless ``PLANNER_TEST_DSN`` is set. Spawns uvicorn via subprocess so
the app exercises its full lifespan (DB pool open, root key bootstrap,
migrations applied), and waits for /health to report ok before yielding.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def pytest_collection_modifyitems(config, items):
    """Live tests are opt-in.

    Both PLANNER_TEST_DSN (for the database) and PLANNER_LIVE=1 must be set.
    The two-flag dance keeps live tests out of the default ``pytest tests/``
    invocation -- they reset the DB and spawn subprocesses, so running them
    alongside the regular integration suite would clash.
    """
    if os.environ.get("PLANNER_TEST_DSN") and os.environ.get("PLANNER_LIVE"):
        return
    skip = pytest.mark.skip(
        reason="set PLANNER_TEST_DSN and PLANNER_LIVE=1 to run live tests"
    )
    # Apply only to items in this directory.
    here = str(Path(__file__).parent)
    for item in items:
        if here in str(item.fspath):
            item.add_marker(skip)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, timeout: float = 30.0) -> dict:
    start = time.monotonic()
    last_err: Exception | None = None
    while time.monotonic() - start < timeout:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return r.json()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(0.3)
    raise RuntimeError(
        f"server did not become healthy at {url}: last_err={last_err}"
    )


@pytest.fixture(scope="module")
def live_server():
    """Spawn uvicorn against a fresh schema in PLANNER_TEST_DSN.

    Yields a dict with ``base_url``, ``api_key`` (root plaintext), and
    ``proc`` for tests to introspect.
    """
    dsn = os.environ["PLANNER_TEST_DSN"]
    # Reset state so the root key gets re-printed each module run.
    _reset_db_sync(dsn)
    # Apply migrations BEFORE spawning uvicorn -- the FastAPI lifespan does
    # NOT run migrations; the docker-compose entrypoint does that
    # (`python -m planner.install && exec uvicorn ...`). We replicate it.
    _apply_migrations_sync(dsn)

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "PYTHONPATH": str(SRC_DIR),
        "PLANNER_LOG_LEVEL": "warning",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "planner.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    base_url = f"http://127.0.0.1:{port}"
    api_key: str | None = None

    try:
        # Read startup log lines for the root key banner.
        deadline = time.monotonic() + 30.0
        buffer: list[str] = []
        while time.monotonic() < deadline and api_key is None:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"uvicorn died: {''.join(buffer)}"
                    )
                continue
            buffer.append(line)
            if line.startswith("  X-API-Key:"):
                api_key = line.split(":", 1)[1].strip()
        _wait_for_health(f"{base_url}/health")
        if api_key is None:
            raise RuntimeError(
                "did not see X-API-Key banner; saw: " + "".join(buffer)
            )
        yield {"base_url": base_url, "api_key": api_key, "proc": proc}
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()


def _reset_db_sync(dsn: str) -> None:
    import asyncpg

    async def _go():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                """
                DO $$
                DECLARE r record;
                BEGIN
                  FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
                    EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
                  END LOOP;
                END$$;
                """
            )
        finally:
            await conn.close()

    asyncio.run(_go())


def _apply_migrations_sync(dsn: str) -> None:
    """Apply migrations the same way the docker entrypoint does."""
    import asyncpg

    from planner.migrations import apply_pending

    migrations_dir = REPO_ROOT / "migrations"

    async def _go():
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
        try:
            await apply_pending(pool, migrations_dir)
        finally:
            await pool.close()

    asyncio.run(_go())


@pytest_asyncio.fixture
async def live_client(live_server) -> httpx.AsyncClient:
    headers = {"X-API-Key": live_server["api_key"]}
    async with httpx.AsyncClient(
        base_url=live_server["base_url"], headers=headers, timeout=10.0
    ) as c:
        yield c
