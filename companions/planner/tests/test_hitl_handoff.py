"""HITL handoff retains ordinary Planner behavior and avoids early wakes."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from planner.repos import tasks as task_module
from planner import notify as notify_module


class Pool:
    async def fetchrow(self, *_args):
        return {"id": 7}


@pytest.mark.asyncio
async def test_handoff_notifies_by_default_but_hitl_can_defer_wake():
    task = SimpleNamespace(id=7, title="Approval", status="in_progress")
    repo = task_module.TasksRepo(Pool())
    with patch.object(task_module, "_row_to_task", return_value=task), patch.object(
        task_module, "fire_assignee_notify"
    ) as notify:
        assert await repo.handoff(7, from_actor="alice", to_actor="owner") is task
        notify.assert_called_once()
        notify.reset_mock()
        assert await repo.handoff(
            7, from_actor="owner", to_actor="alice", notify=False
        ) is task
        notify.assert_not_called()


def test_cancelled_external_wait_routes_are_absent():
    source = Path(__file__).resolve().parents[1] / "src/planner/routers/tasks.py"
    tree = ast.parse(source.read_text())
    routes = {
        decorator.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and isinstance(decorator.func.value, ast.Name)
        and decorator.func.value.id == "router"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
    }
    assert "/{task_id}/external-wait" not in routes
    assert "/{task_id}/external-resume" not in routes


@pytest.mark.asyncio
async def test_stale_scanner_only_queries_review_status():
    queries = []

    class ScannerPool:
        async def fetch(self, query, *_args):
            queries.append(query)
            return []

    calls = 0

    async def one_tick_then_cancel(_delay):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise __import__("asyncio").CancelledError

    with patch.object(notify_module.asyncio, "sleep", one_tick_then_cancel), patch.object(
        notify_module, "_post_notify"
    ) as post:
        await notify_module.run_stale_pending_review_scanner(ScannerPool())
    assert len(queries) == 1
    assert "stage = 'pending_review'" in queries[0]
    assert "in_progress" not in queries[0]
    post.assert_not_called()
