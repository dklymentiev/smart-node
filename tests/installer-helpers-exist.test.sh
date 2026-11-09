#!/usr/bin/env bash
# Every lib.sh-style helper an installer calls (stage_*, register_*, unregister_*,
# manifest_*, report_*, verify_*) is defined in bootstrap/lib.sh or in the
# installer itself. studio.sh --uninstall once ended on stage_clear, which never
# existed: the component was removed and the script still failed at its last step.
set -u
cd "$(dirname "$0")/.."
PY_BIN="$(command -v python3 || command -v python)"
"$PY_BIN" - <<'PY'
import glob, io, re, sys
lib = io.open('bootstrap/lib.sh', encoding='utf-8').read()
defined = set(re.findall(r'^([a-z_][a-z0-9_]*)\(\)\s*\{', lib, re.M))
missing = []
for p in sorted(glob.glob('companions/*.sh') + glob.glob('bootstrap/B0*.sh')):
    s = io.open(p, encoding='utf-8').read()
    own = set(re.findall(r'^([a-z_][a-z0-9_]*)\(\)\s*\{', s, re.M))
    for n, line in enumerate(s.split('\n'), 1):
        m = re.match(r'\s+((?:stage|register|unregister|manifest|report|verify)_[a-z_]+)\b', line)
        if m and m.group(1) not in defined and m.group(1) not in own:
            missing.append('%s:%d %s' % (p, n, m.group(1)))
for x in missing: print('  FAIL undefined helper: ' + x)
print('PASS: installers call only helpers that exist' if not missing else 'FAIL')
sys.exit(1 if missing else 0)
PY
