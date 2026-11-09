#!/usr/bin/env bash
# Every service class in node-versioning/classes.json names the module that
# brings it ("component"), and that module exists in companions/. classes-verify
# skips a service store whose module is not installed; a service class without
# a component would be demanded on every node, and a minimal node failed its
# timer on the planner, mesh and doci databases it never had (2026-10-30).
set -euo pipefail
ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
python3 - "$ROOT" <<'PY'
import json, os, sys
root = sys.argv[1]
d = json.load(open(os.path.join(root, "node-versioning", "classes.json"), encoding="utf-8"))
bad = []
for c in d["classes"]:
    if c.get("kind") != "service" or c.get("status") == "gap":
        continue
    comp = c.get("component")
    if not comp:
        bad.append(f"{c['class']}: no component")
    elif not os.path.isfile(os.path.join(root, "companions", comp + ".sh")):
        bad.append(f"{c['class']}: component {comp} has no companions/{comp}.sh")
if bad:
    print("FAIL classes-components: " + "; ".join(bad)); sys.exit(1)
print("PASS classes-components")
PY
