#!/usr/bin/env bash
# The permission surface is applied once, at the end of a full install (Phase 2.9),
# and not again after every companion. The per-companion pass cost about a minute
# apiece on a node with large agent homes and repeated what 2.9 already enforces.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
f="$repo/orchestrator/install-on.sh"

# run_companion() body: from its definition to the closing brace at column 0
body="$(awk '/^run_companion\(\) \{/{on=1} on{print} on && /^\}/{exit}' "$f")"
[ -n "$body" ] || { echo "FAIL: run_companion() not found"; exit 1; }

if printf '%s\n' "$body" | grep -q 'permissions\.sh'; then
    echo "FAIL: run_companion calls permissions.sh again after each companion"
    exit 1
fi

# the closing pass must still exist, be fatal, and sit before validate
close_line="$(grep -n 'die "permissions stage refused' "$f" | head -1 | cut -d: -f1)"
validate_line="$(grep -n '=== Phase 3 (validate) ===' "$f" | head -1 | cut -d: -f1)"
[ -n "$close_line" ] || { echo "FAIL: Phase 2.9 permissions pass is gone"; exit 1; }
[ -n "$validate_line" ] || { echo "FAIL: Phase 3 marker not found"; exit 1; }
if [ "$close_line" -ge "$validate_line" ]; then
    echo "FAIL: the permissions pass must run before validate"
    exit 1
fi
sed -n "${close_line}p" "$f" | grep -q ' 7$' || { echo "FAIL: the closing pass is no longer fatal (exit 7)"; exit 1; }

echo "permissions-once: PASS"
