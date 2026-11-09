#!/usr/bin/env python3
"""Reject unacknowledged grant/MCP removals between update snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream)
    agents = document.get("agents")
    if not isinstance(agents, dict):
        raise ValueError(f"{path}: agents must be an object")
    return agents


def caps(mode: str | None) -> set[str]:
    if mode is None:
        return set()
    if len(mode) != 3:
        raise ValueError(f"invalid grant mode {mode!r}")
    return {cap for cap, mark in zip("rwd", mode) if mark != "-"}


def compare(before: dict, after: dict) -> tuple[list[str], list[str]]:
    losses: list[str] = []
    gains: list[str] = []
    for agent in sorted(set(before) | set(after)):
        old = before.get(agent, {})
        new = after.get(agent, {})
        old_grants = old.get("grants", {})
        new_grants = new.get("grants", {})
        for tool in sorted(set(old_grants) | set(new_grants)):
            old_mode, new_mode = old_grants.get(tool), new_grants.get(tool)
            removed = caps(old_mode) - caps(new_mode)
            added = caps(new_mode) - caps(old_mode)
            if removed:
                target = new_mode if new_mode is not None else "removed"
                losses.append(f"{agent} {tool}: {old_mode} -> {target} (lost {''.join(sorted(removed))})")
            if added:
                source = old_mode if old_mode is not None else "added"
                gains.append(f"{agent} {tool}: {source} -> {new_mode}")

        old_mcp = set(old.get("mcp_servers", []))
        new_mcp = set(new.get("mcp_servers", []))
        losses.extend(f"{agent} MCP {name}: rendered -> removed" for name in sorted(old_mcp - new_mcp))
        gains.extend(f"{agent} MCP {name}: added" for name in sorted(new_mcp - old_mcp))
    return losses, gains


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accept-reason")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.accept_reason is not None and not args.accept_reason.strip():
        parser.error("--accept-reason must not be empty")

    try:
        losses, gains = compare(load(args.before), load(args.after))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2

    for change in gains:
        print(f"ADDED/WIDENED {change}")
    for change in losses:
        print(f"REMOVED/NARROWED {change}")
    if not losses:
        print("GRANTS CHECK PASS: no capability was removed or narrowed")
        return 0
    if args.accept_reason is not None:
        print(f"GRANTS CHECK ACCEPTED: {args.accept_reason.strip()}")
        return 0
    print("GRANTS CHECK FAIL: pass --accept-grants-change REASON only for an intentional change")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
