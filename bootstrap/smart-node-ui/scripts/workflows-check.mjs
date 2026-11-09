// Browser check of the panel's Workflows page on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/workflows-check.mjs http://127.0.0.1:18097 [shot.png]
// It writes only to the flow named in TEST_FLOW (default content-hitl-demo, which
// no agent may start): off and on again, one agent added and taken off. It puts
// the flow back as it was; the agent's revoke row stays (scripts/README: delete it).
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
const shot = process.argv[3];
const port = 9343;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/wfcheck-prof', 'about:blank'], { stdio: 'ignore', detached: true });
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
const writes = [];
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; return; }
  if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === 'Network.requestWillBeSent' && /\/api\//.test(d.params.request.url) && d.params.request.method !== 'GET') writes.push(d.params.request.method + ' ' + d.params.request.url);
};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (e) => { const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error(e + ' ' + JSON.stringify(r.result.exceptionDetails).slice(0, 300)); return r.result.result.value; };
let fails = 0;
const check = (name, ok, extra) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (!ok && extra !== undefined ? ' -> ' + extra : '')); if (!ok) fails++; };
const cards = "[...document.querySelectorAll('#wfList .wf-card')]";
await send('Runtime.enable');
await send('Network.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/workflows/' });
await sleep(3000);

const api = await js("fetch('/api/workflows').then((r) => r.json()).then((d) => d.workflows)");
check('a card for every workflow', await js(`${cards}.length`) === api.length && api.length > 0, `${await js(`${cards}.length`)} of ${api.length}`);
check('no run form', await js("!document.querySelector('.wf-run, .wf-runbtn, input, button.wf-runbtn')"));
check('each card has its switch', await js(`${cards}.every((c) => c.querySelector('.switch[role=switch]'))`));
const first = api.find((w) => (w.granted_to || []).length && (w.steps || []).length);
check('the agents who may start it, as the API says', await js(`(() => { const c = ${cards}.find((c) => c.querySelector('.wf-meta').textContent.startsWith(${JSON.stringify(first.name)})); return [...c.querySelectorAll('.wf-grants .wf-x')].map((x) => x.dataset.slug).sort().join(); })()`) === [...first.granted_to].sort().join());
const none = api.filter((w) => !(w.granted_to || []).length).length;
check('a flow no agent may start says so', await js("document.querySelectorAll('.wf-none').length") === none, none);
check('steps are folded at first', await js(`${cards}.every((c) => !c.querySelector('details') || !c.querySelector('details').open)`));
await js(`(() => { const d = ${cards}.find((c) => c.querySelector('.wf-meta').textContent.startsWith(${JSON.stringify(first.name)})).querySelector('details'); d.open = true; d.scrollIntoView(); })()`);
await sleep(300);
const rows = await js(`[...${cards}.find((c) => c.querySelector('.wf-meta').textContent.startsWith(${JSON.stringify(first.name)})).querySelectorAll('.wf-steps tr')].map((r) => r.textContent)`);
check('every step is listed once', rows.length === first.steps.length, `${rows.length} of ${first.steps.length}`);
check('the run endings come last', /the run ends/.test(rows[rows.length - 1]) && !/the run ends/.test(rows[0]));
check('nothing wider than the page', await js('document.documentElement.scrollWidth <= innerWidth'));
const desc = "[...document.querySelectorAll('.wf-desc')].find((d) => d.scrollHeight > d.clientHeight + 4)";
check('a long description shows three lines', await js(`!!${desc}`));
check('and the rest on a click', await js(`(() => { const d = ${desc}; d.click(); return d.scrollHeight <= d.clientHeight + 4; })()`));
if (shot) fs.writeFileSync(shot, Buffer.from((await send('Page.captureScreenshot', { format: 'png' })).result.data, 'base64'));

// The controls, on the test flow only.
const T = process.env.TEST_FLOW || 'content-hitl-demo';
const tc = `document.querySelector('.wf-card[data-name="${T}"]')`;
const before = api.find((w) => w.name === T);
check('the test flow is on and no agent may start it', before && before.status === 'available' && !(before.granted_to || []).length, JSON.stringify(before && before.granted_to));
const now = () => js(`fetch('/api/workflows/${T}').then((r) => r.json())`);
await js(`${tc}.querySelector('.switch').click()`);
await sleep(1500);
check('the switch turns it off', (await now()).status === 'off' && await js(`${tc}.querySelector('.switch').getAttribute('aria-checked') === 'false' && /no new run starts/.test(${tc}.textContent)`));
await js(`${tc}.querySelector('.switch').click()`);
await sleep(1500);
check('and on again', (await now()).status === 'available' && await js(`${tc}.querySelector('.switch').classList.contains('on')`));
const pick = await js(`${tc}.querySelector('select.wf-add option:nth-child(2)').value`);
await js(`(() => { const s = ${tc}.querySelector('select.wf-add'); s.value = ${JSON.stringify(pick)}; s.dispatchEvent(new Event('change', { bubbles: true })); })()`);
await sleep(1500);
check('an agent added from the list may start it', ((await now()).granted_to || []).join() === pick && await js(`!!${tc}.querySelector('.wf-x[data-slug="${pick}"]')`), pick);
await js(`${tc}.querySelector('.wf-x[data-slug="${pick}"]').click()`);
await sleep(1500);
check('and taken off with its cross', !((await now()).granted_to || []).length && await js(`/no agent may start it/.test(${tc}.textContent)`));
check('the card says it saved', await js(`/Saved/.test(${tc}.querySelector('.wf-msg').textContent)`));
console.log('test agent: ' + pick);

await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await sleep(500);
check('on a phone, nothing wider than the page', await js('document.documentElement.scrollWidth <= innerWidth'), await js('document.documentElement.scrollWidth'));
if (shot) fs.writeFileSync(shot.replace(/\.png$/, '-phone.png'), Buffer.from((await send('Page.captureScreenshot', { format: 'png' })).result.data, 'base64'));
check('no script errors', !errors.length, errors.join(' | '));
check('writes only to the test flow', writes.length === 4 && writes.every((w) => w.includes('/api/workflows/' + T)), writes.join(', '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
