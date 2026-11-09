"""Sync an agent's tool grants from grants.json into sn_config.agent_tools (the DB).

grants.json (the Tools-tab artifact) is the current store of "which tools an agent
has". For fleet-wide generation we need that queryable in the DB so fan-out
("recompose everyone who has tool X") is one join. This backfills agent_tools from
the file; once the create/grants route writes agent_tools directly, the DB is the
source and grants.json becomes a projection.

    python companions/sync-grants.py <agent-slug>|--all

DB via libpq env; agent dirs under SN_AGENTS_ROOT (no fallbacks -- fail loud).
"""

import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

AGENTS_ROOT = Path(os.environ["SN_AGENTS_ROOT"])

# grants.json values are rwx strings ("rw-", "r--"); map each set position to a cap.
_CAP = {0: "read", 1: "write", 2: "exec"}


def _caps(grant: str) -> list[str]:
    return [_CAP[i] for i, ch in enumerate(grant) if i in _CAP and ch not in ("-", "")]


def _read_grants(slug: str) -> dict[str, str]:
    f = AGENTS_ROOT / slug / "grants.json"
    if not f.is_file():
        return {}
    return (json.loads(f.read_text()).get("grants") or {})


def sync(cur, slug: str) -> None:
    cur.execute("SELECT id FROM sn_config.agents WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if not row:
        print(f"  {slug}: no such agent, skip")
        return
    agent_id = row["id"]
    grants = _read_grants(slug)

    cur.execute("SELECT name, id FROM sn_core.tools")
    tool_id = {r["name"]: r["id"] for r in cur.fetchall()}

    cur.execute("DELETE FROM sn_config.agent_tools WHERE agent_id = %s", (agent_id,))
    placed, missing = [], []
    for name, grant in sorted(grants.items()):
        if name not in tool_id:
            missing.append(name)
            continue
        cur.execute(
            "INSERT INTO sn_config.agent_tools (agent_id, tool_id, granted_caps) "
            "VALUES (%s, %s, %s)",
            (agent_id, tool_id[name], _caps(grant)),
        )
        placed.append(name)
    note = f"  {slug}: synced [{', '.join(placed) or '-'}]"
    if missing:
        note += f"  MISSING from registry: [{', '.join(missing)}]"
    print(note)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: sync-grants.py <agent-slug>|--all")
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if sys.argv[1] == "--all":
                slugs = sorted(p.name for p in AGENTS_ROOT.iterdir()
                               if p.is_dir() and not p.name.startswith(("_", ".")))
            else:
                slugs = [sys.argv[1]]
            for slug in slugs:
                sync(cur, slug)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
