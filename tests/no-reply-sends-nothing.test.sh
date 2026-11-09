#!/usr/bin/env bash
#
# A final answer of exactly NO_REPLY completes the turn and sends nothing.
#
# WHAT WENT WRONG. companions/communication/dispatch.py had no silent outcome
# for a turn: any closing text was delivered to the whole thread, and an empty
# answer failed the turn. Agents copied on one mail thread answered each other
# until the thread flooded (a node in the field, 2026-10-05).
#
# THE PROPERTY: the branch's tests/test_no_reply.py holds against the
# communication sources of the tree, or of REF with --baseline. Sources come
# from REF, the tests (and the test_live_chat harness they use) from the
# working tree.
#
#   tests/no-reply-sends-nothing.test.sh                  # tree, PASS
#   tests/no-reply-sends-nothing.test.sh --baseline REF   # expect FAIL
#
# Needs a Python with pytest, psycopg2 and requests (set COMM_PY); exits 2
# without one.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
BASELINE=""
[[ "${1:-}" == "--baseline" ]] && BASELINE="${2:-}"

NEED='import pytest, psycopg2, requests'
PYTHON="${COMM_PY:-}"
if [[ -z "$PYTHON" ]]; then
    for candidate in python3 /opt/smart-node/communication/venv/bin/python; do
        if "$candidate" -c "$NEED" 2>/dev/null; then PYTHON="$candidate"; break; fi
    done
fi
[[ -n "$PYTHON" ]] && "$PYTHON" -c "$NEED" 2>/dev/null \
    || { echo "NO_REPLY tests need a Python with pytest, psycopg2 and requests (set COMM_PY)" >&2; exit 2; }

if [[ -n "$BASELINE" ]]; then echo "NO_REPLY sends nothing (git:$BASELINE)"
else echo "NO_REPLY sends nothing (working tree)"; fi

D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
C=companions/communication
if [[ -n "$BASELINE" ]]; then
    git -C "$REPO" archive "$BASELINE" "$C" | tar -x -C "$D" \
        || { echo "  FAIL $C is readable at $BASELINE"; echo; echo "FAIL"; exit 1; }
else
    mkdir -p "$D/companions" && cp -r "$REPO/$C" "$D/companions/"
fi
mkdir -p "$D/$C/tests"
cp "$REPO/$C/tests/test_no_reply.py" "$REPO/$C/tests/test_live_chat.py" "$D/$C/tests/"

(cd "$D/$C" && "$PYTHON" -m pytest -q -p no:cacheprovider tests/test_no_reply.py 2>&1) \
    | grep -E '^(FAILED|ERROR)|passed|failed|error' | sed 's/^/  /'
rc=${PIPESTATUS[0]}
echo
if (( rc )); then echo "FAIL"; exit 1; fi
echo "PASS"
