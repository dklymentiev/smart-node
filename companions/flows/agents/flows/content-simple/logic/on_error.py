#!/usr/bin/env python3
"""
on_error: a block of this flow failed. Put the task in front of a person.

The flow's global error handler (top-level `on_error:` in the flow YAML). The
engine runs it when any block raises: a hook that exits non-zero, a script that
is missing, a timeout. It is NOT run for a verdict such as FAIL or REJECT: those
are ordinary routes and end in the flow's own blocks.

WHY IT EXISTS. A flow that broke used to leave its task in_progress with nobody
told, or be closed as done by the Runner once the run was over. Neither is true
of a broken run. What a failure does to the task is the flow's own business
: the task is moved to blocked, with the error as the reason, and handed
to the person the flow names for its stopped end (planner.on_block.stopped), if
it names one. The hold a waiting block had put on the task is removed.

A task that is already blocked, rejected or done is left alone: the handler may
run more than once for one failure and must not write twice.

Context (stdin JSON from rein): block_name, error, task_dir, task_id (the
engine's run id, not the planner task), flow_name.
"""
import http.client
import json
import os
import re
import socket
import sys

SOCK = "/run/smart-node/api/core-api.sock"
P = "/api/planner/api/v1/tasks/"
# A task already ended or stopped for a reason of its own is left alone:
# closed, or queued for a person, a dependency, a date or an error (the
# planner's three statuses, 0010). An older planner says it as one word.
STOPPED_FOR = ("person", "dependency", "schedule", "error")
ENDED_WORDS = ("blocked", "rejected", "done")


def ended(task):
    state = task.get("state")
    if state:
        return state == "closed" or (state == "queued" and task.get("wait_reason") in STOPPED_FOR)
    return (task.get("stage") or task.get("status")) in ENDED_WORDS
# The hold a delegated block puts on its task while it waits for an agent.
WAIT_TAG = "blocked:agent-work"


class _UConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def api(method, path, body=None):
    """One planner request, the same door the other hooks of the flow use."""
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


def planner_task_of(task_dir):
    """The planner task this run was started for, from the run's own input."""
    doc = json.load(open(os.path.join(task_dir, "input", "task.json")))
    ti = doc.get("input") or {}
    tid = str(ti.get("task_id") or "").strip()
    if tid:
        return tid
    m = re.search(r"#(\d+)", ti.get("topic") or "")
    if not m:
        raise ValueError("the run names no planner task")
    return m.group(1)


def main():
    ctx = json.load(sys.stdin)
    block = ctx.get("block_name") or "?"
    error = str(ctx.get("error") or "no error text")
    here = os.path.dirname(os.path.abspath(__file__))
    flow_dir = os.path.dirname(here)
    flow = os.path.basename(flow_dir)
    tid = planner_task_of(ctx.get("task_dir") or "")

    st, task = api("GET", P + tid)
    if st != 200:
        raise RuntimeError(f"GET task {tid} -> {st}")
    if ended(task):
        return

    body = {"status": "queued", "wait_reason": "error",
            "comment": f"Flow {flow} stopped: block {block} failed.\n\n{error[:1500]}"}
    try:
        import yaml
        wf = yaml.safe_load(open(os.path.join(flow_dir, flow + ".yaml"))) or {}
        stopped = ((wf.get("planner") or {}).get("on_block") or {}).get("stopped") or {}
        who = (stopped.get("_default") or {}).get("assignee")
        if who and task.get("assignee") != who:
            body["assignee"] = who
    except Exception as exc:
        print(f"ON_ERROR | could not read who takes a stopped flow: {exc}", file=sys.stderr)
    st, resp = api("PATCH", P + tid, body)
    if st not in (200, 204):
        print(f"ON_ERROR REFUSED | {flow}/{block} asked for blocked, planner said {st} {str(resp)[:200]}",
              file=sys.stderr)
        sys.exit(1)
    # The flow is over, so it lets go of the task. A delegated block that was
    # waiting for an agent left its hold on the task; with it still there the
    # task says "stopped, with a person" and "waiting for an agent" at once.
    _, tags = api("GET", P + tid + "/tags")
    tags = [t if isinstance(t, str) else t.get("tag", "") for t in (tags or [])]
    if WAIT_TAG in tags:
        st, resp = api("DELETE", P + tid + "/tags/" + WAIT_TAG)
        if st not in (200, 204):
            print(f"ON_ERROR | {flow}/{block}: the hold {WAIT_TAG} was not removed, "
                  f"planner said {st} {str(resp)[:200]}", file=sys.stderr)
            sys.exit(1)
    print(f"ON_ERROR | {flow}/{block} -> task {tid} blocked", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ON_ERROR FAILED | {exc}", file=sys.stderr)
        sys.exit(1)
