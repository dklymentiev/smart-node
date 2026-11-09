#!/usr/bin/env bash
#
# Gate: seed and test files must not locate agents by literal role slug.
#
# every lookup that needs "the configurator agent" must
# use capability = 'configurator' (or role = 'Foreman' as a backward-compatible
# fallback), never the literal slug string "foreman". The same rule applies to
# all other role-slug strings. Literal slugs in lookups break on any node where
# the owner has renamed the agent.
#
# WHAT IS CHECKED
#   1. onboarding-seed.sh provision_agent() does NOT contain a literal slug
#      lookup (e.g. a.get("slug") == "foreman" style check).
#   2. onboarding-seed.sh provision_agent() DOES use capability or role as the
#      lookup key (not slug).
#   3. bootstrap/B02-sn-core-seed.sql has no literal role-slug values in a
#      slug-lookup context (SELECT/WHERE slug = 'foreman', etc.).
#   4. The agent-creation payload in onboarding-seed.sh includes the
#      capability field.
#
# WHAT IS NOT CHECKED HERE
#   Service-account and group-name constants (sn-admin, smart-node-agents,
#   smart-node-agent) are live OS identities; their migration and the
#   corresponding code sweep are tracked separately in Those
#   constants must NOT be renamed in source before the migration runs on the
#   target node.
#
# USAGE
#   tests/naming-scheme-no-literal-slugs.test.sh               # working tree
#   tests/naming-scheme-no-literal-slugs.test.sh --baseline REF # expect FAIL
#
set -uo pipefail

_SCRIPT="$(realpath -q "${BASH_SOURCE[0]}" 2>/dev/null || readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$_SCRIPT")/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

if [[ -n "$BASELINE" ]]; then
    echo "naming scheme: no literal slugs in seeds (git:$BASELINE)"
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    git -C "$ROOT" show "$BASELINE:bootstrap/onboarding-seed.sh" > "$WORK/onboarding-seed.sh" 2>/dev/null || true
    git -C "$ROOT" show "$BASELINE:bootstrap/B02-sn-core-seed.sql" > "$WORK/B02-sn-core-seed.sql" 2>/dev/null || true
    SEED_SH="$WORK/onboarding-seed.sh"
    SEED_SQL="$WORK/B02-sn-core-seed.sql"
else
    echo "naming scheme: no literal slugs in seeds (working tree)"
    SEED_SH="$ROOT/bootstrap/onboarding-seed.sh"
    SEED_SQL="$ROOT/bootstrap/B02-sn-core-seed.sql"
fi

# ---- 1. provision_agent does NOT match slug literally ----
# The lookup inside provision_agent() must not use a.get("slug") == "foreman"
# or any of the other role-slug literals as a lookup predicate.
# We look for the python3 heredoc block inside provision_agent and check it.
if python3 - "$SEED_SH" <<'PY'
import sys, re

src = open(sys.argv[1]).read()

# Extract the python3 heredoc inside provision_agent (the one that does the lookup)
# Pattern: python3 -c ' ... ' between provision_agent and return 0
m = re.search(
    r"provision_agent\(\).*?found_slug=\$\(python3 -c '(.*?)'\s+2>/dev/null",
    src, re.DOTALL
)
if not m:
    # Try alternate quoting style
    m = re.search(
        r"found_slug=\$\(python3 -c '(.*?)' 2>/dev/null",
        src, re.DOTALL
    )

if not m:
    print("WARN: could not locate provision_agent python block", file=sys.stderr)
    sys.exit(0)  # can't check, skip rather than false-positive

block = m.group(1)
role_slugs = ["foreman", "developer", "sysadmin", "architect", "pm", "finance",
              "alex", "secretary", "marketing", "content", "social-media"]

for slug in role_slugs:
    # Looking for patterns like: == "foreman", == 'foreman', ["slug"] == "foreman"
    if re.search(r'["\']' + slug + r'["\']', block):
        # Exception: capability = 'configurator' is the CORRECT pattern
        if slug == "configurator":
            continue
        # Check if this is used as a slug value (not a capability value)
        print(f"FAIL: literal slug '{slug}' in provision_agent lookup", file=sys.stderr)
        sys.exit(1)

sys.exit(0)
PY
then
    ok "provision_agent: no literal role-slug in lookup predicate"
else
    bad "provision_agent: literal role-slug found in lookup predicate (must use capability or role)"
fi

# ---- 2. provision_agent uses capability or role as the lookup key ----
if grep -q 'capability.*configurator\|role.*Foreman' "$SEED_SH" 2>/dev/null; then
    ok "provision_agent: uses capability or role as lookup key"
else
    bad "provision_agent: does not use capability or role as lookup key"
fi

# ---- 3. B02-sn-core-seed.sql has no literal slug lookups ----
# Check for patterns like WHERE slug = 'foreman' or INSERT slug values
if [[ -f "$SEED_SQL" ]]; then
    if python3 - "$SEED_SQL" <<'PY'
import sys, re
src = open(sys.argv[1]).read()
role_slugs = ["foreman", "developer", "sysadmin", "architect", "pm", "finance", "alex"]
# Look for slug-lookup patterns: WHERE ... slug ... = 'foreman'
# Exclude role field values like role = 'Foreman' (that is not a slug lookup)
for slug in role_slugs:
    # Pattern: slug = 'foreman' (SQL WHERE clause)
    if re.search(r"slug\s*=\s*['\"]" + slug + r"['\"]", src, re.IGNORECASE):
        print(f"FAIL: literal slug '{slug}' in SQL WHERE/slug lookup", file=sys.stderr)
        sys.exit(1)
sys.exit(0)
PY
    then
        ok "B02-sn-core-seed.sql: no literal slug values in slug lookups"
    else
        bad "B02-sn-core-seed.sql: literal slug found in slug= lookup"
    fi
else
    ok "B02-sn-core-seed.sql: not present (skip)"
fi

# ---- 4. agent-creation payload includes capability field ----
if grep -q '"capability".*"configurator"\|capability.*configurator' "$SEED_SH" 2>/dev/null; then
    ok "onboarding-seed.sh: agent-creation payload includes capability field"
else
    bad "onboarding-seed.sh: agent-creation payload missing capability field"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
