#!/usr/bin/env bash
# Hermetic execution test for the earlier change's core-api avatar writer.
# Run another checkout with SMART_NODE_SOURCE_ROOT=/path/to/tree.

set -uo pipefail

SELF_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${SMART_NODE_SOURCE_ROOT:-$SELF_ROOT}"
ROUTE="$ROOT/companions/core-api/routes/agents.py"

python3 - "$ROUTE" <<'PY'
import ast
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated


failed = 0


def check(label, condition):
    global failed
    print(("ok - " if condition else "not ok - ") + label)
    if not condition:
        failed += 1


tree = ast.parse(Path(sys.argv[1]).read_text())
route = next((node for node in tree.body
              if isinstance(node, ast.AsyncFunctionDef)
              and node.name == "upload_agent_avatar"), None)
check("core-api avatar writer exists", route is not None)
if route is None:
    raise SystemExit(1)
route.decorator_list = []

constants = []
for node in tree.body:
    if not isinstance(node, ast.Assign):
        continue
    names = {target.id for target in node.targets if isinstance(target, ast.Name)}
    if names & {"_AVATAR_UPLOAD_MAX_BYTES", "_AVATAR_UPLOAD_EXTENSIONS"}:
        constants.append(node)


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Request:
    def __init__(self, data):
        self.data = data

    async def body(self):
        return self.data


class Principal:
    def __init__(self, username):
        self.username = username


class NoCwebp:
    TimeoutExpired = subprocess.TimeoutExpired

    @staticmethod
    def run(*_args, **_kwargs):
        raise OSError("cwebp deliberately absent in hermetic test")


with tempfile.TemporaryDirectory() as state:
    updates = []

    def update_agent(slug, payload, updated_by):
        updates.append((slug, payload, updated_by))

    namespace = {
        "Annotated": Annotated,
        "PathParam": lambda **_kwargs: None,
        "Query": lambda default, **_kwargs: default,
        "Depends": lambda dependency: dependency,
        "requires": lambda name, **kw: object(),
        "get_principal": object(),
        "Request": Request,
        "Principal": Principal,
        "HTTPException": HTTPException,
        "AgentUpdate": lambda **kwargs: SimpleNamespace(**kwargs),
        "Path": Path,
        "SN_AVATAR_STATE_DIR": Path(state),
        "re": re,
        "uuid": uuid,
        "tempfile": tempfile,
        "subprocess": NoCwebp,
        "_os": os,
        "update_agent": update_agent,
    }
    module = ast.Module(body=constants + [route], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "agents-avatar-writer.py", "exec"), namespace)
    upload = namespace["upload_agent_avatar"]

    result = asyncio.run(upload("http-agent", Request(b"picture-bytes"), "png", Principal("owner@example.com")))
    files = [path for path in Path(state).iterdir() if not path.name.startswith(".")]
    check("writer keeps the custom/<file> key and legacy URL", result["key"].startswith("custom/http-agent-")
          and result["url"] == "/avatars/" + result["key"])
    check("writer persists the uploaded bytes in node state", len(files) == 1
          and files[0].read_bytes() == b"picture-bytes")
    check("writer attributes the DB pointer update to the resolved principal",
          len(updates) == 1 and updates[0][0] == "http-agent"
          and updates[0][2] == "owner@example.com"
          and updates[0][1].avatar == result["key"])

    for path in Path(state).iterdir():
        path.unlink()

    def refused_update(*_args, **_kwargs):
        raise RuntimeError("database refused update")

    namespace["update_agent"] = refused_update
    try:
        asyncio.run(upload("http-agent", Request(b"orphan"), "png", Principal("owner@example.com")))
    except RuntimeError:
        refused = True
    else:
        refused = False
    check("a refused DB pointer update leaves no orphaned avatar file",
          refused and not list(Path(state).iterdir()))

    try:
        asyncio.run(upload("http-agent", Request(b"x"), "exe", Principal("owner@example.com")))
    except HTTPException as exc:
        unsupported = exc.status_code == 415
    else:
        unsupported = False
    check("unsupported image extensions are refused before writing", unsupported
          and not list(Path(state).iterdir()))

print(f"{6 - failed} passed, {failed} failed")
raise SystemExit(failed != 0)
PY
