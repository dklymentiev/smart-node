// Browser check of the Studio chat layouts (chat-shell.js on a desktop,
// phone-chats.js on a phone) against a running Studio, in headless Chrome over
// the DevTools protocol. Not part of npm test: it needs a live Studio, a chat
// and google-chrome. The Studio must trust Remote-User from this host.
//   node --experimental-websocket scripts/browser-check.mjs http://127.0.0.1:8050 <desk-id>
import { spawn } from 'node:child_process';

const base = process.argv[2];
const desk = process.argv[3];
const port = 9334;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// A Chrome left on the port from an earlier run would answer instead of ours,
// without this run's flags.
try {
  await fetch(`http://127.0.0.1:${port}/json/version`);
  console.error(`port ${port} is taken (a Chrome from an earlier run?): stop it first`);
  process.exit(2);
} catch {}
const chrome = spawn('google-chrome', ['--headless=new', `--remote-debugging-port=${port}`, '--no-sandbox',
  '--disable-gpu', '--user-data-dir=/tmp/shellcheck-prof',
  // Headless Chrome has no mouse and reports (hover: none); a computer has one.
  '--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4',
  'about:blank'], { stdio: 'ignore', detached: true });
// google-chrome is a wrapper: end its whole process group, not the wrapper alone.
const quit = (code) => {
  try { process.kill(-chrome.pid, 'SIGTERM'); } catch { chrome.kill(); }
  process.exit(code);
};
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
ws.onmessage = (m) => {
  const d = JSON.parse(m.data);
  if (d.id && pend[d.id]) { pend[d.id](d); delete pend[d.id]; return; }
  if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text);
  if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') errors.push(d.params.args.map((a) => a.value || a.description).join(' '));
};
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
const js = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result.exceptionDetails) throw new Error(expr + ': ' + JSON.stringify(r.result.exceptionDetails));
  return r.result.result.value;
};
await send('Runtime.enable');
await send('Network.enable');
await send('Network.setExtraHTTPHeaders', { headers: { 'Remote-User': 'admin' } });
await send('Page.enable');
// A headless window has no focus of its own; the Studio reads a chat as read only
// in a focused window.
await send('Emulation.setFocusEmulationEnabled', { enabled: true });
// The unread counts come from the live node, where nothing may be new while the
// check runs: window.__fake = {id, unread} makes one chat's row of
// GET /api/desks/unread say it has news, and every read mark the page sends is
// kept in window.__marks. What the server counts is pinned in core-api's
// tests/test_desk_unread.py.
// A comment the task card posts is answered here and never reaches the
// planner, so the check writes nothing on the node: window.__cmts keeps what
// was posted.
await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
  window.__marks = [];
  window.__cmts = [];
  const f = window.fetch.bind(window);
  window.fetch = async (u, o) => {
    const url = String(u);
    if (o && o.method === 'POST' && url.startsWith('/api/tasks/') && url.endsWith('/comments')) {
      const content = JSON.parse(o.body).content;
      window.__cmts.push(content);
      const row = { id: 'browser-check-' + window.__cmts.length, author: 'checker', content, created_at: new Date().toISOString() };
      return new Response(JSON.stringify(row), { status: 201, headers: { 'Content-Type': 'application/json' } });
    }
    if (o && o.method === 'PATCH' && /read_upto/.test(String(o.body || ''))) window.__marks.push([url, JSON.parse(o.body).read_upto]);
    const r = await f(u, o);
    if (url === '/api/desks/unread' && window.__fake && r.ok) {
      const body = await r.json();
      for (const d of body.desks || []) {
        if (d.id === window.__fake.id) Object.assign(d, { unread: window.__fake.unread, last_at: window.__fake.at,
          last_text: 'browser check: something new', read_upto: '2026-01-01T00:00:00Z' });
      }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return r;
  };
})();` });

let fails = 0;
process.on('uncaughtException', (e) => { console.error(e); quit(3); });
process.on('unhandledRejection', (e) => { console.error(e); quit(3); });
const check = (name, ok, extra) => { console.log((ok ? 'ok   ' : 'FAIL ') + name + (extra && !ok ? ' -> ' + extra : '')); if (!ok) fails++; };
async function load(w, h, mobile, path) {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
  // A phone has a finger: (hover: none), as on a real one. Once on, it stays
  // for the rest of the run, so the phone checks come last.
  if (mobile) await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
  await send('Page.navigate', { url: base + path });
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    if (await js("document.querySelectorAll('#tabs .tab[data-id]').length > 0 && !!document.querySelector('#thread .sn-msg')").catch(() => false)) break;
  }
  await sleep(800);
}

// ---- desktop ----
await load(1440, 900, false, '/chats/' + desk);
await js("localStorage.removeItem('sn-chat-info')");
await load(1440, 900, false, '/chats/' + desk);
check('desktop: shell on', await js("document.querySelector('.app').classList.contains('cs')"));
check('desktop: list header shown', await js("getComputedStyle(document.querySelector('.cs-head')).display !== 'none'"));
check('desktop: chats are rows in a column', await js("getComputedStyle(document.getElementById('tabs')).flexDirection === 'column'"));
const nRows = await js("document.querySelectorAll('#tabs > .tab[data-id]:not([hidden])').length");
check('desktop: rows present', nRows > 0, nRows);
check('desktop: header names the chat', await js("document.querySelector('.cs-chead__nm').textContent.trim().length > 0"));
check('desktop: rail keeps section icons', await js("getComputedStyle(document.getElementById('navChats')).display !== 'none' && document.getElementById('navChats').getBoundingClientRect().width > 0"));
check('desktop: agent list folded', await js("document.getElementById('agents').getBoundingClientRect().height === 0"));
check('desktop: panel open by default', await js("document.querySelector('.app').classList.contains('cs-info-on') && document.querySelector('.cs-info').getBoundingClientRect().width > 200"));
check('desktop: Chat/Canvas switch in the header', await js("!!document.querySelector('.cs-chead__mode #mode')"));
await js("document.querySelector('.cs-info__x').click()");
await sleep(200);
check('desktop: cross hides the panel', await js("!document.querySelector('.app').classList.contains('cs-info-on') && document.querySelector('.cs-info').getBoundingClientRect().width === 0"));
check('desktop: hidden panel remembered', await js("localStorage.getItem('sn-chat-info') === '0'"));
await js("document.querySelector('.cs-ibtn').click()");
await sleep(200);
check('desktop: (i) shows it again', await js("document.querySelector('.app').classList.contains('cs-info-on')"));
await js("(() => { const i = document.querySelector('.cs-search input'); i.value = 'zzzz-no-such'; i.dispatchEvent(new Event('input')); })()");
await sleep(100);
check('desktop: search hides every row', await js("document.querySelectorAll('#tabs > .tab[data-id]:not([hidden])').length === 0"));
check('desktop: empty search says so', await js("!!document.querySelector('#tabs .cs-empty')"));
await js("(() => { const i = document.querySelector('.cs-search input'); i.value = ''; i.dispatchEvent(new Event('input')); })()");
await sleep(100);
check('desktop: clearing search brings rows back', await js("document.querySelectorAll('#tabs > .tab[data-id]:not([hidden])').length") === nRows);
await js("document.querySelector('.cs-chip[data-f=working]').click()");
await sleep(100);
const working = await js("[...document.querySelectorAll('#tabs > .tab[data-id]:not([hidden])')].every((t) => t.classList.contains('is-working'))");
check('desktop: Working filter keeps working chats only', working);
await js("document.querySelector('.cs-chip[data-f=all]').click()");
// another chat from the list
const other = await js("(() => { const t = [...document.querySelectorAll('#tabs > .tab[data-id]')].find((x) => !x.classList.contains('is-active')); if (!t) return null; t.click(); return t.dataset.id; })()");
await sleep(1500);
check('desktop: a row opens its chat', other && await js(`location.pathname.endsWith(${JSON.stringify(other)})`), other);
await js("history.back()");
await sleep(1200);
// Opening a chat lands at the end of its thread: a thread
// built anew used to read as messages that had just arrived, and the view glided
// up to the first of them. The chat given to this check should be long enough
// to scroll.
// history.back() above is such a switch: from another chat back to this one.
const atEnd = "(() => { const sc = document.getElementById('thread').parentElement; return sc.scrollHeight - sc.clientHeight - sc.scrollTop < 40; })()";
await sleep(800);
check('desktop: a chat opened from the list shows its end', await js(atEnd),
  await js("(() => { const sc = document.getElementById('thread').parentElement; return sc.scrollTop + ' of ' + (sc.scrollHeight - sc.clientHeight); })()"));
// A message that arrives in the open chat still glides up to the middle, with
// room kept under it.
await js("bubble('agent', 'browser check: an answer arrives')");
await sleep(900);
check('desktop: an arriving message keeps room under it', await js("!!document.querySelector('#thread > .sn-room')"));
// A message's menu is the right button: no Copy/Reply row
// under the messages any more.
// sel: the element to right-click; onSelection: on the selected text instead,
// where a person right-clicks to quote it (outside it the click moves it).
const rightClick = async (sel, onSelection) => {
  // Bring the message into view first and let the thread settle: a scroll still
  // running when the menu opens closes it, as a person's scroll does.
  if (!onSelection) {
    await js(`(${sel}).scrollIntoView({ block: 'center' })`);
    await sleep(400);
  }
  const pt = JSON.parse(await js(onSelection
    ? `(() => { const r = getSelection().getRangeAt(0).getBoundingClientRect(); return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) }); })()`
    : `(() => { const e = ${sel}; const r = e.getBoundingClientRect();
    return JSON.stringify({ x: Math.round(r.left + 20), y: Math.round(r.top + Math.min(r.height / 2, 30)) }); })()`));
  for (const type of ['mousePressed', 'mouseReleased']) {
    await send('Input.dispatchMouseEvent', { type, x: pt.x, y: pt.y, button: 'right', buttons: 2, clickCount: 1 });
  }
  await sleep(200);
  return pt;
};
const lastAgent = "[...document.querySelectorAll('#thread > .sn-msg--agent:not([hidden])')].filter((m) => !m.dataset.pending).pop()";
await js(`(() => { const m = ${lastAgent}; m.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })); })()`);
check('menu: no Copy/Reply row on hover', await js(`(() => { const m = ${lastAgent}; return [...m.querySelectorAll(':scope > .msg-copy, :scope > .msg-re')].every((c) => getComputedStyle(c).display === 'none'); })()`));
await rightClick(lastAgent);
check('menu: right button opens it', await js("!!document.querySelector('.sn-cm')"));
check('menu: Reply and Copy as text', await js("!!document.querySelector('.sn-cm [data-a=re]') && !!document.querySelector('.sn-cm [data-a=text]')"));
check('menu: inside the window', await js("(() => { const r = document.querySelector('.sn-cm').getBoundingClientRect(); return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; })()"));
await js("document.querySelector('.sn-cm [data-a=re]').click()");
await sleep(150);
check('menu: Reply fills the reply bar and closes', await js("!document.querySelector('.sn-cm') && /Reply to/.test((document.querySelector('#composer .sn-reply') || {}).textContent || '')"));
await js("document.querySelector('#composer .sn-reply__x') && document.querySelector('#composer .sn-reply__x').click()");
await rightClick(lastAgent);
await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
await sleep(100);
check('menu: Esc closes it', await js("!document.querySelector('.sn-cm')"));
// Part of the message selected: Quote that part.
const frag = await js(`(() => { const m = ${lastAgent}; m.scrollIntoView({ block: 'center' }); const w = document.createTreeWalker(m, NodeFilter.SHOW_TEXT);
  let n; while ((n = w.nextNode()) && n.nodeValue.trim().length < 12) {}
  if (!n) return ''; const r = document.createRange(); let i = 0; while (i < n.nodeValue.length && !n.nodeValue[i].trim()) i++; r.setStart(n, i); r.setEnd(n, i + 10);
  getSelection().removeAllRanges(); getSelection().addRange(r); return getSelection().toString().trim(); })()`);
await rightClick(lastAgent, true);
check('menu: a selection adds Quote selection', !!frag && await js("!!document.querySelector('.sn-cm [data-a=quote]')"), frag);
await js("document.querySelector('.sn-cm [data-a=quote]') && document.querySelector('.sn-cm [data-a=quote]').click()");
await sleep(150);
check('menu: Quote replies with only that part', !!frag && await js(`((document.querySelector('#composer .sn-reply') || {}).textContent || '').includes(${JSON.stringify(frag)})`));
await js("document.querySelector('#composer .sn-reply__x') && document.querySelector('#composer .sn-reply__x').click()");
await rightClick(lastAgent);
await js("document.querySelector('.sn-cm [data-a=text]').click()");
await sleep(100);
check('menu: Copy says Copied', await js("((document.querySelector('.sn-cm [data-a=text]') || {}).textContent || '') === 'Copied'"));
await sleep(500);
check('menu: and closes', await js("!document.querySelector('.sn-cm')"));
// other views keep their own layout
await js("document.getElementById('navTasks').click()");
await sleep(800);
check('desktop: Tasks view has no chat shell', await js("!document.querySelector('.app').classList.contains('cs')"));
await js("document.getElementById('navChats').click()");
await sleep(800);
check('desktop: back to chats, shell on', await js("document.querySelector('.app').classList.contains('cs')"));
// The task list stays a table on a computer.
await js("document.getElementById('navTasks').click()");
await sleep(2500);
check('desktop: tasks are a table', await js("getComputedStyle(document.querySelector('#tasksView .tkt thead')).display !== 'none'"));
await js("document.getElementById('navChats').click()");
await sleep(800);

// ---- unread counts ----
await load(1440, 900, false, '/chats/' + desk);
await sleep(1500);
check('unread: the chat on screen is marked read', await js(`window.__marks.every((m) => m[0].endsWith(${JSON.stringify(desk)}))`), await js('JSON.stringify(window.__marks)'));
const news = await js("(() => { const t = [...document.querySelectorAll('#tabs > .tab[data-id]')].find((x) => !x.classList.contains('is-active')); return t ? t.dataset.id : null; })()");
check('unread: another chat to receive news', !!news);
await js(`window.__fake = { id: ${JSON.stringify(news)}, unread: 3, at: new Date().toISOString() }`);
await js("window.dispatchEvent(new Event('focus'))");
await sleep(1200);
const rowOf = (id) => `document.querySelector('#tabs > .tab[data-id=${JSON.stringify(id)}]')`;
check('unread: its row has a badge', await js(`(() => { const b = ${rowOf(news)}.querySelector('.sn-unread'); return !!b && Number(b.textContent) >= 3; })()`));
check('unread: its row moved to the top', await js(`document.querySelector('#tabs > .tab[data-id]').dataset.id === ${JSON.stringify(news)}`));
check('unread: its last line is the new message', await js(`(${rowOf(news)}.querySelector('.m-last') || {}).textContent === 'browser check: something new'`));
check('unread: the open chat shows no badge', await js(`!${rowOf(desk)} || !${rowOf(desk)}.querySelector('.sn-unread')`));
check('unread: the Chats icon has the total', await js("Number((document.querySelector('#navChats .sn-unread') || {}).textContent) >= 3"));
check('unread: the page title has the total', await js('/^\\(\\d+\\+?\\) /.test(document.title)'), await js('document.title'));
check('unread: no mark for a chat not on screen', await js(`!window.__marks.some((m) => m[0].endsWith(${JSON.stringify(news)}))`));
await js("document.querySelector('.cs-chip[data-f=unread]').click()");
await sleep(150);
check('unread: the Unread filter keeps chats with news', await js(`(() => { const v = [...document.querySelectorAll('#tabs > .tab[data-id]:not([hidden])')]; return v.length > 0 && v.every((t) => t.classList.contains('has-unread')) && v.some((t) => t.dataset.id === ${JSON.stringify(news)}); })()`));
check('unread: its chip has the count', await js("Number((document.querySelector('.cs-chip[data-f=unread] b') || {}).textContent) >= 1"));
await js(`${rowOf(news)}.click()`);
await sleep(1500);
check('unread: opening it marks it read up to the newest message', await js(`window.__marks.some((m) => m[0].endsWith(${JSON.stringify(news)}) && m[1] === window.__fake.at)`), await js('JSON.stringify(window.__marks)'));
check('unread: and its badge is gone', await js(`!${rowOf(news)}.querySelector('.sn-unread')`));
await js('window.__fake = null');
await js("document.querySelector('.cs-chip[data-f=all]').click()");

// ---- a task opened by its link ----
// The task view opens before the chat shell loads; the shell kept its column
// and empty header over the task. TASK is any task id on the node.
const taskId = process.env.TASK || '1';
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/tasks/' + taskId });
await sleep(4000);
check('task link: no chat shell', await js("!document.querySelector('.app').classList.contains('cs')"));
check('task link: no chat list header', await js("getComputedStyle(document.querySelector('.cs-head')).display === 'none'"));
// A link in written text is not the text's own colour.
const linkVsText = (scope) => `(() => { const a = document.querySelector('${scope} a[href]'); if (!a) return 'no link';
  return getComputedStyle(a).color !== getComputedStyle(a.parentElement).color; })()`;
const taskLinks = await js(linkVsText('#taskDetail .docbody'));
check('task link: links stand out of the text', taskLinks === true || taskLinks === 'no link', taskLinks);
await js(`(() => { const p = document.createElement('div'); p.className = 'sn-msg sn-msg--agent'; p.id = '__lk';
  p.innerHTML = '<p>see <a href="https://example.com">example.com</a></p>'; document.body.appendChild(p); })()`);
check('chat: links stand out of the text', await js(linkVsText('#__lk')));
await js("document.getElementById('__lk').remove()");

// ---- the comment box on the task card: Ctrl+Enter posts, the name after @
// is lower-cased, the comment lands on top, Reply quotes it and names its
// author. The post is answered in the page (see __cmts above). ----
check('task card: a comment box above the comments', await js(`(() => {
  const b = document.querySelector('#tdBody #tdCmt'), l = document.querySelector('#tdBody .td__cmts');
  return !!b && !!l && !!document.querySelector('#tdBody #tdCmtSend') && !!(b.compareDocumentPosition(l) & Node.DOCUMENT_POSITION_FOLLOWING); })()`));
const cmtCount = () => js(`(() => { const s = [...document.querySelectorAll('#tdBody .td__sec')].find((e) => e.textContent.trim().startsWith('Comments'));
  const m = s && s.textContent.match(/\\((\\d+)\\)/); return m ? Number(m[1]) : -1; })()`);
const before = await cmtCount();
await js(`(() => { const b = document.querySelector('#tdBody #tdCmt'); b.value = '@Checker is this done?';
  b.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true })); })()`);
await sleep(600);
check('task card: Ctrl+Enter posts, the mention lower-cased', await js("window.__cmts[window.__cmts.length - 1] === '@checker is this done?'"), await js('JSON.stringify(window.__cmts)'));
check('task card: the box is emptied and says sent', await js("document.querySelector('#tdBody #tdCmt').value === '' && /^Sent/.test(document.querySelector('#tdBody #tdCmtMsg').textContent)"));
check('task card: the new comment is on top', await js("/@checker is this done\\?/.test((document.querySelector('#tdBody .td__cmts > .td__cmt') || {}).textContent || '')"));
check('task card: the count went up by one', (await cmtCount()) === before + 1, `${before} -> ${await cmtCount()}`);
await js("document.querySelector('#tdBody .td__cmts > .td__cmt .td__cmt-re').click()");
await sleep(200);
check('task card: Reply quotes the comment and names its author', await js(`(() => { const v = document.querySelector('#tdBody #tdCmt').value;
  return v.startsWith('> checker: @checker is this done?') && v.endsWith('@checker '); })()`), await js("JSON.stringify(document.querySelector('#tdBody #tdCmt').value)"));
await js("document.querySelector('#tdBody #tdCmt').value = ''");

// ---- Settings: task statuses live in the node panel; an
// admin gets a link to the panel, nobody a link to the planner's settings ----
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
await send('Page.navigate', { url: base + '/settings' });
await sleep(3000);
check('settings: no task-status card in the Studio', await js("!document.getElementById('statusDicts') && !document.getElementById('statusDictsLink')"));
const pc = JSON.parse(await js("(async () => { const me = await (await fetch('/api/me')).json(); const c = document.getElementById('panelCard'); const a = document.getElementById('panelLink'); return JSON.stringify({ role: me.role, hidden: c && c.hidden, href: a && a.href }); })()"));
check('settings: the node panel card is shown to an admin only', pc.hidden === !['super-admin', 'admin'].includes(pc.role), JSON.stringify(pc));
check('settings: it opens the panel, not a page in it', pc.hidden || /^https?:\/\/[^/]+\/$/.test(pc.href), JSON.stringify(pc));

// ---- phone ----
await load(390, 844, true, '/chats/' + desk);
check('phone: no desktop shell', await js("!document.querySelector('.app').classList.contains('cs')"));
check('phone: chat view of phone-chats', await js("document.querySelector('.app').classList.contains('m-chat')"));
check('phone: list header hidden', await js("getComputedStyle(document.querySelector('.cs-head')).display === 'none'"));
check('phone: panel hidden', await js("document.querySelector('.cs-info').getBoundingClientRect().width === 0"));
await load(390, 844, true, '/');
check('phone: list at /', await js("document.querySelector('.app').classList.contains('m-list')"));
await js(`window.__fake = { id: ${JSON.stringify(desk)}, unread: 2, at: new Date().toISOString() }`);
await js("window.dispatchEvent(new Event('focus'))");
await sleep(1200);
check('phone: a badge on the list row', await js(`!!(${rowOf(desk)} || document.body).querySelector('.sn-unread')`));
check('phone: the list is not reading the chat', await js('window.__marks.length === 0'), await js('JSON.stringify(window.__marks)'));
await js('window.__fake = null');

// Tasks on a phone are cards.
await load(390, 844, true, '/tasks');
await sleep(2500);
check('phone tasks: no table header', await js("getComputedStyle(document.querySelector('#tasksView .tkt thead')).display === 'none'"));
check('phone tasks: rows are cards with the title on its own line', await js(`(() => {
  const tr = document.querySelector('#tkBody tr[data-id]'); if (!tr) return 'no tasks';
  const t = tr.querySelector('.tkt__title');
  return getComputedStyle(tr).display === 'flex' && t.getBoundingClientRect().width > innerWidth * 0.55; })()`));
check('phone tasks: nothing wider than the screen', await js('document.documentElement.scrollWidth <= innerWidth'));
check('phone tasks: the filters sit above the list', await js(`(() => {
  const f = document.querySelector('.tasksv__filters').getBoundingClientRect();
  const l = document.querySelector('#tasksView .tasksv__scroll').getBoundingClientRect();
  return f.bottom <= l.top + 1; })()`));

// Who does it: assignee chips, several at once, kept across visits.
await js("localStorage.removeItem('sn-tasks-who'); document.querySelector('#tkWho [data-all]').click()");
await sleep(200);
const allRows = await js("document.querySelectorAll('#tkBody tr[data-id]').length");
check('tasks who: a chip per assignee and Anyone', await js("document.querySelectorAll('#tkWho .tkwho__c').length > 1 && document.querySelector('#tkWho [data-all]').classList.contains('is-on')"));
const whoPick = await js(`(() => { const c = document.querySelector('#tkWho .tkwho__c:not([data-all])'); c.click(); return c.dataset.w; })()`);
await sleep(200);
check('tasks who: a chip keeps only that assignee', await js(`(() => { const rows = [...document.querySelectorAll('#tkBody tr[data-id]')];
  return rows.length > 0 && rows.every((r) => (_tasksAll.find((t) => String(t.id) === r.dataset.id) || {}).assignee === ${JSON.stringify(whoPick)}); })()`));
await js(`[...document.querySelectorAll('#tkWho .tkwho__c:not([data-all])')].find((c) => c.dataset.w !== ${JSON.stringify(whoPick)}).click()`);
await sleep(200);
check('tasks who: two chips at once', await js("document.querySelectorAll('#tkWho .tkwho__c.is-on').length === 2 && !document.querySelector('#tkWho [data-all]').classList.contains('is-on')"));
await load(390, 844, true, '/tasks');
await sleep(2500);
check('tasks who: the choice is kept after a reload', await js("document.querySelectorAll('#tkWho .tkwho__c.is-on').length === 2"));
await js("document.querySelector('#tkWho [data-all]').click()");
await sleep(200);
check('tasks who: Anyone shows every task again', await js("document.querySelectorAll('#tkBody tr[data-id]').length") === allRows);

check('no page errors', errors.length === 0, errors.join(' | '));
console.log(fails ? `${fails} failed` : 'all passed');
quit(fails ? 1 : 0);
