#!/usr/bin/env bash
#
# The configurator is told how models work, in the contract every node installs.
#
# WHAT WENT WRONG. The node-admin contract showed one example, "model":"claude-cli/sonnet",
# and said nothing else about models. John concluded the runner was pinned to the Claude
# CLI, told the owner so, and then tried openai/gpt-4o, a provider the gateway does not
# have. The owner asked for the instructions to be fixed and shipped to every node.
#
# CHECKED in companions/tracking.sh (the node-admin usage_md) and docs/node/entities.md:
#   shape       model is <provider>/<model>, GPT and the rest through openrouter/<vendor>
#   look        the contract names GET /api/models/providers
#   add         the contract names POST /api/models
#   key         a missing key is the owner's to give; never pasted anywhere
#   entities    entities.md says models live in sn_config.models and can be added
#
#   tests/node-admin-models-contract.test.sh [--baseline REF]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
rd() {
    if [[ -n "$BASELINE" ]]; then git -C "$ROOT" show "$BASELINE:$1" 2>/dev/null
    else cat "$ROOT/$1"; fi
}
echo "node-admin models contract (${BASELINE:+git:}${BASELINE:-working tree})"
usage="$(rd companions/tracking.sh | sed -n '/^## node-admin -- create and wire/,/^USAGE$/p')"
ent="$(rd docs/node/entities.md | grep '^| \*\*Model\*\*')"

F=0
chk() {  # chk <name> <needle> <haystack> [<needle2>]
    local name="$1" hay="$3"
    if grep -qF -- "$2" <<<"$hay" && { [[ -z "${4:-}" ]] || grep -qF -- "$4" <<<"$hay"; }; then
        echo "  ok   $name"
    else
        echo "  FAIL $name"; F=$((F + 1))
    fi
}
chk shape    '<provider>/<model>'                      "$usage" 'openrouter/<vendor>/<model>'
chk look     'GET  __CORE_API_URL__/api/models/providers' "$usage"
chk add      'POST __CORE_API_URL__/api/models'         "$usage"
chk key      'never paste'                              "$usage"
chk entities 'sn_config.models'                         "$ent" 'POST /api/models'

if [[ $F -eq 0 ]]; then echo "PASS: 5 checks"; exit 0; fi
echo "FAIL: $F of 5"; exit 1
