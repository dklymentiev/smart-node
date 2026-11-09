#!/usr/bin/env bash
#
# Gate: the capability column  must be present in
# every layer that exposes or stores it.
#
# WHAT IS CHECKED
#   1. B02-sn-core-schema.sql CREATE TABLE sn_config.agents contains capability
#   2. B02 has the ADD COLUMN IF NOT EXISTS line for the update path
#   3. B02 has the backfill UPDATE for existing rows
#   4. companions/core-api/schemas/agent.py AgentCreate declares capability
#   5. companions/core-api/schemas/agent.py AgentRead declares capability
#   6. companions/core-api/routes/agents.py AGENT_COLS selects a.capability
#   7. companions/core-api/routes/agents.py INSERT includes capability
#   8. AgentUpdate does NOT expose capability (immutability guard)
#   9. Backfill CASE covers all role literals including full-name variants
#      (guards against missing 'Content Manager' -> 'content' etc.)
#
# USAGE
#   tests/agent-capability-column.test.sh                  # working tree
#   tests/agent-capability-column.test.sh --baseline REF   # expect FAIL
#
set -uo pipefail

# Resolve repo root robustly: BASH_SOURCE[0] is the script path (relative or
# absolute). realpath resolves it to absolute, then we go two levels up.
_SCRIPT="$(realpath -q "${BASH_SOURCE[0]}" 2>/dev/null || readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$_SCRIPT")/.." && pwd)"

BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

if [[ -n "$BASELINE" ]]; then
    echo "agent capability column (git:$BASELINE)"
    # Use a temp dir to hold git-extracted files
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    git -C "$ROOT" show "$BASELINE:bootstrap/B02-sn-core-schema.sql" > "$WORK/B02.sql" 2>/dev/null || true
    git -C "$ROOT" show "$BASELINE:companions/core-api/schemas/agent.py" > "$WORK/agent_schema.py" 2>/dev/null || true
    git -C "$ROOT" show "$BASELINE:companions/core-api/routes/agents.py" > "$WORK/agents_route.py" 2>/dev/null || true
    B02="$WORK/B02.sql"
    SCHEMA="$WORK/agent_schema.py"
    AGENTS="$WORK/agents_route.py"
else
    echo "agent capability column (working tree)"
    B02="$ROOT/bootstrap/B02-sn-core-schema.sql"
    SCHEMA="$ROOT/companions/core-api/schemas/agent.py"
    AGENTS="$ROOT/companions/core-api/routes/agents.py"
fi

# All checks use file paths directly, not variables, to avoid bash variable
# size limits and quote-handling subtleties with large files.

# 1. capability in CREATE TABLE
if grep -q "capability text" "$B02"; then
    ok "B02 CREATE TABLE sn_config.agents has capability column"
else
    bad "B02 CREATE TABLE sn_config.agents has capability column -- not found"
fi

# 2. ADD COLUMN IF NOT EXISTS for update path
if grep -q "ADD COLUMN IF NOT EXISTS capability" "$B02"; then
    ok "B02 has ADD COLUMN IF NOT EXISTS capability for the update path"
else
    bad "B02 has ADD COLUMN IF NOT EXISTS capability for the update path -- not found"
fi

# 3. backfill UPDATE for existing rows
if grep -q "SET capability = CASE role" "$B02"; then
    ok "B02 has the capability backfill UPDATE"
else
    bad "B02 has the capability backfill UPDATE -- not found"
fi

# 4. AgentCreate has capability (field must appear before the next class definition)
if python3 - "$SCHEMA" <<'PY'
import sys
src = open(sys.argv[1]).read()
found = False
in_class = False
for line in src.split('\n'):
    if line.startswith('class AgentCreate'):
        in_class = True
    elif in_class and line.startswith('class '):
        break
    elif in_class and 'capability' in line and ':' in line and not line.strip().startswith('#'):
        found = True
        break
sys.exit(0 if found else 1)
PY
then
    ok "AgentCreate declares capability"
else
    bad "AgentCreate declares capability -- not found"
fi

# 5. AgentRead has capability
if python3 - "$SCHEMA" <<'PY'
import sys
src = open(sys.argv[1]).read()
found = False
in_class = False
for line in src.split('\n'):
    if line.startswith('class AgentRead'):
        in_class = True
    elif in_class and line.startswith('class '):
        break
    elif in_class and 'capability' in line and not line.strip().startswith('#'):
        found = True
        break
sys.exit(0 if found else 1)
PY
then
    ok "AgentRead declares capability"
else
    bad "AgentRead declares capability -- not found"
fi

# 6. AGENT_COLS selects a.capability
if grep -q '"a\.capability' "$AGENTS"; then
    ok "AGENT_COLS selects a.capability"
else
    bad "AGENT_COLS selects a.capability -- not found"
fi

# 7. INSERT includes capability
if grep -q "enabled, capability," "$AGENTS"; then
    ok "INSERT INTO sn_config.agents includes capability"
else
    bad "INSERT INTO sn_config.agents includes capability -- not found"
fi

# 8. AgentUpdate does NOT expose capability (immutability)
if python3 - "$SCHEMA" <<'PY'
import sys
src = open(sys.argv[1]).read()
has_field = False
in_class = False
for line in src.split('\n'):
    if line.startswith('class AgentUpdate'):
        in_class = True
    elif in_class and line.startswith('class '):
        break
    elif in_class and 'capability' in line and ':' in line and not line.strip().startswith('#'):
        has_field = True
        break
# We WANT has_field to be False (no capability in AgentUpdate)
sys.exit(0 if not has_field else 1)
PY
then
    ok "AgentUpdate does NOT expose capability (immutability guard)"
else
    bad "AgentUpdate does NOT expose capability (immutability guard) -- it has a capability field"
fi

# 9. Backfill CASE covers all required role literals including full-name variants
#    Guards against a seed querying WHERE capability='content'
#    must not miss content-agent because the CASE only knew the short form 'Content'.
if python3 - "$B02" <<'PY'
import sys, re
src = open(sys.argv[1]).read()
m = re.search(r'SET capability = CASE role(.*?)ELSE slug', src, re.DOTALL)
if not m:
    print("  CASE block not found", file=sys.stderr)
    sys.exit(1)
case_block = m.group(1)
def has_mapping(role, expected):
    pattern = r"WHEN '{}'\s+THEN '{}'".format(re.escape(role), re.escape(expected))
    return bool(re.search(pattern, case_block))
mappings = [
    ('Foreman',                     'configurator'),
    ('Developer',                   'developer'),
    ('sysadmin',                    'sysadmin'),
    ('Sysadmin',                    'sysadmin'),
    ('Secretary',                   'secretary'),
    ('Node Architect',              'architect'),
    ('Architect',                   'architect'),
    ('Project Manager',             'pm'),
    ('Personal Finance Consultant', 'finance'),
    ('Marketing Strategist',        'marketing'),
    ('Content Manager',             'content'),
    ('Social Media Specialist',     'social-media'),
]
missing = [(r, c) for r, c in mappings if not has_mapping(r, c)]
for r, c in missing:
    print(f"  missing: WHEN '{r}' THEN '{c}'", file=sys.stderr)
sys.exit(0 if not missing else 1)
PY
then
    ok "Backfill CASE covers all role literals including full-name variants"
else
    bad "Backfill CASE missing one or more role literal mappings (see stderr above)"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
