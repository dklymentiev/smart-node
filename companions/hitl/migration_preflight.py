#!/usr/bin/env python3
"""Read-only audit before adopting a legacy HITL database and caller states.

Print counts only. Never print caller ids, keys, recipient ids, or answers.
This command does not stop services, copy data, or change ownership.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def decode_key(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def read_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or linked state file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid state object: {path}")
    return value


def audit(db_path: Path, states: list[Path]) -> dict[str, int]:
    if not states or len(set(states)) != len(states):
        raise ValueError("provide each caller state directory exactly once")
    if db_path.is_symlink() or not db_path.is_file():
        raise ValueError("database missing or linked")
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != 3:
            raise ValueError(f"HITL database schema {version} is not supported by this release")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("database integrity check failed")
        callers = dict(db.execute("SELECT id, pubkey FROM callers"))
        recipients = {row[0] for row in db.execute("SELECT id FROM recipients")}
        requests = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT id, caller_id, recipient_id, response_record, expires_at FROM requests"
            )
        }
        answered = sum(row[2] is not None for row in requests.values())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        active = sum(row[2] is None and row[3] > now for row in requests.values())
        blobs = db.execute("SELECT count(*) FROM blobs").fetchone()[0]

    seen_callers: set[str] = set()
    local_requests: set[str] = set()
    consumed: set[str] = set()
    pins = 0
    for state in states:
        if state.is_symlink() or not state.is_dir():
            raise ValueError(f"caller state missing or linked: {state}")
        caller = read_object(state / "caller.json")
        caller_id = caller.get("caller_id")
        secret = decode_key(caller.get("secret_key", ""))
        if not isinstance(caller_id, str) or caller_id not in callers:
            raise ValueError(f"unregistered caller state: {state}")
        if len(secret) != 64 or secret[32:] != decode_key(callers[caller_id]):
            raise ValueError(f"caller key does not match database: {state}")
        if caller_id in seen_callers:
            raise ValueError("duplicate caller identity")
        seen_callers.add(caller_id)
        for path in (state / "pins").glob("*.json"):
            pin = read_object(path)
            if pin.get("recipient_id") != path.stem or path.stem not in recipients:
                raise ValueError(f"pin does not match database: {path}")
            pins += 1
        for path in (state / "requests").glob("*.json"):
            item = read_object(path)
            rid = item.get("request_id")
            row = requests.get(rid)
            if rid != path.stem or row is None or row[0] != caller_id or row[1] != item.get("recipient_id"):
                raise ValueError(f"request does not match database: {path}")
            if rid in local_requests:
                raise ValueError("duplicate local request")
            local_requests.add(rid)
        for path in (state / "consumed").glob("*.json"):
            read_object(path)
            row = requests.get(path.stem)
            if row is None or row[0] != caller_id or row[2] is None or path.stem not in local_requests:
                raise ValueError(f"consumed answer does not match local request: {path}")
            consumed.add(path.stem)

    orphaned = {rid for rid, row in requests.items() if row[0] in seen_callers} - local_requests
    if orphaned:
        raise ValueError(f"{len(orphaned)} database requests lack local caller state")
    return {
        "callers_checked": len(seen_callers),
        "schema_version": version,
        "pins": pins,
        "requests_in_database": len(requests),
        "local_requests": len(local_requests),
        "answered": answered,
        "active_unanswered": active,
        "consumed_local": len(consumed),
        "answered_unconsumed_local": sum(
            requests[rid][2] is not None and rid not in consumed
            for rid in local_requests
        ),
        "blobs": blobs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--caller-state", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.db, args.caller_state), sort_keys=True))
    except (OSError, ValueError, binascii.Error, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"HITL migration preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
