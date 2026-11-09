#!/usr/bin/env bash
#
# The Studio chat tabs: closing a tab hides it and never deletes a chat; a chat
# is deleted only on purpose, after a question.
#
# WHAT WAS WRONG. A whole row under the tabs (#deskWho) held one button, the
# agent's avatar and name, whose menu offered Profile and two session actions;
# the tab above already showed the same avatar. The tab's cross called
# DELETE /api/desks/<id> with no question asked, so one mistaken tap erased the
# chat, its attachments and its place in the agent's history.
#
# THE PROPERTIES:
#   1. no header row: #deskWho, #deskSession, #deskReload and their code are gone
#   2. the Chat/Canvas switch lives in the tabs row (renderTabs puts it back
#      after every render)
#   3. closing never deletes: the only DELETE on /api/desks/<id> is deleteChat,
#      which asks first and says it cannot be undone; closing an agent's tab
#      clears tab_open on its chats
#   4. the cross asks first, then closes the tab: PATCH {tab_open: false}
#   5. tabs are the open desks (tab_open !== false) plus the active one
#   7. opening a closed chat reopens its tab first
#   8. on a phone a popover drops down from what opened it, full width, not a
#      sheet at the bottom (at the bottom it looks detached from the tab)
#   9. on a phone the sheet lies over a scrim that takes the tap: dismissing the
#      sheet never also taps what is under it
#  11. "Close this tab?" is a modal in the middle of the screen over a scrim, on a
#      phone and on the desktop (a confirmation is not a sheet at the bottom)
#  12. the tab icon keeps its small letter: the button must not inherit the tab's
#      13px font (it did, and the letter hung out of its 18px circle)
#  13. "+" (new chat) is the first thing in the tabs row, not after the last tab
#  14. app.js still parses
#  15. every element avInner() fills is a positioning box, or the absolute <img>
#      stretches over the whole popover (it did in the chat card)
#  16. a library face fills its circle: the crew pictures are a head on a wide dark
#      field, so they are drawn larger and clipped back to the circle
#
#   tests/studio-agent-card.test.sh                  # tree, PASS
#   tests/studio-agent-card.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio: tabs close, chats stay (git:$BASELINE)"
else echo "Studio: tabs close, chats stay (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
P=companions/studio/public/next2
read_src "$P/index.html"  > "$D/index.html"
read_src "$P/js/app.js"   > "$D/app.js"
read_src "$P/css/app.css" > "$D/app.css"
for f in index.html app.js app.css; do
    [[ -s "$D/$f" ]] || { bad "$f is readable"; echo; echo "FAIL"; exit 1; }
done

# Body of a top-level function, from its declaration to the next one.
body_of() {
    awk -v n="$1" 'f && /^(async )?function /{exit} $0 ~ "^(async )?function "n"\\("{f=1} f' "$D/app.js"
}

# 1. no header row
if ! grep -Eq 'id="(deskWho|deskSession|deskReload)"' "$D/index.html" \
   && ! grep -Eq 'renderDeskWho|openDeskMenu|deskSession|deskReload' "$D/app.js"; then
    ok "no header row under the tabs"
else
    bad "no header row under the tabs -- #deskWho / #deskSession / #deskReload still there"
fi

# 2. the Chat/Canvas switch is put into the tabs row by renderTabs
if grep -q "appendChild(_modeSeg)" <<<"$(body_of renderTabs)"; then
    ok "the Chat/Canvas switch lives in the tabs row"
else
    bad "the Chat/Canvas switch lives in the tabs row"
fi

# 3. closing never deletes; deleting asks first
del_calls=$(grep -n "api/desks/" "$D/app.js" | grep -v attachments | grep -cE "method: *'DELETE'")
dc=$(body_of deleteChat)
q_line=$(grep -n "cannot be undone" <<<"$dc" | head -1 | cut -d: -f1)
d_line=$(grep -nE "method: *'DELETE'" <<<"$dc" | head -1 | cut -d: -f1)
if [[ "$del_calls" -eq 1 && -n "$q_line" && -n "$d_line" && "$q_line" -lt "$d_line" ]] \
   && grep -q "data-yes" <<<"$dc" \
   && ! grep -qE "method: *'DELETE'|deleteChat\(" <<<"$(body_of confirmCloseAgentTab)$(body_of confirmCloseTab)"; then
    ok "closing a tab never deletes a chat; deleting one asks first"
else
    bad "closing a tab never deletes a chat; deleting one asks first"
fi

# 4. the cross asks, then closes with PATCH tab_open false
tabs=$(body_of renderTabs)
if grep -q "confirmCloseTab(" <<<"$tabs" \
   && grep -q "setTabOpen(id, false)" <<<"$(body_of confirmCloseTab)" \
   && grep -q "tab_open: open" <<<"$(body_of setTabOpen)" \
   && grep -q "method: 'PATCH'" <<<"$(body_of setTabOpen)"; then
    ok "the cross asks first, then closes the tab (PATCH tab_open)"
else
    bad "the cross asks first, then closes the tab (PATCH tab_open)"
fi

# 5. tabs = open desks + the active one
if grep -q "const tabOpen = " "$D/app.js" && grep -q "desks.filter((d) => tabOpen(d) || d.id === activeId" <<<"$tabs"; then
    ok "tabs are the open chats plus the active one"
else
    bad "tabs are the open chats plus the active one"
fi

# 7. a closed chat reopens its tab before it is shown
if grep -q "await setTabOpen(id, true)" <<<"$(body_of openDesk)"; then
    ok "a closed chat that is opened gets its tab back"
else
    bad "a closed chat that is opened gets its tab back"
fi

# 8. phone: popover drops down from its anchor, full width
sp=$(body_of showPop)
if grep -q "_phone.matches" <<<"$sp" && grep -q "pop--drop" <<<"$sp" && ! grep -q "pop--sheet" <<<"$sp" \
   && grep -q "_pop.style.top = (r.bottom + " <<<"$sp" \
   && grep -Eq '\.pop\.pop--drop *\{[^}]*left: *8px[^}]*right: *8px' "$D/app.css" \
   && ! grep -q 'pop--sheet' "$D/app.css"; then
    ok "on a phone a popover drops down from what opened it"
else
    bad "on a phone a popover drops down from what opened it -- still a sheet at the bottom"
fi

# 9. phone sheet over a scrim that takes the tap; no dismiss-on-mousedown on a phone
pop=$(body_of showPop)
if grep -q "pop-scrim" <<<"$pop" && grep -q "addEventListener('click', closePop)" <<<"$pop" \
   && grep -q "if (!_scrim) setTimeout(() => document.addEventListener('mousedown', _popOutside, true), 0);" <<<"$pop" \
   && grep -Eq '\.pop-scrim *\{[^}]*position: *fixed' "$D/app.css"; then
    ok "dismissing the phone sheet does not tap what is under it"
else
    bad "dismissing the phone sheet does not tap what is under it"
fi

# 11. the close question is a centred modal over a scrim
if grep -q "'pop--modal')" <<<"$(body_of confirmCloseTab)" \
   && grep -q "const modal = " <<<"$pop" && grep -q "if (modal || _phone.matches)" <<<"$pop" \
   && grep -Eq '\.pop\.pop--modal *\{[^}]*top: *50%[^}]*transform: *translate\(-50%, *-50%\)' "$D/app.css"; then
    ok "the close question is a modal in the middle of the screen"
else
    bad "the close question is a modal in the middle of the screen"
fi

# 12. the tab icon's letter size is its own
ic_rule=$(grep -E '^\.tab__ic *\{' "$D/app.css" | head -1)
if [[ -n "$ic_rule" ]] && ! grep -q 'font: *inherit' <<<"$ic_rule" && grep -Eq 'font: *600 [0-9]+px/1 ' <<<"$ic_rule"; then
    ok "the tab icon keeps its small letter"
else
    bad "the tab icon keeps its small letter -- ${ic_rule:-no .tab__ic rule}"
fi

# 13. "+" first in the tabs row
if grep -q "\$('#tabs').innerHTML = \`<button class=\"tab-add\" id=\"tabAdd\"" <<<"$tabs"; then
    ok "the new-chat + is the first thing in the tabs row"
else
    bad "the new-chat + is the first thing in the tabs row -- it comes after the last tab"
fi

# 15. an avatar box is a positioning box
pos_rule=$(grep -E '^[^{]*\.tab__av[^{]*\{ *position: *relative' "$D/app.css" | head -1)
boxes=$(grep -oE 'class="[a-z_-]+">\$\{avInner\(' "$D/app.js" | grep -oE '"[a-z_-]+"' | tr -d '"' | sort -u)
missing=""
for b in $boxes; do grep -Eq "\.$b[ ,{]" <<<"$pos_rule" || missing="$missing $b"; done
if [[ -n "$pos_rule" && -n "$boxes" && -z "$missing" ]]; then
    ok "every box avInner fills positions its picture"
else
    bad "every box avInner fills positions its picture -- not positioned:${missing:- no rule}"
fi

# 16. a library face fills its circle
crew=$(grep -E '^\.av-img\[src\*="/crew/"\] *\{' "$D/app.css" | head -1)
# base.css caps every img at max-width:100%; without lifting it the 140% width is cut to
# 100% and the face slides left out of its circle (it did, live, 2026-09-13).
if grep -q 'clip-path: *circle' <<<"$crew" && grep -q 'width: *calc(var(--av-face) \* 100%)' <<<"$crew" && grep -q 'max-width: *none' <<<"$crew" \
   && grep -Eq '^:root *\{ *--av-face: *1\.[0-9]+;' "$D/app.css"; then
    ok "a library face is drawn larger and clipped to its circle"
else
    bad "a library face is drawn larger and clipped to its circle -- ${crew:-no rule}"
fi

# 14. parses
if command -v node >/dev/null 2>&1; then
    if node --check "$D/app.js" 2>/dev/null; then ok "app.js parses"; else bad "app.js parses"; fi
else
    ok "app.js parse check skipped: no node on this host"
fi

echo
if [[ $FAILED -eq 0 ]]; then echo "PASS"; exit 0; else echo "FAIL ($FAILED)"; exit 1; fi
