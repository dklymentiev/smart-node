"""Fan-out recompose: when a shared registry block changes, rebuild every agent
that references it. This is the reverse direction of compose-prompt (which does one
agent). It answers "who is affected?" with a single query against the DB grant/policy
links, then recomposes each affected agent.

    python companions/recompose-affected.py --tool planner   # everyone granted planner
    python companions/recompose-affected.py --policy never_send_email
    python companions/recompose-affected.py --all            # every provisioned agent

DB via libpq env. Recomposition is delegated to compose-prompt.py per agent.
"""

import os
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
AGENTS_ROOT = Path(os.environ["SN_AGENTS_ROOT"])

AFFECTED = {
    "--tool": (
        "SELECT a.slug FROM sn_config.agent_tools at "
        "JOIN sn_config.agents a ON a.id = at.agent_id "
        "JOIN sn_core.tools t ON t.id = at.tool_id "
        "WHERE t.name = %s ORDER BY a.slug"
    ),
    "--policy": (
        "SELECT DISTINCT a.slug FROM sn_config.agents a "
        "WHERE a.department_id IN ("
        "  SELECT department_id FROM sn_config.department_policies WHERE policy_slug = %s)"
        " OR a.id IN ("
        "  SELECT agent_id FROM sn_config.agent_policy_opts WHERE policy_slug = %s)"
        " ORDER BY a.slug"
    ),
    "--all": (
        "SELECT slug FROM sn_config.agents WHERE archived_at IS NULL ORDER BY slug"
    ),
}


def affected_slugs(kind: str, value: str | None) -> list[str]:
    sql = AFFECTED[kind]
    params = {"--tool": (value,), "--policy": (value, value), "--all": ()}[kind]
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [r["slug"] for r in cur.fetchall()]
    finally:
        conn.close()


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] not in AFFECTED:
        sys.exit("usage: recompose-affected.py --tool <name>|--policy <slug>|--all")
    kind = args[0]
    value = args[1] if kind != "--all" else None
    if kind != "--all" and not value:
        sys.exit(f"{kind} needs a value")

    slugs = affected_slugs(kind, value)
    # Only provisioned agents (a DB row may exist with no /srv dir, e.g. an
    # unprovisioned demo): fan-out recomposes files, so skip the dir-less ones.
    provisioned = [s for s in slugs if (AGENTS_ROOT / s).is_dir()]
    skipped = [s for s in slugs if s not in provisioned]
    label = f"{kind} {value}" if value else "--all"
    print(f"affected by [{label}]: {len(slugs)} linked, {len(provisioned)} provisioned"
          + (f", skipped (no dir): {', '.join(skipped)}" if skipped else ""))
    slugs = provisioned

    failed = []
    for slug in slugs:
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, "compose-prompt.py"), slug],
            capture_output=True, text=True,
        )
        ok = r.returncode == 0
        print(("  ok  " if ok else "  FAIL ") + slug + (": " + r.stderr.strip() if not ok else ""))
        if not ok:
            failed.append(slug)
    if failed:
        sys.exit(f"{len(failed)} failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
