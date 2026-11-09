#!/usr/bin/env bash
#
# A work order says whom it is for.
#
# WHY THIS EXISTS. A delegated block posts its order as a comment on the task.
# An agent that is still inside its turn reads the task again, sees the next
# block's order and, with no name on it, takes it for its own. On 2026-10-10
# one agent wrote the answers that were ordered from another on two tasks at
# once. The flow ignored them, because a block accepts a verdict
# only from the assignee, but the task record had an answer nobody asked for.
#
# THE PROPERTIES:
#   1. the order names the agent the flow hands this block to
#   2. the first line of the order is unchanged: it is what finds and closes orders
#   3. a block the flow gives no waiting assignee gets an order without a name
#   4. the delegate hook is the same file in every shipped flow that has one
#   5. the name is a mention (@name): the planner tells a mentioned actor, so a
#      second order to the same agent reaches it though the assignee did not change
#
#   tests/flow-order-addressee.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOWS="${1:-$HERE/../companions/flows/agents/flows}"

echo "flow work orders: each says whom it is for"

python3 - "$FLOWS" <<'PY'
import hashlib, json, os, shutil, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

flows_dir = sys.argv[1]
fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

hooks = sorted(os.path.join(flows_dir, n, "logic", "delegate.py") for n in os.listdir(flows_dir)
               if os.path.exists(os.path.join(flows_dir, n, "logic", "delegate.py")))
if not hooks:
    print("  FAIL a shipped flow carries logic/delegate.py")
    sys.exit(1)

posted = []
class Planner(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _send(self, body):
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        self._send({"id": 7, "assignee": "pm", "status": "open"} if self.path.endswith("/tasks/7") else [])
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        posted.append((self.path, body))
        self._send({})
srv = HTTPServer(("127.0.0.1", 0), Planner)
threading.Thread(target=srv.serve_forever, daemon=True).start()

root = tempfile.mkdtemp()
flow = os.path.join(root, "flows", "demo")
os.makedirs(os.path.join(flow, "logic"))
os.makedirs(os.path.join(flow, "orders"))
shutil.copy(hooks[0], os.path.join(flow, "logic", "delegate.py"))
open(os.path.join(flow, "orders", "execute.md"), "w").write("Do the thing for task #{task_id}.\n")
open(os.path.join(flow, "orders", "plain.md"), "w").write("Do the other thing.\n")
open(os.path.join(flow, "demo.yaml"), "w").write("""
name: demo
planner:
  on_block:
    execute:
      waiting: {assignee: hazel, status: in_progress}
      _default: {}
blocks:
  - name: execute
  - name: plain
""")
env = {**os.environ, "PLANNER_API_URL": "http://127.0.0.1:%d" % srv.server_port, "PLANNER_API_KEY": "k"}
def order(block):
    posted.clear()
    subprocess.run(["python3", os.path.join(flow, "logic", "delegate.py")],
                   input=json.dumps({"output_file": os.path.join(root, block + ".json"),
                                     "task_input": {"task_id": "7"}, "block_config": {"name": block}}),
                   env=env, capture_output=True, text=True)
    return next((b.get("content", "") for p, b in posted if p.endswith("/comments")), "")

text = order("execute")
check("hazel" in text and "not yours" in text, "the order names the agent the flow hands the block to")
check("@hazel" in text, "and names it as a mention, so the order itself tells the agent")
check(text.split("\n", 1)[0] == "[work order] demo/execute", "the first line of the order is unchanged")
check("Do the thing for task #7." in text, "the order itself is still there")
text = order("plain")
check(text.startswith("[work order] demo/plain") and "work order is for" not in text,
      "a block with no waiting assignee gets an order without a name")
check(len({hashlib.md5(open(h, "rb").read()).hexdigest() for h in hooks}) == 1,
      "the delegate hook is the same file in every shipped flow")

srv.shutdown()
print()
print("PASS" if not fails else "FAIL (%d)" % len(fails))
sys.exit(1 if fails else 0)
PY
