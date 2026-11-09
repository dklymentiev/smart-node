#!/usr/bin/env python3
"""Durable HITL -> Planner delivery, run as a node service after verification.

Every operation is retried. While a signed question waits, the human owns its
Planner task. After the verified answer is recorded, the task goes back to the
asking agent. The request id makes answer delivery idempotent.
"""
import http.client
import json
import logging
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import quote

QUEUE = Path(os.environ.get("HITL_QUEUE", "/var/lib/smart-node/hitl-queue"))
SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
MARKERS = QUEUE / "delivered"
TASK_REF = re.compile(r"^task:([1-9][0-9]*)$")
log = logging.getLogger("hitl-delivery")


class UnixConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def api(method: str, path: str, body: dict | None = None):
    if path.startswith("/api/planner/"):
        body = {"method": method, "path": path.removeprefix("/api/planner/"),
                "body": body}
        method, path = "POST", "/api/hitl/planner"
    elif not ((path == "/api/hitl/wake" and method == "POST")
              or (method == "GET" and path.startswith("/api/hitl/recipient/"))):
        raise ValueError("operation outside HITL delivery port")
    conn = UnixConnection("localhost", timeout=15)
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = response.read()
        parsed = json.loads(payload) if payload else {}
        if response.status >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {response.status} {str(parsed)[:300]}")
        return parsed
    finally:
        conn.close()


def stopped_for_another_reason(task: dict) -> bool:
    """A task queued for a reason of its own (a person, a dependency, a date,
    an error, the owner): asking a human on it would cross that stop. Queued
    for approval or review, or not queued at all, is not such a stop. A
    planner older than migration 0010 says it as the one word `blocked`."""
    state = task.get("state") or task.get("status")
    if state == "blocked":
        return True
    return state == "queued" and task.get("wait_reason") not in (None, "approval", "review")


def task_path(task_id: int, suffix: str = "") -> str:
    return f"/api/planner/api/v1/tasks/{task_id}{suffix}"


def process(path: Path) -> None:
    with path.open() as f:
        item = json.load(f)
    rid = item["request_id"]
    match = TASK_REF.fullmatch(item.get("reference", ""))
    if not match:
        raise ValueError(f"{rid}: invalid task reference")
    task_id = int(match.group(1))
    agent = item["agent"]
    recipient = item["recipient"]
    recipient_actor_id = item["recipient_actor_id"]
    marker = MARKERS / f"{rid}.json"
    asked = MARKERS / f"{rid}.asked"
    commented = MARKERS / f"{rid}.commented"
    if marker.exists():
        return
    if item.get("result") is None and (QUEUE / "delivery" / f"{rid}.json").exists():
        return  # the settled copy owns this request now

    task = api("GET", task_path(task_id))
    person = api("GET", "/api/planner/api/v1/actors/" + quote(recipient_actor_id, safe=""))
    if not person.get("active"):
        raise ValueError(f"{rid}: recipient actor is inactive")
    identity = api("GET", "/api/hitl/recipient/" + quote(recipient_actor_id, safe=""))
    if identity.get("eligible") is not True:
        raise ValueError(f"{rid}: recipient actor is not a configured human")
    if task["assignee"] not in (recipient_actor_id, agent):
        raise ValueError(f"{rid}: task {task_id} belongs to another actor")
    result = item.get("result")
    if result is not None:
        if result.get("status") not in ("answered", "expired"):
            raise ValueError(f"{rid}: unverified result")
        if result["status"] == "answered":
            if result.get("request_id") != rid or result.get("caller_ref") != item["reference"]:
                raise ValueError(f"{rid}: verified answer does not match task reference")
    if not asked.exists():
        request_comment = (
            f"HITL request {rid} for task #{task_id}\n"
            f"Decision needed from {recipient_actor_id}: {item.get('title', 'Decision')}\n\n"
            f"{item.get('text', '')}\n\n"
            "The private signed-decision link is sent separately."
        )
        existing = api("GET", task_path(task_id, "/comments"))
        if not any(c.get("comment_type") == "workflow:request"
                   and c.get("content") == request_comment for c in existing):
            api("POST", task_path(task_id, "/comments"),
                {"content": request_comment, "comment_type": "workflow:request"})
        MARKERS.mkdir(parents=True, exist_ok=True)
        asked.touch(mode=0o600, exist_ok=True)
    if task["assignee"] == agent and not commented.exists():
        if stopped_for_another_reason(task):
            raise ValueError(f"{rid}: task is stopped for another reason ({task.get('wait_reason')})")
        api("POST", task_path(task_id, "/handoff"),
            {"from_actor": agent, "to_actor": recipient_actor_id, "notify": False})
        task = api("GET", task_path(task_id))

    if result is None:
        if task["assignee"] != recipient_actor_id:
            raise ValueError(f"{rid}: human handoff did not land")
        return
    if not commented.exists():
        content = json.dumps({
            "type": "workflow:answer", "source": "hitl",
            "request_id": rid, "reference": item["reference"],
            "asking_actor_id": agent, "recipient_actor_id": recipient_actor_id,
            "status": result["status"], "recipient": recipient,
            "answer": result.get("answer"), "signed_at": result.get("signed_at"),
            "assurance": result.get("assurance"),
            "proof_token": result.get("proof_token"),
        }, ensure_ascii=False, sort_keys=True)
        # The worker may crash after Planner commits the comment but before
        # writing its local marker. Read the durable task log on every retry.
        # Compare the whole canonical payload: another comment with the same
        # request id is not proof that this verified answer was recorded.
        existing = api("GET", task_path(task_id, "/comments"))
        if not any(c.get("comment_type") == "workflow:answer"
                   and c.get("content") == content for c in existing):
            api("POST", task_path(task_id, "/comments"),
                {"content": content, "comment_type": "workflow:answer"})
        MARKERS.mkdir(parents=True, exist_ok=True)
        commented.touch(mode=0o600, exist_ok=True)

    task = api("GET", task_path(task_id))
    if task["assignee"] == recipient_actor_id:
        api("POST", task_path(task_id, "/handoff"),
            {"from_actor": recipient_actor_id, "to_actor": agent, "notify": False})
    elif task["assignee"] != agent:
        raise ValueError(f"{rid}: task was reassigned while the answer was delivered")
    api("POST", "/api/hitl/wake", {
        "agent": agent, "task_id": task_id, "request_id": rid,
    })
    MARKERS.mkdir(parents=True, exist_ok=True)
    temp = MARKERS / f".{rid}.tmp"
    with temp.open("w") as f:
        json.dump({"request_id": rid, "task_id": task_id,
                   "status": result["status"]}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, marker)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    while True:
        for root in (QUEUE / "requests", QUEUE / "delivery"):
            for path in sorted(root.glob("*.json")):
                try:
                    process(path)
                except Exception as exc:
                    log.warning("delivery pending for %s: %s", path.name, exc)
        time.sleep(10)


if __name__ == "__main__":
    main()
