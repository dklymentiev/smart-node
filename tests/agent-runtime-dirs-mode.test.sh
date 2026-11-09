#!/usr/bin/env bash
#
# A new agent's runtime dirs are made with the mode the permission surface
# declares.
#
# WHAT WENT WRONG. admin-runner's ensure-user script made
# /srv/agents/<slug>/{logs,state,work} with mkdir -p (2755), while
# bootstrap/permissions.list declares them 2750. Every agent created after the
# last permissions apply was drift, and tests/e2e/permissions.sh failed "node
# matches the declared surface" on the 1.12.46 stand, one DRIFT line per dir.
#
# THE PROPERTY: the script as it ships, run against a scratch agent dir, leaves
# logs/, state/ and work/ at the mode permissions.list declares for
# /srv/agents/*/<dir>. The script is taken out of operations.py and run with
# user-management commands stubbed, so it needs no root.
#
#   tests/agent-runtime-dirs-mode.test.sh                  # tree, PASS
#   tests/agent-runtime-dirs-mode.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "agent runtime dirs take the declared mode (git:$BASELINE)"
else echo "agent runtime dirs take the declared mode (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/admin-runner/operations.py > "$D/operations.py"
read_src bootstrap/permissions.list > "$D/permissions.list"
[[ -s "$D/operations.py" && -s "$D/permissions.list" ]] \
    || { bad "sources are readable"; echo; echo "FAIL"; exit 1; }

python3 - "$D/operations.py" > "$D/ensure.sh" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
for n in tree.body:
    if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_ENSURE_USER_SH" for t in n.targets):
        print(n.value.value)
        break
PY
[[ -s "$D/ensure.sh" ]] || { bad "_ENSURE_USER_SH found in operations.py"; echo; echo "FAIL"; exit 1; }

# Stubs: the user already exists; ownership changes are not ours to make here.
mkdir -p "$D/bin" "$D/home" "$D/agent"
for c in getent id usermod useradd groupadd chown; do
    printf '#!/bin/sh\nexit 0\n' > "$D/bin/$c"; chmod +x "$D/bin/$c"
done
PATH="$D/bin:$PATH" bash "$D/ensure.sh" agent-probe "$D/home" smart-node-agents /usr/sbin/nologin "$D/agent" \
    >"$D/out" 2>&1 || { bad "the ensure script runs ($(tail -1 "$D/out"))"; }

for sub in logs state work; do
    want=$(awk -v p="/srv/agents/*/$sub" '$1 == p {print $4}' "$D/permissions.list")
    if [[ -z "$want" ]]; then bad "permissions.list declares /srv/agents/*/$sub"; continue; fi
    got=$(stat -c '%a' "$D/agent/$sub" 2>/dev/null || echo missing)
    # stat prints 2750 as 2750 and 0755 as 755; compare as octal numbers.
    if [[ "$got" != missing ]] && (( 8#$got == 8#$want )); then
        ok "$sub/ is $want as declared"
    else
        bad "$sub/ is $got, declared $want"
    fi
done

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; exit 0; else echo "FAIL ($FAILED)"; exit 1; fi
