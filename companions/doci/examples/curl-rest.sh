#!/usr/bin/env bash
# DOCI REST walkthrough — create, read, thread, version, delete.
#
# Requires: curl, jq. Expects a running dev stack on :8080.
#
# Run:
#   chmod +x examples/curl-rest.sh
#   API_KEY=$(openssl rand -hex 32) ./examples/curl-rest.sh
# (the dev stack accepts any key — DOCI_API_KEY_HASH is preconfigured)

set -euo pipefail

API="${DOCI_API_URL:-http://localhost:8080/api}"
KEY="${DOCI_API_KEY:-${API_KEY:-dev-key}}"
H_KEY=(-H "X-API-Key: ${KEY}")
H_JSON=(-H "Content-Type: application/json")

say() { printf '\n=== %s ===\n' "$1"; }

say "1. health"
curl -fsS "${API}/health.php" | jq .

say "2. create"
GUID=$(curl -fsS "${H_KEY[@]}" "${H_JSON[@]}" -X POST "${API}/documents.php" \
  -d '{"path":"examples/curl-walkthrough","title":"Curl walkthrough","content":"# Curl walkthrough\n\nFirst paragraph.\n\nSecond paragraph for threading."}' \
  | tee /dev/stderr \
  | jq -r .guid)
echo "Created GUID: ${GUID}"

say "3. read (with content)"
curl -fsS "${H_KEY[@]}" "${API}/documents.php?guid=${GUID}&content=1" | jq .

say "4. update tags"
curl -fsS "${H_KEY[@]}" "${H_JSON[@]}" -X PUT "${API}/documents.php" \
  -d "{\"guid\":\"${GUID}\",\"tags\":[\"example\",\"walkthrough\"]}" | jq .

say "5. start a thread anchored to a passage"
THREAD=$(curl -fsS "${H_KEY[@]}" "${H_JSON[@]}" -X POST "${API}/thread.php" \
  -d "{\"parent_guid\":\"${GUID}\",\"title\":\"What does this mean?\",\"quote\":\"Second paragraph for threading.\",\"message\":\"Asking the agent for context.\"}" \
  | tee /dev/stderr \
  | jq -r .guid)
echo "Thread GUID: ${THREAD}"

say "6. list versions (snapshot was auto-created when the thread was opened)"
curl -fsS "${H_KEY[@]}" "${API}/versions.php?document_guid=${GUID}" | jq .

say "7. inbox quick capture"
curl -fsS "${H_KEY[@]}" "${H_JSON[@]}" -X POST "${API}/inbox.php?action=add" \
  -d '{"content":"Idea: cron the live benchmark on every push.","tags":"ops,ideas"}' | jq .

say "8. soft delete"
curl -fsS "${H_KEY[@]}" -X DELETE "${API}/documents.php?guid=${GUID}" | jq .

say "done"
