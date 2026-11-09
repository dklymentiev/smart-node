#!/usr/bin/env bash
# A cut turn names its limit to a person without a fraction.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$ROOT"
if [[ "${1:-}" == "--baseline" ]]; then
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  git -C "$ROOT" archive "${2:-origin/main}" companions/gateway companions/core-api | tar -x -C "$WORK"
fi
python3 - "$WORK" <<'PY'
import importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('supervisor_under_test', root/'companions/core-api/lib/supervisor.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
phrase = getattr(mod, 'turn_limit_phrase', None)
assert phrase is not None, 'supervisor has no turn_limit_phrase'
cases = {7: '7-second', 97: '97-second', 59: '59-second', 60: '1-minute', 600: '10-minute', 601: '601-second', 90.4: '90-second'}
for seconds, want in cases.items():
    got = phrase(seconds)
    assert got == want, f'{seconds} s reads {got!r}, expected {want!r}'
print('ok   whole minutes read as minutes, everything else as seconds')
for rel in ('companions/gateway/sessions_v2_routes.py', 'companions/core-api/routes/limits.py'):
    text = (root/rel).read_text()
    assert 'turn_limit_phrase(' in text, f'{rel} does not use the shared phrase'
    assert 'minutes:g' not in text, f'{rel} still divides the ceiling by sixty for display'
print('ok   the chat line and the owner alert use the same phrase')
PY
