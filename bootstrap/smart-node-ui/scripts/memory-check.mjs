// Browser check of the panel's Memory page on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/memory-check.mjs http://127.0.0.1:18097
// Read only: it searches and opens documents, it writes nothing.
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
const port = 9342;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/memorycheck-prof', 'about:blank'], { stdio: 'ignore', detached: true });
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
  if (d.method === 'Network.requestWillBeSent' && /\/api\//.test(d.params.request.url)
      && !['GET'].includes(d.params.request.method) && !/\/api\/memory\/search$/.test(d.params.request.url)) writes.push(d.params.request.method + ' ' + d.params.request.url);
};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (e) => { const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true }); if (r.result.exceptionDetails) throw new Error(e + ' ' + JSON.stringify(r.result.exceptionDetails).slice(0, 300)); return r.result.result.value; };
let fails = 0;
const check = (name, ok, extra) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (!ok && extra !== undefined ? ' -> ' + extra : '')); if (!ok) fails++; };
const rows = "[...document.querySelectorAll('#memList .mem-row')]";
await send('Runtime.enable');
await send('Network.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/memory/' });
await sleep(3500);

check('Memory is active in the top bar', await js("!!document.querySelector('.topbar-nav a.nav-item.active[href=\"/memory/\"]')"));
check('the top bar has no empty items', await js("!/soon/i.test(document.querySelector('.topbar-nav').textContent) && !/soon/i.test((document.querySelector('.subtopbar') || {}).textContent || '')"));
check('workspace chips, All first and on', await js("(() => { const c = [...document.querySelectorAll('#memSpaces .mem-chip')]; return c.length > 3 && c[0].dataset.ws === 'all' && c[0].classList.contains('is-on'); })()"));
check('the newest documents are listed', await js(`${rows}.length`) > 5, await js(`${rows}.length`));
check('each row says whose it is', await js(`${rows}.every((r) => r.querySelector('.mem-who') && r.querySelector('.mem-who').textContent.trim())`));
check('the list is newest first', await js(`(() => { const d = ${rows}.map((r) => r.querySelector('.mem-meta span:nth-last-child(1)').textContent); return d.every((x, i) => i === 0 || d[i - 1] >= x); })()`));
check('the types are offered', await js("document.querySelectorAll('#memType option').length > 2"));
check('nothing wider than the page', await js('document.documentElement.scrollWidth <= innerWidth'));

// One agent's memory, as the agent page opens it.
const top = await js("document.querySelectorAll('#memSpaces .mem-chip')[1].dataset.ws");
const topName = await js("document.querySelectorAll('#memSpaces .mem-chip')[1].firstChild.textContent.trim()");
await js("document.querySelectorAll('#memSpaces .mem-chip')[1].click()");
await sleep(1500);
check('a chip shows only that workspace', await js(`${rows}.length > 0 && ${rows}.every((r) => r.dataset.ws === ${JSON.stringify(top)})`));
check('and says so in the address', await js(`location.search === '?ws=' + ${JSON.stringify(top)}`), await js('location.search'));
check('rows name the agent, not the workspace id', await js(`${rows}.every((r) => r.querySelector('.mem-who').textContent === ${JSON.stringify(topName)})`));

// Search by meaning, across all.
await js("document.querySelector('#memSpaces .mem-chip[data-ws=all]').click()");
await sleep(800);
await js("(() => { const q = document.getElementById('memQuery'); q.value = 'tetris build in the screenbox'; q.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' })); })()");
await sleep(2500);
check('search finds documents and shows how close', await js(`${rows}.length > 0 && ${rows}.every((r) => /% match/.test(r.textContent))`), await js(`${rows}.length`));

// Open one.
await js(`${rows}[0].click()`);
await sleep(1500);
check('a row opens the whole document beside the list', await js("!document.getElementById('memDoc').hidden && document.getElementById('memDocBody').textContent.length > 50 && document.body.classList.contains('mem-doc-on')"));
check('with its tags', await js("document.querySelectorAll('#memDocTags .chip').length > 0"));
await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
await sleep(200);
check('Esc closes it', await js("document.getElementById('memDoc').hidden"));

// A type.
await js("(() => { const q = document.getElementById('memQuery'); q.value = ''; q.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' })); })()");
await sleep(1500);
await js("(() => { const s = document.getElementById('memType'); s.value = 'handoff'; s.dispatchEvent(new Event('change')); })()");
await sleep(2000);
check('a type keeps only that type', await js(`${rows}.length > 0 && ${rows}.every((r) => r.querySelector('.mem-type').textContent === 'handoff')`));
check('handoffs are named by their subject, not all "Session handoff"', await js(`new Set(${rows}.map((r) => r.querySelector('.mem-title').textContent)).size > 3`));

// A link straight to one agent's memory.
await send('Page.navigate', { url: base + '/memory/?ws=' + encodeURIComponent(top) });
await sleep(3000);
check('/memory/?ws= opens with that chip on', await js(`document.querySelector('#memSpaces .mem-chip.is-on').dataset.ws === ${JSON.stringify(top)}`));

const r = await send('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync('/tmp/memory-page.png', Buffer.from(r.result.data, 'base64'));
check('nothing was written', writes.length === 0, writes.join(', '));
check('no page errors', errors.length === 0, errors.join(' | '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
