#!/usr/bin/env bash
# Mesh Memory pre-search hook for Claude Code
# Reads user prompt from stdin (JSON), queries Mesh API, returns relevant context
#
# Input: JSON on stdin with { "prompt": "user text..." }
# Output: text printed to stdout (injected as system-reminder)

set -euo pipefail

MESH_API="http://localhost:8000"
MAX_RESULTS=3
MAX_CHARS=300

# Read the hook input JSON from stdin
INPUT=$(cat)

# Extract the prompt text
PROMPT=$(echo "$INPUT" | jq -r '.prompt // empty' 2>/dev/null)

# Skip if prompt is empty or too short (< 10 chars)
if [ -z "$PROMPT" ] || [ ${#PROMPT} -lt 10 ]; then
  exit 0
fi

# Skip for simple greetings and commands
if echo "$PROMPT" | grep -qiE '^(hi|hello|hey|ok|yes|no|sure|thanks|yep|nope|/|commit|push|pull)'; then
  exit 0
fi

# Query Mesh semantic search
RESPONSE=$(curl -sf --max-time 3 \
  -X POST "${MESH_API}/search" \
  -H "Content-Type: application/json" \
  -d "{\"query\": $(echo "$PROMPT" | jq -Rs .), \"limit\": ${MAX_RESULTS}, \"threshold\": 0.82}" \
  2>/dev/null) || exit 0

# Check if we got results
RESULT_COUNT=$(echo "$RESPONSE" | jq -r '.results | length' 2>/dev/null) || exit 0

if [ "$RESULT_COUNT" = "0" ] || [ -z "$RESULT_COUNT" ]; then
  exit 0
fi

# Format output
echo "# Mesh Memory Context"
echo ""
echo "Found ${RESULT_COUNT} relevant documents:"
echo ""

echo "$RESPONSE" | jq -r --argjson max "$MAX_CHARS" '.results[:3] | to_entries[] | {
  i: (.key + 1),
  guid: .value.guid,
  score: (.value.similarity_score // .value.score // 0),
  tags: ([.value.tags[]? | select(startswith("type:") or startswith("project:") or startswith("date:"))] | join(", ")),
  content: (.value.content // "" | if length > $max then .[:$max] + "..." else . end)
} | "**\(.i). [\(.guid)]** (score: \(.score), \(.tags))\n\(.content)\n"' 2>/dev/null || exit 0
