#!/usr/bin/env bash
#
# Bulk import markdown files into Mesh.
#
# Usage:
#   ./examples/bulk_import.sh /path/to/notes/*.md
#   ./examples/bulk_import.sh ~/documents/worklogs/*.md
#
# Each file becomes a document. Tags are derived from the directory name.

MESH_URL="${MESH_URL:-http://localhost:8000}"

if [ $# -eq 0 ]; then
    echo "Usage: $0 <file1.md> [file2.md] ..."
    echo "  Import markdown files into Mesh"
    echo ""
    echo "Environment:"
    echo "  MESH_URL  API base URL (default: http://localhost:8000)"
    exit 1
fi

count=0
errors=0

for file in "$@"; do
    if [ ! -f "$file" ]; then
        echo "[SKIP] Not a file: $file"
        continue
    fi

    content=$(cat "$file")
    filename=$(basename "$file")
    directory=$(basename "$(dirname "$file")")

    # Build tags from directory and filename
    tags="[\"source:import\", \"directory:$directory\"]"

    response=$(curl -s -X PUT "$MESH_URL/" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
            --arg content "$content" \
            --arg filename "$filename" \
            --argjson tags "$tags" \
            '{content: $content, filename: $filename, tags: $tags}'
        )")

    guid=$(echo "$response" | jq -r '.guid // empty')
    if [ -n "$guid" ]; then
        echo "[OK] $filename -> $guid"
        ((count++))
    else
        echo "[ERROR] $filename: $response"
        ((errors++))
    fi
done

echo ""
echo "Imported: $count, Errors: $errors"
