#!/usr/bin/env bash
# documents.sh: the doci-mcp checks look where the unit was told to listen. Loopback unless the
# node's own override (/etc/smart-node/local/smart-node-doci-mcp.env) sets DOCI_MCP_HOST; a node
# that routes doci-mcp to its VPN binds 0.0.0.0 that way, and its update used to stop there.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/companions/documents.sh"
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

listen_for() {  # $1 = the override file's content ("" = no file)
    rm -f "$T/o.env"; [ -n "$1" ] && printf '%s\n' "$1" > "$T/o.env"
    (
        set -euo pipefail   # as documents.sh runs: an absent file must not end it
        DOCI_MCP_UNIT="smart-node-doci-mcp.service"
        eval "$(grep -E '^DOCI_MCP_LISTEN=' "$S" | sed "s#\"\$DOCI_MCP_LOCAL_ENV\"#\"$T/o.env\"#")"
        echo "$DOCI_MCP_LISTEN"
    )
}
check "no override: loopback" "$(listen_for "")" "127.0.0.1"
check "an override without the host: loopback" "$(listen_for "OTHER=1")" "127.0.0.1"
check "the override binds all addresses" "$(listen_for "DOCI_MCP_HOST=0.0.0.0")" "0.0.0.0"
check "quoted the way the runtime env is written" "$(listen_for "DOCI_MCP_HOST='0.0.0.0'")" "0.0.0.0"
check "double quotes too" "$(listen_for 'DOCI_MCP_HOST="10.0.0.5"')" "10.0.0.5"
grep -q 'grep -qF "${DOCI_MCP_LISTEN}:${DOCI_MCP_PORT} "' "$S"; check "the readiness wait looks there" "$?" 0
grep -q 'listens on loopback port $DOCI_MCP_PORT only' "$S"; check "without an override the loopback-only check stays" "$?" 0
exit "$fail"
