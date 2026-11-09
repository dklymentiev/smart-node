#!/bin/bash
# Regression contract for budget alerts.
#
# This test is deliberately runnable against another checkout:
#   SMART_NODE_SOURCE_ROOT=/path/to/v1.12.42 tests/budget-alerts.test.sh
# That is the failing baseline proof.  The test introduced on the fix branch must
# fail against the released tree where notified_at was claimed before a
# best-effort background send.

set -uo pipefail

SELF_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${SMART_NODE_SOURCE_ROOT:-$SELF_ROOT}"
GW="$ROOT/companions/gateway/sessions_v2_routes.py"
BUDGET="$ROOT/companions/gateway/budget.py"
CORE="$ROOT/companions/core-api/routes/budgets.py"
COMM="$ROOT/companions/communication.sh"
CORE_INSTALL="$ROOT/companions/core-api.sh"
GW_INSTALL="$ROOT/companions/gateway.sh"
OUTBOUND="$ROOT/companions/communication/send_outbound.py"
ORCHESTRATOR="$ROOT/orchestrator/install-on.sh"
UPDATER="$ROOT/tools/update-on.sh"

pass=0
fail=0
ok() { echo "ok - $1"; pass=$((pass + 1)); }
bad() { echo "not ok - $1"; fail=$((fail + 1)); }

has() {
    local file="$1" pattern="$2" label="$3"
    if grep -Eq -- "$pattern" "$file"; then ok "$label"; else bad "$label"; fi
}

lacks() {
    local file="$1" pattern="$2" label="$3"
    if grep -Eq -- "$pattern" "$file"; then bad "$label"; else ok "$label"; fi
}

# Installer-owned, non-secret contract: gateway gets a real actor and email
# channel, core-api gets the explicitly selected real mailbox.
has "$COMM" 'BUDGET_NOTIFY_ACTOR=admin' \
    "installer publishes the stock owner actor"
has "$COMM" 'BUDGET_NOTIFY_CHANNEL=email' \
    "installer publishes email as the budget-alert channel"
has "$COMM" 'SN_NOTIFY_SENDER=' \
    "installer publishes a real configured notification sender"
has "$ORCHESTRATOR" '--notify-email-sender' \
    "node installer exposes the system sender choice"
has "$UPDATER" '--notify-email-sender' \
    "update path forwards the system sender choice"
has "$CORE_INSTALL" 'EnvironmentFile=-\$BUDGET_NOTIFICATIONS_ENV' \
    "core-api consumes the notification contract"
has "$GW_INSTALL" 'EnvironmentFile=-\$BUDGET_NOTIFICATIONS_ENV' \
    "gateway consumes the notification contract"
has "$OUTBOUND" 'def _is_system_owner_notification' \
    "outbound recognises the narrowly scoped system alert"
has "$OUTBOUND" 'system_owner_alert = _is_system_owner_notification' \
    "contact gate permits only a system alert to the registered owner"
has "$OUTBOUND" 'address != target' \
    "budget-alert exemption rejects additional recipients"

# The sender policy is executable, not only present in source: a multi-mailbox
# node skips an uncredentialed binding and selects the first address that can
# really authenticate.  An explicit but unreal address must be refused.
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT
sed -n '/^_mailbox_has_password()/,/^}/p' "$COMM" > "$TMPD/functions.sh"
sed -n '/^_resolve_notify_sender()/,/^}/p' "$COMM" >> "$TMPD/functions.sh"
if (
    set -uo pipefail
    source "$TMPD/functions.sh"
    MAIL_AGENT_ACCOUNTS="$TMPD/accounts.env"
    BUDGET_NOTIFICATIONS_ENV="$TMPD/notification.env"
    EMAIL_WIRED=1
    NODE_MAIL_ACCT=""
    printf 'PW_ready_example_test=secret\n' > "$MAIL_AGENT_ACCOUNTS"
    _envfile_get() { return 0; }
    psql_core() { printf 'missing@example.test\nready@example.test\n'; }
    die() { return 1; }
    [[ "$(_resolve_notify_sender)" == "ready@example.test" ]]
); then
    ok "installer selects only a mailbox with installed credentials"
else
    bad "installer selects only a mailbox with installed credentials"
fi
if (
    set -uo pipefail
    source "$TMPD/functions.sh"
    MAIL_AGENT_ACCOUNTS="$TMPD/accounts.env"
    BUDGET_NOTIFICATIONS_ENV="$TMPD/notification.env"
    EMAIL_WIRED=1
    NODE_MAIL_ACCT=""
    SMART_NODE_NOTIFY_EMAIL_SENDER="invented@example.test"
    _envfile_get() { return 0; }
    psql_core() { return 0; }
    die() { exit 1; }
    _resolve_notify_sender >/dev/null
); then
    bad "installer refuses an explicit sender without credentials"
else
    ok "installer refuses an explicit sender without credentials"
fi

# The released defect was an UPDATE in gateway before an unobserved background
# POST.  Gateway must no longer own the claim, and the hard-limit path must wait
# until core-api has either accepted the outbox row or refused it.
lacks "$BUDGET" 'def claim_exhaustion_notification' \
    "gateway no longer stamps notified_at before sending"
has "$GW" 'await _send_budget_exhaustion_notification\(b\["id"\]\)' \
    "hard-limit path waits for notification acceptance"
lacks "$GW" 'create_task\(_send_budget_exhaustion_notification' \
    "exhaustion notification is not fire-and-forget"

# One transaction in core-api locks the row, queues the owner email, then marks
# the period.  SQL-order is checked in the endpoint body because reversing the
# latter two statements recreates the loss observed on 2026-09-19.
python3 - "$CORE" <<'PY'
import ast
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
tree = ast.parse(path.read_text())
fn = next((n for n in tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "notify_budget_exhaustion"), None)
if fn is None:
    print("not ok - core-api has the atomic budget exhaustion endpoint")
    raise SystemExit(1)
src = ast.get_source_segment(path.read_text(), fn) or ""
needles = ("FOR UPDATE", "INSERT INTO sn_core.messages", "SET notified_at = now()")
positions = [src.find(n) for n in needles]
if all(p >= 0 for p in positions) and positions == sorted(positions):
    print("ok - core-api locks, queues, then marks in one endpoint")
else:
    print("not ok - core-api locks, queues, then marks in one endpoint")
    raise SystemExit(1)
for needle, label in (
    ("scope_type", "alert body carries budget scope"),
    ("limit_usd", "alert body carries the limit"),
    ("current_spend_usd", "alert body carries current spend"),
    ("period_reset_at", "alert body carries reset time"),
    ("Raise the limit", "alert body says how to resume"),
):
    if needle not in src:
        print("not ok - " + label)
        raise SystemExit(1)
    print("ok - " + label)
PY
rc=$?
if (( rc == 0 )); then pass=$((pass + 6)); else fail=$((fail + 1)); fi

echo "$pass passed, $fail failed"
(( fail == 0 ))
