#!/usr/bin/env bash
# Turn limits must offer and accept only the actions the gateway enforces.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT"
if [[ "${1:-}" == "--baseline" ]]; then
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  git -C "$ROOT" archive "${2:-50c6152c}" companions/core-api/routes/limits.py bootstrap/smart-node-ui/src/assets/limits.js bootstrap/smart-node-ui/src/limits/index.html bootstrap/smart-node-ui/src/agents/edit/index.html | tar -x -C "$WORK"
fi
python3 - "$WORK" <<'PY'
import ast
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1])
source = (root / 'companions/core-api/routes/limits.py').read_text()
tree = ast.parse(source)
function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_check_profile')
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
scope = {'HTTPException': HTTPException, 'ProfileInput': object}
exec(compile(ast.Module(body=[function], type_ignores=[]), 'limits.py', 'exec'), scope)
check = scope['_check_profile']
for action in ('pause', 'refuse'):
    check('turn_seconds', 'turn', types.SimpleNamespace(action=action))
for action in ('warn', 'ask_owner'):
    try:
        check('turn_seconds', 'turn', types.SimpleNamespace(action=action))
    except HTTPException as exc:
        assert exc.status_code == 422, (action, exc.status_code)
    else:
        raise AssertionError(f'turn_seconds accepted {action}')
print('ok   API accepts only pause and refuse for turn_seconds')
PY
ui="$WORK/bootstrap/smart-node-ui/src/assets/limits.js"
grep -qE 'turn_seconds.*(pause|refuse)' "$ui"
grep -qE 'select\(cell\(tr\),.*p\.code|select\(cell\(tr\),.*item\.code' "$ui"
echo 'ok   panel selects actions using the limit code'
for html in bootstrap/smart-node-ui/src/limits/index.html bootstrap/smart-node-ui/src/agents/edit/index.html; do
  grep -qE 'limits[.]js[?]v=[A-Za-z0-9._-]+' "$WORK/$html"
done
echo 'ok   both panel pages refresh the action list after update'
