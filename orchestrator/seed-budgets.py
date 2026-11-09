#!/usr/bin/env python3
"""Apply the seed manifest's budgets on the node, the way the owner sets a limit.

Reads a JSON array of budget rows on stdin and writes each one through
core-api's budgets API over the unix socket. Run as root: root on that socket
is the operator identity core-api already trusts for the install.

Why the API and not SQL. A budget row is only the meter. The limit the gateway
enforces lives in the limits registry, and core-api writes both in one
transaction. Inserting the row by SQL left the registry without it: a fresh
node carried a declared hard limit in a table nothing enforces.

Exit 0 only when every row is applied. Anything else is exit 1 with the reason
on stderr, so the installer stops: a declared spend limit must not fail silently.
"""
import http.client
import json
import os
import socket
import sys

SOCKET_PATH = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
PERIODS = ("daily", "weekly", "monthly")
SCOPES = ("tenant", "agent", "session_metadata_key")


class SocketConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(self._path)
        self.sock = sock


def call(method, path, body=None):
    conn = SocketConnection(SOCKET_PATH)
    try:
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    try:
        return resp.status, json.loads(raw) if raw else None
    except ValueError:
        return resp.status, raw


def fail(message):
    print(f"seed-budgets: {message}", file=sys.stderr)
    sys.exit(1)


def normalise(row):
    if not isinstance(row, dict):
        fail(f"a budget row must be an object, got {row!r}")
    scope_type = row.get("scope_type", "tenant")
    scope_id = str(row.get("scope_id", "1"))
    period = row.get("period", "monthly")
    limit = row.get("limit_usd")
    if scope_type not in SCOPES:
        fail(f"unknown scope_type {scope_type!r}")
    if period not in PERIODS:
        fail(f"unknown period {period!r} for {scope_type}/{scope_id}")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        fail(f"limit_usd must be a positive number for {scope_type}/{scope_id}/{period}, got {limit!r}")
    return {"scope_type": scope_type, "scope_id": scope_id, "period": period,
            "limit_usd": limit, "hard_limit": bool(row.get("hard_limit", False))}


def main():
    try:
        rows = json.load(sys.stdin)
    except ValueError as exc:
        fail(f"stdin is not JSON: {exc}")
    if not isinstance(rows, list):
        fail("stdin must be a JSON array of budget rows")
    wanted = [normalise(row) for row in rows]

    try:
        status, listing = call("GET", "/api/budgets")
    except OSError as exc:
        fail(f"core-api socket {SOCKET_PATH} is not answering: {exc}")
    if status != 200 or not isinstance(listing, dict):
        fail(f"GET /api/budgets answered {status}: {listing}")
    existing = {(b["scope_type"], str(b["scope_id"]), b["period"]): b["id"]
                for b in listing.get("budgets", [])}

    for row in wanted:
        key = (row["scope_type"], row["scope_id"], row["period"])
        label = "/".join(key)
        if key in existing:
            status, answer = call("PATCH", f"/api/budgets/{existing[key]}",
                                  {"limit_usd": row["limit_usd"],
                                   "hard_limit": row["hard_limit"], "enabled": True})
            verb = "updated"
        else:
            status, answer = call("POST", "/api/budgets", row)
            verb = "created"
        if status not in (200, 201):
            fail(f"{label}: core-api answered {status}: {answer}")
        print(f"seed-budgets: {label} {verb}, limit_usd={row['limit_usd']} hard={str(row['hard_limit']).lower()}")


if __name__ == "__main__":
    main()
