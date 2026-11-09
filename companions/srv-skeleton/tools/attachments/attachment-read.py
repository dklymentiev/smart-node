#!/usr/bin/env python3
"""Materialize one approved attachment for the calling agent's Read tool.

The process runs as the agent. Core-api's Unix socket derives that identity from
SO_PEERCRED, ignores Remote-User, and applies the attachment's parent access and
status gate. Bytes are written under the gateway service's PrivateTmp namespace,
in a 0700 directory named for this uid, with a suffix derived from the stored
content type. No agent receives filesystem access to the durable attachment store.
"""

import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import time
import urllib.parse
import uuid


SOCKET_PATH = os.environ.get(
    "SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock"
)
MAX_BYTES = 25 * 1024 * 1024
STALE_SECONDS = 24 * 60 * 60

SUFFIXES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/webp": ".webp", "application/pdf": ".pdf",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
    "text/html": ".html", "text/css": ".css", "application/json": ".json",
    "application/ld+json": ".json", "application/xml": ".xml",
    "text/xml": ".xml", "application/javascript": ".js",
    "application/yaml": ".yaml", "application/x-yaml": ".yaml",
}


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_PATH)


def request(path):
    conn = UnixConnection("localhost", timeout=30)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read(MAX_BYTES + 1)
        return response.status, dict(response.getheaders()), body
    finally:
        conn.close()


def fail(message):
    print("attachment-read: " + message, file=sys.stderr)
    raise SystemExit(1)


def safe_name(value):
    name = (value or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:160]
    return name or "attachment"


def suffix_for(content_type):
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return SUFFIXES.get(ctype, ".bin")


def cache_root():
    root = Path("/tmp") / ("smart-node-attachments-%s" % os.getuid())
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    cutoff = time.time() - STALE_SECONDS
    for child in root.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        except OSError:
            pass
    return root


def main():
    if len(sys.argv) != 2:
        fail("usage: attachment-read.py <attachment-uuid>")
    try:
        attachment_id = str(uuid.UUID(sys.argv[1]))
    except ValueError:
        fail("attachment id is not a UUID")
    if not os.path.exists(SOCKET_PATH):
        fail("core-api socket is unavailable")

    quoted = urllib.parse.quote(attachment_id, safe="")
    status, _headers, raw = request("/api/attachments/%s" % quoted)
    if status != 200:
        fail("metadata was refused (HTTP %s)" % status)
    try:
        metadata = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        fail("core-api returned invalid metadata")

    content_type = (metadata.get("content_type") or "application/octet-stream").lower()
    if content_type.startswith(("audio/", "video/")):
        fail("audio and video cannot be inspected on this node")

    status, _headers, data = request("/api/attachments/%s/content" % quoted)
    if status != 200:
        fail("content was refused (HTTP %s)" % status)
    if len(data) > MAX_BYTES:
        fail("content exceeds the 25 MiB attachment limit")

    suffix = suffix_for(content_type)
    name = safe_name(metadata.get("filename"))
    if not name.lower().endswith(suffix):
        name += suffix
    directory = cache_root() / attachment_id
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    target = directory / name
    temp = directory / ("." + name + ".part")
    with temp.open("wb") as stream:
        stream.write(data)
    os.chmod(temp, 0o600)
    os.replace(temp, target)
    print(str(target.resolve()))


if __name__ == "__main__":
    main()
