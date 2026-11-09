#!/usr/bin/env bash
# The panel's Planner page (task-status dictionaries) against the planner it
# talks to: every page links to it instead of "Planner soon", and the page sends
# only the fields and legacy stages the planner's dictionaries API accepts.
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
soon = [str(p.relative_to(ui)) for p in pages if 'Planner <em>soon</em>' in p.read_text()]
check('no page says Planner soon', not soon, soon)
for item in ('Documents', 'Mail', 'Voice'):
    left = [str(p.relative_to(ui)) for p in pages if f'>{item} <em>soon</em>' in p.read_text()]
    check(f'no page shows the empty {item} item', not left, left)
conf = (root / 'bootstrap/smart-node-ui/nginx.conf').read_text()
check('pages are served to revalidate', re.search(r'location / \{[^}]*Cache-Control "no-cache"', conf) is not None)
missing = [str(p.relative_to(ui)) for p in pages if 'href="/planner/"' not in p.read_text()]
check('every page with the top bar links to /planner/', not missing, missing)

page = (ui / 'planner/index.html').read_text()
check('the Planner page is active in its own top bar',
      '<a href="/planner/" class="nav-item active" data-group="planner">Planner</a>' in page)
check('the Planner page loads its own stylesheet', 'href="/assets/planner.css' in page)
check('the Planner styles are not in the shared shell.css',
      '.planner-table' not in (ui / 'assets/shell.css').read_text())
check('on / off is the panel switch', "el.className = 'switch'" in (ui / 'assets/planner.js').read_text())
check('the Planner page loads planner.js and the shell',
      re.search(r'src="/assets/planner\.js', page) and re.search(r'src="/assets/shell\.js', page))
for body in ('waitBody', 'closeBody'):
    check(f'the page has the {body} table', f'id="{body}"' in page)

js = (ui / 'assets/planner.js').read_text()
check('planner.js goes through the core-api planner proxy',
      "'/api/planner/api/v1/dictionaries/'" in js)
check('planner.js edits both dictionaries', "'wait-reasons'" in js and "'close-outcomes'" in js)

# The fields the page sends are the ones the planner's models take (extra=forbid).
router = ast.parse((root / 'companions/planner/src/planner/routers/dictionaries.py').read_text())
fields = {}
for node in router.body:
    if isinstance(node, ast.ClassDef) and node.name in ('DictionaryRowIn', 'DictionaryRowPatch'):
        fields[node.name] = {s.target.id for s in node.body if isinstance(s, ast.AnnAssign)}
sent = set(re.findall(r"text\(tr, '(\w+)'", js)) | {'legacy_stage', 'enabled'}
patch_sent = sent - {'code'}
check('a saved row sends only DictionaryRowPatch fields', patch_sent <= fields['DictionaryRowPatch'],
      patch_sent - fields['DictionaryRowPatch'])
check('a new row sends only DictionaryRowIn fields', sent <= fields['DictionaryRowIn'],
      sent - fields['DictionaryRowIn'])

# The legacy stages offered are the planner's own, per dictionary.
statuses = ast.parse((root / 'companions/planner/src/planner/statuses.py').read_text())
consts = {}
for node in statuses.body:
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ('_WAIT_STAGES', '_CLOSE_STAGES'):
        consts[node.targets[0].id] = tuple(ast.literal_eval(node.value))
offered = dict(re.findall(r"key: '([\w-]+)'.*?stages: (\[[^\]]*\])", js, re.S))
check('wait reasons offer the planner wait stages',
      tuple(ast.literal_eval(offered['wait-reasons'])) == consts['_WAIT_STAGES'], offered.get('wait-reasons'))
check('close outcomes offer the planner close stages',
      tuple(ast.literal_eval(offered['close-outcomes'])) == consts['_CLOSE_STAGES'], offered.get('close-outcomes'))

wf = (ui / "workflows/index.html").read_text()
check("the Workflows page draws no step chain", "function board(" not in wf and "wf-arrow" not in wf)
wfjs = (ui / "assets/workflows.js").read_text()
check("each Workflows card links to the Studio diagram", "/workflows/" in wfjs and "wf-diagram" in wfjs)

studio = (root / 'companions/studio/public/next2/index.html').read_text()
check('the Studio Settings has no task-status card, and a node panel card shown to an admin only',
      'id="statusDictsLink"' not in studio and 'id="statusDicts"' not in studio and 'id="panelCard" hidden' in studio)
sys.exit(1 if fails else 0)
PY
