#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE="$ROOT/companions/core-api"
PYTHON="${CORE_API_PY:-python3}"
if ! "$PYTHON" -c 'import pytest, fastapi, psycopg2, pydantic' 2>/dev/null; then
  for candidate in "$ROOT/.venv/bin/python" "$HOME"/.venvs/*/bin/python python3; do
    if "$candidate" -c 'import pytest, fastapi, psycopg2, pydantic' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  done
fi
"$PYTHON" -c 'import pytest, fastapi, psycopg2, pydantic' 2>/dev/null \
  || { echo "axis B tests need a Python with pytest and core-api dependencies (set CORE_API_PY)" >&2; exit 2; }
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT
SN_AGENTS_ROOT="$TEST_ROOT/agents" SN_AGENT_TEMPLATE="$TEST_ROOT/template" \
  PYTHONPATH="$CORE" "$PYTHON" -m pytest -q \
  --confcutdir="$CORE/tests/axis_b" "$CORE/tests/axis_b/test_axis_b.py"
