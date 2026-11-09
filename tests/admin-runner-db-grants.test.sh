#!/usr/bin/env bash
#
# The admin-runner's database role is granted every table its code touches.
#
# WHAT WENT WRONG. The admin-runner connects as the least-privilege role
# sn_admin_runner, and companions/admin-runner.sh grants that role table by
# table. The role model made the service read sn_config.permissions,
# sn_config.role_permissions and sn_core.actor_roles, at startup and on every
# call, and the installer was not given the three grants. The service died in
# its startup check with "permission denied for table permissions" and
# restarted forever. Every test had passed: they ran against a throwaway
# database as its superuser. An update of a stand found it.
#
# THE PROPERTY. Every table named in a FROM, JOIN or INSERT INTO in the
# admin-runner's own modules has the matching grant in the installer.
#
#   tests/admin-runner-db-grants.test.sh                  # tree, PASS
#   tests/admin-runner-db-grants.test.sh --baseline REF   # REF, expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
mkdir -p "$D/code"
if [[ -n "$BASELINE" ]]; then
    echo "admin-runner: database grants cover the code (git:$BASELINE)"
    git -C "$REPO" show "$BASELINE:companions/admin-runner.sh" > "$D/installer.sh" || exit 1
    for f in $(git -C "$REPO" ls-tree --name-only "$BASELINE" companions/admin-runner/ | grep '\.py$'); do
        git -C "$REPO" show "$BASELINE:$f" > "$D/code/$(basename "$f")"
    done
else
    echo "admin-runner: database grants cover the code (working tree)"
    cp "$REPO/companions/admin-runner.sh" "$D/installer.sh" || exit 1
    cp "$REPO"/companions/admin-runner/*.py "$D/code/"
fi

python3 - "$D" <<'PY'
import glob, re, sys
d = sys.argv[1]
need = {}
for path in sorted(glob.glob(d + "/code/*.py")):
    src = open(path).read()
    for verb, table in re.findall(r"\b(FROM|JOIN|INSERT INTO|UPDATE|DELETE FROM)\s+(sn_[a-z_]+\.[a-z_]+)", src):
        priv = {"FROM": "SELECT", "JOIN": "SELECT", "INSERT INTO": "INSERT",
                "UPDATE": "UPDATE", "DELETE FROM": "DELETE"}[verb]
        need.setdefault(table, set()).add(priv)
granted = {}
for privs, table in re.findall(r"^GRANT\s+([A-Z, ]+?)\s+ON\s+(sn_[a-z_]+\.[a-z_]+)\s+TO", open(d + "/installer.sh").read(), re.M):
    granted.setdefault(table, set()).update(p.strip() for p in privs.split(","))
failed = 0
if not need:
    print("  FAIL the admin-runner's queries are readable")
    failed = 1
for table in sorted(need):
    missing = sorted(need[table] - granted.get(table, set()))
    if missing:
        print("  FAIL %s: the code needs %s, the installer does not grant it" % (table, ", ".join(missing)))
        failed += 1
    else:
        print("  ok   %s: %s" % (table, ", ".join(sorted(need[table]))))
print()
print("FAIL" if failed else "PASS")
sys.exit(1 if failed else 0)
PY
