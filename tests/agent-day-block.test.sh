#!/usr/bin/env bash
#
# An agent's prompt carries its own day, read from the records.
#
# WHAT WENT WRONG. A session that starts cold answered "what did you work on today"
# from a dozen cross-channel lines and a few minutes of its own events. It named
# the previous evening's tasks as today's and left out the two it had worked in
# the afternoon. A task the owner had approved sat for eleven hours with nobody
# assigned, and no session was ever told it existed. The records to answer all of
# this were there: the event journal (which tasks the agent touched), the task
# comments (what it wrote), the internal mail (who woke it, who handed it what),
# and the planner (which open tasks name the agent as the doer).
#
# THE PROPERTIES, each run on the real code with a synthetic day:
#
#   1. work is what the agent WROTE (comment, move, create), never what it only read
#   2. a task shows its comments, moves, span, wakes and who handed it over
#   3. an open task that names the agent and has not been touched is listed as not
#      started; a closed one, or one assigned to somebody else, is not
#   4. open tasks assigned to the agent are listed once, and not when done or stuck
#   5. the block is capped, however long the day was, and what the cap cuts is not
#      lost: a batch (nine approvals in a row) shows as a batch, grouped by what
#      was done, not buried under the newest twelve tasks
#   6. an agent reads only its own day; the route needs no new permission
#   7. the prompt shows the block, and a failed read shows nothing and breaks nothing
#
#   tests/agent-day-block.test.sh                  # working tree, PASS
#   tests/agent-day-block.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "agent day block (git:$BASELINE)"
else echo "agent day block (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/lib/agent_day.py       > "$D/agent_day.py"
read_src companions/core-api/routes/project_state.py > "$D/route.py"
read_src companions/brin/services/ledger_service.py  > "$D/ledger.py"

# 1-5, 7a. the collector's rules and the renderer, run together on one synthetic day
if python3 - "$D/agent_day.py" "$D/ledger.py" <<'PY'
import ast, json, re, sys
from datetime import datetime, timedelta, timezone
from typing import Any

def pick(path, consts, funcs):
    src = open(path, encoding="utf-8").read()
    keep = []
    for n in ast.parse(src).body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) in consts for t in n.targets):
            keep.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in funcs:
            keep.append(n)
    mod = ast.Module(body=keep, type_ignores=[])
    ast.fix_missing_locations(mod)
    return mod

failed = []
try:
    mod = pick(sys.argv[1],
               {"SCHEMA_VERSION", "MAX_DONE", "MAX_OPEN", "MAX_NOT_STARTED", "MAX_MAIL", "_COMMENT_SNIPPET",
                "_MAIL_SNIPPET", "MAX_TAIL", "_TAIL_TITLE", "_WRITE_OPS", "_TASK_IN_PATH", "_TASK_IN_TEXT"},
               {"_snip", "_iso", "assemble_card"})
    ns = {"re": re, "datetime": datetime, "timezone": timezone, "timedelta": timedelta, "Any": Any}
    exec(compile(mod, "agent_day", "exec"), ns)
    assemble = ns["assemble_card"]
except Exception as ex:
    print("   - the collector is missing: %r" % ex)
    sys.exit(1)

now = datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc)
since = now - timedelta(hours=24)
t = lambda h, m=0: now - timedelta(hours=h, minutes=m)
events = [
    {"ts": t(9), "op": "planner.get", "path": "api/v1/tasks/9001"},
    {"ts": t(8, 30), "op": "planner.comment", "path": "api/v1/tasks/9001/comments"},
    {"ts": t(8, 20), "op": "planner.comment", "path": "api/v1/tasks/9001/comments"},
    {"ts": t(8, 10), "op": "planner.update", "path": "api/v1/tasks/9001"},
    {"ts": t(8, 0), "op": "planner.update", "path": "api/v1/tasks/9001"},
    {"ts": t(5), "op": "planner.comment", "path": "api/v1/tasks/9002/comments"},
    {"ts": t(4), "op": "planner.get", "path": "api/v1/tasks/9003"},       # read only
    {"ts": t(4), "op": "planner.list", "path": "api/v1/tasks"},
]
mail = [
    {"ts": t(9), "sender": "system", "recipient": "agent-a", "content": "The node woke you for task #%d (x)." % 9001},
    {"ts": t(8, 55), "sender": "system", "recipient": "agent-a", "content": "The node woke you for task #%d (x)." % 9001},
    {"ts": t(9, 5), "sender": "agent-b", "recipient": "agent-a", "content": "Task #%d is yours, start now." % 9001},
    {"ts": t(5, 5), "sender": "agent-a", "recipient": "agent-b", "content": "Done with #%d." % 9002},
]
tasks = {9001: {"title": "First task", "status": "closed", "assignee": "agent-a"},
         9002: {"title": "Second task", "status": "in_progress", "assignee": "agent-a"}}
own = {9001: [{"created_at": t(8, 40), "content": "started"},
              {"created_at": t(8, 20), "content": "Research finished. Link: https://example.test/doc"}],
       9002: [{"created_at": t(5), "content": "partial result"}]}
open_assigned = [
    {"id": 9001, "title": "First task", "status": "queued", "updated_at": t(8)},   # touched: not repeated
    {"id": 9004, "title": "Waiting for review", "status": "queued", "updated_at": t(60)},
    {"id": 9005, "title": "Approved, nobody assigned", "status": "in_progress", "updated_at": t(10)},
]
cands = [
    {"id": 9005, "title": "Approved, nobody assigned", "status": "in_progress", "assignee": None, "created_at": t(11)},
    {"id": 9006, "title": "Closed one", "status": "closed", "assignee": "agent-a", "created_at": t(30)},
    {"id": 9007, "title": "For somebody else", "status": "in_progress", "assignee": "agent-c", "created_at": t(12)},
    {"id": 9001, "title": "First task", "status": "closed", "assignee": "agent-a", "created_at": t(20)},
]
card = assemble("agent-a", since, now, events=events, mail=mail, tasks=tasks, own_comments=own,
                open_assigned=open_assigned, candidates=cands)
card = json.loads(json.dumps(card))   # what crosses the wire

done = {d["id"]: d for d in card["done"]}
if set(done) != {9001, 9002}:
    failed.append("done is %r, wanted only the tasks the agent wrote to" % sorted(done))
else:
    d = done[9001]
    if (d["comments"], d["moves"], d["woken"]) != (2, 2, 2):
        failed.append("9001 shows comments/moves/woken %r" % ((d["comments"], d["moves"], d["woken"]),))
    if not d.get("ordered_by") or d["ordered_by"]["sender"] != "agent-b":
        failed.append("9001 does not say who handed it over")
    if "Research finished" not in (d.get("last_comment") or ""):
        failed.append("9001 does not show its last comment")
    if [x["id"] for x in card["done"]] != [9002, 9001]:
        failed.append("done is not newest first")
if [x["id"] for x in card["not_started"]] != [9005]:
    failed.append("not started is %r, wanted [9005]" % [x["id"] for x in card["not_started"]])
if [x["id"] for x in card["open_assigned"]] != [9004]:
    failed.append("open assigned is %r, wanted [9004]" % [x["id"] for x in card["open_assigned"]])
if any(m["sender"] == "system" for m in card["mail"]) or len(card["mail"]) != 2:
    failed.append("mail rows are not the two between colleagues")

# 5. capped
many = [{"ts": t(1, i), "op": "planner.comment", "path": "api/v1/tasks/%d/comments" % (8000 + i)} for i in range(30)]
big = assemble("agent-a", since, now, events=many, mail=[], tasks={}, own_comments={}, open_assigned=[], candidates=[])
if len(big["done"]) != 12 or big["done_total"] != 30:
    failed.append("not capped: %d shown of %d" % (len(big["done"]), big["done_total"]))

# 5b. a busy day: nine approvals (status changes only) long before twenty comments.
# Recency alone would put the approvals at places 14 to 22 and cut them.
busy = (
    [{"ts": t(6, i), "op": "planner.update", "path": "api/v1/tasks/%d" % (9000 + i)} for i in range(9)]
    + [{"ts": t(5, i), "op": "planner.comment", "path": "api/v1/tasks/%d/comments" % (9100 + i)} for i in range(4)]
    + [{"ts": t(1, i), "op": "planner.comment", "path": "api/v1/tasks/%d/comments" % (9200 + i)} for i in range(20)]
)
busy_tasks = {9100: {"title": "Closed one", "status": "closed", "assignee": "agent-a"}}
for i in range(9):
    busy_tasks[9000 + i] = {"title": "Approved %d" % i, "status": "in_progress", "assignee": None}
bc = json.loads(json.dumps(assemble("agent-a", since, now, events=busy, mail=[], tasks=busy_tasks,
                                    own_comments={}, open_assigned=[], candidates=[])))
groups = {g["kind"]: [x["id"] for x in g["tasks"]] for g in bc.get("others") or []}
if sorted(groups.get("updated", [])) != list(range(9000, 9009)):
    failed.append("the nine approvals are not listed after the cap: %r" % groups.get("updated"))
if groups.get("closed") != [9100]:
    failed.append("a closed task beyond the cap is not grouped as closed: %r" % groups.get("closed"))
if sorted(groups.get("commented", [])) != [9101, 9102, 9103] + list(range(9212, 9220)):
    failed.append("comment-only tasks beyond the cap are not grouped: %r" % groups.get("commented"))
if bc.get("others_total") != 21 or len(bc["done"]) != 12:
    failed.append("tail count is %r" % bc.get("others_total"))

# 7a. the renderer on that card
try:
    mod = pick(sys.argv[2], {"_RESULT_SNIPPET", "_DAY_WINDOW_HOURS"}, {"_snippet", "_local_when", "render_day_card"})
    ns2 = {"datetime": datetime, "timezone": timezone}
    exec(compile(mod, "ledger", "exec"), ns2)
    render = ns2["render_day_card"]
    out = render(card)
    for need in ("[your day", "[end your day]", "First task", "Second task", "woken 2x",
                 "Handed over by agent-b", "Approved, nobody assigned", "nobody assigned",
                 "Waiting for review", "Research finished"):
        if need not in out:
            failed.append("rendered block lacks %r" % need)
    if len(out) > 4000:
        failed.append("rendered block is %d chars" % len(out))
    tail_out = render(bc)
    for need in ["#%d" % n for n in range(9000, 9009)] + ["#%d" % 9100, "Status changed", "Closed", "Comment only"]:
        if need not in tail_out:
            failed.append("rendered busy day lacks %r" % need)
    if len(tail_out) > 6000:
        failed.append("rendered busy day is %d chars" % len(tail_out))
    if render(None) != "":
        failed.append("a failed read must render nothing")
    if "Nothing is recorded" not in render({"done": [], "not_started": [], "open_assigned": [], "mail": []}):
        failed.append("an empty day is not stated")
except Exception as ex:
    failed.append("the renderer is missing or raised: %r" % ex)

for f in failed:
    print("   - " + f)
sys.exit(1 if failed else 0)
PY
then ok "work, wakes, hand-over, not started, open, caps, and the rendered block"
else bad "the day card does not describe the agent's day correctly"; fi

# 6. the route
if python3 - "$D/route.py" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read()
i = src.index('@router.get("/agent-day"')
fn = src[i:]
assert 'requires("work.read")' in fn, "not under the existing permission"
assert "an agent may read only its own day" in fn, "an agent can read another agent's day"
assert "le=168" in fn, "window not capped"
assert "_AGENT_SLUG_RE" in fn, "slug not validated"
PY
then ok "the route sits under work.read, held by the agent role, and an agent reads only itself"
else bad "the route does not enforce the agent's own scope"; fi

# 7b. the wiring
if python3 - "$D/ledger.py" <<'PY'
import sys
src = open(sys.argv[1], encoding="utf-8").read()
assert "/api/project-state/agent-day" in src, "no fetch"
i = src.index("async def build_ledger_sections")
b = src[i:]
assert "await fetch_day_card(agent_slug)" in b, "not fetched in assembly"
assert b.index("render_internal_mail(inbox)") < b.index("render_day_card(day)") < b.index("render_recent_activity(activity)"), "not between mail and recent activity"
f = src[src.index("async def fetch_day_card"):src.index("def _local_when")]
assert "return None" in f and "except Exception" in f, "a failed read can raise"
PY
then ok "assembly fetches it between the mail and the recent activity, and a failure shows nothing"
else bad "the prompt does not carry the day block"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
