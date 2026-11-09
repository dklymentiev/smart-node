// Browser check of the panel's Secrets page on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/secrets-check.mjs http://127.0.0.1:18097
// It WRITES: one secret, uitest/check-<time>, stored, replaced, deleted (its
// last value stays as a 30-day snapshot). Run it on a test node.
import { spawn } from 'node:child_process';

const base = process.argv[2];
const port = 9349;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/seccheck-prof', 'about:blank'], { stdio: 'ignore', detached: true });
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
await send('Page.navigate', { url: base + '/secrets/' });
await sleep(3000);

const REF = 'uitest/check-' + Date.now().toString(36);
const VALUE = 'uitest-value-' + Math.random().toString(36).slice(2);
const items = () => js(`fetch('/api/secrets/refs', { cache: 'no-store' }).then((r) => r.json()).then((d) => d.items.filter((i) => i.ref === '${REF}'))`);
const fill = (o) => js(`(() => { const f = ${JSON.stringify(o)}; for (const k in f) document.getElementById(k).value = f[k]; })()`);
const err = () => js("document.getElementById('secFormError').textContent");
const open = () => js("!document.getElementById('secModalScrim').hidden");
const row = () => js(`!!document.querySelector('#secBody [data-act=delete][data-ref="${REF}"]')`);
const shown = () => js(`document.documentElement.outerHTML.includes('${VALUE}')`);
await js('window.confirm = () => true; true');

check('the page lists the secrets', await js("document.querySelectorAll('#secBody tr').length") > 0);
await js("document.getElementById('secNewBtn').click()");
await sleep(200);
check('New secret opens the form', await open());
await fill({ secRef: REF, secValue: '' });
await js("document.getElementById('secForm').dispatchEvent(new Event('submit', { cancelable: true }))");
await sleep(300);
check('without a value nothing is sent, and the form says so', /required/.test(await err()) && !writes.length, await err());
await fill({ secRef: 'bad ref with spaces', secValue: 'x' });
await js("document.getElementById('secForm').dispatchEvent(new Event('submit', { cancelable: true }))");
await sleep(1000);
check('a path core-api refuses says why in words', /The path may hold letters, digits/.test(await err()), await err());
await fill({ secRef: REF, secValue: VALUE });
await js("document.getElementById('secForm').dispatchEvent(new Event('submit', { cancelable: true }))");
await sleep(1500);
let it = await items();
check('Save stores it, and the form closes', it.length === 1 && !await open(), JSON.stringify(it) + ' ' + await err());
check('the row shows, the value never does', await row() && !await shown());
const before = it[0] && it[0].updated_at;

await sleep(1100);
await js(`document.querySelector('#secBody [data-act=replace][data-ref="${REF}"]').click()`);
await sleep(300);
check('Replace opens the form on this path, the path locked, the value empty', await open()
  && await js("document.getElementById('secRef').readOnly && document.getElementById('secRef').value") === REF
  && await js("document.getElementById('secValue').value") === '');
await fill({ secValue: VALUE + '-2' });
await js("document.getElementById('secForm').dispatchEvent(new Event('submit', { cancelable: true }))");
await sleep(1500);
it = await items();
check('Save replaces it', it.length === 1 && it[0].updated_at !== before && !await open(), JSON.stringify(it) + ' ' + await err());
check('neither value is ever on the page', !await shown());

await js(`document.querySelector('#secBody [data-act=delete][data-ref="${REF}"]').click()`);
await sleep(1500);
check('Delete removes it', (await items()).length === 0 && !await row());
check('no script errors', !errors.length, errors.join(' | '));
check('it wrote only to this test secret', writes.every((w) => /\/api\/secrets$/.test(w) || w.includes(REF)), writes.join(', '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
