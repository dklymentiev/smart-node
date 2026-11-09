"""Read API for the Workflows catalog (sn_core.workflows).

Workflows are Rein flows registered as first-class node objects -- the symmetric
parallel to Tools: catalogued here, granted to agents (sn_config.agent_workflows),
and rendered in the UI. Source of truth = the flow YAMLs under /opt/smart-node/flows,
projected into this catalog by the workflow registrar.
"""
import os as _os

from fastapi import APIRouter, Depends, HTTPException

from auth import Principal
from db import cursor
from events_emit import emit
import flow_publish
from permissions import requires

router = APIRouter()

COLS = (
    "id, name, title, description, status, tags, spec_version, team, "
    "definition_path, trigger, steps, created_at, updated_at, created_by, updated_by"
)


def _granted_map(cur):
    cur.execute(
        """SELECT w.name AS wname, a.slug AS slug
             FROM sn_config.agent_workflows aw
             JOIN sn_core.workflows w ON w.id = aw.workflow_id
             JOIN sn_config.agents a ON a.id = aw.agent_id
            ORDER BY a.slug"""
    )
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["wname"], []).append(r["slug"])
    return out


@router.get("", response_model=dict, dependencies=[Depends(requires("workflows.read"))])
def list_workflows():
    with cursor() as cur:
        cur.execute(f"SELECT {COLS} FROM sn_core.workflows ORDER BY name")
        rows = cur.fetchall()
        grants = _granted_map(cur)
    return {"workflows": [{**dict(r), "granted_to": grants.get(r["name"], [])} for r in rows]}


@router.get("/{name}", response_model=dict, dependencies=[Depends(requires("workflows.read"))])
def get_workflow(name: str):
    with cursor() as cur:
        cur.execute(f"SELECT {COLS} FROM sn_core.workflows WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"workflow '{name}' not found")
        cur.execute(
            """SELECT a.slug FROM sn_config.agent_workflows aw
                 JOIN sn_config.agents a ON a.id = aw.agent_id
                WHERE aw.workflow_id = %s ORDER BY a.slug""",
            (row["id"],),
        )
        granted = [r["slug"] for r in cur.fetchall()]
    return {**dict(row), "granted_to": granted}


import subprocess as _subprocess
from fastapi import Body

# Shipped with the flows tree. It reads the flow YAML for its block names and
# inputs, so a flow authored on this node emits the same events as one that
# came with it. Its predecessor hardcoded one business's blocks, document
# GUIDs and sample customer, and emitted nothing at all for any other flow.
_WRAPPER = _os.environ.get("SN_RUN_WRAPPER", "/opt/smart-node/flows/run-workflow.py")
_VENV_PY = "/opt/smart-node/core-api/venv/bin/python"
# Placeholder only, used when the caller sends no inquiry of their own. Keep it
# business-agnostic: a node may be a studio, a clinic or a shop.
_DEFAULT_INQUIRY = (
    "Hi, I would like to book something for 4 people this Saturday afternoon. "
    "Are you available?"
)


@router.post("/{name}/run", response_model=dict, dependencies=[Depends(requires("workflows.run"))])
def run_workflow(name: str, payload: dict = Body(default={})):
    """Trigger a workflow run. Spawns run-workflow.py and waits up to 1 s for
    an immediate exit. Immediate failures (missing interpreter, bad YAML, missing
    flow file) all exit within milliseconds; this window catches them and returns
    HTTP 500 with the exit code and stderr. Flows that are still running after
    1 s are left to proceed; progress and final status appear in sn_core.events
    (visible on the Events history / Studio Activity)."""
    with cursor() as cur:
        cur.execute("SELECT status FROM sn_core.workflows WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"workflow '{name}' not found")
        if row["status"] != "available":
            raise HTTPException(status_code=409, detail=f"workflow '{name}' is off: no new run starts")
    inquiry = (payload.get("inquiry") or "").strip() or _DEFAULT_INQUIRY
    decision = (payload.get("owner_decision") or "approve").strip() or "approve"
    proc = _subprocess.Popen(
        [_VENV_PY, _WRAPPER, name, inquiry, decision],
        stdout=_subprocess.DEVNULL, stderr=_subprocess.PIPE, start_new_session=True,
    )
    # Poll for 1 s to detect immediate launch failures. run-workflow.py does not
    # write much to stderr (it emits structured events to sn_core.events), so the
    # 64 KB pipe buffer will not fill for long-running flows.
    try:
        rc = proc.wait(timeout=1)
    except _subprocess.TimeoutExpired:
        rc = None
    if rc is not None and rc != 0:
        stderr_text = ""
        try:
            stderr_text = proc.stderr.read(2000).decode(errors="replace").strip()
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail={
                "error": "workflow launch failed",
                "exit_code": rc,
                "detail": stderr_text,
            },
        )
    return {"status": "started", "workflow": name, "owner_decision": decision,
            "note": "running; watch the Events history"}


# ---------------------------------------------------------------------------
# Import: validate + materialize a flow YAML into the catalog (+ auto-grant).
# The YAML on disk is the source of truth; this projects it into
# sn_core.workflows and grants it to the agents named in its blocks. Mirrors
# companions/register-workflows.py, adding validation and per-flow targeting.
# See system/procedures/create-workflow.md (Publish step).
# ---------------------------------------------------------------------------
import glob as _glob
import json as _json
import shutil as _shutil
import yaml as _yaml

_FLOWS_DIR = _os.environ.get("SN_FLOWS_DIR", "/opt/smart-node/flows/agents/flows")
# The node's own flows, as published by this door: what the Runner runs. In
# the node's state, not under /opt, which an update replaces. The installer
# creates the directory, this service owns it, and nothing but flow_publish
# writes inside.
_AUTHORED_DIR = _os.environ.get("SN_AUTHORED_FLOWS_DIR", "/var/lib/smart-node/workflows/flows")
# Where an author may have written the flow he asks to publish. An agent has
# two places of its own on every node: its home, which its OS user owns and its
# shell writes (<homes>/agent-<slug>), and its directory under the agents root,
# which the file tool writes for it (<agents>/<slug>). A node that keeps
# authored flows somewhere else names the place in SN_FLOW_SOURCE_ROOTS.
_AGENTS_ROOT = _os.environ.get("SN_AGENTS_ROOT", "/srv/agents")
_AGENT_HOMES = _os.environ.get("SN_AGENT_HOMES", "/var/lib/smart-node/agents")
_SOURCE_ROOTS = [p for p in _os.environ.get("SN_FLOW_SOURCE_ROOTS",
                                            _AGENT_HOMES + ":" + _AGENTS_ROOT).split(":") if p]
_FLOW_TOOLS = _os.environ.get("SN_FLOW_TOOLS", "/opt/smart-node/flows/tools")


def _step_of(b):
    # The same record companions/register-workflows.py stores, field for field.
    # The two roads into the catalogue disagreed: this one never looked at
    # `logic`, so a flow an agent published showed every script block as a gate,
    # which the page reads as a pause for a person, and it dropped the fields
    # that carry a flow's fan-out.
    spec = b.get("specialist")
    routing = b.get("routing")
    if spec:
        kind = "specialist"
    elif b.get("logic"):
        kind = "logic"
    elif routing:
        kind = "gate"
    else:
        kind = "step"
    return {"name": b.get("name"), "specialist": spec, "kind": kind,
            "routing": routing or {}, "next": b.get("next"),
            "phase": b.get("phase"), "depends_on": b.get("depends_on") or [],
            "parallel": bool(b.get("parallel")), "max_runs": b.get("max_runs")}


def _flow_path(name):
    """A flow is in the shipped tree or among the node's own published flows;
    the door never lets one name be in both."""
    for root in (_FLOWS_DIR, _AUTHORED_DIR):
        path = _os.path.join(root, name, name + ".yaml")
        if _os.path.isfile(path):
            return path
    return _os.path.join(_FLOWS_DIR, name, name + ".yaml")


def _validate_flow(name, cur, path=None):
    """Return (doc, errors, warnings); doc is None if the file is unreadable."""
    errors, warnings = [], []
    path = path or _flow_path(name)
    if not _os.path.isfile(path):
        return None, ["flow file not found: %s" % path], []
    try:
        doc = _yaml.safe_load(open(path)) or {}
    except Exception as ex:
        return None, ["YAML parse error: %s" % ex], []
    if not isinstance(doc, dict):
        return None, ["flow root is not a mapping"], []

    # 3.4.0 is the engine schema with time budgets. Whether the flow matches
    # the version it declares is the lint's check, against the engine's own file.
    if str(doc.get("schema_version")) not in ("3.3.0", "3.4.0"):
        errors.append("schema_version must be \"3.3.0\" or \"3.4.0\" (got %r)"
                      % doc.get("schema_version"))
    if doc.get("name") != name:
        errors.append("name field %r must match folder/file %r" % (doc.get("name"), name))

    blocks = doc.get("blocks") or []
    if not blocks:
        errors.append("flow has no blocks")
    block_names = {b.get("name") for b in blocks}
    flow_dir = _os.path.dirname(path)
    for b in blocks:
        spec = b.get("specialist")
        if spec:
            cur.execute("SELECT 1 FROM sn_config.agents WHERE slug=%s AND archived_at IS NULL", (spec,))
            if not cur.fetchone():
                errors.append("block %r references unknown agent %r" % (b.get("name"), spec))
        custom = (b.get("logic") or {}).get("custom")
        if isinstance(custom, str) and not _os.path.isfile(_os.path.join(flow_dir, custom)):
            errors.append("block %r logic file missing: %s" % (b.get("name"), custom))
        for tok, target in (b.get("routing") or {}).items():
            if target not in block_names:
                warnings.append("block %r routing %r -> %r is not a known block"
                                % (b.get("name"), tok, target))
    return doc, errors, warnings


def _materialize(name, doc, cur, path=None):
    path = path or _flow_path(name)
    blocks = doc.get("blocks", [])
    steps = [_step_of(b) for b in blocks]
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
         doc.get("team"), path, doc.get("trigger"), _json.dumps(steps), ["flow"] + specialists),
    )
    wf_id = cur.fetchone()["id"]
    for slug in specialists:
        cur.execute("SELECT id FROM sn_config.agents WHERE slug=%s", (slug,))
        row = cur.fetchone()
        if row:
            # Not to an agent the owner took off this flow (agent_workflow_revokes).
            cur.execute("INSERT INTO sn_config.agent_workflows (agent_id, workflow_id) "
                        "SELECT %s, %s WHERE NOT EXISTS (SELECT 1 FROM sn_config.agent_workflow_revokes "
                        "WHERE agent_id = %s AND workflow_id = %s) ON CONFLICT DO NOTHING",
                        (row["id"], wf_id, row["id"], wf_id))
    return {"name": name, "status": "available", "steps": len(steps), "granted_to": specialists}


def _publish(name, source, actor):
    """Check a flow its author wrote, publish a copy, register the copy.

    The one act by which a node's own flow comes to exist. On red nothing is
    copied and nothing reaches the catalog, and the answer says which check
    refused and what to fix. See flow_publish for what is checked and why the
    author's own tests are not run here.
    """
    if actor in ("anonymous", "unknown-peer"):
        raise HTTPException(status_code=403, detail="publishing a flow needs a caller the node can name")
    # An agent publishes what is in its own directories. Somebody else's
    # directory is the owner's to publish, not another agent's.
    if _os.path.isdir(_os.path.join(_AGENTS_ROOT, actor)):
        real = _os.path.realpath(source)
        own = [_os.path.realpath(_os.path.join(_AGENTS_ROOT, actor)),
               _os.path.realpath(_os.path.join(_AGENT_HOMES, "agent-" + actor))]
        if not any(real.startswith(o + "/") for o in own):
            raise HTTPException(status_code=403,
                                detail="%s may publish a flow from its own directory, not from anywhere else" % actor)

    cfg = {"shipped": _FLOWS_DIR, "authored": _AUTHORED_DIR, "source_roots": _SOURCE_ROOTS,
           "tools": _FLOW_TOOLS}

    def refuse(step, findings):
        with cursor(commit=True) as cur:
            emit(cur, actor=actor, event_type="workflow.publish.rejected", entity_type="workflow",
                 entity_id=name or "?", payload={"step": step, "source": source, "findings": findings[:20]})
        raise HTTPException(status_code=400, detail={"message": "not published", "step": step, "findings": findings})

    try:
        staging = flow_publish.check(name, source, cfg)
    except flow_publish.Refused as r:
        refuse(r.step, r.findings)
    staged = _os.path.join(staging, "flows", name, name + ".yaml")
    with cursor() as cur:
        doc, errors, warnings = _validate_flow(name, cur, path=staged)
    if errors:
        _shutil.rmtree(staging, ignore_errors=True)
        refuse("catalog", errors)
    path = flow_publish.publish(name, staging, cfg)
    with cursor(commit=True) as cur:
        imported = _materialize(name, doc, cur, path=path)
        emit(cur, actor=actor, event_type="workflow.published", entity_type="workflow", entity_id=name,
             payload={"version": (doc.get("metadata") or {}).get("version"), "source": source,
                      "definition_path": path, "warnings": warnings[:20]})
    return {"imported": [imported], "published": path,
            "report": {name: {"errors": [], "warnings": warnings}}}


@router.post("/import", response_model=dict)
def import_workflows(payload: dict = Body(default={}),
                     principal: Principal = Depends(requires("workflows.import"))):
    """Validate flow YAML(s) on disk and project them into the catalog (+ grants).

    Body: {"name": "<flow>"} imports one; omit to import every flow folder.
    Validation is a hard gate: if any targeted flow has errors, nothing is
    materialized and the report is returned with HTTP 400.

    Body: {"name": "<flow>", "source": "<directory>"} publishes a flow the node
    made itself: the directory is checked, copied into the published tree and
    the copy is registered. See _publish."""
    name = (payload.get("name") or "").strip()
    source = (payload.get("source") or "").strip()
    if source:
        # The author is the account the permission check resolved: an agent on
        # the socket is named by the kernel, the owner by a session or as the
        # node's root account. _publish applies its own rule about whose
        # directory an author may publish from.
        return _publish(name, source, principal.username)
    if name:
        targets = [name]
    else:
        targets = sorted(_os.path.basename(_os.path.dirname(p))
                         for root in (_FLOWS_DIR, _AUTHORED_DIR)
                         for p in _glob.glob(_os.path.join(root, "*", "*.yaml")))
    if not targets:
        raise HTTPException(status_code=404, detail="no flows found")

    report = {}
    with cursor(commit=True) as cur:
        docs, any_error = {}, False
        for t in targets:
            doc, errors, warnings = _validate_flow(t, cur)
            report[t] = {"errors": errors, "warnings": warnings}
            docs[t] = doc
            if errors:
                any_error = True
        if any_error:
            raise HTTPException(status_code=400, detail={"message": "validation failed", "report": report})
        imported = [_materialize(t, docs[t], cur) for t in targets]
    return {"imported": imported, "report": report}


# ---------------------------------------------------------------------------
# The owner's controls (panel Workflows, 2026-10-15): a workflow on or off, and
# which agents may start it. workflows.write is the owner's alone.
#
# Off means no new run starts: this run route refuses it, and the Runner does
# not start a task bound to it (a run already in flight finishes). An agent's
# prompt lists only the flows that are on, so the agents granted it are
# recomposed when it changes.
#
# Taking an agent off a flow is remembered (sn_config.agent_workflow_revokes):
# the registrar and the import door grant a flow to the agents named in its
# blocks every time they run, and without the memory an update would put the
# agent back. Giving the agent the flow again forgets it.
# ---------------------------------------------------------------------------
from pydantic import BaseModel as _BaseModel

_STATES = ("available", "off")


class StatusInput(_BaseModel):
    status: str


class AgentsInput(_BaseModel):
    agents: list[str]


def _recompose(cur, slugs):
    """Rewrite the prompts of these agents (they list the flows they may run)."""
    from routes import agents as _agents  # late: agents imports most of the API
    for slug in sorted(set(slugs)):
        _agents._compose_and_write_claude(cur, slug)


def _wf_row(cur, name):
    cur.execute("SELECT id, status FROM sn_core.workflows WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"workflow '{name}' not found")
    return row


def _granted(cur, wf_id):
    cur.execute("SELECT a.slug FROM sn_config.agent_workflows aw "
                "JOIN sn_config.agents a ON a.id = aw.agent_id WHERE aw.workflow_id = %s", (wf_id,))
    return {r["slug"] for r in cur.fetchall()}


@router.patch("/{name}", response_model=dict)
def set_status(name: str, body: StatusInput, principal: Principal = Depends(requires("workflows.write"))):
    if body.status not in _STATES:
        raise HTTPException(status_code=422, detail=f"status is one of {', '.join(_STATES)}")
    with cursor(commit=True) as cur:
        row = _wf_row(cur, name)
        if row["status"] == body.status:
            return {"name": name, "status": body.status, "changed": False}
        cur.execute("UPDATE sn_core.workflows SET status = %s, updated_at = now(), updated_by = %s "
                    "WHERE id = %s", (body.status, principal.username, row["id"]))
        granted = _granted(cur, row["id"])
        emit(cur, actor=f"user:{principal.username}", event_type="workflow.status_changed",
             entity_type="workflow", entity_id=name,
             payload={"from": row["status"], "to": body.status})
        _recompose(cur, granted)
    return {"name": name, "status": body.status, "changed": True, "recomposed": sorted(granted)}


@router.put("/{name}/agents", response_model=dict)
def set_agents(name: str, body: AgentsInput, principal: Principal = Depends(requires("workflows.write"))):
    want = {s.strip() for s in body.agents if s and s.strip()}
    with cursor(commit=True) as cur:
        row = _wf_row(cur, name)
        cur.execute("SELECT id, slug FROM sn_config.agents WHERE archived_at IS NULL")
        ids = {r["slug"]: r["id"] for r in cur.fetchall()}
        unknown = sorted(want - set(ids))
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown agent: {', '.join(unknown)}")
        have = _granted(cur, row["id"])
        added, removed = sorted(want - have), sorted(have - want)
        for slug in added:
            cur.execute("INSERT INTO sn_config.agent_workflows (agent_id, workflow_id) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", (ids[slug], row["id"]))
            cur.execute("DELETE FROM sn_config.agent_workflow_revokes WHERE agent_id = %s AND workflow_id = %s",
                        (ids[slug], row["id"]))
        for slug in removed:
            cur.execute("DELETE FROM sn_config.agent_workflows WHERE agent_id = %s AND workflow_id = %s",
                        (ids[slug], row["id"]))
            cur.execute("INSERT INTO sn_config.agent_workflow_revokes (agent_id, workflow_id, revoked_by) "
                        "VALUES (%s, %s, %s) ON CONFLICT (agent_id, workflow_id) DO UPDATE SET "
                        "revoked_at = now(), revoked_by = EXCLUDED.revoked_by",
                        (ids[slug], row["id"], principal.username))
        if added or removed:
            emit(cur, actor=f"user:{principal.username}", event_type="workflow.agents_changed",
                 entity_type="workflow", entity_id=name, payload={"added": added, "removed": removed})
            _recompose(cur, added + removed)
    return {"name": name, "granted_to": sorted(want), "added": added, "removed": removed}
