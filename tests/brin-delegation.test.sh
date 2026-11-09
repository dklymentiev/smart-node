#!/usr/bin/env bash
# brin delegates by the address a tool call goes to, not by a slug.
set -uo pipefail
ROOT="${SMART_NODE_SOURCE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
F="$ROOT/companions/brin/core/catalog_tools.py"
pass=0; fail=0
ok() { echo "ok - $1"; pass=$((pass+1)); }
no() { echo "not ok - $1"; fail=$((fail+1)); }
ck() { if grep -q -- "$2" "$F" 2>/dev/null; then ok "$1"; else no "$1"; fi; }
ck "the decision is a named question, not an inline literal" "_is_core_api"
ck "it compares the address" "urlsplit"
ck "the slug still works when a row has no endpoint" "_CORE_API_INTEGRATION"
if grep -A2 "if SERVICE_SECRET" "$F" 2>/dev/null | grep -q "_is_core_api"; then ok "the header attaches on that question"; else no "the header attaches on that question"; fi
if grep -q "row.get(\"integration_slug\") == _CORE_API_INTEGRATION:" "$F" 2>/dev/null && ! grep -q "_is_core_api" "$F" 2>/dev/null; then no "the slug is no longer the only test"; else ok "the slug is no longer the only test"; fi
echo; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
