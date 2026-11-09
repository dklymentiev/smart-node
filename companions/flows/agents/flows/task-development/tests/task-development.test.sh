#!/usr/bin/env bash
#
# task-development: the approval pipeline, walked without a model
#
# WHY THIS EXISTS. task-development is the development pipeline: plan, owner gate, execute, independent verification, delivery. Every hand-off is declared in planner.on_block and written by the engine; this walks the machine that decides which of them fire, and runs it on the real engine.
#
# THE PROPERTIES:
#   1. the approved path runs plan, gate, execute, verify, deliver; deliver stops the flow
#   2. the gate holds the flow while the owner has not answered
#   3. the owner can send the plan back, and reject it: nothing is executed
#   4. the tester can send the work back, or stop it as blocked
#   5. a failed plan or execution ends at stopped; an undeclared or missing verdict never stalls
#   6. the loops stop at max_runs
#   7. on the real engine every route completes, and only the chosen branch acts
#   8. every hand-off is in planner.on_block; README, CHANGELOG, version and lint follow the standards
#
#   flows/task-development/tests/task-development.test.sh
#   /opt/smart-node/flows/tools/flow-test.sh --flow task-development
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

echo "task-development: the approval pipeline, walked without a model"
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

P="plan=PASS,gate=approved,execute=PASS,verify=PASS,deliver=PASS"
expect "the approved path runs plan, gate, execute, verify, deliver" "$P" "plan>gate>execute>verify>deliver>"
[[ "$(stop "$P")" == _stop* ]] && ok "and deliver stops the flow itself" || bad "and deliver stops the flow itself"
expect "the gate holds the flow while the owner has not answered" \
    "plan=PASS,gate=blocked,gate=blocked,gate=approved,execute=PASS,verify=PASS,deliver=PASS" \
    "plan>gate>gate>gate>execute>verify>deliver>"
expect "a sent-back plan is planned again and re-gated" \
    "plan=PASS,gate=revise,plan=PASS,gate=approved,execute=PASS,verify=PASS,deliver=PASS" \
    "plan>gate>plan>gate>execute>verify>deliver>"
expect "a rejected plan ends at rejected, nothing executed" "plan=PASS,gate=reject,rejected=REJECTED" "plan>gate>rejected>"
expect "failed verification returns the work to the developer, and the next round delivers" \
    "plan=PASS,gate=approved,execute=PASS,verify=REVISE,execute=PASS,verify=PASS,deliver=PASS" \
    "plan>gate>execute>verify>execute>verify>deliver>"
expect "a blocked verification ends at stopped, without delivery" \
    "plan=PASS,gate=approved,execute=PASS,verify=BLOCKED,stopped=STOPPED" "plan>gate>execute>verify>stopped>"
expect "a failed plan ends at stopped" "plan=FAIL,stopped=STOPPED" "plan>stopped>"
expect "a failed execution ends at stopped" "plan=PASS,gate=approved,execute=FAIL,stopped=STOPPED" "plan>gate>execute>stopped>"
expect "an undeclared verdict takes _default rather than stalling" \
    "plan=PASS,gate=approved,execute=PASS,verify=banana,deliver=PASS" "plan>gate>execute>verify>deliver>"
expect "a block with no verdict takes _default" \
    "plan=,gate=approved,execute=PASS,verify=PASS,deliver=PASS" "plan>gate>execute>verify>deliver>"
limit "the developer and tester cannot loop past max_runs" \
    "plan=PASS,gate=approved$(printf ',execute=PASS,verify=REVISE%.0s' {1..8})" verify
limit "an owner who never answers does not keep the gate spinning past its limit" \
    "plan=PASS$(printf ',gate=blocked%.0s' {1..12})" gate

runs "approved: the run completes after deliver" \
    "plan=DONE,gate=approved,execute=DONE,verify=PASS,deliver=DONE" "plan>gate>execute>verify>deliver>"
runs "rejected: the run completes at rejected, nothing executed" \
    "plan=DONE,gate=reject,rejected=REJECTED" "plan>gate>rejected>"
runs "work sent back once: the run completes after deliver" \
    "plan=DONE,gate=approved,execute=DONE,execute=DONE,verify=REVISE,verify=PASS,deliver=DONE" \
    "plan>gate>execute>verify>execute>verify>deliver>"
runs "blocked verification: the run completes at stopped" \
    "plan=DONE,gate=approved,execute=DONE,verify=BLOCKED,stopped=STOPPED" "plan>gate>execute>verify>stopped>"

handoffs=$("$PY" - "$FLOW" <<'PY'
import sys, yaml
ob = (((yaml.safe_load(open(sys.argv[1])) or {}).get("planner") or {}).get("on_block") or {})
want = {("plan", "waiting"): {"assignee": "pm", "status": "in_progress"},
        ("plan", "_default"): {"assignee": "owner"},
        ("execute", "waiting"): {"assignee": "developer", "status": "in_progress"},
        ("verify", "waiting"): {"assignee": "tester", "status": "in_progress"},
        ("deliver", "waiting"): {"assignee": "pm", "status": "in_progress"},
        ("deliver", "_default"): {"assignee": "owner"},
        ("rejected", "_default"): {"assignee": "owner"},
        ("stopped", "_default"): {"assignee": "owner"}}
bad = ["%s.%s.%s is %r, expected %r" % (b, s, k, ((ob.get(b) or {}).get(s) or {}).get(k), v)
       for (b, s), f in want.items() for k, v in f.items() if ((ob.get(b) or {}).get(s) or {}).get(k) != v]
bad += ["%s.skipped must write nothing" % b for b in ("gate", "execute", "verify", "deliver", "rejected", "stopped")
        if (ob.get(b) or {}).get("skipped") != {}]
print("; ".join(bad))
PY
)
[[ -z "$handoffs" ]] && ok "every hand-off is in planner.on_block" || bad "$handoffs"

# Who acts when: logic/branch.py against verdicts written the way Rein writes them.
branch=$("$PY" - "$FLOW_DIR/logic" <<'PY'
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1])
import branch
cases = [
    ("gate", {"plan": "DONE"}, True),
    ("gate", {"plan": "FAIL"}, False),
    ("execute", {"gate": "approved"}, True),
    ("execute", {"gate": "reject"}, False),
    ("execute", {"gate": "blocked"}, False),
    ("verify", {"execute": "DONE"}, True),
    ("verify", {"execute": "FAIL"}, False),
    ("deliver", {"verify": "PASS"}, True),
    ("deliver", {"verify": "REVISE"}, False),
    ("deliver", {"verify": "BLOCKED"}, False),
    ("rejected", {"gate": "reject"}, True),
    ("rejected", {"gate": "approved"}, False),
    ("stopped", {"plan": "FAIL"}, True),
    ("stopped", {"plan": "DONE", "gate": "approved", "execute": "FAIL"}, True),
    ("stopped", {"plan": "DONE", "gate": "approved", "execute": "DONE", "verify": "BLOCKED"}, True),
    ("stopped", {"plan": "DONE", "gate": "approved", "execute": "DONE", "verify": "PASS"}, False),
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

# The gate's request is one the Runner recognises: its first line ends with
# "approval gate.". With any other wording the owner is asked, answers, and the
# hold is never lifted.
marker=$("$PY" - "$FLOW_DIR/logic" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import gate
first = gate.INSTRUCTIONS.splitlines()[0].strip()
ok = first.endswith("approval gate.") and first == gate.GATE_MARKER.strip() and "DECISION: approve" in gate.INSTRUCTIONS
print("" if ok else "first line %r, marker %r" % (first, gate.GATE_MARKER))
PY
)
[[ -z "$marker" ]] && ok "the gate asks in the words the engine recognises, and says how to answer" \
    || bad "the gate's request is not one the engine recognises ($marker)"

# One script writes the task's status and assignee: the hand-off hook
# planner_effect.py, from what planner.on_block declares. No other script does.
writers=$(grep -l -E 'set_status\(|[^_]assign\(|api\("PATCH"|"PATCH"' "$FLOW_DIR"/logic/*.py 2>/dev/null \
          | grep -v -E '/(_planner|planner_effect|on_error)\.py$' | xargs -r -n1 basename | tr '\n' ' ')
[[ -z "$writers" ]] && ok "no script but the hand-off hook writes the task's status or assignee" || bad "scripts that write the task: $writers"

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
if "$PY" "$LINT" --root "$ROOT" --flow task-development --quiet >/dev/null 2>&1; then
    ok "the flow lints clean"
else
    bad "the flow lints clean (run flow-lint.py --root $ROOT --flow task-development)"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
