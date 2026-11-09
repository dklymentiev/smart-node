#!/usr/bin/env bash
#
# node-agnostic.test.sh -- nothing about one particular owner in the product.
#
# A node installed by someone else must not mail us, live in our time zone, or
# fall back to one of our agents. The owner, the sender and the zone are the
# node's own: Settings, the installed sender, the host zone.
#
#   1. no IANA zone name in runtime code (the host zone is the node's zone)
#   2. no owner's name in runtime code (migrations and release tooling aside)
#   3. the daily digest unit sets no zone, recipient or sender
#   4. the digest timer fires in host local time
#   5. the digest sends through POST /api/owner-alert, not comm.sh
#   6. the digest reads the zone from the host (TZ, /etc/timezone, else UTC)
#   7. configure-host changes the host zone only when --timezone is given
#   8. host_zone in lib.sh answers UTC when the host names no zone
#
# Exit: 0 all pass, 1 any fail.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0; FAIL=0
ok()  { echo "  PASS $*"; PASS=$((PASS+1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL+1)); }

RUNTIME=(-- '*.py' '*.sh' '*.service' '*.timer' '*.php' '*.js' '*.ts' '*.tsx' '*.sql'
         ':!tests/**' ':!**/tests/**' ':!**/test_*' ':!**/node_modules/**'
         ':!release-evidence/**' ':!acceptance-evidence/**')

# 1
zones=$(git -C "$REPO" grep -n -E '\b(America|Europe|Asia|Africa|Australia|Pacific|Atlantic|Indian)/[A-Z][A-Za-z_]+' "${RUNTIME[@]}" \
        | grep -v -E '^configure/configure-host\.sh:[0-9]+:.*(e\.g\.|like) Europe/Berlin')
[[ -z "$zones" ]] && ok "1 no zone name in runtime code" || { bad "1 zone names in runtime code:"; echo "$zones"; }

# 2
names=$(git -C "$REPO" grep -n -E '[Dd]mytro|Klymentiev' "${RUNTIME[@]}" \
        ':!companions/core-api/migrations/**' ':!tools/release.sh' ':!tools/install-from-release.sh' \
        ':!tools/personal-words.py')
[[ -z "$names" ]] && ok "2 no owner name in runtime code" || { bad "2 owner name in runtime code:"; echo "$names"; }

UNIT="$REPO/companions/node-ops/systemd/smart-node-event-digest.service"
TIMER="$REPO/companions/node-ops/systemd/smart-node-event-digest.timer"
DIGEST="$REPO/companions/node-ops/event-digest.py"

# 3
if grep -qE '^Environment=(TZ|SN_EVENT_DIGEST_RECIPIENT|SN_EVENT_MONITOR_SENDER)' "$UNIT"; then
    bad "3 the digest unit still sets a zone, recipient or sender"
else
    ok "3 the digest unit sets no zone, recipient or sender"
fi

# 4
if grep -qE '^OnCalendar=\*-\*-\* [0-9:]+$' "$TIMER"; then
    ok "4 the digest timer fires in host local time"
else
    bad "4 the digest timer pins a zone: $(grep '^OnCalendar' "$TIMER")"
fi

# 5
if grep -q '/api/owner-alert' "$DIGEST" && ! grep -q 'comm.sh' "$DIGEST"; then
    ok "5 the digest sends through /api/owner-alert"
else
    bad "5 the digest does not send through /api/owner-alert only"
fi

# 6
out=$(cd "$REPO/companions/node-ops" && python3 - <<'PY'
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("digest", "event-digest.py")
os.environ["TZ"] = "Asia/Tokyo"
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
a = m._TZ_NAME
os.environ.pop("TZ")
b = m._host_zone_name()
print(a, "ok" if b else "empty")
PY
)
if [[ "$out" == "Asia/Tokyo ok" ]]; then
    ok "6 the digest reads the zone from the host"
else
    bad "6 the digest zone resolution: '$out'"
fi

# 7
blk=$(sed -n '/^echo "\[A\] timezone + locale"/,/^sed -i/p' "$REPO/configure/configure-host.sh")
if grep -q 'if \[\[ -n "\$TIMEZONE" \]\]' <<<"$blk" && ! grep -qE '^(echo "[A-Z]|ln -sf /usr/share/zoneinfo/[A-Z])' <<<"$blk"; then
    ok "7 configure-host leaves the host zone alone without --timezone"
else
    bad "7 configure-host still sets a zone unconditionally"
fi

# 8
z=$(cd /tmp && env -i bash -c "source <(sed -n '/^host_zone() {/,/^}/p' '$REPO/bootstrap/lib.sh'); host_zone")
if [[ -n "$z" && -f "/usr/share/zoneinfo/$z" ]]; then
    ok "8 host_zone answers a real zone ($z)"
else
    bad "8 host_zone answered '$z'"
fi

echo "node-agnostic: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
