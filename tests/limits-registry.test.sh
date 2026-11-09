#!/usr/bin/env bash
# The three owner-visible limits must resolve through the same durable registry.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP=""
if [[ "${1:-}" == "--baseline" ]]; then
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    git -C "$ROOT" archive "${2:-origin/main}" bootstrap companions tests | tar -x -C "$TMP" || exit 2
    ROOT="$TMP"
fi
fail=0
check() {
    if grep -qE -- "$3" "$ROOT/$2" 2>/dev/null; then
        echo "ok   $1"
    else
        echo "FAIL $1"
        fail=$((fail+1))
    fi
}
check 'limit definitions survive install and update' bootstrap/B02-sn-core-schema.sql 'sn_config.limit_defs'
check 'limit profiles survive install and update' bootstrap/B02-sn-core-schema.sql 'sn_config.limit_profiles'
check 'the seed carries old money values' bootstrap/B02-sn-core-seed.sql 'FROM sn_config.budgets'
if grep -qE "\('spend_usd', 'default'" "$ROOT/bootstrap/B02-sn-core-seed.sql"; then
    echo 'FAIL an unconfigured node gains an unrequested agent spending cap'
    fail=$((fail+1))
else
    echo 'ok   an unconfigured node has no automatic agent spending cap'
fi
check 'one supervisor resolves an effective agent profile' companions/core-api/lib/supervisor.py 'def resolve_profile'
check 'owner can manage limits through API' companions/core-api/routes/limits.py 'APIRouter'
check 'owner can set the missing spending default' companions/core-api/routes/limits.py '"missing": True'
check 'admin has a Limits page' bootstrap/smart-node-ui/src/limits/index.html 'nodePause'
check 'agent has a Limits tab' bootstrap/smart-node-ui/src/agents/edit/index.html 'data-tab="limits"'
check 'turn ceiling reads a profile' companions/gateway/backends/cli_claude.py 'turn_limit_seconds'
check 'wake cap reads a profile' companions/core-api/routes/agents.py 'resolve_profile'
check 'runner checks node pause' companions/core-api/routes/agents.py 'node_paused'
check 'turn cutoff is recorded' companions/gateway/sessions_v2_routes.py 'record_turn_cutoff'
echo "$fail failure(s)"
(( fail == 0 ))
