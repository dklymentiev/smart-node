"""
Shared helpers for task-simple logic scripts.

Two concerns:
  1. core_api()  -- HTTP over the node's Unix socket (planner, etc.)
  2. gateway_call() -- stateless AI call via the node's gateway

Do not import anything outside the Python stdlib except `requests`
(available in the Rein venv).
"""

import http.client
import json
import os
import socket
import sys

CORE_API_SOCK = os.environ.get("SN_CORE_API_SOCK", "/run/smart-node/api/core-api.sock")
GATEWAY_URL   = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
GATEWAY_MODEL = os.environ.get("TASK_SIMPLE_MODEL", "sonnet")


# ──────────────────────────────────────────
# Unix-socket HTTP
# ──────────────────────────────────────────

class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sock_path):
        super().__init__("localhost")
        self._sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._sock_path)


def core_api(method: str, path: str, body=None, *, sock: str = CORE_API_SOCK):
    """Make an HTTP request to the planner (or another core-api route).

    Planner calls go straight to the planner with the Runner's own planner key
    when the engine passes it down (PLANNER_API_URL / PLANNER_API_KEY), the way
    the task-complex blocks talk to it: from release 1.12.48 the core-api socket
    refuses planner mutations from a process that is not an agent (HTTP 403,
    "planner mutations require an authenticated session"), so every comment
    these blocks posted was silently lost. Without the key, and for every other
    route, the request goes over the core-api Unix socket as before.

    Returns (status_code, parsed_json_or_None).
    Raises RuntimeError on connection failure.
    """
    key = os.environ.get("PLANNER_API_KEY", "")
    base = os.environ.get("PLANNER_API_URL", "")
    payload = json.dumps(body).encode() if body is not None else None
    if key and base.startswith("http://") and path.startswith("/api/planner/"):
        host, _, port = base[len("http://"):].rstrip("/").partition(":")
        conn = http.client.HTTPConnection(host, int(port or 80), timeout=30)
        target = path[len("/api/planner"):]
        headers = {"Content-Type": "application/json", "X-API-Key": key}
        where = base
    else:
        conn = _UnixHTTPConnection(sock)
        target = path
        headers = {"Content-Type": "application/json", "Host": "localhost"}
        where = sock
    try:
        conn.request(method, target, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    except OSError as exc:
        raise RuntimeError(
            f"Cannot reach the planner at {where}: {exc}. "
            "Check that it is running and the process may reach it."
        ) from exc

    status = resp.status
    try:
        parsed = json.loads(data) if data else None
    except json.JSONDecodeError:
        parsed = data.decode("utf-8", errors="replace")
    return status, parsed


def get_task(task_id: int):
    """Fetch task details from the planner."""
    status, body = core_api("GET", f"/api/planner/api/v1/tasks/{task_id}")
    if status != 200:
        raise RuntimeError(f"GET /tasks/{task_id} returned {status}: {body}")
    return body


def get_comments(task_id: int):
    """Fetch task comments from the planner."""
    status, body = core_api("GET", f"/api/planner/api/v1/tasks/{task_id}/comments")
    if status != 200:
        raise RuntimeError(f"GET /tasks/{task_id}/comments returned {status}: {body}")
    return body or []


def post_comment(task_id: int, content: str):
    """Post a comment to a planner task."""
    status, body = core_api(
        "POST",
        f"/api/planner/api/v1/tasks/{task_id}/comments",
        body={"content": content},
    )
    if status not in (200, 201):
        raise RuntimeError(f"POST /tasks/{task_id}/comments returned {status}: {body}")
    return body


def set_status(task_id: int, status: str, comment: str):
    """Move a planner task to a new status. Comment is required.

    Posts the comment first so the reason is visible before the state changes.
    Surfaces the planner's refusal honestly: the database enforces the transition
    graph, and a rejection reaches the caller as RuntimeError with the planner's
    own message. Never swallowed.
    """
    post_comment(task_id, comment)
    http_status, body = core_api(
        "PATCH",
        f"/api/planner/api/v1/tasks/{task_id}",
        body={"status": status},
    )
    if http_status not in (200, 204):
        raise RuntimeError(
            f"PATCH /tasks/{task_id} status={status!r} returned {http_status}: {body}"
        )
    return body


def assign(task_id: int, assignee: str, comment: str):
    """Reassign a planner task to a new actor. Comment is required.

    This is the handoff  principle 2: a status change that does not
    name the next actor is a pause, not a handoff.

    Posts the comment first, then PATCHes the assignee. Surfaces refusal.
    """
    post_comment(task_id, comment)
    http_status, body = core_api(
        "PATCH",
        f"/api/planner/api/v1/tasks/{task_id}",
        body={"assignee": assignee},
    )
    if http_status not in (200, 204):
        raise RuntimeError(
            f"PATCH /tasks/{task_id} assignee={assignee!r} returned {http_status}: {body}"
        )
    return body


# ──────────────────────────────────────────
# Gateway AI call
# ──────────────────────────────────────────

def gateway_call(prompt: str, model: str = GATEWAY_MODEL) -> str:
    """Stateless AI call via the node gateway.

    1. Creates a session
    2. Sends the prompt
    3. Closes the session
    Returns the AI's text response.
    """
    try:
        import requests as _req
    except ImportError:
        raise RuntimeError(
            "The 'requests' library is required but not installed in this environment."
        )

    if not GATEWAY_URL:
        raise RuntimeError(
            "AI_GATEWAY_URL is not set. The run-workflow.py wrapper should set it."
        )

    # 1. Create session
    r = _req.post(
        f"{GATEWAY_URL}/api/v1/sessions",
        json={"service": "rein", "model": model},
        timeout=30,
    )
    r.raise_for_status()
    session_id = r.json()["session_id"]

    try:
        # 2. Send prompt
        r = _req.post(
            f"{GATEWAY_URL}/api/v1/sessions/{session_id}/chat",
            json={"content": prompt},
            timeout=300,
        )
        r.raise_for_status()
        return r.json().get("content", "")
    finally:
        # 3. Always close session
        try:
            _req.delete(f"{GATEWAY_URL}/api/v1/sessions/{session_id}", timeout=10)
        except Exception:
            pass


# ──────────────────────────────────────────
# Verdict extraction
# ──────────────────────────────────────────

def extract_verdict(text: str) -> str:
    """Find the last 'VERDICT: X' line and return X (upper-cased, stripped).

    Returns empty string if none found.
    """
    verdict = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("VERDICT:"):
            verdict = stripped[len("VERDICT:"):].strip().upper()
    return verdict


# ──────────────────────────────────────────
# Context reader (standard Rein stdin)
# ──────────────────────────────────────────

def read_context() -> dict:
    """Read the JSON context Rein passes on stdin."""
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse Rein context from stdin: {exc}") from exc


def write_result(output_file: str, result: str):
    """Write result.json in the format Rein expects."""
    import os
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w") as fh:
        json.dump({"result": result}, fh, indent=2)
