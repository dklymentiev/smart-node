#!/usr/bin/env bash
# A stopped database cluster takes the units that Requires= it down with it;
# when the cluster starts again, those units must start again too. Each such
# unit is therefore also WantedBy the cluster, and the mesh MCP sidecar is
# WantedBy the mesh API it Requires. Supports --baseline REF to show RED on main.
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

if [[ -n "$BASELINE" ]]; then echo "database restart brings units back (git:$BASELINE)"
else echo "database restart brings units back (working tree)"; fi

# Every unit heredoc that Requires= a unit must also be WantedBy that unit.
# Prints one line per unit block: <file> <required> <wanted-by list>.
check() {
    local f="$1"
    read_src "$f" | python3 -c '
import re, sys
src = sys.stdin.read()
for block in re.findall(r"\[Unit\](.*?)\nEOF", src, re.S):
    req = re.search(r"^Requires=(\S+)", block, re.M)
    if not req:
        continue
    wb = re.search(r"^WantedBy=(.*)$", block, re.M)
    wanted = wb.group(1).split() if wb else []
    print(req.group(1), "ok" if req.group(1) in wanted else "missing", " ".join(wanted))
'
}

n=0
for f in companions/core-api.sh companions/memory.sh companions/tracking.sh; do
    while read -r req verdict wanted; do
        n=$((n + 1))
        if [[ "$verdict" == ok ]]; then ok "$f: a unit requiring $req is also wanted by it"
        else bad "$f: a unit requires $req but is only wanted by: $wanted"; fi
    done < <(check "$f")
done
[[ $n -ge 4 ]] && ok "found the four units (core-api, mesh, mesh-mcp, planner)" \
               || bad "expected four units with Requires=, found $n"

echo
[[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
exit $FAILED
