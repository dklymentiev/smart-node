#!/bin/sh
# Pre-seed demo content so the version bar + quote anchors are visible
# out of the box on the demo pages.
#
# Three blocks, each idempotent (skips if data already present):
#   1. Farm dashboard  -- one thread on a CSA-share quote (auto-creates a version)
#   2. Root index      -- three threads on different sentences (auto-creates 3 versions)
#   3. Incident report -- manual snapshot of "draft" content, then PUT current to "final"
#
# Called from scripts/docker-entrypoint.sh after index-documents.php has
# registered the files in the documents table.

set -e

DEV_USER="${DEV_USER:-dev}"

# Helper: look up GUID by exact path. Echo empty if not found.
guid_for() {
    php -r "
require '/var/www/html/src/config.php';
\$r = get_db()->prepare('SELECT guid FROM documents WHERE path = ? AND deleted_at IS NULL');
\$r->execute([\$argv[1]]);
\$row = \$r->fetch();
echo \$row ? \$row['guid'] : '';
" -- "$1" 2>/dev/null || true
}

# Helper: count versions for an original document GUID.
version_count() {
    php -r "
require '/var/www/html/src/config.php';
\$r = get_db()->prepare('SELECT COUNT(*) FROM documents WHERE original_guid = ? AND doc_type = ? AND deleted_at IS NULL');
\$r->execute([\$argv[1], 'version']);
echo \$r->fetchColumn();
" -- "$1" 2>/dev/null || echo "0"
}

post_thread() {
    # $1 = documentGuid, $2 = quote, $3 = comment
    curl -sS -X POST http://localhost/api/thread.php \
        -H "Remote-User: $DEV_USER" \
        -H "Content-Type: application/json" \
        -d "$(php -r 'echo json_encode(["documentGuid"=>$argv[1],"quote"=>$argv[2],"comment"=>$argv[3]]);' -- "$1" "$2" "$3")" \
        >/dev/null
}

# ------------------------------------------------------------------
# 1. Farm dashboard -- one quoted-passage thread
# ------------------------------------------------------------------
FARM_GUID="$(guid_for 'farm/index.md')"
if [ -n "$FARM_GUID" ]; then
    if [ "$(version_count "$FARM_GUID")" -eq 0 ]; then
        post_thread "$FARM_GUID" \
            "We added 24 new CSA shares this quarter — Mendocino word-of-mouth still beats our paid ads." \
            "What does the new Fort Bragg dropoff look like for September? Could we sustain a third weekly run if numbers hold?"
        echo "[seed-demo] farm dashboard: 1 thread + 1 version seeded"
    else
        echo "[seed-demo] farm dashboard: versions already present, skipping"
    fi
fi

# ------------------------------------------------------------------
# 2. Root index -- three demo threads anchored to different sentences
# ------------------------------------------------------------------
ROOT_GUID="$(guid_for 'index.md')"
if [ -n "$ROOT_GUID" ]; then
    if [ "$(version_count "$ROOT_GUID")" -eq 0 ]; then
        post_thread "$ROOT_GUID" \
            "Every document has a UUID that survives renames. Links don't rot." \
            "Does this hold across folder reorgs too -- if I move a doc deep into another folder, the GUID URL stays the same? What about across a git history rewrite?"

        post_thread "$ROOT_GUID" \
            "Right-click a sentence, start a discussion attached to that exact selection — and to the snapshot of the page at that moment." \
            "What if the same selection is highlighted by two people in two browsers at the same time? Do both get their own thread and their own snapshot?"

        post_thread "$ROOT_GUID" \
            "A reverse proxy authenticates browser users (Traefik / Authum / any forward-auth). API key for service-to-service. DOCI owns no user database." \
            "Is there a sample Caddy config for a single-user Tailscale setup? The Traefik example assumes Authum and TLS termination."

        echo "[seed-demo] root index: 3 threads + 3 versions seeded"
    else
        echo "[seed-demo] root index: versions already present, skipping"
    fi
fi

# ------------------------------------------------------------------
# 3. Incident -- manual snapshot (draft) then update to "final"
# ------------------------------------------------------------------
INCIDENT_GUID="$(guid_for 'farm/incidents/2026-05-12-irrigation.md')"
FINAL_FILE=/var/www/html/scripts/seed-data/incident-final.md

if [ -n "$INCIDENT_GUID" ] && [ -f "$FINAL_FILE" ]; then
    if [ "$(version_count "$INCIDENT_GUID")" -eq 0 ]; then
        # Snapshot the current "draft" content into V-1.
        curl -sS -X POST http://localhost/api/versions.php \
            -H "Remote-User: $DEV_USER" \
            -H "Content-Type: application/json" \
            -d "{\"document_guid\":\"$INCIDENT_GUID\"}" >/dev/null

        # Update the live document to the "final" content.
        php -r 'echo json_encode(["guid"=>$argv[1],"content"=>file_get_contents($argv[2])]);' \
            -- "$INCIDENT_GUID" "$FINAL_FILE" > /tmp/incident-put.json

        curl -sS -X PUT http://localhost/api/documents.php \
            -H "Remote-User: $DEV_USER" \
            -H "Content-Type: application/json" \
            --data @/tmp/incident-put.json >/dev/null

        rm -f /tmp/incident-put.json
        echo "[seed-demo] incident: V-1 snapshot + current updated to final"
    else
        echo "[seed-demo] incident: versions already present, skipping"
    fi
fi
