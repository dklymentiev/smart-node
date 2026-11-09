#!/usr/bin/env bash
# The panel's Memory page against core-api's /api/memory: every page links to it
# instead of "Memory soon", the empty Settings and Workflows "soon" items are
# gone, the page calls only routes that exist, and the agent page links to one
# agent's memory.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 - "$ROOT" <<'PY'
import ast
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
ui = root / 'bootstrap/smart-node-ui/src'
fails = 0


def check(name, ok, extra=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else ' -> ' + str(extra)))
    if not ok:
        fails += 1


pages = [p for p in ui.rglob('index.html') if 'class="topbar-nav"' in p.read_text()]
rel = lambda ps: [str(p.relative_to(ui)) for p in ps]
check('no page says Memory soon', not rel(p for p in pages if 'Memory <em>soon</em>' in p.read_text()))
check('every page links to /memory/', not rel(p for p in pages if 'href="/memory/"' not in p.read_text()))
check('no page shows the empty Settings item', not rel(p for p in pages if '>Settings <em>soon</em>' in p.read_text()))
check('no subnav shows the empty Workflows item', not rel(p for p in pages if 'subnav-item soon" aria-disabled="true">Workflows' in p.read_text()))

page = (ui / 'memory/index.html').read_text()
check('Memory is active in its own top bar', '<a href="/memory/" class="nav-item active" data-group="memory">Memory</a>' in page)
check('the page loads memory.js, memory.css and the shell',
      'src="/assets/memory.js' in page and 'href="/assets/memory.css' in page and 'src="/assets/shell.js' in page)
js = (ui / 'assets/memory.js').read_text()
ids = set(re.findall(r"\$\('(\w+)'\)", js))
missing = sorted(i for i in ids if f'id="{i}"' not in page)
check('every element the script uses is on the page', not missing, missing)

# The script's calls are core-api routes.
tree = ast.parse((root / 'companions/core-api/routes/memory.py').read_text())
routes = set()
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        for d in node.decorator_list:
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ('get', 'post'):
                routes.add((d.func.attr.upper(), '/api/memory' + d.args[0].value))
used = {('POST' if "'/search'" in m else 'GET', '/api/memory' + m.strip("'")) for m in re.findall(r"API \+ ('/[a-z]+')", js)}
used |= {('GET', '/api/memory/documents/{guid}')} if "API + '/documents/' + encodeURIComponent(guid)" in js else set()
check('the page calls only routes core-api has', used and used <= routes, sorted(used - routes))
check('every memory route is the owner\'s memory.read',
      (root / 'companions/core-api/routes/memory.py').read_text().count('requires("memory.read")') == len(routes))
b02 = (root / 'bootstrap/B02-sn-core-schema.sql').read_text()
grants = re.findall(r"\('([\w-]+)', 'memory\.read'\)", b02)
check('memory.read is in the catalogue and only super-admin holds it',
      "('memory.read'," in b02 and grants == ['super-admin'], grants)

ae = (ui / 'agents/edit/index.html').read_text()
check('the agent page links to that agent\'s memory', 'id="kn-open"' in ae and "'/memory/'" in ae)
sys.exit(1 if fails else 0)
PY
