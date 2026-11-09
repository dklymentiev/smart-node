#!/usr/bin/env python3
"""Private HITL caller bridge. The Unix peer uid, never JSON, names the agent.

The bridge owns the caller key and pinned recipient keys. Agent processes can
connect to the socket but cannot read or alter either key. A verified answer is
consumed once, then staged for the Planner delivery worker. On restart the
worker can recover a consumed answer from the verified check result.
"""

import json
import fcntl
import hashlib
import logging
import os
import pwd
import re
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from recipient_bindings import resolve as recipient_actor

STATE = Path(os.environ.get("HITL_STATE", "/var/lib/smart-node/hitl/caller"))
QUEUE = Path(os.environ.get("HITL_QUEUE", "/var/lib/smart-node/hitl-queue"))
SOCKET = os.environ.get("HITL_AGENT_SOCKET", "/run/smart-node/hitl/agent.sock")
CALLER = os.environ.get("HITL_CALLER", "/opt/smart-node/hitl/caller/hitl.py")
PYTHON = sys.executable
AGENT_RE = re.compile(r"^agent-([a-z][a-z0-9-]{0,62})$")
REQUEST_RE = re.compile(r"^[0-9a-f-]{36}$")
RECIPIENT_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
log = logging.getLogger("hitl-bridge")


def _save(path: Path, value: dict, *, shared: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o640 if shared else 0o600)
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _run(*args: str) -> dict:
    env = {**os.environ, "HITL_STATE": str(STATE)}
    try:
        proc = subprocess.run([PYTHON, CALLER, *args], env=env,
                              capture_output=True, text=True, timeout=90)
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, IndexError, ValueError) as e:
        log.error("caller failed for %s: %s", args[0] if args else "?", e)
        return {"status": "hold", "reason": "caller_unavailable"}


def _actor(sock: socket.socket) -> str | None:
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        name = pwd.getpwuid(uid).pw_name
    except (OSError, KeyError):
        return None
    match = AGENT_RE.fullmatch(name)
    return match.group(1) if match else None


def _owner(request_id: str, actor: str) -> dict | None:
    if not REQUEST_RE.fullmatch(request_id):
        return None
    try:
        with (QUEUE / "requests" / f"{request_id}.json").open() as f:
            item = json.load(f)
    except (OSError, ValueError):
        return None
    return item if item.get("agent") == actor else None


def _intent_path(actor: str, reference: str) -> Path:
    key = hashlib.sha256(f"{actor}\0{reference}".encode()).hexdigest()
    return QUEUE / "intents" / f"{key}.json"


def _recover_intent(path: Path) -> dict | None:
    """Rebuild a queue row when the caller completed before the bridge crashed."""
    if not path.exists():
        return None
    with path.open() as f:
        intent = json.load(f)
    matches = []
    for local_path in (STATE / "requests").glob("*.json"):
        if local_path.stat().st_mtime + 1 < intent["started_at"]:
            continue
        with local_path.open() as f:
            local = json.load(f)
        body = local.get("body", {})
        if (body.get("caller_ref") == intent["reference"]
                and body.get("recipient_id") == intent["recipient"]
                and REQUEST_RE.fullmatch(local.get("request_id", ""))):
            matches.append(local)
    if len(matches) > 1:
        raise ValueError("multiple local HITL requests for one intent")
    if not matches:
        # The caller records locally before making a network request. After
        # its 90-second timeout, an absent record means no request was sent.
        if time.time() - intent["started_at"] > 120:
            path.unlink()
        return None
    local = matches[0]
    if not local.get("link"):
        result = _run("check", local["request_id"])
        if result.get("status") not in ("answered", "expired"):
            return {"status": "hold", "request_id": local["request_id"],
                    "reason": "signed request exists but its private link is unavailable"}
    item = {key: intent[key] for key in
            ("agent", "reference", "recipient", "recipient_actor_id", "title")}
    item["text"] = intent.get("text", "")
    item.update({"request_id": local["request_id"], "link": local.get("link", "")})
    _save(QUEUE / "requests" / f"{local['request_id']}.json", item, shared=True)
    path.unlink()
    if not local.get("link"):
        return {"status": "hold", "request_id": local["request_id"],
                "reason": "signed answer queued; private link unavailable"}
    return {"status": "created", "request_id": local["request_id"],
            "link": local["link"], "recipient_actor_id": item["recipient_actor_id"]}


def _existing(actor: str, reference: str, recipient: str) -> dict | None:
    for path in (QUEUE / "requests").glob("*.json"):
        with path.open() as f:
            item = json.load(f)
        rid = item.get("request_id", "")
        if item.get("agent") != actor or item.get("reference") != reference:
            continue
        if item.get("recipient_actor_id") != recipient_actor(item.get("recipient", "")):
            # The binding moved after this was asked. The delivery worker would
            # refuse its old actor forever, so a fresh ask has to replace it.
            continue
        if (QUEUE / "delivered" / f"{rid}.json").exists():
            continue
        if item.get("recipient") != recipient:
            return {"status": "hold", "request_id": rid,
                    "reason": "another HITL request is pending for this task"}
        return {"status": "created", "request_id": rid,
                "link": item["link"], "expires_at": item.get("expires_at"),
                "recipient_actor_id": item["recipient_actor_id"]}
    return None


def _list() -> dict:
    recipients = []
    for path in sorted((STATE / "pins").glob("*")):
        if not path.is_file():
            continue
        try:
            with path.open() as f:
                pin = json.load(f)
            recipients.append({"recipient": pin["recipient_id"],
                               "mode": pin.get("mode", "device")})
        except (OSError, KeyError, ValueError):
            log.warning("unreadable pin: %s", path.name)
    return {"status": "ok", "recipients": recipients}


def _ask(actor: str, payload: dict) -> dict:
    recipient = payload.get("recipient", "")
    title = payload.get("title", "")
    body = payload.get("text", "")
    reference = payload.get("reference", "")
    if not isinstance(recipient, str) or not RECIPIENT_RE.fullmatch(recipient):
        return {"status": "error", "reason": "invalid recipient"}
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        return {"status": "error", "reason": "title must be 1-200 characters"}
    if not isinstance(body, str) or len(body) > 4000:
        return {"status": "error", "reason": "text must be at most 4000 characters"}
    if not isinstance(reference, str) or not re.fullmatch(r"task:[1-9][0-9]*", reference):
        return {"status": "error", "reason": "reference must be task:<id>"}
    choices = payload.get("choices") or [
        {"value": "approve", "label": "Approve"},
        {"value": "reject", "label": "Reject"},
        {"value": "cancel", "label": "Cancel"},
    ]
    if (not isinstance(choices, list) or not 2 <= len(choices) <= 10
            or any(not isinstance(c, dict) or not isinstance(c.get("value"), str)
                   or not isinstance(c.get("label"), str) for c in choices)):
        return {"status": "error", "reason": "invalid choices"}
    ttl = payload.get("ttl_minutes", 1440)
    if type(ttl) is not int or not 15 <= ttl <= 10080:
        return {"status": "error", "reason": "ttl_minutes must be 15-10080"}
    assurance = payload.get("assurance", "fresh")
    if assurance not in ("fresh", "session"):
        return {"status": "error", "reason": "assurance must be fresh or session"}
    session_hours = payload.get("max_session_hours", 12)
    if type(session_hours) is not int or not 1 <= session_hours <= 24:
        return {"status": "error", "reason": "max_session_hours must be 1-24"}
    manifest = {"title": title, "asked_by": actor,
                "blocks": [{"type": "text", "text": body}],
                "answers": {"choices": choices, "comment": {"max_length": 500}}}
    QUEUE.mkdir(parents=True, exist_ok=True)
    with (QUEUE / "ask.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        intent_path = _intent_path(actor, reference)
        recovered = _recover_intent(intent_path)
        if recovered:
            return recovered
        existing = _existing(actor, reference, recipient)
        if existing:
            return existing
        if intent_path.exists():
            return {"status": "hold", "reason": "HITL request creation needs recovery"}
        recipient_actor_id = recipient_actor(recipient)
        if not recipient_actor_id:
            return {"status": "error", "reason": "recipient has no operator-approved Planner actor binding"}
        _save(intent_path, {"agent": actor, "reference": reference,
                            "recipient": recipient, "recipient_actor_id": recipient_actor_id,
                            "title": title, "text": body, "started_at": time.time()})
        with tempfile.TemporaryDirectory(dir=STATE) as temp:
            mpath = Path(temp) / "manifest.json"
            _save(mpath, manifest)
            policy = ["--assurance", assurance]
            if assurance == "session":
                policy += ["--max-session-age", f"{session_hours}h"]
            result = _run("ask", recipient, "--manifest", str(mpath),
                          "--ref", reference, "--ttl", str(ttl * 60),
                          *policy, "--review")
        if result.get("status") == "created" and REQUEST_RE.fullmatch(result.get("request_id", "")):
            item = {"request_id": result["request_id"], "agent": actor,
                    "reference": reference, "recipient": recipient,
                    "recipient_actor_id": recipient_actor_id, "title": title,
                    "text": body,
                    "link": result["link"], "expires_at": result.get("expires_at")}
            _save(QUEUE / "requests" / f"{result['request_id']}.json", item, shared=True)
            intent_path.unlink()
            result["recipient_actor_id"] = recipient_actor_id
        elif not result.get("request_id"):
            # No caller request was created. A definite refusal is safe to retry.
            if result.get("status") in ("rejected", "error"):
                intent_path.unlink()
        return result


def _check(actor: str, request_id: str) -> dict:
    item = _owner(request_id, actor)
    if not item:
        return {"status": "error", "reason": "request not found for caller"}
    result = _run("check", request_id)
    if result.get("status") == "pending":
        result = {**result, "link": item["link"],
                  "reference": item["reference"],
                  "recipient_actor_id": item["recipient_actor_id"]}
    return result


def dispatch(actor: str, request: dict) -> dict:
    op = request.get("op")
    if op == "list_recipients":
        return _list()
    if op == "ask_human":
        return _ask(actor, request)
    if op == "check_human":
        return _check(actor, request.get("request_id", ""))
    return {"status": "error", "reason": "unknown operation"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        actor = _actor(self.request)
        if not actor:
            result = {"status": "error", "reason": "agent identity not proved"}
        else:
            try:
                line = self.rfile.readline(1_000_001)
                if not line or len(line) > 1_000_000:
                    raise ValueError("request too large")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                result = dispatch(actor, request)
            except (ValueError, TypeError) as e:
                result = {"status": "error", "reason": str(e)}
            except Exception:
                log.exception("request failed for %s", actor)
                result = {"status": "hold", "reason": "bridge_failure"}
        self.wfile.write((json.dumps(result, ensure_ascii=False) + "\n").encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def settle(path: Path) -> None:
    delivery = QUEUE / "delivery" / path.name
    if delivery.exists():
        return
    with path.open() as f:
        item = json.load(f)
    rid = item["request_id"]
    result = _run("check", rid)
    if result.get("status") == "answered":
        consumed = _run("consume", rid)
        # A crash after consume and before delivery leaves the consume marker.
        # check is still verified, so retrying delivery is safe.
        if consumed.get("status") == "answered":
            result = consumed
        elif consumed.get("status") != "rejected" or consumed.get("reason") != "replay":
            return
    elif result.get("status") != "expired":
        return
    _save(delivery, {**item, "result": result}, shared=True)


def watch() -> None:
    while True:
        with (QUEUE / "ask.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for path in sorted((QUEUE / "intents").glob("*.json")):
                try:
                    _recover_intent(path)
                except Exception:
                    log.exception("intent recovery failed for %s", path.name)
        for path in sorted((QUEUE / "requests").glob("*.json")):
            try:
                settle(path)
            except Exception:
                log.exception("watch failed for %s", path.name)
        time.sleep(30)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    Path(SOCKET).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    with Server(SOCKET, Handler) as server:
        os.chmod(SOCKET, 0o660)
        threading.Thread(target=watch, daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
