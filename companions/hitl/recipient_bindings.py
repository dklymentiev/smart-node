#!/usr/bin/env python3
"""Operator-owned HITL recipient -> Planner human actor bindings.

Agents choose a pinned recipient, never a Planner actor. This root-owned map
connects the two identities. Bind only after checking the person's pin and
the intended human actor in Planner.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import re
import tempfile
from pathlib import Path

BINDINGS = Path(os.environ.get("HITL_RECIPIENT_BINDINGS",
                               "/etc/smart-node/hitl-recipient-actors.json"))
ENV_FILE = Path("/etc/smart-node/hitl.env")
RECIPIENT_ID = re.compile(r"^[a-z0-9._-]{1,64}$")
ACTOR_ID = re.compile(r"^[A-Za-z0-9@._:-]{1,128}$")


def read(path: Path = BINDINGS) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("HITL recipient binding file is missing or linked")
    if path.stat().st_mode & 0o022:
        raise ValueError("HITL recipient binding file is writable by non-owner")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not RECIPIENT_ID.fullmatch(key)
        or not isinstance(value, str) or not ACTOR_ID.fullmatch(value)
        for key, value in data.items()
    ):
        raise ValueError("invalid HITL recipient bindings")
    return data


def resolve(recipient: str, path: Path = BINDINGS) -> str | None:
    if not isinstance(recipient, str) or not RECIPIENT_ID.fullmatch(recipient):
        return None
    return read(path).get(recipient)


def caller_state() -> Path:
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("HITL_STATE="):
            return Path(line.partition("=")[2])
    raise ValueError("HITL_STATE is missing from installed environment")


def bind(recipient: str, actor: str, *, path: Path = BINDINGS,
         state: Path | None = None) -> None:
    if os.geteuid() != 0:
        raise PermissionError("recipient binding requires root")
    if not RECIPIENT_ID.fullmatch(recipient) or not ACTOR_ID.fullmatch(actor):
        raise ValueError("recipient and actor must be valid plain IDs")
    state = state or caller_state()
    pin = state / "pins" / f"{recipient}.json"
    if pin.is_symlink() or not pin.is_file():
        raise ValueError("recipient is not pinned by this caller")
    with pin.open(encoding="utf-8") as stream:
        pinned = json.load(stream)
    if not isinstance(pinned, dict) or pinned.get("recipient_id") != recipient:
        raise ValueError("recipient pin identity mismatch")
    bindings = read(path)
    bindings[recipient] = actor
    fd, temp = tempfile.mkstemp(prefix=".hitl-bindings-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(bindings, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temp, 0, grp.getgrnam("smart-node").gr_gid)
        os.chmod(temp, 0o640)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipient")
    parser.add_argument("human_actor")
    args = parser.parse_args()
    bind(args.recipient, args.human_actor)
    print(f"bound pinned HITL recipient {args.recipient} to Planner actor {args.human_actor}")


if __name__ == "__main__":
    main()
