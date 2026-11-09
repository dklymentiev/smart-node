#!/usr/bin/env bash
# update-on --target must be rejected before any node preflight. The updater
# deploys its local tree; pretending it can select another release is unsafe.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

UPDATE="$REPO/tools/update-on.sh"
if [[ -n "$BASELINE" ]]; then
    D="$(mktemp -d)"
    trap 'rm -rf "$D"' EXIT
    UPDATE="$D/update-on.sh"
    git -C "$REPO" show "$BASELINE:tools/update-on.sh" >"$UPDATE"
    chmod +x "$UPDATE"
    echo "update target contract (git:$BASELINE)"
else
    echo "update target contract (working tree)"
fi

set +e
output="$(bash "$UPDATE" --target 1.12.44 2>&1)"
rc=$?
set -e

if [[ "$rc" -eq 2 ]] && grep -q -- '--target is not supported' <<<"$output"; then
    ok "--target is refused explicitly during argument parsing"
else
    bad "--target is refused explicitly during argument parsing"
    printf '       rc=%s output=%q\n' "$rc" "$output"
fi

if grep -q 'deploys the current local checkout' <<<"$output" \
   && grep -q 'check out the desired release tag' <<<"$output"; then
    ok "the refusal explains how release selection actually works"
else
    bad "the refusal explains how release selection actually works"
fi

help="$(bash "$UPDATE" --help)"
if grep -q 'deploys the current local checkout' <<<"$help" \
   && ! grep -q -- '--target VERSION' <<<"$help"; then
    ok "help no longer promises an unimplemented selector"
else
    bad "help no longer promises an unimplemented selector"
fi

if ! grep -q 'INSTALL_ARGS+=("--target"' "$UPDATE"; then
    ok "the updater never forwards an installer flag that does not exist"
else
    bad "the updater never forwards an installer flag that does not exist"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo PASS; else echo "FAIL ($FAILED)"; fi
exit $((FAILED > 0))
