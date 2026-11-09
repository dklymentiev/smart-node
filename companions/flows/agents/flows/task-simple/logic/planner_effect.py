#!/usr/bin/env python3
"""
planner_effect: write the hand-off this block's verdict calls for.

A post hook (logic.post). It runs after the block, inside the flow, and is the
only thing that writes the task's status and assignee on the flow's behalf.

WHY IT IS HERE AND NOT IN THE RUNNER. The Runner launches: agents, and one step
of a flow at a time. What a block's verdict does to the task is the flow's own
logic and lives with the flow. For two weeks the Runner
applied the flow's planner.on_block map itself, with its own record of what it
had "already applied", keyed on the start time of a block run. When a waiting
block ran again every tick, each run was a new key and the hand-off was written
again every tick: 5560 wake requests in three hours on 2026-10-09.

What it does:
  reads planner.on_block.<this block> from the flow's own YAML;
  picks the effect for the verdict the block just emitted, `_default` otherwise;
  keeps only status, wait_reason, close_outcome and assignee (planner 0010:
  `status: closed` goes with a `close_outcome`, `status: queued` may carry a
  `wait_reason`; the old words `done`, `blocked`, `rejected` still work);
  reads the task and writes ONLY what differs.

Reading the task first is what makes a repeat harmless: a block that runs again
finds the task already where the flow wants it and writes nothing. There is no
record beside the run to drift from the task; the task is the record.

Context (stdin JSON from rein LogicRunner): output_file, task_input,
block_config. Uses stdlib only, plus PyYAML to read the flow file.
"""
import http.client
import json
import os
import re
import socket
import sys

SOCK = "/run/smart-node/api/core-api.sock"
P = "/api/planner/api/v1/tasks/"
FIELDS = ("status", "wait_reason", "close_outcome", "assignee")
STATES = ("queued", "in_progress", "closed")


def same(task, key, value):
    """Is the task already where this field wants it? `status` is compared in
    the vocabulary the flow used: a state against the task's `state`, an old
    word against its `stage` (the wire `status` may be either since 0010)."""
    if key != "status":
        return task.get(key) == value
    if value in STATES:
        return task.get("state") == value
    return (task.get("stage") or task.get("status")) == value


class _UConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def api(method, path, body=None):
    """One planner request, the same door delegate.py uses: the Runner's own
    planner key when the engine passed it down, the core-api socket otherwise."""
    key = os.environ.get("PLANNER_API_KEY", "")
    base = os.environ.get("PLANNER_API_URL", "")
    data = json.dumps(body) if body is not None else None
    hdr = {"Content-Type": "application/json"}
    if key and base.startswith("http://"):
        host, _, port = base[len("http://"):].rstrip("/").partition(":")
        c = http.client.HTTPConnection(host, int(port or 80), timeout=30)
        hdr["X-API-Key"] = key
        c.request(method, path.replace("/api/planner", "", 1), body=data, headers=hdr)
    else:
        c = _UConn(SOCK)
        c.request(method, path, body=data, headers=hdr)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw) if raw else {}
    except ValueError:
        return r.status, {}


def task_id_of(ti):
    tid = str(ti.get("task_id") or "").strip()
    if tid:
        return tid
    m = re.search(r"#(\d+)", ti.get("topic") or "")
    if not m:
        raise ValueError("no task id in task_id or topic")
    return m.group(1)


def verdict_signals(output_file):
    """The signals the block produced. Mirrors rein/routing_engine.py
    extract_verdict_signals(), its two legacy aliases included: hooks run under
    the system python and cannot import the engine."""
    try:
        data = json.load(open(output_file))
    except Exception:
        return set()
    inner = data.get("result", data.get("response", ""))
    text = inner if isinstance(inner, str) else json.dumps(inner)
    out = set()
    for line in text.upper().split("\n"):
        line = line.strip()
        if not line.startswith("VERDICT:"):
            continue
        verdict = line.split(":", 1)[1].strip()
        if not verdict:
            continue
        if verdict in ("PASS", "APPROVED"):
            out.add("needs-review")
        elif verdict == "REVISE":
            out.add("revise")
        out.add(verdict.lower())
    return out


def effect_for(rules, signals):
    """The fields this verdict asks for: the first signal the block's rules
    name, `_default` otherwise. Only the FIELDS are ever written."""
    effect = None
    for sig in sorted(signals):
        if sig in rules:
            effect = rules[sig]
            break
    if effect is None:
        effect = rules.get("_default")
    if not isinstance(effect, dict):
        return {}
    return {k: effect[k] for k in FIELDS if k in effect}


def main():
    ctx = json.load(sys.stdin)
    block = ((ctx.get("block_config") or {}).get("name")) or ctx.get("block_name") or "?"
    here = os.path.dirname(os.path.abspath(__file__))
    flow_dir = os.path.dirname(here)
    flow = os.path.basename(flow_dir)

    import yaml
    wf = yaml.safe_load(open(os.path.join(flow_dir, flow + ".yaml"))) or {}
    rules = ((wf.get("planner") or {}).get("on_block") or {}).get(block) or {}
    if not isinstance(rules, dict) or not rules:
        return
    want = effect_for(rules, verdict_signals(ctx.get("output_file") or ""))
    if not want:
        return

    tid = task_id_of(ctx.get("task_input") or {})
    st, task = api("GET", P + tid)
    if st != 200:
        raise RuntimeError(f"GET task {tid} -> {st}")
    body = {k: v for k, v in want.items() if not same(task, k, v)}
    if not body:
        return                      # the task is already where the flow wants it
    st, resp = api("PATCH", P + tid, body)
    if st == 400 and "status" in body and "comment is required" in str(resp):
        # Some moves the planner takes only with a reason (blocked, rejected,
        # done). Which ones is the planner's rule, so it is asked, not copied
        # here: on that answer the same move is sent again with the reason.
        verdict = ", ".join(sorted(s.upper() for s in verdict_signals(ctx.get("output_file") or ""))) or "no verdict"
        st, resp = api("PATCH", P + tid, {**body, "comment": f"Flow {flow}, block {block}: {verdict}."})
    if st not in (200, 204):
        # The planner has its own state machine and may refuse a move. That is
        # a defect in the flow's map, and it is said where the flow's author
        # reads: the block's log. The flow carries on, as it always has.
        print(f"PLANNER EFFECT REFUSED | {flow}/{block} asked for {body}, "
              f"planner said {st} {str(resp)[:200]}", file=sys.stderr)
        return
    print(f"PLANNER EFFECT | {flow}/{block} -> task {tid} {body}", file=sys.stderr)


if __name__ == "__main__":
    # A hand-off that could not be written is said in the block's log and does
    # not fail the block: that is how it behaved while the Runner wrote it, and
    # a hot move of the write is not the place to change what a failure does.
    try:
        main()
    except Exception as exc:
        print(f"PLANNER EFFECT ERROR | {exc}", file=sys.stderr)
