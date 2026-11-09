#!/usr/bin/env bash
# The daily digest is an e-mail: with no mail set up its timer is off, so nothing
# runs and nothing fails. It used to be enabled on every node, and failed daily.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
f="$root/companions/core-api.sh"
fn="$(sed -n '/^install_event_digest() {/,/^}/p' "$f")"

fail=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }

check "the timer depends on the mail sender" \
  'grep -q "SN_NOTIFY_SENDER" <<<"$fn"'
check "without mail it is switched off" \
  'grep -q "disable --now smart-node-event-digest.timer" <<<"$fn"'
check "a failed run left behind is cleared" \
  'grep -q "reset-failed smart-node-event-digest.service" <<<"$fn"'
check "the installer's own check does not require the timer without mail" \
  'sed -n "/timer smart-node-event-digest.timer enabled/{x;p;x};x" "$f" | grep -q "SN_NOTIFY_SENDER"'
check "the timer is recorded for validate only when it is on"   'sed -n "/SN_NOTIFY_SENDER=/,/^    else/p" <<<"$fn" | grep -q "manifest_record systemd-unit \"smart-node-event-digest.timer\""'
exit $fail
