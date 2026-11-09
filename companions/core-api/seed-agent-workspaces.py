"""Give every agent the memory workspace it was supposed to have from birth.

`sn_config.agents.mesh_workspace` is a nullable column with no default. The create
path stores whatever the caller sent, and on a node born from canon nothing ever
sent anything: no installer, no seed, no panel default. Agents that carry a
workspace today carry it because a human or the configurator typed one.

The failure is silent, which is why it survived. compose_claude_md skips the whole
`# Memory workspace` block when the value is empty, so the agent is never told
which workspace is its own, and its memory tool falls back to `default`. No error
is raised anywhere, on either wake path -- the notes simply pile up in the wrong
place, and only a search that comes back empty reveals it.

The slug is the right value: unique per node, already the agent's runtime identity
(SN_AGENT_SLUG), and nothing has to invent it. New agents get it at creation
(routes/agents.py); this seed is for the rows that predate that.

Idempotent, and never overwrites: a workspace an operator set by hand survives
every restart. Filling the column does NOT rewrite an already-composed
/srv/agents/<slug>/CLAUDE.md -- the file is recomposed on the next
POST /api/agents/<slug>/regenerate, which is logged below per agent so the
operator can see exactly which ones are due one.
"""

import logging

import psycopg2


log = logging.getLogger("seed-agent-workspaces")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main() -> None:
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE sn_config.agents SET mesh_workspace = slug "
                " WHERE archived_at IS NULL "
                "   AND (mesh_workspace IS NULL OR btrim(mesh_workspace) = '') "
                "RETURNING slug"
            )
            filled = [r[0] for r in (cur.fetchall() or [])]
            if not filled:
                log.info("every agent already has a memory workspace")
                return
            for slug in filled:
                log.warning(
                    "agent %s had no memory workspace (its notes were going to "
                    "`default`); set to %s -- its CLAUDE.md is stale until "
                    "POST /api/agents/%s/regenerate", slug, slug, slug)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
