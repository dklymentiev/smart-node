#!/usr/bin/env bash
# A Runner restarted during a wake takes its claim back instead of leaving the
# agent busy until the lease runs out. The held claims live in memory, so a
# restart forgot them: on the trials stand (S26) the agent stayed busy and deaf
# after its turn had ended. On start the Runner adopts every busy claim whose ref
# is a Runner wake ref, and the held-claim sweep releases it once the gateway
# says the turn is over. Claims of other components are left alone.
# Supports --baseline REF to show RED on main.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
ok() { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "runner adopts its claims after a restart (git:$BASELINE)"
else echo "runner adopts its claims after a restart (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/runner.py > "$D/runner.py"

main_body=$(awk 'f && /^(def|async def|class|if __name__)/{exit} /^async def main\(/{f=1} f' "$D/runner.py")
adopt_line=$(grep -n 'await _adopt_claims(' <<<"$main_body" | head -1 | cut -d: -f1)
loop_line=$(grep -n 'while True' <<<"$main_body" | head -1 | cut -d: -f1)
if [[ -n "$adopt_line" && -n "$loop_line" && "$adopt_line" -lt "$loop_line" ]]; then
    ok "main() adopts left-over claims once, before the first sweep"
else
    bad "main() adopts left-over claims once, before the first sweep"
fi

python3 - "$D/runner.py" <<'PY'
import ast, asyncio, logging, sys, time
tree = ast.parse(open(sys.argv[1]).read())
fn = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_adopt_claims"]
if not fn:
    print("  FAIL there is an _adopt_claims()"); sys.exit(1)
state = {"agents": [
    {"slug": "a1", "status": "busy", "current_ref": "runner-a1-10-3"},     # ours: adopt
    {"slug": "a2", "status": "busy", "current_ref": "dispatch:77"},        # fast-dispatch's
    {"slug": "a3", "status": "free", "current_ref": "runner-a3-1-1"},      # not busy
    {"slug": "a4", "status": "busy", "current_ref": "runner-a4-2-2"},      # already held
    {"slug": "a5", "status": "busy", "current_ref": None},
]}
async def _get(client, path):
    assert path == "/api/agents/state"
    return state
ns = {"_get": _get, "_time": time, "log": logging.getLogger("t"),
      "_held": {"a4": (1.0, "runner-a4-2-2", 1)}}
exec(compile(ast.Module(body=fn, type_ignores=[]), "runner.py", "exec"), ns)
n = asyncio.run(ns["_adopt_claims"](None))
held = ns["_held"]
bad = 0
def check(name, cond):
    global bad
    print("  %s %s" % ("ok  " if cond else "FAIL", name)); bad += not cond
check("a busy Runner claim is adopted with its ref and a fresh miss count",
      held.get("a1", (0, "", 9))[1:] == ("runner-a1-10-3", 0))
check("a claim fast-dispatch took is left alone", "a2" not in held)
check("a free agent is not adopted", "a3" not in held)
check("a claim already held keeps its own state", held["a4"] == (1.0, "runner-a4-2-2", 1))
check("a busy row without a ref is not adopted", "a5" not in held)
check("it says how many it adopted", n == 1)
async def broken(client, path):
    raise RuntimeError("core-api down")
ns["_get"] = broken
check("a core-api that cannot answer does not stop the Runner starting",
      asyncio.run(ns["_adopt_claims"](None)) == 0)
sys.exit(1 if bad else 0)
PY
[[ $? -eq 0 ]] || FAILED=$((FAILED + 1))

echo
[[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
exit $FAILED
