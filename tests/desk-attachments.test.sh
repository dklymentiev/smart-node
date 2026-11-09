#!/usr/bin/env bash
#
# A file attached in the Studio chat is stored, in the node's one attachment
# store, with the desk as its parent.
#
# WHAT WAS MISSING. The composer let the owner attach files, as a
# preview: nothing was saved, the agent never saw them, and a reload lost them.
# The upload contract's
# store keys every file to a sn_core.messages row, and a desk chat is not one.
#
# THE PROPERTIES:
#   1. schema: message_id may be NULL, desk_id exists with an FK to sn_core.desks
#      ON DELETE CASCADE, exactly one parent, 'approved' in the status set, a
#      scan_reason, an index by desk; an installed node converges (ALTERs)
#   2. the scan stage exists and every desk upload passes through it; phase 1
#      approves the owner's own upload and records why
#   3. an image is served as the type its BYTES prove; a claimed image the bytes
#      do not prove stays opaque; SVG is never inline
#   4. the display name never builds a path; the stored uuid carries a suffix
#      derived from content type so file readers see its type
#   5. routes: upload, list, content, delete, all through one access rule
#      (owner writes; owner, the desk's agent and the whole-node seats read);
#      only approved files are served
#   6. deleting a desk removes its directory; the rows go by cascade
#   7. the attachment API decides a desk file by the desk, not by a NULL message
#   8. core-api.sh no longer takes /srv/data/attachments from communication,
#      and lays desks/ for itself, recorded in the manifest
#
#   tests/desk-attachments.test.sh                  # tree, PASS
#   tests/desk-attachments.test.sh --baseline REF   # expect FAIL
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}

if [[ -n "$BASELINE" ]]; then echo "desk attachments (git:$BASELINE)"
else echo "desk attachments (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
read_src bootstrap/B02-sn-core-schema.sql          > "$D/b02.sql"
read_src companions/core-api/routes/desks.py        > "$D/desks.py"
read_src companions/core-api/routes/attachments.py  > "$D/attachments.py"
read_src companions/core-api/attachment_scan.py     > "$D/scan.py"
read_src companions/core-api/lib/agent_fs.py        > "$D/agent_fs.py"
read_src companions/core-api/companions/recompose-affected.py > "$D/recompose.py"
read_src companions/core-api.sh                     > "$D/core-api.sh"
for f in b02.sql desks.py attachments.py core-api.sh; do
    [[ -s "$D/$f" ]] || { bad "sources are readable ($f)"; echo; echo "FAIL"; exit 1; }
done

# Body of a top-level def, flag-based (a range closes on its own first line).
body_of() { awk -v n="$2" 'f && /^(def|async def|class|@)/{exit} $0 ~ "^(async )?def "n"\\("{f=1} f' "$1"; }

# ---------------------------------------------------------------- 1. schema
table=$(awk '/^CREATE TABLE IF NOT EXISTS sn_core.message_attachments/,/^\);/' "$D/b02.sql")
if grep -qE '^\s+message_id\s+bigint,' <<<"$table" && grep -qE '^\s+desk_id\s+uuid,' <<<"$table" \
   && grep -qE '^\s+scan_reason\s+text,' <<<"$table"; then
    ok "a fresh node: message_id nullable, desk_id and scan_reason exist"
else
    bad "a fresh node: message_id nullable, desk_id and scan_reason exist"
fi
if grep -q 'ADD COLUMN IF NOT EXISTS desk_id uuid;' "$D/b02.sql" \
   && grep -q 'ALTER COLUMN message_id DROP NOT NULL;' "$D/b02.sql" \
   && grep -q "CHECK (status IN ('received', 'approved', 'quarantined', 'processed', 'expired'))" "$D/b02.sql"; then
    ok "an installed node converges to the same shape"
else
    bad "an installed node converges to the same shape"
fi
if grep -q 'FOREIGN KEY (desk_id) REFERENCES sn_core.desks(id) ON DELETE CASCADE' "$D/b02.sql" \
   && grep -q 'CHECK (num_nonnulls(message_id, desk_id, task_id) = 1)' "$D/b02.sql" \
   && grep -q 'message_attachments_desk_id_idx' "$D/b02.sql"; then
    ok "a desk file leaves with its desk, has exactly one parent, and is indexed by desk"
else
    bad "a desk file leaves with its desk, has exactly one parent, and is indexed by desk"
fi

# ------------------------------------------------ 2-4. the scan stage, run
if [[ ! -s "$D/scan.py" ]]; then
    bad "the scan stage exists (attachment_scan.py)"
else
    while IFS= read -r line; do
        case "$line" in ok*) ok "${line#ok }";; FAIL*) bad "${line#FAIL }";; esac
    done < <(python3 - "$D/scan.py" <<'PY'
import sys, types
src = open(sys.argv[1]).read()
m = types.ModuleType("attachment_scan"); exec(compile(src, "attachment_scan.py", "exec"), m.__dict__)
def check(name, cond): print(("ok " if cond else "FAIL ") + name)
check("the owner's own upload is approved, and says why", m.verdict(m.DESK_OWNER, True) == ("approved", "owner-upload"))
check("a trusted sender's file is approved, and says why", m.verdict("email", True) == ("approved", "trusted-sender"))
check("an untrusted sender's file is not approved", m.verdict("email", False)[0] == "received")
check("a PNG is proved by its bytes", m.sniff_image_type(b"\x89PNG\r\n\x1a\n....") == "image/png")
check("a JPEG and a WEBP are proved by their bytes",
      m.sniff_image_type(b"\xff\xd8\xff\xe0") == "image/jpeg" and m.sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp")
check("HTML named like an image proves nothing", m.sniff_image_type(b"<html><script>") is None)
check("SVG is never proved as an image", m.sniff_image_type(b"<svg xmlns=") is None)
check("a display name cannot carry a path", m.safe_filename("../../etc/passwd") == "passwd" and m.safe_filename("a\\b\\c.txt") == "c.txt")
check("an empty or control-only name still has a name", m.safe_filename("\x00\x01") == "file")
suffix = getattr(m, "storage_suffix", lambda _ctype: None)
check("stored names carry a safe suffix derived from content type",
      suffix("image/png") == ".png" and suffix("application/pdf") == ".pdf"
      and suffix("text/csv") == ".csv" and suffix("application/x-unknown") == ".bin")
PY
)
fi

if grep -q 'aid + attachment_scan.storage_suffix(ctype)' "$D/desks.py"; then
    ok "the storage path is uuid plus the content-derived suffix"
else
    bad "the storage path is uuid plus the content-derived suffix"
fi

# --------------------------------------------------------------- 5. routes
for r in upload_desk_attachment list_desk_attachments desk_attachment_content delete_desk_attachment; do
    if grep -qE "^(async )?def $r\(" "$D/desks.py"; then ok "route: $r"; else bad "route: $r"; fi
done
if grep -q 'attachment_scan.verdict(attachment_scan.DESK_OWNER, True)' <<<"$(body_of "$D/desks.py" upload_desk_attachment)"; then
    ok "every desk upload passes through the scan stage"
else
    bad "every desk upload passes through the scan stage"
fi
n=$(grep -c '_desk_for(cur, desk_id, principal, write=' "$D/desks.py")
if [[ "$n" -ge 4 ]]; then ok "all four routes ask the same access rule ($n calls)"; else bad "all four routes ask the same access rule ($n calls)"; fi
if grep -q 'if r\["status"\] != attachment_scan.APPROVED:' <<<"$(body_of "$D/desks.py" desk_attachment_content)"; then
    ok "only an approved file is served"
else
    bad "only an approved file is served"
fi
if grep -q '"X-Content-Type-Options": "nosniff"' "$D/desks.py" && grep -q '_INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")' "$D/desks.py"; then
    ok "only proved images render inline; everything else downloads, never sniffed"
else
    bad "only proved images render inline; everything else downloads, never sniffed"
fi

# ------------------------------------------------------ 6. desk deletion
if grep -q 'shutil.rmtree(_desk_dir(desk_id))' <<<"$(body_of "$D/desks.py" delete_desk)"; then
    ok "deleting a desk removes its files"
else
    bad "deleting a desk removes its files"
fi

# -------------------------------------------------- 7. the attachment API
if grep -q 'if row\["message_id"\] is None:' "$D/attachments.py"; then
    ok "a desk file is decided by its desk, not by a NULL message"
else
    bad "a desk file is decided by its desk, not by a NULL message"
fi

# -------------------------------------------------------- 8. the installer
if grep -q '\[\[ -d /srv/data/attachments \]\] || install -d' "$D/core-api.sh" \
   && ! grep -qE 'install -d .*"\$SMART_NODE_GROUP" /srv/data /srv/data/attachments$' "$D/core-api.sh" \
   && grep -q 'manifest_record dir /srv/data/attachments/desks' "$D/core-api.sh"; then
    ok "core-api.sh no longer takes the shared directory, and lays desks/ for itself"
else
    bad "core-api.sh no longer takes the shared directory, and lays desks/ for itself"
fi

# ------------------------------------------------ 9. generated instructions
if [[ $(grep -c '# Attachments in Studio chat' "$D/agent_fs.py") -eq 1 ]] \
   && grep -q '/srv/tools/attachments/attachment-read.py <id>' "$D/agent_fs.py" \
   && grep -q 'audio and video attachments' "$D/agent_fs.py" \
   && grep -q 'recompose_agent_prompts' "$D/core-api.sh" \
   && grep -q 'recompose-affected.py" --all' "$D/core-api.sh" \
   && grep -q 'archived_at IS NULL' "$D/recompose.py"; then
    ok "the canonical composer teaches every existing and future agent about attachments"
else
    bad "the canonical composer teaches every existing and future agent about attachments"
fi

echo
if [[ "$FAILED" -eq 0 ]]; then echo "PASS"; else echo "FAIL ($FAILED)"; fi
exit $(( FAILED > 0 ))
