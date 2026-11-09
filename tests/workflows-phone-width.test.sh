#!/usr/bin/env bash
# The Workflows card grid never asks for more width than the screen has (a phone is 390 px; the cards wanted 430).
set -u
cd "$(dirname "$0")/.."
C=companions/studio/public/next2/css/app.css
if grep -q '^\.wf-grid {.*minmax(min(430px, 100%), 1fr)' "$C"; then echo "  ok   the card column is capped at the screen width"; echo "PASS: workflows phone width"
else echo "  FAIL the card column can be wider than a phone"; exit 1; fi
