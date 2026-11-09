#!/usr/bin/env bash
#
# A reader who has scrolled up in a chat sees a floating chevron with the number of messages
# below the view, and one press takes them to the end.
#
# WHAT WENT WRONG. Messages that arrive while the reader is scrolled up do not move the view
# (that is right: it must not yank the reader), but nothing said the chat had moved on, and
# the way back to the end was a long scroll.
#
# THE PROPERTIES, each run on the real code:
#   1. the chip shows only when the reader is a clear distance (80 px) from the end, and counts
#      what is below the view; at the end it is hidden
#   2. it is wired to scroll, resize and a change of the thread, so it follows new messages, and
#      pressing it scrolls to the end smoothly
#   3. it is a button in the chat pane that takes the theme's tokens, and its hidden state is
#      not overridden by its own display rule
#
#   tests/studio-chat-jump-to-latest.test.sh                  # working tree, PASS
#   tests/studio-chat-jump-to-latest.test.sh --baseline REF   # git:REF, expect FAIL
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"
FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}
if [[ -n "$BASELINE" ]]; then echo "chat jump to latest (git:$BASELINE)"
else echo "chat jump to latest (working tree)"; fi
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/js/app.js > "$D/app.js"
read_src companions/studio/public/next2/index.html > "$D/index.html"
read_src companions/studio/public/next2/css/app.css > "$D/app.css"

cat > "$D/logic.js" <<'JS'
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const i = src.indexOf('const JUMP_GAP');
if (i < 0) { console.log('   - there is no jump chip logic'); process.exit(1); }
const j = src.indexOf('function paintJump', i);
const fn = new Function(src.slice(i, j) + '; return { chatJumpState, JUMP_GAP };')();
const bad = [];
let s = fn.chatJumpState(0, 0);
if (s.show) bad.push('shown at the very end');
if (fn.chatJumpState(fn.JUMP_GAP - 1, 5).show) bad.push('shown just under the threshold');
s = fn.chatJumpState(fn.JUMP_GAP, 29);
if (!s.show || s.count !== 29) bad.push('29 messages below are not reported: ' + JSON.stringify(s));
if (fn.chatJumpState(500, -3).count !== 0) bad.push('a negative count is not clamped');
bad.forEach((b) => console.log('   - ' + b));
process.exit(bad.length ? 1 : 0);
JS
if node "$D/logic.js" "$D/app.js"
then ok "the chip appears 80 px or more from the end and counts the messages below"
else bad "the chip logic is missing or wrong"; fi

cat > "$D/wiring.py" <<'PY'
import sys
js = open(sys.argv[1], encoding="utf-8").read()
html = open(sys.argv[2], encoding="utf-8").read()
css = open(sys.argv[3], encoding="utf-8").read()
w = js[js.index("(function wireJump()"):][:1100]
for need in ("'scroll'", "'resize'", "MutationObserver", "ResizeObserver", "behavior: 'smooth'", "sc.scrollHeight"):
    assert need in w, "not wired: " + need
assert 'id="threadJump"' in html and "thread__jump" in html and "hidden" in html.split('id="threadJump"')[1][:80], "no hidden chip in the chat pane"
assert html.index('id="threadJump"') > html.index('class="thread__scroll"'), "the chip is not after the scroller"
rule = css[css.index(".thread__jump {"):][:520]
for tok in ("var(--sn-surface-2)", "var(--sn-ink)", "var(--sn-rule)", "var(--sn-r-pill)"):
    assert tok in rule, "no theme token " + tok
assert ".thread__jump[hidden] { display: none; }" in css, "its own display would override hidden"
assert "#fff" not in rule.lower() and "white" not in rule.lower(), "a light colour is hard-coded"
assert "position: relative" in css[css.index("  .thread { display: flex"):][:200], "the pane is not a positioning context"
PY
if python3 "$D/wiring.py" "$D/app.js" "$D/index.html" "$D/app.css"
then ok "wired to scroll, resize and thread changes, smooth scroll to the end; the chip takes the theme"
else bad "the chip is not wired or not themed"; fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAILED: $FAILED"; fi
exit "$FAILED"
