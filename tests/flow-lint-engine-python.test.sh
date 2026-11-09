#!/usr/bin/env bash
# flow-lint.py finds the engine's python when the python it was started with
# has PyYAML but not jsonschema.
#
# The import door (companions/core-api/flow_publish.py) runs flow-lint.py as a
# program, so the shebang's python3 runs it: on a Debian node that python has
# python3-yaml and no jsonschema. flow-lint re-executed into the engine venv only
# when PyYAML was missing, so S0 refused every flow the door was given.
#
# The test starts flow-lint with a python that cannot import jsonschema and
# points SN_FLOW_PY at one that can. It needs a python with PyYAML and
# jsonschema; without one it says so and skips.
#
#   tests/flow-lint-engine-python.test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
LINT="$REPO/companions/flows/tools/flow-lint.py"
ROOT="$REPO/companions/flows/agents"

echo "flow-lint: a python without jsonschema hands over to the engine's python"

REAL=""
for c in "${SN_FLOW_PY:-}" /opt/smart-node/workflows/venv/bin/python python3; do
    [[ -n "$c" ]] || continue
    if "$c" -c "import yaml, jsonschema" >/dev/null 2>&1; then REAL="$(command -v "$c")"; break; fi
done
if [[ -z "$REAL" ]]; then
    echo "  skip no python with PyYAML and jsonschema here"
    echo; echo "PASS (skipped)"; exit 0
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/hide/jsonschema" "$T/bin"
printf 'raise ImportError("hidden by the test")\n' > "$T/hide/jsonschema/__init__.py"
# The python the shebang would find: PyYAML yes, jsonschema no.
printf '#!/bin/sh\nPYTHONPATH="%s" exec "%s" "$@"\n' "$T/hide" "$REAL" > "$T/bin/system-python"
# The engine's python: everything.
printf '#!/bin/sh\nunset PYTHONPATH\nexec "%s" "$@"\n' "$REAL" > "$T/bin/engine-python"
chmod 755 "$T/bin/system-python" "$T/bin/engine-python"

FAILED=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s\n' "$1"; FAILED=$((FAILED + 1)); }

if "$T/bin/system-python" -c "import jsonschema" >/dev/null 2>&1; then
    bad "the stand-in system python cannot import jsonschema"
else
    ok "the stand-in system python cannot import jsonschema"
fi

out="$(SN_FLOW_PY="$T/bin/engine-python" "$T/bin/system-python" "$LINT" --root "$ROOT" --flow task-simple 2>&1)"; rc=$?
if [[ $rc -eq 0 ]] && ! grep -q "jsonschema is not installed" <<< "$out"; then
    ok "task-simple lints clean, checked against the schema by the engine's python"
else
    bad "task-simple lints clean, checked against the schema by the engine's python (exit $rc: $(grep -m1 -E 'ERROR|jsonschema' <<< "$out"))"
fi

out="$(SN_FLOW_PY="$T/nowhere/python" "$T/bin/system-python" "$LINT" --root "$ROOT" --flow task-simple 2>&1)"; rc=$?
if [[ $rc -ne 0 ]] && grep -q "jsonschema is not installed" <<< "$out"; then
    ok "with no engine python, S0 still says jsonschema is missing"
else
    bad "with no engine python, S0 still says jsonschema is missing (exit $rc)"
fi

echo
if (( FAILED )); then echo "FAIL ($FAILED)"; exit 1; fi
echo "PASS"
