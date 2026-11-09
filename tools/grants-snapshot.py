#!/usr/bin/env python3
"""Capture active agents' effective DB grants and rendered MCP server names."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


QUERY = r"""
SELECT a.slug,
       COALESCE(t.name, ''),
       CASE WHEN t.name IS NULL THEN '' ELSE
         (CASE WHEN 'read' = ANY(at.granted_caps) THEN 'r' ELSE '-' END ||
          CASE WHEN 'write' = ANY(at.granted_caps) THEN 'w' ELSE '-' END ||
          CASE WHEN 'delete' = ANY(at.granted_caps) THEN 'd' ELSE '-' END)
       END
FROM sn_config.agents a
LEFT JOIN sn_config.agent_tools at ON at.agent_id = a.id
LEFT JOIN sn_core.tools t ON t.id = at.tool_id
WHERE a.archived_at IS NULL
ORDER BY a.slug, t.name NULLS FIRST
"""


def db_rows(database: str) -> list[tuple[str, str, str]]:
    psql = [
        "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
        "--field-separator=\t", "-d", database, "-c", QUERY,
    ]
    command = ["runuser", "-u", "postgres", "--", *psql] if os.geteuid() == 0 else psql
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        print(result.stderr.strip() or "could not read effective grants", file=sys.stderr)
        raise SystemExit(result.returncode)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise SystemExit(f"unexpected grants row: {line!r}")
        rows.append(tuple(fields))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="smartnode")
    parser.add_argument("--agents-root", type=Path, default=Path("/srv/agents"))
    args = parser.parse_args()

    agents: dict[str, dict[str, object]] = {}
    for slug, tool, mode in db_rows(args.database):
        state = agents.setdefault(slug, {"grants": {}, "mcp_servers": []})
        if tool:
            state["grants"][tool] = mode

    for slug, state in agents.items():
        path = args.agents_root / slug / ".mcp.json"
        if not path.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            servers = document.get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("mcpServers is not an object")
            state["mcp_servers"] = sorted(servers)
        except (OSError, ValueError) as exc:
            print(f"cannot read rendered MCP state for {slug}: {exc}", file=sys.stderr)
            return 1

    json.dump({"agents": agents}, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
