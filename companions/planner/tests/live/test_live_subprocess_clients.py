"""Phase 4.5: live MCP + CLI subprocess clients hit the running server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def test_cli_lists_projects_via_subprocess(live_server) -> None:
    """`python -m planner.cli` against the live server returns JSON we can parse."""
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR),
        "PLANNER_API_URL": live_server["base_url"],
        "PLANNER_API_KEY": live_server["api_key"],
    }
    res = subprocess.run(
        [sys.executable, "-m", "planner.cli", "project", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    # CLI prints a list (possibly empty); make sure it didn't crash.
    assert "Traceback" not in res.stderr


def test_cli_create_then_list_project(live_server) -> None:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR),
        "PLANNER_API_URL": live_server["base_url"],
        "PLANNER_API_KEY": live_server["api_key"],
    }
    create = subprocess.run(
        [
            sys.executable,
            "-m",
            "planner.cli",
            "project",
            "create",
            "cli",
            "CLI",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert create.returncode == 0, create.stderr

    listed = subprocess.run(
        [sys.executable, "-m", "planner.cli", "project", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "cli" in listed.stdout or "CLI" in listed.stdout, listed.stdout


def test_mcp_subprocess_lists_tools(live_server) -> None:
    """Spawn `python -m planner.mcp_server`, send a list_tools request over stdio.

    Uses the MCP JSON-RPC protocol directly: initialize -> initialized notification
    -> tools/list. This is a smoke test that the MCP server starts cleanly and
    advertises its tools; full tool exercising is covered by the in-process
    integration suite.
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR),
        "DATABASE_URL": os.environ["PLANNER_TEST_DSN"],
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "planner.mcp_server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        # JSON-RPC 2.0 initialize
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "live-test", "version": "0.1"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line.strip(), f"no init response. stderr={_drain(proc.stderr)}"
        init_resp = json.loads(line)
        assert init_resp["id"] == 1
        assert "result" in init_resp

        # Initialized notification
        proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            + "\n"
        )
        proc.stdin.flush()

        # tools/list
        proc.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            + "\n"
        )
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line.strip(), f"no tools/list response. stderr={_drain(proc.stderr)}"
        resp = json.loads(line)
        assert resp["id"] == 2
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        # Sanity: at least one CRUD tool per resource (verb_subject convention).
        for n in ("project_create", "project_list", "task_create"):
            assert n in names, f"missing tool {n}; got {sorted(names)}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _drain(stream) -> str:
    try:
        return stream.read() or ""
    except Exception:
        return ""
