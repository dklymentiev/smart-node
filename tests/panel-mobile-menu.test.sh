#!/usr/bin/env bash
# The panel's phone menu is a fixed drawer inside the top bar. It shows only if the bar is not the frame of fixed
# things (no backdrop-filter on the bar itself) and the dimmer sits under the bar, not over the drawer.
set -u
cd "$(dirname "$0")/.."
python3 - <<'PY'
import re, sys
css = open("bootstrap/smart-node-ui/src/assets/shell.css", encoding="utf-8").read()
fails = 0
def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += 0 if ok else 1
bar = re.search(r"\n\.topbar \{(.*?)\n\}", css, re.S).group(1)
check("the bar itself carries no backdrop-filter", "backdrop-filter" not in bar)
before = re.search(r"\.topbar::before \{(.*?)\n\}", css, re.S)
check("the frosted layer is behind the bar, on its ::before", bool(before) and "backdrop-filter" in before.group(1) and "z-index: -1" in before.group(1))
bar_z = int(re.search(r"z-index:\s*(\d+)", bar).group(1))
scrim_z = int(re.search(r"\.menu-scrim \{[^}]*z-index:\s*(\d+)", css).group(1))
check("the dimmer is under the bar (bar %d, dimmer %d)" % (bar_z, scrim_z), scrim_z < bar_z)
nav = re.search(r"\.topbar-nav \{ position: fixed; top: var\(--topbar-h\);", css)
check("the drawer is still the fixed drawer under the bar", bool(nav))
sys.exit(1 if fails else 0)
PY
rc=$?
[ $rc = 0 ] && echo "PASS: panel mobile menu" || { echo FAIL; exit 1; }
