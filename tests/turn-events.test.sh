#!/usr/bin/env bash
#
# Every agent turn leaves a start and an end, and both carry the same turn id.
#
# WHY THIS EXISTS. An agent is launched three ways: a desk chat through brin, a
# wake by the Runner, and a fast channel (email, telegram) through
# communication/dispatch.py. The event log is what answers "is this agent
# working, on what, since when, and on how many things at once". It could not:
# the desk end events carried no id, so two turns in one agent could not be
# paired; a desk turn routed to a workflow wrote a start and no end and kept the
# agent claimed; and a fast-channel turn wrote nothing at all under the agent's
# name. On a node in the field an agent spent a minute answering an email on
# 2026-09-14 and the log did not know it ran.
#
# THE PROPERTIES:
#   1. every desk turn event in brin's /message carries turn_id
#   2. a desk turn routed to a workflow writes its end and releases the claim
#   3. a fast-channel turn writes agent.turn_started before the agent is asked
#   4. it writes agent.turn_completed on success and agent.turn_failed on any
#      failure, with the same turn_id, before the claim is released
#   5. the turn events are the agent's (actor agent:<slug>), not a component's
#
#   tests/turn-events.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
MAIN="$REPO/companions/brin/main.py"
DISPATCH="$REPO/companions/communication/dispatch.py"
DB="$REPO/companions/communication/common/db.py"

echo "turns: every turn leaves a start and an end with one turn id"

python3 - "$MAIN" "$DISPATCH" "$DB" <<'PY'
import ast, sys

main_p, dispatch_p, db_p = sys.argv[1:4]
fails = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)

def fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None

def calls(node, attr):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name == attr:
                out.append(n)
    return out

def event_name(call, pos):
    if len(call.args) > pos and isinstance(call.args[pos], ast.Constant):
        return call.args[pos].value
    return None

def payload_keys(call, pos):
    if len(call.args) > pos:
        a = call.args[pos]
        if isinstance(a, ast.Dict):
            return {k.value for k in a.keys if isinstance(k, ast.Constant)}
    return None

# 1, 2. brin desk path
main = ast.parse(open(main_p).read())
send = fn(main, "send_message")
check(send is not None, "brin has send_message")
desk = calls(send, "_desk_event")
check(len(desk) >= 9, "the desk path emits its start and every end (%d events)" % len(desk))
missing = [event_name(c, 1) for c in desk if "turn_id" not in (payload_keys(c, 3) or set())]
check(not missing, "every desk turn event carries turn_id%s" % ("" if not missing else ": missing on %s" % missing))

wf_ok = False
for node in ast.walk(send):
    if isinstance(node, ast.If) and "ReinWorkflow" in ast.unparse(node.test):
        names = {event_name(c, 1) for c in calls(node, "_desk_event")}
        # now a desk turn gives its claim back through _desk_turn_end,
        # which releases the agent when the last desk turn of that agent ends.
        releases = [c for c in calls(node, "_desk_runtime")
                    if len(c.args) > 1 and isinstance(c.args[1], ast.Constant) and c.args[1].value == "release"]
        releases += calls(node, "_desk_turn_end")
        wf_ok = {"agent.turn_completed", "agent.turn_failed"} <= names and len(releases) >= 2
check(wf_ok, "a desk turn routed to a workflow ends (completed or failed) and releases its claim")
end_fn = fn(main, "_desk_turn_end")
end_releases = end_fn is not None and any(
    len(c.args) > 1 and isinstance(c.args[1], ast.Constant) and c.args[1].value == "release"
    for c in calls(end_fn, "_desk_runtime"))
check(end_releases, "_desk_turn_end gives the agent back (release) when the last desk turn ends")

# 3, 4. fast-channel path
disp = ast.parse(open(dispatch_p).read())
# The turn moved from _handle to _run_turn when a turn began to read every
# message of one conversation together: _handle admits, _run_turn runs.
handle = fn(disp, "_run_turn")
check(handle is not None, "dispatch has _run_turn")
src = ast.unparse(handle)
turns = calls(handle, "emit_turn")
names = [event_name(c, 1) for c in turns]
check("agent.turn_started" in names, "a fast-channel turn writes agent.turn_started")
check("agent.turn_completed" in names and "agent.turn_failed" in names,
      "a fast-channel turn writes turn_completed on success and turn_failed on failure")
i_start = src.find("agent.turn_started")
i_ask = src.find("_ask_brin(")
i_claim = src.find("_claim_agent(")
check(0 <= i_claim < i_start < i_ask, "the start is written after the claim and before the agent is asked")

tries = [n for n in ast.walk(handle) if isinstance(n, ast.Try) and n.finalbody
         and any("_release_agent" in ast.unparse(b) for b in n.finalbody)]
ends_inside = False
for t in tries:
    body_names = {event_name(c, 1) for h in t.handlers for c in calls(h, "emit_turn")}
    else_names = {event_name(c, 1) for b in t.orelse for c in calls(b, "emit_turn")}
    ends_inside = "agent.turn_failed" in body_names and "agent.turn_completed" in else_names
check(ends_inside, "both ends are written inside the claim, before it is released")
check("turn_id" in src and "uuid" in src, "the fast-channel turn carries a turn_id")

# 5. actor
db = open(db_p).read()
check("def emit_turn(" in db and '"agent:%s" % agent' in db, "turn events are written as agent:<slug>")

sys.exit(1 if fails else 0)
PY
rc=$?
echo
if [[ $rc -eq 0 ]]; then echo "PASS"; else echo "FAIL"; fi
exit $rc
