#!/usr/bin/env python3
"""Agent-side HITL MCP over the private Unix socket.

No agent slug is sent. The bridge identifies this process from SO_PEERCRED and
keeps the caller's signing key outside every agent workspace.
"""
import json
import os
import re
import socket
import http.client
import time
from pathlib import Path
from typing import Optional

try:
    from mcp.server import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

SOCKET = os.environ.get("HITL_AGENT_SOCKET", "/run/smart-node/hitl/agent.sock")
CORE_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
mcp = _Server("smart-node-hitl")


class _CoreConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(CORE_SOCKET)


def _planner(method: str, task_id: int, suffix: str = "", body: dict | None = None) -> tuple[int, dict]:
    conn = _CoreConnection("localhost", timeout=30)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, f"/api/planner/api/v1/tasks/{task_id}{suffix}", body=data,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else {}
    finally:
        conn.close()


def _stopped_for_another_reason(task: dict) -> bool:
    """A task queued for a reason of its own (a person, a dependency, a date,
    an error, the owner): a question on it would cross that stop. Queued for
    approval or review, or not queued at all, is not such a stop. A planner
    older than migration 0010 says it as the one word `blocked`."""
    state = task.get("state") or task.get("status")
    if state == "blocked":
        return True
    return state == "queued" and task.get("wait_reason") not in (None, "approval", "review")


def _call(payload: dict) -> str:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(100)
            conn.connect(SOCKET)
            conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            with conn.makefile("rb") as stream:
                line = stream.readline(2_000_001)
        if not line or len(line) > 2_000_000:
            raise OSError("response missing or too large")
        return line.decode()
    except (OSError, UnicodeError) as e:
        return json.dumps({"status": "hold", "reason": f"HITL bridge unavailable: {e}"})


@mcp.tool()
def hitl_recipients() -> str:
    """List people whose public decision keys are pinned on this node."""
    return _call({"op": "list_recipients"})


@mcp.tool()
def ask_human(recipient: str, title: str, text: str, task_id: int,
              choices: Optional[list[dict]] = None, ttl_minutes: int = 1440,
              assurance: str = "fresh", max_session_hours: int = 12) -> str:
    """Ask a person for a signed decision and return the private link.

    Deliver the link to that person through the approved channel. The verified
    answer is recorded on Planner task_id and the task resumes. This receipt
    does not bypass any separate session approval or outbound action gate.
    Use fresh for contracts, payments and irreversible actions; session can
    accept an unlocked device session for lower-risk choices.
    """
    try:
        if task_id <= 0:
            raise ValueError("task_id must be positive")
        status, task = _planner("GET", task_id)
        actor = os.environ.get("SN_AGENT_SLUG") or Path.cwd().name
        if status != 200 or task.get("assignee") != actor:
            return json.dumps({"status": "error", "reason": "task is not assigned to this agent"})
        if _stopped_for_another_reason(task):
            return json.dumps({"status": "error", "reason": "task is stopped for another reason "
                                                           f"({task.get('wait_reason')})"})
        response = json.loads(_call({"op": "ask_human", "recipient": recipient,
                                     "title": title, "text": text,
                                     "reference": f"task:{task_id}",
                                     "choices": choices, "ttl_minutes": ttl_minutes,
                                     "assurance": assurance,
                                     "max_session_hours": max_session_hours}))
        if response.get("status") != "created":
            return json.dumps(response)
        rid = response["request_id"]
        recipient_actor_id = response["recipient_actor_id"]
        # The dedicated delivery worker applies the handoff from its durable
        # queue. Do not release the private link until Planner says the human
        # owns the task. A lost worker response is safe to retry by request id.
        for _ in range(15):
            status, task = _planner("GET", task_id)
            if status == 200 and task.get("assignee") == recipient_actor_id:
                break
            if status != 200 or task.get("assignee") != actor:
                return json.dumps({"status": "hold", "request_id": rid,
                                   "reason": "task changed before HITL handoff; do not send the link"})
            time.sleep(1)
        else:
            return json.dumps({"status": "hold", "request_id": rid,
                               "reason": "HITL request is queued but human handoff is pending; do not send the link"})
        return json.dumps(response)
    except (OSError, ValueError, KeyError) as e:
        return json.dumps({"status": "hold", "reason": f"HITL ask failed: {e}"})


@mcp.tool()
def check_human(request_id: str) -> str:
    """Read a request you created, including its link after human handoff."""
    try:
        result = json.loads(_call({"op": "check_human", "request_id": request_id}))
        if result.get("status") == "pending" and result.get("link"):
            match = re.fullmatch(r"task:([1-9][0-9]*)", result.get("reference", ""))
            if not match:
                return json.dumps({"status": "hold", "reason": "invalid task reference"})
            status, task = _planner("GET", int(match.group(1)))
            if status != 200 or task.get("assignee") != result.get("recipient_actor_id"):
                return json.dumps({"status": "hold", "request_id": request_id,
                                   "reason": "human handoff is pending; do not send the link"})
        return json.dumps(result)
    except (OSError, ValueError, TypeError) as exc:
        return json.dumps({"status": "hold", "reason": f"HITL check failed: {exc}"})


if __name__ == "__main__":
    mcp.run()
