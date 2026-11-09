#!/usr/bin/env bash
# A turn the gateway reports as failed is not an answer. Claude Code ends a
# turn it could not do (the provider answering 529 on every retry) with an
# error result; the gateway returns failed=<reason>, outcome=failed. brin's
# executor did not read the field and returned the turn as a reply, so the
# Runner logged "done" and nothing was journalled (trials step S23).
# Supports --baseline REF to show RED on main.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}
if [[ -n "$BASELINE" ]]; then echo "brin reads the gateway's failed (git:$BASELINE)"
else echo "brin reads the gateway's failed (working tree)"; fi
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/brin/core/executor.py > "$D/executor.py"
read_src companions/gateway/sessions_v2_routes.py > "$D/routes.py"
python3 - "$D/executor.py" "$D/routes.py" <<'PY'
import sys
s, g = open(sys.argv[1]).read(), open(sys.argv[2]).read()
bad = 0
def check(name, cond):
    global bad
    print("  %s %s" % ("ok  " if cond else "FAIL", name)); bad += not cond
check("the gateway still reports a failed turn as failed=<reason>", '"failed": response.failed' in g)
i = s.find('if data.get("stopped"):')
j = s.find('if data.get("failed"):')
k = s.find('artifacts.actions = response.get("tool_calls")')
check("the executor checks failed before it takes the turn as an answer", -1 not in (i, j, k) and i < j < k)
body = s[j:k]
check("a failed turn raises, so callers treat it as a failure", "raise RuntimeError(" in body)
print(); print("PASS" if not bad else "FAIL (%d)" % bad)
sys.exit(1 if bad else 0)
PY
