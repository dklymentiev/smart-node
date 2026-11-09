#!/usr/bin/env bash
#
# Gate: companions/core-api/seed-tools.py grants the node_summary tool by
# capability, not by literal slug.
#
# every seed that looks up an agent to grant a tool
# or a role must query the capability column (immutable, product-role marker),
# not the mutable slug. Slug lookups break on any node where the owner has
# renamed the agent.
#
# WHAT IS CHECKED
#   1. seed-tools.py node_summary grant clause does NOT contain a literal
#      "a.slug = 'foreman'" predicate.
#   2. seed-tools.py node_summary grant clause DOES contain a
#      "a.capability = 'configurator'" predicate.
#
# USAGE
#   tests/seed-tools-capability-lookup.test.sh              # working tree
#   tests/seed-tools-capability-lookup.test.sh --baseline REF
#
set -uo pipefail

_SCRIPT="$(realpath -q "${BASH_SOURCE[0]}" 2>/dev/null || readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$_SCRIPT")/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

if [[ -n "$BASELINE" ]]; then
    echo "seed-tools node_summary grant: capability lookup (git:$BASELINE)"
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    git -C "$ROOT" show "$BASELINE:companions/core-api/seed-tools.py" > "$WORK/seed-tools.py" 2>/dev/null || true
    TARGET="$WORK/seed-tools.py"
else
    echo "seed-tools node_summary grant: capability lookup (working tree)"
    TARGET="$ROOT/companions/core-api/seed-tools.py"
fi

if [[ ! -s "$TARGET" ]]; then
    bad "seed-tools.py is missing or empty at this ref"
    echo
    [[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
    exit "$FAILED"
fi

# Extract the node_summary grant INSERT block. seed-tools.py may contain
# several INSERT INTO sn_config.agent_tools blocks; select the one whose
# WHERE clause names t.name = 'node_summary'.
GRANT_BLOCK="$(awk '
    /INSERT INTO sn_config\.agent_tools/ {
        buf = $0 "\n"
        inblock = 1
        matched = 0
        next
    }
    inblock {
        buf = buf $0 "\n"
        if ($0 ~ /t\.name[[:space:]]*=[[:space:]]*'"'"'node_summary'"'"'/) {
            matched = 1
        }
        if ($0 ~ /ON CONFLICT \(agent_id, tool_id\) DO NOTHING/) {
            if (matched) {
                printf "%s", buf
                exit
            }
            inblock = 0
            buf = ""
            matched = 0
        }
    }
' "$TARGET")"

if [[ -z "$GRANT_BLOCK" ]]; then
    bad "cannot locate node_summary grant INSERT block in seed-tools.py"
    echo
    [[ $FAILED -eq 0 ]] && echo "PASS" || echo "FAIL ($FAILED)"
    exit "$FAILED"
fi

# ---- 1. No literal slug lookup ----
if printf '%s\n' "$GRANT_BLOCK" | grep -Eq "a\.slug[[:space:]]*=[[:space:]]*'foreman'"; then
    bad "node_summary grant uses literal a.slug = 'foreman'"
else
    ok "node_summary grant does not use literal a.slug = 'foreman'"
fi

# ---- 2. Capability lookup present ----
if printf '%s\n' "$GRANT_BLOCK" | grep -Eq "a\.capability[[:space:]]*=[[:space:]]*'configurator'"; then
    ok "node_summary grant uses a.capability = 'configurator'"
else
    bad "node_summary grant missing a.capability = 'configurator' predicate"
fi

echo
if [[ $FAILED -eq 0 ]]; then
    echo "PASS"
else
    echo "FAIL ($FAILED)"
fi
exit "$FAILED"
