#!/usr/bin/env bash
# The panel's Settings page against core-api: every page links to it once, the
# page reads only routes that exist and writes only the digest switch, and access.read (who can
# sign in, every role's holders) is the owner's alone.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
ui = root / 'bootstrap/smart-node-ui/src'
api = root / 'companions/core-api'
fails = 0


def check(name, ok, extra=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else ' -> ' + str(extra)))
    if not ok:
        fails += 1


pages = [p for p in ui.rglob('index.html') if 'class="topbar-nav"' in p.read_text()]
rel = lambda ps: [str(p.relative_to(ui)) for p in ps]
check('every page links to /settings/ once', not rel(p for p in pages if p.read_text().count('href="/settings/" class="nav-item') != 1))
check('Settings comes right after Workflows', not rel(p for p in pages if not re.search(
    r'data-group="workflows">Workflows</a>\s*<a href="/settings/"', p.read_text())))
page = (ui / 'settings/index.html').read_text()
check('the Settings page is active in its own top bar', 'href="/settings/" class="nav-item active"' in page)
check('it loads assets/settings.js and settings.css', '/assets/settings.js' in page and '/assets/settings.css' in page)

js = (ui / 'assets/settings.js').read_text()
called = sorted(set(re.findall(r"get\('(/api/[^'?]+)'", js)))
check('it reads the five routes', called == ['/api/agents', '/api/departments', '/api/node/about', '/api/node/access', '/api/policies'], called)
# The one write is the owner's daily digest switch, and only with limits.write.
digest_call = "fetch('/api/node/digest', { method: 'PUT',"
check('its only write is the digest switch', js.count(digest_call) == 1
      and not re.search(r"method|POST|PUT|PATCH|DELETE", js.replace(digest_call, '')))
check('the digest route requires limits.write', '@router.put("/digest", dependencies=[Depends(requires("limits.write"))])'
      in (api / 'routes/node_settings.py').read_text())

routes = {}
for f in (api / 'routes').glob('*.py'):
    s = f.read_text()
    m = re.search(r'APIRouter\(prefix="([^"]+)"', s)
    base = m.group(1) if m else '/api/' + f.stem.replace('_', '-')
    for path in re.findall(r'@router\.get\("([^"]*)"', s):
        routes[(base + path).rstrip('/') or base] = f.name
main = (api / 'main.py').read_text()
for r in called:
    check('core-api serves GET ' + r, r in routes and re.search(r'app\.include_router\(%s\.router' % routes[r][:-3], main), r)

b02 = (root / 'bootstrap/B02-sn-core-schema.sql').read_text()
check('access.read is in the permission catalogue', "('access.read', " in b02)
holders = re.findall(r"\('([\w-]+)', 'access\.read'\)", b02)
check('only super-admin holds access.read', holders == ['super-admin'], holders)
ns = (api / 'routes/node_settings.py').read_text()
check('the access route requires access.read', 'requires("access.read")' in ns)
check('the account file gives only name, role and date', re.search(r'\{"login": k, "auth_role": .*?"created": ', ns, re.S) and '**v' not in ns)
sys.exit(1 if fails else 0)
PY
