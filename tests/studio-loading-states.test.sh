#!/usr/bin/env bash
# No screen of the Studio shows the word "Loading" while its data is on the way: each shows the shape of what is
# coming (shimmering .skel blocks). The Agents grid draws its cards at once and fills each agent's contacts as its
# own call answers.
set -u
cd "$(dirname "$0")/.."
P=companions/studio/public/next2; fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
words=$(grep -nE ">\s*[Ll]oading(&hellip;|\.\.\.|…)|'[Ll]oading(&hellip;|\.\.\.|…| [a-z]+&hellip;)'|empty\('Loading|textContent = 'loading" $P/js/*.js $P/index.html || true)
chk "no loading word is left in Studio's scripts and page" "[ -z \"\$words\" ]"
[ -n "$words" ] && printf '%s\n' "$words" | head -8
chk "shared skeleton builders exist" "grep -q 'function skRows' $P/js/app.js && grep -q 'function skText' $P/js/app.js && grep -q 'function tasksDashSkel' $P/js/app.js"
chk "the Work Map has its own skeleton" "grep -q 'function wmSkeleton' $P/js/work-map.js && grep -q 'wsk__board' $P/css/work-map.css"
chk "the Agents grid fills contacts per agent, not after all of them" "grep -q 'roster.forEach(async (a) =>' $P/js/app.js && grep -q 'data-contacts=' $P/js/app.js"
chk "a skeleton announces itself to screen readers" "grep -q 'aria-busy=\"true\" aria-label=\"Loading\"' $P/js/app.js"
chk "reduced motion stops the shimmer" "grep -q 'prefers-reduced-motion: reduce){ .skel { animation: none; } }' $P/css/app.css"
chk "the sidebar has no Projects section (it had no backend and was always empty)" "! grep -q 'id=\"projects\"' $P/index.html && ! grep -q \"#projects\" $P/js/app.js"
for f in app agent-page chat-shell work-map; do cp $P/js/$f.js /tmp/_ls_$f.mjs && node --check /tmp/_ls_$f.mjs || fail=1; done
[ $fail = 0 ] && echo "PASS: Studio loading states" || { echo FAIL; exit 1; }
