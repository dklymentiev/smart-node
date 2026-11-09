#!/usr/bin/env bash
#
# The flow writes its own hand-offs; the Runner writes none.
#
# WHY THIS EXISTS. The Runner launches: agents, and one step of a flow at a time.
# What a block's verdict does to the planner task is the flow's logic and lives
# in the flow. For two weeks the Runner applied the flow's
# planner.on_block map itself and kept its own record of what it had applied,
# keyed on the start time of a block run. On 2026-10-09 a waiting block ran again
# every tick, each run was a new key, and the Runner wrote the same hand-off
# every few seconds for three hours: 5560 wake requests.
#
# The hand-off is now written by the flow's post hook logic/planner_effect.py,
# which reads the task first and writes only what differs.
#
# THE PROPERTIES:
#   1. the hook writes the effect the block's verdict selects, _default otherwise
#   2. only status and assignee are written, whatever else the map says
#   3. only what differs from the task is written
#   4. a task already where the flow wants it gets no write at all, however many
#      times the block runs
#   5. a block the map does not name, or names with an empty effect, writes nothing
#   6. a refused write is said in the block's log and does not fail the block
#   6b. a move the planner takes only with a reason is sent again with the reason
#   6c. a block that fails: the flow's on_error blocks the task and hands it to a person
#   7. every shipped flow that declares effects carries the hook on each such block,
#      has an error handler, and says at every end what becomes of the task
#   8. driving a step, the Runner writes neither status nor assignee
#   9. starting a flow, the Runner ties the task to its run and does not move it
#
#   tests/flow-planner-effect.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
RUNNER="$REPO/companions/brin/runner.py"
FLOWS="$REPO/companions/flows/agents/flows"
HOOK="$FLOWS/task-development/logic/planner_effect.py"

FAILED=0
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "flow hand-offs: written by the flow's hook, never by the Runner"

# 1-7. The hook, run as the engine runs it: a subprocess fed the block context on
#      stdin. The planner is a recorder on a local port.
python3 - "$HOOK" "$FLOWS" <<'PY' || FAILED=$((FAILED + 1))
import hashlib, json, os, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import yaml

hook_src, flows_dir = sys.argv[1], sys.argv[2]
fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

if not os.path.exists(hook_src):
    print("  FAIL the flow carries logic/planner_effect.py")
    sys.exit(1)

task = {}
seen = []
refuse = {"on": False}
# Since planner 0010 a task on the wire carries its stage (the old word) and its
# state (queued, in_progress, closed) beside status; the hook compares a state
# word with `state` and an old word with `stage`. The fake answers the same way.
_STATE_OF = {"open": "queued", "awaiting_approval": "queued", "blocked": "queued",
             "escalated": "queued", "in_progress": "in_progress",
             "pending_review": "in_progress", "done": "closed", "rejected": "closed",
             "queued": "queued", "closed": "closed"}
def wire(t):
    out = dict(t)
    st = out.get("status")
    if st is not None:
        out.setdefault("stage", st if st not in ("queued", "closed") else st)
        out.setdefault("state", _STATE_OF.get(st, st))
    return out
class Planner(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        seen.append(("GET", self.path, None))
        self._send(200, wire(task))
    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        seen.append(("PATCH", self.path, body))
        if refuse["on"]:
            return self._send(400, {"detail": "invalid transition"})
        task.update(body)
        if "status" in body:
            task.pop("stage", None); task.pop("state", None)
        self._send(200, wire(task))
srv = HTTPServer(("127.0.0.1", 0), Planner)
threading.Thread(target=srv.serve_forever, daemon=True).start()

root = tempfile.mkdtemp()
flow = os.path.join(root, "flows", "demo")
os.makedirs(os.path.join(flow, "logic"))
shutil.copy(hook_src, os.path.join(flow, "logic", "planner_effect.py"))
open(os.path.join(flow, "demo.yaml"), "w").write("""
schema_version: "3.3.0"
name: demo
team: team-demo
planner:
  on_block:
    execute:
      waiting:  {assignee: dev, status: in_progress, priority: high}
      revise:   {assignee: dev}
      _default: {assignee: tester, status: pending_review}
    gate:
      skipped:  {}
      _default: {}
blocks:
  - name: execute
    routing: {_default: gate}
  - name: gate
    routing: {_default: _stop}
  - name: plain
    routing: {_default: _stop}
""")
env = {**os.environ, "PLANNER_API_URL": "http://127.0.0.1:%d" % srv.server_port, "PLANNER_API_KEY": "k"}

def run(block, text, tid="7"):
    out = os.path.join(root, "%s-result.json" % block)
    json.dump({"result": text}, open(out, "w"))
    seen.clear()
    p = subprocess.run(["python3", os.path.join(flow, "logic", "planner_effect.py")],
                       input=json.dumps({"output_file": out, "task_input": {"task_id": tid},
                                         "block_config": {"name": block}}),
                       env=env, capture_output=True, text=True)
    return p
patches = lambda: [s[2] for s in seen if s[0] == "PATCH"]

# 1, 2. selection by verdict, and field filtering
task.clear(); task.update({"id": 7, "status": "open", "assignee": "pm"})
p = run("execute", "order posted\nVERDICT: WAITING")
check(patches() == [{"assignee": "dev", "status": "in_progress"}],
      "the effect the block's verdict selects is written, and only status and assignee")
check(all(s[1].endswith("/tasks/7") for s in seen), "it is written to the flow's own task")

# 4. the same block again: the task is already there
p = run("execute", "still waiting\nVERDICT: WAITING")
check(patches() == [], "a block that runs again finds the task in place and writes nothing")
for _ in range(20):
    run("execute", "still waiting\nVERDICT: WAITING")
    if patches():
        break
check(patches() == [], "and that holds however many times it runs")

# 3. only what differs
task.update({"status": "in_progress", "assignee": "someone-else"})
run("execute", "VERDICT: WAITING")
check(patches() == [{"assignee": "dev"}], "only the field that differs from the task is written")

# 1. _default when no rule names the verdict; an alias the engine emits
task.update({"status": "in_progress", "assignee": "dev"})
run("execute", "done\nVERDICT: PASS")
check(patches() == [{"assignee": "tester", "status": "pending_review"}],
      "_default is used when no rule names the verdict")
task.update({"status": "in_progress", "assignee": "tester"})
run("execute", "VERDICT: REVISE")
check(patches() == [{"assignee": "dev"}], "a rule named by the verdict wins over _default")

# 5. nothing declared, nothing written
run("gate", "VERDICT: APPROVED")
check(seen == [], "a block whose effect is empty asks the planner nothing")
run("plain", "VERDICT: PASS")
check(seen == [], "a block the map does not name asks the planner nothing")

# 6. a refusal
task.update({"status": "done", "assignee": "pm"})
refuse["on"] = True
p = run("execute", "VERDICT: WAITING")
refuse["on"] = False
check(p.returncode == 0 and "REFUSED" in p.stderr and "400" in p.stderr,
      "a refused write is said in the block's log and does not fail the block")

# 6b. a move the planner takes only with a reason: asked again, with the reason
needs_reason = {"on": True}
_patch = Planner.do_PATCH
def patch_with_rule(self):
    n = int(self.headers.get("Content-Length") or 0)
    body = json.loads(self.rfile.read(n) or b"{}")
    seen.append(("PATCH", self.path, body))
    if body.get("status") in ("blocked", "rejected", "done") and not body.get("comment"):
        return self._send(400, {"detail": "a comment is required when moving a task to %r" % body["status"]})
    task.update({k: v for k, v in body.items() if k != "comment"})
    self._send(200, dict(task))
Planner.do_PATCH = patch_with_rule
open(os.path.join(flow, "demo.yaml"), "a").write("""  - name: stopped
    routing: {_default: _stop}
""")
text = open(os.path.join(flow, "demo.yaml")).read().replace(
    "    gate:\n", "    stopped:\n      skipped:  {}\n      _default: {assignee: boss, status: blocked}\n    gate:\n")
open(os.path.join(flow, "demo.yaml"), "w").write(text)
task.clear(); task.update({"id": 7, "status": "in_progress", "assignee": "dev"})
p = run("stopped", "nothing more to do\nVERDICT: STOPPED")
sent = patches()
check(len(sent) == 2 and "comment" not in sent[0] and sent[1].get("comment")
      and task["status"] == "blocked" and task["assignee"] == "boss",
      "a move the planner takes only with a reason is sent again with the reason")
run("stopped", "VERDICT: SKIPPED")
check(seen == [], "a branch nobody chose (SKIPPED) ends nothing")

# 6c. the flow's error handler: a block failed, the task goes to a person
err_src = os.path.join(os.path.dirname(hook_src), "on_error.py")
if not os.path.exists(err_src):
    check(False, "the flow carries logic/on_error.py")
else:
    shutil.copy(err_src, os.path.join(flow, "logic", "on_error.py"))
    rd = os.path.join(root, "tasks", "task-x")
    os.makedirs(os.path.join(rd, "input"))
    json.dump({"input": {"task_id": "7"}}, open(os.path.join(rd, "input", "task.json"), "w"))
    def fail(error="Post-phase logic failed: logic/x.py"):
        seen.clear()
        return subprocess.run(["python3", os.path.join(flow, "logic", "on_error.py")],
                              input=json.dumps({"block_name": "execute", "error": error, "task_dir": rd,
                                                "task_id": "task-x", "flow_name": "demo"}),
                              env=env, capture_output=True, text=True)
    task.clear(); task.update({"id": 7, "status": "in_progress", "assignee": "dev"})
    p = fail()
    sent = patches()
    check(len(sent) == 1 and sent[0].get("status") == "queued" and sent[0].get("wait_reason") == "error"
          and "execute" in sent[0].get("comment", "")
          and "Post-phase logic failed" in sent[0].get("comment", ""),
          "a failed block stops the task (queued, wait_reason error), with the block and the error as the reason")
    check(sent and sent[0].get("assignee") == "boss",
          "and hands it to the person the flow names for its stopped end")
    p = fail()
    check(patches() == [], "a task that is already stopped is not written again")
Planner.do_PATCH = _patch

# 7. shipped flows: one hook, the same in each, on every block that has an effect
digests = set()
for name in sorted(os.listdir(flows_dir)):
    path = os.path.join(flows_dir, name, name + ".yaml")
    if not os.path.exists(path):
        continue
    wf = yaml.safe_load(open(path)) or {}
    mapped = [b for b, rules in ((wf.get("planner") or {}).get("on_block") or {}).items()
              if isinstance(rules, dict) and any(isinstance(e, dict) and e for e in rules.values())]
    if not mapped:
        continue
    blocks = {b.get("name"): b for b in wf.get("blocks") or []}
    bare = [b for b in mapped if ((blocks.get(b) or {}).get("logic") or {}).get("post") != "logic/planner_effect.py"]
    check(not bare, "%s: every block with an effect carries the hook%s"
          % (name, "" if not bare else " (missing on %s)" % ", ".join(bare)))
    hook = os.path.join(flows_dir, name, "logic", "planner_effect.py")
    check(os.path.exists(hook), "%s: the hook file is in the flow" % name)
    if os.path.exists(hook):
        digests.add(hashlib.md5(open(hook, "rb").read()).hexdigest())
check(len(digests) == 1, "the hook is the same file in every shipped flow")
for name in sorted(os.listdir(flows_dir)):
    path = os.path.join(flows_dir, name, name + ".yaml")
    if not os.path.exists(path):
        continue
    wf = yaml.safe_load(open(path)) or {}
    check(wf.get("on_error") == "logic/on_error.py"
          and os.path.exists(os.path.join(flows_dir, name, "logic", "on_error.py")),
          "%s: a failed block has a handler, and the handler is in the flow" % name)
    ends = [b["name"] for b in wf.get("blocks") or []
            if any(v == "_stop" and k != "skipped" for k, v in (b.get("routing") or {}).items())]
    on_block = (wf.get("planner") or {}).get("on_block") or {}
    silent = [b for b in ends if not ((on_block.get(b) or {}).get("_default") or {}).get("status")]
    check(not silent, "%s: every end of the flow says what becomes of the task%s"
          % (name, "" if not silent else " (silent: %s)" % ", ".join(silent)))

srv.shutdown()
sys.exit(1 if fails else 0)
PY

# 8. The Runner. httpx is stubbed as in runner-planner-map.test.sh; the planner
#    is a recorder. A flow that declares effects is stepped with a finished block
#    on disk, which is exactly when the Runner used to write the hand-off.
python3 - "$RUNNER" <<'PY' || FAILED=$((FAILED + 1))
import asyncio, json, os, sys, types, tempfile, importlib.util

path = sys.argv[1]
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
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
os.makedirs(os.path.join(root, "flows", "demo"))
open(os.path.join(root, "flows", "demo", "demo.yaml"), "w").write("""
schema_version: "3.3.0"
name: demo
team: team-demo
planner:
  on_block:
    execute:
      waiting: {assignee: dev, status: in_progress}
      _default: {assignee: tester, status: pending_review}
blocks:
  - name: execute
    routing: {waiting: execute, _default: _stop}
""")
run = os.path.join(root, "tasks", "task-a")
os.makedirs(os.path.join(run, "execute", "outputs"))
os.makedirs(os.path.join(run, "input"))
json.dump({"input": {"task_id": "7"}}, open(os.path.join(run, "input", "task.json"), "w"))
json.dump({"result": "order posted\nVERDICT: WAITING"},
          open(os.path.join(run, "execute", "outputs", "result.json"), "w"))

calls = []
class Resp:
    status_code, text = 200, ""
    def json(self):
        return []
async def recorder(method, p, **kw):
    calls.append((method, p, kw.get("json")))
    return Resp()
async def nothing(*a, **k):
    return ""
mod._planner = recorder
mod._rein = nothing
mod._emit_workflow = nothing
mod._emit_task_event = nothing
mod._flow_home = lambda flow: root
mod.RUNNER_AGENTS = root
mod._rein_state = lambda d: ("running", {"execute": "waiting"})

for _ in range(3):
    asyncio.run(mod._drive_flow({"id": 7, "flow": "demo", "reindir": "task-a", "topic": "t",
                                 "status": "in_progress", "assignee": "someone"}))
moved = [c for c in calls if c[0] == "PATCH" and set(c[2] or {}) & {"status", "assignee"}]
check(moved == [], "stepping a flow, the Runner writes neither status nor assignee")
check(not os.path.exists(os.path.join(run, "planner-applied.json"))
      and not os.path.exists(os.path.join(run, "planner-written.json")),
      "and keeps no record of its own beside the run")

# 9. starting a flow. The Runner ties the task to its run (the reindir tag) and
#    that is all: the task's status is the flow's first block's to set.
calls.clear()
started = os.path.join(root, "tasks", "task-b")
os.makedirs(os.path.join(started, "input"))
json.dump({"input": {"task_id": "8"}}, open(os.path.join(started, "input", "task.json"), "w"))
async def starts(args):
    return "[TASK] Directory: %s\n" % started
async def no_grounding(flow):
    return {}
mod._rein, mod._flow_grounding = starts, no_grounding
asyncio.run(mod._drive_flow({"id": 8, "flow": "demo", "reindir": None, "topic": "t",
                             "status": "open", "assignee": None}))
check(any(c[0] == "POST" and (c[2] or {}).get("tag") == "reindir:task-b" for c in calls),
      "starting a flow, the Runner ties the task to its run")
check(not any(c[0] == "PATCH" for c in calls),
      "and does not move the task: its status is the flow's to set")

sys.exit(1 if fails else 0)
PY

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL"; exit 1; fi
