// Every panel page, read only, on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/panel-smoke.mjs <base> (--root <dir> | <page>...)
// --root lists the pages from a tree of index.html files (src/, or a copy of
// the panel container's html). A page passes when it opens with no script
// error, no /api call answering 4xx/5xx, no visible "Loading" left after a few
// seconds and no "Failed to load"/"Could not load" text. Every write the page
// tries is refused at the browser, so it is safe against a live node.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const base = process.argv[2];
let pages = process.argv.slice(3);
if (pages[0] === '--root') {
  const root = pages[1];
  const walk = (d) => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(d, e.name)) : e.name === 'index.html' ? [d] : []);
  pages = walk(root).map((d) => '/' + path.relative(root, d).split(path.sep).filter(Boolean).map((s) => s + '/').join('')).sort();
}
if (!base || !pages.length) { console.error('usage: panel-smoke.mjs <base> (--root <dir> | <page>...)'); process.exit(2); }

// Pages that show one thing need to be told which: the first of its kind.
const QUERY = {
  '/agents/edit/': async () => {
    const d = await (await fetch(base + '/api/agents')).json();
    const a = (d.agents || []).find((x) => x.enabled !== false) || (d.agents || [])[0];
    return a ? '?name=' + encodeURIComponent(a.slug) : '';
  },
};
const WAIT = 4000;

const port = 9346;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/smoke-prof', 'about:blank'], { stdio: 'ignore', detached: true });
const quit = (c) => { try { process.kill(-chrome.pid, 'SIGTERM'); } catch {} fs.rmSync('/tmp/smoke-prof', { recursive: true, force: true }); process.exit(c); };
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
let errors = [], bad = [], refused = [];
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; return; }
  if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === 'Network.responseReceived' && d.params.response.status >= 400) bad.push(d.params.response.status + ' ' + d.params.response.url.replace(base, ''));
  if (d.method === 'Fetch.requestPaused') {
    const q = d.params.request;
    if (['GET', 'HEAD', 'OPTIONS'].includes(q.method)) send('Fetch.continueRequest', { requestId: d.params.requestId });
    else { refused.push(q.method + ' ' + q.url.replace(base, '')); send('Fetch.failRequest', { requestId: d.params.requestId, errorReason: 'BlockedByClient' }); }
  }
};
await send('Runtime.enable');
await send('Network.enable');
await send('Fetch.enable', { patterns: [{ urlPattern: '*' }] });
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });

const PROBE = `(() => {
  const spin = [...document.querySelectorAll('.sn-spinner, .spinner, [aria-busy="true"]')].filter((s) => s.offsetParent)
    .map((s) => (s.closest('[id]') || {}).id || s.className);
  const m = (document.body ? document.body.innerText : '').match(/.*(Failed to load|Could not load|Internal Server Error).*/i);
  return { spin, msg: m ? m[0].slice(0, 160) : '' };
})()`;

let fails = 0;
for (const p of pages) {
  errors = []; bad = []; refused = [];
  const url = p + (QUERY[p] ? await QUERY[p]() : '');
  await send('Page.navigate', { url: base + url });
  await sleep(WAIT);
  const r = await send('Runtime.evaluate', { expression: PROBE, returnByValue: true });
  const v = r.result.result?.value || { spin: [], msg: 'the page could not be read' };
  const issues = [...new Set(errors)].map((e) => 'script error: ' + String(e).split('\n')[0].slice(0, 160))
    .concat([...new Set(bad)].map((b) => 'api: ' + b))
    .concat(v.spin.length ? ['still loading: ' + v.spin.join(', ')] : [])
    .concat(v.msg ? ['says: ' + v.msg] : [])
    .concat(refused.length ? ['tried to write (refused): ' + [...new Set(refused)].join(', ')] : []);
  console.log((issues.length ? 'FAIL ' : 'ok   ') + url + (issues.length ? '\n       ' + issues.join('\n       ') : ''));
  if (issues.length) fails++;
}
console.log(fails ? `${fails} of ${pages.length} pages failed` : `all ${pages.length} pages passed`);
quit(fails ? 1 : 0);
