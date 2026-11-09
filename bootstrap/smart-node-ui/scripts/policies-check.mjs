// Browser check of the panel's Policies page on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/policies-check.mjs http://127.0.0.1:18097
// It WRITES: one rule, uitest-<time>, created off, edited, then deleted (a soft
// archive). Run it on a test node, or on a live one only knowing that.
import { spawn } from 'node:child_process';

const base = process.argv[2];
const port = 9348;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/polcheck-prof', 'about:blank'], { stdio: 'ignore', detached: true });
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
await send('Runtime.enable');
await send('Network.enable');
await send('Page.enable');
await send('Page.navigate', { url: base + '/policies/' });
await sleep(3000);

const KEY = 'uitest-' + Date.now().toString(36);
const rows = () => js(`fetch('/api/policies', { cache: 'no-store' }).then((r) => r.json()).then((d) => d.policies.filter((p) => p.slug === '${KEY}'))`);
const fill = (o) => js(`(() => { const f = ${JSON.stringify(o)}; for (const k in f) { const e = document.getElementById(k); if (e.type === 'checkbox') e.checked = f[k]; else e.value = f[k]; } })()`);
const err = () => js("document.getElementById('polFormError').textContent");
const open = () => js("!document.getElementById('polModalScrim').hidden");
const rowText = () => js(`(() => { const r = [...document.querySelectorAll('#polBody tr')].find((t) => t.textContent.includes('${KEY}')); return r ? r.textContent : ''; })()`);
await js('window.confirm = () => true; true');

check('the page lists the rules', await js("document.querySelectorAll('#polBody tr').length") > 0);
await js("document.getElementById('polNewBtn').click()");
await sleep(200);
check('New policy opens the form with a Why field', await open() && await js("!!document.getElementById('polRationale')"));
await fill({ polKey: KEY, polTitle: 'UI test rule', polCategory: 'workflow', polSeverity: 'info', polBodyText: 'A rule the panel check writes and deletes.', polEnabled: false, polRationale: '' });
await js("document.getElementById('polForm').dispatchEvent(new Event('submit', { cancelable: true }))");
await sleep(300);
check('without a reason it is not sent, and the form says why', /why it exists/.test(await err()) && !writes.length, await err());
await fill({ polRationale: '2026-10-15: the panel check proves a rule can be written from the panel.' });
await js("document.getElementById('polSubmit').click()");
await sleep(1500);
let r = await rows();
check('with a reason it is created, off, with the reason kept', r.length === 1 && r[0].enabled === false && /panel check/.test(r[0].rationale), JSON.stringify(r) + ' ' + await err());
check('the form closes and the row shows, off', !await open() && /off/.test(await rowText()));

await js(`document.querySelector('#polBody [data-act=edit][data-key="${KEY}"]').click()`);
await sleep(300);
check('Edit fills the form, the reason too', await js("document.getElementById('polTitle').value") === 'UI test rule' && /panel check/.test(await js("document.getElementById('polRationale').value")));
await fill({ polTitle: 'UI test rule, edited' });
await js("document.getElementById('polSubmit').click()");
await sleep(1500);
r = await rows();
check('Save changes the rule (PATCH), still off', r.length === 1 && r[0].title === 'UI test rule, edited' && r[0].enabled === false, JSON.stringify(r) + ' ' + await err());
check('the row shows the new title', /edited/.test(await rowText()));

await js("document.getElementById('polNewBtn').click()");
await sleep(200);
await fill({ polKey: KEY, polTitle: 'duplicate', polBodyText: 'x', polEnabled: false, polRationale: 'a duplicate on purpose' });
await js("document.getElementById('polSubmit').click()");
await sleep(1200);
check('a refused write says why in words', /already exists/.test(await err()) && !/object Object/.test(await err()), await err());
await js("document.getElementById('polModalCancel').click()");

await js(`document.querySelector('#polBody [data-act=delete][data-key="${KEY}"]').click()`);
await sleep(1500);
check('Delete removes it from the list', (await rows()).length === 0 && !(await rowText()));
check('no script errors', !errors.length, errors.join(' | '));
const other = writes.filter((w) => !w.includes('/api/policies'));
check('it wrote only to this test rule', !other.length && writes.every((w) => /\/api\/policies$/.test(w) || w.includes(KEY)), writes.join(', '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
