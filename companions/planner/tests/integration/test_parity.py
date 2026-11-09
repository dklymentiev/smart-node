"""Interface-parity acceptance test (R1 exit criterion).

Creates the same set of objects via REST, MCP, and CLI, then reads each one
back through every other interface. All three readers must see identical
data. The CLI invokes its commands in-process via Click's CliRunner so we
can run uvicorn-free.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

import httpx
import pytest_asyncio
import uvicorn
from click.testing import CliRunner

from planner.app import create_app
from planner.auth import ENV_AUTH_MODE, ApiKeysRepo
from planner.cli import main as cli_main
from planner.mcp_tools import dispatch as mcp_dispatch


# ---------------------------------------------------------------------------
# Fixtures: live uvicorn so the CLI can hit it over real HTTP
# ---------------------------------------------------------------------------


class _ServerThread(threading.Thread):
    """Run uvicorn in a daemon thread so CLI HTTP calls hit a real socket."""

    def __init__(self, app, port: int) -> None:
        super().__init__(daemon=True)
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)

    def run(self) -> None:
        asyncio.run(self._server.serve())

    def stop(self) -> None:
        self._server.should_exit = True


@pytest_asyncio.fixture
async def live(pool, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["PLANNER_TEST_DSN"])
    # DD-008: parity asserts actor=system end-to-end; pin apikey mode.
    monkeypatch.setenv(ENV_AUTH_MODE, "apikey")
    app = create_app()
    # Need lifespan so app.state.pool is available; uvicorn handles this.
    port = 18088
    server = _ServerThread(app, port=port)
    server.start()
    # Wait for server to come up.
    deadline = time.time() + 5.0
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, timeout=2.0) as probe:
        while time.time() < deadline:
            try:
                r = await probe.get("/health")
                if r.status_code == 200:
                    break
            except Exception:
                await asyncio.sleep(0.05)
        else:
            server.stop()
            raise RuntimeError("uvicorn did not come up in time")

    # Bootstrap an API key for the test.
    key_repo = ApiKeysRepo(pool)
    _, plaintext = await key_repo.create(label="parity-test", actor_id="system")

    monkeypatch.setenv("PLANNER_API_URL", base)
    monkeypatch.setenv("PLANNER_API_KEY", plaintext)

    try:
        yield {
            "base": base,
            "api_key": plaintext,
            "pool": pool,
            "client": httpx.AsyncClient(
                base_url=base,
                headers={"X-API-Key": plaintext},
                timeout=5.0,
            ),
        }
    finally:
        server.stop()


def _run_cli(*args: str) -> dict[str, Any] | str:
    runner = CliRunner()
    result = runner.invoke(cli_main, list(args), catch_exceptions=False)
    assert result.exit_code == 0, f"CLI {args} failed:\n{result.output}"
    out = result.output.strip()
    if not out:
        return {}
    try:
        import json as _json
        return _json.loads(out)
    except Exception:
        return out


async def test_parity_across_three_interfaces(live) -> None:
    rest = live["client"]
    pool = live["pool"]

    # 1. CLI creates a project.
    proj = _run_cli("project", "create", "parity", "Parity Demo")
    assert isinstance(proj, dict) and proj["slug"] == "parity"
    pid = proj["id"]

    # 2. REST creates the actor.
    r = await rest.post(
        "/api/v1/actors",
        json={"id": "parity-bot", "display_name": "Parity Bot", "type": "agent"},
    )
    assert r.status_code == 201
    actor_rest = r.json()

    # 3. MCP creates an objective under the project.
    obj_mcp = await mcp_dispatch(pool, "objective_create", {
        "project_id": pid, "title": "M1", "type": "milestone",
    })

    # 4. Each interface reads each object back.
    cli_proj = _run_cli("project", "get", "parity")
    rest_proj = (await rest.get(f"/api/v1/projects/{pid}")).json()
    mcp_proj = await mcp_dispatch(pool, "project_get", {"id": pid})

    cli_actor = _run_cli("actor", "get", "parity-bot")
    rest_actor = (await rest.get("/api/v1/actors/parity-bot")).json()
    mcp_actor = await mcp_dispatch(pool, "actor_get", {"id": "parity-bot"})

    cli_obj = _run_cli("objective", "tree", "--project-id", str(pid))
    rest_tree = (await rest.get(f"/api/v1/objectives/projects/{pid}/tree")).json()
    mcp_tree = await mcp_dispatch(pool, "objective_tree", {"project_id": pid})

    # 5. Project: every interface sees the same row.
    for snap in (cli_proj, rest_proj, mcp_proj):
        assert snap["id"] == pid
        assert snap["slug"] == "parity"
        assert snap["title"] == "Parity Demo"

    # 6. Actor: identical across interfaces.
    for snap in (cli_actor, rest_actor, mcp_actor):
        assert snap["id"] == "parity-bot"
        assert snap["display_name"] == "Parity Bot"
        assert snap["type"] == "agent"

    # 7. Objective tree: same single milestone in every reading.
    for tree in (cli_obj, rest_tree, mcp_tree):
        assert isinstance(tree, list) and len(tree) == 1
        assert tree[0]["objective"]["id"] == obj_mcp["id"]
        assert tree[0]["objective"]["type"] == "milestone"

    # 8. Create a task via REST, comment via CLI, tag via MCP -- read all three back.
    t = await rest.post(
        "/api/v1/tasks",
        json={
            "title": "parity task", "project_id": pid,
            "objective_id": obj_mcp["id"], "assignee": "parity-bot",
        },
    )
    assert t.status_code == 201
    tid = t.json()["id"]

    _run_cli("comment", "add", str(tid), "via cli")
    add_tag_mcp = await mcp_dispatch(pool, "tag_add", {"task_id": tid, "tag": "via-mcp"})
    assert add_tag_mcp["added"] is True

    cli_comments = _run_cli("comment", "list", str(tid))
    rest_comments = (await rest.get(f"/api/v1/tasks/{tid}/comments")).json()
    mcp_comments = await mcp_dispatch(pool, "comments_list", {"task_id": tid})
    assert all(len(x) == 1 and x[0]["content"] == "via cli" for x in (
        cli_comments, rest_comments, mcp_comments
    ))

    cli_tags = _run_cli("tag", "list", str(tid))
    rest_tags = (await rest.get(f"/api/v1/tasks/{tid}/tags")).json()
    mcp_tags = await mcp_dispatch(pool, "tags_list", {"task_id": tid})
    assert cli_tags == rest_tags == mcp_tags == ["via-mcp"]

    await rest.aclose()
