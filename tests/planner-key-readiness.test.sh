#!/usr/bin/env bash
# Exercise the installer's actual key probe with a local curl stub. No node or
# planner process is touched. --baseline REF demonstrates the old one-shot gap.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [[ "${1:-}" == "--baseline" ]]; then
    git -C "$ROOT" show "${2:?baseline ref}:companions/tracking.sh" > "$TMP/tracking.sh"
else
    cp "$ROOT/companions/tracking.sh" "$TMP/tracking.sh"
fi
awk '/^planner_accepts_core_api_key\(\) \{/{f=1} f{print; if ($0 == "}") exit}' \
    "$TMP/tracking.sh" > "$TMP/probe.sh"
[[ -s "$TMP/probe.sh" ]] || { echo 'FAIL planner key readiness probe missing'; exit 1; }
printf '%s\n' 'PLANNER_API_KEY=test-fixture-key' > "$TMP/key.env"
date +%s > "$TMP/start"
cat > "$TMP/curl" <<'SH'
#!/usr/bin/env bash
if [[ "$STUB_SCENARIO" == refused ]]; then
    printf 401
elif (( $(date +%s) - $(cat "$STUB_START") >= 6 )); then
    printf 200
else
    printf 000
fi
SH
chmod +x "$TMP/curl"
export PATH="$TMP:$PATH" STUB_START="$TMP/start"
export KEY_ENV_CORE_API="$TMP/key.env" PLANNER_HTTP_PORT=8080
export STUB_SCENARIO=late
source "$TMP/probe.sh"
planner_accepts_core_api_key
[[ "$PLANNER_KEY_LAST_HTTP_CODE" == 200 ]]
echo '  ok   planner answering after six seconds passes'
export STUB_SCENARIO=refused
if planner_accepts_core_api_key; then
    echo 'FAIL revoked key passed'
    exit 1
fi
[[ "$PLANNER_KEY_LAST_HTTP_CODE" == 401 ]]
echo '  ok   revoked key fails and reports HTTP 401'
grep -Fq 'echo "[VERIFY-FAIL] planner core-api key probe: HTTP ${PLANNER_KEY_LAST_HTTP_CODE:-000}"' \
    "$TMP/tracking.sh"
echo '  ok   installer exposes the failed probe code'
