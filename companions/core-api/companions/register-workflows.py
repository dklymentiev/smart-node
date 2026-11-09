"""Workflow registrar: project Rein flow YAMLs -> sn_core.workflows (+ auto-grant).

Source of truth for a workflow's definition is its flow YAML under the flows dir
(SN_FLOWS_DIR, default /opt/smart-node/flows/agents/flows). This registrar reads
each <name>/<name>.yaml and upserts a catalog row (name, title, description, team,
spec_version, steps board, trigger), then grants the workflow to the agents whose
specialists appear in its blocks. Idempotent. The "create a workflow" story:
drop a flow YAML, run this, and it shows up in the panel + the agents' grants.

    python companions/register-workflows.py
"""
import glob, json, os, sys

import psycopg2
import yaml

FLOWS_DIR = os.environ.get("SN_FLOWS_DIR", "/opt/smart-node/flows/agents/flows")
# The node's own flows, published by core-api's import door. Registered again
# here so that a reinstall or an update finds them in the catalog: the installer
# never writes this tree, and it must not forget what is in it either.
AUTHORED_DIR = os.environ.get("SN_AUTHORED_FLOWS_DIR", "/var/lib/smart-node/workflows/flows")


def step_of(b):
    spec = b.get("specialist")
    routing = b.get("routing")
    # A block that runs a script is a logic block, not a gate. Calling anything
    # with routing and no specialist a "gate" made every logic hook look like a
    # pause for a human, and the panel then said so in words. The distinction is
    # in the flow YAML already; it was simply not carried across.
    if spec:
        kind = "specialist"
    elif b.get("logic"):
        kind = "logic"
    elif routing:
        kind = "gate"
    else:
        kind = "step"
    # The whole shape, not just the parts a linear picture needs. routing and
    # next carry branches and loop-backs; phase, depends_on and parallel carry
    # the ones that fan out and converge, and they were dropped here. Three of
    # the five flows the engine ships as examples are defined by depends_on
    # alone (deliberation is a 3-phase mesh where each phase-2 block depends on
    # the other two phase-1 blocks), so anything reading this record saw those
    # flows as an unordered list of blocks with no edges at all.
    return {"name": b.get("name"), "specialist": spec, "kind": kind,
            "routing": routing or {}, "next": b.get("next"),
            "phase": b.get("phase"), "depends_on": b.get("depends_on") or [],
            "parallel": bool(b.get("parallel")), "max_runs": b.get("max_runs")}


def main():
    conn = psycopg2.connect(dbname="smartnode", user="smartnode")
    conn.autocommit = True
    cur = conn.cursor()
    n = 0
    for path in sorted(glob.glob(os.path.join(FLOWS_DIR, "*", "*.yaml"))
                       + glob.glob(os.path.join(AUTHORED_DIR, "*", "*.yaml"))):
        # Only <name>/<name>.yaml is a flow; anything else beside it is its data.
        if os.path.basename(path) != os.path.basename(os.path.dirname(path)) + ".yaml":
            continue
        try:
            doc = yaml.safe_load(open(path))
        except Exception as ex:
            print(f"skip {path}: {ex}", file=sys.stderr)
            continue
        if not isinstance(doc, dict) or "blocks" not in doc:
            continue
        if (doc.get("metadata") or {}).get("sample") is True:
            # A sample is a flow to copy, with placeholder names where agents
            # go. In the catalog it would be something a task can be bound to
            # and that hands the task to nobody.
            cur.execute("DELETE FROM sn_config.agent_workflows WHERE workflow_id IN "
                        "(SELECT id FROM sn_core.workflows WHERE name = %s)", (doc.get("name"),))
            cur.execute("DELETE FROM sn_core.workflows WHERE name = %s", (doc.get("name"),))
            print(f"skipped '{doc.get('name')}': a sample, not registered")
            continue
        name = doc["name"]
        blocks = doc.get("blocks", [])
        steps = [step_of(b) for b in blocks]
        specialists = sorted({b.get("specialist") for b in blocks if b.get("specialist")})
        cur.execute(
            """INSERT INTO sn_core.workflows
                 (name, title, description, spec_version, team, definition_path, trigger, steps, tags, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'available')
               ON CONFLICT (tenant_id, name) DO UPDATE SET
                 title=EXCLUDED.title, description=EXCLUDED.description, spec_version=EXCLUDED.spec_version,
                 team=EXCLUDED.team, definition_path=EXCLUDED.definition_path, trigger=EXCLUDED.trigger,
                 steps=EXCLUDED.steps, tags=EXCLUDED.tags, updated_at=now()
               RETURNING id""",
            (name, name.replace("-", " ").title(), doc.get("description", ""), doc.get("schema_version"),
             doc.get("team"), path, doc.get("trigger"), json.dumps(steps), ["flow"] + specialists),
        )
        wf_id = cur.fetchone()[0]
        for slug in specialists:
            cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s", (slug,))
            row = cur.fetchone()
            if row:
                # Not to an agent the owner took off this flow on the panel:
                # the grant from the blocks is a default, the owner's word stands.
                cur.execute("INSERT INTO sn_config.agent_workflows (agent_id, workflow_id) "
                            "SELECT %s, %s WHERE NOT EXISTS (SELECT 1 FROM sn_config.agent_workflow_revokes "
                            "WHERE agent_id = %s AND workflow_id = %s) ON CONFLICT DO NOTHING",
                            (row[0], wf_id, row[0], wf_id))
        print(f"registered '{name}' (id={wf_id}, {len(steps)} steps, granted: {specialists})")
        n += 1
    # The flow file is the source of truth, so a row whose file is gone is not a
    # workflow any more: a flow the release retired, or one the owner deleted.
    # Left in place it stays in the panel as something that can be started and
    # cannot run. Only rows this registrar wrote from this flows dir are judged.
    cur.execute("SELECT id, name, definition_path FROM sn_core.workflows "
                "WHERE definition_path LIKE %s OR definition_path LIKE %s",
                (FLOWS_DIR.rstrip("/") + "/%", AUTHORED_DIR.rstrip("/") + "/%"))
    for wf_id, name, path in cur.fetchall():
        if os.path.exists(path):
            continue
        cur.execute("DELETE FROM sn_config.agent_workflows WHERE workflow_id = %s", (wf_id,))
        cur.execute("DELETE FROM sn_core.workflows WHERE id = %s", (wf_id,))
        print(f"removed '{name}' (id={wf_id}): its flow file is gone")
    print(f"done: {n} workflow(s)")


if __name__ == "__main__":
    main()
