#!/usr/bin/env bash
# Work Map on a phone: grid columns may shrink below their text (minmax(0,1fr)), the top bar is one row, chips fit the screen.
set -u
cd "$(dirname "$0")/.."
C=companions/studio/public/next2/css/work-map.css; fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
chk "the signal cards are not as wide as their longest line" "grep -q 'signalbar{grid-template-columns:minmax(0,1fr)}' $C"
chk "the two-column and one-column boards can shrink" "grep -q 'board{grid-template-columns:minmax(0,1fr)}' $C"
chk "the top bar is one row on a phone" "grep -q '.workmapv .top{height:56px;flex-wrap:nowrap' $C"
chk "chips fit the screen" "grep -q '.workmapv .chip{max-width:100%;min-width:0}' $C"
[ $fail = 0 ] && echo "PASS: work map phone width" || { echo FAIL; exit 1; }
