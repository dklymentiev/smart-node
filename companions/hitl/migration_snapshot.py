#!/usr/bin/env python3
"""Snapshot stopped legacy HITL, including SQLite WAL and both caller trees.

Run only after the old server has stopped. Output contains counts, never keys,
request bodies, or answers. A copy is accepted only if two independent SQLite
reads and both copied caller states pass the same preflight as the source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from migration_preflight import audit


def file_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[str(path.relative_to(root))] = digest.hexdigest()
    return hashes


def ownership(root: Path) -> dict[str, list[int]]:
    """Record original owners; copytree does not preserve uid/gid."""
    result = {}
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"linked HITL path cannot be backed up: {path}")
        info = path.stat()
        result[str(path.relative_to(root))] = [info.st_uid, info.st_gid]
    return result


def restore_ownership(backup: Path, state: Path, caller: Path) -> None:
    with (backup / "ownership.json").open() as stream:
        owners = json.load(stream)
    if set(owners) != {"state", "caller"}:
        raise ValueError("HITL ownership manifest is incomplete")
    for name, root in (("state", state), ("caller", caller)):
        default = owners[name].get(".")
        if not isinstance(default, list) or len(default) != 2:
            raise ValueError("HITL root ownership is missing")
        # New answers and request files created after adoption must be readable
        # by the old service too. Apply the former tree owner, then restore
        # any paths that originally had a distinct owner.
        for path in (root, *root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"linked HITL path during rollback: {path}")
            os.chown(path, *default, follow_symlinks=False)
        for relative, ids in owners[name].items():
            sub = Path(relative)
            if sub.is_absolute() or ".." in sub.parts or len(ids) != 2:
                raise ValueError("invalid HITL ownership record")
            path = root / sub
            if path.exists():
                os.chown(path, *ids, follow_symlinks=False)


def snapshot(state: Path, caller: Path, flow_caller: Path, app: Path,
             dest: Path, route: Path | None = None) -> dict[str, int]:
    before = audit(state / "hitl.sqlite", [caller, flow_caller])
    if dest.exists() and any(dest.iterdir()):
        raise ValueError("backup destination is not empty")
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    owners = {"state": ownership(state), "caller": ownership(caller)}
    (dest / "ownership.json").write_text(json.dumps(owners, sort_keys=True) + "\n")
    os.chmod(dest / "ownership.json", 0o600)
    for source, name in ((state, "state"), (caller, "caller"),
                         (flow_caller, "flow-caller"), (app, "app")):
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"missing or linked source directory: {source}")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError(f"linked HITL tree cannot be backed up: {source}")
        shutil.copytree(source, dest / name, symlinks=True)
        if file_hashes(source) != file_hashes(dest / name):
            raise ValueError(f"HITL backup file differs from source: {name}")
    if route is not None and route.exists():
        if route.is_symlink() or not route.is_file():
            raise ValueError("legacy route is linked or invalid")
        shutil.copy2(route, dest / "route")
    copied = audit(dest / "state/hitl.sqlite", [dest / "caller", dest / "flow-caller"])
    with sqlite3.connect(f"file:{state / 'hitl.sqlite'}?mode=ro", uri=True) as source:
        with sqlite3.connect(dest / "hitl.sqlite.snapshot") as target:
            source.backup(target)
    independent = audit(dest / "hitl.sqlite.snapshot",
                        [dest / "caller", dest / "flow-caller"])
    if before != copied or copied != independent:
        raise ValueError("HITL backup differs from stopped source")
    (dest / "audit.json").write_text(json.dumps(before, sort_keys=True) + "\n")
    os.sync()
    return before


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--caller", type=Path, required=True)
    parser.add_argument("--flow-caller", type=Path)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--dest", type=Path)
    parser.add_argument("--route", type=Path)
    parser.add_argument("--restore-owners", type=Path)
    args = parser.parse_args()
    try:
        if args.restore_owners:
            restore_ownership(args.restore_owners, args.state, args.caller)
        else:
            if not args.flow_caller or not args.app or not args.dest:
                parser.error("snapshot needs --flow-caller, --app and --dest")
            print(json.dumps(snapshot(args.state, args.caller, args.flow_caller,
                                      args.app, args.dest, args.route), sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"HITL snapshot failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
