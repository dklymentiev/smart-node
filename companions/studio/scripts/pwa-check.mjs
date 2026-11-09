// Browser check of the Studio as an installable app, in headless Chrome over
// the DevTools protocol. Not part of npm test: it needs a running Studio (or
// public/ served as it is) on a secure origin, http://127.0.0.1 counts, and
// google-chrome.
//   node --experimental-websocket scripts/pwa-check.mjs http://127.0.0.1:8050
// 1. Chrome reads the manifest without errors and finds the Studio installable.
// 2. The service worker takes over the page and keeps its offline page only.
// 3. With the network gone a page load shows that page; /api is never kept.
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
if (!base) { console.error('usage: pwa-check.mjs <base>'); process.exit(2); }
const port = 9353;
const prof = '/tmp/pwa-check-prof';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error(`port ${port} is taken: stop that Chrome first`); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', `--user-data-dir=${prof}`, 'about:blank'], { stdio: 'ignore', detached: true });
const quit = (c) => { try { process.kill(-chrome.pid, 'SIGTERM'); } catch {} try { fs.rmSync(prof, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); } catch {} process.exit(c); };
let tabs;
for (let i = 0; i < 60; i++) {
  try { tabs = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (tabs.length) break; } catch {}
  await sleep(250);
}
const ws = new WebSocket(tabs.find((t) => t.type === 'page').webSocketDebuggerUrl);
await new Promise((r) => { ws.onopen = r; });
let id = 0;
const pend = {};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; } };
await send('Page.enable');
await send('Network.enable');
const js = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.result?.value;
const go = async (url, ms = 4000) => { await send('Page.navigate', { url: base + url }); await sleep(ms); };
const until = async (expr, ms = 10000) => { for (let t = 0; t < ms; t += 250) { if (await js(expr).catch(() => false)) return true; await sleep(250); } return false; };

let fails = 0;
const check = (name, ok, got) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok || got === undefined ? '' : '\n       got: ' + JSON.stringify(got))); if (!ok) fails++; };

await go('/');
const man = (await send('Page.getAppManifest')).result || {};
check('the manifest loads without errors', !!man.data && (man.errors || []).length === 0, { url: man.url, errors: man.errors });
const parsed = man.data ? JSON.parse(man.data) : {};
check('it names the app and opens it standalone', parsed.short_name === 'Smart Node' && parsed.display === 'standalone', parsed);
const type = await js(`fetch('/manifest.webmanifest').then((r) => r.headers.get('content-type'))`);
check('the manifest is served as a manifest', /application\/manifest\+json/.test(type || ''), type);

const ready = await until(`navigator.serviceWorker.getRegistration().then((r) => !!(r && r.active))`, 15000);
await go('/');
check('the service worker takes over the page', ready && await js(`navigator.serviceWorker.controller !== null`));
const inst = (await send('Page.getInstallabilityErrors')).result || {};
check('Chrome finds the Studio installable', (inst.installabilityErrors || []).length === 0, inst.installabilityErrors);

// Something /api answers, while the worker is in charge.
await js(`fetch('/api/version').catch(() => null)`);
const kept = await js(`caches.keys().then((ks) => Promise.all(ks.map((k) => caches.open(k).then((c) => c.keys()).then((rs) => rs.map((r) => new URL(r.url).pathname))))).then((a) => a.flat())`);
check('the worker keeps the offline page and nothing else', JSON.stringify(kept) === JSON.stringify(['/offline.html']), kept);

// The worker fetches on its own, so the network goes for it as well as for the page.
const net = (offline) => ({ offline, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
const swt = ((await send('Target.getTargets')).result.targetInfos || []).find((t) => t.type === 'service_worker');
const sw = swt && (await send('Target.attachToTarget', { targetId: swt.targetId, flatten: true })).result?.sessionId;
const swSend = (method, params) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, sessionId: sw, method, params })); });
check('the worker is there to cut off', !!sw, swt);
if (sw) { await swSend('Network.enable', {}); await swSend('Network.emulateNetworkConditions', net(true)); }
await send('Network.emulateNetworkConditions', net(true));
await go('/tasks', 3000);
const off = await js(`document.title + ' | ' + ((document.querySelector('h1') || {}).textContent || '')`);
check('with no network a page load shows the offline page', /The node cannot be reached/.test(off || ''), off);
if (sw) await swSend('Network.emulateNetworkConditions', net(false));
await send('Network.emulateNetworkConditions', net(false));
await go('/tasks', 3000);
check('and the Studio again once the network is back', await js(`!!document.querySelector('.sb__brand')`), await js('document.title'));

console.log(fails ? `${fails} checks failed` : 'all checks passed');
quit(fails ? 1 : 0);
