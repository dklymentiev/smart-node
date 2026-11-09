"""The HITL delivery account gets a narrow kernel-proved port, not root access."""

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from routes import hitl


def call(method, path, body=None):
    return hitl.PlannerCall(method=method, path=path, body=body)


def test_port_allows_only_delivery_operations():
    assert hitl._valid_call(call("GET", "api/v1/tasks/7"))
    assert hitl._valid_call(call("GET", "api/v1/tasks/7/comments"))
    assert hitl._valid_call(call("GET", "api/v1/actors/owner"))
    assert hitl._valid_call(call("POST", "api/v1/tasks/7/handoff", {
        "from_actor": "alice", "to_actor": "owner", "notify": False}))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7/handoff", {
        "to_actor": "owner"}))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7/handoff", {
        "from_actor": "owner", "to_actor": "alice", "notify": True}))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7/external-wait", {
        "handle": "wait:" + "a" * 36, "comment": "Waiting"
    }))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7", {"status": "blocked"}))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7", {}))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7/approve", {}))
    assert not hitl._valid_call(call("GET", "api/v1/tasks"))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/7/comments", {
        "comment_type": "approval", "content": "yes"
    }))


def test_request_comment_must_name_the_same_task():
    rid = "a" * 36
    content = f"HITL request {rid} for task #7\nDecision needed from owner: Approve"
    assert hitl._valid_call(call("POST", "api/v1/tasks/7/comments", {
        "comment_type": "workflow:request", "content": content}))
    assert not hitl._valid_call(call("POST", "api/v1/tasks/8/comments", {
        "comment_type": "workflow:request", "content": content}))


def test_only_delivery_os_account_is_admitted():
    request = SimpleNamespace()
    with patch.object(hitl, "peer_credentials", return_value=(1, 123, 456)), patch.object(
        hitl.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="smart-node-hitl-delivery")
    ):
        hitl._delivery_peer(request)
    with patch.object(hitl, "peer_credentials", return_value=(1, 123, 456)), patch.object(
        hitl.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="root")
    ):
        with pytest.raises(HTTPException) as exc:
            hitl._delivery_peer(request)
    assert exc.value.status_code == 403


def test_recipient_identity_uses_users_and_agent_registry():
    queries = []

    class FakeCursor:
        def execute(self, sql, params):
            queries.append((sql, params))

        def fetchone(self):
            return {"eligible": True}

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    with patch.object(hitl, "cursor", fake_cursor), patch.object(hitl, "_delivery_peer"):
        assert hitl.recipient("admin", SimpleNamespace()) == {"eligible": True}
    sql, params = queries[0]
    assert "FROM sn_config.users" in sql
    assert "u.active AND u.archived_at IS NULL" in sql
    assert "FROM sn_config.agents" in sql
    assert "a.slug = %s OR a.actor_id = %s" in sql
    assert "u.role IS DISTINCT FROM 'agent'" in sql
    assert params == ("admin",) * 4

    with patch.object(hitl, "_delivery_peer"):
        with pytest.raises(HTTPException) as exc:
            hitl.recipient("bad actor", SimpleNamespace())
    assert exc.value.status_code == 400


def test_wake_needs_component_answer_on_assigned_task():
    rid = "a" * 36
    answer = json.dumps({"type": "workflow:answer", "source": "hitl",
                         "request_id": rid, "reference": "task:7",
                         "asking_actor_id": "alice"})
    comments = [{"author": "alice", "comment_type": "workflow:answer", "content": answer}]
    calls = []

    def forward(_method, path, _body=None):
        if path.endswith("/comments"):
            return 200, json.dumps(comments).encode(), "application/json"
        return 200, json.dumps({"assignee": "alice", "status": "in_progress"}).encode(), "application/json"

    with patch.object(hitl, "_delivery_peer"), patch.object(hitl, "_forward", forward), patch.object(
        hitl, "_raise_wake", lambda slug, task_id: calls.append((slug, task_id)) or {"ok": True}
    ):
        with pytest.raises(HTTPException):
            asyncio.run(hitl.wake(hitl.WakeCall(agent="alice", task_id=7, request_id=rid), SimpleNamespace()))
        assert not calls
        comments[0]["author"] = "component:hitl-bridge"
        result = asyncio.run(hitl.wake(hitl.WakeCall(agent="alice", task_id=7, request_id=rid), SimpleNamespace()))
    assert result == {"ok": True}
    assert calls == [("alice", 7)]
