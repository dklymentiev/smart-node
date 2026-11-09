#!/usr/bin/env bash
# Pulling down from the top of a screen refreshes the app (an installed app has no reload button).
set -u
cd "$(dirname "$0")/.."
A=companions/studio/public/next2/js/app.js; C=companions/studio/public/next2/css/app.css; fail=0
node tests/pull-to-refresh.behavior.mjs "$A" > /tmp/_ptr.out 2>&1 && echo "  ok   behaviour: pull refreshes, other gestures do not" || { echo "  FAIL behaviour"; tail -6 /tmp/_ptr.out; fail=1; }
grep -q '^\.ptr {' "$C" && echo "  ok   the bar is styled with kit tokens" || { echo "  FAIL the bar has no style"; fail=1; }
! sed -n '/^\.ptr {/,/}/p' "$C" | grep -Eq '#[0-9a-fA-F]{3,8}\b' && echo "  ok   no own colours in it" || { echo "  FAIL own colours"; fail=1; }
[ $fail = 0 ] && echo "PASS: pull to refresh" || { echo FAIL; exit 1; }
