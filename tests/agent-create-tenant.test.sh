#!/usr/bin/env bash
#
# POST /api/agents never stores an agent without a tenant.
#
# WHAT WENT WRONG. The payload's tenant_id defaults to None, the panel sends
# none, and create_agent wrote that NULL into sn_config.agents and into the
# agent's identity row in sn_config.users. auth.get_principal refuses a user
# with no tenant, so every call the agent made answered 403 "has no tenant
# assignment": found by tests/e2e/agent-inbox-read.sh on the 1.12.46 stand,
# where 6 of 8 checks failed for an agent created without tenant_id.
#
# THE PROPERTIES:
#   1. an explicit tenant_id is kept as sent
#   2. none sent, one active tenant on the node: that tenant
#   3. none sent, several active tenants: refused with 400, nothing guessed
#   4. none sent, no tenant at all: refused with 400
#   5. create_agent resolves the tenant before its first INSERT
#
# The helper is taken out of the source and driven with a fake cursor, so the
# test needs neither a database nor pydantic.
#
#   tests/agent-create-tenant.test.sh                  # tree, PASS
#   tests/agent-create-tenant.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "agent create resolves a tenant (git:$BASELINE)"
else echo "agent create resolves a tenant (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/core-api/routes/agents.py > "$D/agents.py"
[[ -s "$D/agents.py" ]] || { bad "agents.py is readable"; echo; echo "FAIL"; exit 1; }

RAN=0
while IFS= read -r line; do
    [[ "$line" == "ok the harness ran every check" ]] && RAN=1
    case "$line" in
        ok*)   ok "${line#ok }";;
        FAIL*) bad "${line#FAIL }";;
    esac
done < <(python3 - "$D/agents.py" <<'PY'
import ast, sys

def check(name, cond):
    print(("ok " if cond else "FAIL ") + name)

tree = ast.parse(open(sys.argv[1]).read())
helper = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "_resolve_create_tenant"), None)
create = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "create_agent"), None)
check("create_agent exists", create is not None)
if helper is None:
    print("FAIL a tenant resolver exists (_resolve_create_tenant)")
    print("ok the harness ran every check")
    sys.exit(0)

class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail); self.status_code = status_code

class Cur:
    def __init__(self, ids): self.ids, self.sql = ids, []
    def execute(self, q, p=None): self.sql.append(" ".join(q.split()))
    def fetchall(self): return [{"id": i} for i in self.ids]

ns = {"HTTPException": HTTPException}
exec(compile(ast.Module(body=[helper], type_ignores=[]), "agents.py", "exec"), ns)
resolve = ns["_resolve_create_tenant"]

def call(ids, sent):
    cur = Cur(ids)
    try:
        return resolve(cur, sent), cur
    except HTTPException as e:
        return e, cur

out, cur = call([1, 2], 2)
check("an explicit tenant_id is kept as sent", out == 2)
out, cur = call([1], None)
check("none sent, one active tenant: that tenant", out == 1)
check("the lookup ignores archived tenants", any("archived_at IS NULL" in q for q in cur.sql))
out, _ = call([1, 2], None)
check("none sent, several tenants: 400, nothing guessed",
      isinstance(out, HTTPException) and out.status_code == 400)
out, _ = call([], None)
check("none sent, no tenant: 400", isinstance(out, HTTPException) and out.status_code == 400)

# 5: the resolver runs before the first INSERT inside create_agent.
calls, first_insert = [], None
for n in ast.walk(create):
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Name) and f.id == "_resolve_create_tenant":
            calls.append(n.lineno)
        if isinstance(f, ast.Attribute) and f.attr == "execute" and n.args \
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str) \
                and n.args[0].value.lstrip().upper().startswith("INSERT"):
            first_insert = n.lineno if first_insert is None else min(first_insert, n.lineno)
check("create_agent resolves the tenant before its first INSERT",
      bool(calls) and first_insert is not None and min(calls) < first_insert)
print("ok the harness ran every check")
PY
)
[[ "$RAN" == 1 ]] || bad "the harness ran every check"

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; exit 0; else echo "FAIL ($FAILED)"; exit 1; fi
