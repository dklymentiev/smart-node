#!/usr/bin/env bash
#
# What the Runner does with a flow task, apart from stepping the engine.
#
# WHY THIS EXISTS. The Runner launches. It decides which flow tasks to step,
# closes a finished run when the planner lets it, lifts a gate's hold on the
# owner's answer, and finds a flow in one of two trees. It does NOT write a
# task's status or assignee on a flow's behalf: that is the flow's own hook,
# and those properties are in tests/flow-planner-effect.test.sh. The
# file keeps its name so the history of these checks stays in one place.
#
# THE PROPERTIES (numbered as they always were; 1-6 and 8 moved out):
#   7. a run in flight is driven even after an agent handed the task to
#      review; a task in review never starts a run, nor one queued for approval
#   9. a finished run is recorded and its task is not moved: the flow ends its
#      own task; one the flow left in progress gets a comment, not a close
#  10. the owner's decision lifts the hold of any flow's gate, and only the
#      owner's, and only one written after the gate asked
#  11. a flow the node made itself, under its own tree, is found and run from
#      there; a flow that is not where the catalog says it is, is not run
#  12. a flow the owner switched off starts no new run; a run in flight
#      finishes; switched on again, the waiting task starts
#
#   tests/runner-planner-map.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RUNNER="$REPO/companions/brin/runner.py"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "runner: which flow tasks it drives and how it ends a run"

[[ -r "$RUNNER" ]] || { bad "runner.py is readable"; echo; echo "FAIL"; exit 1; }

# The behaviour, driven directly. runner.py imports httpx and yaml at
#         module level and neither is needed for the parts under test, so httpx
#         is stubbed; yaml is what reads the map, so it is real.
python3 - "$RUNNER" <<'PY'
import json, os, sys, types, tempfile, importlib.util

path = sys.argv[1]
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
# runner.py reads the turn ceiling through core.turn_budget, which sits beside it
# in the tree and on a node. Loaded by file path, the runner has no such
# neighbour unless its own directory is on the path.
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
# runner.py refuses to import without the address of the node it drives, which is
# right: a Runner with a relative URL dies every five seconds while the node looks
# healthy. Nothing under test here makes a request, so any address will do.
os.environ.setdefault("SN_CORE_API_URL", "http://127.0.0.1:8030")
os.environ.setdefault("AI_GATEWAY_URL", "http://127.0.0.1:8000")
os.environ.setdefault("PLANNER_API_URL", "http://127.0.0.1:8030/api/planner")
spec = importlib.util.spec_from_file_location("runner_under_test", path)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except Exception as exc:
    print("  FAIL runner.py imports for testing: %s" % exc)
    sys.exit(1)

fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

root = tempfile.mkdtemp()
flows = os.path.join(root, "flows", "demo")
os.makedirs(flows)
open(os.path.join(flows, "demo.yaml"), "w").write("""
schema_version: "3.3.0"
name: demo
team: team-demo
planner:
  on_block:
    execute:
      _default: {assignee: tester, status: queued, wait_reason: review, priority: high}
    verify:
      revise: {assignee: developer}
      _default: {assignee: pm}
blocks:
  - name: execute
    routing: {_default: verify}
  - name: verify
    routing: {_default: _stop}
""")
mod.RUNNER_AGENTS = root

import asyncio

# 7. which tasks the Runner drives. A run in flight is driven even after an agent
#    handed the task to review; review never STARTS a run, and a task queued for
#    approval is the owner's, not the Runner's. The rows are the planner's
#    answers in its three statuses (0010).
def T(i, wid, status, title, wait=None):
    return {"id": i, "workflow_id": wid, "status": status, "state": status,
            "wait_reason": wait, "title": title}
store = {
    31: (T(31, 9, "queued", "fresh"), []),
    32: (T(32, 9, "in_progress", "in flight"), ["reindir:task-a"]),
    33: (T(33, 9, "queued", "moved by the agent", "review"), ["reindir:task-b"]),
    34: (T(34, 9, "queued", "never started", "review"), []),
    35: (T(35, 9, "queued", "waiting for its agent", "review"), ["reindir:task-c", "blocked:agent-work"]),
    36: (T(36, 9, "queued", "finished", "review"), ["reindir:task-d", "flow-done:demo"]),
    37: (T(37, None, "queued", "not a flow task", "review"), []),
    38: (T(38, 9, "queued", "filed by an agent", "approval"), []),
    39: (T(39, 9, "queued", "stopped for a person", "person"), ["reindir:task-e"]),
}
class R:
    def __init__(self, body, code=200):
        self._b, self.status_code, self.text = body, code, ""
    def json(self):
        return self._b
def _listed(path, rows):
    q = dict(x.split("=", 1) for x in path.split("?", 1)[1].split("&"))
    return [t for t in rows if t["status"] == q.get("status_filter")
            and ("wait_reason" not in q or t.get("wait_reason") == q["wait_reason"])]
async def listing_planner(method, path, **kw):
    if "status_filter=" in path:
        return R(_listed(path, [t for t, _ in store.values()]))
    if path.endswith("/tags"):
        return R(store[int(path.split("/")[-2])][1])
    return R({}, 404)
async def fake_wf_name(wid):
    return "demo"
mod._planner = listing_planner
mod._wf_name = fake_wf_name
driven = {t["id"] for t in asyncio.run(mod._flow_tasks())}
check(31 in driven, "a new flow task nobody took starts")
check(32 in driven, "a run in flight is driven")
check(33 in driven, "a run in flight is still driven after an agent handed the task to review")
check(34 not in driven, "a task in review does not start a run")
check(38 not in driven, "a task queued for approval does not start a run")
check(39 not in driven, "a run stopped for a person is not driven")
check(35 not in driven, "a task held for its agent is not driven, whatever its status")
check(36 not in driven, "a finished flow is not driven again")
check(37 not in driven, "a task that belongs to no flow is never driven")

# 9. a finished run and the task's reviewer. The planner lets only the reviewer
#    close a task that has one; the Runner is not the reviewer. The shapes below
#    are the planner's own: GET /tasks/N carries reviewer and status, a refused
#    close is a 400 whose text names the reviewer.
tags61 = ["reindir:task-x"]
async def held_planner(method, path, **kw):
    if "status_filter=" in path:
        return R(_listed(path, [{"id": 61, "workflow_id": 5, "status": "queued", "state": "queued",
                                 "wait_reason": "review", "title": "t", "assignee": "dana"}]))
    if path.endswith("/tags"):
        return R(list(tags61))
    return R({})
async def any_flow(_):
    return "demo"
mod._wf_name = any_flow
mod._planner = held_planner
check([x["id"] for x in asyncio.run(mod._flow_tasks())] == [61],
      "a run in flight in review is driven")
tags61.append("flow-review:demo")
check(asyncio.run(mod._flow_tasks()) == [],
      "a task handed to its reviewer is not driven again")

def finish(task):
    calls, evs = [], []
    async def p(method, path, **kw):
        calls.append((method, path, kw.get("json") or {}))
        return R(task) if method == "GET" else R({})
    async def ev(tid, flow, etype, blocks=None):
        evs.append(etype)
    mod._planner, mod._emit_workflow = p, ev
    asyncio.run(mod._finish_flow(51, "demo", {}))
    moves = [b for m, _, b in calls if m == "PATCH"]
    tags = [b.get("tag") for m, path, b in calls if m == "POST" and path.endswith("/tags")]
    notes = [b.get("content") for m, path, b in calls if m == "POST" and path.endswith("/comments")]
    return moves, tags, notes, evs

# The Runner does not end a task: the flow's last block does, and its on_error.
# When the run is over the Runner records that, and nothing else.
mv, tg, nt, ev_ = finish({"id": 51, "status": "queued", "state": "queued", "wait_reason": "review",
                          "reviewer": "admin"})
check(mv == [] and tg == ["flow-review:demo"] and ev_ == ["workflow.awaiting_review"] and nt == [],
      "a task the flow handed to a person is tagged flow-review and not touched")

mv, tg, nt, ev_ = finish({"id": 51, "status": "closed", "state": "closed", "close_outcome": "completed",
                          "reviewer": None})
check(mv == [] and tg == ["flow-done:demo"] and ev_ == ["workflow.completed"] and nt == [],
      "a task the flow closed is tagged flow-done and not touched")

mv, tg, nt, ev_ = finish({"id": 51, "status": "queued", "state": "queued", "wait_reason": "error",
                          "reviewer": None})
check(mv == [] and tg == ["flow-done:demo"] and nt == [],
      "a task the flow stopped (queued for an error) is not moved and not reported on")

mv, tg, nt, ev_ = finish({"id": 51, "status": "pending_review", "reviewer": "admin"})
check(mv == [] and tg == ["flow-review:demo"] and ev_ == ["workflow.awaiting_review"],
      "an older planner's pending_review is read as review")

mv, tg, nt, ev_ = finish({"id": 51, "status": "in_progress", "state": "in_progress", "reviewer": None})
check(mv == [], "a task the flow left in progress is NOT closed by the Runner")
check(len(nt) == 1 and "in_progress" in nt[0] and tg == ["flow-done:demo"],
      "it gets one comment saying the flow declared no end, and is not driven again")

# 10. the owner's answer at a gate. A gate holds its task with blocked:approval
#     and asks in a comment; the owner answers with "DECISION: ..." and the
#     Runner lifts the hold. The request is recognised by its first line, which
#     every gate ends with "approval gate.", whatever the flow calls the thing
#     being approved.
def gate(comments, tags=("blocked:approval", "reindir:t")):
    lifted = []
    async def p(method, path, **kw):
        if "status_filter=in_progress" in path:
            return R([{"id": 71, "workflow_id": 5, "status": "in_progress", "state": "in_progress"}])
        if method == "GET" and path.endswith("/tags"):
            return R(list(tags))
        if method == "GET" and path.endswith("/comments"):
            return R(comments)
        if method == "DELETE":
            lifted.append(path)
        return R({})
    mod._planner = p
    asyncio.run(mod.gate_decision_tick())
    return lifted

ask_plan = {"author": "runner", "content": "Plan-approval gate.\n\nThe plan is in the preceding PLAN: comment."}
ask_material = {"author": "runner", "content": "Material-approval gate.\n\nThe material is named above."}
yes = {"author": mod.APPROVAL_OWNER, "content": "DECISION: approve"}
check(gate([ask_plan, yes]) == ["/api/v1/tasks/71/tags/blocked:approval"],
      "the owner's decision lifts the hold of a plan gate")
check(gate([ask_material, yes]) == ["/api/v1/tasks/71/tags/blocked:approval"],
      "and of a gate that approves something other than a plan")
check(gate([ask_material, {"author": "dana", "content": "DECISION: approve"}]) == [],
      "a decision written by anyone but the owner lifts nothing")
check(gate([yes, ask_material]) == [], "a decision written before the gate asked lifts nothing")
check(gate([ask_material, {"author": mod.APPROVAL_OWNER, "content": "looks fine to me"}]) == [],
      "an owner's comment that records no decision lifts nothing")
check(gate([{"author": "runner", "content": "The plan is ready."}, yes]) == [],
      "without a gate request there is nothing to answer")

# 11. two trees. Shipped flows live in the Runner's agents directory; a node's
#     own flows live in another, whose flows/ is the authored tree. The engine
#     resolves a flow only as <agents-dir>/flows/<name>/<name>.yaml, so the
#     Runner has to hand it the directory that holds the flow.
shipped, authored = tempfile.mkdtemp(), tempfile.mkdtemp()
def put(root, name):
    d = os.path.join(root, "flows", name)
    os.makedirs(d)
    p = os.path.join(d, name + ".yaml")
    open(p, "w").write("name: %s\nplanner:\n  on_block:\n    a:\n      _default: {assignee: dana}\n" % name)
    return p
put(shipped, "task-simple")
own = put(authored, "contract-signing")
mod.RUNNER_AGENTS, mod.AUTHORED_AGENTS = shipped, authored
mod._wf_paths.clear()
check(mod._flow_home("task-simple") == shipped, "a shipped flow is found in the shipped tree")
check(mod._flow_home("contract-signing") == authored, "a flow the node made is found in its own tree")
check(mod._flow_home("no-such-flow") is None, "a flow in neither tree is not found")
check(mod._flow_file("contract-signing") == own, "and its file is read from there")
mod._wf_paths["contract-signing"] = own
check(mod._flow_home("contract-signing") == authored, "a flow that is where the catalog says is run")
mod._wf_paths["contract-signing"] = "/somewhere/else/contract-signing/contract-signing.yaml"
check(mod._flow_home("contract-signing") is None,
      "a flow that is not where the catalog says it is, is not run")
mod._wf_paths.clear()

# The drive itself: the engine is called with the tree that holds the flow, and
# the run directory is under that tree.
calls = []
async def fake_rein(args):
    calls.append(args)
    return "Directory: %s/tasks/task-1\n" % args[args.index("--agents-dir") + 1]
async def quiet_planner(method, path, **kw):
    return R([] if path.endswith("/tags") else {})
async def no_event(*a, **kw):
    return None
async def no_grounding(flow):
    return {}
mod._rein, mod._planner, mod._emit_workflow = fake_rein, quiet_planner, no_event
mod._flow_grounding = no_grounding
mod._rein_state = lambda full: ("running", {})
mod._flow_inflight.clear()
asyncio.run(mod._drive_flow({"id": 81, "flow": "contract-signing", "reindir": None, "topic": "t",
                             "status": "queued", "assignee": None}))
check(len(calls) == 1 and calls[0][calls[0].index("--agents-dir") + 1] == authored,
      "the engine is started with the tree that holds the flow")
os.makedirs(os.path.join(authored, "tasks", "task-1"))
calls.clear()
asyncio.run(mod._drive_flow({"id": 81, "flow": "contract-signing", "reindir": "task-1", "topic": "t",
                             "status": "in_progress", "assignee": None}))
check(len(calls) == 1 and calls[0][calls[0].index("--task-dir") + 1] == os.path.join(authored, "tasks", "task-1"),
      "a run in flight is stepped in that tree's run directory")
parked = []
async def tag_planner(method, path, **kw):
    if method == "POST" and path.endswith("/tags"):
        parked.append((kw.get("json") or {}).get("tag"))
    return R([] if path.endswith("/tags") else {})
mod._planner = tag_planner
calls.clear()
asyncio.run(mod._drive_flow({"id": 82, "flow": "no-such-flow", "reindir": None, "topic": "t",
                             "status": "queued", "assignee": None}))
check(calls == [] and parked == ["blocked:error"],
      "a task bound to a flow that is in neither tree is parked, and the engine is not started")

# 12. off on the panel: no new run, the one in flight goes on.
mod._planner, mod._wf_name = listing_planner, fake_wf_name
mod._wf_status.clear()
mod._wf_status[9] = "off"
driven = {t["id"] for t in asyncio.run(mod._flow_tasks())}
check(31 not in driven, "a flow that is off starts no new run")
check(32 in driven and 33 in driven, "a run in flight of a flow that is off finishes")
mod._wf_status[9] = "available"
check(31 in {t["id"] for t in asyncio.run(mod._flow_tasks())}, "switched on again, the waiting task starts")
mod._wf_status.clear()

sys.exit(1 if fails else 0)
PY
[[ $? -eq 0 ]] || FAILED=$((FAILED + 1))

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
