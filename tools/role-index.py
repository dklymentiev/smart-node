#!/usr/bin/env python3
"""The index of the role library for the setup agent's brief.

  tools/role-index.py [LIBRARY_DIR]   prints the block that sits between the
                                      role-index markers at the end of configurator.md

One line per department: its name, what it is for, and its roles (slug and name, the
head first). The setup agent reads a role in full before proposing it, so the index only
has to let it find the right file. Regenerate after any change to the library; the test
tests/role-library.test.sh fails while the brief and the library disagree.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "companions", "core-api", "role-library")


def front(path):
    t = open(path, encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---", t, re.S)
    return dict((k.strip(), v.strip()) for k, v in (l.split(":", 1) for l in m.group(1).splitlines() if ":" in l)) if m else {}


deps = json.load(open(os.path.join(LIB, "departments.json"), encoding="utf-8"))
roles = {}
for f in sorted(os.listdir(LIB)):
    if f.endswith(".md"):
        fm = front(os.path.join(LIB, f))
        roles.setdefault(fm.get("category"), []).append(fm)

out = ["<!-- role-index:begin -->",
       "## The role library",
       "",
       "Ready-made roles ship with the node, one file each: /srv/smart-node/specialists/<slug>.md",
       "(also GET /api/specialists/library/{slug}). By department, the head first:",
       ""]
for d in deps:
    rs = sorted(roles.get(d["id"], []), key=lambda r: (r.get("archetype") != "head", r.get("slug")))
    if not rs:
        continue
    names = ", ".join(f"{r['slug']} ({r['name']})" for r in rs)
    out.append(f"- **{d['name']}** ({d['purpose']}): {names}")
out.append("<!-- role-index:end -->")
print("\n".join(out))
