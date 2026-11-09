#!/usr/bin/env bash
#
# Gate: the direct-Google Gemini backend is present in
# every source layer that decides whether a Gateway session with provider=gemini
# can be routed at all.
#
# WHAT IS CHECKED
#   1. bootstrap/B02-sn-core-seed.sql seeds sn_config.model_pricing with
#      ('gemini','gemini-flash-latest','input_tokens', ...)
#   2. Same file seeds the matching output_tokens row.
#   3. Same file seeds sn_config.models with ('gemini','gemini-flash-latest', ...).
#   4. companions/gateway/config.py ALLOWED_MODELS['gemini'] contains
#      'gemini-flash-latest' (the whitelist gate that runs before the backend).
#   5. companions/gateway/backends/http_google.py exists and declares
#      provider_name = "gemini".
#   6. companions/gateway/backends/__init__.py resolve_backend() dispatches
#      upstream == "gemini" to the GoogleBackend (not the raise-ValueError path).
#
# Baseline behaviour (before Track 1): every one of these fails, and the
# resolve_backend line for 'gemini' is absent -- proof the branch is what makes
# provider=gemini reachable.
#
# USAGE
#   tests/gateway-backend-gemini.test.sh                  # working tree
#   tests/gateway-backend-gemini.test.sh --baseline REF   # expect FAIL on main
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
    echo "gateway backend gemini (git:$BASELINE)"
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    git -C "$ROOT" show "$BASELINE:bootstrap/B02-sn-core-seed.sql" > "$WORK/seed.sql" 2>/dev/null || true
    git -C "$ROOT" show "$BASELINE:companions/gateway/config.py" > "$WORK/config.py" 2>/dev/null || true
    git -C "$ROOT" show "$BASELINE:companions/gateway/backends/__init__.py" > "$WORK/backends_init.py" 2>/dev/null || true
    # http_google.py is expected to NOT exist on baseline; capture its content if any.
    git -C "$ROOT" show "$BASELINE:companions/gateway/backends/http_google.py" > "$WORK/http_google.py" 2>/dev/null || true
    SEED="$WORK/seed.sql"
    CONFIG="$WORK/config.py"
    INIT="$WORK/backends_init.py"
    HTTP="$WORK/http_google.py"
else
    echo "gateway backend gemini (working tree)"
    SEED="$ROOT/bootstrap/B02-sn-core-seed.sql"
    CONFIG="$ROOT/companions/gateway/config.py"
    INIT="$ROOT/companions/gateway/backends/__init__.py"
    HTTP="$ROOT/companions/gateway/backends/http_google.py"
fi

# 1. model_pricing input_tokens row
if grep -qE "'gemini',[[:space:]]+'gemini-flash-latest',[[:space:]]+'input_tokens'" "$SEED"; then
    ok "B02-sn-core-seed.sql seeds model_pricing input_tokens for gemini/gemini-flash-latest"
else
    bad "B02-sn-core-seed.sql missing model_pricing input_tokens for gemini/gemini-flash-latest"
fi

# 2. model_pricing output_tokens row
if grep -qE "'gemini',[[:space:]]+'gemini-flash-latest',[[:space:]]+'output_tokens'" "$SEED"; then
    ok "B02-sn-core-seed.sql seeds model_pricing output_tokens for gemini/gemini-flash-latest"
else
    bad "B02-sn-core-seed.sql missing model_pricing output_tokens for gemini/gemini-flash-latest"
fi

# 3. sn_config.models row (a chat modality entry with provider=gemini and matching model)
if grep -qE "'gemini',[[:space:]]+'gemini-flash-latest',[[:space:]]+'Gemini" "$SEED"; then
    ok "B02-sn-core-seed.sql seeds sn_config.models chat row for gemini/gemini-flash-latest"
else
    bad "B02-sn-core-seed.sql missing sn_config.models chat row for gemini/gemini-flash-latest"
fi

# 4. ALLOWED_MODELS['gemini'] includes gemini-flash-latest.
# Grep the whole config.py: the set literal for 'gemini' must contain the string.
if awk '/"gemini":[[:space:]]*\{/,/\}/' "$CONFIG" | grep -q '"gemini-flash-latest"'; then
    ok "config.py ALLOWED_MODELS['gemini'] includes 'gemini-flash-latest'"
else
    bad "config.py ALLOWED_MODELS['gemini'] does NOT include 'gemini-flash-latest'"
fi

# 5. http_google.py exists and declares provider_name = "gemini"
if [[ -s "$HTTP" ]] && grep -q 'provider_name = "gemini"' "$HTTP"; then
    ok "backends/http_google.py exists and declares provider_name = \"gemini\""
else
    bad "backends/http_google.py missing or has no provider_name = \"gemini\""
fi

# 6. resolve_backend dispatches 'gemini' to GoogleBackend
if grep -qE 'upstream[[:space:]]*==[[:space:]]*"gemini"' "$INIT" \
        && grep -q 'from backends.http_google import GoogleBackend' "$INIT"; then
    ok "backends/__init__.py resolve_backend() dispatches upstream 'gemini' to GoogleBackend"
else
    bad "backends/__init__.py resolve_backend() does NOT dispatch upstream 'gemini' to GoogleBackend"
fi

echo
if [[ $FAILED -eq 0 ]]; then
    echo "PASS (6 checks)"
    exit 0
else
    echo "FAIL ($FAILED failure(s))"
    exit 1
fi
