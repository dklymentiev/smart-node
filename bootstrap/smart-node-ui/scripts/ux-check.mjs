// The panel's error text and the way from an agent card to its tools, read
// only, on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/ux-check.mjs <base>
// 1. snErr says what a core-api answer means: the fields of a 422, the text of
//    a detail, what a bare status means.
// 2. A list that fails to load says so in words (a 502 with a proxy's HTML
//    page is made up at the browser for /api/runtimes/live).
// 3. Every agent card links to its page's Tools tab, which opens on it; the
//    old grants modal is gone.
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
if (!base) { console.error('usage: ux-check.mjs <base>'); process.exit(2); }
const port = 9350;
const prof = '/tmp/ux-check-prof';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', `--user-data-dir=${prof}`, 'about:blank'], { stdio: 'ignore', detached: true });
const quit = (c) => { try { process.kill(-chrome.pid, 'SIGTERM'); } catch {} fs.rmSync(prof, { recursive: true, force: true }); process.exit(c); };
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
let fake502 = null;
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; return; }
  if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === 'Fetch.requestPaused') {
    const q = d.params.request;
    if (fake502 && q.url.includes(fake502)) {
      send('Fetch.fulfillRequest', { requestId: d.params.requestId, responseCode: 502,
        responseHeaders: [{ name: 'Content-Type', value: 'text/html' }],
        body: Buffer.from('<html><body>502 Bad Gateway</body></html>').toString('base64') });
    } else if (['GET', 'HEAD', 'OPTIONS'].includes(q.method)) send('Fetch.continueRequest', { requestId: d.params.requestId });
    else send('Fetch.failRequest', { requestId: d.params.requestId, errorReason: 'BlockedByClient' });
  }
};
await send('Runtime.enable');
await send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
const evalv = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.result?.value;
const go = async (url, ms = 3500) => { await send('Page.navigate', { url: base + url }); await sleep(ms); };

let fails = 0;
const check = (name, ok, got) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok ? '' : '\n       got: ' + JSON.stringify(got))); if (!ok) fails++; };

await go('/agents/');
const said = await evalv(`({
  fields: snErr(422, { detail: [{ loc: ['body', 'rationale'], msg: 'Field required' }, { loc: ['body', 'title'], msg: 'too short' }] }),
  text: snErr(409, { detail: 'slug taken' }),
  obj: snErr(400, { detail: { message: 'bad ref' } }),
  bare500: snErr(502, {}),
  bare403: snErr(403),
  none: snErr(0)
})`);
check('a 422 names its fields', said?.fields === 'rationale: Field required; title: too short', said);
check('a detail is shown as it is', said?.text === 'slug taken' && said?.obj === 'bad ref', said);
check('a bare 5xx says the node failed and where its log is', /HTTP 502\).*Systems/.test(said?.bare500 || ''), said);
check('a bare 403 and no answer say what they mean', /not allowed/.test(said?.bare403 || '') && /did not answer/.test(said?.none || ''), said);

const cards = await evalv(`[...document.querySelectorAll('.agent-card')].map((c) => ({
  id: (c.querySelector('.agent-card-id') || {}).textContent,
  href: (c.querySelector('a[href*="#tools"]') || {}).getAttribute?.('href') || null,
  label: (c.querySelector('a[href*="#tools"]') || {}).textContent || null
}))`);
check('there are agent cards', Array.isArray(cards) && cards.length > 0, cards);
check('every card links to its Tools tab', cards.length && cards.every((c) => c.href === '/agents/edit/?name=' + encodeURIComponent(c.id.trim()) + '#tools' && c.label === 'Tools and permissions'), cards);
check('the old grants modal is gone', await evalv(`!document.getElementById('agentGrantsScrim')`), null);

const first = cards[0];
await go(first.href, 4500);
const tab = await evalv(`({
  active: (document.querySelector('#aeTabs .ae-tab.is-active') || {}).dataset?.tab,
  shown: [...document.querySelectorAll('[data-tab-panel]')].filter((p) => !p.hidden).map((p) => p.dataset.tabPanel)
})`);
check('the link opens the agent on its Tools tab', tab?.active === 'tools' && tab.shown.includes('tools') && !tab.shown.includes('instructions'), tab);
await go('/agents/edit/?name=' + encodeURIComponent(first.id.trim()), 4500);
check('without the link it still opens on Instructions', (await evalv(`(document.querySelector('#aeTabs .ae-tab.is-active') || {}).dataset?.tab`)) === 'instructions', null);
await go('/agents/edit/?name=' + encodeURIComponent(first.id.trim()) + '#nonsense"]', 4500);
check('an unknown tab in the address is ignored', (await evalv(`(document.querySelector('#aeTabs .ae-tab.is-active') || {}).dataset?.tab`)) === 'instructions', null);

fake502 = '/api/runtimes/live';
await go('/runtimes/');
const rt = await evalv(`(document.getElementById('runtimesBody') || {}).innerText || ''`);
fake502 = null;
check('a list that fails says why in words', /Failed to load: The node failed to do this \(HTTP 502\)/.test(rt), rt);

check('no script errors', errors.length === 0, errors);
console.log(fails ? `${fails} checks failed` : 'all checks passed');
quit(fails ? 1 : 0);
