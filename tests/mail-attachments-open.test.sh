#!/usr/bin/env bash
#
# A file mailed to an agent can be opened by that agent.
#
# WHAT WENT WRONG. Two halves, both on the email ingest, and either one alone
# keeps the file shut:
#   1. companions/communication/common/attachments.py announced a file as
#      "[attachment: name (32 KB, mime)]". brin rewrites only the composer
#      marker "[attachment: name, mime, N bytes, id UUID]" into the command
#      that fetches the file, so the agent never learned how to open it.
#   2. companions/communication/common/db.py stored every mailed file as
#      'received' and never ran the scan stage; the attachment API serves only
#      'approved' files, so the fetch answered 403 even for a trusted sender.
#
# THE PROPERTY: the unit tests of this branch (test_attachment_marker.py,
# test_attachment_scan.py) hold against the communication, brin and core-api
# sources of the tree, or of REF with --baseline. Sources come from REF, the
# tests from the working tree, so --baseline shows what the old code does.
#
#   tests/mail-attachments-open.test.sh                  # tree, PASS
#   tests/mail-attachments-open.test.sh --baseline REF   # expect FAIL
#
# Needs a Python with pytest and psycopg2 (set COMM_PY); exits 2 without one.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

PYTHON="${COMM_PY:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in python3 /opt/smart-node/communication/venv/bin/python; do
        if "$candidate" -c 'import pytest, psycopg2' 2>/dev/null; then PYTHON="$candidate"; break; fi
    done
fi
[[ -n "$PYTHON" ]] && "$PYTHON" -c 'import pytest, psycopg2' 2>/dev/null \
    || { echo "mail attachment tests need a Python with pytest and psycopg2 (set COMM_PY)" >&2; exit 2; }

if [[ -n "$BASELINE" ]]; then echo "mailed attachments can be opened (git:$BASELINE)"
else echo "mailed attachments can be opened (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
C=companions/communication
list_common() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" ls-tree --name-only "$BASELINE" "$C/common/" 2>/dev/null
    else (cd "$REPO" && ls -d "$C"/common/*); fi
}
read_src() {
    if [[ -n "$BASELINE" ]]; then git -C "$REPO" show "$BASELINE:$1" 2>/dev/null
    else cat "$REPO/$1" 2>/dev/null; fi
}
mkdir -p "$D/$C/common" "$D/$C/tests" "$D/companions/brin/core" "$D/companions/core-api"
for f in $(list_common); do
    [[ "$f" == *.py ]] && read_src "$f" > "$D/$f"
done
read_src companions/brin/core/attachment_delivery.py > "$D/companions/brin/core/attachment_delivery.py"
read_src companions/core-api/attachment_scan.py      > "$D/companions/core-api/attachment_scan.py"
cp "$REPO/$C/tests/test_attachment_marker.py" "$REPO/$C/tests/test_attachment_scan.py" "$D/$C/tests/"

(cd "$D/$C" && "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_attachment_marker.py tests/test_attachment_scan.py 2>&1) \
    | grep -E '^(FAILED|ERROR)|passed|failed|error' | sed 's/^/  /'
rc=${PIPESTATUS[0]}
echo
if (( rc )); then echo "FAIL"; exit 1; fi
echo "PASS"
