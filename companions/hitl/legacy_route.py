#!/usr/bin/env python3
"""Find a legacy HITL Traefik router and change only its service upstream."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path


def find(directory: Path, host: str) -> Path:
    matches = [path for path in directory.glob("*.yml")
               if path.is_file() and f"Host(`{host}`)" in path.read_text()]
    if len(matches) != 1:
        raise ValueError("expected exactly one existing HITL router for the passkey origin")
    return matches[0]


def rewrite(path: Path, old: str, new: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("HITL router missing or linked")
    source = path.read_text()
    if source.count(old) == 1:
        result = source.replace(old, new)
    elif source.count(new) == 1:
        return  # an installer retry after the route was already committed
    else:
        raise ValueError("legacy HITL router has an unexpected upstream")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".hitl-route-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, path.stat().st_mode & 0o777)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    locate = sub.add_parser("find")
    locate.add_argument("--directory", type=Path, required=True)
    locate.add_argument("--host", required=True)
    change = sub.add_parser("rewrite")
    change.add_argument("--path", type=Path, required=True)
    change.add_argument("--old", required=True)
    change.add_argument("--new", required=True)
    args = parser.parse_args()
    try:
        if args.action == "find":
            print(find(args.directory, args.host))
        else:
            rewrite(args.path, args.old, args.new)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"HITL route migration failed: {exc}\n")


if __name__ == "__main__":
    main()
