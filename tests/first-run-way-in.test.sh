#!/usr/bin/env bash
# After an install the owner can get in: the page styles load before sign-in, the page
# names the file that holds the token, and a local node (no password) has its way in.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/orchestrator/install-on.sh"
B06="$ROOT/bootstrap/B06-web-interface.sh"
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }

# one sign-up form: Studio's first-run page; the panel has no /setup and no
# route that lets /setup past sign-in
[ -e "$ROOT/bootstrap/smart-node-ui/src/setup" ]; check "the panel has no /setup page" "$?" 1
grep -q 'PathPrefix(\\`/setup\\`)' "$B06"; check "no route lets /setup past sign-in" "$?" 1
grep -q '127.0.0.1/setup' "$B06"; check "the panel readiness does not read /setup" "$?" 1

grep -q 'tools/smart-node-signin' "$S"; check "the installer puts the sign-in tool on the node" "$?" 0
grep -q 'welcome/?token=' "$S"; check "the installer tells a local node where to go" "$?" 0

# signing in to a subscription moves the agent onto that runtime
W="$ROOT/companions/studio/public/welcome/welcome.js"
awk "/#codeGo/,/say\(msg, 'Connected\.'\)/" "$W" | grep -q 'chosenModel ='; check "the subscription sign-in gives the agent a model" "$?" 0

# Settings lists what the node has; the full list belongs to the install
N="$ROOT/companions/studio/public/next2"
grep -q 'filter((m) => m.installed)' "$N/js/app.js"; check "Settings shows installed modules only" "$?" 0
awk '/^async function loadModules/,/^}/' "$N/js/app.js" | grep -q 'Not installed'; check "and never says Not installed there" "$?" 1
grep -q 'run the installer again' "$N/index.html"; check "and sends nobody to the installer" "$?" 1

# the first-run page offers characters: every pack it names ships with the release
A="$ROOT/companions/studio/public/avatars"
[ -s "$A/packs.json" ]; check "the release has the list of avatar packs" "$?" 0
for p in $(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(" ".join(x["id"] for x in (d["packs"] if isinstance(d,dict) else d)))' "$A/packs.json" 2>/dev/null); do
  n=$(ls "$A/$p" 2>/dev/null | wc -l); [ "$n" -gt 0 ]; check "pack $p has its faces" "$?" 0
done
exit "$fail"
