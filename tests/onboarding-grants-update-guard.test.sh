#!/bin/bash
# Regression contract for onboarding grants across updates.
# Run against another checkout with SMART_NODE_SOURCE_ROOT=/path/to/tree.

set -uo pipefail

SELF_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${SMART_NODE_SOURCE_ROOT:-$SELF_ROOT}"
SEED="$ROOT/bootstrap/onboarding-seed.sh"
AGENTS="$ROOT/companions/core-api/routes/agents.py"
INSTALLER="$ROOT/orchestrator/install-on.sh"
UPDATER="$ROOT/tools/update-on.sh"
SNAPSHOT="$ROOT/tools/grants-snapshot.py"
DIFF="$ROOT/tools/grants-diff.py"
RETURNING_USER="$ROOT/tools/acceptance/returning-user.py"

pass=0
fail=0
ok() { echo "ok - $1"; pass=$((pass + 1)); }
bad() { echo "not ok - $1"; fail=$((fail + 1)); }
has() { if grep -Eq -- "$2" "$1" 2>/dev/null; then ok "$3"; else bad "$3"; fi; }
lacks() { if grep -Eq -- "$2" "$1" 2>/dev/null; then bad "$3"; else ok "$3"; fi; }

# ordinary seed is non-destructive; replacement is an explicit action.
has "$SEED" 'grants\?merge=true' \
    "ordinary onboarding grants use merge semantics"
has "$SEED" 'SN_ONBOARD_FORCE_RESEED' \
    "force-reseed is explicit inside the seed"
has "$INSTALLER" '--force-onboarding-reseed' \
    "node installer exposes force-reseed separately from --force"
has "$UPDATER" '--force-onboarding-reseed' \
    "update path forwards explicit force-reseed"
has "$AGENTS" 'merge:.*Query\(False\)' \
    "grants PUT exposes an explicit merge operation"
has "$AGENTS" '_merge_grant_modes|merge_grant_modes' \
    "mode merge is capability-aware rather than last-write-wins"

# Only inspect the grants endpoint body: other historical routes may still have
# their own attribution debt, but this pair must stop minting user:panel events.
python3 - "$AGENTS" <<'PY'
import ast, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    src = p.read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "put_grants")
    body = ast.get_source_segment(src, fn) or ""
except Exception:
    body = ""
if "get_actor(" in body and 'actor="user:panel"' not in body:
    print("ok - grants events use the resolved caller rather than user:panel")
else:
    print("not ok - grants events use the resolved caller rather than user:panel")
    raise SystemExit(1)
PY
if (( $? == 0 )); then pass=$((pass + 1)); else fail=$((fail + 1)); fi

# both state sources are captured and the production update path gates
# on the diff. The helpers are independently executable with fixture snapshots.
has "$SNAPSHOT" 'sn_config\.agent_tools' \
    "snapshot reads effective DB tool capabilities"
has "$SNAPSHOT" '\.mcp\.json' \
    "snapshot reads rendered MCP server names"
has "$UPDATER" 'grants-snapshot\.py' \
    "update-on captures the grants snapshot before and after install"
has "$UPDATER" 'grants-diff\.py' \
    "update-on runs the capability diff"
has "$UPDATER" '--accept-grants-change' \
    "intentional narrowing requires an explicit reason"
has "$RETURNING_USER" 'extra_grant_(tool|mode)' \
    "acceptance stand carries an extra grant across the update"

TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
cat > "$TMPD/before.json" <<'JSON'
{"agents":{"foreman":{"grants":{"operator":"rw-","planner":"rwd"},"mcp_servers":["operator","planner"]}}}
JSON
cat > "$TMPD/same.json" <<'JSON'
{"agents":{"foreman":{"grants":{"operator":"rw-","planner":"rwd"},"mcp_servers":["operator","planner"]}}}
JSON
cat > "$TMPD/add.json" <<'JSON'
{"agents":{"foreman":{"grants":{"operator":"rw-","planner":"rwd","mesh":"r--"},"mcp_servers":["mesh","operator","planner"]}}}
JSON
cat > "$TMPD/narrow.json" <<'JSON'
{"agents":{"foreman":{"grants":{"planner":"rw-"},"mcp_servers":["planner"]}}}
JSON

if [[ -f "$DIFF" ]] && python3 "$DIFF" "$TMPD/before.json" "$TMPD/same.json" >/dev/null; then
    ok "unchanged capabilities pass"
else
    bad "unchanged capabilities pass"
fi
if [[ -f "$DIFF" ]] && python3 "$DIFF" "$TMPD/before.json" "$TMPD/add.json" >/dev/null; then
    ok "additions and widening pass"
else
    bad "additions and widening pass"
fi
if [[ -f "$DIFF" ]] && python3 "$DIFF" "$TMPD/before.json" "$TMPD/narrow.json" >"$TMPD/narrow.out" 2>&1; then
    bad "unaccepted narrowing fails"
elif grep -q 'foreman.*operator' "$TMPD/narrow.out" 2>/dev/null; then
    ok "unaccepted narrowing fails and names agent plus tool"
else
    bad "unaccepted narrowing fails and names agent plus tool"
fi
if [[ -f "$DIFF" ]] && python3 "$DIFF" --accept-reason "release removes operator" \
        "$TMPD/before.json" "$TMPD/narrow.json" >"$TMPD/accepted.out" 2>&1 \
        && grep -q 'release removes operator' "$TMPD/accepted.out"; then
    ok "intentional narrowing passes only with its recorded reason"
else
    bad "intentional narrowing passes only with its recorded reason"
fi

echo "$pass passed, $fail failed"
(( fail == 0 ))
