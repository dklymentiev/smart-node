#!/usr/bin/env bash
# ask_owner must stay unavailable until the HITL action is implemented.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT"
if [[ "${1:-}" == "--baseline" ]]; then
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  git -C "$ROOT" archive "${2:-50c6152c}" companions/core-api/routes/limits.py bootstrap/smart-node-ui/src/assets/limits.js | tar -x -C "$WORK"
fi
python3 - "$WORK" <<'PY'
import ast
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1])
tree = ast.parse((root / 'companions/core-api/routes/limits.py').read_text())
function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_check_profile')
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
scope = {'HTTPException': HTTPException, 'ProfileInput': object}
exec(compile(ast.Module(body=[function], type_ignores=[]), 'limits.py', 'exec'), scope)
check = scope['_check_profile']
for code, period in (('spend_usd', 'month'), ('node_spend_usd', 'day'), ('wakes_unattended', 'hour')):
    try:
        check(code, period, types.SimpleNamespace(action='ask_owner'))
    except HTTPException as exc:
        assert exc.status_code == 422, (code, exc.status_code)
    else:
        raise AssertionError(f'{code} accepted ask_owner')
check('spend_usd', 'month', types.SimpleNamespace(action='warn'))
print('ok   API rejects ask_owner for every limit and still allows spending warn')
PY
ui="$WORK/bootstrap/smart-node-ui/src/assets/limits.js"
if grep -qE "var actions = .*ask_owner" "$ui"; then
  echo 'FAIL panel offers ask_owner' >&2
  exit 1
fi
echo 'ok   panel does not offer ask_owner'
