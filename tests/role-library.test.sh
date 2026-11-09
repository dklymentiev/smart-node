#!/usr/bin/env bash
# The role library: the release's ready-made roles as files, laid by core-api into
# /srv/smart-node/specialists and read by the setup agent. Checks the files (front
# matter, departments), the core-api route that reads them (one role by slug, the list
# without bodies), the installer step, and the index in the setup agent's brief.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$ROOT/companions/core-api/role-library"
fail=0
check() { if [ "$2" = "$3" ]; then echo "[OK]   $1"; else echo "[FAIL] $1: got '$2', wanted '$3'"; fail=1; fi; }

n=$(ls "$LIB"/*.md 2>/dev/null | wc -l)
[ "$n" -gt 50 ]; check "the release carries the role library ($n roles)" "$?" 0
bad=$(python3 - "$LIB" <<'EOF'
import json, os, sys
lib = sys.argv[1]
deps = {d["id"] for d in json.load(open(os.path.join(lib, "departments.json")))}
bad = []
for f in sorted(os.listdir(lib)):
    if not f.endswith(".md"):
        continue
    t = open(os.path.join(lib, f), encoding="utf-8").read()
    head = t.split("---", 2)[1] if t.startswith("---") else ""
    fm = dict(l.split(":", 1) for l in head.strip().splitlines() if ":" in l)
    fm = {k.strip(): v.strip() for k, v in fm.items()}
    if fm.get("slug") != f[:-3]: bad.append(f + ": slug")
    if fm.get("archetype") not in ("head", "specialist"): bad.append(f + ": archetype")
    if fm.get("category") not in deps: bad.append(f + ": department " + fm.get("category", "?"))
    if not fm.get("name") or not fm.get("tagline"): bad.append(f + ": name/tagline")
    if any(ord(c) > 127 for c in t): bad.append(f + ": not ASCII")
print("; ".join(bad))
EOF
)
check "every role has slug, name, archetype, a known department, a tagline, ASCII only" "$bad" ""
grep -l '^category: fun' "$LIB"/*.md >/dev/null 2>&1; check "the showcase characters stay out of the library" "$?" 1
grep -q '"id": "personal"' "$LIB/departments.json"; check "there is a Personal department" "$?" 0

S="$ROOT/companions/core-api.sh"
grep -q '^    lay_role_library$' "$S"; check "core-api lays the library" "$?" 0
grep -q 'chown -R root:root "$ROLE_LIBRARY_DIR"' "$S"; check "the library is root-owned (no agent rewrites a role)" "$?" 0

# the route, against a copy of the library (the core-api venv has FastAPI; SKIP without it)
PY=/opt/smart-node/core-api/venv/bin/python
if [ -x "$PY" ]; then
  out=$(cd "$ROOT/companions/core-api" && "$PY" - "$LIB" <<'EOF' 2>&1
import sys
from pathlib import Path
import routes.specialists as s
s.LIBRARY_DIR = Path(sys.argv[1])
lst = s.list_specialist_library()["specialists"]
one = s.get_specialist_library_role("bookkeeper")
assert all("body_md" not in x for x in lst), "the list carries bodies"
assert one["category"] == "finance" and one["archetype"] == "specialist" and one["tagline"], one
assert one["body_md"].startswith("---"), "no body"
try:
    s.get_specialist_library_role("no-such-role"); print("no 404")
except Exception as e:
    print("404" if getattr(e, "status_code", 0) == 404 else repr(e))
print("ok", len(lst))
EOF
)
  case "$out" in *"404"*"ok "*) r=0 ;; *) r=1; echo "$out" | tail -5 ;; esac
  check "core-api lists roles without bodies and gives one role by slug (404 otherwise)" "$r" 0
else
  echo "SKIP  the route check needs the core-api venv"
fi

# the setup agent's brief carries an index of the library that matches it
C="$ROOT/companions/srv-skeleton/agents/_specialists/configurator.md"
diff <(python3 "$ROOT/tools/role-index.py" "$LIB") <(sed -n '/^<!-- role-index:begin -->$/,/^<!-- role-index:end -->$/p' "$C") >/dev/null
check "the brief's role index matches the library (tools/role-index.py)" "$?" 0
exit "$fail"
