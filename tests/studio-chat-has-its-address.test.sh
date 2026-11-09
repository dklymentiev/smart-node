#!/usr/bin/env bash
#
# A chat opened in the Studio has its own address at once.
#
# WHAT WENT WRONG. The owner opened an agent (/agents/foreman/overview), pressed
# + and New chat. The chat opened and the address bar stayed on the agent page;
# it became /chats/<uuid> only after he clicked the chat's tab. A reload, the
# back button or a copied link therefore lost the chat he had just made.
#
# createDeskFor() created the desk and called setActive() without
# history.pushState. Every New chat goes through it: the agent page +, the
# agent popover, the tab-bar picker. The popover's "recent" entries had the same
# gap. The tab click was the only path that set the address.
#
# THE PROPERTIES:
#   1. createDeskFor sets /chats/<id> before it activates the new chat
#   2. the agent popover's recent entries do the same
#   3. the tab click still does (it is the reference behaviour)
#   4. app.js still parses
#
#   tests/studio-chat-has-its-address.test.sh                  # tree, PASS
#   tests/studio-chat-has-its-address.test.sh --baseline REF   # expect FAIL
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

if [[ -n "$BASELINE" ]]; then echo "Studio: a chat has its address (git:$BASELINE)"
else echo "Studio: a chat has its address (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src companions/studio/public/next2/js/app.js > "$D/app.js"
[[ -s "$D/app.js" ]] || { bad "app.js is readable"; echo; echo "FAIL"; exit 1; }

# Body of a top-level function: from its declaration to the next top-level
# function. A flag, not an awk range: a range closes on its own first line.
body_of() {
    awk -v n="$1" 'f && /^(async )?function /{exit} $0 ~ "^(async )?function "n"\\("{f=1} f' "$D/app.js"
}

# 1. createDeskFor: the address is set, and before the chat is activated.
b=$(body_of createDeskFor)
push=$(grep -n "history.pushState({}, '', '/chats/' + encodeURIComponent(desk.id));" <<<"$b" | head -1 | cut -d: -f1)
act=$(grep -n 'setActive(desk.id);' <<<"$b" | head -1 | cut -d: -f1)
if [[ -n "$push" && -n "$act" && "$push" -lt "$act" ]]; then
    ok "a new chat gets /chats/<id> as it opens"
else
    bad "a new chat gets /chats/<id> as it opens -- the bar keeps the page it came from"
fi

# 2. the agent popover's recent entries.
if grep -q "history.pushState({}, '', '/chats/' + encodeURIComponent(b.dataset.open)); setActive(b.dataset.open);" <<<"$(body_of openAgentMenu)"; then
    ok "a recent chat from the agent menu gets its address"
else
    bad "a recent chat from the agent menu gets its address"
fi

# 3. the reference behaviour is still there.
if grep -q "history.pushState({}, '', '/chats/' + encodeURIComponent(el.dataset.id));" <<<"$(body_of renderTabs)"; then
    ok "a tab click still sets the address"
else
    bad "a tab click still sets the address"
fi

# 5. The address follows whatever chat is on screen, not only the paths that
#    create or pick one. The owner, later the same day: the Studio root showed
#    a live chat under a bare "/". route() falls back to setView('chats') for "/"
#    and unknown paths, and the Chats nav pushes "/".
if grep -q "history.replaceState({}, '', want);" <<<"$(body_of syncChatUrl)"; then
    ok "one rule writes the chat's address, without adding a back-button step"
else
    bad "one rule writes the chat's address, without adding a back-button step"
fi
sa=$(body_of setActive)
mv=$(grep -n 'activeId = id;' <<<"$sa" | head -1 | cut -d: -f1)
sy=$(grep -n 'syncChatUrl();' <<<"$sa" | head -1 | cut -d: -f1)
if [[ -n "$mv" && -n "$sy" && "$mv" -lt "$sy" ]]; then
    ok "switching chats updates the address after the active chat moves"
else
    bad "switching chats updates the address after the active chat moves"
fi
if grep -q "if (v === 'chats' && !/^\\\\/chats\\\\/\[^/\]+\$/.test(snPath())) syncChatUrl();" <<<"$(body_of setView)"; then
    ok "showing chats from / or the nav gives the address the active chat's link"
else
    bad "showing chats from / or the nav gives the address the active chat's link"
fi

# 4. syntax, when node is available (it is on the node).
if command -v node >/dev/null 2>&1; then
    if node --check "$D/app.js" 2>/dev/null; then ok "app.js parses"; else bad "app.js parses"; fi
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
