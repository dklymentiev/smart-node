#!/usr/bin/env bash
# Validation must name companions omitted by an --all install instead of
# silently shrinking its denominator and reporting an unqualified full pass.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
D="$(mktemp -d)"
trap 'rm -rf "$D"' EXIT

ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else sed -n '1,$p' "$REPO/$1" 2>/dev/null; fi
}
has() {
    local text="$1" pattern="$2" label="$3"
    if grep -Eq "$pattern" <<<"$text"; then ok "$label"; else bad "$label"; fi
}

if [[ -n "$BASELINE" ]]; then
    echo "validate omitted companions (git:$BASELINE)"
else
    echo "validate omitted companions (working tree)"
fi

INSTALL="$(read_src orchestrator/install-on.sh)"
VALIDATE="$(read_src validate.sh)"

has "$INSTALL" 'COMPANIONS_ABSENT=\(\)' \
    "the installer tracks intentionally absent companions separately"
has "$INSTALL" 'COMPANIONS_ABSENT\+?=\("?communication"?\)' \
    "a fresh no-channel install records communication as absent"
has "$INSTALL" 'COMPANIONS_ABSENT\+?=\("?voice"?\)' \
    "a manifest-disconnected voice companion is also recorded"
has "$INSTALL" 'validation-scope\.env' \
    "the selected and absent sets are persisted for later validation"

has "$VALIDATE" 'load_validation_scope' \
    "validate loads the durable scope instead of inferring it from artifacts"
has "$VALIDATE" 'selected companion stage .* missing' \
    "a selected companion whose state vanished is a validation failure"
has "$VALIDATE" 'not installed and not checked' \
    "the report names omitted companions outside its denominator"
has "$VALIDATE" '\[COVERAGE\].*not installed' \
    "the interactive run cannot present a scoped pass silently"

printf '%s\n' "$VALIDATE" >"$D/validate.sh"
if bash -s "$D/validate.sh" "$D" <<'SH'
# -e: every [[ ]] below is an assertion, not only the last one.
set -euo pipefail
source_file="$1"
fixture="$2"

_envfile_get() {
    sed -n -E "s/^${2}=//p" "$1" | tail -n1
}
verify_check() {
    local _label="$1"; shift
    VERIFY_TOTAL=$((VERIFY_TOTAL + 1))
    "$@" || VERIFY_ERRORS=$((VERIFY_ERRORS + 1))
}

eval "$(sed -n '/^load_validation_scope()/,/^load_done_stages()/p' "$source_file" | sed '$d')"
eval "$(sed -n '/^check_stage_states()/,/^# ---------- check group 2/p' "$source_file" | sed '$d')"
declare -F load_validation_scope >/dev/null || exit 1
declare -F check_stage_states >/dev/null || exit 1

VALIDATION_SCOPE="$fixture/scope.env"
SMART_NODE_STATE_DIR="$fixture/stages"
STAGE_ID=validate
mkdir -p "$SMART_NODE_STATE_DIR"
printf 'status=done\n' >"$SMART_NODE_STATE_DIR/B01.state"
printf '%s\n' \
    'SN_VALIDATION_SELECTED_STAGES=memory' \
    'SN_VALIDATION_OMITTED_STAGES=communication' >"$VALIDATION_SCOPE"

VALIDATION_SELECTED_STAGES=()
VALIDATION_OMITTED_STAGES=()
VALIDATION_SCOPE_KNOWN=0
load_validation_scope
[[ "$VALIDATION_SCOPE_KNOWN" -eq 1 ]]
[[ "${VALIDATION_SELECTED_STAGES[*]}" == memory ]]
[[ "${VALIDATION_OMITTED_STAGES[*]}" == communication ]]

VERIFY_TOTAL=0
VERIFY_ERRORS=0
check_stage_states 2>/dev/null
[[ "$VERIFY_TOTAL" -eq 2 && "$VERIFY_ERRORS" -eq 1 ]]

printf 'status=done\n' >"$SMART_NODE_STATE_DIR/memory.state"
VERIFY_TOTAL=0
VERIFY_ERRORS=0
check_stage_states 2>/dev/null
[[ "$VERIFY_TOTAL" -eq 2 && "$VERIFY_ERRORS" -eq 0 ]]
SH
then
    ok "fixture proves a selected missing stage fails while an omitted one is reported, not required"
else
    bad "fixture proves a selected missing stage fails while an omitted one is reported, not required"
fi

# The installer's own log tells the operator to install communication from the
# B06 panel later. That run does not rewrite validation-scope.env, so the file
# still lists communication as omitted while its stage is done and checked.
D2="$D/later-install"
mkdir -p "$D2"
if bash -s "$D/validate.sh" "$D2" <<'SH'
set -euo pipefail
source_file="$1"
fixture="$2"

_envfile_get() {
    sed -n -E "s/^${2}=//p" "$1" | tail -n1
}

eval "$(sed -n '/^load_validation_scope()/,/^# ---------- check group 1/p' "$source_file" | sed '$d')"
declare -F load_validation_scope >/dev/null || exit 1
declare -F load_done_stages >/dev/null || exit 1
declare -F drop_installed_from_omitted >/dev/null || exit 1

VALIDATION_SCOPE="$fixture/scope.env"
SMART_NODE_STATE_DIR="$fixture/stages"
mkdir -p "$SMART_NODE_STATE_DIR"
printf 'status=done\n' >"$SMART_NODE_STATE_DIR/B01.state"
printf 'status=done\n' >"$SMART_NODE_STATE_DIR/communication.state"
printf '%s\n' \
    'SN_VALIDATION_SELECTED_STAGES=memory' \
    'SN_VALIDATION_OMITTED_STAGES=communication,voice' >"$VALIDATION_SCOPE"

declare -A DONE_STAGES=()
VALIDATION_SELECTED_STAGES=()
VALIDATION_OMITTED_STAGES=()
VALIDATION_SCOPE_KNOWN=0
load_validation_scope
load_done_stages
drop_installed_from_omitted
[[ "${VALIDATION_OMITTED_STAGES[*]}" == voice ]]
SH
then
    ok "a companion installed after the installer ran is not reported as unchecked"
else
    bad "a companion installed after the installer ran is not reported as unchecked"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo PASS; else echo "FAIL ($FAILED)"; fi
exit $((FAILED > 0))
