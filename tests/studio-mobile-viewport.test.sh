#!/usr/bin/env bash
#
# The Studio fits a phone's visible screen, toolbars and keyboard included.
#
# WHAT WENT WRONG. On the owner's Samsung S25 the chat composer was half hidden at
# the bottom, while the desktop browser's device emulation showed it whole. The
# shell was `height: 100vh` and the composer sat `bottom: 20px` inside it. On
# mobile Chrome and Samsung Internet 100vh is the height with the toolbars
# RETRACTED, so while the address bar and bottom toolbar are shown the bottom of
# the shell, and the composer with it, is under them. Emulation draws no
# toolbars, which is why it looked fine there.
#
# THE PROPERTIES:
#   1. the app shell is as tall as the visible viewport (dvh), not 100vh
#   2. the body's minimum height is dvh too, not 100vh
#   3. the composer clears the gesture bar (safe-area inset on its bottom offset)
#   4. the viewport meta reports that inset (viewport-fit=cover) and lets the
#      keyboard shrink the layout (interactive-widget=resizes-content)
#   5. the thread's bottom padding follows the composer's real bottom offset,
#      so the last message is not left under a composer that moved up
#   6. the field is sized to its content when its width changes, not only on
#      input: on a narrow phone the placeholder wraps to two lines and was cut
#      at one until something was typed and sent
#   7. on a phone there is no Chat/Canvas switch (no room for a canvas beside the
#      thread), and a narrow screen always shows the chat, also when a desktop
#      window is narrowed while in Canvas; CSS and JS use the same breakpoint
#   8. on a phone the sidebar takes no room: it is a full-screen panel opened by a
#      logo button at the top of the main column, and it closes when a view or a
#      chat is opened from it; the desktop keeps its sidebar and collapsed rail.
#      The open-state class (.app.sb-open) is not also a styled class of its own:
#      the first build named the button .sb-open and opening the panel shrank the
#      whole app to the button's 36px
#   9. app.js still parses
#
#   tests/studio-mobile-viewport.test.sh                  # tree, PASS
#   tests/studio-mobile-viewport.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio: fits a phone's visible screen (git:$BASELINE)"
else echo "Studio: fits a phone's visible screen (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
P=companions/studio/public/next2
read_src "$P/css/app.css"  > "$D/app.css"
read_src "companions/studio/public/ui/base.css"  > "$D/base.css"
read_src "$P/index.html"   > "$D/index.html"
read_src "$P/js/app.js"    > "$D/app.js"
for f in app.css base.css index.html app.js; do
    [[ -s "$D/$f" ]] || { bad "$f is readable"; echo; echo "FAIL"; exit 1; }
done

# The declarations of one CSS rule, by its exact selector at the start of a line.
rule_of() { grep -E "^[[:space:]]*$1[[:space:]]*\\{" "$2" | head -1; }

# 1. app shell
app=$(rule_of '\.app' "$D/app.css")
if grep -q 'height: *100dvh' <<<"$app" && ! grep -q '100vh' <<<"$app"; then
    ok "the app shell is as tall as the visible viewport (100dvh)"
else
    bad "the app shell is as tall as the visible viewport -- still 100vh, the bottom hides under the phone's toolbars"
fi

# 2. body
body=$(awk '/^[[:space:]]*body[[:space:]]*\{/{f=1} f{print} f && /\}/{exit}' "$D/base.css")
if grep -q 'min-height: *100dvh' <<<"$body" && ! grep -q '100vh' <<<"$body"; then
    ok "the body's minimum height is 100dvh"
else
    bad "the body's minimum height is 100dvh -- still 100vh"
fi

# 3. composer bottom offset
cmp=$(awk '/^[[:space:]]*\.composer[[:space:]]*\{/{f=1} f{print} f && /\}/{exit}' "$D/app.css")
if grep -Eq 'bottom: *calc\(20px \+ env\(safe-area-inset-bottom' <<<"$cmp"; then
    ok "the composer clears the gesture bar"
else
    bad "the composer clears the gesture bar -- its bottom offset ignores the safe-area inset"
fi

# 4. viewport meta
meta=$(grep -o '<meta name="viewport"[^>]*>' "$D/index.html")
if grep -q 'viewport-fit=cover' <<<"$meta" && grep -q 'interactive-widget=resizes-content' <<<"$meta"; then
    ok "the viewport meta reports the inset and lets the keyboard shrink the layout"
else
    bad "the viewport meta reports the inset and lets the keyboard shrink the layout -- got: ${meta:-none}"
fi

# 5. thread padding follows the composer's computed bottom offset
if grep -q "getComputedStyle(cmp).bottom" "$D/app.js" && grep -q "sc.style.paddingBottom = (cmp.offsetHeight + parseFloat(getComputedStyle(cmp).bottom)" "$D/app.js"; then
    ok "the thread's bottom padding follows the composer's real offset"
else
    bad "the thread's bottom padding follows the composer's real offset -- a fixed +40 leaves the last message under a raised composer"
fi

# 6. the field is re-sized when its width changes (first show, rotation, narrow screen)
ro=$(awk '/new ResizeObserver/{f=1} f{print} f && /\.observe\(_cmpTa\)/{exit}' "$D/app.js")
if grep -q "cmpGrow()" <<<"$ro" && grep -q "\.observe(_cmpTa)" <<<"$ro"; then
    ok "the field is sized to its content as soon as it has a width"
else
    bad "the field is sized to its content as soon as it has a width -- only input resizes it, a wrapped placeholder is cut"
fi

# 7. no Chat/Canvas switch on a phone, and a narrow screen falls back to the chat
css_bp=$(grep -Eo '@media \(max-width: *[0-9]+px\) *\{ *\.tabs #mode *\{ *display: *none' "$D/app.css" | grep -Eo '[0-9]+px' | head -1)
js_bp=$(grep -Eo "matchMedia\('\(max-width: *[0-9]+px\)'\)" "$D/app.js" | grep -Eo '[0-9]+px' | head -1)
if [[ -n "$css_bp" && "$css_bp" == "$js_bp" ]] \
   && grep -q "\$('#mode button\[data-m=\"chat\"\]').click()" "$D/app.js"; then
    ok "no Chat/Canvas switch on a phone ($css_bp), a narrow screen shows the chat"
else
    bad "no Chat/Canvas switch on a phone, a narrow screen shows the chat -- css breakpoint '${css_bp:-none}', js '${js_bp:-none}'"
fi

# 8. phone sidebar: full-screen panel behind a logo button, closed by what it opens
phone_css=$(awk '/@media \(max-width: *760px\) *\{ *\/\* phone shell/{f=1} f{print} f && /^  \}/{exit}' "$D/app.css")
launcher=$(grep -c '<div class="main__top"><button class="sb-launch" id="sbOpen"' "$D/index.html")
if grep -q '\.sb *{[^}]*position: *fixed' <<<"$phone_css" \
   && grep -q '\.app\.sb-open \.sb *{[^}]*transform: *none' <<<"$phone_css" \
   && grep -q '\.sb-launch *{[^}]*display: *grid' <<<"$phone_css" \
   && ! grep -Eq '^[[:space:]]*\.sb-open[[:space:]]*(rect[[:space:]]*)?\{' "$D/app.css" \
   && [[ "$launcher" == "1" ]] \
   && grep -q "sbPanel(false)" <<<"$(awk '/^function setView\(/{f=1} f{print} f && /^}/{exit}' "$D/app.js")" \
   && grep -q "setView('chats')" <<<"$(awk '/^function setActive\(/{f=1} f{print} f && /^}/{exit}' "$D/app.js")"; then
    ok "on a phone the sidebar is a full-screen panel behind a logo button, closed by what it opens"
else
    bad "on a phone the sidebar is a full-screen panel behind a logo button, closed by what it opens -- the sidebar still takes a column"
fi

# 9. app.js parses
if command -v node >/dev/null 2>&1; then
    if node --check "$D/app.js" 2>/dev/null; then ok "app.js parses"; else bad "app.js parses"; fi
else
    ok "app.js parse check skipped: no node on this host"
fi

echo
if [[ $FAILED -eq 0 ]]; then echo "PASS"; exit 0; else echo "FAIL ($FAILED)"; exit 1; fi
