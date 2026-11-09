# Known technical debt and gate gaps

Entries in this file are problems that were found but not fixed in the same
commit, or gate gaps that exist at a known point in time. Each entry has a
status: `open` (not yet addressed) or `resolved` (fixed or gated).

---

## RESOLVED: release gate did not verify FK constraints are co-located with their migration

Status: partially gated in 1.12.33; residual gap documented below.

Found: 2026-09-08 while an existing-node update failed at B02 line ~1482.

The release gate checked that migration ADD COLUMN columns appeared in the
baked schema (B02). It did not check that FK constraints referencing those
columns were co-located with the migration that delivers the column, not in
B02's generic $fk$ loop.

A FK constraint in B02's $fk$ VALUES loop runs before Phase 2 migrations.
On a clean install the column already exists (CREATE TABLE emits it). On an
UPDATE the table pre-exists without the column; when $fk$ tries to ADD
CONSTRAINT on the absent column, psql exits 3.

Fix: the three actor_id FKs (agents, users, contacts) were moved out of the
$fk$ loop and replaced with column-existence-guarded DO blocks. Each
migration (034, 036, 037) already had its own FK constraint DO block.

Gate: tests/b02-fk-column-guard.test.sh runs as part of run-source.sh.
It catches the specific pattern: a FK in the $fk$ loop referencing a column
that (a) a migration delivers via ADD COLUMN IF NOT EXISTS AND (b) B02 does
NOT add via ADD COLUMN IF NOT EXISTS before the $fk$ block runs. A future
migration-delivered FK in the $fk$ loop will fail this test.

Residual gap: the gate does NOT catch FKs on columns that are only in B02's
CREATE TABLE block and NOT in any migration ADD COLUMN IF NOT EXISTS. Such
a column is absent on update (CREATE TABLE is a no-op for existing tables)
but the gate reports it as safe. If a future column follows this pattern,
fix the gate by adding an ADD COLUMN IF NOT EXISTS to B02 before the $fk$
block (making it explicit), which then also removes it from the gate gap.
