#!/usr/bin/env bash
#
# An email with an attachment is stored, not a mailbox stopped.
#
# WHAT WENT WRONG. companions/communication/common/db.py inserted attachment
# rows with ON CONFLICT (message_id, content_hash, filename) DO NOTHING. The only
# unique index on those columns is PARTIAL (B02 message_attachments_dedup_idx,
# WHERE content_hash IS NOT NULL AND filename IS NOT NULL), and PostgreSQL
# infers a partial index for ON CONFLICT only when the statement repeats its
# predicate. So the first attachment the node ever met raised
#     there is no unique or exclusion constraint matching the ON CONFLICT specification
# the message stayed unseen, and every poll after it failed on the same mail:
# the mailbox took in nothing more. Found on the live node on 2026-09-10 with a
# test mail; proved inside a rollback on the live database before the fix.
#
# THE PROPERTY: the ON CONFLICT on message_attachments names the same columns
# AND the same predicate as the partial unique index B02 declares. Both are read
# from the sources, so an index regression cannot pass silently.
#
#   tests/email-attachment-on-conflict.test.sh                  # tree, PASS
#   tests/email-attachment-on-conflict.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "email attachment ON CONFLICT (git:$BASELINE)"
else echo "email attachment ON CONFLICT (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src bootstrap/B02-sn-core-schema.sql            > "$D/b02.sql"
read_src companions/communication/common/db.py       > "$D/db.py"
for f in b02.sql db.py; do
    [[ -s "$D/$f" ]] || { echo "  FAIL sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

python3 - "$D/b02.sql" "$D/db.py" <<'PY'
import re, sys
b02 = open(sys.argv[1]).read()
db = open(sys.argv[2]).read()
norm = lambda s: re.sub(r"[()\s]+", " ", s).strip().lower()
failed = 0
def check(name, cond):
    global failed
    print(("  ok   " if cond else "  FAIL ") + name)
    failed += (not cond)

m = re.search(r"CREATE UNIQUE INDEX IF NOT EXISTS message_attachments_dedup_idx ON "
              r"sn_core\.message_attachments USING btree \(([^)]*)\)\s*WHERE\s*\((.*?)\);", b02, re.S)
check("B02 declares the partial dedup index", m is not None)
if not m:
    sys.exit(1)
cols, pred = norm(m.group(1)), norm(m.group(2))

c = re.search(r"INSERT INTO sn_core\.message_attachments.*?ON CONFLICT\s*\(([^)]*)\)(.*?)DO NOTHING", db, re.S)
check("db.py inserts attachment rows with ON CONFLICT", c is not None)
if not c:
    sys.exit(1)
check("ON CONFLICT names the index columns (%s)" % cols, norm(c.group(1)) == cols)
where = c.group(2).strip()
check("ON CONFLICT repeats the index predicate (%s)" % pred,
      where.lower().startswith("where") and norm(where[5:]) == pred)
print()
print("PASS" if not failed else "FAIL (%d)" % failed)
sys.exit(1 if failed else 0)
PY
