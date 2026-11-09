// Browser check of the panel's Settings page on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/settings-check.mjs http://127.0.0.1:18097 [shot.png]
// Read only: it opens the page and a role's permissions, it writes nothing.
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
const shot = process.argv[3];
const port = 9344;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/stcheck-prof', 'about:blank'], { stdio: 'ignore', detached: true });
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
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/settings/' });
await sleep(3500);

check('Settings is active in the top bar', await js("!!document.querySelector('.topbar-nav a.nav-item.active[href=\"/settings/\"]')"));
check('it comes right after Workflows', await js("(() => { const a = [...document.querySelectorAll('.topbar-nav a')].map((x) => x.getAttribute('href')); return a.indexOf('/settings/') === a.indexOf('/workflows/') + 1; })()"));
check('no section failed or still loads', await js("![...document.querySelectorAll('.st-card')].some((c) => /Could not load/.test(c.textContent) || c.querySelector('.sn-spinner'))"));
const about = await js("fetch('/api/node/about').then((r) => r.json())");
const node = await js("document.getElementById('stNode').textContent");
check('the node card shows the release and domain', node.includes(about.release) && node.includes(about.domain), node);
check('and whether the agents run, with the way to Limits', /running|paused/.test(node) && await js("!!document.querySelector('#stNode a[href=\"/limits/\"]')"));
const pol = await js("fetch('/api/policies').then((r) => r.json()).then((d) => [...new Set(d.policies.map((p) => p.category))].length)");
check('a row per rule category', await js("document.querySelectorAll('#stRules tbody tr').length") === pol, pol);
check('the rules link to Policies', await js("!!document.querySelector('#stRules a[href=\"/policies/\"]')"));
const acc = await js("fetch('/api/node/access').then((r) => r.json())");
check('an account row per sign-in account', await js("document.querySelectorAll('#stAccess tbody tr').length") === acc.logins.length, acc.logins.length);
check('the owner account says super-admin', await js("[...document.querySelectorAll('#stAccess tbody tr')].some((r) => /super-admin/.test(r.textContent))"));
check('a block per role, super-admin first', await js("(() => { const r = [...document.querySelectorAll('.st-role__head b')].map((b) => b.textContent); return r.length === " + acc.roles.length + " && r[0] === 'super-admin'; })()"));
await js("document.querySelector('[data-perms=\"super-admin\"]').click()");
await sleep(300);
check('a role opens its permissions in the modal', await js("!document.querySelector('.modal-scrim').hidden && document.querySelectorAll('.modal-scrim .st-perms li').length") === acc.roles[0].permissions.length);
check('grouped by area, each group once', await js("(() => { const h = [...document.querySelectorAll('.modal-scrim .grants-group-head')].map((x) => x.firstChild.textContent.trim()); return h.length > 1 && new Set(h).size === h.length; })()"));
check('the filter is focused and narrows the list', await js("(() => { const f = document.getElementById('stPermsFilter'); if (document.activeElement !== f) return false; f.value = 'access.read'; f.dispatchEvent(new Event('input')); return [...document.querySelectorAll('.modal-scrim .st-perms li code')].map((c) => c.textContent).includes('access.read') && document.querySelectorAll('.modal-scrim .st-perms li').length < 10; })()"));
check('Escape closes it and gives focus back', await js("(() => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); return document.querySelector('.modal-scrim').hidden && document.activeElement.dataset.perms === 'super-admin'; })()"));
check('a click outside the panel closes it too', await js("(() => { document.querySelector('[data-perms=\"admin\"]').click(); const s = document.querySelector('.modal-scrim'); const open = !s.hidden; s.click(); return open && s.hidden; })()"));
const deps = await js("fetch('/api/departments').then((r) => r.json()).then((d) => d.departments.length)");
check('a row per department', await js("document.querySelectorAll('#stDeps tbody tr').length") === deps, deps);
check('heads are named, not numbered', await js("![...document.querySelectorAll('#stDeps tbody td:nth-child(2)')].some((t) => /^#\d+$/.test(t.textContent))"));
check('nothing wider than the page', await js('document.documentElement.scrollWidth <= innerWidth'));
check('the cards in a row are one height, People and access spans the row under them', await js("(() => { const c = [...document.querySelectorAll('.st-card')].map((x) => x.getBoundingClientRect()); const top = c.filter((r) => Math.abs(r.top - c[0].top) < 1); const w = document.querySelector('.st-card--wide').getBoundingClientRect(); return top.length === 3 && top.every((r) => Math.abs(r.height - top[0].height) < 1) && w.top > top[0].bottom && Math.abs(w.width - document.querySelector('.st-grid').getBoundingClientRect().width) < 1; })()"));
if (shot) fs.writeFileSync(shot, Buffer.from((await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, clip: { x: 0, y: 0, width: 1440, height: await js('document.documentElement.scrollHeight'), scale: 1 } })).result.data, 'base64'));

await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await sleep(500);
check('on a phone, every table fits its card', await js("[...document.querySelectorAll('.st-table')].every((t) => t.getBoundingClientRect().right <= t.closest('.st-card').getBoundingClientRect().right + 1)"));
check('on a phone, nothing wider than the page', await js('document.documentElement.scrollWidth <= innerWidth'), await js('document.documentElement.scrollWidth'));
if (shot) fs.writeFileSync(shot.replace(/\.png$/, '-phone.png'), Buffer.from((await send('Page.captureScreenshot', { format: 'png' })).result.data, 'base64'));

await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/memory/' });
await sleep(2000);
check('another page links to Settings too', await js("!!document.querySelector('.topbar-nav a[href=\"/settings/\"]')"));
check('no script errors', !errors.length, errors.join(' | '));
check('nothing written', !writes.length, writes.join(', '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
