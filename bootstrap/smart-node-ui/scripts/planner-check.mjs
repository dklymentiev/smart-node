// Browser check of the panel's Planner page on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/planner-check.mjs http://127.0.0.1:18097
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
const port = 9340;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/plannercheck-prof', 'about:blank'], { stdio: 'ignore', detached: true });
const quit = (c) => { try { process.kill(-chrome.pid, 'SIGTERM'); } catch {} process.exit(c); };
process.on('uncaughtException', (e) => { console.error(e); quit(3); });
let tabs;
for (let i = 0; i < 60; i++) {
  try { tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (tabs.length) break; } catch {}
  await sleep(250);
}
const ws = new WebSocket(tabs.find((t) => t.type === 'page').webSocketDebuggerUrl);
await new Promise((r) => { ws.onopen = r; });
let id = 0;
const pend = {};
const errors = [];
const requests = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; return; }
  if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === 'Network.requestWillBeSent' && /\/api\//.test(d.params.request.url)) requests.push(d.params.request.method + ' ' + d.params.request.url.replace(base, ''));
};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (e) => { const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error(e + ' ' + JSON.stringify(r.result.exceptionDetails).slice(0, 300)); return r.result.result.value; };
let fails = 0;
const check = (name, ok, extra) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (!ok && extra !== undefined ? ' -> ' + extra : '')); if (!ok) fails++; };
await send('Runtime.enable');
await send('Network.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/planner/' });
await sleep(3000);

check('Planner is active in the top bar', await js("!!document.querySelector('.topbar-nav a.nav-item.active[href=\"/planner/\"]')"));
check('no page says Planner soon', await js("!/Planner\\s*soon/.test(document.querySelector('.topbar-nav').textContent)"));
const nWait = await js("document.querySelectorAll('#waitBody tr[data-code]').length");
const nClose = await js("document.querySelectorAll('#closeBody tr[data-code]').length");
check('wait reasons are listed', nWait >= 5, nWait);
check('close outcomes are listed', nClose >= 2, nClose);
check('each table ends with an Add row', await js("!!document.querySelector('#waitBody tr.pl-new') && !!document.querySelector('#closeBody tr.pl-new')"));
check('the tables stay inside the page', await js("document.documentElement.scrollWidth <= innerWidth"));
check('the default mark shows', await js("document.querySelectorAll('.pl-default').length > 0"));

// Save one row and put it back: the planner holds the change, then the old one.
const code = await js("[...document.querySelectorAll('#waitBody tr[data-code]')].pop().dataset.code");
const old = await js(`document.querySelector('#waitBody tr[data-code="${code}"] input[data-f=label]').value`);
const trSel = `#waitBody tr[data-code="${code}"]`;
await js(`(() => { const i = document.querySelector('${trSel} input[data-f=label]'); i.value = ${JSON.stringify(old)} + ' (check)'; })()`);
await js(`document.querySelector('${trSel} button.btn-row').click()`);
await sleep(1200);
check('Save says saved', await js("document.getElementById('plannerStatus').textContent") === `Saved ${code}.`, await js("document.getElementById('plannerStatus').textContent"));
const stored = await js(`fetch('/api/planner/api/v1/dictionaries/wait-reasons?include_disabled=true').then((r) => r.json()).then((rows) => rows.find((x) => x.code === ${JSON.stringify(code)}).label)`);
check('the planner holds the new label', stored === old + ' (check)', stored);
await js(`(() => { const i = document.querySelector('${trSel} input[data-f=label]'); i.value = ${JSON.stringify(old)}; })()`);
await js(`document.querySelector('${trSel} button.btn-row').click()`);
await sleep(1200);
const back = await js(`fetch('/api/planner/api/v1/dictionaries/wait-reasons?include_disabled=true').then((r) => r.json()).then((rows) => rows.find((x) => x.code === ${JSON.stringify(code)}).label)`);
check('and the old label back', back === old, back);

// On / off is a switch that applies at once; it goes off and on again here.
const enabledOf = () => js(`fetch('/api/planner/api/v1/dictionaries/wait-reasons?include_disabled=true').then((r) => r.json()).then((rows) => rows.find((x) => x.code === ${JSON.stringify(code)}).enabled)`);
check('on / off is the panel switch, not a checkbox', await js(`!!document.querySelector('${trSel} button.switch[role=switch]') && !document.querySelector('.planner-table input[type=checkbox]')`));
check('the switch shows the row on', await js(`document.querySelector('${trSel} .switch').classList.contains('on')`));
await js(`document.querySelector('${trSel} .switch').click()`);
await sleep(1200);
check('a click switches it off at once', (await enabledOf()) === false && !(await js(`document.querySelector('${trSel} .switch').classList.contains('on')`)));
await js(`document.querySelector('${trSel} .switch').click()`);
await sleep(1200);
check('and on again', (await enabledOf()) === true && await js(`document.querySelector('${trSel} .switch').getAttribute('aria-checked') === 'true'`));

// Bad effects never leave the page.
const before = requests.length;
await js(`(() => { const i = document.querySelector('${trSel} input[data-f=effects]'); i.dataset.was = i.value; i.value = '[1,2]'; })()`);
await js(`document.querySelector('${trSel} button.btn-row').click()`);
await sleep(500);
check('effects that are not an object are refused here', /Effects must be a JSON object/.test(await js("document.getElementById('plannerStatus').textContent")) && await js("document.getElementById('plannerStatus').classList.contains('is-bad')"));
check('and nothing was sent', !requests.slice(before).some((r) => r.startsWith('PATCH')), requests.slice(before).join(', '));
await js(`(() => { const i = document.querySelector('${trSel} input[data-f=effects]'); i.value = i.dataset.was; })()`);
// An Add with no label is refused before it is sent.
const before2 = requests.length;
await js("(() => { const tr = document.querySelector('#waitBody tr.pl-new'); tr.querySelector('input[data-f=code]').value = 'zz_check'; tr.querySelector('button').click(); })()");
await sleep(400);
check('Add needs a code and a label', /required/.test(await js("document.getElementById('plannerStatus').textContent")) && !requests.slice(before2).some((r) => r.startsWith('POST')));

const r = await send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync('/tmp/planner-page.png', Buffer.from(r.result.data, 'base64'));
check('no page errors', errors.length === 0, errors.join(' | '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
