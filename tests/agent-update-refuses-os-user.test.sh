#!/usr/bin/env bash
#
# PUT /api/agents/:slug never changes an agent's os_user .
#
# WHAT WENT WRONG. The panel made os_user read-only in its form, but the route
# still wrote whatever os_user it was sent. The kernel identity is uid -> os_user
# -> slug, and the agent's files are owned by that user, so one direct API call
# could cut any agent off from the planner, secrets and messaging. agent-a refused
# the OS account was renamed (2026-09-24).
#
# THE PROPERTIES:
#   1. a PUT that changes os_user is refused (409) and writes nothing
#   2. the same os_user alone is not a change (a form may echo it back)
#   3. the same os_user beside a real edit saves the edit, never os_user
#
# The route is taken out of the source and driven with a recording cursor, so
# the test needs neither a database nor pydantic (not installed where the
# source tests run).
#
#   tests/agent-update-refuses-os-user.test.sh                  # tree, PASS
#   tests/agent-update-refuses-os-user.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "agent update refuses os_user (git:$BASELINE)"
else echo "agent update refuses os_user (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/agents.py > "$D/agents.py"
read_src companions/core-api/schemas/agent.py > "$D/agent_schema.py"
[[ -s "$D/agents.py" && -s "$D/agent_schema.py" ]] || { bad "sources are readable"; echo; echo "FAIL"; exit 1; }

while IFS= read -r line; do
    [[ "$line" == "ok the harness ran every check" ]] && RAN=1
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/agents.py" "$D/agent_schema.py" <<'PY'
import ast, sys
from types import SimpleNamespace
from typing import Annotated

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

schema = ast.parse(open(sys.argv[2]).read())
upd = next((n for n in schema.body if isinstance(n, ast.ClassDef) and n.name == "AgentUpdate"), None)
fields = {s.target.id for s in (upd.body if upd else []) if isinstance(s, ast.AnnAssign)}
check("AgentUpdate still declares os_user (create uses it)", "os_user" in fields)

# update_agent plus any module-level helper or constant it names, minus the decorator.
tree = ast.parse(open(sys.argv[1]).read())
route = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "update_agent"), None)
if route is None:
    print("FAIL update_agent exists"); sys.exit(0)
route.decorator_list = []
used = {n.id for n in ast.walk(route) if isinstance(n, ast.Name)}
keep = []
for n in tree.body:
    if n is route:
        continue
    if isinstance(n, ast.FunctionDef) and n.name.startswith("_") and n.name in used \
            and n.name not in ("_compose_and_write_claude", "_recompose_roster", "_row_to_read", "_perms_from_row"):
        keep.append(n)
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in used and getattr(t, "id", "").startswith("_UPDATE")
                                         for t in n.targets):
        keep.append(n)
keep.append(route)

class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail); self.status_code = status_code

class Cur:
    def __init__(self):
        self.sql = []
    def execute(self, q, p=None):
        self.sql.append((" ".join(q.split()), list(p or [])))
    def fetchone(self):
        last = self.sql[-1][0]
        if last.startswith("SELECT id, version, specialist_id"):
            return {"id": 7, "version": "0.1.4", "specialist_id": None, "os_user": "agent-alex"}
        if "RETURNING" in last:
            return {"id": 9}
        return {"title": "Alex", "model": "sonnet", "role": "r", "description": "d", "slug": "alex"}

class Ctx:
    def __init__(self, cur): self.cur = cur
    def __enter__(self): return self.cur
    def __exit__(self, *a): return False

def run(sent):
    cur, calls = Cur(), []
    ns = {
        "Annotated": Annotated, "PathParam": lambda **k: None, "AgentUpdate": object,
        "HTTPException": HTTPException, "AGENT_COLS": "a.*",
        "psycopg2": SimpleNamespace(errors=SimpleNamespace(CheckViolation=type("CheckViolation", (Exception,), {}))),
        "cursor": lambda commit=False: Ctx(cur),
        "agent_fs": SimpleNamespace(append_changelog=lambda *a, **k: calls.append("changelog_fs"),
                                    rerender_config=lambda *a, **k: calls.append("rerender")),
        "log": SimpleNamespace(warning=lambda *a, **k: None),
        "emit": lambda *a, **k: calls.append("emit"),
        "_compose_and_write_claude": lambda *a, **k: calls.append("compose"),
        "_recompose_roster": lambda *a, **k: calls.append("compose"),
        "_row_to_read": lambda row: SimpleNamespace(model_dump=lambda **k: dict(row)),
        "_perms_from_row": lambda row: {},
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), "agents.py", "exec"), ns)
    names = ("title", "role", "description", "model", "enabled", "os_user", "schedule", "os_groups",
             "mesh_workspace", "fs_read", "fs_write", "forbidden_fs", "forbidden_behavior",
             "policy_categories", "department_id", "specialist_body", "change_note", "avatar")
    payload = SimpleNamespace(**{k: sent.get(k) for k in names}, model_fields_set=set(sent))
    try:
        out = ns["update_agent"](slug="alex", payload=payload, updated_by="admin")
    except HTTPException as e:
        return cur, calls, e
    return cur, calls, out

def update_of(cur):
    return next(((q, p) for q, p in cur.sql if q.startswith("UPDATE sn_config.agents SET")), None)

cur, calls, out = run({"os_user": "agent-mallory"})
check("a PUT that changes os_user is refused with 409",
      isinstance(out, HTTPException) and out.status_code == 409)
check("a refused os_user writes nothing", update_of(cur) is None and "emit" not in calls)

cur, calls, out = run({"os_user": "agent-alex"})
check("the same os_user alone is no change: no UPDATE, answers with the agent",
      update_of(cur) is None and isinstance(out, dict) and out.get("slug") == "alex")

cur, calls, out = run({"os_user": "agent-alex", "title": "Alex"})
u = update_of(cur)
check("the same os_user beside a real edit saves the edit and never writes os_user",
      u is not None and "title=%s" in u[0] and "os_user" not in u[0])
print("ok the harness ran every check")
PY
)

# A harness that died half way printed no FAIL lines and used to read as PASS.
[[ "${RAN:-0}" -eq 1 ]] || bad "the harness ran to the end (it crashed; see the traceback above)"

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
