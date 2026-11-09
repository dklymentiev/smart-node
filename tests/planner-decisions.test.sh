#!/usr/bin/env bash
#
# A person's decision on a waiting task has a door, one format, and lands in one
# transaction.
#
# WHAT WENT WRONG. The Studio Approve button posted a comment and then PATCHed
# {status, approved_at}. TaskPatch is extra="forbid" and has no approved_at, so
# the PATCH failed with 422 after the comment was written: the owner was told
# the decision was not recorded when half of it was, on every node. The
# pipeline could approve and reject; it had no HTTP door. And a flow gate that
# reads decisions matched words in prose ("do not reject this" rejected it).
#
# THE ARCHITECT'S CONDITION: replacing the decision:* tag must be atomic,
# so a gate that fires twice cannot leave two decision tags.
#
# THE PROPERTIES:
#   1. the decision format: first line "DECISION: <what>", a note mentioning
#      another decision stays a note, prose is not a decision
#   2. approve from awaiting_approval: status move (approved_at stamped), comment
#      and tag replacement happen between one BEGIN and its COMMIT, after a row lock
#   3. modify: in_progress WITHOUT approved_at; an assignee is handed the rework
#   4. reject from a task that has not started
#   5. a decision on a task already moved (a second decision) is refused as a
#      conflict: rolled back, nothing applied, no tag written
#   6. the router: POST approve/modify/reject exist; modify and reject need a note;
#      a conflict is 409
#   7. the Studio sends the decision to those doors, never PATCHes approved_at,
#      offers Modify, and the Studio server proxies the three routes
#
# Pipeline methods are taken out of the source and run against a recording
# connection, so neither a database nor pydantic is needed.
#
#   tests/planner-decisions.test.sh                  # tree, PASS
#   tests/planner-decisions.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "planner decisions (git:$BASELINE)"
else echo "planner decisions (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
P=companions/planner/src/planner
read_src $P/decisions.py      > "$D/decisions.py"
read_src $P/pipeline.py       > "$D/pipeline.py"
read_src $P/routers/tasks.py  > "$D/tasks.py"
read_src companions/studio/public/next2/js/app.js > "$D/app.js"
read_src companions/studio/src/server.js    > "$D/server.js"

# A harness that crashes half way must not read as a pass: the Python side prints
# END as its last act, and a run without it fails.
ENDSEEN=0
while IFS= read -r line; do
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
        END)   ENDSEEN=1;;
        *)     printf '       %s\n' "$line";;
    esac
done < <(python3 - "$D" 2>&1 <<'PY'
import ast, asyncio, sys, types, typing
from types import SimpleNamespace
D = sys.argv[1]

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

# 1. the format -------------------------------------------------------------
src = open(f"{D}/decisions.py").read()
if not src.strip():
    for n in ("the decision format", "approve lands in one transaction", "modify does not stamp approved_at",
              "reject from a task that has not started", "a second decision is refused"):
        print("FAIL " + n + " -- planner/decisions.py does not exist")
    dec = None
else:
    dec = types.ModuleType("planner.decisions"); exec(compile(src, "decisions.py", "exec"), dec.__dict__)
    body = dec.compose("approve", "do not reject the next one, revise it")
    check("the decision format: first line is the machine's, a note mentioning another decision stays a note",
          body.splitlines()[0] == "DECISION: approve" and dec.parse(body)[0] == "approve"
          and dec.parse("looks good, ship it") == (None, None) and dec.tag_for("modify") == "decision:modify")

# 2-5. the pipeline, against a recording connection ------------------------
if dec is not None:
    tree = ast.parse(open(f"{D}/pipeline.py").read())
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Pipeline"), None)
    meths = {n.name: n for n in (cls.body if cls else []) if isinstance(n, ast.AsyncFunctionDef)}
    consts = [n for n in (cls.body if cls else []) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    top_err = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name.endswith("Error")]
    if "decide" not in meths:
        for n in ("approve lands in one transaction", "modify does not stamp approved_at",
                  "reject from a task that has not started", "a second decision is refused"):
            print("FAIL " + n + " -- Pipeline.decide does not exist")
    else:
        sys.modules["planner"] = types.ModuleType("planner"); sys.modules["planner.decisions"] = dec
        sys.modules["planner"].decisions = dec
        ns = {"Final": typing.Final, "TaskStatus": str, "Task": object, "asyncpg": None,
              "logger": SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None),
              "fire_assignee_notify": lambda **k: None, "fire_status_notify": lambda *a, **k: None,
              "should_suppress_wake": lambda **k: False, "PRE_APPROVAL_STATUSES": frozenset(),
              "_NOW": "NOW()"}
        exec(compile(ast.Module(body=top_err, type_ignores=[]), "pipeline.py", "exec"), ns)
        body = consts + [meths["status_change"], meths["decide"]] + ([meths["_proposed_doer"]] if "_proposed_doer" in meths else [])
        klass = ast.ClassDef(name="P", bases=[], keywords=[], body=body, decorator_list=[], type_params=[])
        exec(compile(ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[])), "pipeline.py", "exec"), ns)
        P = ns["P"]

        class Conn:
            def __init__(self, log, state): self.log, self.state = log, state
            def transaction(self):
                log = self.log
                class T:
                    async def __aenter__(s): log.append(("BEGIN",))
                    async def __aexit__(s, et, e, tb): log.append(("ROLLBACK",) if et else ("COMMIT",)); return False
                return T()
            async def fetchval(self, q, *a):
                self.log.append(("SQL", " ".join(q.split()), a)); return self.state["status"]
            async def execute(self, q, *a):
                self.log.append(("SQL", " ".join(q.split()), a))
        class Pool:
            def __init__(self, conn): self.conn = conn
            def acquire(self):
                c = self.conn
                class A:
                    async def __aenter__(s): return c
                    async def __aexit__(s, *e): return False
                return A()
            async def fetchval(self, q, *a): return None
            async def fetchrow(self, q, *a): return None   # no proposed doer on the task

        def run(decision, status, assignee=None):
            log, state = [], {"status": status}
            conn = Conn(log, state); p = P(); p._pool = Pool(conn)
            async def apply(c, task_id, to, **kw):
                log.append(("APPLY", to, kw.get("extra_fields"), kw.get("comment")))
                return SimpleNamespace(id=task_id, assignee=assignee, reviewer=None, title="t", status=to)
            async def noop(*a, **k): return True
            p._apply_status_change = apply; p._cascade_unblock_blockees = noop; p._scope_gate_passed = noop
            try:
                asyncio.run(p.decide(7, decision, actor="owner", comment=dec.compose(decision, "n"), assignee=assignee))
                return log, None
            except Exception as e:
                return log, e

        def inside(log):
            b = [i for i, x in enumerate(log) if x[0] == "BEGIN"]; c = [i for i, x in enumerate(log) if x[0] in ("COMMIT", "ROLLBACK")]
            return (b[0], c[0]) if b and c else (None, None)

        log, err = run("approve", "awaiting_approval")
        b, c = inside(log)
        lock = next((i for i, x in enumerate(log) if x[0] == "SQL" and "FOR UPDATE" in x[1]), None)
        app = next((i for i, x in enumerate(log) if x[0] == "APPLY"), None)
        dele = next((i for i, x in enumerate(log) if x[0] == "SQL" and x[1].startswith("DELETE FROM task_tags")), None)
        ins = next((i for i, x in enumerate(log) if x[0] == "SQL" and x[1].startswith("INSERT INTO task_tags")), None)
        ok2 = (err is None and None not in (b, c, lock, app, dele, ins) and log[c][0] == "COMMIT"
               and b < lock < app < dele < ins < c
               and log[app][1] == "in_progress" and "approved_at" in (log[app][2] or {})
               and "decision:" in log[dele][2] and log[ins][2][-1] == "decision:approve"
               and log[app][3].startswith("DECISION: approve"))
        check("approve lands in one transaction: lock, status move with approved_at, old decision tags out, decision:approve in", ok2)

        log, err = run("modify", "awaiting_approval", assignee="agent-b")
        app = next((x for x in log if x[0] == "APPLY"), None)
        check("modify does not stamp approved_at and hands the rework to the assignee",
              err is None and app is not None and app[1] == "in_progress"
              and "approved_at" not in (app[2] or {}) and (app[2] or {}).get("assignee") == "agent-b")

        log, err = run("reject", "open")
        app = next((x for x in log if x[0] == "APPLY"), None)
        check("reject from a task that has not started", err is None and app is not None and app[1] == "rejected")

        log, err = run("approve", "in_progress")
        check("a second decision is refused as a conflict: rolled back, nothing applied, no tag written",
              err is not None and type(err).__name__ == "StatusConflictError"
              and not any(x[0] == "APPLY" for x in log)
              and not any(x[0] == "SQL" and "task_tags" in x[1] for x in log)
              and ("ROLLBACK",) in log)

# 6. the router ---------------------------------------------------------------
t = open(f"{D}/tasks.py").read()
routes = all(f'@router.post("/{{task_id}}/{d}"' in t for d in ("approve", "modify", "reject"))
check("the router: POST approve/modify/reject; modify and reject need a note; a conflict is 409",
      routes and "needs a note" in t and "status_code=409" in t and "StatusConflictError" in t)

# 7. the Studio -------------------------------------------------------------------
a = open(f"{D}/app.js").read(); s = open(f"{D}/server.js").read()
gd = a[a.find("async function gateDecide"):][:2500]
check("the Studio posts the decision to the planner's door and never PATCHes approved_at",
      "/api/tasks/${encodeURIComponent(id)}/${decision}" in gd and "approved_at" not in gd
      and 'id="tdModify"' in a)
check("the Studio server proxies approve, modify and reject",
      "for (const decision of ['approve', 'modify', 'reject'])" in s and "/api/v1/tasks/" in s)
print("END")
PY
)

[[ "$ENDSEEN" -eq 1 ]] || bad "the check itself ran to the end (it crashed; see the traceback above)"

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
