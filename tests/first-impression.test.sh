#!/usr/bin/env bash
# What a person meets first: names, messages, and an install that says so when the
# Studio cannot be reached. Each check names a defect seen on a clean install.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/orchestrator/install-on.sh"
W="$ROOT/companions/studio/public/welcome/welcome.js"
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }

# the installed app carries the product's name
grep -q 'Studio' "$ROOT/companions/studio/public/manifest.webmanifest"; check "the app is not called Studio" "$?" 1

# the module that writes the marker is the one the check looks for
marker=$(grep -oE 'companions\.d/[a-z-]+\.json' "$ROOT/companions/brin/main.py" | head -1 | sed 's#.*/##; s/\.json//')
[ -f "$ROOT/companions/$marker.manifest.json" ]; check "the task watcher looks for a module that exists ($marker)" "$?" 0

# any node with a setup token is told where first run is, not only a local one
sed -n '/A node with a setup token/,/^        fi$/p' "$S" | grep -q 'welcome/?token='; check "every node is led to first run" "$?" 0

# an agent with no model says so in plain words, before the runtime answers for it
grep -q 'has no AI model yet' "$ROOT/companions/gateway/backends/cli_claude.py"; check "no model is said in plain words" "$?" 0

# reloading the first-run page goes on with the setup
sed -n '/---- start ----/,$p' "$W" | grep -q 'resume()'; check "a reload continues the setup" "$?" 0
grep -q 'the install put it on your desktop' "$W"; check "the page does not promise a shortcut" "$?" 1
grep -q 'from your desktop' "$ROOT/bootstrap/authum/src/index.php"; check "nor does the sign-in page" "$?" 1

# the System chat wears the product's mark: in the row, the recent list and the header
n=$(grep -c 'SN_MARK\|sysAv(' "$ROOT/companions/studio/public/next2/js/chat-shell.js")
[ "$n" -ge 3 ]; check "the System chat shows the Smart Node mark" "$?" 0
grep -q 'tab__ic"><i data-lucide="server"' "$ROOT/companions/studio/public/next2/js/app.js"; check "and no longer a server icon" "$?" 1

# an HTML preview loads nothing from outside: an image in an agent's reply fires with no click
sed -n '/function htmlPreview/,/^  }/p' "$ROOT/companions/studio/public/next2/js/formatter.js" | grep -q "default-src 'none'"; check "an HTML preview cannot call out" "$?" 0

# a dropped download is tried again
bad=$(grep -hE '^\s*curl -fsSL ' "$ROOT"/companions/flint.sh "$ROOT"/companions/studio.sh "$ROOT"/companions/agent-runtimes.sh \
  "$ROOT"/bootstrap/B02-persistence.sh "$ROOT"/bootstrap/B03-routing.sh "$ROOT"/bootstrap/B04-secrets-slice.sh | grep -vc -- '--retry')
check "every download retries" "$bad" 0

# the installer asks the node for the Studio before it calls the install done
grep -q 'studio_answers' "$S"; check "a Studio nobody can reach is not a success" "$?" 0
exit "$fail"
