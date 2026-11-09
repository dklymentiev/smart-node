#!/usr/bin/env bash
#
# Gate: every FOREIGN KEY in B02's $fk$ VALUES loop must NOT reference a
# column that is delivered by a migration (ADD COLUMN IF NOT EXISTS).
#
# WHY THIS EXISTS. B02 runs in Phase 1 (Layer C) before Phase 2 migrations.
# On a clean install the column is present because CREATE TABLE emits it. On
# an UPDATE the table pre-exists without the column; the column arrives only
# when the migration runs (Phase 2, after Layer C). If the FK is in B02's
# $fk$ loop, psql exits 3 on the update path -- found in 1.12.33,
# fixed in
#
# WHAT IS CHECKED. For each FOREIGN KEY (col) entry in B02's $fk$ VALUES
# block: verify that no migration file (companions/core-api/migrations/*.sql)
# has an "ALTER TABLE schema.table ADD COLUMN IF NOT EXISTS col" for the same
# table. If one does, that column is migration-delivered and the FK must live
# in the migration, not in the $fk$ loop.
#
# Columns that only appear in B02's CREATE TABLE (predating the migration
# system) are allowed. Columns added by both B02 ADD COLUMN IF NOT EXISTS
# and a migration are also allowed (the migration ADD COLUMN is idempotent).
# The specific case that breaks is: column in CREATE TABLE + in migration
# ADD COLUMN + FK in $fk$ loop.
#
# USAGE:
#   tests/b02-fk-column-guard.test.sh                 # working tree
#   tests/b02-fk-column-guard.test.sh --baseline REF  # git:REF, expect FAIL
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

B02_REL="bootstrap/B02-sn-core-schema.sql"
MIGRATIONS_REL="companions/core-api/migrations"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
B02_TMP="$WORK/B02.sql"
MIG_DIR="$WORK/migrations"
mkdir -p "$MIG_DIR"

if [[ -n "$BASELINE" ]]; then
    git -C "$ROOT" show "$BASELINE:$B02_REL" > "$B02_TMP" 2>/dev/null \
        || { echo "cannot read $B02_REL at $BASELINE" >&2; exit 2; }
    # Extract migration files at the baseline ref
    for mig in $(git -C "$ROOT" ls-tree --name-only "$BASELINE" "$MIGRATIONS_REL/" 2>/dev/null); do
        fname="${mig##*/}"
        git -C "$ROOT" show "$BASELINE:$mig" > "$MIG_DIR/$fname" 2>/dev/null || true
    done
    WHAT="$BASELINE"
else
    cp "$ROOT/$B02_REL" "$B02_TMP"
    cp "$ROOT/$MIGRATIONS_REL/"*.sql "$MIG_DIR/" 2>/dev/null || true
    WHAT="working tree"
fi

echo "$B02_REL ($WHAT)"

python3 <<PYEOF
import re
import sys
from pathlib import Path

b02_path    = "$B02_TMP"
mig_dir     = Path("$MIG_DIR")
what        = "$WHAT"
b02_rel     = "$B02_REL"

# Established historical patterns: columns delivered by OLD migrations (already
# applied on all nodes for many releases) whose FKs remain in the \$fk\$ loop.
# These are safe because the migration ran before the node was ever upgraded to
# a version with this test. Add new entries ONLY after confirming the migration
# predates all supported upgrade sources.
HISTORICAL_SAFE = {
    # Migration 008b (multi-release ago): tenant_id on sn_core tables
    ("sn_core.cron_runs", "tenant_id"),
    ("sn_core.crons",     "tenant_id"),
    ("sn_core.tools",     "tenant_id"),
}

with open(b02_path) as f:
    b02 = f.read()

# --- Step 1: extract the \$fk\$ block ---
fk_match = re.search(r'DO \\\$fk\\\$(.*?)\\\$fk\\\$;', b02, re.DOTALL)
if not fk_match:
    print('  (no \$fk\$ block found -- nothing to check)')
    print(f'\nPASS  [{b02_rel} @ {what}]')
    sys.exit(0)

fk_block = fk_match.group(1)

# --- Step 2: extract (schema.table, col) pairs from FOREIGN KEY entries ---
fk_entries = re.findall(
    r'ALTER TABLE (sn_\w+\.\w+) ADD CONSTRAINT \w+ FOREIGN KEY \((\w+)\)',
    fk_block
)
if not fk_entries:
    print('  (no FOREIGN KEY entries in \$fk\$ block -- nothing to check)')
    print(f'\nPASS  [{b02_rel} @ {what}]')
    sys.exit(0)

# --- Step 3: build a map of columns delivered by migrations ---
# A column is migration-delivered if any migration has:
#   ALTER TABLE schema.table ADD COLUMN IF NOT EXISTS colname
migration_cols = {}  # {schema.table: set(col_names)}
for mig_file in sorted(mig_dir.glob("*.sql")):
    content = mig_file.read_text()
    for m in re.finditer(
        r'ALTER TABLE (\w+\.\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)',
        content
    ):
        migration_cols.setdefault(m.group(1), set()).add(m.group(2))

# Also check B02 ADD COLUMN IF NOT EXISTS lines (those appear BEFORE the
# \$fk\$ loop and make the update path safe -- the column exists by the time
# \$fk\$ runs, so FKs on those columns are safe in \$fk\$).
b02_add_col = {}  # {schema.table: set(col_names)}
fk_block_pos = b02.find(fk_block)
b02_before_fk = b02[:fk_block_pos] if fk_block_pos >= 0 else b02
for m in re.finditer(
    r'ALTER TABLE (\w+\.\w+)\s+ADD COLUMN IF NOT EXISTS\s+(\w+)',
    b02_before_fk
):
    b02_add_col.setdefault(m.group(1), set()).add(m.group(2))

# --- Step 4: check each FK entry ---
fails = 0
checked = 0
for (schema_table, col) in fk_entries:
    if (schema_table, col) in HISTORICAL_SAFE:
        print(f'  ok   {schema_table}.{col}  (historical; migration pre-dates all upgrade sources)')
        checked += 1
        continue

    in_migration = col in migration_cols.get(schema_table, set())
    in_b02_add   = col in b02_add_col.get(schema_table, set())

    if in_migration and not in_b02_add:
        # Column is migration-delivered and B02 does NOT add it before \$fk\$ runs.
        # On an UPDATE path the column is absent when \$fk\$ runs => FK will fail.
        print(f'  FAIL {schema_table}.{col}: FOREIGN KEY ({col}) is in the \$fk\$ loop '
              f'and a migration delivers this column via ADD COLUMN IF NOT EXISTS. '
              f'The FK must live in that migration, not in \$fk\$.')
        fails += 1
    else:
        print(f'  ok   {schema_table}.{col}')
        checked += 1

print()
if fails > 0:
    print(f'FAIL  {fails} check(s) failed, {checked} passed  [{b02_rel} @ {what}]')
    sys.exit(1)
print(f'PASS  {checked} FK column(s) checked -- none are migration-delivered without B02 guard  [{b02_rel} @ {what}]')
PYEOF
