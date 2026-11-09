#!/usr/bin/env bash
#
# task-simple: execute, verify, deliver, walked without a model
#
# WHY THIS EXISTS. Every edge of the four-step task flow, and the two defects that only show on the real engine: a block that runs nothing, and a branch that starts although nobody chose it.
#
# THE PROPERTIES:
#   1. the approved path runs execute, verify, deliver, done, and done stops the flow
#   2. a REVISE returns the work to execute and a later approval delivers
#   3. a failure, or a verdict nobody declared, ends at stopped and never delivers
#   4. the loop between execute and verify stops at max_runs
#   5. planner writes use the Runner's planner key (the socket refuses them)
#   6. on the real engine every route completes, and only the chosen branch acts
#   7. logic/branch.py lets each block act only where the flow sent it
#   8. every block runs something; only the first block writes the task's status
#   9. README, CHANGELOG, version and lint follow Flow Maintenance Standards
#
#   flows/task-simple/tests/task-simple.test.sh
#   /opt/smart-node/flows/tools/flow-test.sh --flow task-simple
#
# No model, no gateway, no planner writes: flow-trace.py walks the routing table,
# flow-stubrun.py runs the flow through the real Rein engine on stub blocks.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLOW_DIR="$(cd "$HERE/.." && pwd)"
FLOW="$FLOW_DIR/$(basename "$FLOW_DIR").yaml"
ROOT="$(cd "$FLOW_DIR/../.." && pwd)"
TOOLS="${SN_FLOW_TOOLS:-/opt/smart-node/flows/tools}"
TRACE="$TOOLS/flow-trace.py"
STUBRUN="$TOOLS/flow-stubrun.py"
PY="${SN_FLOW_PY:-$([[ -x /opt/smart-node/workflows/venv/bin/python ]] && echo /opt/smart-node/workflows/venv/bin/python || echo python3)}"
LINT="$TOOLS/flow-lint.py"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "task-simple: execute, verify, deliver, walked without a model"
[[ -r "$FLOW" ]] || { bad "the flow is readable at $FLOW"; echo; echo "FAIL"; exit 1; }

walk()  { "$PY" "$TRACE" "$FLOW" --script "$1" 2>&1; }
route() { walk "$1" | awk '/signal=/ {printf "%s>", $1} END {print ""}'; }
stop()  { walk "$1" | sed -n 's/^STOP  //p'; }
expect() {  # expect <name> <script> <route>
    local r; r=$(route "$2")
    [[ "$r" == "$3" ]] && ok "$1" || bad "$1 (got: $r)"
}
limit() {  # limit <name> <script> <block>
    if walk "$2" | grep -q "^STOP  max_runs at $3"; then ok "$1"; else bad "$1 (got: $(walk "$2" | tail -1))"; fi
}
# The whole route through the real engine, on stubs: which blocks acted, and the
# run must complete (an empty block, or a branch that never ends, fails here).
runs() {  # runs <name> <script> <acted>
    local out; out=$("$PY" "$STUBRUN" "$FLOW_DIR" "$2" 2>&1)
    if [[ "$(sed -n 1p <<< "$out")" == "$3" && "$(sed -n 2p <<< "$out")" == "status=completed" ]]; then
        ok "$1"
    else
        bad "$1 (got: $(tr '\n' ' ' <<< "$out"))"
    fi
}

# Edges, from the routing table.
expect "execute, verify, deliver, done: the approved path" \
    "execute=DONE,verify=APPROVE,deliver=DONE,done=DONE" "execute>verify>deliver>done>"
[[ "$(stop "execute=DONE,verify=APPROVE,deliver=DONE,done=DONE")" == _stop* ]] \
    && ok "and done stops the flow itself" || bad "and done stops the flow itself"
expect "a REVISE goes back to execute, and the next round delivers" \
    "execute=DONE,verify=REVISE,execute=DONE,verify=APPROVE,deliver=DONE,done=DONE" \
    "execute>verify>execute>verify>deliver>done>"
expect "a failed execution ends at stopped" "execute=FAIL,stopped=STOPPED" "execute>stopped>"
expect "a failed verification ends at stopped, without delivery" \
    "execute=DONE,verify=FAIL,stopped=STOPPED" "execute>verify>stopped>"
expect "an undeclared verification verdict ends at stopped, never at deliver" \
    "execute=DONE,verify=banana,stopped=STOPPED" "execute>verify>stopped>"
expect "an execution with no verdict still goes to verification" \
    "execute=,verify=APPROVE,deliver=DONE,done=DONE" "execute>verify>deliver>done>"
expect "a failed delivery ends at stopped" \
    "execute=DONE,verify=APPROVE,deliver=FAIL,stopped=STOPPED" "execute>verify>deliver>stopped>"
limit "the developer and tester cannot loop past max_runs" \
    "execute=DONE,verify=REVISE,execute=DONE,verify=REVISE,execute=DONE,verify=REVISE,execute=DONE" execute

# Planner writes go with the Runner's key: the core-api socket refuses them
# (403) from a process that is not an agent, and the blocks swallow the error.
transport=$(PLANNER_API_URL=http://127.0.0.1:8080 PLANNER_API_KEY=k "$PY" - "$FLOW_DIR/logic" <<'PY'
import http.client, sys
sys.path.insert(0, sys.argv[1])
import _planner
seen = []
class Fake:
    def __init__(self, *a, **k): seen.append(("tcp",) + a)
    def request(self, m, path, body=None, headers=None): seen.append((m, path, (headers or {}).get("X-API-Key")))
    def getresponse(self):
        class R:
            status = 201
            def read(self): return b"{}"
        return R()
    def close(self): pass
http.client.HTTPConnection = Fake
_planner.core_api("POST", "/api/planner/api/v1/tasks/1/comments", {"content": "x"})
ok = bool(seen) and seen[0][0] == "tcp" and seen[1] == ("POST", "/api/v1/tasks/1/comments", "k")
print("" if ok else "planner write went %r" % seen)
PY
)
[[ -z "$transport" ]] && ok "planner writes use the Runner's planner key, not the core-api socket" || bad "$transport"

# The whole route through the real engine, on stubs.
runs "approved: the run completes after done" \
    "execute=DONE,verify=APPROVE,deliver=DONE,done=DONE" "execute>verify>deliver>done>"
runs "revised once: the run completes after done" \
    "execute=DONE,execute=DONE,verify=REVISE,verify=APPROVE,deliver=DONE,done=DONE" \
    "execute>verify>execute>verify>deliver>done>"
runs "execution failed: the run completes at stopped" \
    "execute=FAIL,stopped=STOPPED" "execute>stopped>"
runs "verification failed: the run completes at stopped, nothing delivered" \
    "execute=DONE,verify=FAIL,stopped=STOPPED" "execute>verify>stopped>"

# Who acts when: logic/branch.py against verdicts written the way Rein writes them.
branch=$("$PY" - "$FLOW_DIR/logic" <<'PY'
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import branch
cases = [
    ("verify", {"execute": "DONE"}, True),
    ("verify", {"execute": "FAIL"}, False),
    ("deliver", {"verify": "APPROVE"}, True),
    ("deliver", {"verify": "REVISE"}, False),
    ("deliver", {"verify": "FAIL"}, False),
    ("done", {"deliver": "DONE"}, True),
    ("done", {"deliver": "SKIPPED"}, False),
    ("done", {"deliver": "FAIL"}, False),
    ("stopped", {"execute": "FAIL"}, True),
    ("stopped", {"execute": "DONE", "verify": "FAIL"}, True),
    ("stopped", {"execute": "DONE", "verify": "banana"}, True),
    ("stopped", {"execute": "DONE", "verify": "APPROVE", "deliver": "FAIL"}, True),
    ("stopped", {"execute": "DONE", "verify": "APPROVE", "deliver": "DONE"}, False),
    ("stopped", {"execute": "DONE", "verify": "REVISE"}, False),
]
bad = []
for block, verdicts, expected in cases:
    d = tempfile.mkdtemp()
    for b, v in verdicts.items():
        os.makedirs(os.path.join(d, b, "outputs"))
        json.dump({"result": "x\nVERDICT: " + v}, open(os.path.join(d, b, "outputs", "result.json"), "w"))
    got = branch.RULES[block](lambda b: branch.verdict(d, b))
    if got != expected:
        bad.append("%s with %s acts=%s, expected %s" % (block, verdicts, got, expected))
missing = [b for b in branch.RULES if b not in branch.RUN or not os.path.exists(os.path.join(sys.argv[1], branch.RUN[b]))]
if missing:
    bad.append("no script to run for " + ", ".join(missing))
print("; ".join(bad))
PY
)
[[ -z "$branch" ]] && ok "logic/branch.py lets each block act only where the flow sent it" || bad "$branch"

# Every block runs something: Rein refuses a block with no script, prompt or
# command, and a flow ending in an empty block never completes.
idle=$("$PY" - "$FLOW" <<'PY'
import sys, yaml
wf = yaml.safe_load(open(sys.argv[1])) or {}
print(" ".join(b.get("name", "?") for b in wf.get("blocks") or []
               if not ((b.get("logic") or {}).get("custom") or b.get("prompt") or b.get("command"))))
PY
)
[[ -z "$idle" ]] && ok "every block runs a script or a prompt" || bad "blocks that run nothing: $idle"

# The flow moves its own task, and in one place: execute.py takes it when the
# flow starts (open -> in_progress). No other script writes status or assignee.
writers=$(grep -l -E 'set_status\(|[^_]assign\(|api\("PATCH"|"PATCH"' "$FLOW_DIR"/logic/*.py 2>/dev/null \
          | grep -v -E '/(_planner|execute|planner_effect|on_error)\.py$' | xargs -r -n1 basename | tr '\n' ' ')
[[ -z "$writers" ]] && ok "no script but the first block writes the task's status or assignee" || bad "scripts that write the task: $writers"

for f in README.md CHANGELOG.md; do
    [[ -s "$FLOW_DIR/$f" ]] && ok "$f is present" || bad "$f is missing"
done
ver=$("$PY" -c "import yaml,sys; print(((yaml.safe_load(open(sys.argv[1])) or {}).get('metadata') or {}).get('version') or '')" "$FLOW")
[[ -n "$ver" ]] && grep -q "^## $ver — " "$FLOW_DIR/CHANGELOG.md" \
    && ok "metadata.version $ver has a CHANGELOG entry" \
    || bad "metadata.version '$ver' has no CHANGELOG entry (## $ver — date — title)"
for s in "## When to use" "## How to use" "## What happens" "## Reference"; do
    grep -q "^$s" "$FLOW_DIR/README.md" && ok "README has '$s'" || bad "README lacks '$s'"
done
[[ -z "$(find "$FLOW_DIR" -name '*.bak*' -o -name '*~')" ]] && ok "no backup files in the flow" || bad "backup files in the flow directory"
if "$PY" "$LINT" --root "$ROOT" --flow task-simple --quiet >/dev/null 2>&1; then
    ok "the flow lints clean"
else
    bad "the flow lints clean (run flow-lint.py --root $ROOT --flow task-simple)"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
