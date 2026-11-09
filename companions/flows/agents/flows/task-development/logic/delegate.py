#!/usr/bin/env python3
"""
delegate: hand one block's work to a real node agent and wait for its verdict.

The flow does not do the work. It guarantees the order of hand-offs, the logic
between them and the planner state; the work is done by a real agent in its own
session: the planner is the record, a hand-off is a reassignment.

State machine, per block and per round:
  no open work order for this block:
    Post a work order comment (first line "[work order] <flow>/<block>"),
    add the tag blocked:agent-work, emit VERDICT: WAITING.
    The order mentions the agent the flow hands the block to (@name), and the
    mention is what tells the agent: it holds when the task is already with
    that agent and no field of the task changes. The Runner skips the task
    while blocked:agent-work is present.
  open work order, and a later comment BY THE TASK'S ASSIGNEE with a
  "VERDICT: X" line:
    Close the order ("[work order closed] ..."), emit VERDICT: X.
    The Runner lifts blocked:agent-work when that comment appears.
  open work order, no verdict yet:
    Re-add blocked:agent-work if it was lifted, emit VERDICT: WAITING.

What the agent is asked to do is the block's order file, orders/<block>.md in
the flow directory. This block never changes the task's status or assignee.
"""
import http.client
import json
import os
import re
import socket
import sys

SOCK = "/run/smart-node/api/core-api.sock"
WAIT_TAG = "blocked:agent-work"
OPEN = "[work order] "
CLOSED = "[work order closed] "
# Authors that are the machinery, never the agent answering the order.
MACHINE = {"", "unknown-peer", "system", "runner", "component:runner"}


class _UConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def api(method, path, body=None):
    """One planner request. The Runner that launched this block carries its own
    planner key (PLANNER_API_URL / PLANNER_API_KEY) and the engine passes it
    down, so the block writes as the runner, directly to the planner. Without
    the key it falls back to the core-api socket, which on 1.12.48 refuses
    planner mutations from a non-agent process (403)."""
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


def must(method, path, body=None):
    """A write this block depends on: a refusal fails the block, loudly."""
    st, resp = api(method, path, body)
    if st not in (200, 201, 204):
        raise RuntimeError(f"{method} {path} -> {st} {str(resp)[:200]}")
    return resp


P = "/api/planner/api/v1/tasks/"


def write_result(output_file, verdict, note):
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w") as fh:
        json.dump({"result": f"{note}\nVERDICT: {verdict}"}, fh, indent=2)


def task_id_of(ti):
    tid = str(ti.get("task_id") or "").strip()
    if tid:
        return tid
    m = re.search(r"#(\d+)", ti.get("topic") or "")
    if not m:
        raise ValueError("no task id in task_id or topic")
    return m.group(1)


def last_verdict(text):
    v = None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.upper().startswith("VERDICT:"):
            v = s.split(":", 1)[1].strip()
    return v or None


def addressee(flow_dir, flow, block):
    """The agent this block hands the task to while it waits: what the flow
    declares in planner.on_block.<block>.waiting. None when it declares none."""
    try:
        import yaml
        wf = yaml.safe_load(open(os.path.join(flow_dir, flow + ".yaml"))) or {}
        rules = ((wf.get("planner") or {}).get("on_block") or {}).get(block) or {}
        return ((rules.get("waiting") or {}).get("assignee") or "").strip() or None
    except Exception:
        return None


def main():
    ctx = json.load(sys.stdin)
    out = ctx.get("output_file", "/tmp/delegate-result.json")
    block = ((ctx.get("block_config") or {}).get("name")) or ctx.get("block_name") or "?"
    ti = ctx.get("task_input") or {}
    here = os.path.dirname(os.path.abspath(__file__))
    flow = os.path.basename(os.path.dirname(here))
    try:
        tid = task_id_of(ti)
    except ValueError as exc:
        write_result(out, "FAIL", f"delegate {block}: {exc}")
        return

    st, task = api("GET", P + tid)
    if st != 200:
        write_result(out, "FAIL", f"delegate {block}: GET task {tid} -> {st}")
        return
    _, comments = api("GET", P + tid + "/comments")
    comments = comments if isinstance(comments, list) else (comments or {}).get("comments", [])
    _, tags = api("GET", P + tid + "/tags")
    tags = [t if isinstance(t, str) else t.get("tag", "") for t in (tags or [])]

    head = OPEN + f"{flow}/{block}"
    closed_head = CLOSED + f"{flow}/{block}"
    # The latest order for this block, and whether it was closed after it.
    idx = None
    for i, c in enumerate(comments):
        first = ((c.get("content") or "").strip().split("\n", 1)[0]).strip()
        if first == head:
            idx = i
        elif first.startswith(closed_head) and idx is not None and i > idx:
            idx = None
    assignee = (task.get("assignee") or "").strip()

    if idx is not None:
        for c in comments[idx + 1:]:
            author = (c.get("author") or "").strip()
            if author in MACHINE or (assignee and author != assignee):
                continue
            v = last_verdict(c.get("content"))
            if v:
                must("POST", P + tid + "/comments",
                     {"content": f"{closed_head}: VERDICT {v.upper()} by {author}"})
                if WAIT_TAG in tags:
                    api("DELETE", P + tid + "/tags/" + WAIT_TAG)
                write_result(out, v.upper(), f"{block}: {author} reported {v}")
                return
        if WAIT_TAG not in tags:
            must("POST", P + tid + "/tags", {"tag": WAIT_TAG})
        write_result(out, "WAITING", f"{block}: waiting for {assignee or 'the agent'}")
        return

    order_path = os.path.join(os.path.dirname(here), "orders", f"{block}.md")
    try:
        order = open(order_path).read().strip()
    except OSError:
        write_result(out, "FAIL", f"delegate {block}: no order file {order_path}")
        return
    order = order.replace("{task_id}", tid)
    # Tags the flow declares stale when this block starts a new round
    # (orders/<block>.clear, one per line): e.g. plan clears the gate's arming
    # and the previous decision, so the next gate reads a fresh answer.
    try:
        for tag in open(order_path[:-3] + ".clear").read().split():
            if tag in tags:
                api("DELETE", P + tid + "/tags/" + tag)
    except OSError:
        pass
    # The order says whom it is for. An agent still inside its turn reads the
    # task again and sees the next order; without a name on it, it took the
    # next agent's work for its own (seen on a live node, 2026-10-10).
    # The first line stays exactly as it was: it is what closes and finds orders.
    #
    # The name is written as a mention. The planner tells a mentioned actor
    # about the comment, so the order reaches its agent by itself. Before, an
    # agent was told only when the task's assignee changed: two blocks in a
    # row for the same agent changed nothing, and the second order woke nobody
    # (the three-block run on one agent, 2026-10-10).
    who = addressee(os.path.dirname(here), flow, block)
    to = (f"@{who}, this work order is for you. If you are not {who}, do not answer it: "
          f"it is not yours.\n\n") if who else ""
    must("POST", P + tid + "/comments", {"content": f"{head}\n\n{to}{order}"})
    must("POST", P + tid + "/tags", {"tag": WAIT_TAG})
    write_result(out, "WAITING", f"{block}: work order posted")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:          # a refused write is a failed block, never a silent one
        print(f"DELEGATE ERROR | {exc}", file=sys.stderr)
        sys.exit(1)
