#!/usr/bin/env bash
# The panel's Workflows page: no run form; it reads /api/workflows and the
# agents, writes only the owner's two controls (on/off, who may start it), and
# lists each flow's steps in the order they run, with where each outcome leads.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UI="$ROOT/bootstrap/smart-node-ui/src"
fails=0
check() { if [ "$2" = 0 ]; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }

page="$UI/workflows/index.html"
js="$UI/assets/workflows.js"
grep -q 'src="/assets/workflows.js"' "$page"; check "the page loads assets/workflows.js" $?
! grep -qE 'wf-runbtn|Run a test inquiry|inquiry' "$page" "$js"; check "no test inquiry form" $?
! grep -q "/run'" "$js"; check "the page starts no run" $?
[ "$(grep -o "fetch('[^']*'" "$js" | sort -u | paste -sd ' ' -)" = "fetch('/api/agents' fetch('/api/workflows' fetch('/api/workflows/'" ]; check "it reads the workflows and the agents, and writes under /api/workflows/<name>" $?
[ "$(grep -oE "send\(name, '(PATCH|PUT)', '[^']*'" "$js" | sort -u | paste -sd ' ' -)" = "send(name, 'PATCH', '' send(name, 'PUT', '/agents'" ]; check "the writes are PATCH <name> and PUT <name>/agents" $?
wf="$ROOT/companions/core-api/routes/workflows.py"
grep -q '@router.get("", ' "$wf"; check "core-api serves GET /api/workflows" $?
grep -q '@router.patch("/{name}"' "$wf" && grep -q '@router.put("/{name}/agents"' "$wf"; check "core-api serves both writes" $?
[ "$(grep -c 'Depends(requires("workflows.write"))' "$wf")" = 2 ]; check "both need workflows.write" $?
b02="$ROOT/bootstrap/B02-sn-core-schema.sql"
[ "$(grep -oE "\('[a-z-]+', 'workflows\.write'\)" "$b02")" = "('super-admin', 'workflows.write')" ]; check "only super-admin holds workflows.write" $?

node - "$js" <<'JS' || fails=$((fails + 1))
const w = require(process.argv[2]);
let bad = 0;
const check = (name, ok, extra) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : ' -> ' + JSON.stringify(extra))); if (!ok) bad++; };
// Stored out of order, as the catalog keeps them.
const steps = [
  { name: 'done', routing: { _default: '_stop' } },
  { name: 'review', depends_on: ['draft'], routing: { fail: 'cancelled', revise: 'draft', waiting: 'review', skipped: '_stop', _default: 'send' } },
  { name: 'cancelled', routing: { _default: '_stop' } },
  { name: 'draft', depends_on: [], routing: { waiting: 'draft', _default: 'review' } },
  { name: 'send', depends_on: ['review'], routing: { approve: 'done', cancel: 'cancelled', expired: 'send', _default: 'cancelled' } },
  { name: 'orphan', depends_on: ['x'], routing: { _default: 'done' } },
];
const order = w.ordered(steps).map(s => s.name);
check('steps read in the order they run, endings last', JSON.stringify(order) === JSON.stringify(['draft', 'review', 'send', 'orphan', 'done', 'cancelled']), order);
const r = w.routes(steps[1]);
check('waiting on itself and skipped are left out', !r.some(x => /waiting|skipped/.test(x.when)), r);
check('outcomes listed with where they lead, otherwise last',
  JSON.stringify(r) === JSON.stringify([{ when: 'fail', to: 'cancelled' }, { when: 'revise', to: 'draft' }, { when: 'otherwise', to: 'send' }]), r);
const s = w.routes(steps[4]);
check('outcomes that lead to the same step are one entry; expired asking again is left out',
  JSON.stringify(s) === JSON.stringify([{ when: 'approve', to: 'done' }, { when: 'cancel, otherwise', to: 'cancelled' }]), s);
check('a step with one way on has no "otherwise"', JSON.stringify(w.routes(steps[3])) === JSON.stringify([{ when: '', to: 'review' }]));
check('an ending is a step that only stops', w.ending(steps[0]) && !w.ending(steps[3]));
const html = w.card({ name: 'demo', title: 'Demo <b>', status: 'available', granted_to: [], steps }, 'https://studio.example');
check('a card escapes its text', html.includes('Demo &lt;b&gt;') && !html.includes('Demo <b>'));
check('a card says when no agent may start it', html.includes('no agent may start it yet'));
const ag = [{ slug: 'dev', title: 'Dev' }, { slug: 'ops', title: 'Ops' }, { slug: 'qa', title: 'QA' }];
const two = w.card({ name: 'f', status: 'available', granted_to: ['ops', 'dev'], steps: [] }, '', ag);
check('a granted agent is a chip with its name and a way to take it off', two.includes('>Ops<button') && two.includes('data-act="drop" data-slug="ops"'));
check('the add list offers only the agents not granted', two.includes('<option value="qa">QA</option>') && !two.includes('<option value="dev">'));
check('on: the switch is on and says so', two.includes('class="switch on" role="switch" aria-checked="true"') && !two.includes('wf-offnote'));
const off = w.card({ name: 'f', status: 'off', granted_to: [], steps: [] }, '', ag);
check('off: the switch is off and the card says what that means', off.includes('class="switch" role="switch" aria-checked="false"') && off.includes('no new run starts'));
check('a card lists its steps and links to the Studio', html.includes('6 steps') && html.includes('https://studio.example/workflows/demo'));
check('a card without a Studio has no link', !w.card({ name: 'x', steps: [] }, '').includes('wf-diagram'));
process.exit(bad ? 1 : 0);
JS

exit $(( fails ? 1 : 0 ))
