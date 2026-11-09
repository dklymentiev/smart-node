#!/usr/bin/env bash
# Settings shows which Smart Node release and interface build this is; the version endpoint carries the release.
set -u
cd "$(dirname "$0")/.."
O=companions/studio; fail=0
chk() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1"; fail=1; fi; }
chk "/api/version returns the release beside the build" "grep -q 'release: releaseNow()' $O/src/server.js && ! grep -q 'const RELEASE =' $O/src/server.js"
chk "the release is read from the installer record when asked, not once at start-up" "grep -q '/etc/smart-node/release' $O/src/server.js"
chk "Settings has an About card" "grep -q 'id=\"aboutRelease\"' $O/public/next2/index.html"
chk "Settings fills it when opened, both ways in" "[ \"\$(grep -c 'loadPanelLink(); loadSignInLink(); loadAbout();' $O/public/next2/js/app.js)\" = 2 ]"
cp $O/public/next2/js/app.js /tmp/_about_check.mjs && node --check /tmp/_about_check.mjs && node --check $O/src/server.js && echo "  ok   both files parse" || fail=1
[ $fail = 0 ] && echo "PASS: settings about version" || { echo FAIL; exit 1; }
