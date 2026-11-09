#!/usr/bin/env bash
#
# A companion re-run must keep what it was not told about.
#
# WHAT WENT WRONG. companions/communication.sh writes .env WHOLESALE on every
# run, from SMART_NODE_* inputs. A key the run was not given was therefore not
# left alone -- it was deleted. So the supported way to add a second channel
# destroyed the first: re-running with SMART_NODE_TG_TOKEN to add telegram
# stripped IMAP_HOST, SMTP_HOST, the egress allowlist and AGENT_ACCOUNTS_FILE.
# The node kept running. Mail simply stopped being read, and nothing said so.
#
# The same edge took CONTACT_GATE back to `off` and emptied the outbound
# allowlist -- a re-run silently undoing a security decision the operator made
# once. That half was not in the original report and is the worse one.
#
# This is why the two communication fixes in 1.12.19 are on
# neither of our nodes: passing --mail-host/--tg-token blind to a live node
# would have deleted its wiring, so the companion was skipped on both.
#
# WHAT IS CHECKED. Not the source -- the behaviour. Each case seeds a .env the
# way an installed node has one, sources the companion with stubs standing in
# for lib.sh, runs the SAME resolution run_intent performs, calls the real
# write_env, and reads what it wrote:
#
#   add-telegram  an email-only node gains telegram and keeps all of mail,
#                 the gate, the allowlist, the interval and the business name
#   reverse       a telegram-only node gains mail and keeps telegram
#   fresh         a node with no .env wires ONLY what this run supplies --
#                 a first install must not change at all
#   clear         an input set-but-empty still clears: carrying forward must
#                 not make a deliberate blanking impossible
#
#   tests/env-carry-forward.test.sh                 # working tree, expect PASS
#   tests/env-carry-forward.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REL="companions/communication.sh"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:$REL" > "$WORK/variant-src.sh" 2>/dev/null \
        || { echo "cannot read $REL at $BASELINE" >&2; exit 2; }
    WHAT="$BASELINE"
else
    cp "$ROOT/$REL" "$WORK/variant-src.sh"
    WHAT="working tree"
fi

fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

echo "$REL ($WHAT)"

# --- the stubs write_env reaches, standing in for lib.sh --------------------
# Quoted heredoc on purpose: the body is shell, and an unquoted one would
# expand it here instead of there ( for what that costs).
cat > "$WORK/lib.sh" <<'STUB'
log() { :; }
die() { echo "DIE: $*" >&2; exit 1; }
manifest_record() { :; }
_envfile_get() {
    local file="$1" key="$2"
    [[ -r "$file" ]] || return 0
    sed -n -E "s/^${key}=//p" "$file" | tail -n1
}
# install(1) minus the ownership flags -- the test is not root.
install() {
    local args=()
    while (($#)); do
        case "$1" in -o|-g) shift 2 ;; *) args+=("$1"); shift ;; esac
    done
    command install "${args[@]}"
}
STUB

# --- the runner: one scenario, in its own shell -----------------------------
cat > "$WORK/runner.sh" <<'RUNNER'
#!/usr/bin/env bash
# runner.sh <variant-src> <lib-stub> <case>  -- prints the .env write_env made.
set -uo pipefail
SRC="$1"; STUB="$2"; CASE="$3"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
mkdir -p "$W/communication" "$W/dispatcher"

cat > "$W/dispatcher/.env" <<'EOF'
BRIN_API_KEY=test-brin-key
PORT=8101
HOST=172.17.0.1
EOF

case "$CASE" in
  add-telegram)
    cat > "$W/communication/.env" <<'EOF'
SN_BUSINESS_NAME=Test Distillery
DISPATCH_INTERVAL=7
FAST_CHANNELS=email
IMAP_HOST=imap.example-co.com
IMAP_PORT=993
SMTP_HOST=smtp.example-co.com
SMTP_PORT=587
IMAP_USER=node@example-co.com
IMAP_PASSWORD=nodepw
EMAIL_TLS_VERIFY=1
EMAIL_ALLOWED_DOMAINS=example-co.com
EMAIL_ALLOWED_ADDRESSES=owner@example-co.com
AGENT_ACCOUNTS_FILE=/opt/smart-node/mail/agent-accounts.env
CONTACT_GATE=on
CONTACT_GATE_INBOUND=on
EOF
    export SMART_NODE_TG_TOKEN="123:test-token"
    export SMART_NODE_TG_CHANNEL_ID="-1001234567890"
    ;;
  reverse)
    cat > "$W/communication/.env" <<'EOF'
TG_TOKEN=999:already-here
TG_CHANNEL_ID=-100999
FAST_CHANNELS=telegram
CONTACT_GATE=on
EOF
    export SMART_NODE_MAIL_HOST="mail.example-co.com"
    export SMART_NODE_MAIL_ACCOUNT="node@example-co.com"
    export SMART_NODE_MAIL_ACCOUNT_PW="pw"
    ;;
  fresh)
    export SMART_NODE_TG_TOKEN="123:test-token"
    export SMART_NODE_TG_CHANNEL_ID="-1001234567890"
    ;;
  clear)
    cat > "$W/communication/.env" <<'EOF'
IMAP_HOST=imap.example-co.com
SMTP_HOST=smtp.example-co.com
EMAIL_ALLOWED_DOMAINS=example-co.com
EMAIL_ALLOWED_ADDRESSES=owner@example-co.com
CONTACT_GATE=on
EOF
    export SMART_NODE_MAIL_ALLOWED_DOMAINS=""
    export SMART_NODE_MAIL_ALLOWED_ADDRESSES=""
    ;;
  *) echo "unknown case: $CASE" >&2; exit 2 ;;
esac

# Point the companion at the stub lib and stop its trailing dispatch, so it can
# be sourced off a node.
sed -e "s|^LIB=\"/usr/local/lib/smart-node/lib.sh\"|LIB=\"$STUB\"|" \
    -e 's|^case "\$MODE" in|case "__test_no_dispatch__" in|' \
    "$SRC" > "$W/variant.sh"

# Sourced with an explicit empty arg: the script parses "$@", which would
# otherwise still be this runner's.
# shellcheck source=/dev/null
source "$W/variant.sh" ""

# What load_persistence_contract would have read on a node.
PG_CORE_DB="sn_core"; PG_PORT=5432
DISPATCHER_ENV="$W/dispatcher/.env"
ENV_FILE="$W/communication/.env"
COMP_STATE_DIR="/opt/smart-node/communication/state"
SERVICE_USER="$(id -un)"; SMART_NODE_GROUP="$(id -gn)"
MAIL_AGENT_ACCOUNTS="/opt/smart-node/mail/agent-accounts.env"

# The same resolution run_intent performs. The adopt_* helpers are the fix, so
# a baseline without them must still run -- hence declare -F.
if tg_wired_check; then
    TG_WIRED=1
elif declare -F adopt_telegram_from_env >/dev/null && adopt_telegram_from_env; then
    TG_WIRED=1
fi
if external_mail_requested; then
    resolve_email_external; EMAIL_WIRED=1
elif declare -F adopt_email_from_env >/dev/null && adopt_email_from_env; then
    EMAIL_WIRED=1
fi
require_a_channel

write_env
grep -vE '^[[:space:]]*#' "$ENV_FILE" | grep -v '^$'
RUNNER
chmod +x "$WORK/runner.sh"

run_case() {
    bash "$WORK/runner.sh" "$WORK/variant-src.sh" "$WORK/lib.sh" "$1" 2>&1
}

# --- 1. add telegram to an email-only node ----------------------------------
out=$(run_case add-telegram)
for key in \
    "IMAP_HOST=imap.example-co.com" \
    "SMTP_HOST=smtp.example-co.com" \
    "IMAP_USER=node@example-co.com" \
    "IMAP_PASSWORD=nodepw" \
    "EMAIL_ALLOWED_DOMAINS=example-co.com" \
    "EMAIL_ALLOWED_ADDRESSES=owner@example-co.com" \
    "AGENT_ACCOUNTS_FILE=/opt/smart-node/mail/agent-accounts.env" \
    "CONTACT_GATE=on" \
    "CONTACT_GATE_INBOUND=on" \
    "DISPATCH_INTERVAL=7" \
    "SN_BUSINESS_NAME=Test Distillery"
do
    # herestring, not a pipe -- see the note in tools/capabilities-check.sh
    if grep -qxF "$key" <<< "$out"; then
        ok "add-telegram keeps $key"
    else
        bad "add-telegram LOST $key"
    fi
done
if grep -qxF "TG_TOKEN=123:test-token" <<< "$out"; then
    ok "add-telegram wires the new channel"
else
    bad "add-telegram did not wire telegram at all"
fi
if grep -qxF "FAST_CHANNELS=email,telegram" <<< "$out"; then
    ok "add-telegram answers on both channels (FAST_CHANNELS=email,telegram)"
else
    bad "add-telegram left FAST_CHANNELS as $(printf '%s\n' "$out" | grep '^FAST_CHANNELS=' || echo '(absent)') -- a channel not listed here is never answered"
fi

# --- 2. the other direction -------------------------------------------------
out=$(run_case reverse)
if grep -qxF "TG_TOKEN=999:already-here" <<< "$out"; then
    ok "reverse keeps the telegram already wired"
else
    bad "reverse LOST the telegram already wired"
fi
if grep -qxF "FAST_CHANNELS=email,telegram" <<< "$out"; then
    ok "reverse answers on both channels"
else
    bad "reverse left FAST_CHANNELS as $(printf '%s\n' "$out" | grep '^FAST_CHANNELS=' || echo '(absent)')"
fi

# --- 3. a first install must be untouched by any of this --------------------
# Compared against the baseline's own output rather than a literal: the point
# is that nothing changed, and a hardcoded expectation would drift with any
# unrelated edit to write_env.
fresh_now=$(run_case fresh)
if [[ -n "$BASELINE" ]]; then
    ok "fresh install unchanged (baseline is the reference)"
else
    fresh_head=$(git -C "$ROOT" show "HEAD:$REL" > "$WORK/head-src.sh" 2>/dev/null \
        && bash "$WORK/runner.sh" "$WORK/head-src.sh" "$WORK/lib.sh" fresh 2>&1)
    if [[ -z "$fresh_head" ]]; then
        ok "fresh install produced output (no HEAD to compare against)"
    elif [[ "$fresh_now" == "$fresh_head" ]]; then
        ok "fresh install byte-identical to HEAD -- a first install is untouched"
    else
        bad "fresh install differs from HEAD -- the carry-forward changed a clean install"
    fi
fi
if grep -q '^IMAP_HOST=' <<< "$fresh_now"; then
    bad "fresh install invented an email channel out of nothing"
else
    ok "fresh install wires only what this run supplied"
fi

# --- 4. clearing on purpose still works -------------------------------------
out=$(run_case clear)
if grep -qxF "EMAIL_ALLOWED_DOMAINS=" <<< "$out"; then
    ok "an input set-but-empty clears the value"
else
    bad "an input set-but-empty did not clear -- carrying forward beat a deliberate blanking"
fi
if grep -qxF "CONTACT_GATE=on" <<< "$out"; then
    ok "clearing one key does not disturb another"
else
    bad "clearing one key reset CONTACT_GATE"
fi

echo
if (( fails > 0 )); then
    echo "$fails failure(s) against $WHAT"
    exit 1
fi
echo "all checks pass against $WHAT"
