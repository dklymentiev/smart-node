#!/usr/bin/env bash
#
# A flow that stops on a failed block lets go of the task it was holding.
#
# WHY THIS EXISTS. A delegated block tags its task blocked:agent-work while it
# waits for an agent; the Runner does not step a task that carries the tag. When
# the block then fails (an agent that never answers runs it out of its time
# budget), the flow's error handler moves the task to blocked and hands it to a
# person. It left the tag behind: the task said two things at once, "stopped,
# with a person" and "still waiting for an agent", and whoever picked it up had
# to know to remove a tag before anything would move again. Seen on the first
# live time-budget runs, 2026-10-10.
#
# THE PROPERTIES:
#   1. the handler moves the task to blocked, with the error as the reason
#   2. it removes the hold the flow had set
#   3. a task that carries no hold is not asked to drop one
#   4. a task that is already ended is left alone, tag and all
#   5. the handler is the same file in every shipped flow
#
#   tests/flow-on-error-hold.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOWS="${1:-$HERE/../companions/flows/agents/flows}"

echo "flow error handler: a stopped flow lets go of its task"

python3 - "$FLOWS" <<'PY'
import hashlib, json, os, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

flows_dir = sys.argv[1]
fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

hooks = sorted(os.path.join(flows_dir, n, "logic", "on_error.py") for n in os.listdir(flows_dir)
               if os.path.exists(os.path.join(flows_dir, n, "logic", "on_error.py")))
if not hooks:
    print("  FAIL a shipped flow carries logic/on_error.py")
    sys.exit(1)

state = {"task": {}, "tags": []}
calls = []
class Planner(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
    def do_GET(self):
        calls.append(("GET", self.path, None))
        self._send(state["tags"] if self.path.endswith("/tags") else state["task"])
    def do_PATCH(self):
        body = self._body()
        calls.append(("PATCH", self.path, body))
        self._send({})
    def do_DELETE(self):
        calls.append(("DELETE", self.path, None))
        self._send({})
srv = HTTPServer(("127.0.0.1", 0), Planner)
threading.Thread(target=srv.serve_forever, daemon=True).start()

root = tempfile.mkdtemp()
flow = os.path.join(root, "flows", "demo")
os.makedirs(os.path.join(flow, "logic"))
shutil.copy(hooks[0], os.path.join(flow, "logic", "on_error.py"))
open(os.path.join(flow, "demo.yaml"), "w").write("""
name: demo
planner:
  on_block:
    stopped:
      _default: {assignee: owner, status: pending_review}
blocks:
  - name: wait
  - name: stopped
""")
run = os.path.join(root, "run")
os.makedirs(os.path.join(run, "input"))
json.dump({"flow": "demo", "input": {"task_id": "7"}}, open(os.path.join(run, "input", "task.json"), "w"))
env = {**os.environ, "PLANNER_API_URL": "http://127.0.0.1:%d" % srv.server_port, "PLANNER_API_KEY": "k"}
def fail_block(task, tags):
    state["task"], state["tags"] = task, tags
    calls.clear()
    p = subprocess.run(["python3", os.path.join(flow, "logic", "on_error.py")],
                       input=json.dumps({"block_name": "wait", "task_dir": run,
                                         "error": "time budget exceeded: 2m27s elapsed, budget 2m"}),
                       env=env, capture_output=True, text=True)
    return p.returncode

rc = fail_block({"id": 7, "status": "in_progress", "assignee": "hazel"}, ["blocked:agent-work", "reindir:x"])
patches = [b for m, p, b in calls if m == "PATCH"]
check(rc == 0 and len(patches) == 1 and patches[0].get("status") == "queued"
      and patches[0].get("wait_reason") == "error"
      and "time budget exceeded" in patches[0].get("comment", "") and patches[0].get("assignee") == "owner",
      "a failed block stops the task (queued, wait_reason error), with the reason, to the person the flow names")
check(("DELETE", "/api/v1/tasks/7/tags/blocked:agent-work", None) in calls,
      "and the hold the flow had set is removed")

fail_block({"id": 7, "status": "in_progress", "assignee": "hazel"}, ["reindir:x"])
check(not [c for c in calls if c[0] == "DELETE"], "a task that carries no hold is not asked to drop one")

fail_block({"id": 7, "status": "queued", "state": "queued", "wait_reason": "error", "assignee": "owner"},
           ["blocked:agent-work"])
check(not [c for c in calls if c[0] in ("PATCH", "DELETE")], "a task that is already stopped is left alone")

fail_block({"id": 7, "status": "closed", "state": "closed", "close_outcome": "completed", "assignee": "owner"},
           ["blocked:agent-work"])
check(not [c for c in calls if c[0] in ("PATCH", "DELETE")], "a closed task is left alone")

fail_block({"id": 7, "status": "blocked", "assignee": "owner"}, ["blocked:agent-work"])
check(not [c for c in calls if c[0] in ("PATCH", "DELETE")], "an older planner's blocked task is left alone")

check(len({hashlib.md5(open(h, "rb").read()).hexdigest() for h in hooks}) == 1,
      "the handler is the same file in every shipped flow")

srv.shutdown()
print()
print("PASS" if not fails else "FAIL (%d)" % len(fails))
sys.exit(1 if fails else 0)
PY
