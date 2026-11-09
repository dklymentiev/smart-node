// The panel's writes on Agents, an agent's Limits and New rule, and Limits, in
// headless Chrome on the test stand (scripts/panel-stand.py):
// node --experimental-websocket scripts/write-check.mjs <base>
// It WRITES, on a test node only: it creates an agent uitest-<time> on New
// agent, stops and enables it on Agents, sets and clears one of its limits,
// adds a rule for it on its page, pauses and resumes the node on Limits, and
// deletes the agent on its page. Whatever is left after a failure is removed
// through the API at the end (the rule is archived; the node is resumed).
// The agent and the rule stay as archived rows, as the panel leaves them.
import { spawn } from 'node:child_process';
import fs from 'node:fs';

const base = process.argv[2];
if (!base) { console.error('usage: write-check.mjs <base>'); process.exit(2); }
const port = 9352;
const prof = '/tmp/write-check-prof';
const slug = 'uitest-' + Date.now().toString(36);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
try { await fetch(`http://127.0.0.1:${port}/json/version`); console.error('port taken'); process.exit(2); } catch {}
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
const errors = [], dialogs = [];
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; return; }
  if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === 'Page.javascriptDialogOpening') { dialogs.push(d.params.message); send('Page.handleJavaScriptDialog', { accept: true }); }
};
await send('Runtime.enable');
await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
const js = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result.exceptionDetails) throw new Error(expr.slice(0, 80) + ': ' + (r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text));
  return r.result.result.value;
};
const go = async (url, ms = 4000) => { await send('Page.navigate', { url: base + url }); await sleep(ms); };
const api = (path, method = 'GET') => js(`fetch(${JSON.stringify(path)}, { method: ${JSON.stringify(method)}, cache: 'no-store' }).then(async (r) => ({ status: r.status, j: await r.json().catch(() => null) }))`);
const until = async (expr, ms = 8000) => { for (let t = 0; t < ms; t += 250) { if (await js(expr).catch(() => false)) return true; await sleep(250); } return false; };
const set = (sel, v) => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); e.value = ${JSON.stringify(v)}; e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
const click = (sel) => js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return false; e.click(); return true; })()`);
const agent = async () => ((await api('/api/agents')).j?.agents || []).find((a) => a.slug === slug);

let fails = 0, ruleSlug = null;
const check = (name, ok, got) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (ok || got === undefined ? '' : '\n       got: ' + JSON.stringify(got))); if (!ok) fails++; return ok; };

async function main() {
  // ---- New agent ----
  await go('/agents/new/');
  await until(`!!document.querySelector('#anSlug')`);
  await set('#anTitle', 'UI test');
  await set('#anSlug', slug);
  await set('#anBody', '# UI test\n\nA throwaway agent for the panel write check. It does nothing.');
  await click('#anSubmit');
  const landed = await until(`location.pathname === '/agents/edit/' && location.search.includes(${JSON.stringify(slug)})`, 20000);
  check('New agent creates it and opens its page', landed && !!(await agent()), await js(`(document.getElementById('anError') || {}).textContent || location.href`));
  if (!(await agent())) return;

  // ---- Agents: Stop and Enable ----
  await go('/agents/');
  const stopSel = `[data-agent-stop="${slug}"]`, enSel = `[data-agent-enable="${slug}"]`;
  const first = await js(`document.querySelector('${stopSel}') ? 'stop' : document.querySelector('${enSel}') ? 'enable' : 'none'`);
  check('the new agent has a card with Stop or Enable', first !== 'none', first);
  if (first === 'stop') {
    await click(stopSel);
    const off = await until(`fetch('/api/agents').then((r) => r.json()).then((d) => d.agents.some((a) => a.slug === ${JSON.stringify(slug)} && a.enabled === false))`, 15000);
    check('Stop disables it (after a confirm)', off && dialogs.some((d) => d.startsWith('Stop ' + slug)), dialogs);
    await until(`!!document.querySelector('${enSel}')`, 10000);
  }
  check('its card then offers Enable', await until(`!!document.querySelector('${enSel}')`, 10000));
  await click(enSel);
  const on = await until(`fetch('/api/agents').then((r) => r.json()).then((d) => d.agents.some((a) => a.slug === ${JSON.stringify(slug)} && a.enabled !== false))`, 15000);
  check('Enable enables it again', on);

  // ---- The agent's Limits tab ----
  await go('/agents/edit/?name=' + encodeURIComponent(slug) + '#limits', 5000);
  check('the Limits tab lists the limits', await until(`document.querySelectorAll('#agentLimitsBody tr input[type=number]').length > 0`));
  const code = await js(`document.querySelector('#agentLimitsBody tr').cells[0].textContent`);
  const period = await js(`document.querySelector('#agentLimitsBody tr').cells[1].textContent`);
  await js(`(() => { const i = document.querySelector('#agentLimitsBody tr input[type=number]'); i.value = '0'; return true; })()`);
  await js(`document.querySelector('#agentLimitsBody tr button').click()`);
  await sleep(800);
  check('a limit of 0 is refused in words', /positive limit/.test(await js(`document.getElementById('agentLimitsStatus').textContent`)), await js(`document.getElementById('agentLimitsStatus').textContent`));
  await js(`(() => { const i = document.querySelector('#agentLimitsBody tr input[type=number]'); i.value = '1.5'; return true; })()`);
  await js(`document.querySelector('#agentLimitsBody tr button').click()`);
  const own = async () => ((await api('/api/limits/agents/' + slug)).j?.limits || []).find((l) => l.code === code && l.period === period)?.override;
  await until(`/override saved/.test(document.getElementById('agentLimitsStatus').textContent)`);
  check(`Save keeps its ${code}/${period} override`, Number((await own())?.value) === 1.5, await own());
  await until(`[...document.querySelectorAll('#agentLimitsBody tr')[0].querySelectorAll('button')].some((b) => b.textContent === 'Clear')`);
  await js(`[...document.querySelectorAll('#agentLimitsBody tr')[0].querySelectorAll('button')].find((b) => b.textContent === 'Clear').click()`);
  await sleep(2000);
  check('Clear removes the override', !(await own()), await own());

  // ---- New rule on its page ----
  await go('/agents/edit/?name=' + encodeURIComponent(slug), 5000);
  await click('#nr-open');
  await set('#nr-title', 'uitest rule ' + slug);
  await click('#nr-add');
  await sleep(800);
  check('a rule without its why is refused in words', await js(`getComputedStyle(document.getElementById('nr-note')).display !== 'none' && /why/i.test(document.getElementById('nr-note').textContent)`), await js(`document.getElementById('nr-note').textContent`));
  await set('#nr-why', '2026-10-16: the panel write check');
  await set('#nr-scope', 'agent');
  await click('#nr-add');
  await sleep(3000);
  const rule = ((await api('/api/policies')).j?.policies || []).find((p) => p.title === 'uitest rule ' + slug);
  ruleSlug = rule?.slug || null;
  check('Add creates the rule for this agent, with its why', !!rule && rule.rationale === '2026-10-16: the panel write check', rule);
  check('and the agent page lists it', await until(`document.body.innerText.includes(${JSON.stringify('uitest rule ' + slug)})`));
  if (ruleSlug) {
    const del = await api(`/api/policies/${encodeURIComponent(ruleSlug)}?scope=agent&agent_slug=${encodeURIComponent(slug)}`, 'DELETE');
    if (check('the rule is archived again', del.status === 204, del)) ruleSlug = null;
  }

  // ---- Limits: pause and resume the node ----
  await go('/limits/');
  await until(`/node/.test(document.getElementById('nodePause').textContent)`);
  const was = await js(`document.getElementById('nodePause').textContent`);
  check('the node is running before the check', was === 'Pause node', was);
  if (was === 'Pause node') {
    await click('#nodePause');
    await until(`document.getElementById('nodePause').textContent === 'Resume node'`);
    check('Pause node pauses it', (await api('/api/limits')).j?.node_paused === true);
    await click('#nodePause');
    await until(`document.getElementById('nodePause').textContent === 'Pause node'`);
    check('Resume node resumes it', (await api('/api/limits')).j?.node_paused === false);
  }

  // ---- Delete the agent on its page ----
  await go('/agents/edit/?name=' + encodeURIComponent(slug), 5000);
  await click('#aeDelete');
  const gone = await until(`location.pathname === '/agents/'`, 15000);
  check('Delete agent removes it (after a confirm) and goes back to Agents', gone && !(await agent()) && dialogs.some((d) => d.startsWith('Delete agent')), await js(`(document.getElementById('aeError') || {}).textContent || location.href`));
}

try { await main(); } catch (e) { check('the check ran through', false, String(e)); }
// Whatever a failure left.
try {
  if (ruleSlug) await api(`/api/policies/${encodeURIComponent(ruleSlug)}?scope=agent&agent_slug=${encodeURIComponent(slug)}`, 'DELETE');
  if ((await api('/api/limits')).j?.node_paused === true) await js(`fetch('/api/limits/pause', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paused: false }) })`);
  if (await agent()) await api('/api/agents/' + encodeURIComponent(slug), 'DELETE');
} catch (e) { console.log('cleanup: ' + e); }
check('no script errors', errors.length === 0, errors);
console.log(`agent ${slug}, rule ${ruleSlug || '-'}`);
console.log(fails ? `${fails} checks failed` : 'all checks passed');
quit(fails ? 1 : 0);
