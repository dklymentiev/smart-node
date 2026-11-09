"""Narrow Planner and wake port for the kernel-identified HITL delivery worker."""

from __future__ import annotations

import json
import pwd
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from auth import peer_credentials
from db import cursor
from permissions import requires
from routes import planner


router = APIRouter(prefix="/api/hitl", tags=["hitl"])
DELIVERY_OS_USER = "smart-node-hitl-delivery"
ACTOR = "component:hitl-bridge"
TASK = re.compile(r"^api/v1/tasks/([1-9][0-9]*)(?:/(comments|handoff))?$")
PERSON = re.compile(r"^api/v1/actors/[A-Za-z0-9@._:-]{1,128}$")
AGENT = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REQUEST_ID = re.compile(r"^[0-9a-f-]{36}$")


class PlannerCall(BaseModel):
    method: Literal["GET", "POST"]
    path: str
    body: dict | None = None


class WakeCall(BaseModel):
    agent: str
    task_id: int
    request_id: str


def _delivery_peer(request: Request) -> None:
    creds = peer_credentials(request)
    if creds is None:
        raise HTTPException(403, "HITL delivery requires the core-api unix socket")
    try:
        name = pwd.getpwuid(creds[1]).pw_name
    except KeyError:
        raise HTTPException(403, "unknown HITL delivery peer") from None
    if name != DELIVERY_OS_USER:
        raise HTTPException(403, "HITL delivery peer mismatch")


def _valid_call(call: PlannerCall) -> bool:
    match = TASK.fullmatch(call.path)
    if not match:
        return call.method == "GET" and call.body is None and bool(PERSON.fullmatch(call.path))
    suffix = match.group(2)
    if call.method == "GET":
        return suffix in (None, "comments") and call.body is None
    if not isinstance(call.body, dict):
        return False
    if call.method == "POST" and suffix == "handoff":
        return (set(call.body) == {"from_actor", "to_actor", "notify"}
                and call.body["notify"] is False
                and all(isinstance(value, str)
                        and bool(re.fullmatch(r"[A-Za-z0-9@._:-]{1,128}", value))
                        for value in (call.body["from_actor"], call.body["to_actor"])))
    if suffix == "comments":
        body = call.body
        if set(body) != {"content", "comment_type"}:
            return False
        if body["comment_type"] == "workflow:request":
            if not isinstance(body["content"], str):
                return False
            prefix = re.fullmatch(
                r"HITL request ([0-9a-f-]{36}) for task #([1-9][0-9]*)",
                body["content"].split("\n", 1)[0],
            )
            return bool(prefix and REQUEST_ID.fullmatch(prefix.group(1))
                        and prefix.group(2) == match.group(1))
        if body["comment_type"] != "workflow:answer":
            return False
        try:
            content = json.loads(body["content"])
        except (TypeError, ValueError):
            return False
        return (isinstance(content, dict) and content.get("type") == "workflow:answer"
                and content.get("source") == "hitl"
                and isinstance(content.get("request_id"), str)
                and bool(REQUEST_ID.fullmatch(content["request_id"]))
                and content.get("reference") == f"task:{match.group(1)}")
    return False


def _forward(method: str, path: str, body: dict | None = None):
    raw = json.dumps(body).encode() if body is not None else b""
    status, out, content_type = planner._handle(
        method, path, "", raw, ACTOR, "application/json", strength="proved"
    )
    return status, out, content_type


def _raise_wake(agent: str, task_id: int):
    from routes import agents  # lazy: the HITL port does not need agent_fs at import
    return agents.agent_wake(
        agent, agents.WakeRequest(source="hitl-bridge", reason=f"signed decision for task {task_id}")
    )


def _recipient_eligible(actor_id: str) -> bool:
    """Use configured identities, not Planner's historical actor type."""
    with cursor() as cur:
        cur.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM sn_config.users u "
            "WHERE u.active AND u.archived_at IS NULL "
            "AND u.role IS DISTINCT FROM 'agent' "
            "AND u.actor_id LIKE 'user:%%' "
            "AND (u.email = %s OR u.actor_id = %s) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM sn_config.agents a "
            "WHERE a.slug = %s OR a.actor_id = %s "
            "OR a.slug = u.email OR a.actor_id = u.actor_id"
            ")"
            ") AS eligible",
            (actor_id, actor_id, actor_id, actor_id),
        )
        return bool(cur.fetchone()["eligible"])


@router.get("/recipient/{actor_id}", dependencies=[Depends(requires("hitl.delivery"))])
def recipient(actor_id: str, request: Request):
    _delivery_peer(request)
    if not PERSON.fullmatch("api/v1/actors/" + actor_id):
        raise HTTPException(400, "invalid HITL recipient actor")
    return {"eligible": _recipient_eligible(actor_id)}


@router.post("/planner", dependencies=[Depends(requires("hitl.delivery"))])
async def planner_call(call: PlannerCall, request: Request):
    _delivery_peer(request)
    if not _valid_call(call):
        raise HTTPException(403, "operation outside HITL delivery port")
    status, out, content_type = await run_in_threadpool(_forward, call.method, call.path, call.body)
    return Response(content=out, status_code=status, media_type=content_type)


@router.post("/wake", dependencies=[Depends(requires("hitl.delivery"))])
async def wake(call: WakeCall, request: Request):
    _delivery_peer(request)
    if (not AGENT.fullmatch(call.agent) or call.task_id < 1
            or not REQUEST_ID.fullmatch(call.request_id)):
        raise HTTPException(400, "invalid HITL wake reference")
    task_path = f"api/v1/tasks/{call.task_id}"
    status, raw, _ = await run_in_threadpool(_forward, "GET", task_path)
    if status != 200:
        raise HTTPException(status, "HITL task unavailable")
    task = json.loads(raw)
    if task.get("assignee") != call.agent:
        raise HTTPException(409, "HITL task is not ready for this agent")
    status, raw, _ = await run_in_threadpool(_forward, "GET", task_path + "/comments")
    if status != 200:
        raise HTTPException(status, "HITL answer unavailable")
    for comment in json.loads(raw):
        if comment.get("comment_type") != "workflow:answer" or comment.get("author") != ACTOR:
            continue
        try:
            answer = json.loads(comment["content"])
        except (KeyError, TypeError, ValueError):
            continue
        if (answer.get("source") == "hitl" and answer.get("request_id") == call.request_id
                and answer.get("reference") == f"task:{call.task_id}"
                and answer.get("asking_actor_id") == call.agent):
            return await run_in_threadpool(_raise_wake, call.agent, call.task_id)
    raise HTTPException(409, "no component-authored HITL answer for this task")
