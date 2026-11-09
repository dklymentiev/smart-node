#!/usr/bin/env python3
"""flow_sweep.py -- enforce the time budgets of flow runs, and say so.

WHY THIS EXISTS. A flow run on a node is stepped by the Runner and lives
nowhere between two steps. A block that waits for an agent that never answers
therefore waits for good: on 2026-10-09 a task stood eight hours that way. The
engine can hold a time budget for a block and for a run, but a budget is only
data until something looks at the clock. This is that something. It runs once a
minute from smart-node-flow-sweep.timer.

It is its own process and not a pass of the Runner on purpose: the Runner wakes
agents every few seconds in one loop, and a walk over every run directory inside
that loop would hold every wake for as long as the walk takes.

It only LAUNCHES the engine's own check (rein.sweep). What an expired budget
does to a planner task is the flow's business: the engine runs the flow's
on_error, and that handler moves the task. Nothing here writes a task.

WHAT IS RECORDED, in sn_core.events through core-api, actor component:flow-sweep:
    flow.sweep_started          every run
    flow.time_budget_exceeded   one per run directory that went over its budget
    flow.sweep_failed           a run directory that could not be checked, or
                                the sweep itself failing
    flow.sweep_finished         every run, with the counts and the seconds it took

Environment (the Runner's own, from the same files, so the engine the sweep
brings up can reach the planner exactly as the engine the Runner steps does):
    SN_CORE_API_URL             where events go
    SN_SERVICE_AUTH_SECRET      proof for that call
    SN_AUTHORED_AGENTS, SN_RUNNER_AGENTS    the two trees that hold runs
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.request

CORE = (os.environ.get("SN_CORE_API_URL") or os.environ.get("CORE_API_URL") or "").rstrip("/")
ACTOR = "component:flow-sweep"
ROOTS = [os.environ.get("SN_AUTHORED_AGENTS", "/var/lib/smart-node/workflows"),
         os.environ.get("SN_RUNNER_AGENTS", "/opt/smart-node/flows/runner-agents")]

_unrecorded = []
_ELAPSED_RE = re.compile(r"elapsed=(\d+)s > budget=(\d+)s")


def emit(event_type, entity_type, entity_id, payload):
    """One event. A failure to record is kept and fails the run at the end: a
    sweep nobody can see is the thing this file exists to prevent."""
    try:
        secret = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()
        if not CORE or not secret:
            raise RuntimeError("SN_CORE_API_URL or SN_SERVICE_AUTH_SECRET is not set")
        body = json.dumps({"actor": ACTOR, "event_type": event_type, "entity_type": entity_type,
                           "entity_id": str(entity_id), "payload": payload}).encode()
        req = urllib.request.Request(CORE + "/api/events", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-SN-Service": "brin", "X-SN-Service-Auth": secret})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError("core-api answered %s" % resp.status)
    except Exception as exc:
        _unrecorded.append("%s: %s" % (event_type, exc))
        print("[flow-sweep] event %s not recorded: %s" % (event_type, exc), file=sys.stderr)


def run_facts(task_dir, since):
    """What a run directory says about itself: the flow, the planner task it was
    started for, and what ran out of time at or after `since`: a block, or
    the whole run."""
    facts = {"run": os.path.basename(task_dir)}
    try:
        doc = json.load(open(os.path.join(task_dir, "input", "task.json"))) or {}
        facts["flow"] = doc.get("flow")
        task_id = str((doc.get("input") or {}).get("task_id") or "").strip()
        if task_id:
            facts["task_id"] = int(task_id) if task_id.isdigit() else task_id
    except (OSError, ValueError):
        pass
    # Which budget ran out is read from the run's own log: the engine clears
    # a block's budget record once the failure is routed, so its state no
    # longer names the block. Lines look like
    #   <iso time> | TIME BUDGET EXCEEDED | block=wait | elapsed=148s > budget=120s
    #   <iso time> | TIME BUDGET EXCEEDED | run | elapsed=665s > budget=600s
    over = []
    try:
        for line in open(os.path.join(task_dir, "state", "rein.log"), errors="replace"):
            if "TIME BUDGET EXCEEDED" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            try:
                at = datetime.datetime.fromisoformat(parts[0]).timestamp()
            except ValueError:
                continue
            if at < since - 1:
                continue              # an earlier sweep already reported it
            what = parts[2] if len(parts) > 2 else ""
            m = _ELAPSED_RE.search(line)
            over.append({"scope": "run" if what == "run" else "block",
                         "block": what.split("=", 1)[1] if what.startswith("block=") else None,
                         "seconds_elapsed": int(m.group(1)) if m else None,
                         "seconds_budget": int(m.group(2)) if m else None})
    except OSError:
        pass
    facts["over"] = over
    return facts


def main():
    began = time.time()
    seen, roots = set(), []
    for root in ROOTS:
        real = os.path.realpath(root)
        if real not in seen and os.path.isdir(os.path.join(real, "tasks")):
            seen.add(real)
            roots.append(root)
    emit("flow.sweep_started", "node", "flow-sweep", {"roots": roots})

    counts = {"checked": 0, "expired": 0, "busy": 0, "errors": 0}
    try:
        from rein.sweep import sweep
        for root in roots:
            said = []
            result = sweep(root, log=said.append)
            counts["checked"] += sum(len(v) for v in result.values())
            for key in ("expired", "busy", "errors"):
                counts[key] += len(result[key])
            for name in result["expired"]:
                facts = run_facts(os.path.join(root, "tasks", name), began)
                emit("flow.time_budget_exceeded", "workflow", facts.get("flow") or name, facts)
                print("[flow-sweep] %s: over its time budget (%s)" % (name, facts.get("flow")))
            for name in result["errors"]:
                why = next((line for line in said if name in line and "error" in line), "")
                emit("flow.sweep_failed", "workflow", name,
                     {"run": name, "root": root, "error": why[-600:]})
                print("[flow-sweep] %s: could not be checked: %s" % (name, why), file=sys.stderr)
    except Exception as exc:
        emit("flow.sweep_failed", "node", "flow-sweep",
             {"error": "%s: %s" % (type(exc).__name__, exc), "counts": counts})
        print("[flow-sweep] failed: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1

    emit("flow.sweep_finished", "node", "flow-sweep",
         {**counts, "seconds": round(time.time() - began, 2), "roots": roots})
    print("[flow-sweep] checked=%(checked)d expired=%(expired)d busy=%(busy)d errors=%(errors)d" % counts)
    return 1 if (counts["errors"] or _unrecorded) else 0


if __name__ == "__main__":
    sys.exit(main())
