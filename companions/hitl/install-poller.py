#!/usr/bin/env python3
"""Install the optional signed-answer poller when its workflow is present."""

import http.client
import json
import os
import shlex
import shutil
import socket
import tempfile
from pathlib import Path


FLOW = Path("/var/lib/smart-node/workflows/flows/content-article-hitl")
SOURCE = Path(__file__).resolve().parent
DEST = Path("/opt/smart-node/hitl")
CONFIG = Path("/etc/smart-node/content-article-hitl-poller.env")
CRONTAB = Path("/etc/cron.d/smart-node-agents")
SOCKET = "/run/smart-node/api/core-api.sock"
SLUG = "content-article-hitl-poll"
COMMAND = str(DEST / "flow_poller.sh")
SCHEDULE = "* * * * *"
PREREQUISITES = (
    Path("/etc/smart-node/planner-key-runner.env"),
    Path("/opt/smart-node/hitl/caller/hitl.py"),
    Path("/usr/local/lib/smart-node/cron-runner.sh"),
)


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(SOCKET)


def api(method, path, payload=None):
    conn = UnixConnection("core-api", timeout=30)
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"} if body else {})
        response = conn.getresponse()
        raw = response.read()
        data = json.loads(raw) if raw else {}
        if response.status not in (200, 201):
            raise RuntimeError(f"{method} {path}: HTTP {response.status}: {str(data)[:250]}")
        return data
    finally:
        conn.close()


def create_config(flow):
    values = {
        "CONTENT_ARTICLE_HITL_FLOW_DIR": str(flow),
        "CONTENT_ARTICLE_HITL_TASKS_DIR": str(flow.parent.parent / "tasks"),
        "CONTENT_ARTICLE_HITL_STATE_DIR": "/var/lib/smart-node/hitl-flow-caller",
    }
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".content-article-hitl-", dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={shlex.quote(value)}\n")
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, CONFIG)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)


def install():
    if not (FLOW / "content-article-hitl.yaml").is_file():
        return
    if os.geteuid() != 0:
        raise RuntimeError("poller installation must run as root")

    # Workflows is installed before the optional native HITL companion. On a
    # node with the old Docker deployment the caller lives under app/caller,
    # and on a node without HITL there is no caller at all. Both are normal
    # states here: hitl.sh retries this installer after placing the native
    # caller, when the poller can actually run.
    missing = [path for path in PREREQUISITES if not path.is_file()]
    if missing:
        print("HITL poller deferred; prerequisites missing: " + ", ".join(map(str, missing)))
        return

    existing = next((row for row in api("GET", "/api/crons")["crons"]
                     if row.get("slug") == SLUG), None)
    if existing and (existing.get("command") != COMMAND or not existing.get("enabled")):
        raise RuntimeError("the existing poller cron differs; leaving it unchanged")

    sources = {name: SOURCE / name for name in ("flow_poller.py", "flow_poller.sh")}
    for path in sources.values():
        if not path.is_file():
            raise RuntimeError(f"poller source is missing: {path}")

    DEST.mkdir(mode=0o2755, parents=True, exist_ok=True)
    for name, source in sources.items():
        target = DEST / name
        if not target.exists() and not target.is_symlink():
            temporary = DEST / f".{name}.new"
            shutil.copyfile(source, temporary)
            os.chown(temporary, 0, 0)
            temporary.chmod(0o755 if name.endswith(".sh") else 0o644)
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)

    create_config(FLOW)
    if existing:
        return

    api("POST", "/api/crons", {
        "slug": SLUG,
        "schedule": SCHEDULE,
        "command": COMMAND,
        "script_path": str(DEST / "flow_poller.py"),
        "description": "Check signed workflow answers and resume waiting tasks.",
        "group": "workflows",
        "crontab_user": "root",
        "enabled": True,
        "notes": "Optional workflow signed-answer poller.",
    })
    api("POST", "/api/crons/render")
    if not CRONTAB.is_file() or f"cron-runner.sh {SLUG} {COMMAND}" not in CRONTAB.read_text():
        raise RuntimeError("the generated cron file does not run the poller")


if __name__ == "__main__":
    install()
