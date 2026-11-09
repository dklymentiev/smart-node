#!/usr/bin/env bash
#
# PUT /api/agents/:slug writes the avatar it is sent, and a PUT that writes
# nothing reports nothing.
#
# WHAT WENT WRONG. The Studio avatar picker (library pick, upload, Remove) sends
# PUT {avatar}. AgentUpdate had no `avatar`, pydantic dropped the key, and
# update_agent answered 200 with the agent unchanged. The Studio re-read the
# roster and showed the letter again, so every avatar ever picked or uploaded,
# on every node, was lost without a word. Worse, the empty PUT still bumped the
# version, wrote a changelog entry, recomposed CLAUDE.md and emitted
# agent.updated: on example-node six such PUTs raised six versions for nothing.
#
# THE PROPERTIES:
#   1. AgentUpdate declares avatar
#   2. a PUT with an avatar key writes that key
#   3. Remove (the Studio sends "") and null clear the column to NULL
#   4. a PUT without avatar leaves the column out of the UPDATE
#   5. a PUT that carries no field the route writes changes nothing: no UPDATE,
#      no version bump, no changelog, no recompose, no event -- and still 200s
#      with the agent, so callers see the current state
#   6. an avatar that climbs out of the store ("..", absolute, backslash) is refused
#
# The route is taken out of the source and driven with a recording cursor, so
# the test needs neither a database nor pydantic (not installed where the
# source tests run).
#
#   tests/agent-update-writes-avatar.test.sh                  # tree, PASS
#   tests/agent-update-writes-avatar.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "agent update writes avatar (git:$BASELINE)"
else echo "agent update writes avatar (working tree)"; fi

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
check("AgentUpdate declares avatar", "avatar" in fields)

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

cur, calls, out = run({"avatar": "crew/alex.webp"})
u = update_of(cur)
check("a PUT with an avatar key writes that key",
      u is not None and "avatar=%s" in u[0] and "crew/alex.webp" in u[1])

for label, value in (("Remove (\"\")", ""), ("null", None)):
    cur, calls, out = run({"avatar": value})
    u = update_of(cur)
    ok_clear = False
    if u is not None and "avatar=%s" in u[0]:
        sets = u[0].split(" SET ", 1)[1].split(" WHERE ")[0].split(", ")
        idx = [s for s in sets if "%s" in s].index("avatar=%s")
        ok_clear = u[1][idx] is None
    check(f"{label} clears the avatar to NULL", ok_clear)

cur, calls, out = run({"title": "Alex"})
u = update_of(cur)
check("a PUT without avatar leaves the column alone", u is not None and "avatar" not in u[0])

cur, calls, out = run({"department_id": 3, "change_note": "x"})
check("a PUT that writes nothing issues no UPDATE", update_of(cur) is None)
check("a PUT that writes nothing bumps no version and writes no changelog",
      not any("changelog_entries" in q for q, _ in cur.sql) and "changelog_fs" not in calls)
check("a PUT that writes nothing recomposes nothing and emits nothing",
      not ({"compose", "rerender", "emit"} & set(calls)))
check("a PUT that writes nothing still answers with the agent", isinstance(out, dict) and out.get("slug") == "alex")

for bad_key in ("../../etc/passwd", "/etc/passwd", "crew\\x.webp"):
    cur, calls, out = run({"avatar": bad_key})
    check(f"an avatar outside the store is refused: {bad_key!r}",
          isinstance(out, HTTPException) and out.status_code == 400 and update_of(cur) is None)
print("ok the harness ran every check")
PY
)

# A harness that died half way printed no FAIL lines and used to read as PASS.
[[ "${RAN:-0}" -eq 1 ]] || bad "the harness ran to the end (it crashed; see the traceback above)"

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
