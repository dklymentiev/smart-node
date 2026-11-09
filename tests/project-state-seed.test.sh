#!/usr/bin/env bash
#
# project-state-seed.test.sh -- a fresh or updated node must come with what the
# live node was given by hand: the project-state MCP server, its contract
# text, and the index the desk list needs.
#
# project_state is an api tool, which only brin's function-calling loop runs; a
# Claude CLI agent reaches it through an MCP server named in .mcp.json. Without the
# server row and a non-empty usage_md the agent holds the grant and cannot call the
# tool, or never reads when to.
#
# Usage: tests/project-state-seed.test.sh [--baseline REF]
# Exit: 0 all pass, 1 any fail, 2 setup error.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

fetch() {  # fetch <relpath> <dest>
    if [[ -n "$BASELINE" ]]; then git -C "$ROOT" show "$BASELINE:$1" > "$2" 2>/dev/null || : > "$2"
    else cp "$ROOT/$1" "$2" 2>/dev/null || : > "$2"; fi
}
fetch companions/dispatcher.sh "$WORK/dispatcher.sh"
fetch bootstrap/B02-sn-core-schema.sql "$WORK/B02.sql"
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "  ok   $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
echo "project-state-seed (${BASELINE:-working tree})"

# The registration function, and only it.
FN="$(sed -n '/^register_messaging_mcp() {/,/^}/p' "$WORK/dispatcher.sh")"

grep -q -- '--slug      "project-state"' <<< "$FN" \
    && pass "S1: the project-state MCP server is registered with the installer" \
    || fail "S1: register_messaging_mcp does not register slug project-state"
grep -q 'mcp_project_state.py' <<< "$FN" \
    && pass "S2: it points at mcp_project_state.py in the install dir" \
    || fail "S2: no mcp_project_state.py in the registration"

# The contract text reaches the tool row: run the jq step on the real heredoc.
sed -n "/<<'USAGE_PS'/,/^USAGE_PS/p" <<< "$FN" | sed '1d;$d' > "$WORK/ps.md"
if [[ -s "$WORK/ps.md" ]] && grep -q 'project_state(' "$WORK/ps.md"; then
    pass "S3: the project-state contract text is in the installer ($(wc -c < "$WORK/ps.md") bytes)"
else
    fail "S3: no project-state contract text in the installer"
fi
if grep -q 'name:"project-state"' <<< "$FN" && grep -q 'usage_md:\$p' <<< "$FN"; then
    pass "S4: the tool row carries that text as usage_md"
else
    fail "S4: the project-state tool row has no usage_md"
fi

grep -q 'CREATE INDEX IF NOT EXISTS idx_sessions_conversation' "$WORK/B02.sql" \
    && pass "S5: bootstrap creates idx_sessions_conversation (IF NOT EXISTS)" \
    || fail "S5: bootstrap does not create idx_sessions_conversation"

echo
if (( FAIL == 0 )); then echo "PASS  $PASS/$((PASS+FAIL)) check(s) passed  [project-state-seed]"; exit 0; fi
echo "FAIL  $FAIL/$((PASS+FAIL)) check(s) failed  [project-state-seed]"; exit 1
