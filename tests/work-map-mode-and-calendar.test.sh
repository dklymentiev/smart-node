#!/usr/bin/env bash
# Work Map: a press on a view or range button brings the board into view (on a phone it sits far below the buttons),
# and the phone bar is one row. Calendar: the picked day is a soft tile, not a square frame.
set -u
cd "$(dirname "$0")/.."
P=companions/studio/public/next2; fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
chk "the view buttons bring the board into view" "grep -q 'function showBoard' $P/js/work-map.js && [ \$(grep -c 'showBoard();' $P/js/work-map.js) = 2 ]"
chk "the phone bar is one row and stays at the top" "grep -q '.workmapv .top{height:56px;flex-wrap:nowrap' $P/css/work-map.css"
chk "the board stops below the sticky bar" "grep -q '.workmapv .map{scroll-margin-top:64px}' $P/css/work-map.css"
chk "the picked calendar day is a rounded tile, with no 2px frame" "grep -q 'calm__d.is-picked::before' $P/css/app.css && ! grep -q 'is-picked { box-shadow: inset 0 0 0 2px' $P/css/app.css"
chk "the pull line stays at the top of the display, outside the screen that moves" "grep -q 'document.body.appendChild(ind)' $P/js/app.js && grep -q 'top: calc(env(safe-area-inset-top, 0px) + 5px); left: 10px; right: 10px; height: 3px' $P/css/app.css"
[ $fail = 0 ] && echo "PASS: work map mode and calendar" || { echo FAIL; exit 1; }
