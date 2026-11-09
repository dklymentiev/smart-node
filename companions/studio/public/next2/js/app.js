// Smart Node Studio -- shell. Real identity + agents from the node; canvas / chat
// interactions scaffolded, data fills in as sections get wired.
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

/* ---- auth-expiry interceptor ----
   When the Authum session dies, the forwardAuth bounces every /api call to the
   login page; a fetch FOLLOWS that redirect and resolves with the login HTML,
   which the app then fails to parse and shows as a generic error ("the agent did
   not respond in time"). Detect the bounce -- a 401, or a response whose final
   URL landed on the auth host -- and send the whole page to login (a top-level
   reload, which Traefik redirects there), instead of leaving a broken-looking
   chat and making the person guess they must refresh. */
/* that detection never fired. The login page is on ANOTHER origin
   (auth.<domain>), so a fetch that follows the bounce fails CORS and rejects
   with a bare TypeError: no response, nothing to inspect, and every caller
   swallowed it. A long message was "sent" into nothing, and only a reload told
   the owner he had been signed out. Now every same-origin call is made with
   redirect:'manual' (this app's own API never redirects), so the bounce comes
   back as a response of type 'opaqueredirect' and raises the wall below. */
let _authExpired = false;
const _rawFetch = window.fetch.bind(window);
function sameOrigin(input) {
  try { return new URL(typeof input === 'string' ? input : input.url, location.href).origin === location.origin; }
  catch (e) { return false; }
}
function isAuthBounce(r) {
  if (!r) return false;
  if (r.type === 'opaqueredirect' || r.status === 401) return true;
  try { return r.redirected && /(^|\.)auth\./.test(new URL(r.url, location.href).hostname); } catch (e) { return false; }
}
window.fetch = async (input, init) => {
  if (sameOrigin(input) && !(init && init.redirect)) init = Object.assign({}, init, { redirect: 'manual' });
  const r = await _rawFetch(input, init);
  if (isAuthBounce(r)) authExpired();
  return r;
};
// Is the session alive right now? Asked before a message leaves the composer,
// so an expired session keeps the text where it is instead of losing it.
async function authAlive() {
  try { return !isAuthBounce(await _rawFetch('/api/me', { redirect: 'manual', headers: { Accept: 'application/json' } })); }
  catch (e) { return true; } // offline is not signed out; the send reports its own failure
}
// The wall: said once, plainly, with the way back. Whatever is in the composer
// is stored for its chat first, so it is there again after signing in.
function authExpired() {
  if (_authExpired) return;
  _authExpired = true;
  try { if (typeof draftStash === 'function' && activeId) draftStash(activeId); } catch (e) {}
  const wall = document.getElementById('authWall');
  if (wall) wall.hidden = false;
}
{
  const b = document.getElementById('authWallGo');
  if (b) b.addEventListener('click', () => location.reload());
}
// A page left open in a background tab learns about it when it comes back,
// before anything is typed, not on the first send.
document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'visible' && !_authExpired && !(await authAlive())) authExpired();
});

/* ---- theme toggle (shared key with the rest of Smart Node) ---- */
$('#themeToggle').addEventListener('click', () => {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('sn-theme', next); document.cookie='sn-theme='+next+'; domain='+location.hostname.replace(/^[^.]+\./,'')+'; path=/; max-age=31536000; SameSite=Lax'; } catch (e) {}
  if (snPath() === '/tasks/dashboard') renderTasksDash();
  // re-tint any open html previews to the new theme
  if (window.snFormatter && window.snFormatter.themeHtmlPreviews) window.snFormatter.themeHtmlPreviews();
});

/* ---- collapse the sidebar (arrow collapses; logo expands when collapsed) ---- */
const _sbSet = (on) => {
  $('.app').classList.toggle('sb-collapsed', on);
  try { localStorage.setItem('sn-sb-collapsed', on ? '1' : '0'); } catch (e) {}
};
// a phone has no room for a sidebar column. There the sidebar is a full-screen panel
// (app.css, same breakpoint) opened by the logo button in the main column; the arrow closes it,
// and opening a view or a chat closes it (setView). The desktop's collapsed rail is a desktop
// preference: it is not applied on a phone and not overwritten by it.
const _phone = matchMedia('(max-width: 760px)');
const sbPanel = (on) => $('.app').classList.toggle('sb-open', on && _phone.matches);
$('#sbCollapse').addEventListener('click', () => (_phone.matches ? sbPanel(false) : _sbSet(true)));
$('#sbOpen').addEventListener('click', () => sbPanel(true));
// Esc closes what floats: the phone's panel, and any menu or question window
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { sbPanel(false); if (_pop) closePop(); } });
$('.sb__mk').addEventListener('click', () => {
  if (!_phone.matches && $('.app').classList.contains('sb-collapsed')) _sbSet(false);
});
function sbFitScreen() {
  let rail = false;
  try { rail = localStorage.getItem('sn-sb-collapsed') === '1'; } catch (e) {}
  $('.app').classList.toggle('sb-collapsed', rail && !_phone.matches);
  if (!_phone.matches) sbPanel(false);
}
_phone.addEventListener('change', sbFitScreen);
sbFitScreen();

/* ---- docs view: collapse the tree (left) / recent (right) panels like the sidebar.
   Open/closed state persists in localStorage (sn-docs-lcol / sn-docs-rcol). ---- */
const _dkv = () => $('#docsView');
const _dkSet = (side, on) => {
  const cls = side === 'l' ? 'is-lcol' : 'is-rcol';
  _dkv().classList.toggle(cls, on);
  try { localStorage.setItem('sn-docs-' + (side === 'l' ? 'lcol' : 'rcol'), on ? '1' : '0'); } catch (e) {}
};
const _on = (id, fn) => { const el = $('#' + id); if (el) el.addEventListener('click', fn); };
_on('dkLeftCol',  () => _dkSet('l', true));
_on('dkLeftExp',  () => _dkSet('l', false));
_on('dkRightCol', () => _dkSet('r', true));
_on('dkRightExp', () => _dkSet('r', false));
try {
  if (_dkv()) {
    // on a phone the default is BOTH panels collapsed, so the document
    // takes the full width and the panels open as overlays on demand (app.css,
    // same 640 px breakpoint). Explicit localStorage always wins over the
    // viewport-based default, so a user's choice on a rotated tablet sticks.
    const _dkNarrow = matchMedia('(max-width: 640px)').matches;
    const _dkL = localStorage.getItem('sn-docs-lcol');
    const _dkR = localStorage.getItem('sn-docs-rcol');
    if (_dkL === '1' || (_dkL === null && _dkNarrow)) _dkv().classList.add('is-lcol');
    if (_dkR === '1' || (_dkR === null && _dkNarrow)) _dkv().classList.add('is-rcol');
  }
} catch (e) {}

/* ---- desks: the user's workspaces shown as tabs. Create with +; x closes the tab, never the chat.
   Backed by core-api (/api/desks); owner- and tenant-scoped server-side. ---- */
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let desks = [];
let activeId = null;
let agentsList = [];
const agentBySlug = (s) => agentsList.find((a) => a.slug === s);
const deskAgent = (d) => (d && d.metadata && d.metadata.agent) || null;
const deskIsSystem = (d) => !!(d && d.metadata && d.metadata.system);
// The System chat speaks for the node: the product's mark, not an agent's face.
const SN_MARK = '<svg width="16" height="16" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><rect x="1" y="1" width="10" height="10"/><rect x="9" y="9" width="10" height="10"/></svg>';
// a desk whose tab was closed stays a chat in its agent's history; only an explicit
// tab_open === false hides the tab, so desks from before this field show as before.
const tabOpen = (d) => !(d && d.metadata && d.metadata.tab_open === false);
// The Chat/Canvas switch, kept by reference: renderTabs rewrites the tabs row and puts it back.
const _modeSeg = document.getElementById('mode');

// Who is signed in, and what mail domain this node calls its own. Both
// arrive from /api/me at boot. They exist so that no label in this file has to
// name a particular person or a particular domain: on somebody else's node a
// literal is simply wrong, and it is wrong silently.
let meIdentity = { user: null, handle: null, role: null, mailDomain: null, voiceUrl: null };
let meAuthUrl = '';   // Authum's address, from /api/me (sign out, password)

// The contacts registry, handle (lower-cased) -> display name, resolved by the
// server at boot. Empty is normal and harmless: an address that resolves to
// nothing renders as itself. See /api/directory in src/server.js for why the
// panel shows the correspondent at all rather than a generic word.
let contactsByHandle = {};
function dshContactName(value) {
  const s = String(value || '').trim().toLowerCase();
  return (s && contactsByHandle[s]) || '';
}

const _deskMeta = {};
// Canonical provider-context measurement comes from core-api.  Studio
// never derives it from messages, transcript files, or cumulative session usage.
let _contextByDesk = {};
let _contextReadError = false;
function contextBand(s) {
  if (_contextReadError) return 'error';
  if (!s || s.percent == null) return 'empty';
  return s.percent >= 80 ? 'high' : (s.percent >= 60 ? 'warn' : 'normal');
}
function contextBadge(s) {
  if (!s || s.percent == null) return '';
  const pct = Math.max(0, Number(s.percent) || 0);
  return `<span class="ctxbadge is-${contextBand(s)}" title="Provider context used">${pct}%</span>`;
}
// The tab shows context as a line along its bottom edge, not a number in its title
//: its length is how full the context is, and it runs through a
// green-amber-red gradient on the meter's 60/80 bands (app.css .tab__ctx). The figures
// are in the tab's tooltip.
function tabContextLine(s) {
  if (!s || s.percent == null) return '';
  const pct = Math.min(100, Math.max(0, Number(s.percent) || 0));
  if (!pct) return '';   // nothing used: no line (and no division by zero in the CSS)
  return `<span class="tab__ctx is-${contextBand(s)}" aria-hidden="true"><span style="width:${pct}%;--pct:${pct}"></span></span>`;
}
function tabContextTip(s) {
  if (!s || s.percent == null) return '';
  return `Context ${Math.max(0, Number(s.percent) || 0)}% \u00b7 ${formatTokens(s.context_tokens)} / ${formatTokens(s.threshold_tokens)}`;
}
function contextForAgent(slug) {
  return desks.filter((d) => deskAgent(d) === slug)
    .map((d) => ({ desk: d, status: _contextByDesk[d.id] }))
    .filter((x) => x.status && x.status.percent != null)
    .sort((a, b) => Number(b.status.percent) - Number(a.status.percent))[0] || null;
}
function formatTokens(n) {
  if (n == null) return '';
  if (n >= 1000000) return (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + 'm';
  if (n >= 1000) return Math.round(n / 1000) + 'k';
  return String(n);
}
// What a chat's context meter says. The meter lives in the chat card, next to
// the other session actions: the composer only carries the message,
// and how full the context is already shows as the line under the tab.
function contextView(s) {
  const band = contextBand(s);
  let text = '\u2026', pct = 0;
  if (_contextReadError) text = 'Context status unavailable';
  else if (s && s.percent != null) {
    // The fill shows even where automatic handoff does not run (an HTTP agent,
    // or handoff switched off): how full the context is still matters.
    pct = Math.max(0, Number(s.percent) || 0);
    text = `${pct}% \u00b7 ${formatTokens(s.context_tokens)} / ${formatTokens(s.threshold_tokens)}`
      + (s.enabled ? '' : ' \u00b7 handoff off');
  }
  else if (s && !s.enabled) text = s.threshold_scope === 'agent'
    ? 'N/A \u00b7 disabled by this agent\'s override'
    : 'N/A \u00b7 automatic context handoff is off';
  return { band, text, pct: Math.min(100, pct), canClean: !_contextReadError && !!(s && s.session_id) };
}
function paintActiveContext() {
  const meter = $('#contextMeter'), value = $('#ctxValue'), fill = $('#ctxFill'), clean = $('#ctxClean');
  if (!meter || !value || !fill || !clean) return;
  const s = activeId && _contextByDesk[activeId];
  const band = contextBand(s);
  meter.className = 'ctxmeter is-' + band;
  let text = '\u2026';
  let pct = 0;
  if (_contextReadError) text = 'Context status unavailable';
  else if (s && s.percent != null) {
    pct = Math.max(0, Number(s.percent) || 0);
    text = `${pct}% · ${formatTokens(s.context_tokens)} / ${formatTokens(s.threshold_tokens)}`
      + (s.enabled ? '' : ' · handoff off');
  }
  else if (s && !s.enabled) text = s.threshold_scope === 'agent'
    ? 'N/A · disabled by this agent\'s override'
    : 'N/A · automatic context handoff is off';
  value.textContent = text;
  fill.style.width = Math.min(100, pct) + '%';
  const bar = meter.querySelector('[role="progressbar"]');
  if (bar) bar.setAttribute('aria-valuenow', String(Math.min(100, pct)));
  clean.disabled = _contextReadError || !s || !s.session_id;
}
function paintAgentContext() {
  $$('[data-context-agent]').forEach((el) => {
    const hit = contextForAgent(el.dataset.contextAgent);
    if (!hit) { el.hidden = true; el.style.display = 'none'; return; }
    el.hidden = false;
    el.dataset.contextDesk = hit.desk.id;
    el.className = 'acard__context ctxbadge is-' + contextBand(hit.status);
    el.textContent = hit.status.percent + '% context';
    el.title = 'Open the chat using the most provider context';
  });
}
async function loadContextStatuses(deskId) {
  const qs = deskId ? `&desk_id=${encodeURIComponent(deskId)}` : '';
  try {
    const r = await fetch('/api/context-status?scope=desks' + qs, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const rows = (await r.json()).contexts || [];
    if (!deskId) _contextByDesk = {};
    if (deskId && !rows.some((row) => row.desk_id === deskId)) delete _contextByDesk[deskId];
    for (const row of rows) if (row.desk_id) _contextByDesk[row.desk_id] = row;
    _contextReadError = false;
  } catch (e) {
    _contextReadError = true;
  }
  renderTabs();
  if (_apSlug) renderAgentChats(_apSlug);
  paintAgentContext();
  paintActiveContext();
}
// Poll cursor: history length last rendered per desk, so the relay poller
// only re-renders when a new message actually arrives.
const _histLen = {};
function relTime(v) {
  if (!v) return '';
  const s = Math.floor((Date.now() - new Date(v)) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + '\u00a0min';
  if (s < 86400) return Math.floor(s / 3600) + '\u00a0h';
  if (s < 172800) return 'yesterday';
  if (s < 604800) return Math.floor(s / 86400) + '\u00a0d';
  const d = new Date(v); return d.getDate() + '\u00a0' + ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
}
// A due reminder reaches the agent wrapped in what the model needs (core-api
// routes/reminders.py: "Reminder due now, which the person asked for in this chat
// on <when>: <text>. Remind them now: ..."). A person reads only <text>.
function reminderText(s) {
  // the tail may be cut off in a preview, so it is optional
  const m = String(s || '').match(/^Reminder due now, which the person asked for in this chat on [^:]+:\d\d: ([\s\S]*?)(?:\. Remind them now:[\s\S]*)?$/);
  return m ? m[1].replace(/\. Remind[\s\S]*$/, '') : String(s || '');
}
function _autoTitle(userMsg, agentReply) {
  const clean = (s) => String(s || '').replace(/[*_`#>\[\]]/g, '').replace(/\s+/g, ' ').trim();
  const u = clean(userMsg);
  if (u.length > 20) return u.slice(0, 50);
  const a = clean(agentReply).split('\n')[0];
  if (a.length > 8) return a.slice(0, 50);
  return null;
}
function _patchDeskTitle(deskId, title) {
  fetch('/api/desks/' + encodeURIComponent(deskId), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ title }),
  }).then((r) => r.ok ? r.json() : null).then((upd) => {
    if (!upd || !upd.title) return;
    const d = desks.find((x) => x.id === deskId);
    if (d) d.title = upd.title;
    renderTabs();
  }).catch(() => {});
}
// the top bar shows ONE tab per agent, chats grouped inside. Each tab is a two-row
// button: agent name and last activity on top, active chat title on the bottom, a chevron on
// the right that opens the list of that agent's recent chats. Chats with no agent (the
// Studio desk case) still render as their own single-row tab.
// Which chat is "current" inside an agent's tab: the one the owner had on screen last for
// that agent. Falls back to activeId (if it belongs to the agent), else the freshest by
// last_message_at. Persisted in memory only; a page reload starts from freshest.
const _lastChatByAgent = {};
function _rememberActiveAgentDesk() {
  const d = desks.find((x) => x.id === activeId);
  if (!d) return;
  const slug = deskAgent(d);
  if (slug) _lastChatByAgent[slug] = activeId;
}
// For the age label and the tooltip: the freshest thing we know about the chat,
// including the last assistant reply we just loaded into the thread. Only for
// display: sorting must be stable across clicks, so _deskOrder is what tabs use
// for their left-to-right position.
function _deskWhen(x) {
  const m = _deskMeta[x.id] || {};
  return String(x.last_message_at || m.lastAt || x.updated_at || '');
}
// For left-to-right ordering: only the server-side timestamp. Opening a chat
// runs loadHistory and updates _deskMeta[id].lastAt to the last assistant reply's
// created_at, which was fresher than the other agents' server times and pulled
// the tab left on every activation. Sort must ignore it.
function _deskOrder(x) {
  return String(x.last_message_at || x.updated_at || '');
}
function renderTabs() {
  // the tabs are the chats with an open tab, plus the one on screen (a link can open a
  // closed chat). The cross closes a tab, never the chat, and asks first.
  // grouped per agent. An agent's tab exists iff any of that agent's chats is open
  // (tab_open) or is the active one.
  _rememberActiveAgentDesk();
  // A closed chat that has news is shown too: its count is in the totals, so its row must exist.
  const shown = desks.filter((d) => tabOpen(d) || d.id === activeId || (window.snUnreadOf && window.snUnreadOf(d.id) > 0));
  const byAgent = new Map();
  const noAgent = [];
  for (const d of shown) {
    const slug = deskAgent(d);
    if (slug) {
      if (!byAgent.has(slug)) byAgent.set(slug, []);
      byAgent.get(slug).push(d);
    } else {
      noAgent.push(d);
    }
  }
  for (const arr of byAgent.values()) arr.sort((p, q) => _deskOrder(q).localeCompare(_deskOrder(p)));
  noAgent.sort((p, q) => _deskOrder(q).localeCompare(_deskOrder(p)));
  const agentSlugs = [...byAgent.keys()].sort((s1, s2) =>
    _deskOrder(byAgent.get(s2)[0]).localeCompare(_deskOrder(byAgent.get(s1)[0])));
  const totalTabs = agentSlugs.length + noAgent.length;
  const closable = totalTabs > 1;
  const agentTabs = agentSlugs.map((slug) => {
    const chats = byAgent.get(slug);
    const cur = chats.find((d) => d.id === activeId)
              || chats.find((d) => d.id === _lastChatByAgent[slug])
              || chats[0];
    const ag = agentBySlug(slug);
    const nm = ag ? (ag.title || ag.slug || slug) : slug;
    const icon = ag
      ? avInner(avResolve(ag.avatar), (nm || '?').trim().charAt(0).toUpperCase())
      : '<i data-lucide="layout-dashboard"></i>';
    const _t = relTime(_deskWhen(cur));
    const isActive = cur.id === activeId;
    const _m = _deskMeta[cur.id] || {};
    const _ctxTip = tabContextTip(_contextByDesk[cur.id]);
    const _tip = esc([_m.lastMsg || '', _ctxTip].filter(Boolean).join('\n'));
    return `
    <div class="tab tab--agent ${isActive ? 'is-active sn-edge sn-lit' : ''}" data-id="${cur.id}" data-agent="${esc(slug)}" title="${_tip}" tabindex="0" role="button">
      <span class="tab__av tab__ic">${icon}</span>
      <div class="tab__rows">
        <div class="tab__row tab__row--top"><span class="tab__nm">${esc(nm)}</span></div>
        <div class="tab__row tab__row--bot"><span class="tab__title">${esc(cur.title)}</span></div>
      </div>
      <div class="tab__meta">
        ${_t ? `<span class="tab__age">${_t}</span>` : '<span class="tab__age"></span>'}
      </div>
      ${tabContextLine(_contextByDesk[cur.id])}
      ${closable ? `<span class="tab__x" data-close-agent="${esc(slug)}" title="Close tab (all chats with ${esc(nm)})">&times;</span>` : ''}
    </div>`;
  });
  const noAgentTabs = noAgent.map((d) => {
    const _m = _deskMeta[d.id] || {};
    const _t = relTime(_deskWhen(d));
    const _ctxTip = tabContextTip(_contextByDesk[d.id]);
    const _tip = esc([_m.lastMsg || '', _ctxTip].filter(Boolean).join('\n'));
    // A row like an agent's, its name in place of the agent's
    if (deskIsSystem(d)) return `
    <div class="tab tab--agent ${d.id === activeId ? 'is-active sn-edge sn-lit' : ''}" data-id="${d.id}" title="${_tip}" tabindex="0" role="button">
      <span class="tab__av tab__ic">${SN_MARK}</span>
      <div class="tab__rows"><div class="tab__row tab__row--top"><span class="tab__nm">${esc(d.title)}</span></div></div>
      <div class="tab__meta">${_t ? `<span class="tab__age">${_t}</span>` : '<span class="tab__age"></span>'}</div>
      ${closable ? `<span class="tab__x" data-close="${d.id}" title="Close tab">&times;</span>` : ''}
    </div>`;
    return `
    <div class="tab ${d.id === activeId ? 'is-active sn-edge sn-lit' : ''}" data-id="${d.id}" title="${_tip}" tabindex="0" role="button">
      <span class="tab__av tab__ic"><i data-lucide="layout-dashboard"></i></span><span class="tab__title">${esc(d.title)}</span>${_t ? `<span class="tab__age">${_t}</span>` : ''}${tabContextLine(_contextByDesk[d.id])}
      ${closable ? `<span class="tab__x" data-close="${d.id}" title="Close tab">&times;</span>` : ''}
    </div>`;
  });
  // One order for all: a chat with no agent rises by its new messages like the rest
  //.
  const allTabs = [...agentTabs.map((html, i) => ({ html, at: _deskOrder(byAgent.get(agentSlugs[i])[0]) })),
    ...noAgentTabs.map((html, i) => ({ html, at: _deskOrder(noAgent[i]) }))]
    .sort((p, q) => q.at.localeCompare(p.at)).map((t) => t.html).join('');
  // New chat first: at the end of a long row the + was out of reach.
  $('#tabs').innerHTML = `<button class="tab-add" id="tabAdd" title="New chat"><i data-lucide="plus"></i></button>` + allTabs;
  $('#tabs').appendChild(_modeSeg);
  // a chat row is reached by Tab and opened by Enter or Space, as a button is
  $$('#tabs .tab').forEach((el) => el.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target === el) { e.preventDefault(); el.click(); }
  }));
  $$('#tabs .tab').forEach((el) => el.addEventListener('click', (e) => {
    const xa = e.target.closest('[data-close-agent]');
    if (xa) { e.stopPropagation(); confirmCloseAgentTab(xa, xa.dataset.closeAgent); return; }
    const x = e.target.closest('[data-close]');
    if (x) { e.stopPropagation(); confirmCloseTab(x, el.dataset.id); return; }
    history.pushState({}, '', '/chats/' + encodeURIComponent(el.dataset.id));
    setActive(el.dataset.id);
  }));
  $('#tabAdd').addEventListener('click', (e) => { e.stopPropagation(); openNewChatPicker($('#tabAdd')); });
  if (window.lucide) lucide.createIcons();
  paintDeskWork();
}
// dropdown for an agent's tab. Shows up to 5 most recent chats (freshest first),
// New chat, and View all (opens the agent page's Chats tab). Picking a chat that is not
// tab_open reopens it. View all: /agents/<slug>/chats.
function openAgentTabMenu(anchor, slug) {
  const mine = desks.filter((d) => deskAgent(d) === slug);
  const when = _deskWhen;
  // Chats with something new come first, each with its count, then the latest
  //.
  const unreadOf = (d) => (d.id === activeId || !window.snUnreadOf ? 0 : window.snUnreadOf(d.id));
  const recent = mine.slice().sort((p, q) => ((unreadOf(q) > 0) - (unreadOf(p) > 0)) || when(q).localeCompare(when(p)))
    .slice(0, Math.max(5, mine.filter((d) => unreadOf(d) > 0).length));
  const ag = agentBySlug(slug);
  const nm = ag ? (ag.title || ag.slug || slug) : slug;
  // Each row's "..." takes its time's place on hover and opens Rename and Delete
  //.
  const recentHtml = recent.length
    ? recent.map((d) => `<button class="pop__it ${d.id === activeId ? 'is-cur' : ''}" data-open="${d.id}"><span class="pop__it__t">${esc(d.title)}</span>`
      + `${unreadOf(d) ? `<b class="sn-unread pop__unread">${unreadOf(d)}</b>` : ''}<span class="pop__when"><time class="pop__it__at">${relTime(when(d))}</time><span class="pop__more" data-more="${d.id}" title="More">&middot;&middot;&middot;</span></span></button>`).join('')
    : '<div class="pop__empty">no chats yet</div>';
  const pop = showPop(anchor, `
    <div class="pop__lbl">Recent chats with ${esc(nm)}</div>
    ${recentHtml}
    ${mine.length > 5 ? `<div class="pop__sep"></div><button class="pop__it" data-act="all"><i data-lucide="list"></i> View all ${mine.length} chats</button>` : ''}`);
  pop.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', (e) => {
    const more = e.target.closest('[data-more]');
    if (more) { e.stopPropagation(); return openChatRowMenu(more, more.dataset.more); }
    closePop();
    _lastChatByAgent[slug] = b.dataset.open;
    openDesk(b.dataset.open);
  }));
  const ba = pop.querySelector('[data-act="all"]');
  if (ba) ba.addEventListener('click', () => { closePop(); openAgentPage(slug, { tab: 'chats' }); });
}
// A chat's own menu: Rename and Delete, each asking in a window in the middle.
function openChatRowMenu(anchor, id) {
  // The "..." lives in the menu this one replaces, and showPop closes that menu
  // first, so the new one is placed at a stand-in where the "..." was.
  const r = anchor.getBoundingClientRect(), at = document.createElement('span');
  at.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;pointer-events:none`;
  document.body.appendChild(at);
  const pop = showPop(at, '<button class="pop__it" data-act="rename"><i data-lucide="pencil"></i> Rename</button>'
    + '<button class="pop__it" data-act="link"><i data-lucide="link"></i> Copy link</button>'
    + (deskIsSystem(desks.find((d) => d.id === id)) ? '' : '<button class="pop__it pop__it--danger" data-act="delete"><i data-lucide="trash-2"></i> Delete</button>'));
  at.remove();
  pop.querySelector('[data-act="rename"]').addEventListener('click', () => renameChat(id));
  pop.querySelector('[data-act="link"]').addEventListener('click', async () => {
    closePop();
    try { await navigator.clipboard.writeText(location.origin + '/chats/' + encodeURIComponent(id)); window.snToast('Link copied'); } catch {}
  });
  const del = pop.querySelector('[data-act="delete"]');
  if (del) del.addEventListener('click', () => deleteChat(id));
  if (window.lucide) lucide.createIcons();
}
function renameChat(id) {
  const d = desks.find((x) => x.id === id); if (!d) return;
  const pop = showPop(document.body, `<div class="pop__q">Rename the chat</div>
    <label class="sn-field" style="margin:6px 4px 14px"><input data-title value="${esc(d.title || '')}"></label>
    <div class="pop__row"><button class="pop__btn" type="button" data-no>Cancel</button><button class="pop__btn pop__btn--pri" type="button" data-yes>Save</button></div>`, 'pop--modal');
  const inp = pop.querySelector('[data-title]');
  inp.focus(); inp.select();
  const save = async () => {
    const title = inp.value.trim();
    closePop();
    if (!title || title === d.title) return;
    const r = await fetch(`/api/desks/${encodeURIComponent(id)}`, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ title }) }).catch(() => null);
    if (r && r.ok) { d.title = title; renderTabs(); }
    else window.snToast(r ? `Could not rename the chat (${r.status}). Try again.` : 'Could not reach the node. Try again.');
  };
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } });
  pop.querySelector('[data-no]').addEventListener('click', closePop);
  pop.querySelector('[data-yes]').addEventListener('click', save);
}
function deleteChat(id) {
  const d = desks.find((x) => x.id === id); if (!d) return;
  const pop = showPop(document.body, `<div class="pop__q">Delete “${esc(d.title || 'this chat')}”?</div>
    <div class="pop__note">It will be gone from your chats. This cannot be undone.</div>
    <div class="pop__row"><button class="pop__btn" type="button" data-no>Cancel</button><button class="pop__btn pop__btn--danger" type="button" data-yes>Delete</button></div>`, 'pop--modal');
  pop.querySelector('[data-no]').addEventListener('click', closePop);
  pop.querySelector('[data-yes]').addEventListener('click', async () => {
    closePop();
    const r = await fetch(`/api/desks/${encodeURIComponent(id)}`, { method: 'DELETE', headers: { Accept: 'application/json' } }).catch(() => null);
    // a refusal is said, not swallowed: the window closed and the chat stayed, with
    // no word why
    if (!r || !r.ok) { window.snToast(r ? `Could not delete the chat (${r.status}). Try again.` : 'Could not reach the node. Try again.'); return; }
    const slug = deskAgent(d);
    desks = desks.filter((x) => x.id !== id);
    if (activeId === id) {
      // the agent's freshest chat takes its place, or a new one
      const next = desks.filter((x) => deskAgent(x) === slug).sort((p, q) => _deskWhen(q).localeCompare(_deskWhen(p)))[0];
      if (next) openDesk(next.id); else createDeskFor(slug, 'New chat');
    } else renderTabs();
  });
}
// close the whole agent tab: clear tab_open on every chat of that agent. Chats
// themselves are not deleted. If activeId was among them, jump to the freshest still-open
// tab; if none, leave activeId as is (renderTabs still shows it as the current one).
function confirmCloseAgentTab(anchor, slug) {
  const mine = desks.filter((d) => deskAgent(d) === slug);
  const openOnes = mine.filter(tabOpen);
  const activeInAgent = mine.some((d) => d.id === activeId);
  if (openOnes.length === 0 && !activeInAgent) return;
  const ag = agentBySlug(slug);
  const nm = ag ? (ag.title || ag.slug || slug) : slug;
  const nToClose = openOnes.length || (activeInAgent ? 1 : 0);
  const pop = showPop(anchor, `
    <div class="pop__q">Close ${nToClose === 1 ? 'this tab' : 'all ' + nToClose + ' tabs'} with ${esc(nm)}?</div>
    <div class="pop__note">The chats stay in ${esc(nm)}'s history. Open any of them again from the agent page or a link.</div>
    <div class="pop__row"><button class="pop__btn" type="button" data-no>Cancel</button><button class="pop__btn pop__btn--pri" type="button" data-yes>Close tab</button></div>`, 'pop--modal');
  pop.querySelector('[data-no]').addEventListener('click', closePop);
  pop.querySelector('[data-yes]').addEventListener('click', async () => {
    closePop();
    try {
      for (const d of openOnes) await setTabOpen(d.id, false);
      delete _lastChatByAgent[slug];
      if (activeInAgent) {
        const next = desks.find((x) => deskAgent(x) !== slug && tabOpen(x));
        if (next) { history.pushState({}, '', '/chats/' + encodeURIComponent(next.id)); setActive(next.id); return; }
      }
      renderTabs();
    } catch (e) { console.error('[studio] close agent tab failed', e); }
  });
}

// While a chat is on screen the address names it: /chats/<id>. replaceState, not
// push: this corrects the address of what is already shown, it is not a new step
// for the back button. Paths that already push /chats/<id> make this a no-op.
function syncChatUrl() {
  if (!activeId) return;
  const want = '/chats/' + encodeURIComponent(activeId);
  if (snPath() !== want) history.replaceState({}, '', want);
}
function setActive(id) {
  const prev = activeId;
  if (prev !== id) draftStash(prev);
  setView('chats');
  activeId = id;
  if (prev !== id) draftRestore(id);
  syncDeskLive();
  syncChatUrl();
  renderTabs();
  paintChatControls();
  loadHistory(id);
  loadSession(id);
  paintActiveContext();
  loadContextStatuses(id);
}
// Each load of the thread gets a number; only the latest one draws. Two loads in
// flight at once (the boot path and the route both ask for the chat on screen)
// each cleared the thread and then both appended, and the history showed twice.
let _histGen = 0;
// Which chat the thread on screen belongs to. A reload of the SAME chat keeps what
// is shown until the new history has arrived and then swaps it in one go: the
// relay poll resyncs every chat once after each answer, and clearing first left
// the thread empty for the length of the request -- the owner saw the whole chat
// flash after every reply. A switch to another chat still clears
// at once, so the old chat never shows under the new tab.
let _threadFor = null;
// Opening a chat lands on its last message. One scrollTop write is not enough:
// images and formatter frames reach their height after the thread is drawn, and
// the end moves away from the reader. Keep the view on the end while the thread
// grows, and let go the moment the reader scrolls or presses a key.
// Where each chat was left, kept for this tab's life (and across a reload of it).
const _scrollMem = {};
// sn.scroll2: positions saved before 2026-10-30 were stale (saved only on leaving
// a chat) and a reload kept landing on them; the new key starts clean.
function scrollMemGet(id) {
  if (_scrollMem[id]) return _scrollMem[id];
  try { const v = JSON.parse(sessionStorage.getItem('sn.scroll2.' + id) || 'null'); if (v && typeof v.top === 'number') return (_scrollMem[id] = v); } catch {}
  return null;
}
function scrollMemSet(id, v) {
  _scrollMem[id] = v;
  try { sessionStorage.setItem('sn.scroll2.' + id, JSON.stringify(v)); } catch {}
}
// Noted as the reader scrolls, not only on leaving for another chat: a reload
// never left the chat, so it came back to a position from long before, near the
// top. A moment after the scroll stops is enough.
(function rememberScroll() {
  const sc = $('#thread') && $('#thread').parentElement;
  if (!sc) return;
  let tm = null;
  sc.addEventListener('scroll', () => {
    clearTimeout(tm);
    tm = setTimeout(() => {
      if (_threadFor && sc.clientHeight > 0) {
        scrollMemSet(_threadFor, { top: sc.scrollTop, atBottom: sc.scrollHeight - sc.scrollTop - sc.clientHeight < 40 });
      }
    }, 200);
  }, { passive: true });
})();
// A chat opened where it was left. Images and formatted blocks reach their height
// after the thread is drawn, so the position is written again a moment later;
// the reader's own scroll cancels that.
function restoreTop(sc, top) {
  let done = false;
  const set = () => { if (!done) sc.scrollTop = top; };
  const stop = () => {
    done = true;
    for (const ev of ['wheel', 'touchstart', 'keydown', 'mousedown']) sc.removeEventListener(ev, stop);
  };
  set();
  requestAnimationFrame(set);
  setTimeout(set, 250);
  setTimeout(set, 900);
  setTimeout(stop, 1200);
  for (const ev of ['wheel', 'touchstart', 'keydown', 'mousedown']) sc.addEventListener(ev, stop, { once: true });
}
function pinBottom(sc) {
  const down = () => { sc.scrollTop = sc.scrollHeight; };
  down();
  requestAnimationFrame(down);
  const thr = $('#thread');
  if (!thr || typeof ResizeObserver === 'undefined') return;
  const ro = new ResizeObserver(down);
  const stop = () => {
    ro.disconnect();
    for (const ev of ['wheel', 'touchstart', 'keydown', 'mousedown']) sc.removeEventListener(ev, stop);
  };
  ro.observe(thr);
  for (const ev of ['wheel', 'touchstart', 'keydown', 'mousedown']) sc.addEventListener(ev, stop, { once: true });
  setTimeout(stop, 2500);
}
// A reader who has scrolled up from the end of a chat is shown a floating chevron with the
// number of messages below the view; pressing it scrolls to the end. New messages that
// arrive while the reader is up there do not move the view (see loadHistory), so without
// this the reader has no sign that the chat has moved on.
const JUMP_GAP = 80;   // px from the end under which the reader counts as "at the end"
function chatJumpState(gap, below) {
  return { show: gap >= JUMP_GAP, count: Math.max(0, below | 0) };
}
function paintJump() {
  const btn = $('#threadJump'), thr = $('#thread');
  if (!btn || !thr) return;
  const sc = thr.parentElement;
  if (!sc || sc.clientHeight === 0) { btn.hidden = true; return; }
  const edge = sc.getBoundingClientRect().bottom;
  let below = 0;
  for (const el of thr.children) {
    if (el.classList && el.classList.contains('sn-msg') && el.getBoundingClientRect().top >= edge) below++;
  }
  const st = chatJumpState(sc.scrollHeight - sc.scrollTop - sc.clientHeight, below);
  btn.hidden = !st.show;
  if (!st.show) return;
  // sit just above the composer, which keeps the end of the chat clear by its own height
  btn.style.bottom = Math.max(16, (parseFloat(sc.style.paddingBottom) || 0) - 6) + 'px';
  $('#threadJumpN').textContent = st.count ? st.count + (st.count === 1 ? ' message' : ' messages') : '';
}
(function wireJump() {
  const btn = $('#threadJump'), thr = $('#thread');
  if (!btn || !thr) return;
  const sc = thr.parentElement;
  let queued = false;
  const later = () => { if (queued) return; queued = true; requestAnimationFrame(() => { queued = false; paintJump(); }); };
  sc.addEventListener('scroll', later, { passive: true });
  window.addEventListener('resize', later);
  if (typeof MutationObserver !== 'undefined') new MutationObserver(later).observe(thr, { childList: true });
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(later).observe(thr);
  btn.addEventListener('click', () => sc.scrollTo({ top: sc.scrollHeight, behavior: 'smooth' }));
  later();
})();

// One stored message of the chat, drawn at the end of the thread.
function drawStored(m, opts) {
  const isUser = m.role === 'user';
  // A user turn goes through userHtml so a pasted block stays collapsed after
  // a reload, as it was when sent. A reply cut short by Stop keeps its
  // mark after a reload.
  const el = bubble(isUser ? 'user' : 'agent',
    isUser ? userHtml(m.content)
           : (snHumanFailure(m.content) ? `<span class="sn-reconnect">${esc(snHumanFailure(m.content))}</span>`
             : window.snFormatter ? snFormatter.format(m.content, {}) : esc(m.content))
             + (m.stopped ? stoppedNote(null) : ''), opts);
  // What "Copy as Markdown" hands back: the message as it was written.
  if (el) el._raw = m.content;
  if (el) stampTime(el, m.created_at);
  if (el && m.message_id != null) el.dataset.messageId = String(m.message_id);
  if (el && m.reminder) el.dataset.reminder = '1';
  if (el && m.system) el.dataset.author = 'system';
  if (el && m.tools) usedLine(el, m.tools);
  return el;
}

async function loadHistory(id) {
  const t = $('#thread');
  if (!t) return;
  const gen = ++_histGen;
  const _opening = _threadFor !== id;
  // Leaving a chat: note where it was being read, so coming back lands there
  //. At the end of the chat is "no position": it opens at
  // the end, where the newest message is.
  if (_opening) {
    const _sc0 = t.parentElement;
    if (_threadFor && _sc0 && _sc0.clientHeight > 0) {
      scrollMemSet(_threadFor, { top: _sc0.scrollTop, atBottom: _sc0.scrollHeight - _sc0.scrollTop - _sc0.clientHeight < 40 });
    }
    t.innerHTML = ''; _threadFor = id; _tailDesk = null;
  }
  if (!id) return;
  try {
    const r = await fetch(`/api/desks/${encodeURIComponent(id)}/history`, { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const { messages } = await r.json();
    if (gen !== _histGen || id !== activeId) return;
    const sc = t.parentElement;
    const atBottom = !sc || sc.scrollHeight - sc.scrollTop - sc.clientHeight < 40;
    const keepTop = sc ? sc.scrollTop : 0;
    t.innerHTML = '';
    for (const m of (messages || [])) drawStored(m);
    // The newest stored message on screen: the live stream's next "this chat
    // changed" reads only what came after it (refreshOpenChat).
    const _newest = (messages || [])[(messages || []).length - 1];
    if (_newest) _histLast[id] = _newest.created_at; else delete _histLast[id];
    _busDrawn[id] = new Set();
    // Turns still running or queued in this chat. Rebuilding the thread
    // on a tab switch used to throw their pending replies away, so coming back to
    // a chat that was working showed a question and no sign of work. The message
    // itself is usually stored already; it is drawn only when it is not.
    for (const turn of deskTurns(id)) {
      const stored = turn.base != null
        && (messages || []).some((m, i) => i >= turn.base && m.role === 'user' && m.content === turn.text);
      if (!stored) bubble('user', userHtml(turn.text, id));
      turnBubble(turn, false);
    }
    // Failures kept for this chat: the question again when the history has not
    // stored it, and the reply that did not come, with Try again.
    for (const f of _failed[id] || []) {
      if (!(messages || []).some((m) => m.role === 'user' && m.content === f.text)) bubble('user', userHtml(f.text, id));
      failedTurn(id, f.text, f.message);
    }
    // The person's last message with no answer while nothing works on it, seen
    // after a reload (the failure was only on the screen that sent it). A
    // minute's grace covers an answer still being written.
    const _lm = (messages || [])[(messages || []).length - 1];
    if (_lm && _lm.role === 'user' && !hasTurns(id) && !(_failed[id] || []).some((f) => f.text === _lm.content)
        && _deskWorkKnown === true && !_deskWork[id] && Date.now() - new Date(_lm.created_at).getTime() > 60000) {
      failedTurn(id, _lm.content, 'No reply.');
    }
    // heads, day lines, avatars drawn now, in this step (chat-authors.js)
    if (window.snAuthorsPaint) window.snAuthorsPaint();
    paintChatControls();
    paintQueuedHistoryControls(id);
    // With room kept under the last question the view stays where the reader
    // has it: rebuilding the thread must not send it to the end of that room.
    if (_tailDesk === id) { syncTail(); if (sc) sc.scrollTop = keepTop; }
    // A hidden scroller (the phone chat list is showing) has no height and loses
    // its position, so a write now lands nowhere: pin it, the observer in
    // pinBottom writes again once the chat is shown.
    else if (sc && _opening && sc.clientHeight > 0 && scrollMemGet(id) && !scrollMemGet(id).atBottom) restoreTop(sc, scrollMemGet(id).top);
    else if (sc && (_opening || sc.clientHeight === 0)) pinBottom(sc);
    else if (sc) sc.scrollTop = atBottom ? sc.scrollHeight : keepTop;
    _histLen[id] = (messages || []).length;
    const _last = (messages || []).filter((m) => m.role === 'assistant').pop();
    if (_last) { _deskMeta[id] = { lastMsg: String(snHumanFailure(_last.content) || _last.content || '').replace(/[*_`#>\[\]]/g, '').replace(/\s+/g, ' ').slice(0, 80).trim(), lastAt: String(_last.created_at || '') }; renderTabs(); }
    const _dk = desks.find((d) => d.id === id);
    if (_dk && ['New chat', 'Untitled desk'].includes(_dk.title) && (messages || []).length > 0) {
      const _fu = (messages || []).find((m) => m.role === 'user');
      const _fa = (messages || []).find((m) => m.role === 'assistant');
      const _tt = _autoTitle(_fu && _fu.content, _fa && _fa.content);
      if (_tt) _patchDeskTitle(id, _tt);
    }
  } catch {}
}

// The newest gateway session of each desk, for the chat card's session actions (these
// were two hidden buttons in the removed header row).
const _deskSess = {};
async function loadSession(id) {
  if (!id) return;
  try {
    const r = await fetch(`/api/desks/${encodeURIComponent(id)}/sessions`, { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const s = ((await r.json()).sessions || [])[0];
    if (s) _deskSess[id] = s; else delete _deskSess[id];
  } catch {}
}

async function loadDesks() {
  try {
    const r = await fetch('/api/desks', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    desks = (await r.json()).desks || [];
  } catch { desks = []; }
  // A user always keeps a home desk -- where a chat lands when no other desk is
  // chosen. If they have none yet, create it so there is always somewhere to talk.
  if (desks.length === 0) {
    const home = await createHome();
    if (home) desks = [home];
  }
  if (!desks.some((d) => d.id === activeId)) activeId = (desks.find(tabOpen) || desks[0])?.id ?? null;
  // Prep the active desk, but don't let this async load steal the view from a
  // deep link (e.g. /docs/x, /tasks/x, /dashboard): route() already chose it.
  renderTabs();
  // On a deep link to an agent page, the chats list + overload counts rendered
  // before desks loaded -> refresh them now that desks are here.
  if (_apSlug) { renderAgentChats(_apSlug); renderOverview(); }
  loadHistory(activeId);
  loadSession(activeId);
  loadContextStatuses();
  if (!/^\/(tasks|docs)(\/[^/]+)?$|^\/dashboard$|^\/settings$|^\/agents(\/|$)|^\/workflows(\/|$)|^\/work-map$|^\/calendar$/.test(snPath())) setView('chats');
}

// The node's first agent is whatever the node has -- it was `sysadmin`, then `foreman`
// This used to hardcode 'secretary', so the Main desk was bound to
// an agent that does not exist and the FIRST thing a guest typed came back HTTP 500. Ask
// the node instead of guessing; agentsList is not loaded yet at this point in boot.
async function firstAgentSlug() {
  try {
    const r = await fetch('/api/agents', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const list = (await r.json()).agents || [];
    const a = list.find(x => x.enabled !== false) || list[0];
    return a ? a.slug : null;
  } catch (e) { console.error('[studio] could not resolve the first agent', e); return null; }
}

async function createHome() {
  try {
    const slug = await firstAgentSlug();
    const body = slug ? { title: 'Main desk', agent_slug: slug } : { title: 'Main desk' };
    const r = await fetch('/api/desks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    return await r.json();
  } catch (e) { console.error('[studio] could not create home desk', e); return null; }
}

// The agent's latest chat, or a new one when it has none.
function openAgentChat(slug) {
  const d = desks.filter((k) => deskAgent(k) === slug).sort((p, q) => String(_deskWhen(q)).localeCompare(String(_deskWhen(p))))[0];
  return d ? openDesk(d.id) : createDeskFor(slug, 'New chat');
}
async function createDeskFor(slug, title) {
  try {
    // An empty chat with this agent is opened, not made again: each press of New
    // chat made one more "New chat", where ChatGPT and Claude
    // return to the blank one. The server's list says which have no messages; only
    // one this button made (its default title), not an old empty chat with a name.
    const lr = await fetch('/api/desks', { headers: { Accept: 'application/json' } });
    const blank = lr.ok && ((await lr.json()).desks || []).find((d) => d.message_count === 0 && d.title === (title || 'New chat') && deskAgent(d) === (slug || null));
    if (blank) {
      if (!desks.some((d) => d.id === blank.id)) desks.unshift(blank);
      history.pushState({}, '', '/chats/' + encodeURIComponent(blank.id));
      setActive(blank.id);
      closePop();
      return blank;
    }
    const r = await fetch('/api/desks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ title: title || 'New chat', agent_slug: slug || null }),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    const desk = await r.json();
    desks.unshift(desk);
    // The new chat gets its address at once, the same way a tab click gives one
    // Without it the bar kept the page the chat was made from, e.g.
    // /agents/foreman/overview, so a reload or a shared link lost the chat.
    history.pushState({}, '', '/chats/' + encodeURIComponent(desk.id));
    setActive(desk.id);
    closePop();
  } catch (e) { console.error('[studio] create desk failed', e); }
}

/* ---- agent / new-chat popovers ---- */
let _pop = null;
let _scrim = null, _popArrow = null;
function closePop() {
  if (_popArrow) { _popArrow.remove(); _popArrow = null; }
  if (_scrim) { _scrim.remove(); _scrim = null; }
  if (_pop) { _pop.remove(); _pop = null; document.removeEventListener('mousedown', _popOutside, true); }
}
function _popOutside(e) { if (_pop && !_pop.contains(e.target)) closePop(); }
function showPop(anchor, html, cls) {
  closePop();
  _pop = document.createElement('div');
  _pop.className = 'pop' + (cls ? ' ' + cls : '');
  _pop.innerHTML = html;
  document.body.appendChild(_pop);
  // A modal (a question that must be answered) sits in the middle of the screen on every device.
  const modal = !!(cls && cls.split(' ').includes('pop--modal'));
  if (modal || _phone.matches) {
    // a modal, and any popover on a phone, lies over a scrim. The scrim is a real element
    // that takes the dismissing tap: closing on mousedown, as the desktop does, removed the
    // popover before the tap ended, so the tap landed on what was under it (the owner closed the
    // card and got "Close this tab?" from the tab's cross).
    _scrim = document.createElement('div');
    _scrim.className = 'pop-scrim';
    _scrim.addEventListener('click', closePop);
    document.body.insertBefore(_scrim, _pop);
  }
  if (!modal && _phone.matches) {
    // On a phone a popover drops down from what opened it, across the screen, with a pointer to
    // it; the row it came from stays undimmed. A sheet at the bottom looked detached.
    const r = anchor.getBoundingClientRect();
    _pop.classList.add('pop--drop');
    _pop.style.top = (r.bottom + 8) + 'px';
    _pop.style.maxHeight = Math.max(200, window.innerHeight - r.bottom - 20) + 'px';
    _scrim.style.top = r.bottom + 'px';
    _popArrow = document.createElement('div');
    _popArrow.className = 'pop-arrow';
    _popArrow.style.top = (r.bottom + 3) + 'px';
    _popArrow.style.left = Math.max(16, Math.min(r.left + r.width / 2 - 6, window.innerWidth - 28)) + 'px';
    document.body.appendChild(_popArrow);
  } else if (!modal) {
    const r = anchor.getBoundingClientRect();
    const w = _pop.offsetWidth || 250;
    _pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + 'px';
    _pop.style.top = (r.bottom + 5) + 'px';
    _pop.style.maxHeight = Math.max(160, window.innerHeight - r.bottom - 16) + 'px';
  }
  if (!_scrim) setTimeout(() => document.addEventListener('mousedown', _popOutside, true), 0);
  if (window.lucide) lucide.createIcons();
  return _pop;
}
function openAgentMenu(anchor, slug) {
  const recent = desks.filter((d) => deskAgent(d) === slug).slice(0, 5);
  const recentHtml = recent.length
    ? recent.map((d) => `<button class="pop__it" data-open="${d.id}">${esc(d.title)}</button>`).join('')
    : '<div class="pop__empty">no chats yet</div>';
  const pop = showPop(anchor, `
    <button class="pop__it pop__new" data-new><i data-lucide="plus"></i> New chat</button>
    <div class="pop__lbl">recent</div>${recentHtml}
    <div class="pop__sep"></div>
    <button class="pop__it" data-settings><i data-lucide="settings"></i> Settings</button>`);
  pop.querySelector('[data-new]').addEventListener('click', () => createDeskFor(slug, 'New chat'));
  // Same as a tab click: the chat opens at its own address.
  pop.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => { history.pushState({}, '', '/chats/' + encodeURIComponent(b.dataset.open)); setActive(b.dataset.open); closePop(); }));
  pop.querySelector('[data-settings]').addEventListener('click', () => { closePop(); openAgentPage(slug); });
}
function openNewChatPicker(anchor) {
  const items = agentsList.length
    ? agentsList.map((a) => `<button class="pop__it" data-pick="${esc(a.slug)}"><span class="pop__av">${avInner(avResolve(a.avatar), (a.title || a.slug || '?').trim().charAt(0).toUpperCase())}</span> <span class="pop__nm">${esc(a.title || a.slug)}</span></button>`).join('')
    : '<div class="pop__empty">no agents</div>';
  // A long team ran past the bottom of the screen in one column: on a
  // computer the agents lie in up to four columns, six to a column, and the list scrolls.
  const cols = Math.min(4, Math.max(1, Math.ceil(agentsList.length / 6)));
  const pop = showPop(anchor, `<div class="pop__lbl">new chat with</div><div class="pop__grid" style="--cols:${cols}">${items}</div>`, 'pop--agents');
  pop.querySelectorAll('[data-pick]').forEach((b) => b.addEventListener('click', () => createDeskFor(b.dataset.pick, 'New chat')));
}

// close or reopen a chat's tab. A view state kept on the desk by core-api
// (metadata.tab_open), so it follows the owner to every device. The chat itself is never
// deleted from the Studio: the owner's rule, 2026-09-12.
async function setTabOpen(id, open) {
  const r = await fetch('/api/desks/' + encodeURIComponent(id), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ tab_open: open }),
  });
  if (!r.ok) throw new Error('http ' + r.status);
  const upd = await r.json();
  const d = desks.find((x) => x.id === id);
  if (d && upd.metadata) d.metadata = upd.metadata;
  return d;
}
// Closing a tab is confirmed: one mistaken tap must not take a chat off the screen.
function confirmCloseTab(anchor, id) {
  const d = desks.find((x) => x.id === id);
  const a = agentBySlug(deskAgent(d));
  const who = a ? (a.title || a.slug) : 'the agent';
  const pop = showPop(anchor, `
    <div class="pop__q">Close this tab?</div>
    <div class="pop__note">The chat stays in ${esc(who)}'s history. Open it again from the chat card or the agent page.</div>
    <div class="pop__row"><button class="pop__btn" type="button" data-no>Cancel</button><button class="pop__btn pop__btn--pri" type="button" data-yes>Close tab</button></div>`, 'pop--modal');
  pop.querySelector('[data-no]').addEventListener('click', closePop);
  pop.querySelector('[data-yes]').addEventListener('click', async () => {
    closePop();
    try {
      await setTabOpen(id, false);
      if (activeId === id) {
        const next = desks.find((x) => x.id !== id && tabOpen(x));
        if (next) { history.pushState({}, '', '/chats/' + encodeURIComponent(next.id)); setActive(next.id); return; }
      }
      renderTabs();
    } catch (e) { console.error('[studio] close tab failed', e); }
  });
}
// Open a chat, giving its tab back first if it was closed. url: the address to show
// (the agent page keeps its own /agents/<slug>/chats/<id>).
async function openDesk(id, url) {
  const d = desks.find((x) => x.id === id);
  if (d && !tabOpen(d)) {
    try { await setTabOpen(id, true); } catch (e) { console.error('[studio] reopen tab failed', e); }
  }
  history.pushState({}, '', url || '/chats/' + encodeURIComponent(id));
  setActive(id);
}
function confirmCleanContext(anchor, deskId = activeId) {
  if (!deskId) return;   // the endpoint finds the chat's session itself
  const pop = showPop(anchor, `
    <div class="pop__q">Start with clean context?</div>
    <div class="pop__note">This closes the current provider session. Your visible chat history, Mesh memory, and Documents stay intact. The next message starts a clean session; unsaved facts from the old provider transcript will not carry over.</div>
    <div class="pop__row"><button class="pop__btn" type="button" data-no>Cancel</button><button class="pop__btn pop__btn--pri" type="button" data-yes>Start clean</button></div>`, 'pop--modal');
  pop.querySelector('[data-no]').addEventListener('click', closePop);
  pop.querySelector('[data-yes]').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = 'Clearing...';
    try {
      const r = await fetch(`/api/desks/${encodeURIComponent(deskId)}/clean-context`, {
        method: 'POST', headers: { Accept: 'application/json' },
      });
      if (!r.ok) throw new Error('http ' + r.status);
      closePop();
      delete _deskSess[deskId];
      await loadContextStatuses(deskId);
      loadSession(deskId);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = 'Could not clear — retry';
    }
  });
}

/* ---- Chat | Canvas mode (CSS positions the composer per mode) ---- */
// Canvas: the chat column's width, dragged by its left edge and kept per browser.
// The board keeps at least 320 px and so does the chat.
(function threadWidth() {
  const task = $('#task'), grip = $('#threadGrip');
  if (!task || !grip) return;
  const clamp = (w) => Math.round(Math.max(320, Math.min(w, task.clientWidth - 320)));
  const set = (w) => task.style.setProperty('--thread-w', clamp(w) + 'px');
  try { const w = Number(localStorage.getItem('sn.threadW')); if (w) set(w); } catch {}
  grip.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    task.classList.add('is-sizing');
    const right = task.getBoundingClientRect().right;
    const move = (ev) => set(right - ev.clientX);
    const up = () => {
      task.classList.remove('is-sizing');
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      try { localStorage.setItem('sn.threadW', parseInt(task.style.getPropertyValue('--thread-w'), 10)); } catch {}
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
  });
})();
$$('#mode button').forEach((b) => b.addEventListener('click', () => {
  $$('#mode button').forEach((x) => { x.classList.remove('is-on'); x.setAttribute('aria-selected', 'false'); });
  b.classList.add('is-on'); b.setAttribute('aria-selected', 'true');
  $('#task').className = 'task mode-' + b.dataset.m;
}));
// the switch is hidden on a narrow screen (app.css, same breakpoint), so a narrow
// screen must never be left in Canvas with no way out, e.g. a desktop window narrowed.
function modeFitScreen() {
  if (_phone.matches && $('#task').classList.contains('mode-canvas')) $('#mode button[data-m="chat"]').click();
}
_phone.addEventListener('change', modeFitScreen);
modeFitScreen();

/* ---- movable canvas: drag the background to pan the board ---- */
(function () {
  const tc = $('#tcanvas'), inner = $('#streamInner');
  const pan = { x: 0, y: 0 };
  let drag = null;
  const apply = () => { inner.style.transform = `translate(${pan.x}px, ${pan.y}px)`; };
  tc.addEventListener('mousedown', (e) => {
    if (e.target.closest('.composer')) return;
    e.preventDefault();
    drag = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
    tc.classList.add('is-panning');
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    pan.x = drag.px + (e.clientX - drag.x);
    pan.y = drag.py + (e.clientY - drag.y);
    apply();
  });
  window.addEventListener('mouseup', () => { if (drag) { drag = null; tc.classList.remove('is-panning'); } });
})();

/* ---- identity + logout ---- */
async function loadMe() {
  let me;
  try { me = await (await fetch('/api/me', { headers: { Accept: 'application/json' } })).json(); }
  catch { me = { user: null, authUrl: null, mailDomain: null }; }
  meIdentity = { user: me.user || null, handle: me.handle || null, role: me.role || null, mailDomain: me.mailDomain || null, voiceUrl: me.voiceUrl || null };
  $('#whoName').textContent = me.user || 'unknown';
  const form = $('#logoutForm');
  meAuthUrl = me.authUrl ? me.authUrl.replace(/\/$/, '') : '';
  if (me.authUrl) form.action = me.authUrl.replace(/\/$/, '') + '/';   // Authum asks, with its own CSRF token
  else $('#logoutBtn').disabled = true;
}

// Names for the feed. A failure here must not cost the panel anything: labels
// simply stay addresses, which is what they fall back to per-address anyway.
async function loadDirectory() {
  try {
    const r = await fetch('/api/directory', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    contactsByHandle = (await r.json()).handles || {};
  } catch { contactsByHandle = {}; }
}

/* ---- My Studio: real agents from core-api ---- */
function agentRow(a) {
  const initial = (a.title || a.slug || '?').trim().charAt(0).toUpperCase();
  const st = a.enabled ? 'work' : 'idle';
  const role = a.role || a.description || '';
  return `<div class="agent ${a.enabled ? '' : 'is-off'}" data-slug="${a.slug}">
    <div class="sn-av">${avInner(avResolve(a.avatar), initial)}<span class="sn-av__st sn-av__st--${st}"></span></div>
    <div><div class="nm">${a.title || a.slug}</div><small>${role}</small><small class="agent__seen" data-seen-slug="${a.slug}"></small></div>
  </div>`;
}
async function loadAgents() {
  const box = $('#agents');
  try {
    const r = await fetch('/api/agents', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const data = await r.json();
    const list = data.agents || [];
    agentsList = list;
    box.innerHTML = list.length ? list.map(agentRow).join('') : `<div class="sb__empty">No agents yet.</div>`;
    paintAgentState();
    paintLastSeen();
    renderTabs();
    if (/^\/agents\/?$/.test(snPath())) renderAgentsGrid();
    if (_apSlug) { const _a = agentBySlug(_apSlug); const _nm = $('#apName'); if (_nm && _a) _nm.textContent = _a.title || _a.slug; renderAgentChats(_apSlug); loadAgentActivity(_apSlug); renderOverview(); }
  } catch {
    box.innerHTML = `<div class="sb__empty">Couldn't load agents.</div>`;
  }
}

/* ---- who is working: a turning ring on an agent that holds a turn ----
   The owner could not tell from the Studio which agents were busy. core-api already
   answers it (GET /api/agents/state: free | busy | broken, with the ref a busy agent
   holds and stale=true for a busy row whose lease has run out), so the sidebar and
   the agent cards read it every few seconds and mark the avatar. Busy turns a ring;
   stale and broken are still marks, because a thing that needs a person should not
   look like a thing that is working. */
let _agentState = {};
let _agentStateLoaded = false;        // an empty map before the first answer is "not known", not "nobody"
function paintAgentState() {
  if (typeof paintLastSeen === 'function') paintLastSeen();
  document.querySelectorAll('.agent[data-slug], .acard[data-slug]').forEach((el) => {
    const s = _agentState[el.dataset.slug];
    const st = !s ? 'free' : (s.status === 'busy' ? (s.stale ? 'stale' : 'busy') : (s.status || 'free'));
    el.classList.toggle('is-busy', st === 'busy');
    el.classList.toggle('is-stale', st === 'stale');
    el.classList.toggle('is-broken', st === 'broken');
    const av = el.querySelector('.sn-av');
    if (!av) return;
    av.title = st === 'busy' ? 'Working' + (s.current_ref ? ': ' + s.current_ref : '')
      : st === 'stale' ? 'Marked busy, but its lease has run out'
      : st === 'broken' ? 'Broken' + (s.last_error ? ': ' + s.last_error : '')
      : 'Free';
  });
  // The agent page: the hero avatar gets the same ring, and the Status card says in
  // words what the agent holds and since when. "Active" beside it is the on/off
  // switch, not this; the owner read it as "working" (2026-09-14).
  document.querySelectorAll('[data-state-slug]').forEach((el) => {
    const s = _agentState[el.dataset.stateSlug];
    const st = !s ? 'free' : (s.status === 'busy' ? (s.stale ? 'stale' : 'busy') : (s.status || 'free'));
    el.classList.toggle('is-busy', st === 'busy');
    el.classList.toggle('is-stale', st === 'stale');
    el.classList.toggle('is-broken', st === 'broken');
  });
  // The agent page: while the agent holds a turn, the Live timeline opens with a line for
  // it (the owner's idea, 2026-09-14: the present belongs at the top of the timeline, not
  // in a separate field). Free: no line.
  document.querySelectorAll('[data-now-row]').forEach((row) => {
    const slug = row.dataset.nowRow;
    const s = _agentState[slug];
    const txt = row.querySelector('[data-now-slug]');
    const time = row.querySelector('[data-now-time]');
    if (!s || !txt || s.status === 'free' || !s.status) { row.hidden = true; return; }
    let text;
    if (s.status === 'busy' && !s.stale) {
      const t = nowTask(slug);
      const what = t ? 'task #' + t.id + (t.title ? ' ' + t.title : '') : describeRef(s.current_ref);
      text = 'Working on ' + what;
    } else if (s.status === 'busy') {
      text = 'Marked busy on ' + describeRef(s.current_ref) + ', but its lease ran out';
    } else {
      text = 'Broken' + (s.last_error ? ': ' + s.last_error : '');
    }
    txt.innerHTML = tlLinkTasks(text);
    txt.title = text;
    // The time column holds when the turn started, and it is the time that pulses while
    // the turn is live: the line reads as "since 14:55, happening now".
    if (time) {
      time.textContent = s.updated_at ? tkDate(s.updated_at) : '';
      time.classList.toggle('is-live', s.status === 'busy' && !s.stale);
    }
    row.hidden = false;
  });
}
// The task an agent is on: its in-progress task touched most recently, preferring the
// one its latest tool call named. Only on the agent page, where its tasks are loaded.
function nowTask(slug) {
  if (slug !== _apSlug || !Array.isArray(_apTasks)) return null;
  // The task its latest tool call touched comes first, whatever that task's status:
  // an agent updating #201 is on #201 even if the update moved it to review.
  const lastTool = Array.isArray(_apTimeline) ? _apTimeline.find((e) => e.type === 'tool.invoked' && tlTaskId(e.path)) : null;
  if (lastTool) {
    const id = tlTaskId(lastTool.path);
    return _apTasks.find((t) => String(t.id) === id) || { id, title: '' };
  }
  const mine = _apTasks.filter((t) => tkStage(t) === 'in_progress');
  if (!mine.length) return null;
  return mine.slice().sort((a, b) => (Date.parse(b.updated_at) || 0) - (Date.parse(a.updated_at) || 0))[0];
}
// What a runtime ref means to a person: desk-<id> is a chat, dispatch:<n> a message
// that came in on a channel, anything else is shown as it is.
function describeRef(ref) {
  const r = String(ref || '');
  if (!r) return 'something';
  if (r.startsWith('desk-')) return 'a chat';
  if (r.startsWith('runner-')) return 'a wake from the runner';
  const m = r.match(/^dispatch:(\d+)$/);
  if (m) return 'message #' + m[1];
  return r;
}
async function loadAgentState() {
  try {
    const r = await fetch('/api/agents/state', { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const next = {};
    ((await r.json()).agents || []).forEach((a) => { next[a.slug] = a; });
    _agentState = next;
    _agentStateLoaded = true;
    paintAgentState();
    dshLoadNow();                       // the dashboard card follows the same poll as the sidebar
  } catch { /* a missed poll changes nothing; the next one repaints */ }
}
loadAgentState();
// no poll: the live stream (below) reads it again on agent.* and on (re)open

// When each agent last did anything, under its role in the sidebar. Polled slower than
// the busy state: minutes do not need five-second precision.
let _lastSeen = {};
function paintLastSeen() {
  document.querySelectorAll('[data-seen-slug]').forEach((el) => {
    // While the agent holds a turn the line says so and pulses, like the start time of
    // the live line on its page. The list keeps its alphabetical
    // order: moving a working agent to the top reshuffles what a person is scanning.
    const st = _agentState[el.dataset.seenSlug];
    const working = !!st && st.status === 'busy' && !st.stale;
    el.classList.toggle('is-live', working);
    if (working) { el.textContent = 'working now'; el.title = st.current_ref ? 'Working: ' + st.current_ref : 'Working'; return; }
    const ts = _lastSeen[el.dataset.seenSlug];
    if (!ts) { el.textContent = ''; return; }
    const r = relTime(ts);
    el.textContent = (r === 'just now' || r === 'yesterday') ? 'active ' + r : 'active ' + r + ' ago';
    el.title = new Date(ts).toLocaleString();
  });
}
async function loadLastSeen() {
  try {
    const r = await fetch('/api/agents/last-seen', { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    _lastSeen = (await r.json()).last_seen || {};
    paintLastSeen();
  } catch { /* next poll */ }
}
loadLastSeen();
// no poll: read again on agent.* events and on the stream's (re)open

// Contact-row glyph: pick a Lucide icon per channel. Lucide ships a real `slack`
// mark; it has no Telegram logo, so `send` (paper plane) is the house convention
// (matches CH_ICONS in the dashboard feed).
function contactIcon(c) {
  const s = String((c && (c.slug || c.label)) || '').toLowerCase();
  const ref = String((c && c.ref) || '');
  if (s.includes('slack')) return 'slack';
  if (s.includes('telegram')) return 'send';
  if (s.includes('whatsapp')) return 'message-circle';
  if (s.includes('mail') || s.includes('email') || ref.includes('@')) return 'mail';
  if (s.includes('chat') || s.includes('web')) return 'message-circle';
  if (s.includes('voice') || s.includes('phone') || s.includes('call')) return 'phone';
  return 'radio';
}

/* ---- Agents overview: a card per agent; click opens the agent page. The footer
   opens the agent page's tabs; it led to Mail / Chats / Activity, tabs the page
   no longer has. ---- */
const ACARD_TABS = [['chat', 'message-square', 'Chat'], ['channels', 'radio', 'Channels'], ['knowledge', 'book-open', 'Knowledge'], ['memory', 'brain', 'Memory']];
const _agentContacts = {};   // slug -> the agent's enabled channels, kept between visits
async function renderAgentsGrid() {
  const box = $('#agentsGrid'); if (!box) return;
  // an agent is created in the node panel (it lays out the agent's folder); the
  // Studio had no way there. Same audience as Settings' link.
  meReady.then(() => {
    const nl = $('#newAgentLink'); if (!nl) return;
    const host = location.hostname.replace(/^studio\./, 'admin.');
    nl.href = (host !== location.hostname ? `${location.protocol}//${host}` : '') + '/agents/new/';
    nl.hidden = !PANEL_ROLES.includes(meIdentity.role);
  });
  if (!agentsList.length) { box.innerHTML = '<div class="skel sk-acard" role="status" aria-busy="true" aria-label="Loading"></div>'.repeat(6); return; }
  const roster = agentsList.slice();
  // Contact data = the agent\'s ENABLED channels (real instance_ref, e.g. an email address), one call per agent;
  // failures degrade to none. The cards are drawn at once from what is already known (kept from the last visit),
  // and each agent's contacts fill in when its own call answers: the grid used to stay blank until the slowest of
  // all the calls had come back.
  const contactsHtml = (chs) => chs.map((c) => `<div class="acard__contact"><i data-lucide="${contactIcon(c)}"></i><span>${esc(c.ref || c.label)}</span></div>`).join('');
  box.innerHTML = roster.map((a, i) => {
    const init = (a.title || a.slug || '?').trim().charAt(0).toUpperCase();
    const role = a.role || '';
    const desc = a.description || '';
    const chs = _agentContacts[a.slug];   // undefined: not asked yet
    const contact = chs && chs.length ? contactsHtml(chs) : (chs ? '' : '<div class="skel skline w70"></div>');
    const meta = [];
    // The whole <runtime>/<model>: split('/').pop() showed openrouter/openai/gpt-5
    // as "gpt-5", hiding who runs the agent -- the very thing the id now states.
    if (a.model) meta.push(esc(String(a.model)));
    if (a.schedule) meta.push(esc(a.schedule));
    meta.push(a.enabled ? 'active' : 'disabled');
    const hottest = contextForAgent(a.slug);
    return `<div class="acard acard--file" data-slug="${esc(a.slug)}">
      <div class="acard__main">
        <span class="sn-av acard__av">${avInner(avResolve(a.avatar), init)}</span>
        <div class="acard__info">
          <div class="acard__nm">${esc(a.title || a.slug)}</div>
          <div class="acard__role">${esc(role)}</div>
          ${desc ? `<div class="acard__desc">${esc(desc)}</div>` : ''}
          <div class="acard__contacts" data-contacts="${esc(a.slug)}"${chs && !chs.length ? ' hidden' : ''}>${contact}</div>
          <button class="acard__context ctxbadge${hottest ? ' is-' + contextBand(hottest.status) : ''}" type="button" data-context-agent="${esc(a.slug)}"${hottest ? ` data-context-desk="${hottest.desk.id}"` : ' hidden'}>${hottest ? hottest.status.percent + '% context' : ''}</button>
          <div class="acard__meta">${meta.join(' · ')}</div>
        </div>
      </div>
      <div class="acard__foot">${ACARD_TABS.map(([tab, icon, label]) => `<button class="acard__btn" data-act="${tab}"><i data-lucide="${icon}"></i><span>${label}</span></button>`).join('')}</div>
    </div>`;
  }).join('');
  paintAgentState();
  box.querySelectorAll('[data-context-agent]').forEach((b) => b.addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (b.dataset.contextDesk) openDesk(b.dataset.contextDesk);
  }));
  box.querySelectorAll('.acard__btn').forEach((b) => b.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const card = b.closest('[data-slug]'); if (!card) return;
    if (b.dataset.act === 'chat') return openAgentChat(card.dataset.slug);
    openAgentPage(card.dataset.slug, { tab: b.dataset.act });
  }));
  box.querySelectorAll('.acard--file').forEach((card) => card.addEventListener('click', () => openAgentPage(card.dataset.slug)));
  if (window.lucide) lucide.createIcons();
  roster.forEach(async (a) => {
    let chs = [];
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(a.slug)}/channels`, { headers: { Accept: 'application/json' } });
      if (r.ok) chs = ((await r.json()).channels || []).filter((c) => c.enabled).map((c) => ({ slug: c.channel_slug, label: c.channel_label, ref: c.instance_ref || '' }));
    } catch { /* none */ }
    _agentContacts[a.slug] = chs;
    const el = box.querySelector(`[data-contacts="${CSS.escape(a.slug)}"]`);
    if (!el) return;                       // the grid was drawn again meanwhile; the next draw uses what is kept
    el.hidden = !chs.length; el.innerHTML = contactsHtml(chs);
    if (window.lucide) lucide.createIcons();
  });
}

/* ---- desk chat: composer -> brin (default agent = secretary), rendered in the thread ---- */
const thread = $('#thread');
function bubble(role, html, opts) {
  // Never render an empty bubble (e.g. a blank/empty message): it shows up as a
  // hollow circle in the thread. Skip it entirely.
  if (html == null || !String(html).trim()) return null;
  const d = document.createElement('div');
  const arrive = !!(opts && opts.arrive);
  d.className = 'sn-msg sn-msg--' + role + (arrive ? ' is-arriving' : '');
  d.innerHTML = html;
  thread.appendChild(d);
  if (role === 'user') stampTime(d, new Date().toISOString());
  const sc = thread.parentElement;
  if (arrive) {
    // A message sent or answered now rises into place and the thread glides up
    // to it. The class goes once the animation is done, so a
    // later re-render cannot replay it.
    d.addEventListener('animationend', () => d.classList.remove('is-arriving'), { once: true });
  }
  if (_tailDesk && _tailDesk === activeId) {
    // A message was just sent in this chat: it goes to the top and its reply is
    // written under it. Only the person's own message moves the view.
    if (role === 'user' && arrive) showLastQuestion(); else syncTail();
  } else if (arrive) {
    sc.scrollTo({ top: sc.scrollHeight, behavior: 'smooth' });
  } else {
    sc.scrollTop = sc.scrollHeight;
  }
  return d;
}
// Room for the answer. The chat used to live at its bottom
// edge: a sent message appeared there, its pending reply was a thin strip under
// it, and when the answer came the strip became half a screen of text and the
// thread jumped to follow. Now a sent message glides to the TOP of the view and
// the space under it is kept free, so the reply is written into room that is
// already there and nothing moves but the text. The room is an empty block at
// the end of the thread, exactly as tall as what is missing for the last
// question to sit at the top; it shrinks as the reply grows and is gone once
// the reply fills the view. It exists only in the chat a message was just sent
// in: a chat that is merely opened still shows its end, as before.
var _tailDesk = null;
// Where the last question rests: its lower edge on the middle of the view, so
// what was said before stays in sight above it and half the view is free for
// the answer. A question
// taller than half the view starts at the top instead, so its beginning is read.
function questionOffset(last, usable) {
  return Math.max(0, Math.floor(usable * 0.5 - last.offsetHeight));
}
function lastQuestion() {
  const all = thread.querySelectorAll(':scope > .sn-msg--user');
  return all.length ? all[all.length - 1] : null;
}
function syncTail() {
  let s = thread.querySelector(':scope > .sn-tail');
  const last = _tailDesk && _tailDesk === activeId ? lastQuestion() : null;
  if (!last) { if (s) s.remove(); return; }
  if (!s) {
    s = document.createElement('div');
    s.className = 'sn-tail';
    s.setAttribute('aria-hidden', 'true');
  }
  if (thread.lastElementChild !== s) thread.appendChild(s);
  const sc = thread.parentElement;
  const cs = getComputedStyle(sc);
  const usable = sc.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
  // offsetTop, not a client rect: a message that is still rising into place is
  // drawn a few pixels off where it will rest, and the room must fit where it rests.
  const filled = s.offsetTop - last.offsetTop;
  const h = Math.max(0, Math.floor(usable - questionOffset(last, usable) - filled)) + 'px';
  if (s.style.height !== h) s.style.height = h;
}
function showLastQuestion() {
  syncTail();
  const last = lastQuestion();
  if (!last) return;
  const sc = thread.parentElement;
  const threadTop = thread.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop;
  const rel = last.offsetParent === thread ? last.offsetTop : last.offsetTop - thread.offsetTop;
  const cs = getComputedStyle(sc);
  const padTop = parseFloat(cs.paddingTop) || 0;
  const usable = sc.clientHeight - padTop - (parseFloat(cs.paddingBottom) || 0);
  sc.scrollTo({ top: Math.max(0, threadTop + rel - padTop - questionOffset(last, usable)),
                behavior: 'smooth' });
}
// The reply changes height on its own schedule (text streams in, the console is
// opened, the answer lands); the room follows whatever changed the thread.
if (window.ResizeObserver) new ResizeObserver(() => syncTail()).observe(thread);

// Copy a message: as the text on screen, or as the Markdown
// the agent wrote. The control is added to a message the first time the pointer
// is over it, so drawing a long history costs nothing, and never to a reply still
// in flight. Markdown comes from the stored content kept on the element (_raw);
// a message that has none, the person's own just-sent one, copies as its text.
const COPY_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
  + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
function ensureCopy(el) {
  if (!el || el.querySelector(':scope > .msg-copy')) return;
  if (el.querySelector('.sn-think, .sn-thinking')) return;
  const c = document.createElement('span');
  c.className = 'msg-copy';
  c.innerHTML = `<button type="button" class="msg-copy__btn" title="Copy" aria-label="Copy">${COPY_ICON}</button>`
    + '<span class="msg-copy__menu" hidden>'
    + '<button type="button" data-as="text">Copy as text</button>'
    + '<button type="button" data-as="md">Copy as Markdown</button></span>';
  el.appendChild(c);
}
thread.addEventListener('mouseover', (e) => {
  const m = e.target.closest ? e.target.closest('.sn-msg') : null;
  if (m && thread.contains(m)) ensureCopy(m);
});
thread.addEventListener('click', async (e) => {
  const btn = e.target.closest ? e.target.closest('.msg-copy button') : null;
  const closeAll = (except) => thread.querySelectorAll('.msg-copy__menu:not([hidden])')
    .forEach((m) => { if (m !== except) m.hidden = true; });
  if (!btn) { closeAll(null); return; }
  e.stopPropagation();
  const c = btn.closest('.msg-copy');
  const el = c.parentElement;
  const menu = c.querySelector('.msg-copy__menu');
  if (btn.classList.contains('msg-copy__btn')) {
    closeAll(menu);
    menu.hidden = !menu.hidden;
    return;
  }
  // Closed BEFORE the text is read: innerText leaves hidden elements out, and the
  // menu's own two labels must not end up in what is copied.
  menu.hidden = true;
  const text = (el.innerText || '').trim();
  const raw = typeof el._raw === 'string' ? el._raw.trim() : '';
  const out = btn.dataset.as === 'md' && raw ? raw : text;
  let ok = true;
  try { await navigator.clipboard.writeText(out); } catch { ok = false; }
  c.classList.add(ok ? 'is-done' : 'is-failed');
  c.querySelector('.msg-copy__btn').title = ok ? 'Copied' : 'Could not copy';
  setTimeout(() => {
    c.classList.remove('is-done', 'is-failed');
    c.querySelector('.msg-copy__btn').title = 'Copy';
  }, 1400);
});

// --- chat turns: one per message, kept per chat, never a page-wide lock ---
// Before: while an agent worked, the person could only watch; they could not
// type, stop it, or tell from the tabs which agents were working. The page had ONE
// busy flag, so a turn in any chat disabled the composer in every chat.
//
// Now each message is its own turn with its own request, kept in the list of the
// chat it was sent in. The composer is never disabled. A message sent while that
// chat's Flint agent is still working is passed into that turn; other runtimes
// queue it behind the running turn. Stop ends
// the turn running in the chat on screen, and only that one: the queued messages
// behind it were sent and still run.
var _turns = {};                 // deskId -> [turn], oldest first
const deskTurns = (id) => _turns[id] || [];
const hasTurns = (id) => deskTurns(id).length > 0;
const anyTurns = () => Object.values(_turns).some((l) => l.length > 0);
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The pending reply of a turn: one line of text with light passing over it, the way
// AI chat apps show a turn in flight. Built ONCE per element and
// only its text changes; rebuilding it would restart the shimmer every tick. Seconds
// appear after 10 s, and count from when the turn STARTED, not from when a queued
// message was sent.
function thinkingIndicator(el, t0) {
  let start = t0 || Date.now();
  let label = 'working';
  let iv = null;
  el.innerHTML = '<span class="sn-think"><span class="sn-think__label sn-shimmer"></span><span class="sn-think__time"></span></span>';
  const lab = el.querySelector('.sn-think__label');
  const tim = el.querySelector('.sn-think__time');
  // The light over the label runs on the Web Animations API: base.css stops every
  // CSS animation under prefers-reduced-motion, and this one is the sign that the
  // agent is alive, not decoration. The kit's .sn-shimmer is
  // the gradient; the line is built once, so the light never starts over.
  if (lab.animate) lab.animate([{ backgroundPosition: '100% 0' }, { backgroundPosition: '0% 0' }], { duration: 1800, iterations: Infinity });
  const words = (l) => (l === 'working' ? 'Thinking'
    : l === 'queued' ? 'Queued, answered after the current turn'
    : l === 'stopping' ? 'Stopping'
    : l === 'reconnecting' ? 'Reconnecting'
    : l === 'node-unavailable' ? 'The node is not answering'
    : /^no-liveness: /.test(l) ? 'No sign of life since ' + l.slice(13)
    : /^working: /.test(l) ? doingWords(l.slice(9)) : l);
  const paint = () => {
    const s = Math.floor((Date.now() - start) / 1000);
    const w = words(label);
    if (lab.textContent !== w) lab.textContent = w;
    if (lab.classList) lab.classList.toggle('is-offline', label === 'node-unavailable' || /^no-liveness: /.test(label));
    // A real separator in the text, not only the flex gap: without it the label and
    // the seconds read as one word ("Thinking19s") whenever the gap does not apply,
    // and always when the text is copied. No-break, so it neither
    // collapses nor wraps away from the number.
    tim.textContent = label !== 'queued' && s >= 10 ? ' ' + s + 's' : '';
  };
  paint();
  iv = setInterval(paint, 1000);
  return {
    setLabel(l) { label = l; paint(); },
    restart(t) { start = t || Date.now(); paint(); },
    stop() { if (iv) { clearInterval(iv); iv = null; } },
  };
}

// Draw (or redraw, after a tab switch rebuilt the thread) the pending reply of a turn.
function turnBubble(turn, arrive) {
  if (turn.think) turn.think.stop();
  turn.el = bubble('agent', '<span class="sn-thinking"></span>', { arrive: !!arrive });
  if (turn.state === 'waiting-stopped') {
    turn.think = null;
    if (turn.el) renderOutcome(turn.el, { kind: 'waiting-stopped' });
    return;
  }
  turn.think = turn.el ? thinkingIndicator(turn.el, turn.t0) : null;
  paintTurn(turn);
  renderLiveText(turn);
  renderSteps(turn);
}
function paintTurn(turn) {
  if (!turn.think) return;
  if (turn.state === 'queued') turn.think.setLabel('queued');
  else if (turn.state === 'cancelling') turn.think.setLabel('Cancelling queued message');
  else if (turn.state === 'stopping') turn.think.setLabel('stopping');
  else if (turn.nodeUnavailableSince) {
    const downFor = Date.now() - turn.nodeUnavailableSince;
    turn.think.setLabel(downFor >= NO_SIGN_OF_LIFE_MS
      ? 'no-liveness: ' + new Date(turn.nodeUnavailableSince).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : 'node-unavailable');
  }
  else turn.think.setLabel(turn.action ? 'working: ' + turn.action : 'working');
  paintTurnStop(turn);
}

// Stop sits on the reply it stops. A button by the composer
// said "stop something in this chat"; with several turns in one chat that is not
// the same as "stop this answer". Only the running turn has one: a queued message
// has already gone to the gateway and waits there, and nothing can take it back
// yet. The button goes away with the bubble's content when the turn ends.
function paintTurnStop(turn) {
  if (!turn.el || !turn.el.isConnected) return;
  // kept on the turn: the button lives in the run's head, outside the reply
  let b = turn.stopBtn || turn.el.querySelector('.turn-stop');
  const queued = turn.state === 'queued' || turn.state === 'cancelling';
  const live = turn.state === 'running' || turn.state === 'stopping' || queued;
  if (!live) { if (b) b.remove(); turn.stopBtn = null; return; }
  if (b && b.dataset.action !== (queued ? 'cancel' : 'stop')) { b.remove(); b = null; }
  if (!b) {
    b = document.createElement('button');
    b.type = 'button';
    b.className = 'turn-stop' + (queued ? ' turn-stop--queued' : '');
    b.dataset.action = queued ? 'cancel' : 'stop';
    // The square says stop on its own; the word is the tooltip and the name a
    // screen reader reads.
    b.title = queued ? 'Cancel queued message' : 'Stop';
    b.setAttribute('aria-label', b.title);
    b.innerHTML = queued ? '&times;' : '<span class="turn-stop__sq"></span>';
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      if (queued) cancelQueuedTurn(turn.deskId, turn);
      else stopTurn(turn.deskId);
    });
  }
  // In the head of the agent's run, right of its name and time: a fixed place that neither a reply widening as it is written
  // nor a long tool line can move. Without a head (a phone, the page drawn
  // before chat-authors ran), in the live line itself.
  // A queued message's cancel stays in its bubble: chat-authors.js finds the
  // queue by it there.
  const head = queued ? null : runHeadOf(turn.el);
  const host = head || turn.el.querySelector('.sn-think') || turn.el;
  if (b.parentElement !== host) host.appendChild(b);
  turn.stopBtn = b;
  b.disabled = turn.state === 'stopping' || turn.state === 'cancelling';
  b.title = turn.state === 'stopping' ? 'Stopping...'
    : turn.state === 'cancelling' ? 'Cancelling...'
    : queued ? 'Cancel queued message' : 'Stop this answer';
}

// The head (.sn-who: avatar, name, time) of the run a message belongs to.
function runHeadOf(el) {
  for (let x = el && el.previousElementSibling; x; x = x.previousElementSibling) {
    if (x.classList.contains('sn-who')) return x;
    if (!x.classList.contains('sn-msg--agent')) return null;
  }
  return null;
}
// The chat on screen repaints its turns' Stop buttons (a chat reopened mid-turn
// draws its bubbles again).
function paintChatControls() {
  deskTurns(activeId).forEach(paintTurnStop);
}

async function postChat(deskId, text, ag, turn) {
  const r = await fetch(`/api/desks/${encodeURIComponent(deskId)}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ message: text, agent: ag || undefined,
      client_turn_id: turn.clientTurnId }),
  });
  const data = await r.json().catch(() => ({}));
  return { r, data };
}

// A chat call can fail two ways: an HTTP error from Studio/brin, OR a 200 whose
// content is a gateway error string ("[ERROR] Gateway returned 5xx ...") that
// brin passes through. Both mean the session turn did not complete.
function chatErrored(r, data) {
  if (!r || !r.ok) return true;
  const c = (data && data.content) || '';
  return /^\s*\[ERROR\]/.test(c) || /Gateway returned\s+5\d\d/.test(c);
}

// Friendly copy for a failed turn. Distinguish timeout from overload so the
// message is accurate: a long response hitting a 504/524 is not the same as
// the spawn cap being full (429) or a true backend error (502/503).
function chatErrorMessage(r, data) {
  const status = r ? r.status : 0;
  const detail = String((data && (data.error || data.content)) || '');
  if (status === 504 || status === 524)
    return 'The agent did not respond in time. Please try again.';
  if (status === 429 || /spawn cap reached/i.test(detail))
    return 'All agent slots are busy. Please try again in a moment.';
  if ([502, 503].includes(status) || /Gateway returned\s+5\d\d/i.test(detail))
    return 'Could not reach the agent. Please try again.';
  if (/\[ERROR\]/i.test(detail))
    return 'The agent encountered an error. Please try again.';
  return 'Could not reach the agent. Please try again.';
}

// A turn the CLI could not do comes back as an "answer" that is really an API
// error. The reader gets a calm state, not the raw line; the line stays in the
// event log. Temporary conditions (limit, overload) are waiting, not breakage.
window.snHumanFailure = function (text) {
  const t = String(text || '').trim();
  if (!/^(API Error\b|Prompt is too long|Claude AI usage limit reached|You've hit your (usage )?limit)/i.test(t)) return null;
  if (/\b429\b|rate.?limit|usage limit|hit your/i.test(t)) return 'Waiting: the subscription limit is reached. The agent continues when it resets.';
  if (/\b(529|503|502)\b|overload/i.test(t)) return 'The model is busy right now. Please try again in a moment.';
  if (/too long/i.test(t)) return 'This chat has grown too long for the model. Start a new chat to continue.';
  return 'The agent could not finish this turn. Please try again.';
};

async function fetchHistory(deskId) {
  try {
    const r = await fetch(`/api/desks/${encodeURIComponent(deskId)}/history`, { headers: { Accept: 'application/json' } });
    if (!r.ok) return null;
    return ((await r.json()).messages) || [];
  } catch { return null; }
}
// The answer to one turn, found in the stored history. Turns in one chat run one
// after another, so answers come back in the order the messages were sent: pair
// them in order and take the answer to this turn's own message. "The newest
// assistant message" is wrong as soon as two messages are waiting. The turn's own
// message is the first one with its text at or after the history length taken
// just before it was sent (turn.base); server timestamps are not compared with
// the browser clock, which need not agree with it.
function replyFor(msgs, turn) {
  const waiting = [];
  const answer = new Map();
  msgs.forEach((m, i) => {
    if (m.role === 'user') waiting.push(i);
    else if (m.role === 'assistant' && waiting.length) answer.set(waiting.shift(), m);
  });
  let mine = -1;
  if (turn.base != null) {
    mine = msgs.findIndex((m, i) => i >= turn.base && m.role === 'user' && m.content === turn.text);
  } else {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'user' && msgs[i].content === turn.text) { mine = i; break; }
    }
  }
  if (mine < 0) return null;
  return answer.get(mine) || null;
}

// A turn whose request came back without its answer (the gateway's sync window
// passed, or the connection dropped): the turn goes on in the gateway and its
// answer is stored when it ends. Wait for it for as long as the gateway says this
// chat still has a turn running. The old wait gave up after two minutes and said
// "did not respond in time" over a turn that was still working (the case
// logged on example-node 2026-09-15). Only a chat that has gone quiet for 30 s
// without the answer appearing is given up on.
// The next word about this chat from the live stream (an event about it, any
// agent event, or the stream reopening), or ms at the most: the wait for an
// answer is driven by the bus, not by asking every 3 s.
function nextChatWord(deskId, ms) {
  return new Promise((resolve) => {
    const onEv = (e) => { const ev = e.detail || {}; if (ev.desk === deskId || String(ev.type).startsWith('agent.')) done(); };
    const t = setTimeout(done, ms);
    function done() {
      clearTimeout(t);
      document.removeEventListener('sn:event', onEv);
      document.removeEventListener('sn:resync', done);
      resolve();
    }
    document.addEventListener('sn:event', onEv);
    document.addEventListener('sn:resync', done);
  });
}
async function awaitReply(deskId, turn) {
  let quietSince = null;
  for (;;) {
    // the safety bound only matters if an event is lost
    await nextChatWord(deskId, 30000);
    const msgs = await fetchHistory(deskId);
    const a = msgs && replyFor(msgs, turn);
    if (a) return a;
    const w = _deskWork[deskId];
    // The node says this chat has a turn running: show what it is doing, not
    // the "Reconnecting" this wait may have started under.
    if (w && w.in_progress && turn.think && !turn.nodeUnavailableSince) paintTurn(turn);
    // A recovered node has answered both probes: history is readable and the
    // liveness endpoint says this desk has no turn.  There is then nothing left
    // to wait for.  Do not repost the person's message: the old process may have
    // committed an answer just before it died, and a resend can run it twice.
    if (turn.sawNodeUnavailable && msgs && _deskWorkKnown === true && !w) return { lost: true };
    if (w || _deskWorkKnown === false) { quietSince = null; continue; }
    quietSince = quietSince || Date.now();
    if (Date.now() - quietSince > 30000) return null;
  }
}

async function runTurn(deskId, turn, text, ag) {
  let r, data;
  // Where this turn's message will land in the stored history, taken BEFORE it is
  // sent: the gateway stores it the moment it arrives, so a length read in
  // parallel could already include it.
  const before = await fetchHistory(deskId);
  turn.base = before ? before.length : null;
  try {
    ({ r, data } = await postChat(deskId, text, ag, turn));
  } catch (e) {
    r = null; data = {};
  }
  if (r && r.ok && data && data.stopped) return { kind: 'stopped', content: data.content || '', by: data.stopped_by };
  if (r && r.ok && data && data.cancelled) return { kind: 'cancelled' };
  if (r && r.ok && data && data.failed) return { kind: 'error', message: data.content || 'The agent could not answer.' };
  if (r && r.ok && data && !data.working && !chatErrored(r, data)) {
    if (String(data.content || '').trim()) {
      return { kind: 'ok', content: data.content, specialist: data.specialist };
    }
    // The sync response said ok but carried no text. On a long turn the body
    // can be dropped while the stored answer has already landed in history.
    // Consult it before giving up; fall through to awaitReply if it is not
    // there yet, same path a quiet sync takes.
    const _msgs = await fetchHistory(deskId);
    const _a = _msgs && replyFor(_msgs, turn);
    if (_a && String(_a.content || '').trim()) {
      return { kind: 'ok', content: _a.content, specialist: _a.specialist || data.specialist };
    }
  }
  // No answer in hand. Never resend blindly: the turn may be running or done on
  // the server, and a resend would run the work twice. Wait for its stored answer.
  // The request coming back without an answer is the ordinary end of the sync
  // window on a long turn. With the chat still reported in progress the agent is
  // working, not lost: a two-minute tool call read as "Reconnecting 155s"
  //. The label is for the case where nothing says so.
  const _alive = _deskWork[deskId] && _deskWork[deskId].in_progress;
  if (turn.think && !(data && data.working) && !_alive) turn.think.setLabel('reconnecting');
  const got = await awaitReply(deskId, turn);
  if (got && got.lost) return { kind: 'lost' };
  if (got) return got.stopped ? { kind: 'stopped', content: got.content || '', by: null } : { kind: 'ok', content: got.content || '' };
  if (data && data.working) {
    return { kind: 'late', message: data.content || 'The agent is still working. The response will appear in this chat.' };
  }
  // Never post the message a second time.  A dropped response does not prove the
  // first request failed before it became durable.
  return { kind: 'error', message: chatErrorMessage(r, data) };
}

function stoppedNote(by) {
  const who = !by ? '' : (by === meIdentity.user ? ' by you' : ' by ' + by);
  return `<div class="sn-stopped">Stopped${esc(who)}</div>`;
}
// A done action by its own tool, never by its server's "doing" line: the registry
// words an MCP server once for all its tools ("Setting a reminder"), and a
// listing must not read as setting. The tool's own registry title, else its
// name in words: mcp_reminders_list_reminders -> "List reminders".
function toolDone(name) {
  name = String(name || '').trim();
  const row = _toolLabels[name];
  if (row && row.title) return row.title;
  const m = /^mcp_{1,2}(.+)$/.exec(name);
  let tool = name;
  if (m) {
    const rest = m[1].replace(/__/g, '_');
    const server = Object.keys(_toolLabels).filter((s) => _toolLabels[s].kind === 'mcp' && rest.startsWith(s + '_'))
      .sort((x, y) => y.length - x.length)[0];
    tool = server ? rest.slice(server.length + 1) : rest;
    if (_toolLabels[tool] && _toolLabels[tool].title) return _toolLabels[tool].title;
  }
  const w = tool.replace(/_/g, ' ').trim();
  return w ? w.charAt(0).toUpperCase() + w.slice(1) : 'A tool';
}
// What the agent really did this turn, under its answer, in the registry's words
// (toolDoing): "Set a reminder". A claim with no such line is visible as one.
function usedLine(el, tools) {
  const seen = new Map();
  for (const t of tools) { const w = toolDone(t); seen.set(w, (seen.get(w) || 0) + 1); }
  const d = document.createElement('div');
  d.className = 'sn-used';
  d.textContent = [...seen].map(([w, n]) => (n > 1 ? `${w} ×${n}` : w)).join(' · ');
  el.appendChild(d);
}
// Replies that did not come, per chat, until Try again. The thread is rebuilt from
// the stored history every few seconds, and a failed turn may have stored nothing:
// a failure drawn only once was wiped a moment later with the question above it.
const _failed = {};
function failedTurn(deskId, text, message) {
  const el = bubble('agent', '<span></span>');
  if (!el) return;
  el._retry = () => {
    _failed[deskId] = (_failed[deskId] || []).filter((f) => f.text !== text);
    deliverChat(deskId, text);
  };
  failedReply(el, message);
}
// A reply that did not come says so and offers to send the same text again, on
// the person's press only: an automatic resend could run the work twice.
function failedReply(el, text) {
  el.innerHTML = `<span class="sn-msg-err">${esc(text)}</span>`
    + (el._retry ? ' <button type="button" class="sn-btn sn-btn--ghost sn-retry">Try again</button>' : '');
  const b = el.querySelector('.sn-retry');
  if (b) b.addEventListener('click', () => { b.disabled = true; el._retry(); }, { once: true });
}
function renderOutcome(el, outcome, ag) {
  const fmt = (raw, spec) => (window.snFormatter ? snFormatter.format(raw, { specialist: spec || ag }) : esc(raw));
  if (!outcome) return failedReply(el, 'Could not reach the agent.');
  if (outcome.kind === 'ok') {
    const _hf = snHumanFailure(outcome.content);
    const html = _hf ? `<span class="sn-reconnect">${esc(_hf)}</span>`
      : String(outcome.content || '').trim() ? fmt(outcome.content, outcome.specialist) : '';
    // An empty answer is a reply that did not come: said, never just removed.
    if (!html.trim()) return failedReply(el, 'The agent did not reply.');
    el.innerHTML = html;
    el._raw = outcome.content;
    el.dataset.at = new Date().toISOString();
    stampTime(el);
    // The answer fades in over the line it replaces instead of snapping in.
    el.classList.add('is-settled');
    el.addEventListener('animationend', () => el.classList.remove('is-settled'), { once: true });
  } else if (outcome.kind === 'stopped') {
    // What the agent had said before the stop stays: the person watched that part happen.
    el.innerHTML = (String(outcome.content || '').trim() ? fmt(outcome.content) : '') + stoppedNote(outcome.by);
    el._raw = outcome.content || '';
  } else if (outcome.kind === 'late') {
    el.innerHTML = `<span class="sn-reconnect">${esc(outcome.message)}</span>`;
  } else if (outcome.kind === 'waiting-stopped') {
    el.innerHTML = '<span class="sn-reconnect">Stopped waiting; if the turn is still running, its answer will appear here.</span>';
  } else if (outcome.kind === 'lost') {
    failedReply(el, 'This turn was lost when the node restarted.');
  } else {
    failedReply(el, String(outcome.message || '').replace(/\s*Please try again\.?$/, ''));
  }
}

function removeCancelledTurn(deskId, turn) {
  if (turn.cancelled) return;
  turn.cancelled = true;
  const list = deskTurns(deskId);
  const i = list.indexOf(turn);
  if (i >= 0) list.splice(i, 1);
  // Gateway removed this queued user message from stored history. Later turns
  // took their history offsets before that deletion; shift those offsets too,
  // or a second message with the same text can no longer find its own reply.
  if (turn.base != null) for (const waiting of list) {
    if (waiting.base != null && waiting.base > turn.base) waiting.base--;
  }
  if (!list.length) delete _turns[deskId];
  if (turn.think) turn.think.stop();
  if (turn.el && turn.el.isConnected) turn.el.remove();
  if (turn.userEl && turn.userEl.isConnected) turn.userEl.remove();
  delete _histLen[deskId];
  paintChatControls();
  paintDeskWork();
  if (deskId === activeId) loadHistory(deskId);
}

function settleTurn(deskId, turn, outcome, ag) {
  if (turn.cancelled) return;
  if (outcome && outcome.kind === 'cancelled') {
    removeCancelledTurn(deskId, turn);
    return;
  }
  if (turn.steerNotes) {
    fetch(`/api/desks/${encodeURIComponent(deskId)}/activity`,
      { headers: { Accept: 'application/json' } })
      .then((r) => r.ok ? r.json() : null)
      .then((j) => updateSteerNotes(turn, j && j.steers))
      .catch(() => {});
  }
  const list = deskTurns(deskId);
  const i = list.indexOf(turn);
  if (i >= 0) list.splice(i, 1);
  if (!list.length) delete _turns[deskId];
  if (turn.think) turn.think.stop();
  // Painted only if its bubble is still on screen. In another chat, or after the
  // thread was rebuilt, the stored history already carries the outcome.
  const failed = !outcome || ['error', 'lost'].includes(outcome.kind)
    || (outcome.kind === 'ok' && !String(outcome.content || '').trim());
  if (failed) {
    const message = !outcome ? 'Could not reach the agent.'
      : outcome.kind === 'lost' ? 'This turn was lost when the node restarted.'
      : outcome.kind === 'ok' ? 'The agent did not reply.'
      : String(outcome.message || '').replace(/\s*Please try again\.?$/, '');
    (_failed[deskId] = _failed[deskId] || []).push({ text: turn.text, message });
  }
  if (turn.el && turn.el.isConnected) {
    turn.el._retry = () => {
      _failed[deskId] = (_failed[deskId] || []).filter((f) => f.text !== turn.text);
      deliverChat(deskId, turn.text);
    };
    renderOutcome(turn.el, outcome, ag);
  }
  // The relay poll compares lengths; let it resync this chat once it is idle.
  delete _histLen[deskId];
  // The next turn starts its step log from zero, on a new cursor.
  _stepSeq[deskId] = 0;
  syncDeskLive();
  paintChatControls();
  paintDeskWork();
  if (deskId === activeId && !hasTurns(deskId)) {
    if (_tailDesk !== deskId) thread.parentElement.scrollTop = thread.parentElement.scrollHeight;
    loadSession(deskId);
    loadContextStatuses(deskId);
  }
}

// True from the moment a send reads the composer until it has emptied it. The
// sign-in check in between is a network call; on a slow line a second Enter or
// click landed inside it, read the same text, and the message went out twice.
let _sendReading = false;
async function sendChat() {
  // Not while a file is still on its way: the message names only stored files,
  // and sending now would silently leave that one out.
  if (cmpUploading()) return;
  if (_sendReading) return;
  // The message is the typed text, every pasted chip, and one line per stored
  // file. Files alone are a message too: their lines are text.
  const { message: text } = cmpCompose();
  if (!activeId || !text) return;
  const deskId = activeId;
  _sendReading = true;
  try {
    // Signed out: the text stays in the composer and the wall says why.
    if (_authExpired || !(await authAlive())) { authExpired(); return; }
    cmpReset();
    draftForget(deskId);
  } finally { _sendReading = false; }
  return deliverChat(deskId, text);
}
async function deliverChat(deskId, text) {
  // A chat opened by its link is on screen before the list of chats has loaded, and
  // until it has, the chat's agent is unknown. Sent then, the message went out with
  // no agent, and brin gave it to the default agent in a session of its own, with no
  // turn events and no lease (seen on the earlier change stand). Wait for the list.
  if (!desks.some((d) => d.id === deskId) && typeof _desksReady !== 'undefined') await _desksReady;
  const ag = deskAgent(desks.find((d) => d.id === deskId));
  const running = deskTurns(deskId).find((t) => t.state === 'running');
  if (running && (running.canSteer ?? _deskWork[deskId]?.can_steer ?? (ag === 'agent-flint'))) {
    _tailDesk = deskId;
    const el = bubble('user', userHtml(text, deskId), { arrive: true });
    const note = document.createElement('div');
    note.className = 'sn-steer-status';
    note.textContent = 'Sending to agent…';
    if (el) el.appendChild(note);
    try {
      const r = await fetch(`/api/desks/${encodeURIComponent(deskId)}/steer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ text }),
      });
      const out = await r.json();
      if (out.status === 'accepted' || out.status === 'delivered' || out.status === 'pending') {
        note.textContent = out.status === 'delivered' ? 'Passed to agent'
          : out.status === 'pending' ? 'Waiting for agent' : 'Accepted';
        if (out.request_id) (running.steerNotes = running.steerNotes || {})[out.request_id] = note;
      } else {
        note.textContent = out.status === 'too_late' ? 'Too late for this turn; send again' : 'Could not pass to agent; send again';
      }
    } catch {
      note.textContent = 'Could not pass to agent; send again';
    }
    return;
  }
  const turn = {
    deskId, clientTurnId: window.crypto.randomUUID(),
    text, sentAt: Date.now(), t0: Date.now(), action: null, el: null, think: null,
    state: hasTurns(deskId) ? 'queued' : 'running',
  };
  (_turns[deskId] = _turns[deskId] || []).push(turn);
  // Both rise into place: the message first, its pending reply a beat after.
  _tailDesk = deskId;
  turn.userEl = bubble('user', userHtml(text, deskId), { arrive: true });
  turnBubble(turn, true);
  syncDeskLive();
  paintChatControls();
  paintDeskWork();
  let outcome = null;
  try {
    outcome = await runTurn(deskId, turn, text, ag);
  } finally {
    settleTurn(deskId, turn, outcome, ag);
    // Signed out between the check above and the send: keep the text as the
    // chat's draft, so it is back in the composer after signing in.
    if (_authExpired && (!outcome || outcome.kind !== 'ok')) draftSaveText(deskId, text);
  }
}

async function cancelQueuedTurn(deskId, turn) {
  if (turn.state !== 'queued' || !turn.clientTurnId) return;
  turn.state = 'cancelling';
  paintTurn(turn);
  const status = await requestQueuedCancellation(deskId, turn.clientTurnId);
  if (status === 'cancelled') {
    removeCancelledTurn(deskId, turn);
    return;
  }
  if (status === 'too_late') {
    turn.state = 'running';
    paintTurn(turn);
    return;
  }
  if (!turn.cancelled && turn.state === 'cancelling') {
    turn.state = 'queued';
    paintTurn(turn);
  }
}

async function requestQueuedCancellation(deskId, clientTurnId) {
  try {
    // The click can race Brin's initial preparation. The gateway records a
    // short-lived cancellation request and confirms it once the turn arrives.
    for (let attempt = 0; attempt < 20; attempt++) {
      const r = await fetch(`/api/desks/${encodeURIComponent(deskId)}/cancel-queued`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ client_turn_id: clientTurnId }),
      });
      const out = await r.json();
      if (!r.ok) return 'unavailable';
      if (out.status !== 'pending') return out.status;
      await _sleep(500);
    }
  } catch {}
  return 'unavailable';
}

async function cancelQueuedHistoryMessage(deskId, clientTurnId, button) {
  button.disabled = true;
  button.textContent = 'Cancelling…';
  const status = await requestQueuedCancellation(deskId, clientTurnId);
  if (status === 'cancelled') {
    delete _histLen[deskId];
    if (deskId === activeId) loadHistory(deskId);
  } else if (status === 'too_late') {
    button.remove();
  } else {
    button.disabled = false;
    button.textContent = 'Cancel queued';
  }
}

function paintQueuedHistoryControls(deskId) {
  if (deskId !== activeId) return;
  const local = new Set(deskTurns(deskId).map((t) => t.clientTurnId));
  const pending = new Map(((_deskWork[deskId] || {}).queued_turns || [])
    .filter((item) => !local.has(item.client_turn_id))
    .map((item) => [String(item.message_id), item.client_turn_id]));
  thread.querySelectorAll('.sn-msg--user[data-message-id]').forEach((row) => {
    const clientTurnId = pending.get(row.dataset.messageId);
    let button = row.querySelector('.queue-cancel');
    if (!clientTurnId) { if (button) button.remove(); return; }
    if (button) return;
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'queue-cancel';
    button.textContent = 'Cancel queued';
    button.addEventListener('click', () => cancelQueuedHistoryMessage(deskId, clientTurnId, button));
    row.appendChild(button);
  });
}

// Stop ends the browser's wait immediately. It also asks the gateway to interrupt
// the agent in place, but that is best-effort: the node may be the unavailable
// thing. A later stored answer still replaces the local notice.
async function stopTurn(deskId) {
  const running = deskTurns(deskId).find((t) => t.state === 'running');
  if (!running) return;
  // Stop is a local promise first.  The node may be exactly what is unavailable,
  // so the bubble must settle before (and regardless of) the server-side attempt.
  running.state = 'waiting-stopped';
  running.stoppedAt = Date.now();
  if (running.think) running.think.stop();
  if (running.el && running.el.isConnected) renderOutcome(running.el, { kind: 'waiting-stopped' });
  paintChatControls();
  // The bubble already says the wait ended. If the node did not take the stop,
  // it says so: the turn goes on and its answer still lands here.
  const notStopped = () => {
    if (running.el && running.el.isConnected) renderOutcome(running.el, { kind: 'late',
      message: 'The agent did not confirm the stop. If it is still working, its answer will appear here.' });
  };
  try {
    const r = await fetch(`/api/desks/${encodeURIComponent(deskId)}/abort`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ agent: deskAgent(desks.find((d) => d.id === deskId)) || undefined }),
    });
    // ok:false with a 200 is brin finding no running turn to interrupt (it had
    // just ended, or the stop raced it): the page must not claim it stopped one.
    const out = r.ok ? await r.json().catch(() => null) : null;
    if (!r.ok || !out || out.ok === false) notStopped();
  } catch { notStopped(); }
}

$('#orchSend').addEventListener('click', () => sendChat());

// Which chats have a turn running, from the gateway itself: one poll for every tab
// The agent's busy ring in the sidebar is its lease; this is the turn, per
// chat, which is what "is it still working on what I asked" means. A failed poll
// leaves the marks as they were rather than clearing them.
var _deskWork = {};
var _deskWorkKnown = null;
const NODE_UNAVAILABLE_MS = 5000;
const NO_SIGN_OF_LIFE_MS = 60000;
var _deskWorkLastOkAt = Date.now();
var _deskWorkFailedSince = null;
var _nodeUnavailableSince = null;
async function loadDeskWork() {
  try {
    const r = await fetch('/api/desks/in-progress', { headers: { Accept: 'application/json' } });
    const j = r.ok ? await r.json() : { desks: null };
    if (!Array.isArray(j.desks)) throw new Error('node liveness unavailable');
    const next = {};
    for (const d of j.desks) next[d.desk_id] = d;
    _deskWork = next;
    _deskWorkKnown = true;
    syncDetachedTurn();
    syncDeskLive();
    _deskWorkLastOkAt = Date.now();
    _deskWorkFailedSince = null;
    _nodeUnavailableSince = null;
    for (const turns of Object.values(_turns)) for (const turn of turns) {
      if (!turn.nodeUnavailableSince) continue;
      turn.nodeUnavailableSince = null;
      if (turn.state === 'running') paintTurn(turn);
    }
    for (const [id, w] of Object.entries(_deskWork)) {
      const started = deskTurns(id).find((t) => t.state === 'queued'
        && t.clientTurnId && t.clientTurnId === w.running_turn_id);
      if (started) {
        started.state = 'running';
        started.t0 = Date.now();
        if (started.think) started.think.restart(started.t0);
        paintTurn(started);
      }
      // The page said "stopped waiting" for a turn the node is still working on
      // (the stop did not land, or the person wrote again and the agent went on).
      // Several seconds after the press, a turn the node still reports running is
      // shown as running: the notice would otherwise stay over live work.
      const stale = deskTurns(id).find((t) => t.state === 'waiting-stopped' && t.clientTurnId
        && t.clientTurnId === w.running_turn_id && Date.now() - (t.stoppedAt || 0) > 6000);
      if (stale && stale.el && stale.el.isConnected) {
        stale.state = 'running';
        stale.action = null;
        stale.t0 = Date.now() - ((w.elapsed || 0) * 1000);
        stale.el.innerHTML = '<span class="sn-thinking"></span>';
        stale.think = thinkingIndicator(stale.el, stale.t0);
        paintTurn(stale);
        renderLiveText(stale);
        renderSteps(stale);
      }
      const running = deskTurns(id).find((t) => t.state === 'running');
      const act = w.action ? (w.action + (w.detail ? ': ' + w.detail : '')) : null;
      if (running && act !== running.action) { running.action = act; paintTurn(running); }
    }
    paintQueuedHistoryControls(activeId);
    paintDeskWork();
  } catch {
    _deskWorkKnown = false;
    _deskWorkFailedSince = _deskWorkFailedSince || Date.now();
    if (Date.now() - _deskWorkFailedSince < NODE_UNAVAILABLE_MS) return;
    _nodeUnavailableSince = _nodeUnavailableSince || _deskWorkLastOkAt || _deskWorkFailedSince;
    for (const turns of Object.values(_turns)) for (const turn of turns) {
      if (!['running', 'stopping', 'waiting-stopped'].includes(turn.state)) continue;
      turn.nodeUnavailableSince = _nodeUnavailableSince;
      turn.sawNodeUnavailable = true;
      paintTurn(turn);
    }
    paintDeskWork();
  }
}
// A turn this page did not start -- the tab was reloaded, the chat is open in a
// second window, or a colleague's relay woke the agent -- has no turn object, so
// the chat showed nothing while the agent worked and the person could not tell a
// long turn from a dead one. The poll above knows it is running; give it the same
// pending bubble, and drop it the moment the poll says the turn is over, so the
// history poll (which skips while turns exist) can bring the answer in.
// The live steps of the turn in flight. The gateway keeps them per
// session and hands out what is newer than our cursor; we keep the last few in
// the bubble. Only what the agent SAID and which tool it called with what: tool
// output is not carried, it can be enormous and can hold a secret.
var _stepSeq = {};
const STEPS_KEPT = 40;
// A reply in flight is three things, in this order, and nothing floats:
//   1. what the agent has said so far (absent until it says something);
//   2. ONE line saying what it is doing now, the one with light passing over it,
//      with a small chevron after it once there are commands to show;
//   3. the console, closed by default. The chevron opens it under that line: a
//      dark panel of fixed height that scrolls inside itself, and closes it again.
// The first attempt kept a one-line console and pinned the
// "doing now" line over the composer; it floated over the reply, covered Stop
// and flickered when text arrived. This shape replaced it.
function renderLiveText(turn) {
  if (!turn || !turn.el || !turn.el.isConnected) return;
  let box = turn.el.querySelector('.sn-live-text');
  if (!turn.liveText) { if (box) box.remove(); return; }
  if (!box) {
    box = document.createElement('div');
    box.className = 'sn-live-text';
    turn.el.insertBefore(box, turn.el.firstChild);
  }
  // The agent's phrases arrive with the tool calls between them taken out, and
  // what is left where each one sat is a run of blank lines. One blank line
  // separates two phrases; more is empty space the reader scrolls through.
  const text = String(turn.liveText).replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n').replace(/^\n+|\s+$/g, '');
  // Written only when it changed: rewriting the same text every second is what
  // made the reply blink.
  if (box.textContent !== text) box.textContent = text;
}
// What the person reads while a turn runs: the last few tools as plain words
//. The command itself stays in the console, where only
// someone who opens it sees it: it can carry a path or the name of a secret.
// The words come from the tools registry (sn_core.tools: "doing", else "title"),
// edited in the panel, not from a list in this page. A runtime names an MCP
// server's tool mcp_<server>_<tool> (Flint) or mcp__<server>__<tool> (Claude
// Code): the tool's own row first, then its server's. A tool the registry does
// not know reads as its name in words.
let _toolLabels = {};
fetch('/api/tools/labels', { headers: { Accept: 'application/json' } })
  .then((r) => (r.ok ? r.json() : {})).then((j) => { _toolLabels = j.labels || {}; }).catch(() => {});
function toolDoing(name) {
  name = String(name || '').trim();
  const say = (row) => row && (row.doing || row.title);
  if (say(_toolLabels[name])) return say(_toolLabels[name]);
  const m = /^mcp_{1,2}(.+)$/.exec(name);
  if (m) {
    const rest = m[1].replace(/__/g, '_');
    const server = Object.keys(_toolLabels).filter((s) => _toolLabels[s].kind === 'mcp' && rest.startsWith(s + '_'))
      .sort((x, y) => y.length - x.length)[0];
    if (server) return say(_toolLabels[rest.slice(server.length + 1)]) || say(_toolLabels[server]);
  }
  const w = name.replace(/^mcp_{1,2}[a-z0-9-]+?_{1,2}/, '').replace(/_/g, ' ');
  return w ? w.charAt(0).toUpperCase() + w.slice(1) : 'Working';
}
// "tool: detail" as the line shows it: the words, then the detail, an address
// by its site.
function doingWords(action) {
  const i = String(action).indexOf(':');
  const name = i < 0 ? action : action.slice(0, i);
  let det = i < 0 ? '' : action.slice(i + 1).trim();
  if (/^https?:\/\//.test(det)) { try { det = new URL(det).hostname.replace(/^www\./, ''); } catch {} }
  return toolDoing(name) + (det ? ' · ' + det : '');
}
function plainStep(text) {
  return toolDoing(String(text || '').split(':')[0].replace(/\s*×\d+$/, ''));
}
function renderRecent(turn, steps, anchor) {
  const lines = [];
  for (const st of steps.slice(-8)) {
    const w = (typeof st.desc === 'string' && st.desc) ? st.desc : plainStep(st.text);
    const last = lines[lines.length - 1];
    if (last && last.text === w) last.count += 1; else lines.push({ text: w, count: 1 });
  }
  const html = lines.slice(-3).map((l) =>
    `<div class="sn-recent__i">${esc(l.text)}`
      + (l.count > 1 ? ` <span class="sn-recent__n">×${l.count}</span>` : '') + '</div>').join('');
  let rc = turn.el.querySelector('.sn-recent');
  if (!rc) {
    rc = document.createElement('div');
    rc.className = 'sn-recent';
    anchor.insertAdjacentElement('afterend', rc);
  }
  if (rc._html !== html) { rc.innerHTML = html; rc._html = html; }
}
function consoleOpen() {
  try { return localStorage.getItem('sn.console.open') === '1'; } catch { return false; }
}
function renderSteps(turn) {
  if (!turn || !turn.el || !turn.el.isConnected) return;
  // The console shows commands: paths, names of secrets. Admins only; everyone
  // else has the plain-words lines.
  const items = PANEL_ROLES.includes(meIdentity.role) ? (turn.steps || []).filter((s) => s.kind === 'tool') : [];
  const now = turn.el.querySelector('.sn-think');
  let box = turn.el.querySelector('.sn-steps');
  let tgl = now ? now.querySelector('.sn-console__tgl') : null;
  if (!items.length || !now) {
    if (box) box.remove();
    if (tgl) tgl.remove();
    const rc0 = turn.el.querySelector('.sn-recent');
    if (rc0) rc0.remove();
    if (now) now.classList.remove('has-console');
    return;
  }
  if (!box) {
    box = document.createElement('div');
    box.className = 'sn-steps' + (consoleOpen() ? ' is-open' : '');
    now.insertAdjacentElement('afterend', box);
  }
  if (!tgl) {
    tgl = document.createElement('button');
    tgl.type = 'button';
    tgl.className = 'sn-console__tgl';
    const label = (open) => {
      tgl.classList.toggle('is-open', open);
      tgl.title = open ? 'Hide console' : 'Show console';
      tgl.setAttribute('aria-label', tgl.title);
      tgl.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    tgl.addEventListener('click', (e) => {
      e.stopPropagation();
      const b = turn.el && turn.el.querySelector('.sn-steps');
      if (!b) return;
      const open = !b.classList.contains('is-open');
      b.classList.toggle('is-open', open);
      // The reader's choice, kept for the next turn and the other chats.
      try { localStorage.setItem('sn.console.open', open ? '1' : '0'); } catch {}
      label(open);
      b.scrollTop = b.scrollHeight;
    });
    label(box.classList.contains('is-open'));
    now.appendChild(tgl);
    // The whole line opens the console, not only the chevron at its end: it is
    // the bottom edge of the reply and the reader presses the edge.
    now.classList.add('has-console');
    if (!now._consoleClick) {
      now._consoleClick = true;
      now.addEventListener('click', () => {
        const c = now.querySelector('.sn-console__tgl');
        if (c) c.click();
      });
    }
  }
  const grouped = new Map();
  for (const step of items.slice(-STEPS_KEPT)) {
    const repeated = grouped.get(step.text);
    grouped.delete(step.text);
    grouped.set(step.text, { text: step.text, count: (repeated?.count || 0) + 1 });
  }
  const html = [...grouped.values()].map((s) =>
    `<div class="sn-steps__i sn-steps__i--tool">${esc(s.text)}`
      + (s.count > 1 ? ` <span class="sn-steps__count">×${s.count}</span>` : '') + '</div>'
  ).join('');
  if (box._html !== html) {
    box.innerHTML = html;
    box._html = html;
    // The newest line is the one being read, so the panel follows it down.
    box.scrollTop = box.scrollHeight;
  }
  renderRecent(turn, items, box);
}
function updateSteerNotes(turn, steers) {
  if (!Array.isArray(steers) || !turn.steerNotes) return;
  for (const item of steers) {
    const note = turn.steerNotes[item.request_id];
    if (note) note.textContent = item.status === 'delivered' ? 'Passed to agent'
      : item.status === 'too_late' ? 'Too late for this turn; send again'
      : item.status === 'submitted' ? 'Waiting for agent' : 'Accepted';
  }
}
// The open chat's turn arrives pushed: the node sends its whole live
// state (text so far, the current tool, the steps of the turn, steer statuses) on
// every change, through /api/desks/:id/live. This replaced a 400 ms poll, which a
// hidden or occluded tab stopped, so the reply sat on "Thinking" until it landed.
// One stream, for the chat on screen; opening another chat moves it.
let _live = { id: null, es: null };
// Open only while something works in the chat on screen: the person's own turn,
// or an agent the node reports at work there. On plain http a browser keeps six
// connections to one host, and an idle tab holding this one besides the page's
// live stream left the next tabs without theirs. The live stream says when an
// agent starts (agent.turn_started) and the turn's end closes it again.
function syncDeskLive() {
  const id = activeId;
  const busy = id && (hasTurns(id) || (_deskWork[id] && _deskWork[id].in_progress));
  watchDeskLive(busy ? id : null);
}
// The chat's turn rides the page's one WebSocket (liveSocket below): the page
// says which chat it follows, and the socket carries that chat's turn as it moves.
function watchDeskLive(id) {
  if (_live.id === id) return;
  _live = { id, es: null };
  liveSend({ watch: id || null });
}
const _peerTurn = new Set();   // chats whose turn another tab started, read once
function onDeskLive(id, j) {
  if (id !== activeId) return;
  // a colleague answering in this chat: its pending bubble shows the turn
  if (j.colleague) { if (window.snColleagueLive) window.snColleagueLive(j.agent, j); return; }
  const turn = deskTurns(id).find((t) => t.state === 'running' || t.state === 'stopping');
  if (!turn) {
    // The turn was started in another tab or on another device: this tab has no
    // turn of its own, and the person's message reaches the history about a second
    // after turn_started, so the read at turn_started found nothing and the message
    // showed only at the end (tester, round 3: 3 to 8 s). The gateway's first push
    // comes after it wrote the message: read the history once then.
    if (j.in_progress && !_peerTurn.has(id)) { _peerTurn.add(id); soon('chat', refreshOpenChat); }
    return;
  }
  updateSteerNotes(turn, j.steers);
  if (!j.in_progress) return;
  if (typeof j.text === 'string' && j.text !== turn.liveText) {
    turn.liveText = j.text;
    renderLiveText(turn);
  }
  if (typeof j.can_steer === 'boolean') turn.canSteer = j.can_steer;
  // the push carries the turn's whole ring of steps, not the new ones only
  if (Array.isArray(j.steps)) {
    turn.steps = j.steps.slice(-STEPS_KEPT);
    renderSteps(turn);
  }
  const act = j.action ? (j.action + (j.detail ? ': ' + j.detail : '')) : null;
  if (act !== turn.action) { turn.action = act; paintTurn(turn); }
}

var _detached = {};
function dropDetached(id) {
  const t = _detached[id];
  if (!t) return;
  delete _detached[id];
  const list = deskTurns(id);
  const i = list.indexOf(t);
  if (i >= 0) list.splice(i, 1);
  if (!list.length) delete _turns[id];
  if (t.think) t.think.stop();
  if (t.el && t.el.isConnected) t.el.remove();
  delete _histLen[id];
  paintChatControls();
  if (id === activeId) loadHistory(id);
}
function syncDetachedTurn() {
  for (const id of Object.keys(_detached)) {
    const w = _deskWork[id];
    const mine = deskTurns(id).some((t) => !t.detached && (t.state === 'running' || t.state === 'queued'));
    if (id !== activeId || !w || !w.in_progress || mine) dropDetached(id);
  }
  const id = activeId;
  if (!id || _detached[id]) return;
  const w = _deskWork[id];
  if (!w || !w.in_progress) return;
  if (deskTurns(id).length) return;   // this page is already showing its own turn
  const turn = {
    // deskId: Stop calls stopTurn(turn.deskId), and a turn this page picked up
    // (started in another tab, or before a reload) had none, so its Stop did
    // nothing (found 2026-10-30; production has the same gap)
    deskId: id,
    text: '', sentAt: Date.now(), t0: Date.now() - (w.elapsed || 0) * 1000,
    action: null, el: null, think: null, state: 'running', detached: true,
  };
  _detached[id] = turn;
  (_turns[id] = _turns[id] || []).push(turn);
  turnBubble(turn, true);
  paintChatControls();
}

function deskIsWorking(id) { return _nodeUnavailableSince ? false : (!!_deskWork[id] || hasTurns(id)); }
function paintDeskWork() {
  // the chat header's "working" reads the same state (chat-shell.js)
  document.dispatchEvent(new CustomEvent('sn-deskwork'));
  document.querySelectorAll('#tabs .tab[data-id], .ap__chat[data-open]').forEach((el) => {
    const id = el.dataset.id || el.dataset.open;
    const on = deskIsWorking(id);
    el.classList.toggle('is-working', on);
    const w = _deskWork[id];
    const queued = (w && w.queued) || Math.max(0, deskTurns(id).length - 1);
    el.dataset.working = on ? ('working' + (queued ? ' · ' + queued + ' queued' : '')) : '';
  });
}
loadDeskWork();
// no poll: a turn starting or ending (agent.*) reads it again; so does the stream's (re)open
// A hidden page does not poll; coming back to it must not show marks from before.
// A PWA suspended by the OS returns with a dead or zombie WebSocket (readyState=OPEN
// but no packets flow), and the onclose backoff's setTimeout(open, wait) is frozen with
// the tab, so the reconnect queued minutes ago is still waiting. Without the force
// reopen below, sn:resync never fires, awaitReply stays blocked, settleTurn never runs,
// and the "Thinking Xs" counter keeps ticking against stale state.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  loadDeskWork();
  // a socket that has been heard from lately is fine: do not drop it for every tab switch
  if (_liveReopen && !_liveHealthy()) _liveReopen();
});
window.addEventListener('pageshow', () => { if (_liveReopen && !_liveHealthy()) _liveReopen(); });
window.addEventListener('online', () => { if (_liveReopen) _liveReopen(); });

/* ---- composer: a field that grows; long pastes and files as chips ----
   Pasted text goes into the message as a labelled block. Files and images are
   STORED: uploaded when added, into this desk's part of the
   node's attachment store, and named in the message by one line each, which the
   thread renders as chips that open the file. The agent sees the names for now;
   opening the files is deferred work. */
const CMP_PASTE_CHARS = 200;             // a paste longer than this becomes a chip
const CMP_PASTE_LINES = 6;               // ...and so does one with more lines than this
const CMP_MAX_FILE = 25 * 1024 * 1024;   // core-api's per-file limit, refused on the chip first
let _cmpItems = [];                      // {id, kind: 'paste'|'file'|'image', text|name,size,url, bad}
let _cmpSeq = 0;
const _cmpTa = $('#orch');

// Each chat keeps its own draft. The composer is one field for every chat,
// so text typed in one chat followed you into the next, and a file attached in one
// chat sat in the tray of another, where Send would have sent it. The draft is the
// text AND the tray. The text also survives a reload (localStorage, one entry per
// chat); the tray does not, because a chip is a live upload and an object URL that
// belong to this page.
const DRAFT_KEY = 'sn-studio-drafts';
const _drafts = {};
function draftsStored() {
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || '{}') || {}; } catch (e) { return {}; }
}
function draftSaveText(id, text) {
  if (!id) return;
  try {
    const all = draftsStored();
    if (text && text.trim()) all[id] = text; else delete all[id];
    localStorage.setItem(DRAFT_KEY, JSON.stringify(all));
  } catch (e) {}
}
function draftStash(id) {
  if (!id) return;
  _drafts[id] = { text: _cmpTa.value, items: _cmpItems };
  draftSaveText(id, _cmpTa.value);
}
function draftRestore(id) {
  const d = id ? _drafts[id] : null;
  _cmpTa.value = d ? d.text : ((id && draftsStored()[id]) || '');
  _cmpItems = d ? d.items : [];
  cmpRender();
  cmpGrow();
  cmpSyncPad();
}
function draftForget(id) {
  delete _drafts[id];
  draftSaveText(id, '');
}

function cmpFmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return Math.round(b / 1024) + ' KB';
  return (b / (1024 * 1024)).toFixed(1) + ' MB';
}
// Grow upward with the text, to 40% of the window, then scroll inside.
function cmpGrow() {
  // Not on screen (another view is open, or a chat is being opened from one): there is
  // nothing to measure, and the height came out 0, a field cut to its padding with half a
  // line in it until the next keystroke. Its own size until shown.
  if (!_cmpTa.offsetParent) { _cmpTa.style.height = ''; return; }
  _cmpTa.style.height = 'auto';
  const max = Math.round(window.innerHeight * 0.4);
  _cmpTa.style.height = Math.min(_cmpTa.scrollHeight, max) + 'px';
  _cmpTa.style.overflowY = _cmpTa.scrollHeight > max ? 'auto' : 'hidden';
}
// Grown again once the field is laid out, and when its width changes (the lines wrap anew).
{
  let w0 = 0;
  new ResizeObserver(() => { const w = _cmpTa.clientWidth; if (!w) { w0 = 0; return; } if (w !== w0) { w0 = w; cmpGrow(); } }).observe(_cmpTa);
}
// The thread's bottom padding follows the composer's real height, so the last
// message is never hidden under a composer that has grown. The bottom offset is read, not
// assumed: on a phone it includes the gesture-bar inset.
function cmpSyncPad() {
  const sc = document.querySelector('.thread__scroll'), cmp = $('#composer');
  if (sc) sc.style.paddingBottom = (cmp.offsetHeight + parseFloat(getComputedStyle(cmp).bottom) + 20) + 'px';
  // The same clearance, for what must stay visible just above the composer.
  if (sc) sc.style.setProperty('--cmp-clear', sc.style.paddingBottom);
}
function cmpRender() {
  const tray = $('#cmpTray');
  tray.hidden = _cmpItems.length === 0;
  tray.innerHTML = _cmpItems.map((it) => {
    const icon = it.kind === 'paste' ? 'clipboard-list' : (it.bad ? 'alert-circle' : 'file');
    const lead = it.kind === 'image' && it.url && !it.bad
      ? `<img class="cmp-chip__img" src="${it.url}" alt="">`
      : `<span class="cmp-chip__ic"><i data-lucide="${icon}"></i></span>`;
    const title = it.kind === 'paste' ? 'Pasted text' : esc(it.name);
    const meta = it.bad ? esc(it.bad)
      : it.uploading ? 'uploading...'
      : (it.kind === 'paste' ? it.text.length.toLocaleString() + ' chars' : cmpFmtSize(it.size));
    const tip = it.kind === 'paste' ? 'Click to read or edit' : title;
    return `<div class="cmp-chip${it.bad ? ' is-bad' : ''}" data-id="${it.id}" data-kind="${it.kind}" title="${tip}">`
      + `${lead}<span class="cmp-chip__t"><b>${title}</b><small>${meta}</small></span>`
      + `<button class="cmp-chip__x" data-x="${it.id}" type="button" title="Remove">&times;</button></div>`;
  }).join('');
  tray.querySelectorAll('[data-x]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const gone = _cmpItems.find((x) => x.id === b.dataset.x);
    if (gone && gone.url) URL.revokeObjectURL(gone.url);
    // A stored file removed before sending leaves the store now, not at the
    // desk's deletion; one still uploading is taken out when it lands.
    if (gone) cmpDeleteStored(gone);
    _cmpItems = _cmpItems.filter((x) => x.id !== b.dataset.x);
    if ($('#cmpPeek').dataset.id === b.dataset.x) cmpPeekClose();
    cmpRender();
  }));
  tray.querySelectorAll('[data-kind="paste"]').forEach((c) => c.addEventListener('click', () => cmpPeek(c.dataset.id)));
  if (window.lucide) lucide.createIcons();
  cmpSyncPad();
}
// Open a pasted chip to read or edit it; emptying it removes the chip.
function cmpPeek(id) {
  const it = _cmpItems.find((x) => x.id === id);
  if (!it || it.kind !== 'paste') return;
  const box = $('#cmpPeek');
  box.dataset.id = id;
  box.hidden = false;
  box.innerHTML = '<div class="cmp-peek__bar"><span>Pasted text</span><button type="button" data-done>Done</button></div>'
    + '<textarea class="cmp-peek__ta"></textarea>';
  const ta = box.querySelector('textarea');
  ta.value = it.text;
  ta.focus();
  box.querySelector('[data-done]').addEventListener('click', () => { cmpPeekClose(); _cmpTa.focus(); });
  cmpSyncPad();
}
function cmpPeekClose() {
  const box = $('#cmpPeek');
  const ta = box.querySelector('textarea');
  const it = _cmpItems.find((x) => x.id === box.dataset.id);
  if (it && ta) {
    if (ta.value.trim()) it.text = ta.value;
    else _cmpItems = _cmpItems.filter((x) => x.id !== it.id);
  }
  box.hidden = true; box.innerHTML = ''; delete box.dataset.id;
  cmpRender();
}
// A file added to the tray is uploaded at once: into this desk's
// part of the node's attachment store, through Studio to core-api. The chip says
// "uploading", then its size, or the refusal core-api gave. A file joins a
// message only once it is stored, so what the agent is told about is always
// something the node actually holds.
function cmpAddFiles(list) {
  for (const f of Array.from(list || [])) {
    const isImg = /^image\//.test(f.type || '');
    const it = {
      id: 'c' + (++_cmpSeq), kind: isImg ? 'image' : 'file',
      name: f.name || (isImg ? 'image' : 'file'), size: f.size || 0,
      url: isImg ? URL.createObjectURL(f) : null,
      desk: activeId, aid: null, ctype: null, uploading: false,
      bad: f.size > CMP_MAX_FILE ? `too large: ${cmpFmtSize(f.size)}, limit ${cmpFmtSize(CMP_MAX_FILE)}` : null,
    };
    if (!it.bad && !it.desk) it.bad = 'open a chat first';
    _cmpItems.push(it);
    if (!it.bad) cmpUpload(it, f);
  }
  cmpRender();
}
async function cmpUpload(it, file) {
  it.uploading = true;
  try {
    const r = await fetch(`/api/desks/${encodeURIComponent(it.desk)}/attachments`, {
      method: 'POST',
      // The real type rides in its own header: as the request's Content-Type, a
      // .json file would be parsed by Studio's JSON body parser, not stored.
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Content-Type': file.type || 'application/octet-stream',
        'X-Filename': encodeURIComponent(it.name),
        Accept: 'application/json',
      },
      body: file,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((data && (data.detail || data.error)) || ('http ' + r.status));
    it.aid = data.id; it.ctype = data.content_type; it.size = data.size_bytes;
    // Removed from the tray while it was still on its way: it reached the store
    // anyway, so take it back out.
    if (!_cmpItems.includes(it)) cmpDeleteStored(it);
  } catch (e) {
    it.bad = 'not stored: ' + String(e.message || e).slice(0, 120);
  } finally {
    it.uploading = false;
    cmpRender();
  }
}
function cmpDeleteStored(it) {
  if (!it.aid || !it.desk) return;
  fetch(`/api/desks/${encodeURIComponent(it.desk)}/attachments/${encodeURIComponent(it.aid)}`, { method: 'DELETE' })
    .catch((e) => console.error('[studio] could not delete attachment', it.aid, e));
}
const cmpUploading = () => _cmpItems.some((it) => it.uploading);
// What goes to the agent: the typed text, each pasted chip as a labelled block,
// then one line per STORED file -- the attachment summary line, carrying the
// handle. The agent sees the names now; opening the files is deferred work.
function cmpCompose() {
  if (!$('#cmpPeek').hidden) cmpPeekClose();
  let message = _cmpTa.value.trim();
  for (const p of _cmpItems.filter((it) => it.kind === 'paste')) {
    message += (message ? '\n\n' : '')
      + `--- pasted text (${p.text.length} chars) ---\n${p.text}\n--- end of pasted text ---`;
  }
  const files = _cmpItems.filter((it) => it.kind !== 'paste' && it.aid);
  if (files.length) {
    message += (message ? '\n\n' : '') + files.map((f) =>
      `[attachment: ${f.name.replace(/[\r\n\]]/g, ' ')}, ${f.ctype || 'application/octet-stream'}, ${f.size} bytes, id ${f.aid}]`
    ).join('\n');
  }
  return { message, files };
}
function cmpReset() {
  _cmpItems = [];
  _cmpTa.value = '';
  cmpRender();
  cmpGrow();
}
// One stored file in a message, as cmpCompose writes it.
const CMP_ATTACH_RE = /^\[attachment: (.+), ([\w.+\/-]+), (\d+) bytes, id ([0-9a-f-]{36})\]$/;
// A user turn as the thread shows it: text with its line breaks, each pasted
// block collapsed under a summary, and the stored files as chips that open them
// (a thumbnail for an image). Used for the live bubble and for history, so a
// reload shows what was sent. Only the TRAILING attachment lines are files: a
// line inside a pasted block that merely looks like one stays text.
function userHtml(text, deskId) {
  const lines = String(text || '').split('\n');
  let i = lines.length;
  while (i > 0 && CMP_ATTACH_RE.test(lines[i - 1])) i--;
  const files = lines.slice(i).map((ln) => CMP_ATTACH_RE.exec(ln));
  const s = lines.slice(0, i).join('\n');
  const re = /--- pasted text \((\d+) chars\) ---\n([\s\S]*?)\n--- end of pasted text ---/g;
  const trimNl = (x) => x.replace(/^\n+|\n+$/g, '');
  let out = '', last = 0, m;
  while ((m = re.exec(s))) {
    out += esc(trimNl(s.slice(last, m.index)));
    out += `<details class="cmp-pasted"><summary>Pasted text, ${Number(m[1]).toLocaleString()} chars</summary><pre>${esc(m[2])}</pre></details>`;
    last = re.lastIndex;
  }
  return out + esc(trimNl(s.slice(last))) + cmpFilesHtml(files, deskId || activeId);
}
function cmpFilesHtml(files, desk) {
  if (!files.length || !desk) return '';
  return '<div class="cmp-sent">' + files.map(([, name, ctype, size, aid]) => {
    const url = `/api/desks/${encodeURIComponent(desk)}/attachments/${encodeURIComponent(aid)}/content`;
    const img = /^image\/(png|jpeg|gif|webp)$/.test(ctype) ? `<img class="cmp-chip__img" src="${url}" alt="" loading="lazy">` : '';
    return `<a class="cmp-chip" href="${url}" target="_blank" rel="noopener">${img}`
      + `<span class="cmp-chip__t"><b>${esc(name)}</b><small>${cmpFmtSize(Number(size))}</small></span></a>`;
  }).join('') + '</div>';
}

_cmpTa.addEventListener('input', cmpGrow);
_cmpTa.addEventListener('input', () => draftSaveText(activeId, _cmpTa.value));
// Size the field whenever its width changes, not only on input: on a narrow phone the
// placeholder wraps to two lines and was cut at one until the first send ran cmpGrow. The width
// check keeps our own height change from calling back into itself; 0 is the hidden composer.
let _cmpTaW = 0;
new ResizeObserver((entries) => {
  const w = Math.round(entries[0].contentRect.width);
  if (!w || w === _cmpTaW) return;
  _cmpTaW = w;
  cmpGrow();
  cmpSyncPad();
}).observe(_cmpTa);
// Enter sends; Shift+Enter and Ctrl/Cmd+Enter are a new line (the owner reached
// for Ctrl+Enter first, and it sent). A textarea inserts nothing on Ctrl+Enter
// by itself, so that line break is put in here. isComposing / 229: an IME (e.g.
// a Ukrainian or Chinese input method) is mid-word, and Enter belongs to it.
_cmpTa.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    const s = _cmpTa.selectionStart, t = _cmpTa.selectionEnd;
    _cmpTa.value = _cmpTa.value.slice(0, s) + '\n' + _cmpTa.value.slice(t);
    _cmpTa.selectionStart = _cmpTa.selectionEnd = s + 1;
    cmpGrow();
    return;
  }
  sendChat();
});
_cmpTa.addEventListener('paste', (e) => {
  const cd = e.clipboardData;
  if (!cd) return;
  const files = Array.from(cd.files || []);
  if (files.length) { e.preventDefault(); cmpAddFiles(files); return; }
  const t = cd.getData('text/plain') || '';
  if (t.length > CMP_PASTE_CHARS || t.split('\n').length > CMP_PASTE_LINES) {
    e.preventDefault();
    _cmpItems.push({ id: 'c' + (++_cmpSeq), kind: 'paste', text: t });
    cmpRender();
  }
});
// "+" opens the file picker straight away. A separate Image entry was dropped:
// the file picker takes images too and they get a thumbnail by MIME type, so a menu
// with one item left was only an extra click.
$('#cmpPlus').addEventListener('click', (e) => {
  e.stopPropagation();
  $('#cmpFile').click();
});
$('#cmpFile').addEventListener('change', (e) => {
  cmpAddFiles(e.target.files);
  e.target.value = '';
});
(function () {
  const comp = $('#composer');
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  ['dragenter', 'dragover'].forEach((t) => comp.addEventListener(t, (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    comp.classList.add('is-drop');
  }));
  comp.addEventListener('dragleave', (e) => { if (!comp.contains(e.relatedTarget)) comp.classList.remove('is-drop'); });
  comp.addEventListener('drop', (e) => {
    comp.classList.remove('is-drop');
    if (!hasFiles(e)) return;
    e.preventDefault();
    cmpAddFiles(e.dataTransfer.files);
  });
  if (window.ResizeObserver) new ResizeObserver(cmpSyncPad).observe(comp);
})();

const meReady = loadMe();
// The person's time zone, from this browser: "remind me at 9" means their 9, and
// the agent tells times in it. Only the old /next sent it, so on the new
// Studio every person stayed on UTC (an agent said 18:03 at 13:03, 2026-10-30).
meReady.then(() => fetch('/api/reminders/timezone', {
  method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
  body: JSON.stringify({ timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }),
})).catch(() => {});
loadDirectory();
loadAgents();
// Kept so a message sent before the chat list arrived can wait for it (see sendChat).
var _desksReady = loadDesks();
// Context changes only after a provider request completes. Poll the visible chat
// lightly; the initial bulk read supplies tabs, agent chat rows, and card maxima.
// the open chat's context meter: read again when a turn in it ends (live stream)
// What arrives in the open chat after its reply (a colleague's answer, a
// reminder, a relay): /history includes them, so a length change means a new
// message to draw. Skip while a turn is in flight so we never fight the
// optimistic bubbles. Called by the live stream below the moment the chat
// changes; the poll is only the fallback for a dropped stream.
// What the live stream's "this chat changed" reads: the messages after the newest
// one on screen, not the whole chat again. A colleague's answer relayed into the
// chat (an @mention answered by mail) is drawn as it arrives, also while the chat's
// own agent is working: the page used to skip the chat during a turn, so such an
// answer stayed unseen until the person wrote again. The chat's own turns are drawn by their turn while it runs; once none
// runs, stored rows of a turn rebuild the thread from the history, as before.
var _histLast = {};     // desk id -> created_at of the newest stored message drawn
var _busDrawn = {};     // desk id -> colleagues' answers drawn since the last rebuild
const _histKey = (m) => String(m.created_at) + '|' + String(m.content || '').slice(0, 80);
function firstPendingEl(id) {
  const mine = new Set();
  for (const t of deskTurns(id)) { if (t.userEl) mine.add(t.userEl); if (t.el) mine.add(t.el); }
  for (const el of thread.children) if (mine.has(el)) return el;
  return null;
}
async function refreshOpenChat() {
  const id = activeId;
  if (!id) return;
  const since = _histLast[id];
  if (since == null || _threadFor !== id) {
    if (!hasTurns(id)) { loadHistory(id); }
    return;
  }
  try {
    const r = await fetch('/api/desks/' + encodeURIComponent(id) + '/history?after=' + encodeURIComponent(since),
      { headers: { Accept: 'application/json' } });
    if (!r.ok) return;
    const fresh = ((await r.json()).messages) || [];
    if (id !== activeId || _histLast[id] !== since || !fresh.length) return;   // rebuilt meanwhile, or nothing new
    // a gateway turn's rows (message_id) and the person's own messages
    const own = (m) => m.message_id != null || m.role === 'user';
    const busy = hasTurns(id);
    if (!busy && fresh.some(own)) {
      loadHistory(id);
      const sc = $('#thread') && $('#thread').parentElement;
      if (sc && _tailDesk !== id) setTimeout(() => { sc.scrollTop = sc.scrollHeight; }, 50);
      return;
    }
    const drawn = _busDrawn[id] || (_busDrawn[id] = new Set());
    const add = fresh.filter((m) => !own(m) && !drawn.has(_histKey(m)));
    const pending = busy ? firstPendingEl(id) : null;
    for (const m of add) {
      drawn.add(_histKey(m));
      const el = drawStored(m, { arrive: true });
      // above the turn still being written, which stays the last thing in the chat
      if (el && pending && pending.isConnected) thread.insertBefore(el, pending);
    }
    // During a turn the mark stays: its own rows come later and rebuild the thread.
    if (!busy) _histLast[id] = fresh[fresh.length - 1].created_at;
    if (!add.length) return;
    _histLen[id] = (_histLen[id] || 0) + add.length;
    if (window.snAuthorsPaint) window.snAuthorsPaint();
    paintChatControls();
    const _last = add[add.length - 1];
    _deskMeta[id] = { lastMsg: String(snHumanFailure(_last.content) || _last.content || '').replace(/[*_`#>\[\]]/g, '').replace(/\s+/g, ' ').slice(0, 80).trim(), lastAt: String(_last.created_at || '') };
    renderTabs();
  } catch {}
}
// The one live stream (core GET /api/events/stream through
// Studio): each event about one of the person's chats arrives as it is written,
// {id, type, desk}, and only what it touches is read again. The page used to
// learn of a change on its next poll, and a chat closed elsewhere stayed on
// screen until a reload. No polls: when the stream opens, and again
// after a drop, the page reads everything once (sn:resync), so nothing missed
// while it was down stays missed; a dropped stream is opened again, waiting
// longer each time.
// A burst of events reads each thing once: the call runs 250 ms after the last ask.
const _soonT = {};
function soon(key, fn) { clearTimeout(_soonT[key]); _soonT[key] = setTimeout(fn, 250); }
function onScreen(sel) { const el = $(sel); return !!el && el.style.display !== 'none'; }
// One WebSocket for the page (Studio /api/live): the bus's events and the open
// chat's turn. Two event streams per tab counted against the six connections a
// browser keeps to one host on plain http, and a sent message waited behind them
// (tester, 2026-10-30); WebSockets are not counted.
let _ws = null;
let _liveReopen = null;
let _liveHeardAt = 0;   // the last time anything came from the socket; the server ticks every 25 s
const _liveHealthy = () => !!_ws && _ws.readyState === 1 && Date.now() - _liveHeardAt < 40000;
function liveSend(o) { if (_ws && _ws.readyState === 1) _ws.send(JSON.stringify(o)); }
(function liveSocket() {
  let wait = 1000;
  let waitT = null;
  function resync() {
    loadDesks(); loadAgentState(); loadDeskWork(); loadLastSeen(); refreshOpenChat();
    if (activeId) loadContextStatuses(activeId);
    if (onScreen('#dashView')) dshLoadLive();
    document.dispatchEvent(new CustomEvent('sn:resync'));
  }
  function open() {
    if (waitT) { clearTimeout(waitT); waitT = null; }
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/live');
    _ws = ws;
    _liveHeardAt = Date.now();
    ws.onmessage = (m) => {
      _liveHeardAt = Date.now();
      let msg; try { msg = JSON.parse(m.data); } catch { return; }
      // the bus answered: read everything once, and follow the open chat's turn again
      if (msg.kind === 'open') { wait = 1000; if (_live.id) liveSend({ watch: _live.id }); resync(); return; }
      if (msg.kind === 'live') { try { onDeskLive(msg.desk, msg.data); } catch {} return; }
      if (msg.kind !== 'event') return;
      const ev = msg;
      const t = String(ev.type || '');
      if (t.startsWith('desk.')) soon('desks', loadDesks);         // created, renamed, closed, deleted
      if (ev.desk && ev.desk === activeId) soon('chat', refreshOpenChat);
      if (t.startsWith('agent.')) {
        soon('state', loadAgentState); soon('work', loadDeskWork); soon('seen', loadLastSeen);
      }
      if (t === 'agent.turn_started' && ev.desk === activeId) watchDeskLive(activeId);
      if (t === 'agent.turn_completed' && ev.desk) _peerTurn.delete(ev.desk);
      if (t === 'agent.turn_completed' && ev.desk === activeId) soon('ctx', () => loadContextStatuses(activeId));
      // "What started it" names the turn that just began, and the chat's newest message
      if (t.startsWith('agent.')) _dshRecent.at = 0;
      if (ev.desk) _dshDesks.delete(ev.desk);
      if (onScreen('#dashView')) soon('dash', () => { dshLoadLive(); dshLoadNow(); });
      document.dispatchEvent(new CustomEvent('sn:event', { detail: ev }));   // unread counts, panels, pages
    };
    ws.onclose = () => {
      if (_ws === ws) _ws = null;
      if (waitT) clearTimeout(waitT);
      waitT = setTimeout(open, wait);
      wait = Math.min(wait * 2, 30000);
    };
  }
  // visibilitychange calls this when the tab comes back: a mobile PWA often returns
  // with a dead or zombie socket and a frozen onclose backoff. Drop the old socket,
  // reset the backoff, and open one now so the server's 'open' event fires resync().
  _liveReopen = function () {
    wait = 1000;
    const prev = _ws;
    _ws = null;
    if (prev) { try { prev.onclose = null; prev.close(); } catch (_) {} }
    open();
  };
  open();
  // A socket can die without a word (a phone asleep, a network that changed): nothing then
  // arrives and nothing says so. Three ticks missed while the page is in view: open another.
  setInterval(() => { if (!document.hidden && _ws && Date.now() - _liveHeardAt > 70000) _liveReopen(); }, 15000);
})();

/* ---- Pull down to refresh. An installed app has no reload button, and its screens scroll inside the page,
   so the browser's own pull-to-refresh never fires: the person had to close and reopen it. A pull from the top
   of a screen asks the server which build is current (a newer one reloads the page) and otherwise opens a new
   live socket, whose first message makes the page read everything again. ---- */
(function pullToRefresh() {
  if (!('ontouchstart' in window)) return;
  const NEED = 80;                 // px of pull that counts
  let y0 = 0, x0 = 0, dy = 0, live = false, busy = false, ind = null;
  const scrolledAway = (el) => {
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) if (e.scrollTop !== 0) return true;
    return false;
  };
  const typing = (el) => !!(el && el.closest && el.closest('input,textarea,select,[contenteditable="true"]'));
  function bar() {
    if (ind) return ind;
    ind = document.createElement('div');
    ind.className = 'ptr'; ind.setAttribute('role', 'status');
    document.body.appendChild(ind);   // outside the screen: the line stays at the top while the screen moves down
    return ind;
  }
  // The screen follows the finger down, as in a native app, and a thin line stays at the very top of the display
  // (below the system bar) and fills as the pull grows, then runs while the app reads everything again. At rest the
  // transform is removed, not set to 0: a transform would make the screen the frame of everything fixed inside it.
  const root = () => document.querySelector('.app');
  function show(text, pull) {
    const el = bar(), r = root(), px = Math.min(pull, 90);
    // no words: the label is for screen readers only
    const fill = Math.min(1, pull / (NEED / 2));
    el.setAttribute('aria-label', text);
    el.style.setProperty('--ptr-fill', String(fill));
    el.classList.toggle('is-busy', busy);
    el.style.opacity = pull > 6 || busy ? '1' : '0';
    if (r) { r.style.transition = 'none'; r.style.transform = px > 0 ? `translateY(${px}px)` : ''; }
  }
  function hide() {
    if (ind) ind.style.opacity = '0';
    const r = root();
    if (!r || !r.style.transform) return;
    r.style.transition = 'transform .2s ease'; r.style.transform = 'translateY(0)';
    setTimeout(() => { r.style.transition = ''; r.style.transform = ''; }, 220);
  }
  async function refresh() {
    busy = true; show('Refreshing...', 60);
    const t0 = Date.now();
    try {
      const r = await fetch('/api/version', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      const b = r.ok ? String((await r.json()).build || '') : '';
      if (b && window.__BUILD__ && b !== String(window.__BUILD__)) { location.reload(); return; }
    } catch { /* offline: the new socket below says nothing either, and the bar goes away */ }
    if (typeof _liveReopen === 'function' && _liveReopen) _liveReopen();
    setTimeout(() => { busy = false; hide(); }, Math.max(0, 1200 - (Date.now() - t0)));
  }
  document.addEventListener('touchstart', (e) => {
    // a pull whose end never reached the page (the system took the gesture) must not leave the screen shifted
    const r = root();
    if (!busy && r && r.style.transform) { r.style.transition = ''; r.style.transform = ''; }
    live = !busy && e.touches.length === 1 && !typing(e.target) && !scrolledAway(e.target);
    if (live) { y0 = e.touches[0].clientY; x0 = e.touches[0].clientX; dy = 0; }
  }, { passive: true });
  document.addEventListener('touchmove', (e) => {
    if (!live) return;
    dy = e.touches[0].clientY - y0;
    if (Math.abs(e.touches[0].clientX - x0) > Math.abs(dy) || dy <= 0) { live = false; hide(); return; }
    show(dy >= NEED ? 'Release to refresh' : 'Pull to refresh', dy * 0.5);
  }, { passive: true });
  document.addEventListener('touchend', () => {
    if (!live) return;
    live = false;
    if (dy >= NEED) refresh(); else hide();
  }, { passive: true });
  document.addEventListener('touchcancel', () => { live = false; hide(); }, { passive: true });
})();

/* ---- Studio views (planner-backed): Tasks list / Task detail / Dashboard.
   Rendered inside the Studio shell; data from the planner companion via the
   /api/tasks BFF proxy (not core). A tiny view-router toggles the panels. ---- */
const TK_STATUSES = ['open','assessing','awaiting_approval','in_progress','pending_review','done','blocked','escalated','rejected','incident'];
const TK_PRIORITIES = ['critical','high','medium','low'];
// local: status model v1 (planner 0010). A task has three statuses, queued /
// in_progress / closed; why a queued task waits (wait_reason) and how a closed
// task ended (close_outcome) are rows of two dictionaries the owner edits in
// Settings. The planner still sends the legacy one-word name as `stage` (and,
// while PLANNER_STATUS_COMPAT=legacy, as `status` too); everything here that
// decides on the old names reads tkStage(t), so the panel works in both modes.
const TK_STATES = ['queued', 'in_progress', 'closed'];
let _tkDicts = { wait_reasons: [], close_outcomes: [] };
function tkStage(t) { return (t && (t.stage || t.status)) || ''; }
function tkState(t) {
  if (t && t.state) return t.state;
  const s = tkStage(t);
  if (s === 'in_progress' || s === 'assessing' || s === 'incident') return 'in_progress';
  if (s === 'done' || s === 'rejected') return 'closed';
  return 'queued';
}
function tkWhy(t) { return (t && (t.wait_reason || t.close_outcome)) || ''; }
function tkStatusChip(t) {
  if (!t) return '<span class="tkt__mut">&mdash;</span>';
  const st = tkState(t), why = tkWhy(t), stage = tkStage(t) || st;
  const label = why ? `${esc(st)} &middot; ${esc(why)}` : esc(st);
  return `<span class="tkt__chip tkt__chip--s-${esc(stage)}" title="${esc(stage)}">${label}</span>`;
}
function tkStatusOptions() {
  const out = [{ v: 'queued', t: 'queued (all)' }, { v: 'queued:', t: 'queued, no reason yet' }];
  (_tkDicts.wait_reasons || []).filter((r) => r.enabled !== false).forEach((r) => out.push({ v: 'queued:' + r.code, t: 'queued · ' + r.code }));
  out.push({ v: 'in_progress', t: 'in_progress' }, { v: 'closed', t: 'closed (all)' });
  (_tkDicts.close_outcomes || []).filter((r) => r.enabled !== false).forEach((r) => out.push({ v: 'closed:' + r.code, t: 'closed · ' + r.code }));
  return out;
}
function tkStatusMatch(t, sel) {
  if (!sel) return true;
  const i = sel.indexOf(':');
  if (i < 0) return tkState(t) === sel;
  return tkState(t) === sel.slice(0, i) && tkWhy(t) === sel.slice(i + 1);
}
// The tasks module's API, asked only on a node that has the module. Every page
// called it, and on a node without it each call was a 502 in the console and an
// error on the interface map (tester, 2026-10-30). Without the module the caller
// gets an empty "not found", which every caller already reads as "no tasks".
function tasksFetch(url) {
  return (window.snComponents || Promise.resolve()).then(() =>
    (window.snInstalled && !window.snInstalled.has('tracking'))
      ? new Response('[]', { status: 404 })
      : fetch(url, { headers: { Accept: 'application/json' } }));
}
async function tkLoadDicts() {
  if (window.snComponents) await window.snComponents;
  if (window.snInstalled && !window.snInstalled.has('tracking')) return;
  try {
    const [w, c] = await Promise.all(['wait-reasons', 'close-outcomes'].map((d) => fetch('/api/dictionaries/' + d, { headers: { Accept: 'application/json' } })));
    if (w.ok) _tkDicts.wait_reasons = await w.json();
    if (c.ok) _tkDicts.close_outcomes = await c.json();
  } catch (_) { /* the filter falls back to the three states */ }
  const el = $('#fStatus');
  if (el) {
    const keep = el.value;
    el.innerHTML = '<option value="">All status</option>';
    tkStatusOptions().forEach((o) => { const op = document.createElement('option'); op.value = o.v; op.textContent = o.t; el.appendChild(op); });
    el.value = keep;
  }
}
// Settings: a link to the node panel, for an admin only. Everyone else does not run the
// node and the panel would refuse them. The panel is this host with panel. for
// Studio.
const PANEL_ROLES = ['super-admin', 'admin'];
// Settings: the password and passkeys live in Authum (it alone holds them), on its own
// settings page; this row opens it. Everyone signs in, so everyone sees it.
async function loadSignInLink() {
  await meReady;
  const card = $('#signinCard'), a = $('#signinLink'); if (!card || !a) return;
  a.href = `${meAuthUrl}/?page=settings`;
  card.hidden = !meAuthUrl;
}
async function loadPanelLink() {
  await meReady;
  const card = $('#panelCard'), a = $('#panelLink'); if (!card || !a) return;
  const host = location.hostname.replace(/^studio\./, 'admin.');
  a.href = host !== location.hostname ? `${location.protocol}//${host}/` : '/';
  card.hidden = !PANEL_ROLES.includes(meIdentity.role);
}
async function loadAbout() {
  const rel = $('#aboutRelease'), sub = $('#aboutBuild'); if (!rel || !sub) return;
  try {
    const r = await fetch('/api/version', { headers: { Accept: 'application/json' }, cache: 'no-store' });
    const v = r.ok ? await r.json() : {};
    rel.textContent = v.release || 'version unknown';
    sub.textContent = v.build ? `Interface build ${v.build}${String(window.__BUILD__ || '') === String(v.build) ? '' : ' (this tab is older; reload)'}` : '';
  } catch { rel.textContent = 'version unknown'; sub.textContent = ''; }
}
let _tasksAll = [];
let _projMap = {}, _objMap = {}, _wfMap = {};

// view: 'chats' | 'tasks' | 'detail' | 'dash'. Inline display beats the
// .task/.tasksv{display:flex} rules, so panels never overlap.
/* ============ Workflows view ============
   Three levels inside the shell, breadcrumb-driven:
     list   -> a card per workflow (flow board like the landing + the granted crew)
     detail -> one workflow: full board + run trigger + run history
     run    -> one run: full chronology with the real content (drafts, decision)
   Run data is DERIVED from /api/events (entity_type=workflow): events form a chain
   via parent_event_id; the message.inbound (parent=null) is a run's root, so a run
   = events grouped by (parent_event_id || id). No extra API needed. */
let _wfList = [];                                  // cached workflow definitions
let _wfState = { level: 'list', name: null, runId: null };

const WF_ICON = {
  trigger: '<path d="M4 5h16v11H7l-3 3z"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/>',
  draft: '<path d="M4 20l4-1 11-11-3-3L5 16z"/><path d="M14 6l3 3"/>',
  gate: '<path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/>',
  send: '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>',
  step: '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
};
function wfIcon(nd) {
  if (nd.trigger) return WF_ICON.trigger;
  if (nd.gate) return WF_ICON.gate;
  const n = nd.name || '';
  if (/calendar/.test(n)) return WF_ICON.calendar;
  if (/draft|revise|reply/.test(n)) return WF_ICON.draft;
  if (n === 'send') return WF_ICON.send;
  return WF_ICON.step;
}
const wfInitial = (s) => esc((s || '?').trim().charAt(0).toUpperCase());

// ---- flow graph ----------------------------------------------------------
// Edges live in three places in a flow and a picture that reads one of them is
// wrong for most flows: depends_on carries fan-out and convergence, routing
// carries branches and the loop back, next carries conditional jumps. Of the
// five flows the engine ships as examples, three are defined by depends_on
// alone (deliberation is a three-phase mesh) and one by a conditional next.
// The engine's own state builder reads depends_on only, which is why a flow
// written in our routing style draws there as a straight chain.
function wfEdges(steps) {
  const known = new Set(steps.map((s) => s.name));
  const seen = new Set();
  const edges = [];
  const add = (from, to, label) => {
    if (!known.has(from) || !known.has(to) || from === to) return;
    const k = from + '\u0000' + to;
    if (seen.has(k)) return;                 // one wire per pair; first label wins
    seen.add(k);
    edges.push({ from, to, label: label || '' });
  };
  steps.forEach((s) => {
    (s.depends_on || []).forEach((d) => add(d, s.name, ''));
    Object.keys(s.routing || {}).forEach((sig) =>
      add(s.name, s.routing[sig], sig === '_default' ? '' : sig));
    const nx = s.next;
    if (typeof nx === 'string') add(s.name, nx, '');
    else if (Array.isArray(nx)) nx.forEach((n) => {
      if (!n || typeof n !== 'object') return;
      if (n.goto) add(s.name, n.goto, 'if');
      if (n.else) add(s.name, n.else, 'else');
    });
  });
  return edges;
}

// Declaration order decides what counts as a step forward, so the revise arc is
// drawn as an arc instead of being mistaken for depth.
function wfRanks(steps, edges) {
  const order = new Map(steps.map((s, i) => [s.name, i]));
  if (steps.some((s) => s.phase !== null && s.phase !== undefined)) {
    const ps = steps.map((s) => (s.phase == null ? 1 : s.phase));
    const min = Math.min.apply(null, ps);
    return new Map(steps.map((s) => [s.name, (s.phase == null ? min : s.phase) - min]));
  }
  const rank = new Map(steps.map((s) => [s.name, 0]));
  for (let pass = 0; pass < steps.length; pass++) {
    let moved = false;
    edges.forEach((e) => {
      if (order.get(e.to) <= order.get(e.from)) return;      // back edge
      const want = rank.get(e.from) + 1;
      if (want > rank.get(e.to)) { rank.set(e.to, want); moved = true; }
    });
    if (!moved) break;
  }
  return rank;
}

// A flow whose picture today is already true keeps today's picture exactly:
// one chip per rank, no branch, no arc. The graph is for the ones that are not.
function wfIsChain(steps, edges) {
  const out = {}, inn = {};
  const order = new Map(steps.map((s, i) => [s.name, i]));
  for (const e of edges) {
    if (order.get(e.to) <= order.get(e.from)) return false;   // a loop back
    out[e.from] = (out[e.from] || 0) + 1;
    inn[e.to] = (inn[e.to] || 0) + 1;
    if (out[e.from] > 1 || inn[e.to] > 1) return false;       // a branch or a merge
  }
  return true;
}

// The flow as a linear node chain, drawn from what the registrar actually stored:
// per-step kind, specialist and routing. It used to narrate instead. It prepended
// a "New inquiry / inbound" node to every flow, called every gate "waits for
// owner", dropped any step whose next was literally wait_for_owner, and named
// blocks called send and done "deliver" and "complete". That is one flow's story
// written into the renderer for all of them: on a flow with no inbound message and
// a deliver block that is a logic hook, the picture asserted things that were not
// true. Same shape as the demo harness removed from the run path in 1.12.14.
//
// Still a straight line: the branches and the loop-back are in s.routing and are
// not drawn here yet.
function wfBoard(w, full) {
  const steps = w.steps || [];
  const edges = wfEdges(steps);
  // Last-step is "no edge leaves it", computed from the edges, not from whether
  // the block itself declared a routing or a next. A block is very often linked
  // by the NEXT block's depends_on, and reading only its own declarations marked
  // every fan-out source as an end: all three phase-1 blocks of a deliberation,
  // and the first block of a conditional flow.
  const hasOut = new Set(edges.map((e) => e.from));
  if (steps.length && !wfIsChain(steps, edges)) return wfGraphBoard(w, full, steps, edges, hasOut);
  return wfChainBoard(w, full, hasOut);
}

// The same chips, placed by rank instead of in a row, with the wires drawn as
// one SVG layer over the measured positions. Nothing new to look at: the chip
// markup and its classes are exactly the chain's, so the theme carries over
// unchanged and there is no second visual language to keep in step.
function wfGraphBoard(w, full, steps, edges, hasOut) {
  const rank = wfRanks(steps, edges);
  const cols = [];
  steps.forEach((s) => {
    const r = rank.get(s.name) || 0;
    (cols[r] = cols[r] || []).push(s);
  });
  const body = cols.map((col) => '<div class="wf-graph__col">' + (col || []).map((s) => {
    const nd = {
      gate: s.kind === 'gate',
      term: !hasOut.has(s.name),
      name: String(s.name).replace(/_/g, ' '),
      who: s.specialist || s.kind || 'step',
    };
    const cls = nd.gate ? 'is-gate' : (nd.term ? 'is-term' : '');
    const tag = full ? `<span class="wf-node__tag">${esc(nd.who)}</span>` : '';
    return `<span class="wf-node ${cls}" data-wfn="${esc(s.name)}">
      <span class="wf-node__ic"><svg viewBox="0 0 24 24">${wfIcon(nd)}</svg></span>
      <span class="wf-node__t"><span class="wf-node__nm">${esc(nd.name)}</span>${tag}</span>
    </span>`;
  }).join('') + '</div>').join('');
  return `<div class="wf-graph ${full ? 'wf-graph--full' : ''}" data-edges="${esc(JSON.stringify(edges))}">
    <svg class="wf-graph__svg" aria-hidden="true"></svg>${body}</div>`;
}

// Wires are drawn after layout because only the browser knows where the chips
// ended up. The graph runs top to bottom: a forward edge drops from the bottom
// of a step to the top of the next, and a loop back is an arc on the right so
// it cannot be read as another step. Called again on resize, since a
// measured drawing is stale the moment the column widths change.
function wfDrawEdges() {
  document.querySelectorAll('.wf-graph').forEach(wfDrawEdgesIn);
}
function wfDrawEdgesIn(wrap) {
  const svg = wrap.querySelector('.wf-graph__svg');
  if (!svg) return;
  let edges = [];
  try { edges = JSON.parse(wrap.getAttribute('data-edges') || '[]'); } catch (e) { return; }
  const box = wrap.getBoundingClientRect();
  const pos = {};
  wrap.querySelectorAll('[data-wfn]').forEach((el) => {
    const r = el.getBoundingClientRect();
    const l = r.left - box.left, t = r.top - box.top;
    pos[el.getAttribute('data-wfn')] = {
      l, r: l + r.width, t, b: t + r.height,
      cx: l + r.width / 2, cy: t + r.height / 2,
    };
  });
  const right = Object.keys(pos).reduce((m, k) => Math.max(m, pos[k].r), 0);
  const parts = [];
  edges.forEach((e) => {
    const a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    let d, lx, ly;
    if (b.t > a.b) {                                   // forward, downwards
      const mid = a.b + (b.t - a.b) / 2;
      d = `M ${a.cx} ${a.b} C ${a.cx} ${mid}, ${b.cx} ${mid}, ${b.cx} ${b.t}`;
      lx = (a.cx + b.cx) / 2 + 8; ly = mid;
    } else {                                           // back, an arc on the right
      const x = right + 60;
      d = `M ${a.r} ${a.cy} C ${x} ${a.cy}, ${x} ${b.cy}, ${b.r} ${b.cy}`;
      lx = x - 14; ly = (a.cy + b.cy) / 2 + 4;
    }
    parts.push(`<path class="wf-edge" d="${d}" marker-end="url(#wfArrow)"/>`);
    if (e.label) parts.push(`<text class="wf-edge__lbl" x="${lx}" y="${ly}" text-anchor="middle">${esc(e.label)}</text>`);
  });
  const w = Math.max(box.width, right + 90), h = box.height;
  svg.setAttribute('width', w);
  svg.setAttribute('height', h);
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.innerHTML = `<defs><marker id="wfArrow" viewBox="0 0 8 8" refX="7" refY="4"
      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,1 L7,4 L0,7 z" class="wf-edge__head"/></marker></defs>` + parts.join('');
}
let _wfRedraw;
window.addEventListener('resize', () => {
  clearTimeout(_wfRedraw);
  _wfRedraw = setTimeout(wfDrawEdges, 120);
});

function wfChainBoard(w, full, hasOut) {
  // Only a trigger the flow actually declares. No synthetic one.
  const nodes = w.trigger ? [{ trigger: true, name: w.trigger, who: 'trigger' }] : [];
  (w.steps || []).forEach((s) => {
    const isGate = s.kind === 'gate';
    nodes.push({
      gate: isGate,
      term: !hasOut || !hasOut.has(s.name),
      name: String(s.name).replace(/_/g, ' '),
      who: s.specialist || s.kind || 'step',
    });
  });
  return `<div class="wf-board ${full ? 'wf-board--full' : ''}">` + nodes.map((nd, i) => {
    const wire = i ? '<span class="wf-wire"></span>' : '';
    const cls = nd.gate ? 'is-gate' : (nd.term ? 'is-term' : (nd.trigger ? 'is-trig' : ''));
    const tag = full ? `<span class="wf-node__tag">${esc(nd.who)}</span>` : '';
    return `${wire}<span class="wf-node ${cls}">
      <span class="wf-node__ic"><svg viewBox="0 0 24 24">${wfIcon(nd)}</svg></span>
      <span class="wf-node__t"><span class="wf-node__nm">${esc(nd.name)}</span>${tag}</span>
    </span>`;
  }).join('') + '</div>';
}

// group flat events into runs keyed by the inbound root (parent_event_id || id)
function wfGroupRuns(events) {
  const byRoot = new Map();
  for (const e of events) {
    const root = e.parent_event_id || e.id;
    if (!byRoot.has(root)) byRoot.set(root, []);
    byRoot.get(root).push(e);
  }
  const ety = (e) => e.event_type || e.type || '';
  const runs = [];
  for (const [root, evs] of byRoot) {
    evs.sort((a, b) => (a.id || 0) - (b.id || 0));
    const inbound = evs.find((e) => ety(e) === 'message.inbound') || evs[0];
    const done = evs.find((e) => ety(e) === 'workflow.completed');
    const gate = evs.find((e) => ety(e) === 'workflow.gate');
    const sent = evs.find((e) => ety(e) === 'message.sent');
    runs.push({
      id: root,
      name: (inbound && inbound.entity_id) || (evs[0] && evs[0].entity_id),
      ts: (inbound && inbound.ts) || (evs[0] && evs[0].ts),
      events: evs,
      actors: [...new Set(evs.map((e) => e.actor).filter((a) => a && a !== 'system' && a !== 'owner'))],
      decision: gate ? (gate.payload || {}).decision : null,
      result: done ? (done.payload || {}).result : (sent ? 'sent' : 'running'),
      subject: (inbound && ((inbound.payload || {}).subject || (inbound.payload || {}).body)) || '',
    });
  }
  runs.sort((a, b) => b.id - a.id);                // newest first
  return runs;
}

function wfTime(v) {
  if (!v) return '';
  const d = new Date(v); if (isNaN(d)) return esc(v);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function wfEvTitle(ty, p) {
  const m = {
    'message.inbound': 'Inbound' + (p.channel ? ' · ' + p.channel : ''),
    'workflow.started': 'Flow started',
    'workflow.step': p.block ? String(p.block).replace(/_/g, ' ') : 'step',
    'workflow.gate': 'Approval gate' + (p.decision ? ' — ' + p.decision : ''),
    'message.sent': 'Sent' + (p.to ? ' to ' + p.to : ''),
    'workflow.completed': 'Completed' + (p.result ? ' (' + p.result + ')' : ''),
  };
  return m[ty] || ty;
}

/* ---- navigation between the three levels ---- */
function wfGo(level, name, runId) {
  _wfState = { level, name: name || null, runId: runId || null };
  let url = '/workflows';
  if (level === 'detail') url = '/workflows/' + encodeURIComponent(name);
  if (level === 'run') url = '/workflows/' + encodeURIComponent(name) + '/runs/' + encodeURIComponent(runId);
  history.pushState({}, '', url);
  wfRender();
}
function wfCrumbs() {
  const c = $('#wfCrumbs'); if (!c) return;
  const items = [{ label: 'Workflows', to: 'list' }];
  if (_wfState.name) items.push({ label: _wfState.name, to: 'detail' });
  if (_wfState.level === 'run' && _wfState.runId) items.push({ label: 'Run #' + _wfState.runId, to: null });
  c.innerHTML = items.map((it, i) => {
    const cur = i === items.length - 1;
    return `<span class="wf-crumb ${cur ? 'is-cur' : ''}" ${!cur && it.to ? `data-wfnav="${it.to}"` : ''}>${esc(it.label)}</span>`;
  }).join('<span class="wf-crumb__sep">/</span>');
  c.querySelectorAll('[data-wfnav]').forEach((el) => el.addEventListener('click', () => {
    const to = el.getAttribute('data-wfnav');
    wfGo(to, to === 'detail' ? _wfState.name : null);
  }));
}

// view entry: ensure the workflow list is loaded, then render the current level.
// Callers set _wfState first (nav click / deep-link route); this just paints it.
async function renderWorkflows() {
  if (!_wfList.length) {
    try { _wfList = ((await (await fetch('/api/workflows')).json()).workflows) || []; }
    catch (e) { _wfList = []; }
  }
  wfRender();
}
function wfRender() {
  wfCrumbs();
  if (_wfState.level === 'detail') return wfRenderDetail();
  if (_wfState.level === 'run') return wfRenderRun();
  return wfRenderList();
}

/* ---- level 1: cards ---- */
function wfRenderList() {
  const box = $('#workflowsBody'); if (!box) return;
  if (!_wfList.length) { box.innerHTML = '<div class="ap__empty">No workflows yet.</div>'; return; }
  box.innerHTML = '<div class="wf-grid">' + _wfList.map((w) => {
    const crew = (w.granted_to || []).map((a) => `<span class="wf-crew__a" title="${esc(a)}">${wfInitial(a)}</span>`).join('');
    return `<button class="wf-card" data-wf="${esc(w.name)}">
      <div class="wf-card__h"><span class="wf-card__t">${esc(w.title || w.name)}</span><span class="wf-card__st wf-st--${esc(w.status)}">${esc(w.status)}</span></div>
      <div class="wf-card__d">${esc(w.description || '')}</div>
      <div class="wf-card__meta">${(w.steps || []).length} steps${w.team ? ' · ' + esc(w.team) : ''}</div>
      <div class="wf-card__f">
        <span class="wf-crew">${crew || '<span class="wf-mut">no crew</span>'}</span>
        <span class="wf-card__go">Open<i data-lucide="arrow-right"></i></span>
      </div>
    </button>`;
  }).join('') + '</div>';
  box.querySelectorAll('[data-wf]').forEach((b) => b.addEventListener('click', () => wfGo('detail', b.getAttribute('data-wf'))));
  if (window.lucide) lucide.createIcons();
}

async function wfLoadRuns(name) {
  const box = $('#wfRuns'); if (!box) return;
  let runs = [];
  try {
    const ev = ((await (await fetch('/api/events?entity_type=workflow&limit=400')).json()).events) || [];
    runs = wfGroupRuns(ev).filter((r) => r.name === name);
  } catch (e) { box.innerHTML = '<div class="ap__empty">Could not load runs.</div>'; return; }
  if (!runs.length) { box.innerHTML = '<div class="ap__empty">No runs yet — trigger one above.</div>'; return; }
  box.innerHTML = runs.map((r) => {
    const crew = r.actors.map((a) => `<span class="wf-crew__a" title="${esc(a)}">${wfInitial(a)}</span>`).join('');
    const dec = r.decision ? `<span class="wf-dec--${r.decision === 'APPROVED' ? 'ok' : 'no'}">${esc(r.decision)}</span>` : '';
    return `<button class="wf-runrow" data-run="${esc(String(r.id))}">
      <span class="wf-runrow__id">#${esc(String(r.id))}</span>
      <span class="wf-runrow__sub">${esc((r.subject || r.name || '').slice(0, 80))}</span>
      <span class="wf-crew">${crew}</span>
      ${dec}
      <span class="wf-res wf-res--${esc(r.result)}">${esc(r.result)}</span>
      <span class="wf-runrow__ts">${wfTime(r.ts)}</span>
    </button>`;
  }).join('');
  box.querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => wfGo('run', name, b.getAttribute('data-run'))));
}
async function wfRun(name) {
  const inq = ($('#wfInq') || {}).value || '';
  const dec = ($('#wfDec') || {}).value || 'approve';
  const msg = $('#wfMsg'); const btn = $('#wfRunBtn');
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = ' starting…';
  try {
    await (await fetch('/api/workflows/' + encodeURIComponent(name) + '/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ inquiry: inq, owner_decision: dec }) })).json();
    if (msg) msg.textContent = ' ✓ started (' + dec + '). The crew is running it (~1 min) — history updates below.';
    let k = 0; const iv = setInterval(() => { if (_wfState.level === 'detail') wfLoadRuns(name); if (++k > 18) clearInterval(iv); }, 4000);
  } catch (e) { if (msg) msg.textContent = ' failed'; }
  finally { if (btn) btn.disabled = false; }
}

/* ---- level 3: one run chronology ---- */
async function wfRenderRun() {
  const box = $('#workflowsBody'); if (!box) return;
  box.innerHTML = skRows(5, false);
  let run = null;
  try {
    const ev = ((await (await fetch('/api/events?entity_type=workflow&limit=400')).json()).events) || [];
    run = wfGroupRuns(ev).find((r) => String(r.id) === String(_wfState.runId));
  } catch (e) {}
  if (!run) { box.innerHTML = '<div class="ap__empty">Run not found.</div>'; return; }
  const rows = run.events.map((e) => {
    const ty = e.event_type || e.type || '';
    const p = e.payload || {};
    const kind = ty.replace('workflow.', '').replace('message.', '');
    const body = p.summary || p.body || (p.decision ? 'Decision: ' + p.decision : '') || '';
    return `<div class="wf-tl__row wf-tl--${esc(kind)}">
      <span class="wf-tl__rail"><span class="wf-tl__dot">${wfInitial(e.actor)}</span></span>
      <div class="wf-tl__card">
        <div class="wf-tl__h"><span class="wf-tl__who">${esc(e.actor)}</span><span class="wf-tl__t">${esc(wfEvTitle(ty, p))}</span><span class="wf-tl__ts">${wfTime(e.ts)}</span></div>
        ${body ? `<div class="wf-tl__b">${esc(body)}</div>` : ''}
      </div>
    </div>`;
  }).join('');
  box.innerHTML = `
    <div class="wf-run-head">
      <span class="wf-res wf-res--${esc(run.result)}">${esc(run.result)}</span>
      <span class="wf-run-head__sub">${esc(run.subject || run.name || '')}</span>
    </div>
    <div class="wf-tl">${rows}</div>`;
}

function setView(v) {
  closePop();       // a popover belongs to the view it was opened on
  sbPanel(false);   // on a phone, what was chosen in the full-screen panel is now on screen
  if (v !== 'dash' && _dashIv) { clearInterval(_dashIv); _dashIv = null; }  // stop the live feed poll when leaving the Dashboard
  // Work Map owns a resize listener and an in-flight fetch; drop them when we
  // leave the view so neither escapes into other screens.
  if (v !== 'workmap' && typeof window.teardownWorkMap === 'function') { try { window.teardownWorkMap(); } catch (e) {} }
  const show = (sel, on) => { const el = $(sel); if (el) el.style.display = on ? '' : 'none'; };
  show('#task', v === 'chats'); show('#tabs', v === 'chats');
  show('#tasksView', v === 'tasks'); show('#taskDetail', v === 'detail'); show('#dashView', v === 'dash'); show('#tasksDashView', v === 'tasksdash');
  show('#docsView', v === 'docs');
  show('#agentPage', v === 'agent');
  show('#agentsView', v === 'agents');
  show('#workflowsView', v === 'workflows');
  show('#settingsView', v === 'settings');
  show('#missingView', v === 'missing');
  show('#workMapView', v === 'workmap');
  show('#calendarView', v === 'calendar');
  const navOf = { calendar: '#navCalendar', chats: '#navChats', tasks: '#navTasks', detail: '#navTasks', tasksdash: '#navPlannerDash', dash: '#navDash', docs: '#navDocs', docdetail: '#navDocs', agents: '#navAgents', workflows: '#navWorkflows', settings: '#navSettings', workmap: '#navWorkMap' };
  ['#navCalendar', '#navChats', '#navTasks', '#navPlannerDash', '#navDash', '#navDocs', '#navAgents', '#navWorkflows', '#navSettings', '#navWorkMap'].forEach((s) => { const e = $(s); if (e) e.classList.remove('is-active'); });
  const a = $(navOf[v]); if (a) a.classList.add('is-active');
  document.querySelectorAll('#tabbar [data-for]').forEach((b) => b.classList.toggle('is-active', b.dataset.for === navOf[v]));
  // Chats shown from '/', the Chats nav, or any path that names no chat: give the
  // address the active chat's link. Not when the path already names a chat, so a
  // tab click's fresh /chats/<new> is never overwritten with the old id before
  // setActive() moves activeId.
  if (v === 'chats' && !/^\/chats\/[^/]+$/.test(snPath())) syncChatUrl();
  // The chat drawn while another view was on screen had no height, and the
  // pin to its end lasts 2.5 s: shown later it sat at the top with dozens of
  // messages below (2026-10-30). Shown now, it goes where it was left, or to the end.
  if (v === 'chats' && _wasHidden && activeId && _threadFor === activeId) {
    const sc = $('#thread') && $('#thread').parentElement, m = scrollMemGet(activeId);
    if (sc) requestAnimationFrame(() => { if (m && !m.atBottom) restoreTop(sc, m.top); else pinBottom(sc); });
  }
  _wasHidden = v !== 'chats';
}
let _wasHidden = true;

function tkChip(kind, v) {
  if (!v) return '<span class="tkt__mut">&mdash;</span>';
  return `<span class="tkt__chip tkt__chip--${kind}-${esc(v)}">${esc(v)}</span>`;
}
function tkDate(v) {
  if (!v) return '<span class="tkt__mut">&mdash;</span>';
  const d = new Date(v); if (isNaN(d)) return esc(v);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* --- Tasks list --- */
function tkWorkflow(t) {
  const wf = t.workflow_id != null ? _wfMap[t.workflow_id] : null;
  if (!wf) return '<span class="tkt__mut">&mdash;</span>';
  return `<span class="tkt__tag tkt__tag--wf" title="Flow / process">${esc(wf)}</span>`;
}
function tkProjGoal(t) {
  const proj = t.project_id != null ? _projMap[t.project_id] : null;
  const goal = t.objective_id != null ? _objMap[t.objective_id] : null;
  if (!proj && !goal) return '<span class="tkt__mut">&mdash;</span>';
  let h = '';
  if (proj) h += `<span class="tkt__tag tkt__tag--proj" title="Project">${esc(proj)}</span>`;
  if (goal) h += `<span class="tkt__tag tkt__tag--goal" title="Goal">${esc(goal)}</span>`;
  return h;
}
function tkRowHTML(t) {
  return `<tr data-id="${t.id}"><td class="tkt__id">${t.id}</td><td class="tkt__title" title="${esc(t.title)}">${esc(t.title)}</td><td class="tkt__pg">${tkProjGoal(t)}</td><td class="tkt__wf">${tkWorkflow(t)}</td><td>${tkStatusChip(t)}</td><td>${tkChip('p', t.priority)}</td><td>${t.assignee ? esc(t.assignee) : '<span class="tkt__mut">&mdash;</span>'}</td><td>${t.requester ? esc(t.requester) : '<span class="tkt__mut">&mdash;</span>'}</td><td>${t.due_date ? esc(t.due_date) : '<span class="tkt__mut">&mdash;</span>'}</td><td>${tkDate(t.updated_at)}</td></tr>`;
}
function tkTableHTML(rows) {
  return `<table class="tkt"><thead><tr><th style="width:46px">#</th><th style="width:360px">Title</th><th style="width:200px">Project / Goal</th><th style="width:150px">Flow</th><th style="width:150px">Status</th><th style="width:96px">Priority</th><th style="width:150px">Assignee</th><th style="width:130px">Created by</th><th style="width:96px">Due</th><th style="width:112px">Updated</th></tr></thead><tbody>${rows.length ? rows.map(tkRowHTML).join('') : '<tr><td colspan="10" class="tkt__empty">No tasks.</td></tr>'}</tbody></table>`;
}
// Who it is assigned to (the assignee, not who filed it): a row of chips over
// the list, a name and the number of their tasks, no avatar to keep it short; several can be on at once, none on is everyone. It was a text field the name had to be typed into exactly. The
// choice is kept across visits; a chosen name with no tasks left stays as a chip.
const TK_WHO_KEY = 'sn-tasks-who';
const TK_NOBODY = '';   // the chip of the tasks with no assignee
let _tkWho = new Set((() => {
  try { const v = JSON.parse(localStorage.getItem(TK_WHO_KEY) || '[]'); return Array.isArray(v) ? v.map(String) : []; } catch (_) { return []; }
})());
const tkWhoOf = (t) => String(t.assignee || TK_NOBODY);
function tkWhoPaint() {
  const box = $('#tkWho');
  if (!box) return;
  const n = new Map();
  for (const t of _tasksAll) n.set(tkWhoOf(t), (n.get(tkWhoOf(t)) || 0) + 1);
  for (const w of _tkWho) if (!n.has(w)) n.set(w, 0);
  const who = [...n.keys()].sort((a, b) => (a === TK_NOBODY) - (b === TK_NOBODY) || n.get(b) - n.get(a) || a.localeCompare(b));
  const chip = (w) => {
    const ag = w ? agentBySlug(w) : null;
    const nm = w ? ((ag && ag.title) || w.charAt(0).toUpperCase() + w.slice(1)) : 'Unassigned';
    const on = _tkWho.has(w);
    return `<button type="button" class="tkwho__c${on ? ' is-on' : ''}" data-w="${esc(w)}" aria-pressed="${on}">`
      + `${esc(nm)}<b>${n.get(w)}</b></button>`;
  };
  box.innerHTML = `<button type="button" class="tkwho__c tkwho__c--all${_tkWho.size ? '' : ' is-on'}" data-all aria-pressed="${!_tkWho.size}">Anyone</button>`
    + who.map(chip).join('');
}
function tkWhoToggle(e) {
  const b = e.target.closest('.tkwho__c');
  if (!b) return;
  if (b.hasAttribute('data-all')) _tkWho.clear();
  else if (_tkWho.has(b.dataset.w)) _tkWho.delete(b.dataset.w);
  else _tkWho.add(b.dataset.w);
  try { localStorage.setItem(TK_WHO_KEY, JSON.stringify([..._tkWho])); } catch (_) {}
  tkRender();
}
function tkRender() {
  const st = $('#fStatus').value, pr = $('#fPriority').value;
  tkWhoPaint();
  const rows = _tasksAll.filter((t) => tkStatusMatch(t, st) && (!pr || t.priority === pr) && (!_tkWho.size || _tkWho.has(tkWhoOf(t))));
  $('#tkCount').textContent = `${rows.length} of ${_tasksAll.length}`;
  const body = $('#tkBody');
  if (!rows.length) { body.innerHTML = '<tr><td colspan="10" class="tkt__empty">No tasks match.</td></tr>'; return; }
  body.innerHTML = rows.map(tkRowHTML).join('');
  body.querySelectorAll('tr[data-id]').forEach((tr) => tr.addEventListener('click', () => openTask(tr.dataset.id)));
}
async function loadTasks() {
  const body = $('#tkBody');
  try {
    const [tr, pr, orr, wr] = await Promise.all([
      fetch('/api/tasks', { headers: { Accept: 'application/json' } }),
      fetch('/api/projects', { headers: { Accept: 'application/json' } }),
      fetch('/api/objectives', { headers: { Accept: 'application/json' } }),
      fetch('/api/workflows', { headers: { Accept: 'application/json' } }),
    ]);
    if (!tr.ok) throw new Error(`API ${tr.status}`);
    _tasksAll = await tr.json();
    if (!Array.isArray(_tasksAll)) _tasksAll = [];
    try { const p = pr.ok ? await pr.json() : []; _projMap = {}; (Array.isArray(p) ? p : []).forEach((x) => { _projMap[x.id] = x.title || x.slug || ('#' + x.id); }); } catch (_) {}
    try { const o = orr.ok ? await orr.json() : []; _objMap = {}; (Array.isArray(o) ? o : []).forEach((x) => { _objMap[x.id] = x.title || x.name || ('#' + x.id); }); } catch (_) {}
    try { const w = wr.ok ? await wr.json() : []; const wl = Array.isArray(w) ? w : (w && Array.isArray(w.workflows) ? w.workflows : []); _wfMap = {}; wl.forEach((x) => { _wfMap[x.id] = x.title || x.name || ('#' + x.id); }); } catch (_) {}
    tkRender();
  } catch (e) {
    body.innerHTML = `<tr><td colspan="10" class="tkt__empty">Could not load tasks: ${esc(e.message)}</td></tr>`;
    $('#tkCount').textContent = 'error';
  }
}

/* --- Task detail --- */
function openTask(id) { history.pushState({}, '', `/tasks/${id}`); setView('detail'); loadDetail(id); }
// The overview opens a task in a side drawer instead of leaving the page. Same card as the task page; "Open page" goes there.
function openTaskDrawer(id) {
  const dr = $('#taskDrawer'), sc = $('#tdrwScrim');
  if (!dr) { openTask(id); return; }
  dr.dataset.id = id;
  // closed it is inert: off screen, it stayed in the Tab order (interface map, 2026-10-30)
  dr.inert = false;
  dr.classList.add('is-open'); if (sc) sc.classList.add('is-open');
  loadDetail(id, $('#tdrwBody'), $('#tdrwTitle'));
}
function closeTaskDrawer() {
  const dr = $('#taskDrawer'), sc = $('#tdrwScrim');
  if (dr) { dr.classList.remove('is-open'); dr.inert = true; } if (sc) sc.classList.remove('is-open');
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && $('#taskDrawer') && $('#taskDrawer').classList.contains('is-open')) closeTaskDrawer(); });
document.addEventListener('click', (e) => {
  if (e.target.closest('#tdrwClose') || e.target.id === 'tdrwScrim') closeTaskDrawer();
  else if (e.target.closest('#tdrwPage')) { const id = $('#taskDrawer').dataset.id; closeTaskDrawer(); openTask(id); }
});
// One labelled cell in the property grid. `full` (optional) is the untruncated
// value, surfaced on hover when the cell ellipsizes.
function tdProp(label, val, full) {
  return `<div class="td__prop"><i>${label}</i><span${full ? ` title="${esc(full)}"` : ''}>${val}</span></div>`;
}
// Climb objective parents until a type:strategy ancestor is found.
function resolveStrategy(objId, objMap) {
  let cur = objMap[objId];
  for (let guard = 0; cur && guard < 20; guard++) {
    if (cur.type === 'strategy') return cur.title || cur.name || ('#' + cur.id);
    cur = cur.parent_id != null ? objMap[cur.parent_id] : null;
  }
  return null;
}
// Renders one task into `host` (the task page by default, or the overview's side
// drawer), so the card and its approve/review gates exist in one place.
async function loadDetail(id, host, titleEl) {
  host = host || $('#tdBody'); titleEl = titleEl || $('#tdTitle');
  titleEl.textContent = `Task #${id}`;
  host.innerHTML = skText(7);
  try {
    const [tr, cr, pr, orr] = await Promise.all([
      fetch(`/api/tasks/${encodeURIComponent(id)}`, { headers: { Accept: 'application/json' } }),
      fetch(`/api/tasks/${encodeURIComponent(id)}/comments`, { headers: { Accept: 'application/json' } }),
      fetch('/api/projects', { headers: { Accept: 'application/json' } }),
      fetch('/api/objectives', { headers: { Accept: 'application/json' } }),
    ]);
    if (!tr.ok) throw new Error(`API ${tr.status}`);
    const t = await tr.json();
    let comments = []; try { if (cr.ok) comments = await cr.json(); } catch (_) {}
    if (!Array.isArray(comments)) comments = [];
    // Resolve project / objective / strategy names so the card shows labels,
    // not bare ids. Lists are small; failures degrade to "#id".
    let projects = []; try { if (pr.ok) projects = await pr.json(); } catch (_) {}
    let objectives = []; try { if (orr.ok) objectives = await orr.json(); } catch (_) {}
    const projMap = {}; (Array.isArray(projects) ? projects : []).forEach((p) => { projMap[p.id] = p.title || p.slug || ('#' + p.id); });
    const objMap = {}; (Array.isArray(objectives) ? objectives : []).forEach((o) => { objMap[o.id] = o; });
    const projName = t.project_id != null ? (projMap[t.project_id] || ('#' + t.project_id)) : null;
    const objName = t.objective_id != null && objMap[t.objective_id]
      ? (objMap[t.objective_id].title || objMap[t.objective_id].name || ('#' + t.objective_id)) : null;
    const stratName = t.objective_id != null ? resolveStrategy(t.objective_id, objMap) : null;
    const dash = '<span class="tkt__mut">&mdash;</span>';
    const cText = (c) => c.content || c.body || c.text || '';
    const commentsHtml = comments.length
      ? comments.map((c) => `<div class="td__cmt"><div class="td__cmt-h"><span class="td__cmt-a">${esc(c.author || c.actor_id || '—')}</span>${c.comment_type ? ` <span class="tkt__id">${esc(c.comment_type)}</span>` : ''}<span class="tkt__mut">${tkDate(c.created_at)}</span></div><div class="td__cmt-b docbody">${window.snFormatter.markdown(cText(c))}</div></div>`).join('')
      : '<div class="tkt__mut">No comments.</div>';
    // Which gate to show is one decision (tdReviewGate), taken from the task,
    // the comment feed and who is looking at the card. Rendering routes on the
    // kind so a new gate never has to guess where in the if-chain it belongs.
    const _gate = tdReviewGate(t, comments, meIdentity.user, meIdentity.handle);
    let gateHtml = '';
    if (_gate.kind === 'draftApproval' || _gate.kind === 'statusApproval') {
      const _statusGate = _gate.kind === 'statusApproval';
      const _gateMsg = _statusGate
        ? 'This task needs your approval before it proceeds. Approve to let it run, or reject to stop it.'
        : 'A reply is drafted and held. Approve to send it to the customer, or reject to discard.';
      // A task cannot start with nobody to do it (the planner refuses it), so a task that
      // has no doer yet asks the owner to name one with the approval. The filer's proposal,
      // when it named one, is already chosen.
      const _doerChoices = Array.from(new Set([...(agentsList || []).map((a) => a.slug).filter(Boolean), 'admin'])).sort();
      const _doerHtml = (_statusGate && !t.assignee)
        ? `<label class="tkt__mut" style="flex:1 1 100%">Who does it: <select id="tdDoer" class="td__doer"><option value="">-- choose --</option>${_doerChoices.map((n) => `<option value="${esc(n)}"${n === t.proposed_assignee ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select></label>`
        : '';
      gateHtml = `<div class="td__sec">Owner approval</div>
      <div class="td__gate" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0">
        <span class="tkt__mut" style="flex:1 1 100%">${_gateMsg}</span>
        ${_doerHtml}
        <button class="sn-btn sn-btn--primary" id="tdApprove" style="min-width:128px">Approve</button>
        ${_statusGate ? '<button class="sn-btn sn-btn--ghost" id="tdModify" style="min-width:128px">Modify</button>' : ''}
        <button class="sn-btn sn-btn--ghost" id="tdReject" style="min-width:128px">${_statusGate ? 'Reject' : 'Decline'}</button>
        <div id="tdNoteWrap" style="display:none;flex:1 1 100%;margin-top:6px">
          <textarea id="tdNote" rows="3" style="width:100%" placeholder="What should change, or why this is rejected"></textarea>
          <div style="display:flex;gap:8px;margin-top:6px">
            <button class="sn-btn sn-btn--primary" id="tdNoteSend" style="min-width:128px">Send</button>
            <button class="sn-btn sn-btn--ghost" id="tdNoteCancel">Cancel</button>
          </div>
        </div>
      </div>`;
    } else if (_gate.kind === 'reviewerClose') {
      // pending_review: only the reviewer can accept it into done (the
      // confirmer is never the finder; planner refuses if actor != reviewer).
      // Done requires a comment quoting evidence, so the button opens a note box
      // rather than firing straight -- silence would fail at the planner. A send-
      // back to in_progress is the other legal action here, and it needs a note
      // too so the assignee reads WHY it came back.
      gateHtml = `<div class="td__sec">Review</div>
      <div class="td__gate" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0">
        <span class="tkt__mut" style="flex:1 1 100%">This task is in pending_review and you are its reviewer. Close to done with a report, or send it back for more work.</span>
        <button class="sn-btn sn-btn--primary" id="tdClose" style="min-width:128px">Close to done</button>
        <button class="sn-btn sn-btn--ghost" id="tdSendBack" style="min-width:128px">Send back</button>
        <div id="tdNoteWrap" style="display:none;flex:1 1 100%;margin-top:6px">
          <textarea id="tdNote" rows="3" style="width:100%" placeholder="Report or note"></textarea>
          <div style="display:flex;gap:8px;margin-top:6px">
            <button class="sn-btn sn-btn--primary" id="tdNoteSend" style="min-width:128px">Send</button>
            <button class="sn-btn sn-btn--ghost" id="tdNoteCancel">Cancel</button>
          </div>
        </div>
      </div>`;
    } else if (_gate.kind === 'sent') {
      gateHtml = `<div class="td__sec">Owner approval</div><div class="td__gate"><span class="tkt__mut">Approved &mdash; sent to the customer.</span></div>`;
    } else if (_gate.kind === 'decided') {
      gateHtml = `<div class="td__sec">Owner approval</div><div class="td__gate"><span class="tkt__mut">${_gate.rejected ? 'Rejected &mdash; nothing sent.' : 'Approved.'}</span></div>`;
    }
    host.innerHTML = `
      <div class="td__crumb"><span class="tkt__id">#${t.id}</span></div>
      <h2 class="td__h">${esc(t.title)}</h2>
      <div class="td__props">
        ${tdProp('Status', tkStatusChip(t))}
        ${tdProp('Priority', tkChip('p', t.priority))}
        ${tdProp('Project', projName ? esc(projName) : dash, projName)}
        ${tdProp('Strategy', stratName ? esc(stratName) : dash, stratName)}
        ${tdProp('Objective', objName ? esc(objName) : dash, objName)}
        ${tdProp('Assignee', t.assignee ? esc(t.assignee) : dash)}
        ${tdProp('Requester', t.requester ? esc(t.requester) : dash)}
        ${tdProp('Complexity', t.complexity_hint && t.complexity_hint !== 'unknown' ? esc(t.complexity_hint) : dash)}
        ${tdProp('Created', t.created_at ? tkDate(t.created_at) : dash)}
        ${tdProp('Updated', t.updated_at ? tkDate(t.updated_at) : dash)}
        ${tdProp('Due', t.due_date ? esc(t.due_date) : dash)}
        ${tdProp('ETA', t.eta ? esc(t.eta) : dash)}
        ${tdProp('Assessed', t.assessed_at ? tkDate(t.assessed_at) : dash)}
        ${tdProp('Approved', t.approved_at ? tkDate(t.approved_at) : dash)}
        ${tdProp('Started', t.execution_started_at ? tkDate(t.execution_started_at) : dash)}
        ${tdProp('Completed', t.completed_at ? tkDate(t.completed_at) : dash)}
        ${tdProp('Workflow', t.workflow_id != null ? `<span class="tkt__id">${esc(t.workflow_id)}</span>` : dash, t.workflow_id != null ? String(t.workflow_id) : '')}
        ${tdProp('Scope', t.scope ? esc(t.scope) : dash, t.scope)}
        ${t.blocked_by != null ? tdProp('Blocked by', `task #${esc(t.blocked_by)}`) : ''}
        ${t.blocked_reason ? tdProp('Blocked reason', esc(t.blocked_reason)) : ''}
        ${t.external_detail ? tdProp('External', esc(t.external_detail), t.external_detail) : ''}
        ${t.verification_attempts > 0 ? tdProp('Review attempts', esc(t.verification_attempts)) : ''}
        ${t.previous_status ? tdProp('Previous', tkChip('s', t.previous_status)) : ''}
      </div>
      ${gateHtml}
      <div class="td__sec">Description</div>
      <div class="td__desc docbody">${t.description ? window.snFormatter.markdown(t.description) : '<span class="tkt__mut">No description.</span>'}</div>
      <div class="td__sec">Comments <span class="tkt__mut">(${comments.length})</span></div>
      <div class="td__cmtnew" style="margin:6px 0 12px">
        <textarea id="tdCmt" rows="3" style="width:100%;box-sizing:border-box;resize:vertical;border:1px solid var(--sn-rule);border-radius:var(--sn-r-md);background:var(--sn-surface);color:var(--sn-ink);padding:10px 12px;font:400 13.5px/1.5 var(--sn-font);outline:none" placeholder="Write a comment. @name tells that agent, for example: @john what is left here?"></textarea>
        <div style="display:flex;gap:8px;margin-top:6px;align-items:center;justify-content:flex-end">
          <span class="tkt__mut" id="tdCmtMsg" style="flex:1">Ctrl+Enter sends. Newest comments first.</span>
          <button class="sn-btn sn-btn--primary" id="tdCmtSend" style="min-width:128px">Send</button>
        </div>
      </div>
      <div class="td__cmts">${commentsHtml}</div>`;
    window.snFormatter.bindHtmlPreviews(host);
    // Comment box. Actor ids are lower case and a mention matches the id exactly,
    // so "@Dana" would tell nobody: the name after @ is lower-cased before it is
    // posted, with the planner's own pattern, so an e-mail address is left alone.
    {
      // Newest first: the box sits right under the description, and the newest
      // comment right under the box, so a question and its answer are both in
      // view without scrolling. While the card is open, new comments are fetched
      // every 4 s (tab visible only) and put on top; the card is never redrawn.
      const box = host.querySelector('#tdCmt'), send = host.querySelector('#tdCmtSend'), msg = host.querySelector('#tdCmtMsg');
      const list = host.querySelector('.td__cmts');
      const countEl = [...host.querySelectorAll('.td__sec')].find((e) => e.textContent.trim().startsWith('Comments'));
      const known = new Set(comments.map((c) => c.id));
      let total = comments.length;
      const one = (c) => `<div class="td__cmt"><div class="td__cmt-h"><span class="td__cmt-a">${esc(c.author || c.actor_id || '—')}</span>${c.comment_type ? ` <span class="tkt__id">${esc(c.comment_type)}</span>` : ''}<span class="tkt__mut">${tkDate(c.created_at)}</span></div><div class="td__cmt-b docbody">${window.snFormatter.markdown(cText(c))}</div></div>`;
      // Reply: the box gets the comment quoted and its author mentioned, so the
      // author is told and the reply says what it answers. The planner skips
      // names inside the quote, so only the author outside it is woken.
      const quiet = new Set(['admin', 'owner', 'system', 'runner', 'voice', String(meIdentity.user || '').split('@')[0]]);
      const reply = (c) => {
        const flat = ' ' + cText(c).split('\n').filter((l) => !l.trim().startsWith('>')).join(' ').replace(/\s+/g, ' ').trim();
        const quote = '> ' + (c.author || '') + ':' + (flat.length > 240 ? flat.slice(0, 237) + '...' : flat);
        const who = c.author && !quiet.has(c.author) ? '@' + c.author + ' ' : '';
        box.value = quote + '\n\n' + who + (box.value.trim() ? box.value.trim() + ' ' : '');
        box.scrollIntoView({ block: 'center', behavior: 'smooth' });
        box.focus({ preventScroll: true }); box.setSelectionRange(box.value.length, box.value.length);
      };
      const deco = (el, c) => {
        const h = el && el.querySelector('.td__cmt-h');
        if (!h || h.querySelector('.td__cmt-re')) return;
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'td__cmt-re'; b.textContent = 'Reply';
        b.style.cssText = 'border:none;background:none;color:var(--sn-ink-mute);font:500 12px var(--sn-font);cursor:pointer;padding:0;margin-left:10px';
        b.addEventListener('click', () => reply(c));
        (h.lastElementChild || h).appendChild(b);
      };
      if (list) {
        const els = [...list.querySelectorAll(':scope > .td__cmt')];
        els.forEach((el, i) => deco(el, comments[i]));
        els.reverse().forEach((el) => list.appendChild(el));
      }
      const add = (rows) => {
        const fresh = (Array.isArray(rows) ? rows : []).filter((c) => c && c.id != null && !known.has(c.id));
        if (!fresh.length || !list) return;
        if (!total) list.innerHTML = '';
        fresh.forEach((c) => { known.add(c.id); list.insertAdjacentHTML('afterbegin', one(c)); deco(list.firstElementChild, c); });
        total += fresh.length;
        if (countEl) countEl.innerHTML = `Comments <span class="tkt__mut">(${total})</span>`;
        window.snFormatter.bindHtmlPreviews(list);
      };
      const pull = async () => {
        try {
          const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/comments`, { headers: { Accept: 'application/json' } });
          if (r.ok) add(await r.json());
        } catch (_) {}
      };
      if (host._tdPoll) clearInterval(host._tdPoll);
      host._tdPoll = setInterval(() => {
        if (!host.isConnected || !list || !list.isConnected) { clearInterval(host._tdPoll); return; }
        if (document.hidden || host.offsetParent === null) return;
        pull();
      }, 4000);
      const post = async () => {
        const text = (box.value || '').trim();
        if (!text) { box.focus(); return; }
        const content = text.replace(/(^|[^\w@./-])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})/g, (m, pre, name) => pre + '@' + name.toLowerCase());
        send.disabled = true; msg.textContent = 'Sending...';
        try {
          const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/comments`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ content }),
          });
          if (!r.ok) {
            let detail = 'API ' + r.status;
            try { const j = await r.json(); if (j && j.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (_) {}
            throw new Error(detail);
          }
          let mine = null; try { mine = await r.json(); } catch (_) {}
          box.value = '';
          msg.textContent = /(^|[^\w@./-])@[a-z0-9]/.test(content) ? 'Sent. The reply will appear right below.' : 'Sent.';
          if (mine && mine.id != null) add([mine]); else pull();
        } catch (e) {
          msg.textContent = 'Not sent: ' + e.message;
        }
        send.disabled = false;
      };
      if (send) send.addEventListener('click', post);
      if (box) box.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); post(); } });
    }
    const _ap = host.querySelector('#tdApprove'), _rj = host.querySelector('#tdReject'), _md = host.querySelector('#tdModify');
    const _cl = host.querySelector('#tdClose'), _sb = host.querySelector('#tdSendBack');
    const _btns = [_ap, _rj, _md, _cl, _sb];
    // Modify and Reject (owner-approval gate), and Close-to-done / Send-back
    // (reviewer gate), all need a note. Reveal the box, focus it, then dispatch.
    // Approve does not: silence is a complete answer when the answer is yes.
    const _statusGate = _gate.kind === 'statusApproval';
    const _ask = (decision, placeholder) => {
      const wrap = host.querySelector('#tdNoteWrap'), note = host.querySelector('#tdNote'), send = host.querySelector('#tdNoteSend'), cancel = host.querySelector('#tdNoteCancel');
      if (!wrap) return;
      wrap.style.display = 'block';
      note.placeholder = placeholder;
      note.focus();
      send.onclick = () => {
        const text = (note.value || '').trim();
        if (!text) { note.focus(); return; }
        gateDecide(id, decision, _btns, _statusGate, text, host);
      };
      cancel.onclick = () => { wrap.style.display = 'none'; };
    };
    if (_ap) _ap.addEventListener('click', () => gateDecide(id, 'approve', _btns, _statusGate, undefined, host));
    if (_md) _md.addEventListener('click', () => _ask('modify', 'What should change'));
    if (_rj) _rj.addEventListener('click', () => (_statusGate ? _ask('reject', 'Why this is rejected') : gateDecide(id, 'reject', _btns, false, undefined, host)));
    if (_cl) _cl.addEventListener('click', () => _ask('close', 'Report: quote the evidence for each acceptance item'));
    if (_sb) _sb.addEventListener('click', () => _ask('send_back', 'Why it goes back and what to fix'));
  } catch (e) {
    host.innerHTML = `<div class="tkt__empty">Could not load task: ${esc(e.message)}</div>`;
  }
}

// The owner answers a gate. Three shapes, because there are three gates.
//
// A task that is itself waiting (awaiting_approval) goes through the planner's
// decision endpoints: one call that records the decision, moves the task and
// tags it, with the planner setting approved_at itself. Studio used to assemble
// that by hand as a comment plus a PATCH carrying approved_at, and the PATCH
// schema forbids the field, so the press failed AFTER the comment was written
// and the owner was told their decision was not recorded when half of it was.
//
// A held draft reply is not a task status, so it stays a comment: the Runner's
// approval pass reads that comment and sends or discards.
//
// A pending_review task closes with a PATCH {status:'done', comment:<report>}:
// the planner enforces actor == reviewer at that gate, so the button
// only shows to that reviewer, and a wrong caller is refused with the planner's
// own error surfaced back into the page. Send-back is the same PATCH shape with
// status:'in_progress' and a note explaining what to fix. Both had no button in
// the Studio panel, so a reviewer had to shell out to close their own task; the
// task review was blocked on that missing surface.
// The body of an approve / modify / reject: the note, and the doer the owner chose on the
// card when the task had none (the planner refuses to start work on a task with nobody to do it).
function decisionBody(note, host) {
  const body = note ? { note } : {};
  const doer = host && host.querySelector ? host.querySelector('#tdDoer') : null;
  if (doer && doer.value) body.assignee = doer.value;
  return body;
}

async function gateDecide(id, decision, btns, statusGate, note, host) {
  (btns || []).forEach((b) => { if (b) b.disabled = true; });
  try {
    if (decision === 'close' || decision === 'send_back') {
      const nextStatus = decision === 'close' ? 'done' : 'in_progress';
      const body = { status: nextStatus };
      if (note) body.comment = note;
      const r = await fetch(`/api/tasks/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        let detail = 'API ' + r.status;
        try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
        throw new Error(detail);
      }
    } else if (statusGate) {
      const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/${decision}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(decisionBody(note, host)),
      });
      if (!r.ok) {
        let detail = 'API ' + r.status;
        try { const j = await r.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
        throw new Error(detail);
      }
    } else {
      const r = await fetch(`/api/tasks/${encodeURIComponent(id)}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ content: decision, comment_type: 'approval' }),
      });
      if (!r.ok) throw new Error('API ' + r.status);
    }
    loadDetail(id, host);
  } catch (e) {
    alert('Could not record your decision: ' + e.message);
    (btns || []).forEach((b) => { if (b) b.disabled = false; });
  }
}

// One place decides which gate the card shows: an approval gate for the owner
// (awaiting_approval), or a reviewer gate for the person named reviewer on a
// pending_review card, or nothing. The point of routing on kind is that a new
// gate never has to guess where in an if-chain it belongs -- the caller matches
// the kind and stops.
//
// The viewer match is by local-part, the same shape dshIsOwner uses: the panel
// is served behind Authum which stamps Remote-User with an address like
// admin@acme.test, but the planner stores agent slugs (admin, architect, agent-b), and
// pinning the compare to the local part means the button works on any node
// without a hardcoded domain living in this file.
function tdReviewGate(t, comments, viewer, handle) {
  const list = Array.isArray(comments) ? comments : [];
  const hasDraft = list.some((c) => c && c.comment_type === 'draft');
  const decided = list.find((c) => c && c.comment_type === 'approval');
  const sent = list.find((c) => c && c.comment_type === 'sent');
  const status = tkStage(t);
  if (sent) return { kind: 'sent' };
  if (status === 'awaiting_approval' && !hasDraft && !decided) {
    return { kind: 'statusApproval' };
  }
  if (hasDraft && !decided) return { kind: 'draftApproval' };
  if (decided) {
    const txt = (decided.content || decided.body || decided.text || '').trim().toLowerCase();
    return { kind: 'decided', rejected: txt.startsWith('reject') };
  }
  if (status === 'pending_review'
      && (tdViewerMatches(viewer, t && t.reviewer) || tdViewerMatches(handle, t && t.reviewer))) {
    return { kind: 'reviewerClose' };
  }
  return { kind: 'none' };
}

// Does the signed-in user match the actor named on the task? The panel receives
// an address (Authum), the planner stores a slug: the compare uses the local
// part on both sides, mirroring dshIsOwner, so this file never names a domain.
// Empty on either side is not a match: a card whose reviewer is unset must not
// grant its buttons to whoever happens to be looking.
function tdViewerMatches(viewer, actor) {
  const v = String(viewer || '').toLowerCase().trim();
  const a = String(actor || '').toLowerCase().trim();
  if (!v || !a) return false;
  return v === a || v.split('@')[0] === a.split('@')[0];
}

/* --- Dashboard: metrics computed client-side from the task list --- */
function dshRow(t) {
  return `<div class="dsh__item" data-id="${t.id}"><span class="tkt__id">${t.id}</span><span class="dsh__it-t">${esc(t.title)}</span>${tkStatusChip(t)}</div>`;
}
function pgRing(pct) {
  const r = 17, c = 2 * Math.PI * r, off = c * (1 - Math.max(0, Math.min(100, pct)) / 100);
  const lvl = pct >= 80 ? 'hi' : pct >= 40 ? 'mid' : 'lo';
  return `<svg class="pgc__ring pgc__ring--${lvl}" width="46" height="46" viewBox="0 0 46 46">
      <circle class="pgc__track" cx="23" cy="23" r="${r}"></circle>
      <circle class="pgc__val" cx="23" cy="23" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" transform="rotate(-90 23 23)"></circle>
      <text class="pgc__pct" x="23" y="23" dominant-baseline="central" text-anchor="middle">${pct}%</text>
    </svg>`;
}
function pgCard(o) {
  return `<div class="pgc__card">${pgRing(o.pct)}<div class="pgc__b"><div class="pgc__t">${esc(o.title)}</div><div class="pgc__m">${o.done}/${o.total} tasks</div>${o.desc ? `<div class="pgc__d">${esc(o.desc)}</div>` : ''}</div></div>`;
}
let _td = { tasks: [], projects: [], objectives: [] };
let _tdStrat = null; // selected strategy id; null = all
let _tdGoal = null; // selected goal id; null = none
let _tdProj = null; // selected project id; null = none
let _tdRange = null; // time window in days; null = all
// the overview shows live objectives only, freshest first, a page at a time.
const TD_GOALS_STEP = 8, TD_TASKS_PAGE = 50;
let _tdClosed = false; // show completed/cancelled objectives too
let _tdGoalsN = TD_GOALS_STEP; // goal cards shown
let _tdCols = 0; // goal grid columns at the current width; the page shows whole rows
// Collapsed sections, remembered in this browser.
let _tdFold = { strat: false, proj: false, goals: false };
try { const f = JSON.parse(localStorage.getItem('sn-tdash-fold') || '{}'); _tdFold = { strat: !!f.strat, proj: !!f.proj, goals: !!f.goals }; } catch (e) {}
let _tdProjAll = false; // projects strip: two rows, or all of them
let _tdPCols = 0; // projects strip columns at the current width
const TD_CHEV = '<svg class="tdx__chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
let _tdFind = ''; // goal title filter
let _tdPage = 0; // task table page
async function loadTasksDash() {
  const body = $('#tasksDashBody');
  body.innerHTML = tasksDashSkel();
  try {
    const [tr, pr, orr, wr] = await Promise.all([
      // every task, not the planner's default page of 100
      fetch('/api/tasks/overview', { headers: { Accept: 'application/json' } }),
      fetch('/api/projects', { headers: { Accept: 'application/json' } }),
      fetch('/api/objectives', { headers: { Accept: 'application/json' } }),
      fetch('/api/workflows', { headers: { Accept: 'application/json' } }),
    ]);
    if (!tr.ok) throw new Error(`tasks: HTTP ${tr.status}`);
    _td.tasks = await tr.json();
    _td.projects = pr.ok ? await pr.json() : [];
    _td.objectives = orr.ok ? await orr.json() : [];
    let wf = []; try { wf = wr.ok ? await wr.json() : []; } catch (_) {}
    const wfl = Array.isArray(wf) ? wf : (wf && Array.isArray(wf.workflows) ? wf.workflows : []);
    _projMap = {}; (Array.isArray(_td.projects) ? _td.projects : []).forEach((p) => { _projMap[p.id] = p.title || p.slug || ('#' + p.id); });
    _objMap = {}; (Array.isArray(_td.objectives) ? _td.objectives : []).forEach((o) => { _objMap[o.id] = o.title || o.name || ('#' + o.id); });
    _wfMap = {}; wfl.forEach((x) => { _wfMap[x.id] = x.title || x.name || ('#' + x.id); });
    _tdStrat = null; _tdGoal = null; _tdProj = null; _tdGoalsN = 2 * (_tdCols || 4); _tdFind = ''; _tdPage = 0;
    renderTasksDash();
  } catch (e) {
    body.innerHTML = `<div class="tkt__empty">Could not load overview: ${esc(e.message)}</div>`;
  }
}
function tlChartHTML(tasks, windowDays) {
  const now = Date.now(), day = 86400000, span = windowDays * day, start = now - span;
  const bucketDays = windowDays <= 14 ? 1 : (windowDays <= 60 ? 3 : 7);
  const nb = Math.max(2, Math.ceil(windowDays / bucketDays));
  const created = new Array(nb).fill(0), completed = new Array(nb).fill(0);
  const idx = (ms) => { const i = Math.floor((ms - start) / (bucketDays * day)); return i >= 0 && i < nb ? i : -1; };
  tasks.forEach((t) => {
    if (t.created_at) { const i = idx(new Date(t.created_at).getTime()); if (i >= 0) created[i]++; }
    if (t.completed_at) { const i = idx(new Date(t.completed_at).getTime()); if (i >= 0) completed[i]++; }
  });
  if (created.reduce((a, b) => a + b, 0) + completed.reduce((a, b) => a + b, 0) === 0)
    return '<div class="tkt__mut" style="padding:14px 4px">No activity in this range.</div>';
  const max = Math.max(1, ...created, ...completed), W = 1000, H = 100, pT = 6, pB = 6;
  const x = (i) => (i / (nb - 1)) * W;
  const y = (v) => pT + (1 - v / max) * (H - pT - pB);
  const line = (arr) => arr.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const fmt = (ms) => { const d = new Date(ms); return `${d.getMonth() + 1}/${d.getDate()}`; };
  return `<div class="tlc__legend"><span class="tlc__k tlc__k--c">opened</span><span class="tlc__k tlc__k--d">closed</span></div>
    <svg class="tlc__svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline class="tlc__ln tlc__ln--c" vector-effect="non-scaling-stroke" points="${line(created)}"></polyline>
      <polyline class="tlc__ln tlc__ln--d" vector-effect="non-scaling-stroke" points="${line(completed)}"></polyline>
    </svg>
    <div class="tlc__x"><span>${fmt(start)}</span><span>${fmt(start + span / 2)}</span><span>${fmt(now)}</span></div>`;
}
let _tlChart = null;
function renderTimeline(canvasId, tasks, windowDays) {
  if (typeof Chart === 'undefined') return;
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (_tlChart) { _tlChart.destroy(); _tlChart = null; }
  const now = Date.now(), day = 86400000, start = now - windowDays * day;
  const bd = windowDays <= 14 ? 1 : (windowDays <= 60 ? 3 : 7);
  const nb = Math.max(2, Math.ceil(windowDays / bd));
  const opened = new Array(nb).fill(0), closed = new Array(nb).fill(0), labels = [];
  for (let i = 0; i < nb; i++) { const d = new Date(start + i * bd * day); labels.push((d.getMonth() + 1) + '/' + d.getDate()); }
  const idx = (ms) => { const i = Math.floor((ms - start) / (bd * day)); return i >= 0 && i < nb ? i : -1; };
  tasks.forEach((t) => {
    if (t.created_at) { const i = idx(new Date(t.created_at).getTime()); if (i >= 0) opened[i]++; }
    if (t.completed_at) { const i = idx(new Date(t.completed_at).getTime()); if (i >= 0) closed[i]++; }
  });
  const cs = getComputedStyle(document.documentElement), v = (n, f) => (cs.getPropertyValue(n).trim() || f);
  const cOpen = v('--sn-work', '#6f7e90'), cClose = v('--sn-ink-2', '#1c1c1e'), cGrid = v('--sn-rule', '#e7e6e1'), cTick = v('--sn-ink-faint', '#b3b2ad');
  _tlChart = new Chart(el.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [
      { label: 'opened', data: opened, borderColor: cOpen, backgroundColor: cOpen, fill: false, tension: 0.3, borderWidth: 2, pointRadius: 2, pointHoverRadius: 4 },
      { label: 'closed', data: closed, borderColor: cClose, backgroundColor: cClose, fill: false, tension: 0.3, borderWidth: 2, pointRadius: 2, pointHoverRadius: 4 },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', align: 'start', labels: { boxWidth: 10, boxHeight: 10, color: cTick, font: { size: 11 }, usePointStyle: true } } },
      scales: {
        x: { grid: { display: false }, border: { color: cGrid }, ticks: { color: cTick, font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
        y: { beginAtZero: true, grid: { color: cGrid }, border: { display: false }, ticks: { color: cTick, font: { size: 10 }, precision: 0, maxTicksLimit: 4 } },
      },
    },
  });
}
function renderTasksDash() {
  const body = $('#tasksDashBody');
  const tlAll = Array.isArray(_td.tasks) ? _td.tasks : [];
  const now = Date.now();
  const tl = _tdRange ? tlAll.filter((t) => { const d = t.created_at ? new Date(t.created_at).getTime() : 0; return d && d >= now - _tdRange * 86400000; }) : tlAll;
  const objs = Array.isArray(_td.objectives) ? _td.objectives : [];
  const projects = Array.isArray(_td.projects) ? _td.projects : [];
  const byId = {}; objs.forEach((o) => { byId[o.id] = o; });
  const kids = {}; objs.forEach((o) => { (kids[o.parent_id] = kids[o.parent_id] || []).push(o); });
  // Goals that belong to no strategy hang under one virtual strategy, so they get a
  // chip, a ring and a place in the freshness order like the real ones. Most of the
  // current work lives there: on example-node no real strategy has had a task change
  // since April.
  const NONE_ID = -1;
  kids[NONE_ID] = objs.filter((o) => o.type !== 'strategy' && (o.parent_id == null || !byId[o.parent_id]));
  byId[NONE_ID] = { id: NONE_ID, type: 'strategy', status: 'active', title: 'No strategy', parent_id: null };
  const subtreeIds = (id) => { const out = [id], st = [id]; while (st.length) { (kids[st.pop()] || []).forEach((ch) => { out.push(ch.id); st.push(ch.id); }); } return out; };
  // Per-objective task counts and freshness, once, then rolled up per subtree.
  // Freshness = the latest change of any task under the objective: that is what
  // "is this goal alive" means to the owner, not the objective's own updated_at.
  const own = {};
  tl.forEach((x) => {
    if (x.objective_id == null) return;
    const a = own[x.objective_id] || (own[x.objective_id] = { done: 0, total: 0, open: 0, last: 0 });
    a.total++;
    if (tkStage(x) === 'done') a.done++; else if (tkStage(x) !== 'rejected') a.open++;
    const u = new Date(x.updated_at || x.created_at || 0).getTime(); if (u > a.last) a.last = u;
  });
  const aggMemo = {};
  const aggObj = (id) => {
    if (aggMemo[id]) return aggMemo[id];
    let d = 0, t = 0, op = 0, last = 0;
    subtreeIds(id).forEach((i) => { const a = own[i]; if (a) { d += a.done; t += a.total; op += a.open; if (a.last > last) last = a.last; } });
    return (aggMemo[id] = { done: d, total: t, open: op, last, pct: t ? Math.round(100 * d / t) : 0 });
  };
  const stratOf = (o) => { let c = o, g = 0; while (c && g++ < 20) { if (c.type === 'strategy') return c.id; c = c.parent_id != null ? byId[c.parent_id] : null; } return NONE_ID; };
  const live = (o) => _tdClosed || o.status === 'active' || o.status === 'draft';
  const fresh = (a, b) => (aggObj(b.id).last - aggObj(a.id).last) || (b.id - a.id);
  const strategies = objs.filter((o) => o.type === 'strategy' && (live(o) || o.id === _tdStrat))
    .concat(kids[NONE_ID].length ? [byId[NONE_ID]] : []).sort(fresh);
  const goals = objs.filter((o) => o.type !== 'strategy');
  // current scope: a selected goal narrows below a selected strategy
  const scopeId = _tdGoal != null ? _tdGoal : (_tdStrat != null ? _tdStrat : null);
  const scopeSet = scopeId != null ? new Set(subtreeIds(scopeId)) : null;
  const scopedTasks = scopeSet ? tl.filter((x) => scopeSet.has(x.objective_id)) : tl;
  const sel = _tdGoal != null && byId[_tdGoal] ? byId[_tdGoal] : (_tdStrat != null && byId[_tdStrat] ? byId[_tdStrat] : null);
  // strategies: one strip of chips, freshest first
  const stratChip = (sv) => { const a = aggObj(sv.id); const on = _tdStrat === sv.id; return `<button class="strat__chip${on ? ' is-on' : ''}" data-strat="${sv.id}" title="${esc(sv.title || '')}">${pgRing(a.pct)}<span class="strat__chip-b"><span class="strat__chip-t">${esc(sv.title || ('#' + sv.id))}</span><span class="strat__chip-m">${a.done}/${a.total} tasks${a.open ? ` &middot; ${a.open} open` : ''}</span></span></button>`; };
  // goals: live, in the selected strategy, matching the filter, freshest first, a page at a time
  const find = _tdFind.trim().toLowerCase();
  const shownGoalsAll = goals
    .filter((g) => (live(g) || g.id === _tdGoal) && (_tdStrat === null || stratOf(g) === _tdStrat) && (_tdProj === null || g.project_id === _tdProj) && (!find || (g.title || '').toLowerCase().includes(find)))
    .sort(fresh);
  const shownGoals = shownGoalsAll.slice(0, _tdGoalsN);
  if (_tdGoal != null && !shownGoals.some((g) => g.id === _tdGoal) && byId[_tdGoal]) shownGoals.unshift(byId[_tdGoal]);
  const ago = (ms) => { if (!ms) return ''; const d = Math.floor((now - ms) / 86400000); return d <= 0 ? 'today' : d === 1 ? 'yesterday' : d < 60 ? `${d}d ago` : `${Math.floor(d / 30)}mo ago`; };
  const goalCard = (o) => {
    const a = aggObj(o.id); const on = _tdGoal === o.id;
    const closed = o.status === 'completed' || o.status === 'cancelled' ? ` &middot; ${esc(o.status)}` : '';
    // numbers under the ring (the owner: the space there stood empty), words on the right
    const stats = `<div class="tdx__gs" title="${a.done} done of ${a.total}, ${a.open} open"><b>${a.done}/${a.total}</b>${a.last ? `<span>${ago(a.last)}</span>` : ''}</div>`;
    return `<div class="pgc__card pgc__card--sel tdx__goal${on ? ' is-on' : ''}" data-goal="${o.id}"><div class="tdx__gl">${pgRing(a.pct)}${stats}</div><div class="pgc__b"><div class="pgc__t" title="${esc(o.title || '')}">${esc(o.title || ('#' + o.id))}</div>${closed ? `<div class="pgc__m">${closed.replace(' &middot; ', '')}</div>` : ''}${o.description ? `<div class="pgc__d">${esc(o.description)}</div>` : ''}</div></div>`;
  };
  // projects: those appearing in the scoped tasks; full-project progress
  const scopeProjIds = scopeSet ? new Set(scopedTasks.map((x) => x.project_id).filter((v) => v != null)) : null;
  const shownProjects = scopeProjIds ? projects.filter((p) => scopeProjIds.has(p.id)) : projects;
  // Projects: same chip as a strategy, freshest first, two rows unless expanded.
  // They filter the goals (objective.project_id) and the task table.
  const pAgg = {};
  tl.forEach((x) => {
    if (x.project_id == null) return;
    const a = pAgg[x.project_id] || (pAgg[x.project_id] = { done: 0, total: 0, open: 0, last: 0 });
    a.total++; if (tkStage(x) === 'done') a.done++; else if (tkStage(x) !== 'rejected') a.open++;
    const u = new Date(x.updated_at || x.created_at || 0).getTime(); if (u > a.last) a.last = u;
  });
  const pOf = (id) => pAgg[id] || { done: 0, total: 0, open: 0, last: 0 };
  const liveProjects = shownProjects
    .filter((p) => p.id === _tdProj || (pOf(p.id).total > 0 && (_tdClosed || (p.status || 'active') === 'active')))
    .sort((a, b) => (pOf(b.id).last - pOf(a.id).last) || (a.id - b.id));
  const pLimit = _tdProjAll ? liveProjects.length : 2 * (_tdPCols || 5);
  const shownProjChips = liveProjects.slice(0, pLimit);
  if (_tdProj != null && !shownProjChips.some((p) => p.id === _tdProj)) { const sp = liveProjects.find((p) => p.id === _tdProj); if (sp) shownProjChips.unshift(sp); }
  const projChip = (p) => { const a = pOf(p.id); const pct = a.total ? Math.round(100 * a.done / a.total) : 0; const on = _tdProj === p.id; const t = p.title || p.slug || ('#' + p.id); return `<button class="strat__chip${on ? ' is-on' : ''}" data-proj="${p.id}" title="${esc(t)}">${pgRing(pct)}<span class="strat__chip-b"><span class="strat__chip-t">${esc(t)}</span><span class="strat__chip-m">${a.done}/${a.total} tasks${a.open ? ` &middot; ${a.open} open` : ''}</span></span></button>`; };
  const projMore = liveProjects.length - shownProjChips.length;
  const selProj = _tdProj != null ? projects.find((p) => p.id === _tdProj) : null;
  const selBits = [];
  if (_tdStrat != null && byId[_tdStrat]) selBits.push(byId[_tdStrat].title);
  if (_tdGoal != null && byId[_tdGoal]) selBits.push(byId[_tdGoal].title);
  if (selProj) selBits.push(selProj.title || selProj.slug);
  const selLabel = selBits.join(' · ');
  // tasks: scoped, freshest first, paginated
  const tableTasks = (_tdProj != null ? scopedTasks.filter((x) => x.project_id === _tdProj) : scopedTasks).slice()
    .sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0));
  const pages = Math.max(1, Math.ceil(tableTasks.length / TD_TASKS_PAGE));
  if (_tdPage >= pages) _tdPage = pages - 1;
  const pageRows = tableTasks.slice(_tdPage * TD_TASKS_PAGE, (_tdPage + 1) * TD_TASKS_PAGE);
  const from = tableTasks.length ? _tdPage * TD_TASKS_PAGE + 1 : 0, to = _tdPage * TD_TASKS_PAGE + pageRows.length;
  const pager = pages > 1 ? `<div class="tdx__pager"><button class="tchip" data-page="prev"${_tdPage === 0 ? ' disabled' : ''}>&lsaquo; Newer</button><span>${from}&ndash;${to} of ${tableTasks.length}</span><button class="tchip" data-page="next"${_tdPage >= pages - 1 ? ' disabled' : ''}>Older &rsaquo;</button></div>` : '';
  const objScopeAll = scopeSet ? tlAll.filter((x) => scopeSet.has(x.objective_id)) : tlAll;
  const timelineTasks = _tdProj != null ? objScopeAll.filter((x) => x.project_id === _tdProj) : objScopeAll;
  const dayMs = 86400000;
  const createdTs = timelineTasks.map((t) => t.created_at ? new Date(t.created_at).getTime() : NaN).filter((v) => !isNaN(v));
  const winDays = _tdRange || Math.max(7, Math.ceil((now - (createdTs.length ? Math.min(...createdTs) : now - 90 * dayMs)) / dayMs) + 1);
  const timeChips = [['', 'All'], ['7', '7d'], ['30', '30d'], ['90', '90d']].map((rc) => `<button class="tchip${(_tdRange === (rc[0] === '' ? null : Number(rc[0]))) ? ' is-on' : ''}" data-range="${rc[0]}">${rc[1]}</button>`).join('');
  const ttEl = $('#tdashTime'); if (ttEl) ttEl.innerHTML = timeChips;
  const more = shownGoalsAll.length - shownGoals.length;
  // How many goals: whole rows at the current width (2, 4, 8 rows) or all; the
  // owner can go back to a short list after "show more" (2026-09-24).
  const colsNow = _tdCols || 4;
  const nChoices = [2, 4, 8].map((r) => r * colsNow);
  const showingAll = _tdGoalsN >= shownGoalsAll.length && _tdGoalsN > nChoices[nChoices.length - 1];
  if (!showingAll && !nChoices.includes(_tdGoalsN)) nChoices.push(_tdGoalsN);
  const goalsNOpts = nChoices.sort((a, b) => a - b).map((n) => `<option value="${n}"${!showingAll && n === _tdGoalsN ? ' selected' : ''}>Show ${n}</option>`).join('')
    + `<option value="all"${showingAll ? ' selected' : ''}>Show all</option>`;
  const findHadFocus = document.activeElement && document.activeElement.id === 'tdxFind';
  body.innerHTML = `
    <div class="tdx__head">
      <button type="button" class="tdx__fold${_tdFold.strat ? ' is-shut' : ''}" data-fold="strat" aria-expanded="${!_tdFold.strat}">${TD_CHEV}<span class="pgc__sec">Strategies (${strategies.length})</span></button>
      <span class="tdx__grow"></span>
      <span class="tdx__closed" id="tdxClosedWrap"><button type="button" class="switch${_tdClosed ? ' on' : ''}" id="tdxClosed" role="switch" aria-checked="${_tdClosed}" aria-label="Show closed"><span class="knob"></span></button>show closed</span>
    </div>
    ${_tdFold.strat ? '' : `<div class="strat__strip tdx__strip">${strategies.map(stratChip).join('') || '<div class="tkt__mut">No live strategies.</div>'}</div>`}
    <div class="tdx__head">
      <button type="button" class="tdx__fold${_tdFold.proj ? ' is-shut' : ''}" data-fold="proj" aria-expanded="${!_tdFold.proj}">${TD_CHEV}<span class="pgc__sec">Projects${sel ? ' &middot; in scope' : ''} (${liveProjects.length})</span></button>
      <span class="tdx__grow"></span>
    </div>
    ${_tdFold.proj ? '' : `<div class="strat__strip tdx__strip tdx__projs">${shownProjChips.map(projChip).join('') || '<div class="tkt__mut">No projects in scope.</div>'}</div>
    ${projMore > 0 || _tdProjAll ? `<div class="tdx__more"><button class="tchip" id="tdxProjAll">${_tdProjAll ? 'Show less' : `Show all &middot; ${projMore} more`}</button></div>` : ''}`}
    <div class="tdx__head">
      <button type="button" class="tdx__fold${_tdFold.goals ? ' is-shut' : ''}" data-fold="goals" aria-expanded="${!_tdFold.goals}">${TD_CHEV}<span class="pgc__sec">Goals${_tdStrat !== null && byId[_tdStrat] ? ` &middot; ${esc(byId[_tdStrat].title)}` : ''} (${shownGoalsAll.length})</span></button>
      <span class="tdx__grow"></span>
      ${_tdFold.goals ? '' : `<input class="tdx__find" id="tdxFind" type="search" placeholder="Filter goals" value="${esc(_tdFind)}" autocomplete="off">
      <select class="tdx__n" id="tdxN" title="How many goals to show">${goalsNOpts}</select>`}
    </div>
    ${_tdFold.goals ? '' : `<div class="pgc__grid tdx__goals">${shownGoals.map(goalCard).join('') || '<div class="tkt__mut">No live goals here.</div>'}</div>
    ${more > 0 ? `<div class="tdx__more"><button class="tchip" id="tdxMore">Show ${Math.min(more, 2 * (_tdCols || 4))} more &middot; ${more} left</button></div>` : ''}`}
    <div class="tlc tdx__act"><div class="pgc__sec">Activity &middot; last ${winDays}d</div><div class="tlc__cv"><canvas id="tlCanvas"></canvas></div></div>
    <div class="tdx__head">
      <div class="pgc__sec" style="margin:0">Tasks${selLabel ? ` &middot; ${esc(selLabel)}` : ''} (${tableTasks.length})</div>
      <span class="tdx__grow"></span>${pager}
    </div>
    <div class="tdh__tablewrap">${tkTableHTML(pageRows)}</div>
    ${pager ? `<div class="tdx__foot">${pager}</div>` : ''}`;
  if (ttEl) ttEl.querySelectorAll('.tchip').forEach((b) => b.addEventListener('click', () => { const v = b.getAttribute('data-range'); _tdRange = v === '' ? null : Number(v); _tdPage = 0; renderTasksDash(); }));
  body.querySelectorAll('[data-strat]').forEach((b) => b.addEventListener('click', () => { const id = Number(b.getAttribute('data-strat')); _tdStrat = _tdStrat === id ? null : id; _tdGoal = null; _tdProj = null; _tdGoalsN = 2 * (_tdCols || 4); _tdPage = 0; renderTasksDash(); }));
  body.querySelectorAll('.pgc__card[data-goal]').forEach((b) => b.addEventListener('click', () => { const id = Number(b.getAttribute('data-goal')); _tdGoal = _tdGoal === id ? null : id; _tdProj = null; _tdPage = 0; renderTasksDash(); }));
  body.querySelectorAll('[data-proj]').forEach((b) => b.addEventListener('click', () => { const id = Number(b.getAttribute('data-proj')); _tdProj = _tdProj === id ? null : id; _tdGoal = null; _tdGoalsN = 2 * (_tdCols || 4); _tdPage = 0; renderTasksDash(); }));
  const pa = $('#tdxProjAll'); if (pa) pa.addEventListener('click', () => { _tdProjAll = !_tdProjAll; renderTasksDash(); });
  body.querySelectorAll('[data-page]').forEach((b) => b.addEventListener('click', () => { _tdPage += b.getAttribute('data-page') === 'next' ? 1 : -1; renderTasksDash(); }));
  const ns = $('#tdxN'); if (ns) ns.addEventListener('change', () => { _tdGoalsN = ns.value === 'all' ? 100000 : Number(ns.value); renderTasksDash(); });
  const mb = $('#tdxMore'); if (mb) mb.addEventListener('click', () => { _tdGoalsN += 2 * (_tdCols || 4); renderTasksDash(); });
  // Show whole rows: the column count depends on the window, so measure it and
  // round the number of cards to two full rows (and keep it a multiple after).
  const pEl = body.querySelector('.tdx__projs');
  if (pEl) { const pc = getComputedStyle(pEl).gridTemplateColumns.split(' ').filter(Boolean).length || 1; if (pc !== _tdPCols) { _tdPCols = pc; if (!_tdProjAll) { renderTasksDash(); return; } } }
  const gridEl = body.querySelector('.tdx__goals');
  if (gridEl) {
    const cols = getComputedStyle(gridEl).gridTemplateColumns.split(' ').filter(Boolean).length || 1;
    const want = Math.max(2 * cols, Math.ceil(_tdGoalsN / cols) * cols);
    if (cols !== _tdCols || want !== _tdGoalsN) {
      const first = !_tdCols; _tdCols = cols;
      const target = first ? 2 * cols : want;
      if (target !== _tdGoalsN) { _tdGoalsN = target; renderTasksDash(); return; }
    }
  }
  const cw = $('#tdxClosedWrap'); if (cw) cw.addEventListener('click', () => { _tdClosed = !_tdClosed; _tdGoalsN = 2 * (_tdCols || 4); renderTasksDash(); });
  body.querySelectorAll('[data-fold]').forEach((b) => b.addEventListener('click', () => {
    const k = b.getAttribute('data-fold'); _tdFold[k] = !_tdFold[k];
    try { localStorage.setItem('sn-tdash-fold', JSON.stringify(_tdFold)); } catch (e) {}
    renderTasksDash();
  }));
  const fi = $('#tdxFind');
  if (fi) {
    // the body is repainted on every keystroke; keep the caret in the field
    if (findHadFocus) { fi.focus(); fi.setSelectionRange(fi.value.length, fi.value.length); }
    fi.addEventListener('input', () => { _tdFind = fi.value; _tdGoalsN = 2 * (_tdCols || 4); renderTasksDash(); });
  }
  body.querySelectorAll('.tdh__tablewrap tr[data-id]').forEach((el) => el.addEventListener('click', () => openTaskDrawer(el.dataset.id)));
  renderTimeline('tlCanvas', timelineTasks, winDays);
}
function dshAv(initial, st) {
  return `<div class="sn-av">${initial}${st ? `<span class="sn-av__st sn-av__st--${st}"></span>` : ''}</div>`;
}
// Studio > Dashboard: owner "morning" view. Static demo mock, Studio styles only.
// 50/25/25 two-row bento: decisions/coming-up/today + feed/emails/messages. No hero text.
// Skeleton rows for the loading state (modern skeleton-screen pattern: fixed
// layout + shimmer, so the panel never jumps from "Loading..." to data).
function dshSkel(n, av) {
  let h = '';
  for (let i = 0; i < n; i++) h += `<div class="skrow">${av ? '<div class="skel skav"></div>' : ''}<div class="skcol"><div class="skel skline w70"></div><div class="skel skline w45"></div></div></div>`;
  return h;
}
// Working now while it loads: the table's own shape (the "nobody" line, the head
// and a row per agent), so the card does not jump when the data lands and push
// the cards below it
function dshNowSkel() {
  const n = Math.min(agentsList.length || 5, DSH_NOW_ROWS);
  // the same elements as the real rows (an avatar circle sets their height), not
  // look-alike bars of a guessed height
  const cell = (w) => `<div class="wnt__c"><div class="skel skline ${w}"></div></div>`;
  return '<div class="wn__none dshm__mut">&nbsp;</div>'
    + '<div class="wnt__r wnt__h"><div class="wnt__c">Agent</div><div class="wnt__c">On</div><div class="wnt__c">What started it</div><div class="wnt__c wnt__t">When</div></div>'
    + Array.from({ length: n }, () => `<div class="wnt__r"><div class="wnt__c wnt__a"><div class="sn-av skel"></div><div class="skel skline w70"></div></div>${cell('w45')}${cell('w70')}${cell('w45')}</div>`).join('');
}
// Every loading state is the shape of what is coming, shimmering, not a word: nothing jumps when the data lands, and
// a slow call reads as work in progress. These are the shared builders; a screen with its own layout builds its own
// (tasksDashSkel, wmSkeleton) from the same .skel blocks.
function skRows(n, av) { return `<div class="sk" role="status" aria-busy="true" aria-label="Loading">${dshSkel(n, av)}</div>`; }
function skText(n) {
  const w = ['w90', 'w70', 'w90', 'w45', 'w70', 'w90'];
  return `<div class="sk sk--text" role="status" aria-busy="true" aria-label="Loading"><div class="skel sk-title"></div>`
    + Array.from({ length: n }, (_, i) => `<div class="skel skline ${w[i % w.length]}"></div>`).join('') + '</div>';
}
window.snSkelRows = skRows;   // for the scripts that load beside this one (agent page, chat panel)
function tasksDashSkel() {
  const chips = (n) => `<div class="sk-chips">${'<div class="skel sk-chip"></div>'.repeat(n)}</div>`;
  const head = '<div class="skel sk-head"></div>';
  return `<div class="sk" role="status" aria-busy="true" aria-label="Loading">`
    + head + chips(5) + head + chips(6)
    + head + `<div class="sk-cards">${'<div class="skel sk-card"></div>'.repeat(4)}</div>`
    + head + '<div class="skel sk-chart"></div>'
    + head + dshSkel(8, false) + '</div>';
}
function loadDashboard() {
  const body = $('#dashBody');
  const dec = (i, st, t, d, primary) => `
    <div class="dec">${dshAv(i, st)}
      <div style="min-width:0"><div class="dec__t">${t}</div><div class="dec__d">${d}</div></div>
      <div class="dec__act"><button class="sn-btn sn-btn--ghost" type="button">Review</button><button class="sn-btn sn-btn--primary" type="button">${primary}</button></div>
    </div>`;
  const stat = (n, l, cls) => `<div class="gl__row"><span class="gl__l">${l}</span><span class="gl__n ${cls || ''}">${n}</span></div>`;
  const ev = (i, html, time, first) => `<div class="fd__i ${first ? 'first' : ''}">${dshAv(i, '')}<div class="fd__t">${html}</div><div class="fd__time">${time}</div></div>`;
  const up = (w, t, first) => `<div class="up__i ${first ? 'first' : ''}"><div class="up__w">${w}</div><div class="up__t">${t}</div></div>`;
  const ms = (from, when, pre, first) => `<div class="ms__i ${first ? 'first' : ''}"><div class="ms__top"><span class="ms__from">${from}</span><span class="ms__when">${when}</span></div><div class="ms__pre">${pre}</div></div>`;
  body.innerHTML = `
    <div class="dshm">
      <div class="dshm__grid">
        <div class="dc dc--now">
          <div class="dc__h">Working now <span class="n" id="dshNowN">&middot;</span></div>
          <div id="dshNow">${dshNowSkel()}</div>
        </div>
        <div class="dc dc--decs" data-needs="tracking">
          <div class="dc__h">Needs your call <span class="n" id="dshDecN">&middot;</span></div>
          <div id="dshDecs">${dshSkel(5, true)}</div>
        </div>
        <div class="dc dc--coming" data-needs="tracking">
          <div class="dc__h">Open work</div>
          <div id="dshOpen">${dshSkel(5, false)}</div>
        </div>
        <div class="dc dc--glance" data-needs="tracking">
          <div class="dc__h">Today <span class="ok" id="dshOkBadge"><i></i>&middot;</span></div>
          <div class="gl" id="dshStats">${dshSkel(4, false)}</div>
        </div>
        <div class="dc dc--feed">
          <div class="dc__h">What your team did <span class="sn-tag sn-tag--on">Live</span></div>
          <div id="dshFeed">${dshSkel(6, true)}</div>
        </div>
        <div class="dc dc--emails" data-needs="communication">
          <div class="dc__h">Recent emails</div>
          <div id="dshEmails">${dshSkel(6, false)}</div>
        </div>
        <div class="dc dc--msgs">
          <div class="dc__h">Recent messages</div>
          <div id="dshMsgs">${dshSkel(5, false)}</div>
        </div>
      </div>
    </div>`;
  if (window.lucide) try { lucide.createIcons(); } catch (_) {}
  // the three task cards come with the planner (tracking); without it they stayed
  // grey placeholders forever
  if (window.snApplyNeeds) window.snApplyNeeds(body);
  dshLoadLive();                                  // paint real data over the shell
  // Working now draws from the agents' state the page already holds. It waited
  // for that state's next 5 s poll: the first one runs before the Dashboard is on
  // screen, and the tasks load that also repaints it does not exist without the
  // tasks module.
  dshLoadNow();
  if (_dashIv) clearInterval(_dashIv);         // no poll: the live stream repaints it
}

// Live wiring for the Dashboard (WS4). All six panels now render REAL node data:
//   "What your Studio did"  <- /api/activity (curated node-wide event stream)
//   "Needs your call"       <- /api/tasks    (awaiting_approval, owner gates)
//   "Open work"             <- /api/tasks    (open tasks, test/dev junk filtered)
//   "Today"                 <- /api/tasks    (in-progress / waiting / done counts)
//   "Recent emails"         <- /api/inbox    (inbound email)
//   "Recent messages"       <- /api/inbox    (inbound web-chat/telegram/internal)
// The node has no calendar source (0 tasks carry a due_date), so the old dated
// "Coming up" panel was repurposed to "Open work" rather than fake dates.
let _dashIv = null;

const DSH_NAME = { 'send-outbound': 'Studio', 'floor-manager': 'Floor manager' };
function dshPretty(slug) {
  // an agent by its name, not its slug ("Foreman messaged the team" was John);
  // the person's own web line is "You", another person's by their name
  const ag = typeof agentBySlug === 'function' ? agentBySlug(slug) : null;
  if (ag && ag.title) return ag.title;
  if (slug === 'web:owner' || (meIdentity.user && slug === 'web:' + meIdentity.user)) return 'You';
  if (/^web:/.test(String(slug || ''))) slug = String(slug).slice(4);
  const s = String(slug || '').split('@')[0].replace(/[._-]+/g, ' ').trim();
  if (DSH_NAME[slug]) return DSH_NAME[slug];
  return s ? s.replace(/\b\w/g, (c) => c.toUpperCase()) : 'Someone';
}
function dshInitial(name) { return (String(name || '?').trim()[0] || '?').toUpperCase(); }
// Say who it is. A known contact renders by name; anything else renders as
// itself, because an address the owner can read is worth more than a word that
// fits everybody. The channel-generic labels below are kept for the one case
// where they are TRUE -- an arrival carrying no address at all.
function dshDisplayName(value, channel) {
  const raw = String(value || '').trim();
  if (!raw) {
    if (channel === 'web-chat' || channel === 'webchat') return 'Website visitor';
    if (channel === 'telegram') return 'Telegram user';
    return 'Someone';
  }
  const known = dshContactName(raw);
  if (known) return known;
  return raw.includes('@') ? raw : dshPretty(raw);
}

// ---- Agent avatars ----------------------------------------------------------
// An agent's `avatar` is a DATA field (from core-api): a library key such as
// `crew/alex.webp` (served from /avatars/), or a full http(s) URL, or null.
// avInner() fills any existing circular avatar element with an <img>; the initial
// letter stays behind it as a graceful fallback (the img self-removes on error,
// revealing the initial + the element's brand colour). No call site needs to know
// where images live -- changing an avatar is a pure data change (PUT/SQL).
function avResolve(av) {
  const s = String(av || '').trim();
  if (!s) return null;
  return /^https?:\/\//i.test(s) ? s : '/avatars/' + s.replace(/^\/+/, '');
}
function avBySlug(slug) { const a = agentBySlug(slug); return a && a.avatar ? avResolve(a.avatar) : null; }
function avInner(src, initial) {
  const img = src ? `<img class="av-img" src="${esc(src)}" alt="" loading="lazy" onerror="this.remove()">` : '';
  return img + esc(initial);
}
// Avatar picker (drawer): a library grid + upload + remove. Assigning an avatar is
// a data change -- library pick / remove = PUT {avatar}; upload = POST the image,
// which the Studio optimizes to WebP and stores under custom/, then sets the field.
async function setAgentAvatar(slug, key) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ avatar: key }),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    await afterAvatarChange(slug);
  } catch (e) { console.error('[studio] set avatar failed', e); }
}
async function uploadAgentAvatar(slug, file) {
  const ext = (file.name.split('.').pop() || 'png').toLowerCase();
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/avatar?ext=${encodeURIComponent(ext)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || ('http ' + r.status)); }
    await afterAvatarChange(slug);
  } catch (e) { console.error('[studio] avatar upload failed', e); alert('Avatar upload failed: ' + e.message); }
}
async function afterAvatarChange(slug) {
  closePop();
  await loadAgents(); // re-fetch roster -> refreshes tabs, overview, chat bar
  const a = agentBySlug(slug); const el = $('#drAv');
  if (el && drawer && drawer.dataset.slug === slug && a) el.innerHTML = avInner(avResolve(a.avatar), (a.title || a.slug || '?').trim().charAt(0).toUpperCase());
}
async function openAvatarPicker(anchor, slug) {
  const pop = showPop(anchor, `<div class="pop__lbl">avatar</div>
    <div class="avpick" id="avpickGrid"><div class="sk-avgrid" role="status" aria-busy="true" aria-label="Loading">${'<div class="skel sk-av"></div>'.repeat(12)}</div></div>
    <div class="avpick__foot">
      <label class="avpick__up">Upload image<input type="file" accept="image/*" id="avpickFile" hidden></label>
      <button class="avpick__rm" id="avpickRm" type="button">Remove</button>
    </div>`);
  pop.classList.add('pop--av');
  const grid = pop.querySelector('#avpickGrid');
  // by pack, as on the welcome page: the types first, a type opens its faces
  // (three hundred faces in one grid was a heap). The packs and
  // their labels are public/avatars/packs.json; uploaded ones are a pack of their own.
  try {
    const [lib, manifest] = await Promise.all([
      fetch('/api/avatars', { headers: { Accept: 'application/json' } }).then((r) => r.json()),
      fetch('/avatars/packs.json').then((r) => (r.ok ? r.json() : [])).catch(() => [])]);
    const cur = (agentBySlug(slug) || {}).avatar || '';
    const byPack = {};
    lib.forEach((x) => { (byPack[x.group] = byPack[x.group] || []).push(x); });
    const packs = manifest.filter((m) => byPack[m.id])
      .concat(Object.keys(byPack).filter((id) => !manifest.some((m) => m.id === id)).map((id) => ({ id, label: id === 'custom' ? 'Uploaded' : id })));
    let open = (packs.find((p) => cur.startsWith(p.id + '/')) || {}).id || null;
    const paint = () => {
      const p = packs.find((x) => x.id === open);
      grid.classList.toggle('avpick--packs', !p); grid.scrollTop = 0;
      grid.innerHTML = !lib.length ? '<div class="pop__empty">library empty</div>'
        : p ? `<button class="avpick__back" type="button" data-types><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>All types<span>${esc(p.label)}</span></button>`
          + byPack[p.id].map((x) => `<button class="avpick__it ${x.key === cur ? 'is-sel' : ''}" type="button" data-key="${esc(x.key)}" title="${esc(x.name)}"><img src="${esc(x.url)}" alt="" loading="lazy"></button>`).join('')
        : packs.map((x) => { const team = byPack[x.id], cover = team.find((a) => x.cover && a.key === `${x.id}/${x.cover}`) || team[0];
          return `<button class="avpick__pack ${cur.startsWith(x.id + '/') ? 'is-sel' : ''}" type="button" data-pack="${esc(x.id)}"><img src="${esc(cover.url)}" alt="" loading="lazy">${esc(x.label)}</button>`; }).join('');
    };
    grid.addEventListener('click', (e) => {
      const b = e.target.closest('button'); if (!b) return;
      if (b.dataset.types != null) { open = null; return paint(); }
      if (b.dataset.pack) { open = b.dataset.pack; return paint(); }
      if (b.dataset.key) setAgentAvatar(slug, b.dataset.key);
    });
    paint();
  } catch { grid.innerHTML = '<div class="pop__empty">could not load library</div>'; }
  const file = pop.querySelector('#avpickFile');
  if (file) file.addEventListener('change', () => { if (file.files && file.files[0]) uploadAgentAvatar(slug, file.files[0]); });
  const rm = pop.querySelector('#avpickRm');
  if (rm) rm.addEventListener('click', () => setAgentAvatar(slug, ''));
}

// Channel glyphs from Lucide (data-lucide). lucide.createIcons() runs after each
// dashboard render so they survive the 5s feed repaint.
const CH_ICONS = {
  email: '<i data-lucide="mail" class="nf__gl"></i>',
  telegram: '<i data-lucide="send" class="nf__gl"></i>',
  slack: '<i data-lucide="hash" class="nf__gl"></i>',
  internal: '<i data-lucide="arrow-left-right" class="nf__gl"></i>',
  chat: '<i data-lucide="message-circle" class="nf__gl"></i>',
  voice: '<i data-lucide="phone" class="nf__gl"></i>',
};
const CH_LABEL = { email: 'Email', telegram: 'Telegram', slack: 'Slack', internal: 'Internal', chat: 'Web chat', voice: 'Voice' };
function dshChan(ch) { const c = String(ch || '').toLowerCase(); if (c === 'web-chat' || c === 'webchat') return 'chat'; return CH_ICONS[c] ? c : 'internal'; }
function dshChips(channels) {
  const seen = new Set();
  return (channels || []).map(dshChan).filter((c) => !seen.has(c) && seen.add(c)).slice(0, 4)
    .map((c) => `<span class="nf__ic nf__ic--${c}" title="${esc(CH_LABEL[c] || c)}">${CH_ICONS[c]}</span>`).join('');
}

// One curated event -> a landing-style card row (coloured avatar, action line,
// muted preview, channel pill). Every party goes through dshDisplayName, so a
// known contact reads by name and an unknown one reads as its address.
function dshActLine(e) {
  const t = e.type || '';
  let who = dshPretty(e.agent), avSlug = String(e.agent || ''), line = '', tag = '', glyph = '';
  const fromStr = String(e.from || '');
  if (t === 'message.transmitted') {                       // node delivered to the owner
    const ch = String(e.channel || '').toLowerCase();
    line = `<b>Node</b> delivered to you via <b>${esc(CH_LABEL[ch] || dshChanLabel(ch))}</b>`;
    tag = ch; glyph = CH_ICONS[ch] || CH_ICONS.internal; avSlug = '';
  } else if (t === 'tool.succeeded') {                     // a write the Studio performed
    // The verb comes from payload.op, which names the call. It used to come from
    // entity_id, which for every planner call is the literal string 'planner' --
    // so a comment, an update and a task creation all read "created planner".
    // Reads no longer arrive here at all; the endpoint drops them.
    const op = String(e.op || '');
    const ACT = {
      'planner.create':  ['created', 'a task'],
      'planner.update':  ['updated', 'a task'],
      'planner.comment': ['commented on', 'a task'],
    };
    let verb = '', obj = '';
    if (ACT[op]) { [verb, obj] = ACT[op]; tag = 'task'; }
    else {
      const m = op.match(/^planner\.([a-z_]+)\.(create|update|delete|read)$/);
      if (m) { verb = { create: 'created', update: 'updated', delete: 'deleted', read: 'read' }[m[2]]; obj = 'a ' + m[1].replace(/s$/, '').replace(/_/g, ' '); tag = 'task'; }
      else if (e.entity === 'calendar_create_event') { verb = 'added'; obj = 'a calendar event'; tag = 'calendar'; }
      else { verb = 'did'; obj = String(e.entity || '').replace(/_/g, ' ') || 'something'; tag = 'task'; }
    }
    line = `<b>${esc(who)}</b> ${esc(verb)} <b>${esc(obj)}</b>`;
  } else if (t === 'message.routing_denied') {             // held, waiting on the owner
    // Collapsed upstream by (reason, channel): one row carries how many times
    // the same verdict was reached, so a standing misconfiguration reads as one
    // actionable line instead of hundreds of identical ones.
    const n = Number(e.count || 1);
    const ch = String(e.channel || '').toLowerCase();
    const what = n > 1 ? `${n} messages` : 'a message';
    const why = /no.?binding|no agent bound/i.test(String(e.reason || ''))
      ? `no agent is bound to ${esc(CH_LABEL[ch] || dshChanLabel(ch) || 'that channel')}`
      : 'needs your input';
    line = `<b>Node</b> held <b>${esc(what)}</b> &middot; ${why}`;
    tag = 'deny'; glyph = CH_ICONS.internal; avSlug = '';
  } else if (t === 'message.inbound') {
    if (e.channel === 'internal') {                        // agent-to-agent
      who = dshPretty(fromStr || e.agent); avSlug = fromStr || e.agent;
      line = `<b>${esc(who)}</b> messaged the team`; tag = 'internal';
    } else {                                               // external contact
      who = dshDisplayName(fromStr, e.channel); avSlug = '';
      line = `<b>${esc(who)}</b> contacted ${esc(dshPretty(e.to) || 'the team')}`;
      tag = (e.channel ? dshChanLabel(e.channel) : 'chat').toLowerCase();
    }
  } else if (t === 'message.sent' || t === 'message.reply') {
    // Who it went to decides the wording. The owner is whoever is signed in, a
    // colleague is any slug the node itself reports as an agent, and anything
    // else is an outside reply. None of the three may be a name in the source:
    // `alex` was one here, so on every other node a colleague read as a
    // stranger.
    const toOwner = dshIsOwner(e.to);
    const toAgent = !toOwner && !!agentBySlug(String(e.to || '').toLowerCase().trim());
    const verb = toOwner ? 'relayed to' : (toAgent ? 'reported to' : 'replied to');
    const toName = toOwner ? 'You' : dshDisplayName(e.to, e.channel);
    line = `<b>${esc(who)}</b> ${verb} <b>${esc(toName)}</b>`;
    tag = toOwner ? 'voice' : 'internal';
  } else {
    line = `<b>${esc(who)}</b> ${esc(t)}`; tag = 'internal';
  }
  const prev = e.preview ? `<div class="nf__prev">${esc(reminderText(e.preview).slice(0, 110))}</div>` : '';
  const av = glyph
    ? `<div class="nf__av nf__av--node">${glyph}</div>`
    : (avSlug
        // No tile behind an avatar: a picture shows as itself, a letter or a
        // channel icon stands on the card.
        ? `<div class="nf__av">${avInner(avBySlug(avSlug), dshInitial(who))}</div>`
        : `<div class="nf__av nf__av--cust">${esc(dshInitial(who))}</div>`);
  const pillTxt = tag === 'voice' ? 'to you' : (tag === 'deny' ? 'held' : (CH_LABEL[tag] ? CH_LABEL[tag].toLowerCase() : tag));
  const pill = tag ? `<span class="nf__pill nf__pill--${esc(tag.replace(/[^a-z]/g, ''))}">${esc(pillTxt)}</span>` : '';
  return `<div class="nf__row">${av}<div class="nf__body"><div class="nf__line">${line}</div>${prev}</div>${pill}<div class="nf__time">${esc(relTime(e.ts))}</div></div>`;
}

// One awaiting-approval task -> a "Needs your call" row that deep-links to it.
function dshDecRow(tk, handedOver) {
  // Who brought it: the assignee, else the requester. The planner calls the
  // latter `requester`; this read `created_by`, which does not exist, so every
  // row fell back to "Studio" and showed an O that read as a zero (2026-09-24).
  // A task handed to the owner is assigned to the owner, so the face is whoever asked for it.
  const slug = handedOver ? (tk.requester || '')
    : ((tk.assignee && tk.assignee !== 'admin') ? tk.assignee : (tk.requester || ''));
  const who = slug ? dshPretty(slug) : 'Studio';
  const title = esc(tk.title || ('Task #' + tk.id));
  const desc = esc(String(tk.description || '').replace(/\s+/g, ' ').slice(0, 90));
  return `
    <div class="dec"><div class="nf__av" title="${esc(who)}">${avInner(avBySlug(slug), dshInitial(who))}</div>
      <div style="min-width:0"><div class="dec__t">${title}</div><div class="dec__d">${handedOver ? 'Waiting on you &middot; task from ' + esc(who) : (desc || 'Awaiting your approval')}</div></div>
      <div class="dec__act"><button class="sn-btn sn-btn--primary" type="button" data-id="${esc(tk.id)}">${handedOver ? 'Open' : 'Review'}</button></div>
    </div>`;
}

// Drop scaffolding/test tasks so the demo Dashboard shows real office work only.
function dshIsJunkTask(tk) {
  const t = String(tk.title || '');
  return /^test\b|\btest\b|^TEST|WS\d|breaker|isolation|link demo|smoke|placeholder/i.test(t);
}
// One open task -> an "Open work" row (deep-links to the task).
function dshOpenRow(tk) {
  const who = dshPretty((tk.assignee && tk.assignee !== 'admin') ? tk.assignee : 'Studio');
  const pr = String(tk.priority || 'med').toLowerCase();
  const prShort = pr === 'medium' ? 'med' : pr;
  return `<tr data-id="${esc(tk.id)}"><td class="owt__task">${esc(String(tk.title || ('Task #' + tk.id)).slice(0, 64))}</td><td>${esc(who)}</td><td><span class="owt__pri">${esc(prShort)}</span></td></tr>`;
}
// channel -> short label chip for the messages panel.
function dshChanLabel(ch) {
  const m = { 'web-chat': 'Chat', webchat: 'Chat', telegram: 'Telegram', internal: 'Internal', sms: 'SMS', whatsapp: 'WhatsApp' };
  return m[ch] || (ch ? ch.replace(/\b\w/g, (c) => c.toUpperCase()) : 'Msg');
}
// One inbound message -> a "Recent emails / messages" row.
// Clock time (HH:MM) for the mail-queue table; falls back to relative time.
function dshClock(ts) { try { return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }); } catch (_) { return relTime(ts); } }

// Resolve a mail address to a clean label: the owner -> "You"; an agent mailbox
// (<slug>@<node-domain>) -> the agent's name; a known contact -> that contact's
// name; anything else -> the address itself, so the owner can see who wrote.
// Is this reference the person signed in? `owner` is the node's own convention
// for "the human who owns this node"; the rest is whoever Authum says is here.
// No personal name or address may appear in this test.
function dshIsOwner(ref) {
  const s = String(ref || '').toLowerCase().trim();
  if (!s) return false;
  if (s === 'owner' || s.startsWith('owner@')) return true;
  const user = String(meIdentity.user || '').toLowerCase().trim();
  if (!user) return false;
  return s === user || s.split('@')[0] === user.split('@')[0];
}

function dshMailName(addr) {
  const s = String(addr || '').toLowerCase().trim();
  if (!s) return 'Someone';

  if (dshIsOwner(s)) return 'You';

  // An agent is recognised by matching the local part against the node's OWN
  // roster, never by a domain written into this file. The domain check is a
  // guard, not the test: with mailDomain configured a stranger who happens to
  // share a slug stays a customer; without it we still label by slug rather
  // than claiming a domain we do not know.
  const at = s.indexOf('@');
  const local = at === -1 ? s : s.slice(0, at);
  const domain = at === -1 ? '' : s.slice(at + 1);
  const ourDomain = String(meIdentity.mailDomain || '').toLowerCase();
  const domainIsOurs = ourDomain ? domain === ourDomain : true;
  if (domainIsOurs && agentBySlug(local)) return dshPretty(local);

  const known = dshContactName(s);
  if (known) return known;
  if (at !== -1) return s;
  return dshPretty(s);
}

// "Recent emails" as a compact mail-queue table: Time / direction / From / To /
// Subject / Status, both inbound and outbound. Parties resolve the same way as
// everywhere else (dshMailName): a name when the node knows one, else the address.
function dshEmailTable(rows) {
  if (!rows.length) return '<div class="ms__i"><div class="ms__pre dshm__mut">No email yet.</div></div>';
  const UP = '<i data-lucide="arrow-up" class="mq__ar"></i>';
  const DN = '<i data-lucide="arrow-down" class="mq__ar"></i>';
  const tr = (m) => {
    const out = m.direction === 'outbound';
    const from = esc(dshMailName(m.from));
    const subj = esc((m.subject || m.preview || '').slice(0, 64)) || '<span class="dshm__mut">(no subject)</span>';
    const st = out ? 'sent' : 'received';
    return `<tr><td class="mq__t">${esc(dshClock(m.ts))}</td><td class="mq__d">${out ? UP : DN}</td><td class="mq__f">${from}</td><td class="mq__s">${subj}</td><td class="mq__st">${st}</td></tr>`;
  };
  return `<div class="mq__wrap"><table class="mq"><thead><tr><th>Time</th><th></th><th>From</th><th>Subject</th><th>Status</th></tr></thead><tbody>${rows.slice(0, 12).map(tr).join('')}</tbody></table></div>`;
}

function dshMsgRow(m, withChan) {
  const out = m.direction === 'outbound';
  // who wrote, and for what went out, to whom: "Twine -> You". The arrow and the
  // recipient alone read as the sender ("You" over the agent's answer, round 2).
  const sender = dshDisplayName(m.from, m.channel) || 'Someone';
  const arr = '<i data-lucide="arrow-right" class="ms__ar"></i>';
  const from = out ? esc(sender) + arr + esc(dshDisplayName(m.to, m.channel) || 'Recipient') : esc(sender);
  const when = esc(relTime(m.ts));
  const chip = withChan ? `<span class="ms__ch">${esc(dshChanLabel(m.channel))}</span> &middot; ` : '';
  const pre = esc(reminderText(m.subject || m.preview || '').slice(0, 90)) || '<span class="dshm__mut">(no preview)</span>';
  return `<div class="ms__i"><div class="ms__top"><span class="ms__from">${from}</span><span class="ms__when">${when}</span></div><div class="ms__pre">${chip}${pre}</div></div>`;
}

// Count items whose timestamp falls within the last 24h.
function dsh24h(items, field) {
  const cut = Date.now() - 86400000;
  return items.filter((x) => { const v = x[field]; return v && new Date(v).getTime() >= cut; }).length;
}

// ---- "Working now": which agents are working, which worked recently, and on what ----
// The owner could see THAT an agent was busy (the ring in the sidebar) but not on what
// or why, and nothing showed who had just finished. Work Map answers it per task, is
// heavy and is not live. This card is per agent and reads only what is already served:
//   - who holds a turn, and the ref it holds: /api/agents/state, polled into _agentState
//   - each agent's latest turn: the journal, agent.woken (a wake by the runner) and
//     agent.turn_started (a chat, or a message that came in on a channel)
//   - what started a turn: the waking message id inside the ref, runner-<slug>-<id>-<n>
//     or dispatch:<id>. agent.wake_requested gives the planner's reason and task,
//     message.inbound and /api/inbox give the sender and the text. A chat (desk-<id>)
//     is read from its own history: the last thing the person wrote
// The Task column is what started the turn: the task the wake names, or the chat or
// message it came from. The task an agent's planner calls point at (/api/work/now) is
// deliberately not shown: an agent reads other tasks in passing, and the row then named
// a task nobody had sent it to.
// One table for both groups, so a column means the same thing in every row.
let _dshTaskTitles = {};
const _dshWakes = new Map();          // waking message id -> { reason, taskId, from, ... }; a wake never changes
const _dshDesks = new Map();          // chat id -> { text, at }; a chat moves on, so this one is re-read
const _dshTitles = new Map();         // task id -> title, for tasks the dashboard's own list does not carry
const _dshFrom = new Map();           // task id -> who the task is from (its requester)
let _dshRecent = { at: 0, list: [] }; // each agent's latest turn; minutes do not need five-second precision
let _dshNowHtml = null;
let _dshNowBusy = false;
const DSH_NOW_ROWS = 10;
const DSH_RECENT_MS = 24 * 3600 * 1000;
const DSH_SLOW_MS = 30000;
const DSH_WAKE_WORDS = {
  assigned: 'task assigned',
  mentioned: 'mentioned in a comment',
  status_changed: 'task status changed',
  reviewer_assigned: 'asked to review',
  stale_pending_review: 'review waiting too long',
  stale_in_progress: 'reminder: the task was idle',
  stale_in_progress_escalated: 'escalation: the assignee did not respond',
};
async function dshEvents(type) {
  const r = await fetch('/api/events?event_type=' + type + '&limit=200', { headers: { Accept: 'application/json' } });
  return r.ok ? ((await r.json()).events || []) : [];
}
function dshWakeId(ref) {
  const m = String(ref || '').match(/^runner-.+-(\d+)-\d+$/) || String(ref || '').match(/^dispatch:(\d+)$/);
  return m ? m[1] : null;
}
function dshDeskId(ref) {
  const r = String(ref || '');
  return r.startsWith('desk-') ? r.slice(5) : null;
}
async function dshLearnWakes(ids) {
  const unknown = ids.filter((id) => !_dshWakes.has(id));
  if (!unknown.length) return;
  const inbox = async () => {
    const r = await fetch('/api/inbox', { headers: { Accept: 'application/json' } });
    return r.ok ? ((await r.json()).messages || []) : [];
  };
  const [wakes, inbound, mail] = await Promise.all([dshEvents('agent.wake_requested'), dshEvents('message.inbound'), inbox()]);
  const found = {};
  inbound.forEach((e) => {
    const p = e.payload || {};
    found[String(e.entity_id)] = { from: p.from || '', channel: p.channel || '', preview: p.preview || '' };
  });
  // The inbox row is the message itself, so where both know an id it wins.
  mail.forEach((m) => {
    found[String(m.id)] = { from: m.from || '', channel: m.channel || '', subject: m.subject || '', preview: m.preview || '' };
  });
  wakes.forEach((e) => {
    const p = e.payload || {};
    if (p.message_id == null) return;
    const id = String(p.message_id);
    // One write can raise two wakes on the same message (made reviewer, and status
    // changed); "asked to review" is the one a person would say.
    const reason = (found[id] || {}).reason === 'reviewer_assigned' ? 'reviewer_assigned' : (p.reason || '');
    found[id] = Object.assign(found[id] || {}, { reason, taskId: p.task_id || null,
                                                 ts: e.ts, agent: String(e.entity_id || '') });
  });
  // An id the journal no longer returns is remembered as empty, so it is asked once.
  unknown.forEach((id) => _dshWakes.set(id, found[id] || {}));
}
// What the person last wrote in each of these chats. One small request per chat on show.
async function dshLearnDesks(ids) {
  const now = Date.now();
  await Promise.all(ids.filter((id) => !_dshDesks.has(id) || now - _dshDesks.get(id).at > DSH_SLOW_MS).map(async (id) => {
    let text = '';
    try {
      const r = await fetch('/api/desks/' + encodeURIComponent(id) + '/history', { headers: { Accept: 'application/json' } });
      if (r.ok) {
        const all = (await r.json()).messages || [];
        const asked = all.filter((m) => m.role === 'user');
        const last = asked[asked.length - 1] || all[all.length - 1];
        text = last ? String(last.content || '').replace(/\s+/g, ' ').trim().slice(0, 200) : '';
      }
    } catch (_) { /* the row falls back to "in a chat" */ }
    _dshDesks.set(id, { text, at: now });
  }));
}
// Who caused a planner wake. The event names the reason and the task, not the person.
// The task's comment log has it: the planner writes the status change, or the comment
// that mentions the agent, in the same second it raises the wake. So the comment made
// just before the wake is the cause, and its author is the answer. An assignment and a
// reminder from the node leave no such comment; those rows say who the task is from.
const DSH_ACTOR_REASONS = { status_changed: 'status', reviewer_assigned: 'status', mentioned: 'mention' };
async function dshLearnActors(ids) {
  await Promise.all(ids.map(async (id) => {
    const w = _dshWakes.get(id);
    const kind = w && DSH_ACTOR_REASONS[w.reason];
    if (!kind || !w.taskId || !w.ts || w.asked) return;
    w.asked = true;                                 // a wake never changes: one look is enough
    try {
      const r = await fetch('/api/tasks/' + encodeURIComponent(w.taskId) + '/comments', { headers: { Accept: 'application/json' } });
      if (!r.ok) return;
      const body = await r.json();
      const at = Date.parse(w.ts);
      const near = (Array.isArray(body) ? body : (body.comments || []))
        .filter((c) => { const d = at - Date.parse(c.created_at); return d >= -2000 && d <= 10000; })
        .filter((c) => (kind === 'status' ? c.comment_type === 'status_change'
                                          : String(c.content || '').includes('@' + w.agent)));
      if (near.length) w.by = near[near.length - 1].author || '';
    } catch (_) { /* the row keeps the reason without a name */ }
  }));
}
// The dashboard's task list is the planner's first page; a task outside it is asked for once.
async function dshLearnTitles(ids) {
  await Promise.all(ids.filter((id) => !(id in _dshTaskTitles) && !_dshTitles.has(id)).map(async (id) => {
    let title = '';
    try {
      const r = await fetch('/api/tasks/' + encodeURIComponent(id), { headers: { Accept: 'application/json' } });
      if (r.ok) {
        const t = await r.json();
        title = String(t.title || '');
        _dshFrom.set(id, t.requester || '');
      }
    } catch (_) { /* the row shows the number alone */ }
    _dshTitles.set(id, title);
  }));
}
// Each agent's latest turn within the last day, newest first.
async function dshRecentTurns() {
  if (Date.now() - _dshRecent.at < DSH_SLOW_MS) return _dshRecent.list;
  const [woken, started] = await Promise.all([dshEvents('agent.woken'), dshEvents('agent.turn_started')]);
  const last = {};
  const note = (slug, ref, ts) => {
    const t = Date.parse(ts) || 0;
    if (slug && ref && (!last[slug] || t > last[slug].t)) last[slug] = { slug, ref, ts, t };
  };
  woken.forEach((e) => note(String(e.entity_id || ''), (e.payload || {}).ref, e.ts));
  started.forEach((e) => {
    const p = e.payload || {}, actor = String(e.actor || '');
    if (!actor.startsWith('agent:')) return;
    note(actor.slice(6), p.ref || (p.message_id != null ? 'dispatch:' + p.message_id : ''), e.ts);
  });
  const since = Date.now() - DSH_RECENT_MS;
  _dshRecent = { at: Date.now(), list: Object.values(last).filter((r) => r.t >= since).sort((a, b) => b.t - a.t) };
  return _dshRecent.list;
}
function dshWhy(ref) {
  const r = String(ref || '');
  const deskId = dshDeskId(r);
  if (deskId) return { text: (_dshDesks.get(deskId) || {}).text || 'in a chat', deskId };
  const id = dshWakeId(r);
  const w = (id && _dshWakes.get(id)) || {};
  if (w.reason) {
    let text = DSH_WAKE_WORDS[w.reason] || w.reason.replace(/_/g, ' ');
    const from = w.taskId ? _dshFrom.get(String(w.taskId)) : '';
    if (w.by) text += ' by ' + dshDisplayName(w.by);
    else if (from) text += ' \u00b7 task from ' + dshDisplayName(from);
    return { text, taskId: w.taskId };
  }
  if (w.from && w.from !== 'system') {
    // "email from Ann: Re: the quote", "message from Sam: first words of it".
    const kind = w.channel && w.channel !== 'internal' ? dshChanLabel(w.channel).toLowerCase() : 'message';
    const what = String(w.subject || w.preview || '').trim();
    return { text: kind + ' from ' + dshDisplayName(w.from, w.channel) + (what ? ': ' + what : ''), msg: true };
  }
  if (id) return { text: 'a message in its inbox', msg: true };
  return { text: describeRef(r) };
}
// How long a turn has been running, in the words a person would use.
function dshFor(ts) {
  const m = Math.max(0, Math.floor((Date.now() - (Date.parse(ts) || Date.now())) / 60000));
  if (m < 1) return 'just started';
  if (m < 60) return 'for ' + m + ' min';
  return 'for ' + Math.floor(m / 60) + ' h ' + String(m % 60).padStart(2, '0') + ' min';
}
// the same short form as the feed and the chat list ("2 h"): the table said
// "2 h ago" beside the feed's "2 h"
function dshAgo(ts) { return relTime(ts); }
// One row of the table. live: the agent holds a turn now.
function dshNowRow(slug, name, ref, ts, live) {
  const why = dshWhy(ref);
  const taskId = why.taskId || null;
  let task = '<span class="wnt__no">&ndash;</span>';
  if (taskId) {
    const title = _dshTaskTitles[String(taskId)] || _dshTitles.get(String(taskId)) || '';
    task = `<a data-task="${esc(String(taskId))}">#${esc(String(taskId))}${title ? ' ' + esc(title) : ''}</a>`;
  } else if (why.deskId) {
    // the chat by its name: a bare "chat" / "message" said nothing
    const d = desks.find((x) => x.id === why.deskId);
    task = `<a data-desk="${esc(why.deskId)}">${esc((d && d.title) || 'A chat')}</a>`;
  } else if (why.msg) {
    task = '<span class="wnt__no">A message</span>';
  }
  const said = String(why.text || '').replace(/Reminder due now, [\s\S]*/, (w) => reminderText(w));
  const when = live ? dshFor(ts) : dshAgo(ts);
  return `<div class="wnt__r${live ? ' is-now' : ''}">
    <div class="wnt__c wnt__a"><div class="sn-av">${avInner(avBySlug(slug), dshInitial(name))}</div><b>${esc(name)}</b></div>
    <div class="wnt__c wnt__k">${task}</div>
    <div class="wnt__c wnt__w" title="${esc(said)}">${esc(said)}</div>
    <div class="wnt__c wnt__t${live ? ' is-live' : ''}" title="${esc((live ? 'since ' : '') + new Date(ts).toLocaleString())}">${esc(when)}</div>
  </div>`;
}
async function dshLoadNow() {
  const box = $('#dshNow'), count = $('#dshNowN');
  // Not on the dashboard, state not known yet (the skeleton stays), or a paint in flight.
  if (!box || !_agentStateLoaded || _dshNowBusy) return;
  _dshNowBusy = true;
  try {
    const busy = Object.values(_agentState).filter((a) => a.status === 'busy' && !a.stale)
      .sort((a, b) => (Date.parse(a.updated_at) || 0) - (Date.parse(b.updated_at) || 0));
    const live = new Set(busy.map((a) => a.slug));
    const past = (await dshRecentTurns())
      .filter((r) => _agentState[r.slug] && !live.has(r.slug))
      .slice(0, Math.max(0, DSH_NOW_ROWS - busy.length));
    const refs = busy.map((a) => a.current_ref).concat(past.map((r) => r.ref));
    await Promise.all([
      dshLearnWakes(refs.map(dshWakeId).filter(Boolean)),
      dshLearnDesks(refs.map(dshDeskId).filter(Boolean)),
    ]);
    await dshLearnActors(refs.map(dshWakeId).filter(Boolean));
    await dshLearnTitles(refs.map((ref) => dshWhy(ref).taskId).filter(Boolean).map(String));

    const rows = busy.map((a) => dshNowRow(a.slug, a.title || dshPretty(a.slug), a.current_ref, a.updated_at, true))
      .concat(past.map((r) => dshNowRow(r.slug, (_agentState[r.slug] || {}).title || dshPretty(r.slug), r.ref, r.ts, false)));
    const head = '<div class="wnt__r wnt__h"><div class="wnt__c">Agent</div><div class="wnt__c">On</div><div class="wnt__c">What started it</div><div class="wnt__c wnt__t">When</div></div>';
    // with nobody at work the rows are the latest turns, and the line says so:
    // "Nobody is working" over a row of an agent read as a contradiction
    const html = (busy.length ? '' : `<div class="wn__none dshm__mut">Nobody is working right now.${past.length ? ' The latest work:' : ''}</div>`)
      + (rows.length ? head + rows.join('') : '');
    if (count) count.textContent = String(busy.length);
    if (html === _dshNowHtml && box.firstElementChild && !box.querySelector('.skel')) return;   // same picture: leave the DOM alone
    _dshNowHtml = html;
    box.innerHTML = html;
    box.querySelectorAll('a[data-task]').forEach((el) => el.addEventListener('click', () => openTask(el.dataset.task)));
    box.querySelectorAll('a[data-desk]').forEach((el) => el.addEventListener('click', () => openDesk(el.dataset.desk)));
  } catch (_) { /* transient; the next poll repaints */ }
  finally { _dshNowBusy = false; }
}

async function dshLoadLive() {
  const feed = $('#dshFeed'), decs = $('#dshDecs'), decN = $('#dshDecN');
  const openBox = $('#dshOpen'), statsBox = $('#dshStats'), okBadge = $('#dshOkBadge');
  const emailsBox = $('#dshEmails'), msgsBox = $('#dshMsgs');
  try {
    const [actR, tkR, inR] = await Promise.all([
      fetch('/api/activity', { headers: { Accept: 'application/json' } }),
      tasksFetch('/api/tasks'),  // proxy drops query; filter client-side
      fetch('/api/inbox', { headers: { Accept: 'application/json' } }),
    ]);

    let actEmails = [];
    if (actR.ok) {
      const aj = await actR.json();
      const evs = aj.events || [];
      actEmails = aj.emails || [];
      if (feed) feed.innerHTML = evs.length
        ? evs.slice(0, 12).map(dshActLine).join('')
        : '<div class="sn-empty">No activity yet. Ask an agent something in Chats.</div>';   // the kit's empty line: the feed row's layout broke it word by word
    } else if (feed) feed.innerHTML = '<div class="sn-empty">Could not load the activity. Reload the page to try again.</div>';

    if (tkR.ok) {
      const body = await tkR.json();
      const all = (Array.isArray(body) ? body : (body.tasks || body.items || [])) || [];
      _dshTaskTitles = {};
      all.forEach((t) => { _dshTaskTitles[String(t.id)] = t.title || ''; _dshFrom.set(String(t.id), t.requester || ''); });
      dshLoadNow();                               // titles just arrived; do not wait for the next state poll
      const waiting = all.filter((t) => tkStage(t) === 'awaiting_approval');
      // Work that stands on the owner because somebody handed it over: assigned to the
      // owner, asked for by somebody else, not finished. The owner's own tasks under
      // his own name are not listed; nobody is waiting on those.
      const handed = all.filter((t) => tkStage(t) !== 'awaiting_approval' && !['done', 'rejected', 'escalated'].includes(tkStage(t))
        && dshIsOwner(t.assignee) && !dshIsOwner(t.requester) && !dshIsJunkTask(t));
      const calls = waiting.map((t) => dshDecRow(t)).concat(handed.map((t) => dshDecRow(t, true)));
      const inProg = all.filter((t) => tkStage(t) === 'in_progress');
      const open = all.filter((t) => tkStage(t) === 'open' && !dshIsJunkTask(t));
      const doneToday = dsh24h(all.filter((t) => tkStage(t) === 'done'), 'completed_at');

      if (decs) {
        if (decN) decN.textContent = calls.length || '0';
        decs.innerHTML = calls.length
          ? calls.slice(0, 8).join('')
          : '<div class="dec__d" style="padding:6px 2px">Nothing waiting on you.</div>';
        decs.querySelectorAll('button[data-id]').forEach((b) => b.addEventListener('click', () => openTask(b.dataset.id)));
      }
      if (openBox) {
        openBox.innerHTML = open.length
          ? `<table class="owt"><thead><tr><th>Task</th><th>Owner</th><th>Pri</th></tr></thead><tbody>${open.slice(0, 14).map(dshOpenRow).join('')}</tbody></table>`
          : '<div class="up__i"><div class="up__t dshm__mut">No open work.</div></div>';
        openBox.querySelectorAll('tr[data-id]').forEach((el) => el.addEventListener('click', () => openTask(el.dataset.id)));
      }
      if (statsBox) {
        const stat = (n, l, cls) => `<div class="gl__row"><span class="gl__l">${l}</span><span class="gl__n ${cls || ''}">${n}</span></div>`;
        statsBox.innerHTML = [
          stat(open.length, 'open'),
          stat(inProg.length, 'in&nbsp;progress'),
          stat(waiting.length, 'waiting', waiting.length ? 'attn' : ''),
          stat(doneToday, 'done&nbsp;24h'),
        ].join('');
      }
      if (okBadge) okBadge.innerHTML = waiting.length ? `<i></i>${waiting.length} to review` : '<i></i>All good';
    } else {
      // a placeholder that never resolves reads as broken: say it could not load
      const fail = '<div class="dec__d dshm__mut" style="padding:6px 2px">Could not load tasks.</div>';
      [decs, openBox, statsBox].forEach((b) => { if (b) b.innerHTML = fail; });
      if (decN) decN.textContent = '';
    }

    if (inR.ok && (emailsBox || msgsBox)) {
      const msgs = ((await inR.json()).messages) || [];
      const emails = msgs.filter((m) => m.channel === 'email');
      const others = msgs.filter((m) => m.channel !== 'email' && m.channel !== 'system');  // system = notification noise
      if (emailsBox) emailsBox.innerHTML = dshEmailTable(emails);
      if (msgsBox) msgsBox.innerHTML = others.length
        ? others.slice(0, 4).map((m) => dshMsgRow(m, true)).join('')
        : '<div class="ms__i"><div class="ms__pre dshm__mut">No recent messages.</div></div>';
    } else if (!inR.ok) {
      const fail = '<div class="ms__i"><div class="ms__pre dshm__mut">Could not load messages. Reload the page to try again.</div></div>';
      if (emailsBox) emailsBox.innerHTML = fail;
      if (msgsBox) msgsBox.innerHTML = fail;
    }
    if (window.lucide) try { lucide.createIcons(); } catch (_) {}   // render Lucide icons in the freshly-rebuilt feed/tables
  } catch (_) { /* transient; next poll retries */ }
}


/* ---- Documents view (doci-backed): left directory tree | center document |
   right recent. One /api/docs fetch feeds both -- the doci engine returns
   documents newest-modified first. Markdown rendered with snFormatter. ---- */
let _docsAll = [];
let _dkCurrent = null;

function dkDate(v) {
  if (!v) return '';
  const d = new Date(v); if (isNaN(d)) return esc(v);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// Build a nested folder tree from flat document paths ("a/b/doc").
function dkBuildTree(docs) {
  const root = { dirs: {}, files: [] };
  for (const d of docs) {
    const parts = String(d.path).split('/');
    const file = parts.pop();
    let node = root;
    for (const seg of parts) { node.dirs[seg] = node.dirs[seg] || { dirs: {}, files: [] }; node = node.dirs[seg]; }
    node.files.push({ name: file, path: d.path, title: d.title, guid: d.guid });
  }
  return root;
}

/* expanded-directory state persists in localStorage (sn-docs-tree = list of dir paths) */
function _dkTreeOpen() {
  try { return new Set(JSON.parse(localStorage.getItem('sn-docs-tree') || '[]')); } catch (e) { return new Set(); }
}
function _dkTreeSave(set) {
  try { localStorage.setItem('sn-docs-tree', JSON.stringify([...set])); } catch (e) {}
}

function dkRenderTree(node, depth, prefix, open) {
  prefix = prefix || '';
  open = open || _dkTreeOpen();
  let html = '';
  for (const name of Object.keys(node.dirs).sort()) {
    const pad = depth * 12;
    const key = prefix + '/' + name;
    const isOpen = open.has(key);
    html += `<div class="dktree__dir" data-dir="${esc(key)}" style="padding-left:${pad}px"><span class="dktree__tw">${isOpen ? '[-]' : '[+]'}</span> ${esc(name)}</div>`;
    html += `<div class="dktree__kids${isOpen ? ' is-open' : ''}">${dkRenderTree(node.dirs[name], depth + 1, key, open)}</div>`;
  }
  for (const f of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
    const pad = depth * 12 + 14;
    html += `<div class="dktree__file" data-id="${esc(f.guid || f.path)}" data-path="${esc(f.path)}" style="padding-left:${pad}px" title="${esc(f.path)}">${esc(f.title || f.name)}</div>`;
  }
  return html;
}

function dkPaintTree() {
  const box = $('#dkTree'); if (!box) return;
  if (!_docsAll.length) { box.innerHTML = '<div class="dk__mut">No documents yet.</div>'; return; }
  box.innerHTML = dkRenderTree(dkBuildTree(_docsAll), 0);
  box.querySelectorAll('.dktree__file').forEach((el) => el.addEventListener('click', () => openDoc(el.dataset.id)));
  box.querySelectorAll('.dktree__dir').forEach((el) => el.addEventListener('click', () => {
    const kids = el.nextElementSibling, tw = el.querySelector('.dktree__tw');
    const open = kids && kids.classList.toggle('is-open'); if (tw) tw.textContent = open ? '[-]' : '[+]';
    const set = _dkTreeOpen(); if (open) set.add(el.dataset.dir); else set.delete(el.dataset.dir); _dkTreeSave(set);
  }));
  if (_dkCurrent) dkMark(_dkCurrent);
}

function dkPaintRecent() {
  const box = $('#dkRecent'); if (!box) return;
  // Newest first. This used to take the first fifteen rows in whatever order
  // /api/docs returned them, which is tree order, so the column showed the same
  // list forever and the word Recent meant nothing. Same idiom as every other
  // list in this file (chats, done tasks, the task table).
  const recent = _docsAll.slice()
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
    .slice(0, 15);
  if (!recent.length) { box.innerHTML = '<div class="dk__mut">Nothing yet.</div>'; return; }
  box.innerHTML = recent.map((d) => `
    <div class="dkrecent__it" data-id="${esc(d.guid || d.path)}" title="${esc(d.path)}">
      <div class="dkrecent__t">${esc(d.title || d.path)}</div>
      <div class="dkrecent__m">${dkDate(d.updated_at)}</div>
    </div>`).join('');
  box.querySelectorAll('.dkrecent__it').forEach((el) => el.addEventListener('click', () => openDoc(el.dataset.id)));
}

function dkMark(id) {
  $$('#dkTree .dktree__file').forEach((el) => el.classList.toggle('is-active', el.dataset.id === id || el.dataset.path === id));
}

async function loadDocs() {
  try {
    const r = await fetch('/api/docs', { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error(`API ${r.status}`);
    const data = await r.json();
    _docsAll = Array.isArray(data.documents) ? data.documents : [];
  } catch (e) { _docsAll = []; }
  dkPaintTree();
  dkPaintRecent();
}

function openDoc(path) { history.pushState({}, '', '/docs/' + encodeURIComponent(path)); setView('docs'); loadDoc(path); }

function dkStripTitle(md, title) {
  if (!md || !title) return md || '';
  // Header already shows the metadata title; Doci bodies repeat it as a leading
  // H1 (sometimes a variant), so drop the first leading H1 unconditionally.
  var m = md.match(/^\s*#\s+.+?[ \t]*(?:\n+|$)/);
  return m ? md.slice(m[0].length) : md;
}
async function loadDoc(path) {
  const center = $('#dkCenter'); if (!center) return;
  _dkCurrent = path; dkMark(path);
  center.innerHTML = skText(10);
  try {
    const isGuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(path);
    const qp = (isGuid ? 'guid=' : 'path=') + encodeURIComponent(path);
    const r = await fetch(`/api/docs/doc?${qp}&content=1`, { headers: { Accept: 'application/json' } });
    if (!r.ok) {
      // Doci's by-path lookup fails for HTML documents; resolve the guid from the
      // catalogue and retry by guid (which works). Deep links by path land here.
      if (!isGuid) {
        try {
          if (!_docsAll.length) { _docsAll = ((await (await fetch('/api/docs')).json()).documents) || []; }
          const norm = (p) => String(p || '').replace(/\.(md|html|svg)$/i, '');
          const hit = _docsAll.find((d) => d.guid && norm(d.path) === norm(path));
          if (hit) return loadDoc(hit.guid);
        } catch (_) {}
      }
      throw new Error(`API ${r.status}`);
    }
    const doc = (await r.json()).document || {};
    // HTML/SVG documents render inside a sandboxed iframe whose srcdoc is set
    // AFTER innerHTML replacement -- this is the ONLY way to render untrusted
    // markup without letting <script> in a doc run in the Studio origin (any
    // authorized writer can create a doc, so the payload is effectively user-
    // controlled). The sandbox="" attribute blocks scripts, forms, and same-
    // origin access. Full HTML docs, HTML fragments, and SVG files all take
    // this path; markdown continues to render through the formatter.
    const raw = doc.content || '';
    const isFullDoc = /^\s*<(?:!doctype html|html)\b/i.test(raw);
    const docPath = doc.path || path;
    const isHtmlFile = /\.html$/i.test(docPath);
    const isSvgFile  = /\.svg$/i.test(docPath);
    const isFragment = isHtmlFile && !isFullDoc;
    // srcdoc for full HTML: use the doc verbatim.
    // srcdoc for HTML fragment: wrap in a bare <!doctype html> shell so
    //   relative CSS still applies and default margins are sane.
    // srcdoc for SVG: wrap the <svg>...</svg> in a minimal HTML shell that
    //   centers the image and lets it size to its viewBox.
    const svgShell = (svg) => (
      '<!doctype html><meta charset="utf-8">'
      + '<style>html,body{margin:0;padding:0;background:transparent}'
      + 'svg{display:block;max-width:100%;height:auto;margin:0 auto}</style>'
      + svg
    );
    const fragmentShell = (frag) => (
      '<!doctype html><meta charset="utf-8">'
      + '<style>html,body{margin:0;padding:0;font-family:system-ui,sans-serif;'
      + 'color:inherit;background:transparent}</style>'
      + frag
    );
    let iframeSrcdoc = null;
    if (isFullDoc)       iframeSrcdoc = raw;
    else if (isSvgFile)  iframeSrcdoc = svgShell(raw);
    else if (isFragment) iframeSrcdoc = fragmentShell(raw);
    const useIframe = iframeSrcdoc !== null;
    const tags = Array.isArray(doc.tags) && doc.tags.length
      ? `<div class="td__chips">${doc.tags.map((t) => `<span class="tkt__chip">${esc(t)}</span>`).join('')}</div>` : '';
    const author = doc.updated_by || doc.created_by;
    // sandbox="allow-same-origin" without allow-scripts: no JS runs inside
    // (an SVG <script>, a <script> in a fragment, or JS in a full HTML doc
    // are all blocked), forms cannot POST, plugins are disabled. Keeping
    // allow-same-origin lets the parent measure the iframe's scrollHeight
    // so the frame can auto-fit its content -- safe here precisely because
    // scripts can't run to abuse the same-origin permission. Every render
    // path -- SVG, HTML fragment, full HTML page -- uses this same wrapper.
    const bodyHtml = useIframe
      ? '<iframe id="dkHtml" class="docbody docbody--html" sandbox="allow-same-origin" style="width:100%;border:0;background:transparent;min-height:400px"></iframe>'
      : `<div class="docbody">${window.snFormatter ? window.snFormatter.markdown(dkStripTitle(raw, doc.title)) : esc(raw)}</div>`;
    center.innerHTML = `
      <div class="td__crumb"><span class="tkt__mut">${esc(docPath)}</span></div>
      <h2 class="td__h">${esc(doc.title || path)}</h2>
      ${tags}
      <div class="td__meta" style="margin-bottom:14px">
        ${author ? `<div class="td__row"><span class="td__k">Author</span><span class="td__v">${esc(author)}</span></div>` : ''}
        <div class="td__row"><span class="td__k">Updated</span><span class="td__v">${dkDate(doc.updated_at || doc.created_at) || '&mdash;'}</span></div>
      </div>
      ${bodyHtml}
      <div class="td__sec" id="dkVersionsH" style="display:none">Versions</div>
      <div class="td__cmts" id="dkVersions"></div>
      <div class="td__sec" id="dkThreadsH" style="display:none">Discussions</div>
      <div class="td__cmts" id="dkThreads"></div>`;
    if (useIframe) {
      const f = document.getElementById('dkHtml');
      if (f) {
        // allow-same-origin lets us read contentDocument to auto-fit the
        // frame to its inner content. Scripts still can't run, so the
        // measurement is the only cross-boundary interaction.
        const fit = () => {
          try {
            const inner = f.contentDocument && f.contentDocument.documentElement;
            const h = inner ? inner.scrollHeight : 0;
            if (h > 0) f.style.height = (h + 24) + 'px';
            else f.style.height = isSvgFile ? '520px' : '1500px';
          } catch (_) {
            f.style.height = isSvgFile ? '520px' : '1500px';
          }
        };
        f.addEventListener('load', fit);
        f.srcdoc = iframeSrcdoc;
        setTimeout(fit, 300);
      }
    }
    if (doc.guid) loadDocSide(doc.guid);
  } catch (e) {
    center.innerHTML = `<div class="tkt__empty">Could not load document: ${esc(e.message)}</div>`;
  }
}

// Versions + discussion threads -- read-only side panels under the document.
async function loadDocSide(guid) {
  try {
    const vr = await fetch(`/api/docs/versions?document=${encodeURIComponent(guid)}`, { headers: { Accept: 'application/json' } });
    if (vr.ok) {
      const versions = ((await vr.json()).versions) || [];
      if (versions.length && $('#dkVersionsH')) {
        $('#dkVersionsH').style.display = '';
        $('#dkVersions').innerHTML = versions.map((v) => `<div class="td__cmt"><div class="td__cmt-h"><span class="td__cmt-a">${esc(v.created_by || '—')}</span><span class="tkt__mut">${dkDate(v.created_at)}</span></div></div>`).join('');
      }
    }
  } catch (_) {}
  try {
    const tr = await fetch(`/api/docs/threads?document=${encodeURIComponent(guid)}`, { headers: { Accept: 'application/json' } });
    if (tr.ok) {
      const td = await tr.json();
      const threads = Array.isArray(td.threads) ? td.threads : (Array.isArray(td) ? td : []);
      if (threads.length && $('#dkThreadsH')) {
        $('#dkThreadsH').style.display = '';
        $('#dkThreads').innerHTML = threads.map((t) => `<div class="td__cmt"><div class="td__cmt-h"><span class="td__cmt-a">${esc(t.created_by || t.author || '—')}</span><span class="tkt__mut">${dkDate(t.created_at)}</span></div><div class="td__cmt-b">${esc(t.quote || t.title || '')}</div></div>`).join('');
      }
    }
  } catch (_) {}
}

/* ---- Settings: the owner's notification channels (email / telegram / whatsapp
   / sms). Loaded from GET /api/users/{id}/notify-channels, each row saved with a
   PUT on toggle or address blur. Targets the demo owner (user id 1); identity is
   forwarded server-side via Remote-User. ---- */
const NOTIFY_USER_ID = 1;
// Only the channels the node sends the owner something on, each with what it
// sends (core-api lib/owner_alert.py: email; routes/notifications.py: email and
// telegram). WhatsApp and SMS had fields and no sender.
const NOTIFY_CHANNELS = [
  { channel: 'email', label: 'Email', placeholder: 'name@example.com', what: 'When agents are stopped, the daily summary, tasks assigned to you' },
  { channel: 'telegram', label: 'Telegram', placeholder: '@username or chat id', what: 'Tasks assigned to you' },
];

async function loadModules() {
  const card = $('#modulesCard'); if (!card) return;
  let mods = [];
  try { mods = (await (await fetch('/api/node/components', { headers: { Accept: 'application/json' } })).json()).modules || []; } catch { /* the section stays hidden */ }
  // What this node has. Modules are chosen at install: a list with nothing to press helps nobody here.
  mods = mods.filter((m) => m.installed);
  if (!mods.length) return;
  mods.sort((a, b) => a.title.localeCompare(b.title));
  $('#modulesList').innerHTML = mods.map((m) => `<div class="sn-row"><span class="sn-row__main"><span class="sn-row__title">${esc(m.title)}</span>`
    + `<span class="sn-row__sub">${esc(m.provides)}</span></span></div>`).join('');
  card.hidden = false;
}

async function loadNotifyChannels() {
  const box = $('#notifyChannels'); if (!box) return;
  // everything to the owner goes out through the Communication module
  await window.snComponents;
  if (window.snInstalled && !window.snInstalled.has('communication')) {
    box.innerHTML = '<div class="sn-empty">Notifications are sent by the Communication module, which is not installed on this node.</div>';
    return;
  }
  let saved = {};
  try {
    const r = await fetch(`/api/users/${NOTIFY_USER_ID}/notify-channels`, { headers: { Accept: 'application/json' } });
    if (r.ok) {
      const data = await r.json();
      saved = Object.fromEntries(((data && data.channels) || []).map((c) => [c.channel, c]));
    }
  } catch (e) { /* render defaults below; PUT still works once backend is up */ }

  // a row per channel: what it sends, the address, on or off; saved on change
  box.innerHTML = '<div class="sn-group">' + NOTIFY_CHANNELS.map((ch) => {
    const c = saved[ch.channel] || {};
    const addr = (c.address || '').trim();  // owner email comes only from the DB (user_channels); no hardcoded default
    return `<label class="sn-row"><span class="sn-row__main"><span class="sn-row__title">${ch.label}</span><span class="sn-row__sub sn-row__sub--wrap">${esc(ch.what)}</span></span>`
      + `<span class="sn-field agp-fieldw"><input type="text" value="${esc(addr)}" placeholder="${esc(ch.placeholder)}" data-ntaddr="${ch.channel}"></span>`
      + `<span class="sn-switch"><input type="checkbox" role="switch" ${c.enabled ? 'checked' : ''} data-ntsw="${ch.channel}"></span></label>`;
  }).join('') + '</div>';
  box.onchange = (e) => {
    const ch = e.target.dataset.ntaddr || e.target.dataset.ntsw; if (!ch) return;
    const addr = box.querySelector(`[data-ntaddr="${ch}"]`), sw = box.querySelector(`[data-ntsw="${ch}"]`);
    saveNotifyChannel(ch, (addr.value || '').trim(), sw.checked);
  };
}

async function saveNotifyChannel(channel, address, enabled) {
  try {
    const r = await fetch(`/api/users/${NOTIFY_USER_ID}/notify-channels/${encodeURIComponent(channel)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ address, enabled }),
    });
    // the toast, as on the agent page: a hint line under the list went unseen
    window.snToast(r.ok ? 'Saved' : `Could not save (${r.status})`);
  } catch (e) {
    window.snToast('Could not save: network error');
  }
}

(function initWorkspace() {
  const sel = (id, opts) => { const el = $(id); opts.forEach((o) => { const op = document.createElement('option'); op.value = o; op.textContent = o; el.appendChild(op); }); };
  sel('#fPriority', TK_PRIORITIES); tkLoadDicts();
  ['#fStatus', '#fPriority'].forEach((id) => $(id).addEventListener('input', tkRender));
  $('#tkWho').addEventListener('click', tkWhoToggle);
  $('#navTasks').addEventListener('click', () => { history.pushState({}, '', '/tasks'); setView('tasks'); loadTasks(); });
  { const e = $('#navPlannerDash'); if (e) e.addEventListener('click', () => { history.pushState({}, '', '/tasks/dashboard'); setView('tasksdash'); loadTasksDash(); }); }
  // Work Map lives as an SPA section (#workMapView). The runtime in work-map.js
  // exposes window.renderWorkMap / window.teardownWorkMap; the nav click opens
  // it the same way other sections do, so the Studio sidebar stays wrapped around it.
  { const e = $('#navWorkMap'); if (e) e.addEventListener('click', () => { history.pushState({}, '', '/work-map'); setView('workmap'); if (window.renderWorkMap) window.renderWorkMap(); }); }
  { const e = $('#tkDashLink'); if (e) e.addEventListener('click', () => { history.pushState({}, '', '/tasks/dashboard'); setView('tasksdash'); loadTasksDash(); }); }
  { const e = $('#tdashToList'); if (e) e.addEventListener('click', () => { history.pushState({}, '', '/tasks'); setView('tasks'); loadTasks(); }); }
  const navC = $('#navChats'); if (navC) navC.addEventListener('click', () => { history.pushState({}, '', '/'); setView('chats'); });
  const navD = $('#navDash'); if (navD) navD.addEventListener('click', () => { history.pushState({}, '', '/dashboard'); setView('dash'); loadDashboard(); });
  // The phone tab bar does what the sidebar entry of the same section does.
  document.querySelectorAll('#tabbar [data-for]').forEach((b) => b.addEventListener('click', () => { const n = $(b.dataset.for); if (n) n.click(); }));
  const back = $('#tdBack'); if (back) back.addEventListener('click', () => { history.pushState({}, '', '/tasks'); setView('tasks'); loadTasks(); });
  const navDk = $('#navDocs'); if (navDk) navDk.addEventListener('click', () => { history.pushState({}, '', '/docs'); setView('docs'); loadDocs(); });
  const navAg = $('#navAgents'); if (navAg) navAg.addEventListener('click', () => { history.pushState({}, '', '/agents'); setView('agents'); renderAgentsGrid(); });
  const navWf = $('#navWorkflows'); if (navWf) navWf.addEventListener('click', () => { history.pushState({}, '', '/workflows'); setView('workflows'); _wfState = { level: 'list', name: null, runId: null }; renderWorkflows(); });
  const navSt = $('#navSettings'); if (navSt) navSt.addEventListener('click', () => { history.pushState({}, '', '/settings'); setView('settings'); loadNotifyChannels(); loadModules(); loadPanelLink(); loadSignInLink(); loadAbout(); });
  // A module's paths, by the menu entry that leads there: its data-needs names the
  // module and its title the page. Not installed: the page says so, instead of an
  // empty screen ("No documents yet" on a node with no Documents, tester 2026-10-30).
  const MODULE_PATHS = { '/docs': '#navDocs', '/workflows': '#navWorkflows', '/tasks': '#navTasks', '/work-map': '#navWorkMap' };
  function route() {
    const p = snPath();
    const nav = $(Object.entries(MODULE_PATHS).find(([k]) => p === k || p.startsWith(k + '/'))?.[1] || '#none');
    const need = nav && (nav.dataset.needs || (nav.closest('[data-needs]') || {}).dataset?.needs);
    if (need) {
      return window.snComponents.then(() => {
        // the list unknown (its call failed): the module's own screen, as before
        if (!window.snInstalled || window.snInstalled.has(need)) return routeTo(p);
        $('#missingT').textContent = nav.title || need;
        setView('missing');
      });
    }
    routeTo(p);
  }
  function routeTo(p) {
    if (p === '/tasks/dashboard') { setView('tasksdash'); loadTasksDash(); }
    else if (/^\/tasks\/[^/]+$/.test(p)) { setView('detail'); loadDetail(p.split('/')[2]); }
    else if (p === '/tasks') { setView('tasks'); loadTasks(); }
    else if (p === '/dashboard') { setView('dash'); loadDashboard(); }
    else if (/^\/docs\/[^/]+$/.test(p)) { setView('docs'); loadDocs(); loadDoc(decodeURIComponent(p.split('/')[2])); }
    else if (p === '/docs') { setView('docs'); loadDocs(); }
    else if (p === '/agents' || p === '/agents/') { setView('agents'); renderAgentsGrid(); }
    else if (p === '/workflows') { setView('workflows'); _wfState = { level: 'list', name: null, runId: null }; renderWorkflows(); }
    else if (p === '/settings') { setView('settings'); loadNotifyChannels(); loadModules(); loadPanelLink(); loadSignInLink(); loadAbout(); }
    else if (p === '/work-map') { setView('workmap'); if (window.renderWorkMap) window.renderWorkMap(); }
    else if (/^\/workflows\/([^/]+)\/runs\/([^/]+)$/.test(p)) { const m = p.split('/'); setView('workflows'); _wfState = { level: 'run', name: decodeURIComponent(m[2]), runId: decodeURIComponent(m[4]) }; renderWorkflows(); }
    else if (/^\/workflows\/([^/]+)$/.test(p)) { const m = p.split('/'); setView('workflows'); _wfState = { level: 'detail', name: decodeURIComponent(m[2]), runId: null }; renderWorkflows(); }
    else if (/^\/agents\/([^/]+)\/mail\/([^/]+)$/.test(p)) {
      const parts = p.split('/');
      openAgentPage(decodeURIComponent(parts[2]), { tab: 'mail', counterpart: decodeURIComponent(parts[4]), noHistory: true });
    }
    else if (/^\/agents\/([^/]+)\/chats\/([^/]+)$/.test(p)) {
      const parts = p.split('/');
      openAgentPage(decodeURIComponent(parts[2]), { tab: 'chats', deskId: decodeURIComponent(parts[4]), noHistory: true });
    }
    else if (/^\/agents\/([^/]+)\/(overview|knowledge|mail|chats|activity|tasks)$/.test(p)) {
      const parts = p.split('/');
      openAgentPage(decodeURIComponent(parts[2]), { tab: parts[3], noHistory: true });
    }
    else if (/^\/agents\/[^/]+$/.test(p)) {
      openAgentPage(decodeURIComponent(p.split('/')[2]), { noHistory: true });
    }
    else if (/^\/chats\/[^/]+$/.test(p)) {
      setActive(decodeURIComponent(p.split('/')[2]));
    }
    else { setView('chats'); }
  }
  window.addEventListener('popstate', route);
  // Defer the initial route so it runs AFTER all module-level `let` state
  // (_apSlug, _apMail, _apActivityLoaded, ...) is initialized; those are declared
  // below this IIFE, so calling openAgentPage synchronously here hits the TDZ.
  setTimeout(route, 0);
})();


/* ---- agent drawer: minimal profile (role + instruction) + channels (read + bind) ---- */
const scrim = $('#scrim'), drawer = $('#drawer');
function closeDrawer() { if (drawer) drawer.classList.remove('is-open'); if (scrim) scrim.classList.remove('is-open'); }
{ const dc = $('#drClose'); if (dc) dc.addEventListener('click', closeDrawer); }
if (scrim) scrim.addEventListener('click', closeDrawer);

async function openDrawer(slug) {
  if (!slug || !drawer) return; // drawer removed -- agent settings live on the page now
  drawer.classList.add('is-open'); scrim.classList.add('is-open');
  $('#drName').textContent = '...'; $('#drRole').textContent = ''; $('#drChannels').innerHTML = '';
  try {
    const a = await (await fetch(`/api/agents/${encodeURIComponent(slug)}`, { headers: { Accept: 'application/json' } })).json();
    $('#drName').textContent = a.title || a.slug;
    $('#drRole').textContent = a.role || '';
    $('#drAv').innerHTML = avInner(avResolve(a.avatar), (a.title || a.slug || '?').trim().charAt(0).toUpperCase());
    $('#drBody').value = a.specialist_body || a.body_md || '';
    drawer.dataset.slug = slug;
    // Click the avatar to change it (library pick / upload / remove).
    const drAvEl = $('#drAv');
    if (drAvEl) { drAvEl.classList.add('is-editable'); drAvEl.title = 'Change avatar'; drAvEl.onclick = (e) => { e.stopPropagation(); openAvatarPicker(drAvEl, slug); }; }
  } catch (e) { $('#drName').textContent = slug; }
  loadPolicies(slug);
  try {
    const data = await (await fetch(`/api/agents/${encodeURIComponent(slug)}/channels`, { headers: { Accept: 'application/json' } })).json();
    const bySlug = Object.fromEntries((data.channels || []).map((c) => [c.channel_slug, c]));
    const CONTACTS = [
      { slug: 'email', label: 'Email', icon: 'mail' },
      { slug: 'telegram', label: 'Telegram', icon: 'send' },
      { slug: 'voice', label: 'Voice', icon: 'phone' },
    ];
    $('#drChannels').innerHTML = CONTACTS.map((ch) => {
      const c = bySlug[ch.slug] || {};
      const ref = (c.instance_ref || '').trim();
      if (ch.slug === 'voice') {
        // Voice needs no address -- a plain on/off that controls whether this agent
        // is callable (i.e. shows up in the phone). Real toggle, wired below.
        const on = !!c.enabled;
        return `
        <div class="ctc" data-channel="voice">
          <span class="ctc__ic"><i data-lucide="${ch.icon}"></i></span>
          <span class="ctc__lab">${ch.label}</span>
          <span class="ctc__val is-empty">${on ? 'callable in the phone' : 'off'}</span>
          <button class="ntc__sw ${on ? 'is-on' : ''}" type="button" role="switch" aria-checked="${on ? 'true' : 'false'}" id="drVoiceSw" title="${on ? 'Voice on' : 'Voice off'}"><span class="ntc__knob"></span></button>
        </div>`;
      }
      const on = !!c.enabled && !!ref;
      return `
        <div class="ctc">
          <span class="ctc__ic"><i data-lucide="${ch.icon}"></i></span>
          <span class="ctc__lab">${ch.label}</span>
          <span class="ctc__val ${ref ? '' : 'is-empty'}">${ref ? esc(ref) : 'not set'}</span>
          <span class="ctc__st ${on ? '' : 'off'}">${on ? 'on' : 'off'}</span>
        </div>`;
    }).join('');
    if (window.lucide) lucide.createIcons();
    // Voice on/off: no address, just callable-or-not. PUTs the channel enable flag;
    // reverts the switch if the write fails.
    const vsw = $('#drVoiceSw');
    if (vsw) vsw.addEventListener('click', async () => {
      const on = !vsw.classList.contains('is-on');
      vsw.classList.toggle('is-on', on); vsw.setAttribute('aria-checked', on ? 'true' : 'false'); vsw.title = on ? 'Voice on' : 'Voice off';
      const val = vsw.closest('.ctc').querySelector('.ctc__val');
      if (val) val.textContent = on ? 'callable in the phone' : 'off';
      try {
        const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/channels/voice`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ enabled: on }),
        });
        if (!r.ok) throw new Error('http ' + r.status);
      } catch (e) {
        vsw.classList.toggle('is-on', !on); vsw.setAttribute('aria-checked', !on ? 'true' : 'false');
        if (val) val.textContent = !on ? 'callable in the phone' : 'off';
        console.error('[studio] voice toggle failed', e);
      }
    });
  } catch (e) { $('#drChannels').innerHTML = '<div class="sb__empty">Could not load channels.</div>'; }
}

/* drawer tabs */
$$('.dr__tab').forEach((t) => t.addEventListener('click', () => {
  $$('.dr__tab').forEach((x) => x.classList.toggle('is-on', x === t));
  $$('.dr__pane').forEach((p) => p.classList.toggle('is-on', p.dataset.p === t.dataset.p));
}));

async function setPolicy(slug, policy, action) {
  try {
    await fetch(`/api/agents/${encodeURIComponent(slug)}/policies/${encodeURIComponent(policy)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ action }),
    });
  } catch (e) { /* ignore */ }
}

async function loadPolicies(slug) {
  const box = $('#drRules'); if (!box) return;
  box.innerHTML = '';
  try {
    const data = await (await fetch(`/api/agents/${encodeURIComponent(slug)}/policies`, { headers: { Accept: 'application/json' } })).json();
    const list = data.policies || [];
    const applied = list.filter((p) => p.applied);
    const avail = list.filter((p) => !p.applied);
    const chips = applied.map((p) =>
      `<span class="rchip is-on" title="${esc(p.body_md || '')}">${esc(p.title)}<button class="rchip__x" data-act="clear" data-policy="${esc(p.slug)}" title="Remove">&times;</button></span>`).join('');
    const pick = avail.length
      ? avail.map((p) => `<button class="rchip rchip--pick" data-act="include" data-policy="${esc(p.slug)}" title="${esc(p.body_md || '')}">${esc(p.title)}</button>`).join('')
      : '<span class="sb__empty">All policies applied.</span>';
    box.innerHTML = `<div class="rule-row">${chips}<button class="rchip rchip--add" id="rAddBtn" type="button">+ policy</button></div><div class="rule-pick" id="rulePick" hidden>${pick}</div>`;
    const addB = $('#rAddBtn'), pickBox = $('#rulePick');
    if (addB) addB.addEventListener('click', () => { pickBox.hidden = !pickBox.hidden; });
    box.querySelectorAll('[data-act]').forEach((el) => el.addEventListener('click', async (e) => {
      e.stopPropagation();
      await setPolicy(slug, el.dataset.policy, el.dataset.act);
      loadPolicies(slug);
    }));
  } catch (e) { box.innerHTML = '<div class="sb__empty">Could not load policies.</div>'; }
}

const drSaveBody = $('#drSaveBody');
if (drSaveBody) drSaveBody.addEventListener('click', async () => {
  const slug = drawer.dataset.slug; if (!slug) return;
  const hint = $('#drBodyHint');
  drSaveBody.disabled = true; drSaveBody.textContent = 'Saving...';
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ specialist_body: $('#drBody').value }),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    drSaveBody.textContent = 'Saved'; drSaveBody.classList.add('saved'); if (hint) hint.textContent = 'system prompt rebuilt';
    setTimeout(() => { drSaveBody.textContent = 'Save'; drSaveBody.classList.remove('saved'); drSaveBody.disabled = false; if (hint) hint.textContent = ''; }, 1800);
  } catch (e) { drSaveBody.textContent = 'Error'; drSaveBody.disabled = false; }
});

$('#agents').addEventListener('click', (e) => {
  const el = e.target.closest('.agent');
  if (el) openAgentPage(el.dataset.slug);
});

/* ---- Agent page (workspace launcher): header + Mail|Chats|Activity.
   Chats reuse the desks list + the existing desk canvas (setActive). No new chat component. ---- */
let _apSlug = null;
function apSetTab(name, noHistory) {
  $$('#apBar [data-ap]').forEach((b) => {
    const on = b.dataset.ap === name;
    b.classList.toggle('is-active', on); b.classList.toggle('sn-edge', on); b.classList.toggle('sn-lit', on);
  });
  $$('#agentView .appane').forEach((p) => p.classList.toggle('is-on', p.dataset.ap === name));
  if (name === 'activity' && _apSlug && !_apActivityLoaded) { _apActivityLoaded = true; loadAgentActivity(_apSlug); }
  if (name === 'channels' && _apSlug) renderChannels(_apSlug);
  if (!noHistory && _apSlug) history.pushState({}, '', '/agents/' + encodeURIComponent(_apSlug) + '/' + name);
}
function _chatClean(s) {
  return String(s || '').replace(/^\[MEMORY CONTEXT\][\s\S]*?\n\n/, '').replace(/[*_`#>[\]]/g, '').replace(/\s+/g, ' ').trim();
}
function renderAgentChats(slug) {
  const list = $('#apChatList'); if (!list) return;
  const mine = desks.filter((d) => deskAgent(d) === slug)
    .slice().sort((a, b) => String(b.last_message_at || b.updated_at || '').localeCompare(String(a.last_message_at || a.updated_at || '')));
  { const n = $('#apChatsN'); if (n) n.textContent = mine.length ? '(' + mine.length + ')' : ''; }
  // one compact line per chat: time (left) · title + last message · count (right)
  list.innerHTML = mine.length
    ? mine.map((d) => {
        const when = d.last_message_at || d.updated_at;
        const last = _chatClean(d.last_message);
        const cnt = d.message_count || 0;
        return `<button class="ap__chat" data-open="${d.id}">`
          + `<span class="ap__chat-time">${when ? tkDate(when) : ''}</span>`
          + `<span class="ap__chat-n">${cnt ? cnt + ' msg' : ''}</span>`
          + `${contextBadge(_contextByDesk[d.id]) || '<span class="ctxbadge-slot"></span>'}`
          + `<span class="ap__chat-t">${esc(d.title || 'Chat')}</span>`
          + `<span class="ap__chat-last">${last ? esc(last) : ''}</span></button>`;
      }).join('')
    : '<div class="ap__empty">No chats yet. Start one with New chat.</div>';
  list.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => {
    openDesk(b.dataset.open, _apSlug ? '/agents/' + encodeURIComponent(_apSlug) + '/chats/' + encodeURIComponent(b.dataset.open) : null);
  }));
  paintDeskWork();
}
function openAgentPage(slug, opts) {
  opts = opts || {};
  if (!slug) return;
  _apSlug = slug;
  _apActivityLoaded = false;
  _apChains = null;
  _apTasks = null;
  _apDocs = null;
  _apPolicies = null;
  closePop();
  const a = agentBySlug(slug) || { slug, title: slug };
  setView('agent');
  $('#apAv').innerHTML = avInner(a.avatar ? avResolve(a.avatar) : avBySlug(slug), (a.title || a.slug || '?').trim().charAt(0).toUpperCase());
  $('#apName').textContent = a.title || a.slug;
  const apNm = (a.title || a.slug || '').toLowerCase();
  const apRl = a.role || a.description || '';
  $('#apRole').textContent = (apRl && apRl.toLowerCase() !== apNm) ? apRl : '';
  renderAgentChats(slug);
  loadAgentMail(slug, opts.counterpart);
  loadAgentActivity(slug);
  loadAgentTasks(slug);
  loadAgentDocuments(slug);
  loadAgentPolicies(slug);
  renderOverview();
  const _tab = opts.tab || 'overview';
  apSetTab(_tab, true);
  if (!opts.noHistory) history.pushState({}, '', '/agents/' + encodeURIComponent(slug) + '/' + _tab);
  if (opts.deskId) setActive(opts.deskId);
  if (window.lucide) lucide.createIcons();
}
let _apMail = [];
let _apActivityLoaded = false;
let _apKnow = null;   // this agent's knowledge items (null = not loaded)
let _apMem = null;    // this agent's memory docs (null = not loaded)
let _knKinds = null;  // cached source kinds + availability
const apDisp = (s) => { const a = agentBySlug(s); return (a && a.title) || s || '?'; };
async function loadAgentMail(slug, autoCounterpart) {
  const box = $('#apMail'); if (!box) return;
  box.innerHTML = skRows(5, true);
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/mail`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    _apMail = (await r.json()).threads || [];
    { const n = $('#apMailN'); if (n) n.textContent = _apMail.length ? '(' + _apMail.length + ')' : ''; }
    renderOverview();
    if (!_apMail.length) { box.innerHTML = '<div class="ap__empty">No mail yet.</div>'; return; }
    box.innerHTML = '<div class="ap__list">' + _apMail.map((t, i) => {
      const inbound = t.last_from === t.counterpart;
      const subj = _chatClean((t.snippet || '').split('\n')[0]) || '(no subject)';
      return `<button class="ap__chat" data-idx="${i}">`
        + `<span class="ap__chat-time">${t.last_at ? tkDate(t.last_at) : ''}</span>`
        + `<span class="ap__chat-n">${t.count ? t.count + ' msg' : ''}</span>`
        + `<span class="ap__chat-t"><span class="ap__maildir" title="${inbound ? 'from' : 'to'}">${inbound ? '↓' : '↑'}</span>${esc(apDisp(t.counterpart))}</span>`
        + `<span class="ap__chat-last">${esc(subj)}</span></button>`;
    }).join('') + '</div>';
    box.querySelectorAll('[data-idx]').forEach((b) => b.addEventListener('click', () => apShowThread(slug, Number(b.dataset.idx))));
    if (autoCounterpart) { const _ai = _apMail.findIndex((t) => String(t.last_id) === String(autoCounterpart)); if (_ai >= 0) apShowThread(slug, _ai); }
  } catch (e) { box.innerHTML = '<div class="ap__empty">Could not load mail.</div>'; }
}
function apShowThread(slug, idx, backTo) {
  const box = $('#apMail'); const t = _apMail[idx]; if (!box || !t) return;
  history.pushState({}, '', '/agents/' + encodeURIComponent(slug) + '/mail/' + encodeURIComponent(t.last_id));
  const tsubj = (((t.messages && t.messages[0] && t.messages[0].content) || '').split('\n')[0] || '(no subject)').slice(0, 140);
  box.innerHTML = `<button class="ap__back" id="apMailBack"><i data-lucide="arrow-left"></i><span>${esc(apDisp(t.counterpart))}</span></button><div class="ap__subj">${esc(tsubj)}</div><div class="ap__thread">` +
    (t.messages || []).slice().reverse().map((m) => { const out = m.sender === slug; return `<div class="ap__turn ${out ? 'is-out' : 'is-in'}"><div class="ap__bubble"><div class="ap__turn-h"><span class="ap__who">${esc(apDisp(m.sender))}</span><span class="ap__mailtime">${tkDate(m.received_at)}</span></div><div class="ap__turn-b">${esc(m.content || '')}</div></div></div>`; }).join('') +
    `</div>`;
  const bk = $('#apMailBack'); if (bk) bk.addEventListener('click', () => {
    if (backTo === 'activity') { history.pushState({}, '', '/agents/' + encodeURIComponent(slug) + '/activity'); apSetTab('activity', true); return; }
    history.pushState({}, '', '/agents/' + encodeURIComponent(slug) + '/mail');
    loadAgentMail(slug);
  });
  if (window.lucide) lucide.createIcons();
}
/* ---- Activity (Flow): message events involving this agent, grouped into
   work chains. Heuristic grouping (no parent_event_id yet): a message.inbound
   trigger or a >6min gap starts a new chain. One collapsible row per chain. ---- */
async function loadAgentActivity(slug) {
  loadAgentTimeline(slug);
  const box = $('#apActivity'); if (!box) return;
  _apActivityLoaded = true;
  box.innerHTML = skRows(5, true);
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/activity`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const events = (await r.json()).events || [];
    const apN = $('#apActivityN');
    if (!events.length) { box.innerHTML = '<div class="ap__empty">No activity yet.</div>'; if (apN) apN.textContent = ''; _apChains = []; renderOverview(); return; }
    events.sort((a, b) => (a.id || 0) - (b.id || 0));
    const GAP = 6 * 60 * 1000;
    const chains = []; let cur = null, prevTs = 0;
    for (const e of events) {
      const t = Date.parse(e.ts) || 0;
      if (!cur || e.type === 'message.inbound' || (t - prevTs) > GAP) { cur = []; chains.push(cur); }
      cur.push(e); prevTs = t;
    }
    chains.reverse();
    _apChains = chains;
    renderOverview();
    if (apN) apN.textContent = chains.length ? '(' + chains.length + ')' : '';
    box.innerHTML = '<div class="ap__flow">' + chains.map((ch, i) => apChainRow(ch, i, slug)).join('') + '</div>';
    box.querySelectorAll('[data-chain]').forEach((row) => row.addEventListener('click', () => row.classList.toggle('is-open')));
    box.querySelectorAll('.ap__cd-open').forEach((el) => el.addEventListener('click', (ev) => { ev.stopPropagation(); apOpenMailFor(slug, el.dataset.cp); }));
    if (window.lucide) lucide.createIcons();
  } catch (e) { box.innerHTML = '<div class="ap__empty">Could not load activity.</div>'; }
}
function apNode(slug) {
  const d = apDisp(slug); const init = (d || '?').trim().charAt(0).toUpperCase();
  return `<span class="ap__node" title="${esc(d)}"><span class="sn-av ap__nav">${avInner(avBySlug(slug), init)}</span><span class="ap__nlabel">${esc(d)}</span></span>`;
}
function apChainRow(ch, i, slug) {
  const path = [];
  if (ch.length) path.push(ch[0].from);
  for (const e of ch) { if (e.to && path[path.length - 1] !== e.to) path.push(e.to); }
  const flow = path.map(apNode).join('<i class="ap__arrow" data-lucide="chevron-right"></i>');
  const last = ch[ch.length - 1]; const first = ch[0];
  const subj = ((first.preview || '').split('\n')[0] || '(message)').slice(0, 80);
  const detail = ch.map((e) => { const cp = e.from === slug ? e.to : e.from; return `<div class="ap__chain-detail ap__cd-open" data-cp="${esc(cp)}" title="Open mail thread"><div class="ap__cd-h"><span>${esc(apDisp(e.from))}</span><i data-lucide="arrow-right"></i><span>${esc(apDisp(e.to))}</span><span class="ap__mailtime">${tkDate(e.ts)}</span><i class="ap__cd-go" data-lucide="external-link"></i></div><div class="ap__cd-b">${esc((e.preview || '').slice(0, 200))}</div></div>`; }).join('');
  return `<div class="ap__chain" data-chain="${i}"><div class="ap__chain-top"><div class="ap__cmeta"><span class="ap__cdate">${tkDate(last.ts)}</span><span class="ap__count">${ch.length}</span></div><div class="ap__chain-flow">${flow}</div></div><div class="ap__chain-subj">${esc(subj)}</div><div class="ap__chain-body">${detail}</div></div>`;
}
function apOpenMailFor(slug, cp) {
  if (!cp) return;
  apSetTab('mail');
  const idx = _apMail.findIndex((t) => t.counterpart === cp);
  if (idx >= 0) apShowThread(slug, idx, 'activity');
}
$$('#apBar [data-ap]').forEach((b) => b.addEventListener('click', () => apSetTab(b.dataset.ap)));
(function () {
  const add = $('#apAdd');
  if (add) add.addEventListener('click', () => {
    if (!_apSlug) return;
    const pop = showPop(add, `
      <button class="pop__it" data-mk="chat"><i data-lucide="message-square"></i> New chat</button>
      <button class="pop__it" data-mk="mail"><i data-lucide="mail"></i> New mail</button>`);
    pop.querySelector('[data-mk="chat"]').addEventListener('click', () => { closePop(); createDeskFor(_apSlug, 'New chat'); });
    pop.querySelector('[data-mk="mail"]').addEventListener('click', () => { closePop(); apSetTab('mail'); });
  });
  const st = $('#apSettings'); if (st) st.addEventListener('click', () => { if (_apSlug) openDrawer(_apSlug); });
})();

/* ---- Stale-tab guard: if the server reports a newer build than the one
   this tab loaded, soft-reload (when idle) or offer a reload banner. Avoids
   running old in-memory JS after a redeploy. ---- */
(function versionWatch() {
  const LOADED = String(window.__BUILD__ || '');
  let banner = null;
  function showBanner() {
    if (banner) return;
    banner = document.createElement('div');
    banner.className = 'sn-update';
    banner.innerHTML = '<span>Interface updated.</span><button class="sn-update__btn">Reload</button>';
    banner.querySelector('button').addEventListener('click', () => location.reload());
    document.body.appendChild(banner);
  }
  async function check(auto) {
    try {
      const r = await fetch('/api/version', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (!r.ok) return;
      const b = String((await r.json()).build || '');
      if (!b || !LOADED || b === LOADED) return;
      const typing = document.activeElement && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
      if (auto && !anyTurns() && !typing) location.reload();
      else showBanner();
    } catch (e) { /* offline / transient: ignore */ }
  }
  // no poll: a new build restarts the node's web service, the live stream drops
  // and opens again, and that is when to ask
  document.addEventListener('sn:resync', () => check(false));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(true); });
})();

/* ---- Agent Overview (summary landing) + Tasks (planner tasks for this agent) ---- */
let _apChains = null;
let _apTasks = null;

// A 5-field cron rendered for humans ("daily 8:30am"); falls back to raw cron.
function cronHuman(c) {
  if (!c) return '';
  const p = String(c).trim().split(/\s+/);
  if (p.length !== 5) return c;
  const [mi, ho, dom, mon, dow] = p;
  const clock = (() => {
    const hh = parseInt(ho, 10), mm = parseInt(mi, 10);
    if (isNaN(hh) || isNaN(mm) || !/^\d+$/.test(ho) || !/^\d+$/.test(mi)) return null;
    const ap = hh < 12 ? 'am' : 'pm'; let h12 = hh % 12; if (h12 === 0) h12 = 12;
    return `${h12}:${String(mm).padStart(2, '0')}${ap}`;
  })();
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  if (mi.startsWith('*/') && ho === '*') return `every ${mi.slice(2)} min`;
  if (mi === '*' && ho === '*') return 'every minute';
  if (/^\d+$/.test(mi) && ho.startsWith('*/')) return `every ${ho.slice(2)}h`;
  if (/^\d+$/.test(mi) && ho === '*') return `hourly at :${String(parseInt(mi, 10)).padStart(2, '0')}`;
  if (clock) {
    if (dom === '*' && mon === '*' && dow === '*') return `daily ${clock}`;
    if (dom === '*' && mon === '*' && dow === '1-5') return `weekdays ${clock}`;
    if (dom === '*' && mon === '*' && /^[0-7](,[0-7])*$/.test(dow)) return `${dow.split(',').map((d) => days[+d]).join(', ')} ${clock}`;
    if (/^\d+$/.test(dom) && mon === '*' && dow === '*') return `monthly day ${dom}, ${clock}`;
  }
  return c;
}
async function setAgentEnabled(slug, enabled) {
  const a = agentBySlug(slug); if (!a) return;
  const prev = a.enabled;
  a.enabled = enabled; renderOverview(); // optimistic
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!r.ok) throw new Error('http ' + r.status);
  } catch (e) { a.enabled = prev; renderOverview(); console.error('[studio] toggle enabled failed', e); }
}
// One activity event -> a human line ("Replied · ...").
/* ---- Agent page timeline: what the agent did, in words ---- */
let _apTimeline = null;
async function loadAgentTimeline(slug) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/timeline`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    if (slug !== _apSlug) return;
    _apTimeline = Array.isArray(d.events) ? d.events : [];
  } catch (_) { if (slug !== _apSlug) return; _apTimeline = null; }
  renderOverview();
}
document.addEventListener('sn:event', (e) => { const ev = e.detail || {};
  if (_apSlug && $('.ovd') && String(ev.type).startsWith('agent.') && (!ev.agent || ev.agent === _apSlug)) soon('aptl', () => loadAgentTimeline(_apSlug)); });
function tlTaskId(path) { const m = String(path || '').match(/tasks\/(\d+)/); return m ? m[1] : null; }
function tlLabel(e) {
  const t = e.type || '';
  const task = tlTaskId(e.path);
  const on = task ? ' #' + task : '';
  if (t === 'tool.invoked' || t === 'tool.failed') {
    const OPS = {
      'planner.comment': 'Commented on task' + on, 'planner.comments': 'Read the comments on task' + on,
      'planner.get': 'Opened task' + on, 'planner.update': 'Updated task' + on, 'planner.create': 'Created a task',
      'planner.list': 'Looked at the task list', 'planner.search': 'Searched the tasks',
    };
    const what = OPS[e.op] || ('Used ' + (e.op || 'a tool') + on);
    return t === 'tool.failed' ? 'Failed: ' + what + (e.error ? ' (' + e.error + ')' : '') : what;
  }
  if (t === 'agent.woken') return 'Woken by the runner';
  if (t === 'agent.wake_requested') return 'Wake requested';
  if (t === 'agent.wake_failed') return 'Wake failed';
  if (t === 'agent.turn_started') return e.via === 'desk' ? 'Started answering in a chat' : e.via === 'fast-dispatch' ? 'Started answering a message' : 'Started a turn';
  if (t === 'agent.turn_completed') return 'Finished the turn';
  if (t === 'agent.turn_failed') return 'The turn failed';
  if (t === 'agent.turn_settled') return 'Settled the turn';
  if (t === 'message.inbound') return 'Received' + (e.from ? ' from ' + e.from : '') + (e.preview ? ' · ' + e.preview : '');
  if (t === 'message.sent' || t === 'message.reply') return 'Sent' + (e.to ? ' to ' + e.to : '') + (e.preview ? ' · ' + e.preview : '');
  return t.replace(/[._]/g, ' ');
}
// Repeats in a row read as one line with a count: an agent polling the task list five
// times is one thing it did, not five.
// Task numbers in a timeline line open the task in a new browser tab, so reading an
// agent's page is not lost by following one of its tasks. The text is
// escaped first and only the "#<digits>" it produced is turned into a link.
function tlLinkTasks(text) {
  return esc(text).replace(/#(\d+)\b/g, '<a class="ovd__tk" href="/tasks/$1" target="_blank" rel="noopener">#$1</a>');
}
function tlCollapse(list) {
  const out = [];
  for (const e of list) {
    const label = tlLabel(e);
    const prev = out[out.length - 1];
    if (prev && prev.label === label) { prev.n += 1; continue; }
    out.push({ ts: e.ts, label, n: 1 });
  }
  return out;
}

function apEvtLabel(e) {
  const p = String(e.preview || '').replace(/\s+/g, ' ').trim().slice(0, 70);
  const t = String(e.type || '');
  let verb = t || 'event';
  if (t === 'message.reply') verb = 'Replied';
  else if (t === 'message.inbound') verb = 'Received';
  else if (t === 'message.outbound') verb = 'Sent';
  else if (t.indexOf('task') === 0) verb = t.replace(/[._]/g, ' ');
  return verb + (p ? ' · ' + p : '');
}
// Overview = "what's happening with this agent right now": header + current
// mission + health + activity timeline + workload + recent outcomes. All from
// data we already load (tasks / activity / mail / desks / agent record).
// Agent page -> Channels tab. The agent's channels with a real Voice on/off toggle
// (voice needs no address -- it just controls whether the agent is callable in the
// phone). Email/telegram stay read-only here. Replaces the old settings drawer.
async function renderChannels(slug) {
  const box = $('#apChannels'); if (!box || !slug) return;
  box.innerHTML = skRows(4, false);
  try {
    const data = await (await fetch(`/api/agents/${encodeURIComponent(slug)}/channels`, { headers: { Accept: 'application/json' } })).json();
    const bySlug = Object.fromEntries((data.channels || []).map((c) => [c.channel_slug, c]));
    const CONTACTS = [
      { slug: 'email', label: 'Email', icon: 'mail' },
      { slug: 'telegram', label: 'Telegram', icon: 'send' },
      { slug: 'voice', label: 'Voice', icon: 'phone' },
    ];
    box.innerHTML = `<div class="apch"><p class="apch__lbl">Channels</p>` + CONTACTS.map((ch) => {
      const c = bySlug[ch.slug] || {};
      const ref = (c.instance_ref || '').trim();
      if (ch.slug === 'voice') {
        const on = !!c.enabled;
        return `
        <div class="ctc" data-channel="voice">
          <span class="ctc__ic"><i data-lucide="${ch.icon}"></i></span>
          <span class="ctc__lab">${ch.label}</span>
          <span class="ctc__val is-empty">${on ? 'callable in the phone' : 'off'}</span>
          <button class="ntc__sw ${on ? 'is-on' : ''}" type="button" role="switch" aria-checked="${on ? 'true' : 'false'}" id="apVoiceSw" title="${on ? 'Voice on' : 'Voice off'}"><span class="ntc__knob"></span></button>
        </div>`;
      }
      const on = !!c.enabled && !!ref;
      return `
        <div class="ctc">
          <span class="ctc__ic"><i data-lucide="${ch.icon}"></i></span>
          <span class="ctc__lab">${ch.label}</span>
          <span class="ctc__val ${ref ? '' : 'is-empty'}">${ref ? esc(ref) : 'not set'}</span>
          <span class="ctc__st ${on ? '' : 'off'}">${on ? 'on' : 'off'}</span>
        </div>`;
    }).join('') + `</div>`;
    if (window.lucide) lucide.createIcons();
    const vsw = $('#apVoiceSw');
    if (vsw) vsw.addEventListener('click', async () => {
      const on = !vsw.classList.contains('is-on');
      vsw.classList.toggle('is-on', on); vsw.setAttribute('aria-checked', on ? 'true' : 'false'); vsw.title = on ? 'Voice on' : 'Voice off';
      const val = vsw.closest('.ctc').querySelector('.ctc__val');
      if (val) val.textContent = on ? 'callable in the phone' : 'off';
      try {
        const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/channels/voice`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ enabled: on }),
        });
        if (!r.ok) throw new Error('http ' + r.status);
      } catch (e) {
        vsw.classList.toggle('is-on', !on); vsw.setAttribute('aria-checked', !on ? 'true' : 'false');
        if (val) val.textContent = !on ? 'callable in the phone' : 'off';
        console.error('[studio] voice toggle failed', e);
      }
    });
  } catch (e) { box.innerHTML = '<div class="ap__empty">Could not load channels.</div>'; }
}

function renderOverview() {
  const box = $('#apOverview'); if (!box || !_apSlug) return;
  const slug = _apSlug;
  const a = agentBySlug(slug);
  // Agent roster not loaded yet -> show a placeholder, not the bare slug. Re-runs
  // from loadAgents once the record is available (no "alex" -> "Alex" flash).
  if (!a) { box.innerHTML = skRows(3, false); return; }
  const tasks = Array.isArray(_apTasks) ? _apTasks : [];
  const mOrder = ['awaiting_approval', 'pending_review', 'in_progress', 'assessing', 'open'];
  const mission = tasks.filter((t) => mOrder.includes(tkStage(t))).sort((x, y) => mOrder.indexOf(tkStage(x)) - mOrder.indexOf(tkStage(y)))[0];
  const events = Array.isArray(_apChains) ? _apChains.flat() : [];
  const own = Array.isArray(_apTimeline) ? _apTimeline : null;
  const lastSrc = own && own.length ? own : events;
  const lastTs = lastSrc.length ? Math.max.apply(null, lastSrc.map((e) => Date.parse(e.ts) || 0)) : 0;
  const queue = tasks.filter((t) => ['open', 'in_progress', 'assessing'].includes(tkStage(t))).length;
  const total = tasks.length;
  const pending = tasks.filter((t) => tkStage(t) === 'awaiting_approval' || tkStage(t) === 'pending_review').length;
  const chats = desks.filter((d) => deskAgent(d) === slug).length;
  const mail = Array.isArray(_apMail) ? _apMail.length : 0;
  const done = tasks.filter((t) => tkStage(t) === 'done').sort((x, y) => String(y.completed_at || y.updated_at || '').localeCompare(String(x.completed_at || x.updated_at || ''))).slice(0, 4);
  const tl = own ? tlCollapse(own).slice(0, 12)
    : events.slice().sort((x, y) => (Date.parse(y.ts) || 0) - (Date.parse(x.ts) || 0)).slice(0, 6).map((e) => ({ ts: e.ts, label: apEvtLabel(e), n: 1 }));
  const kv = (k, v) => `<div class="ovd__kv"><span>${k}</span><b>${v}</b></div>`;
  const sc = (k, v) => `<div class="ovd__sc"><div class="ovd__sclbl">${k}</div><div class="ovd__scval">${v}</div></div>`;
  // "waiting for" derived from the current mission
  let waiting = '<span class="ovd__mut">nothing</span>';
  if (mission) {
    const ws = { awaiting_approval: 'Your approval', pending_review: 'Review', blocked: 'Unblock', assessing: 'Assessment', in_progress: 'In progress', open: 'Pickup' };
    waiting = esc(mission.blocked_reason ? mission.blocked_reason.replace(/_/g, ' ') : (ws[tkStage(mission)] || tkStage(mission)));
  }
  // memory counts from the already-loaded mesh documents
  const mdocs = (_apDocs && Array.isArray(_apDocs.docs)) ? _apDocs.docs : null;
  const instrWords = (a.specialist_body || a.body_md || '').trim().split(/\s+/).filter(Boolean).length;
  const rulesN = Array.isArray(_apPolicies) ? _apPolicies.length : null;
  box.innerHTML = `
    <div class="ovd">
      <section class="ovd__hero" data-state-slug="${esc(slug)}">
        <button class="agtog ovd__herotog ${a.enabled === false ? 'is-off' : 'is-on'}" id="agToggle" title="Click to ${a.enabled === false ? 'activate' : 'pause'}"><span class="agtog__dot"></span>${a.enabled === false ? 'Paused' : 'Active'}</button>
        <div class="ovd__hrow"><span class="ovd__hav">${avInner(a.avatar ? avResolve(a.avatar) : avBySlug(slug), (a.title || slug || '?').trim().charAt(0).toUpperCase())}</span><div class="ovd__hidn"><h1 class="ovd__hname">${esc(a.title || slug)}</h1>${(a.role || a.description) ? `<div class="ovd__hrole">${esc(a.role || a.description)}</div>` : ''}</div></div>
      </section>
      <div class="ovd__row ovd__row--3">
        <section class="ovd__card">
          <div class="ovd__h">Status</div>
          <div class="ovd__kvs">
            ${kv('Waiting for', waiting)}
            ${kv('Last action', lastTs ? relTime(lastTs) : '<span class="ovd__mut">never</span>')}
            ${kv('Queue', queue + ' task' + (queue === 1 ? '' : 's'))}
          </div>
        </section>
        <section class="ovd__card">
          <div class="ovd__h">Workload</div>
          <div class="ovd__kvs">
            ${kv('Tasks', total)}
            ${kv('Pending approval', pending)}
            ${kv('Chats', chats)}
            ${kv('Mail', mail)}
          </div>
        </section>
        <section class="ovd__card">
          <div class="ovd__h">Knowledge</div>
          <div class="ovd__kvs">
            ${kv('Instructions', instrWords ? instrWords + ' words' : '<span class="ovd__mut">none</span>')}
            ${kv('Rules', rulesN == null ? '<span class="ovd__mut">&mdash;</span>' : rulesN)}
            ${kv('Documents', mdocs == null ? '<span class="ovd__mut">&mdash;</span>' : mdocs.length)}
          </div>
        </section>
      </div>
      <section class="ovd__card con" id="apConsole"></section>
      <div class="ovd__row ovd__row--2">
        <section class="ovd__card">
          <div class="ovd__h">Live timeline</div>
          <div class="ovd__tl ovd__tl--now" data-now-row="${esc(slug)}" hidden><span class="ovd__tlt" data-now-time></span><span class="ovd__tld" data-now-slug="${esc(slug)}"></span></div>
          ${tl.length
            ? tl.map((e) => `<div class="ovd__tl"><span class="ovd__tlt">${tkDate(e.ts)}</span><span class="ovd__tld" title="${esc(e.label)}">${tlLinkTasks(e.label)}${e.n > 1 ? ' <span class="ovd__mut">\u00d7' + e.n + '</span>' : ''}</span></div>`).join('')
            : '<div class="ovd__mut">No activity yet.</div>'}
        </section>
        <section class="ovd__card">
          <div class="ovd__h">Recent results</div>
          ${done.length
            ? done.map((t) => `<button class="ovd__out" data-id="${t.id}">✓ ${esc(t.title || '')}</button>`).join('')
            : '<div class="ovd__mut">Nothing completed yet.</div>'}
        </section>
      </div>
    </div>`;
  paintAgentState();
  { const tg = $('#agToggle'); if (tg) tg.addEventListener('click', () => setAgentEnabled(slug, a.enabled === false)); }
  { const hav = box.querySelector('.ovd__hav'); if (hav) { hav.style.cursor = 'pointer'; hav.title = 'Change avatar'; hav.onclick = (e) => { e.stopPropagation(); openAvatarPicker(hav, slug); }; } }
  box.querySelectorAll('[data-id]').forEach((b) => b.addEventListener('click', () => openTask(b.dataset.id)));
  consoleMount(slug);
}

/* ---- Agent card: Console ----
   "working now" said THAT the agent works, never WHAT: a turn the runner started
   (a task, a message) had no viewer at all, only desk chats did. The gateway
   answers per agent for every turn in flight, whatever started it, with the
   steps it already keeps for the chat (tool + what it touches, what the agent
   says; never tool results), and the turn finished last. renderOverview redraws
   the whole card, so the state lives here and the box is repainted from it. */
let _conSlug = null, _conData = null, _conTimer = null;
let _conOpen = (() => { try { return localStorage.getItem('sn-console-open') === '1'; } catch (_) { return false; } })();
const _conScroll = {};   // session_id -> { top, atBottom }: a poll must not yank the reader

function consoleMount(slug) {
  if (_conSlug !== slug) { _conSlug = slug; _conData = null; }
  paintConsole();
  consoleSchedule(0);
}
function consoleSchedule(ms) {
  clearTimeout(_conTimer);
  if (!_conOpen) return;
  _conTimer = setTimeout(consolePoll, ms);
}
async function consolePoll() {
  const slug = _conSlug;
  const el = $('#apConsole');
  if (!el || slug !== _apSlug) return;               // left the agent: stop
  if (document.hidden || el.offsetParent === null) { consoleSchedule(4000); return; }  // tab away: idle
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/console`, { headers: { Accept: 'application/json' } });
    const d = await r.json();
    if (slug !== _conSlug) return;
    _conData = d;
  } catch (_) { /* keep what is shown; the next poll tries again */ }
  paintConsole();
  consoleSchedule(2000);
}
function conDur(s) { s = Math.max(0, s | 0); return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + (s % 60) + 's'; }
function conClock(t) { const d = new Date((t || 0) * 1000); return isNaN(d) ? '' : d.toTimeString().slice(0, 8); }
// The runner's key is runner-<slug>-<newest pending message id>-<n> (brin
// runner.py); id 0 means a raised flag, i.e. a planner task, not a message.
// "agent-b:runner-agent-b-6832-72" -> "runner · message 6832"; "agent-b:desk:<uuid>" -> "chat"
function conLabel(conv) {
  const c = String(conv || '').replace(/^[^:]+:/, '');
  const m = c.match(/^runner-.+?-(\d+)-\d+$/);
  if (m) return m[1] === '0' ? 'runner · task' : 'runner · message ' + m[1];
  if (/^desk:/.test(c)) return 'chat';
  return c || 'turn';
}
function conSteps(sid, steps) {
  if (!steps || !steps.length) return '<div class="con__none">no steps yet: thinking</div>';
  return `<div class="con__steps" data-sid="${esc(sid)}">` + steps.map((s) =>
    `<div class="con__i con__i--${s.kind === 'say' ? 'say' : 'tool'}"><span class="con__t">${conClock(s.t)}</span><span class="con__x" title="${esc(s.text)}">${esc(s.text)}</span></div>`
  ).join('') + '</div>';
}
function paintConsole() {
  const el = $('#apConsole'); if (!el) return;
  if (!PANEL_ROLES.includes(meIdentity.role)) { el.hidden = true; el.style.display = 'none'; return; }
  // Remember where each list was scrolled before the repaint.
  el.querySelectorAll('.con__steps').forEach((b) => {
    _conScroll[b.dataset.sid] = { top: b.scrollTop, atBottom: b.scrollTop + b.clientHeight >= b.scrollHeight - 4 };
  });
  const d = _conData;
  const turns = d && Array.isArray(d.turns) ? d.turns : [];
  const last = d && d.last;
  let status;
  if (!_conOpen) status = '';
  else if (!d) status = '<span class="skel skline sk-inline" role="status" aria-label="Loading"></span>';
  else if (turns.length) status = `<span class="con__live">${turns.length === 1 ? 'working' : turns.length + ' turns running'}</span>`;
  else status = '<span class="ovd__mut">idle' + (last && last.ended_at ? ', last turn ' + relTime(last.ended_at * 1000) : '') + '</span>';
  let body = '';
  if (_conOpen && d) {
    if (turns.length) {
      body = turns.map((t) => `
        <div class="con__turn">
          <div class="con__head"><b>${esc(conLabel(t.conversation))}</b><span>${conDur(t.elapsed)}</span>${t.action ? `<span class="con__now">${esc(t.action)}${t.detail ? ': ' + esc(t.detail) : ''}</span>` : '<span class="ovd__mut">thinking</span>'}${t.stopping ? '<span class="ovd__mut">stopping</span>' : ''}</div>
          ${conSteps(t.session_id, t.steps)}
        </div>`).join('');
    } else if (last) {
      body = `
        <div class="con__turn con__turn--last">
          <div class="con__head"><b>Last turn · ${esc(conLabel(last.conversation))}</b><span>took ${conDur(last.elapsed)}</span></div>
          ${conSteps('last:' + last.session_id, last.steps)}
        </div>`;
    } else {
      body = '<div class="ovd__mut">No turn since the gateway started.</div>';
    }
  }
  el.innerHTML = `
    <div class="con__bar">
      <div class="ovd__h con__title">Console</div>
      <div class="con__status">${status}</div>
      <button class="con__tog" type="button" aria-expanded="${_conOpen}">${_conOpen ? 'Hide' : 'Show'}</button>
    </div>
    ${_conOpen ? `<div class="con__body">${body}</div>` : ''}`;
  el.querySelector('.con__tog').onclick = () => {
    _conOpen = !_conOpen;
    try { localStorage.setItem('sn-console-open', _conOpen ? '1' : '0'); } catch (_) {}
    paintConsole();
    consoleSchedule(0);
  };
  // Follow new steps only if the reader was already at the bottom.
  el.querySelectorAll('.con__steps').forEach((b) => {
    const s = _conScroll[b.dataset.sid];
    b.scrollTop = (!s || s.atBottom) ? b.scrollHeight : s.top;
  });
}

/* ---- Agent page: System instruction (editable) + Rules + Documents (mesh) ---- */
let _apDocs = null;     // { ws, docs } -- the agent's mesh workspace
let _apLocalDocs = [];  // { id, slug, name, type } -- files picked locally, NOT yet uploaded (session-only tiles)
let _apLocalSeq = 0;    // local tile id counter
let _apPolicies = null; // the agent's rules (policies)

async function loadAgentDocuments(slug) {
  _apDocs = null;
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents?limit=200`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    if (slug !== _apSlug) return;
    _apDocs = { ws: d.workspace, docs: Array.isArray(d.docs) ? d.docs : [] };
  } catch (_) { if (slug !== _apSlug) return; _apDocs = { ws: null, docs: [], error: true }; }
  const n = $('#apKnowN'); if (n) n.textContent = (_apDocs.docs && _apDocs.docs.length) ? '(' + _apDocs.docs.length + ')' : '';
  renderAgentKnowledge(slug);
}
async function loadAgentPolicies(slug) {
  _apPolicies = null;
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/policies`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    if (slug !== _apSlug) return;
    _apPolicies = Array.isArray(d.policies) ? d.policies : (Array.isArray(d) ? d : []);
  } catch (_) { if (slug !== _apSlug) return; _apPolicies = []; }
  renderAgentKnowledge(slug);
}
function docTitle(p) { return ((String(p || '').split('\n').find((l) => l.trim()) || '') || '(untitled)').slice(0, 120); }
function docType(it) {
  const ext = (String(it.p || '').match(/\.([a-z0-9]{2,4})\b/) || [])[1] || '';
  if (ext === 'pdf') return 'PDF';
  if (['xls', 'xlsx', 'csv'].includes(ext)) return 'XLS';
  if (['doc', 'docx'].includes(ext)) return 'DOC';
  if (['htm', 'html'].includes(ext)) return 'HTML';
  const y = String(it.y || '').toLowerCase();
  if (y === 'contact') return 'CARD';
  if (y === 'session-snapshot') return 'LOG';
  return 'TXT';
}
function renderAgentKnowledge(slug) {
  const box = $('#apKnowledge'); if (!box) return;
  const a = agentBySlug(slug) || { slug };
  const prompt = String(a.specialist_body || a.body_md || '').trim();
  const promptHtml = prompt
    ? esc(prompt.slice(0, 400)) + (prompt.length > 400 ? '<span class="tkt__mut"> &hellip;</span>' : '')
    : '<span class="tkt__mut">No instruction set. Click Edit to write one.</span>';
  let rulesHtml;
  if (_apPolicies == null) rulesHtml = skRows(3, false);
  else if (!_apPolicies.length) rulesHtml = '<div class="tkt__mut">No rules.</div>';
  else rulesHtml = '<div class="kp__rules">' + _apPolicies.map((p) =>
    `<span class="kp__rule kp__rule--btn ${p.applied ? 'is-on' : 'is-off'}" role="button" tabindex="0" data-policy="${esc(p.slug)}" data-applied="${p.applied ? 1 : 0}" title="${esc(p.body_md || p.title || p.slug)}\n\n(click to ${p.applied ? 'deactivate' : 'activate'} for this agent)"><span class="kp__rdot"></span>${esc(p.title || p.slug)}</span>`).join('') + '</div>';
  let docsHtml;
  const localDocs = _apLocalDocs.filter((x) => x.slug === slug);
  if (_apDocs == null) docsHtml = skRows(3, false);
  else if (_apDocs.error) docsHtml = '<div class="ap__empty">Could not load documents.</div>';
  else if (!_apDocs.docs.length && !localDocs.length) docsHtml = '<div class="ap__empty">No documents yet. Add something the agent should know.</div>';
  else {
    const serverTiles = _apDocs.docs.map((it) => {
      const tags = it.t || [];
      const nm = esc(docTitle(it.p));
      // Linked Google Doc: a pointer, not a copy. Opens in Drive; source of truth
      // stays there. The X unlinks (removes the pointer; the Google doc is kept).
      const pinned = tags.includes('pin');
      const pinBtn = `<button class="kn__pin${pinned ? ' kn__pin--on' : ''}" data-pin="${esc(it.g)}" data-pinned="${pinned ? 1 : 0}" title="${pinned ? 'Pinned: content is pre-loaded into the prompt (snapshot). Click to unpin.' : 'Pin: pre-load this document into the prompt as a snapshot.'}"><span class="kn__pin__ic"><i data-lucide="pin"></i></span></button>`;
      const syncBtn = pinned ? `<button class="kn__sync" data-sync="${esc(it.g)}" title="Re-sync: pull the latest version of this document into the prompt (a pinned copy is a snapshot, so this refreshes it)."><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg></button>` : '';
      if (tags.includes('source:google-drive')) {
        const link = ((it.p || '').match(/https?:\/\/\S+/) || [''])[0];
        return `<div class="kn__tile kn__tile--gdrive" data-gopen="${esc(link)}" title="Open in Google Drive">
        <span class="kn__ic kn__ic--gdoc">DOC</span>
        <span class="kn__name">${nm}</span>
        <span class="kp__src kp__src--linked" title="linked Google Doc, lives in your Drive">linked</span>
        ${syncBtn}${pinBtn}
        <button class="kn__x" data-ddel="${esc(it.g)}" title="Unlink (keeps the Google doc)">&times;</button>
      </div>`;
      }
      const ty = docType(it);
      const mine = tags.includes('via:ui');
      return `<div class="kn__tile" data-dpv="${esc(it.g)}" title="Preview">
        <span class="kn__ic kn__ic--${ty.toLowerCase()}">${ty}</span>
        <span class="kn__name">${nm}</span>
        ${mine ? '' : '<span class="kp__src" title="agent-written note">agent</span>'}
        ${mine ? syncBtn + pinBtn : ''}
        ${mine ? `<button class="kn__x" data-ddel="${esc(it.g)}" title="Remove">&times;</button>` : ''}
      </div>`;
    }).join('');
    // Locally-picked files mid-upload: a transient tile showing extraction/index
    // progress. On success the tile is dropped and the real mesh doc takes its
    // place; on failure it stays with an error and a remove button.
    const localTiles = localDocs.map((it) => {
      const ty = docType({ p: it.name });
      const failed = it.status === 'failed';
      const badge = failed
        ? `<span class="kp__src kp__src--err" title="${esc(it.error || 'upload failed')}">failed</span>`
        : '<span class="kp__src kp__src--busy" title="Extracting text and indexing into memory">indexing&hellip;</span>';
      return `<div class="kn__tile kn__tile--local${failed ? ' kn__tile--err' : ''}" title="${failed ? esc(it.error || 'upload failed') : 'Extracting and indexing'}">
        <span class="kn__ic kn__ic--${ty.toLowerCase()}">${ty}</span>
        <span class="kn__name">${esc(it.name)}</span>
        ${badge}
        ${failed ? `<button class="kn__x" data-lrm="${it.id}" title="Dismiss">&times;</button>` : ''}
      </div>`;
    }).join('');
    docsHtml = '<div class="kn__grid">' + serverTiles + localTiles + '</div>';
  }
  box.innerHTML = `
    <div class="kp__sec">
      <div class="kp__h"><span>System instruction</span><button class="kp__edit" id="kpEdit">Edit</button></div>
      <div class="kp__prompt" id="kpPrompt">${promptHtml}</div>
    </div>
    <div class="kp__sec">
      <div class="kp__h"><span>Rules</span></div>
      ${rulesHtml}
    </div>
    <div class="kp__sec">
      <div class="kp__h"><span>Documents</span><button class="kn__add" id="docAdd"><i data-lucide="plus"></i> Add</button></div>
      ${docsHtml}
    </div>`;
  { const e = $('#kpEdit'); if (e) e.addEventListener('click', () => openPromptEditor(slug)); }
  { const e = $('#kpPrompt'); if (e) e.addEventListener('click', () => openPromptEditor(slug)); }
  { const e = $('#docAdd'); if (e) e.addEventListener('click', () => {
      // "+" opens a source picker: write a doc inline, upload a local file, or
      // pull from Google Drive. New document + Upload file are wired; Drive is a
      // mockup placeholder. Upload currently only makes a local tile -- the file
      // is NOT sent to the server yet (placeholder for the real upload).
      const pop = showPop(e, `
        <button class="pop__it" data-dm="new"><i data-lucide="file-plus"></i> New document</button>
        <button class="pop__it" data-dm="upload"><i data-lucide="upload"></i> Upload file</button>
        <button class="pop__it" data-dm="drive"><i data-lucide="hard-drive"></i> New Google Doc</button>
        <button class="pop__it" data-dm="link"><i data-lucide="link"></i> Link a Google Doc</button>`);
      pop.querySelector('[data-dm="new"]').addEventListener('click', () => { closePop(); openDocTextModal(slug); });
      pop.querySelector('[data-dm="upload"]').addEventListener('click', () => { closePop(); pickLocalFiles(slug); });
      pop.querySelector('[data-dm="drive"]').addEventListener('click', () => { closePop(); openGdriveCreateModal(slug); });
      pop.querySelector('[data-dm="link"]').addEventListener('click', () => { closePop(); openGdrivePicker(slug); });
    }); }
  box.querySelectorAll('.kn__x[data-ddel]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const tile = b.closest('.kn__tile');
    const nm = (tile && tile.querySelector('.kn__name') && tile.querySelector('.kn__name').textContent.trim()) || 'this document';
    if (!confirm(`Remove "${nm}" from the agent's memory?\n\nThis permanently deletes it from memory and the agent will no longer know about it.`)) return;
    deleteDocument(slug, b.dataset.ddel);
  }));
  box.querySelectorAll('.kn__x[data-lrm]').forEach((b) => b.addEventListener('click', (e) => { e.stopPropagation(); _apLocalDocs = _apLocalDocs.filter((x) => String(x.id) !== b.dataset.lrm); renderAgentKnowledge(slug); }));
  box.querySelectorAll('.kn__tile[data-dpv]').forEach((t) => t.addEventListener('click', () => openDocPreview(slug, t.dataset.dpv)));
  box.querySelectorAll('.kn__tile[data-gopen]').forEach((t) => t.addEventListener('click', () => { if (t.dataset.gopen) window.open(t.dataset.gopen, '_blank', 'noopener'); }));
  box.querySelectorAll('.kn__pin[data-pin]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const guid = b.dataset.pin;
    const wantPinned = b.dataset.pinned !== '1';
    // Optimistic: flip the doc's pin state in memory and re-render now (this also
    // shows/hides the per-doc sync button), reconcile only if the server rejects.
    const flip = (on) => {
      const doc = ((_apDocs && _apDocs.docs) || []).find((x) => x.g === guid);
      if (doc) { doc.t = (doc.t || []).filter((x) => x !== 'pin'); if (on) doc.t.push('pin'); }
      renderAgentKnowledge(slug);
    };
    flip(wantPinned);
    fetch(`/api/agents/${encodeURIComponent(slug)}/documents/${encodeURIComponent(guid)}/pin`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pinned: wantPinned }),
    }).then((r) => { if (!r.ok) flip(!wantPinned); }).catch(() => flip(!wantPinned));
  }));
  box.querySelectorAll('.kn__sync[data-sync]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    const guid = b.dataset.sync;
    // Drive the spin via the Web Animations API rather than a CSS @keyframes class:
    // base.css disables ALL css animations under prefers-reduced-motion, and that reset
    // wins the cascade even against !important. WAAPI is script-driven and is NOT gated by
    // the reduced-motion media query, so the spinner shows regardless of the OS setting.
    let anim = null;
    if (b.animate) {
      anim = b.animate(
        [{ transform: 'rotate(0deg)' }, { transform: 'rotate(360deg)' }],
        { duration: 700, iterations: Infinity, easing: 'linear' },
      );
    }
    const t0 = Date.now();
    // Re-assert the pin: the endpoint clears the snapshot cache and recomposes, so the
    // pinned content is re-fetched fresh from the source. Spin for at least ~0.7s so the
    // refresh is visible even when the request is fast.
    fetch(`/api/agents/${encodeURIComponent(slug)}/documents/${encodeURIComponent(guid)}/pin`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pinned: true }),
    }).catch(() => {}).finally(() => setTimeout(() => { if (anim) anim.cancel(); }, Math.max(0, 700 - (Date.now() - t0))));
  }));
  // Rule chips toggle the policy on/off for THIS agent. action: include = force on,
  // exclude = force off (PUT /agents/<slug>/policies/<slug>). Optimistic flip + re-render,
  // reconcile only if the server rejects.
  box.querySelectorAll('.kp__rule[data-policy]').forEach((chip) => {
    const toggle = (e) => {
      e.stopPropagation();
      if (chip.classList.contains('kp__rule--busy')) return;
      const ps = chip.dataset.policy;
      const wantOn = chip.dataset.applied !== '1';
      const flip = (on) => {
        const pol = (_apPolicies || []).find((x) => x.slug === ps);
        if (pol) pol.applied = on;
        renderAgentKnowledge(slug);
      };
      flip(wantOn);
      fetch(`/api/agents/${encodeURIComponent(slug)}/policies/${encodeURIComponent(ps)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: wantOn ? 'include' : 'exclude' }),
      }).then((r) => { if (!r.ok) flip(!wantOn); }).catch(() => flip(!wantOn));
    };
    chip.addEventListener('click', toggle);
    chip.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(e); } });
  });
  if (window.lucide) lucide.createIcons();
}
// Upload file: open the OS file picker and, for each chosen file, stage an
// "indexing" tile, stream the file to the server (extract -> markdown -> mesh),
// then drop the tile and reload the real documents. On failure the tile stays
// with an error. The file goes straight to the agent's memory; nothing is kept
// client-side on success.
function pickLocalFiles(slug) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.multiple = true;
  inp.style.display = 'none';
  inp.addEventListener('change', () => {
    const files = Array.from(inp.files || []);
    inp.remove();
    files.forEach((f) => uploadOneFile(slug, f));
  });
  document.body.appendChild(inp);
  inp.click();
}
async function uploadOneFile(slug, file) {
  const entry = { id: ++_apLocalSeq, slug, name: file.name, status: 'indexing' };
  _apLocalDocs.push(entry);
  renderAgentKnowledge(slug);
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents/upload?name=${encodeURIComponent(file.name)}`,
      { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file });
    if (!r.ok) {
      let msg = 'http ' + r.status;
      try { const j = await r.json(); msg = j.error || msg; } catch (_) {}
      throw new Error(msg);
    }
    // success: drop the staging tile and pull the freshly-indexed doc from mesh
    _apLocalDocs = _apLocalDocs.filter((x) => x.id !== entry.id);
    await loadAgentDocuments(slug);
    renderAgentKnowledge(slug);
    renderOverview();
  } catch (e) {
    entry.status = 'failed';
    entry.error = String((e && e.message) || e);
    if (slug === _apSlug) renderAgentKnowledge(slug);
  }
}
function openPromptEditor(slug) {
  const a = agentBySlug(slug) || { slug };
  const cur = String(a.specialist_body || a.body_md || '');
  const ov = document.createElement('div'); ov.className = 'knmodal';
  ov.innerHTML = `<div class="knmodal__box knmodal__box--lg"><div class="knmodal__h">System instruction</div>
    <textarea class="sn-field knmodal__c knmodal__c--lg" id="pmText"></textarea>
    <div class="knmodal__err" id="pmErr"></div>
    <div class="knmodal__f"><button class="knbtn" id="pmCancel">Cancel</button><button class="knbtn knbtn--p" id="pmSave">Save</button></div></div>`;
  document.body.appendChild(ov);
  ov.querySelector('#pmText').value = cur;
  const close = () => ov.remove();
  ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#pmCancel').addEventListener('click', close);
  ov.querySelector('#pmSave').addEventListener('click', async () => {
    const body = ov.querySelector('#pmText').value;
    const btn = ov.querySelector('#pmSave'); btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(slug)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ specialist_body: body }) });
      if (!r.ok) throw new Error('http ' + r.status);
      const ag = agentBySlug(slug); if (ag) ag.specialist_body = body;
      close(); renderAgentKnowledge(slug); renderOverview();
    } catch (e) { ov.querySelector('#pmErr').textContent = 'Could not save: ' + e.message; btn.disabled = false; btn.textContent = 'Save'; }
  });
}
function openDocTextModal(slug) {
  const ov = document.createElement('div'); ov.className = 'knmodal';
  ov.innerHTML = `<div class="knmodal__box"><div class="knmodal__h">Add a document</div>
    <input class="sn-field knmodal__t" id="dcT" placeholder="Title (e.g. Refund policy)">
    <textarea class="sn-field knmodal__c" id="dcC" placeholder="Paste the text the agent should know…"></textarea>
    <div class="knmodal__err" id="dcErr"></div>
    <div class="knmodal__f"><button class="knbtn" id="dcCancel">Cancel</button><button class="knbtn knbtn--p" id="dcSave">Add</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#dcCancel').addEventListener('click', close);
  ov.querySelector('#dcT').focus();
  ov.querySelector('#dcSave').addEventListener('click', async () => {
    const title = ov.querySelector('#dcT').value.trim();
    const content = ov.querySelector('#dcC').value.trim();
    const err = ov.querySelector('#dcErr');
    if (!content) { err.textContent = 'Paste some text first.'; return; }
    const btn = ov.querySelector('#dcSave'); btn.disabled = true; btn.textContent = 'Adding…';
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents`, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ title, content }) });
      if (!r.ok && r.status !== 201) throw new Error('http ' + r.status);
      close(); loadAgentDocuments(slug);
    } catch (e) { err.textContent = 'Could not add: ' + e.message; btn.disabled = false; btn.textContent = 'Add'; }
  });
}
// Link an EXISTING Google doc via the Google Picker. Under drive.file the app
// can only read a doc the user selects in the Picker, so we open the Picker,
// the user picks, and we store a thin pointer (source of truth stays in Drive).
let _gapiLoaded = false;
function _loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve();
    const s = document.createElement('script'); s.src = src; s.async = true;
    s.onload = resolve; s.onerror = () => reject(new Error('failed to load ' + src));
    document.head.appendChild(s);
  });
}
async function openGdrivePicker(slug) {
  let cfg;
  try {
    cfg = await (await fetch(`/api/agents/${encodeURIComponent(slug)}/gdrive/picker-config`, { headers: { Accept: 'application/json' } })).json();
  } catch (_) { cfg = null; }
  if (!cfg || !cfg.client_id || !cfg.api_key) { alert('Google Drive is not connected on this node yet.'); return; }
  try {
    await _loadScript('https://accounts.google.com/gsi/client');
    await _loadScript('https://apis.google.com/js/api.js');
    if (!_gapiLoaded) { await new Promise((res) => gapi.load('picker', res)); _gapiLoaded = true; }
  } catch (e) { alert('Could not load Google Picker: ' + e.message); return; }
  const tokenClient = google.accounts.oauth2.initTokenClient({
    client_id: cfg.client_id,
    scope: 'https://www.googleapis.com/auth/drive.file',
    callback: (resp) => {
      if (resp.error) { alert('Google sign-in was cancelled or failed.'); return; }
      _showGdrivePicker(slug, cfg, resp.access_token);
    },
  });
  tokenClient.requestAccessToken({ prompt: '' });
}
function _showGdrivePicker(slug, cfg, token) {
  const view = new google.picker.DocsView(google.picker.ViewId.DOCS)
    .setIncludeFolders(true).setSelectFolderEnabled(false).setOwnedByMe(true);
  const picker = new google.picker.PickerBuilder()
    .setAppId(cfg.app_id)
    .setOAuthToken(token)
    .setDeveloperKey(cfg.api_key)
    .addView(view)
    .setTitle('Pick a document to link')
    .setCallback(async (data) => {
      if (data.action !== google.picker.Action.PICKED) return;
      const doc = data.docs && data.docs[0]; if (!doc) return;
      const url = doc.url || `https://docs.google.com/document/d/${doc.id}/edit`;
      try {
        const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents/gdrive-link`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ name: doc.name, url }) });
        if (!r.ok && r.status !== 201) throw new Error('http ' + r.status);
        await loadAgentDocuments(slug); renderAgentKnowledge(slug);
      } catch (e) { alert('Could not link the document: ' + e.message); }
    })
    .build();
  picker.setVisible(true);
}
// New Google Doc: created in the connected account's designated folder via
// core-api; a thin link pointer lands in the agent's knowledge (source of truth
// stays in Drive). The agent reads it live with its gdrive_read tool.
function openGdriveCreateModal(slug) {
  const ov = document.createElement('div'); ov.className = 'knmodal';
  ov.innerHTML = `<div class="knmodal__box"><div class="knmodal__h">New Google Doc</div>
    <input class="sn-field knmodal__t" id="gdT" placeholder="Title (e.g. Weekend specials)">
    <textarea class="sn-field knmodal__c" id="gdC" placeholder="Content in markdown. Optional, you can fill it in Google later."></textarea>
    <div class="knmodal__err" id="gdErr"></div>
    <div class="knmodal__f"><button class="knbtn" id="gdCancel">Cancel</button><button class="knbtn knbtn--p" id="gdSave">Create in Google Drive</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#gdCancel').addEventListener('click', close);
  ov.querySelector('#gdT').focus();
  ov.querySelector('#gdSave').addEventListener('click', async () => {
    const title = ov.querySelector('#gdT').value.trim();
    const content = ov.querySelector('#gdC').value;
    const err = ov.querySelector('#gdErr');
    if (!title) { err.textContent = 'Give it a title.'; return; }
    const btn = ov.querySelector('#gdSave'); btn.disabled = true; btn.textContent = 'Creating…';
    try {
      const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents/gdrive-create`, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify({ title, content }) });
      if (!r.ok && r.status !== 201) { let m = 'http ' + r.status; try { const j = await r.json(); m = j.detail || j.error || m; } catch (_) {} throw new Error(m); }
      close(); await loadAgentDocuments(slug); renderAgentKnowledge(slug);
    } catch (e) { err.textContent = 'Could not create: ' + e.message; btn.disabled = false; btn.textContent = 'Create in Google Drive'; }
  });
}
async function deleteDocument(slug, guid) {
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents/${encodeURIComponent(guid)}`, { method: 'DELETE' });
    if (!r.ok && r.status !== 204) throw new Error('http ' + r.status);
    loadAgentDocuments(slug);
  } catch (e) { console.error('[studio] delete document failed', e); }
}
async function openDocPreview(slug, guid) {
  const ov = document.createElement('div'); ov.className = 'knmodal';
  ov.innerHTML = `<div class="knmodal__box"><div class="knmodal__h">Document</div><div class="mem__body docbody" id="dpvBody">${skText(8)}</div><div class="knmodal__f"><button class="knbtn" id="dpvClose">Close</button></div></div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
  ov.querySelector('#dpvClose').addEventListener('click', close);
  try {
    const r = await fetch(`/api/agents/${encodeURIComponent(slug)}/documents/${encodeURIComponent(guid)}`, { headers: { Accept: 'application/json' } });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    ov.querySelector('#dpvBody').innerHTML = window.snFormatter.markdown(d.content || d.text || d.preview || '');
  } catch (e) { ov.querySelector('#dpvBody').innerHTML = '<div class="tkt__mut">Could not load: ' + esc(e.message) + '</div>'; }
}


async function loadAgentTasks(slug) {
  _apTasks = null;
  const box = $('#apTasks');
  try {
    const r = await tasksFetch('/api/tasks');
    if (!r.ok) throw new Error('http ' + r.status);
    const all = await r.json();
    const list = (Array.isArray(all) ? all : (all.tasks || [])).filter((t) => (t.assignee || '') === slug);
    if (slug !== _apSlug) return;
    _apTasks = list;
    const n = $('#apTasksN'); if (n) n.textContent = list.length ? '(' + list.length + ')' : '';
    renderTasksPane(slug);
    renderOverview();
  } catch (e) {
    _apTasks = [];
    if (box) box.innerHTML = '<div class="ap__empty">Could not load tasks.</div>';
    renderOverview();
  }
}

let _apTaskChart = null;
const _AP_ST_COLOR = { open: '#cbd5e1', assessing: '#7dd3fc', awaiting_approval: '#c4b5fd', in_progress: '#a5b4fc', pending_review: '#fcd34d', done: '#6ee7b7', blocked: '#fdba74', escalated: '#f0abfc', rejected: '#94a3b8', incident: '#fda4af' };
// Agent tasks = a mini dashboard: status donut + KPIs + progress (left), grouped
// task list with gates on top and Done collapsed (right).
function renderTasksPane() {
  const box = $('#apTasks'); if (!box) return;
  if (_apTaskChart) { _apTaskChart.destroy(); _apTaskChart = null; }
  const tasks = _apTasks || [];
  if (!tasks.length) { box.innerHTML = '<div class="ap__empty">No tasks for this agent.</div>'; return; }
  const order = ['awaiting_approval', 'pending_review', 'in_progress', 'assessing', 'open', 'blocked', 'escalated', 'incident', 'rejected', 'done'];
  const oi = (s) => { const i = order.indexOf(s); return i < 0 ? 50 : i; };
  // gates first, then pipeline order; newest-updated within a status
  const sorted = tasks.slice().sort((a, b) => oi(tkStage(a)) - oi(tkStage(b)) || String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  const rows = sorted.map(tkRowHTML).join('');
  box.innerHTML = `
    <div class="aptk2__tablewrap">
      <table class="tkt"><thead><tr><th style="width:46px">#</th><th style="width:360px">Title</th><th style="width:200px">Project / Goal</th><th style="width:150px">Flow</th><th style="width:150px">Status</th><th style="width:96px">Priority</th><th style="width:150px">Assignee</th><th style="width:130px">Created by</th><th style="width:96px">Due</th><th style="width:112px">Updated</th></tr></thead><tbody>${rows}</tbody></table>
    </div>`;
  box.querySelectorAll('.tkt tbody tr[data-id]').forEach((r) => r.addEventListener('click', () => openTask(r.dataset.id)));
}

/* ==== workflow studio =====================================================
   The workflow detail page: the flow on a canvas you can drag and zoom, drawn
   top to bottom, with a side panel for Run / History / Block. Clicking a run
   shows the same drawing with that run's block states, polled every 3 s while
   it is open. A run is what the Runner's workflow events say about one planner
   task: payload.flow, payload.task_id and the block snapshot on each step. */
let _wfs = { name: null, tab: 'history', block: null, run: null, poll: null, z: 1, x: 40, y: 30 };

function wfsEdges(steps) {
  const known = new Set(steps.map((s) => s.name));
  const out = [], seen = new Set();
  const add = (from, to, label) => {
    if (!known.has(from) || !known.has(to)) return;
    const k = from + '>' + to + '>' + label;
    if (seen.has(k)) return; seen.add(k);
    out.push({ from, to, label });
  };
  steps.forEach((s) => Object.keys(s.routing || {}).forEach((sig) =>
    add(s.name, s.routing[sig], sig === '_default' ? '' : sig)));
  steps.forEach((s) => (s.depends_on || []).forEach((d) => {
    if (!out.some((e) => e.from === d && e.to === s.name)) add(d, s.name, '');
  }));
  return out;
}

function wfsBoard(w) {
  const steps = w.steps || [];
  const edges = wfsEdges(steps);
  const rank = wfRanks(steps, edges.filter((e) => e.from !== e.to));
  const rows = [];
  steps.forEach((s) => { const r = rank.get(s.name) || 0; (rows[r] = rows[r] || []).push(s); });
  const hasOut = new Set(edges.map((e) => e.from));
  const body = rows.map((row) => '<div class="wfs-row">' + (row || []).map((s) => {
    const nd = { gate: s.kind === 'gate' || /gate/.test(s.name), term: !hasOut.has(s.name),
                 name: String(s.name).replace(/_/g, ' '), who: s.specialist || s.kind || 'step' };
    const cls = nd.gate ? 'is-gate' : (nd.term ? 'is-term' : '');
    return `<button class="wf-node wfs-node ${cls}" data-wfn="${esc(s.name)}" data-rank="${rank.get(s.name) || 0}">
      <span class="wf-node__ic"><svg viewBox="0 0 24 24">${wfIcon(nd)}</svg></span>
      <span class="wf-node__t"><span class="wf-node__nm">${esc(nd.name)}</span><span class="wf-node__tag">${esc(nd.who)}</span></span>
      <span class="wfs-state"></span>
    </button>`;
  }).join('') + '</div>').join('');
  return `<div class="wfs-graph" data-edges="${esc(JSON.stringify(edges))}"><svg class="wfs-svg" aria-hidden="true"></svg>${body}</div>`;
}

function wfsDraw() {
  const g = document.querySelector('.wfs-graph'); if (!g) return;
  const svg = g.querySelector('.wfs-svg');
  let edges = []; try { edges = JSON.parse(g.getAttribute('data-edges') || '[]'); } catch (e) { return; }
  // Measured on screen and divided by the zoom: independent of margins and of
  // which ancestor is positioned.
  const gb = g.getBoundingClientRect(), z = _wfs.z || 1;
  const pos = {};
  g.querySelectorAll('[data-wfn]').forEach((el) => {
    const r = el.getBoundingClientRect();
    const l = (r.left - gb.left) / z, t = (r.top - gb.top) / z, w = r.width / z, h = r.height / z;
    pos[el.getAttribute('data-wfn')] = { l, t, r: l + w, b: t + h, cx: l + w / 2, cy: t + h / 2,
      rk: +el.getAttribute('data-rank') || 0 };
  });
  const right = Object.values(pos).reduce((m, p) => Math.max(m, p.r), 0);
  const left = Object.values(pos).reduce((m, p) => Math.min(m, p.l), Infinity);
  const parts = []; let lane = 0, llane = 0;
  edges.forEach((e) => {
    const a = pos[e.from], b = pos[e.to]; if (!a || !b) return;
    let d, lx, ly;
    if (e.from === e.to) {                                  // waits on itself
      d = `M ${a.r} ${a.cy - 8} C ${a.r + 46} ${a.cy - 30}, ${a.r + 46} ${a.cy + 30}, ${a.r} ${a.cy + 8}`;
      lx = a.r + 40; ly = a.cy + 3;
    } else if (b.t > a.b - 2 && b.rk - a.rk > 1) {         // skips rows: along the left
      llane += 1;
      const x = left - 50 - llane * 26;
      d = `M ${a.l} ${a.cy} C ${x} ${a.cy}, ${x} ${b.cy}, ${b.l} ${b.cy}`;
      lx = x - 4; ly = (a.cy + b.cy) / 2 + 3;
      parts.push(`<path class="wf-edge wfs-edge" data-from="${esc(e.from)}" data-to="${esc(e.to)}" d="${d}" marker-end="url(#wfsArrow)"/>`);
      if (e.label) parts.push(`<text class="wf-edge__lbl" x="${lx}" y="${ly}" text-anchor="end">${esc(e.label)}</text>`);
      return;
    } else if (b.t > a.b - 2) {                             // down
      const mid = a.b + (b.t - a.b) / 2;
      d = `M ${a.cx} ${a.b} C ${a.cx} ${mid}, ${b.cx} ${mid}, ${b.cx} ${b.t}`;
      lx = (a.cx + b.cx) / 2 + 6; ly = mid + 3;
    } else {                                                // back up, along the right
      lane += 1;
      const x = right + 60 + lane * 26;
      d = `M ${a.r} ${a.cy} C ${x} ${a.cy}, ${x} ${b.cy}, ${b.r} ${b.cy}`;
      lx = x - 8; ly = (a.cy + b.cy) / 2 + 3;
    }
    parts.push(`<path class="wf-edge wfs-edge" data-from="${esc(e.from)}" data-to="${esc(e.to)}" d="${d}" marker-end="url(#wfsArrow)"/>`);
    if (e.label) parts.push(`<text class="wf-edge__lbl" x="${lx}" y="${ly}">${esc(e.label)}</text>`);
  });
  const W = right + 120 + lane * 26, H = gb.height / z + 20;
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  svg.style.width = W + 'px'; svg.style.height = H + 'px';
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = `<defs><marker id="wfsArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6"
    orient="auto-start-reverse"><path d="M0,1 L7,4 L0,7 z" class="wf-edge__head"/></marker>
    <marker id="wfsArrowOn" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6"
    orient="auto-start-reverse"><path d="M0,1 L7,4 L0,7 z" fill="#1f8a4c"/></marker></defs>` + parts.join('');
  if (_wfs.run && _wfs.painted) wfsPaintRun(_wfs.painted);
}

function wfsApplyView() {
  const inner = document.querySelector('.wfs-inner');
  if (inner) inner.style.transform = `translate(${_wfs.x}px, ${_wfs.y}px) scale(${_wfs.z})`;
  const z = document.querySelector('#wfsZoom'); if (z) z.textContent = Math.round(_wfs.z * 100) + '%';
}

function wfsCanvas() {
  const c = document.querySelector('.wfs-canvas'); if (!c) return;
  let drag = null;
  c.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    drag = { x: e.clientX - _wfs.x, y: e.clientY - _wfs.y, sx: e.clientX, sy: e.clientY,
             node: e.target.closest('[data-wfn]'), moved: false };
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener('pointermove', (e) => {
    if (!drag) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 4) return;
    drag.moved = true; c.classList.add('is-drag');
    _wfs.x = e.clientX - drag.x; _wfs.y = e.clientY - drag.y; wfsApplyView();
  });
  const end = () => {
    if (drag && !drag.moved && drag.node) wfsPick(drag.node);
    drag = null; c.classList.remove('is-drag');
  };
  c.addEventListener('pointerup', end); c.addEventListener('pointercancel', () => { drag = null; c.classList.remove('is-drag'); });
  c.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = c.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const z2 = Math.min(2.5, Math.max(0.3, _wfs.z * (e.deltaY < 0 ? 1.1 : 0.9)));
    _wfs.x = mx - (mx - _wfs.x) * (z2 / _wfs.z); _wfs.y = my - (my - _wfs.y) * (z2 / _wfs.z);
    _wfs.z = z2; wfsApplyView();
  }, { passive: false });
  const fit = document.querySelector('#wfsFit');
  if (fit) fit.addEventListener('click', () => { wfsCenter(); });
}
function wfsPick(n) {
  _wfs.block = n.getAttribute('data-wfn'); _wfs.tab = 'block'; wfsSide();
  document.querySelectorAll('.wfs-canvas [data-wfn]').forEach((x) => x.classList.toggle('is-sel', x === n));
}
// Zoom 100% with the drawing centred horizontally near the top.
function wfsCenter() {
  // Fit the whole drawing (wires included) in the canvas, never above 100%.
  const c = document.querySelector('.wfs-canvas'), g = document.querySelector('.wfs-graph');
  const svg = g && g.querySelector('.wfs-svg');
  if (c && g) {
    const gw = Math.max(g.offsetWidth, svg ? parseFloat(svg.style.width) || 0 : 0);
    const gh = Math.max(g.offsetHeight, svg ? parseFloat(svg.style.height) || 0 : 0);
    _wfs.z = Math.min(1, (c.clientWidth - 40) / gw, (c.clientHeight - 30) / gh);
    _wfs.x = Math.max(10, (c.clientWidth - gw * _wfs.z) / 2); _wfs.y = 14;
  }
  wfsApplyView();
}

// Runs grouped by the planner task they drive (the runner's events carry task_id).
async function wfsRuns(name) {
  let ev = [];
  try { ev = ((await (await fetch('/api/events?entity_type=workflow&limit=600')).json()).events) || []; }
  catch (e) { return []; }
  const by = new Map();
  ev.forEach((e) => {
    const p = e.payload || {};
    if (p.flow !== name || p.task_id == null) return;
    const k = String(p.task_id);
    if (!by.has(k)) by.set(k, { task: k, events: [] });
    by.get(k).events.push(e);
  });
  return [...by.values()].map((r) => {
    r.events.sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
    const last = r.events[r.events.length - 1];
    const steps = r.events.filter((e) => (e.event_type || '') === 'workflow.step');
    r.blocks = steps.length ? (steps[steps.length - 1].payload || {}).blocks || {} : {};
    r.done = r.events.some((e) => (e.event_type || '') === 'workflow.completed');
    r.started = r.events[0].ts; r.ts = last.ts;
    const age = Date.now() - new Date(r.ts).getTime();
    // The Runner ends a flow whose task waits for its reviewer with
    // workflow.awaiting_review, and says workflow.completed only when the task
    // is closed. Without this state such a run read as "running" for half an
    // hour after its last block, although nothing was running.
    r.review = !r.done && (last.event_type || '') === 'workflow.awaiting_review';
    r.state = r.done ? 'completed' : (r.review ? 'review' : (age < 30 * 60 * 1000 ? 'running' : 'idle'));
    return r;
  }).sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
}

const wfsStateLabel = (s) => (s === 'review' ? 'awaiting review' : s);

function wfsPaintRun(run) {
  _wfs.painted = run || null;
  const g = document.querySelector('.wfs-graph'); if (!g) return;
  const w = _wfList.find((x) => x.name === _wfs.name) || {};
  const blocks = (run && run.blocks) || {};
  // "now": the first block that is not done and whose dependencies are done.
  let now = null;
  if (run && !run.done) {
    (w.steps || []).some((s) => {
      const st = blocks[s.name];
      if (st === 'done' || st === 'skipped') return false;
      if ((s.depends_on || []).every((d) => blocks[d] === 'done')) { now = s.name; return true; }
      return false;
    });
  }
  g.classList.toggle('is-run', !!run);
  g.querySelectorAll('[data-wfn]').forEach((n) => {
    const k = n.getAttribute('data-wfn');
    const st = run ? (k === now ? (run.state === 'running' ? 'now' : 'stopped') : (blocks[k] || 'waiting')) : '';
    n.dataset.st = st;
    const lab = n.querySelector('.wfs-state');
    if (lab) lab.textContent = st === 'now' ? 'in progress' : (st === 'stopped' ? 'stopped here' : st);
  });
  g.querySelectorAll('.wfs-edge').forEach((p) => {
    const a = blocks[p.dataset.from], b = blocks[p.dataset.to];
    p.classList.toggle('is-walked', !!run && a === 'done' && (b === 'done' || p.dataset.to === now));
    p.setAttribute('marker-end', p.classList.contains('is-walked') ? 'url(#wfsArrowOn)' : 'url(#wfsArrow)');
  });
}

async function wfsOpenRun(task) {
  if (_wfs.poll) { clearInterval(_wfs.poll); _wfs.poll = null; }
  _wfs.run = task;
  const name = _wfs.name;
  const tick = async () => {
    if (_wfState.level !== 'detail' || _wfs.run !== task) { clearInterval(_wfs.poll); _wfs.poll = null; return; }
    const runs = await wfsRuns(name);
    if (_wfState.level !== 'detail' || _wfs.run !== task || _wfs.name !== name) return;
    const r = runs.find((x) => x.task === task);
    wfsPaintRun(r);
    const h = document.querySelector('#wfsRunHead');
    if (h && r) h.innerHTML = `<b>Task #${esc(r.task)}</b> <span class="wfs-pill wfs-pill--${r.state}">${wfsStateLabel(r.state)}</span>
      <span class="wfs-mut">last step ${wfTime(r.ts)}</span> <button class="wf-btn wfs-btn--ghost" id="wfsBack">Back to the flow</button>`;
    const bk = document.querySelector('#wfsBack');
    if (bk) bk.onclick = () => { _wfs.run = null; wfsPaintRun(null); h.innerHTML = ''; wfsSide(); wfsDraw(); };
    if (_wfs.tab === 'history') wfsSide(runs);
  };
  await tick();
  _wfs.poll = setInterval(tick, 3000);
}

async function wfsSide(runsIn) {
  const box = document.querySelector('#wfsSide'); if (!box) return;
  const w = _wfList.find((x) => x.name === _wfs.name) || {};
  document.querySelectorAll('[data-wfstab]').forEach((b) => b.classList.toggle('is-on', b.dataset.wfstab === _wfs.tab));
  if (_wfs.tab === 'run') {
    box.innerHTML = `<div class="wfs-sh">Run a test inquiry</div>
      <input class="wf-inq" id="wfInq" placeholder="Optional: type a customer inquiry (blank = sample booking)">
      <select class="wf-dec" id="wfDec"><option value="approve">owner approves</option><option value="reject">owner rejects</option></select>
      <button class="wf-btn" id="wfRunBtn">Run</button><div class="wf-msg" id="wfMsg"></div>`;
    const b = document.querySelector('#wfRunBtn'); if (b) b.addEventListener('click', () => wfRun(w.name));
    return;
  }
  if (_wfs.tab === 'block') {
    const s = (w.steps || []).find((x) => x.name === _wfs.block);
    if (!s) { box.innerHTML = '<div class="wfs-mut">Click a block on the canvas.</div>'; return; }
    const rt = Object.keys(s.routing || {}).map((k) =>
      `<tr><td>${esc(k === '_default' ? 'anything else' : k)}</td><td>then ${esc(s.routing[k])}</td></tr>`).join('');
    box.innerHTML = `<div class="wfs-sh">${esc(s.name)}</div>
      <div class="wfs-kv"><span>runs as</span><b>${esc(s.specialist || s.kind || 'step')}</b></div>
      <div class="wfs-kv"><span>after</span><b>${esc((s.depends_on || []).join(', ') || 'start')}</b></div>
      <div class="wfs-kv"><span>max runs</span><b>${esc(s.max_runs ?? '')}</b></div>
      <div class="wfs-sub">verdict, then next step</div><table class="wfs-tbl">${rt || '<tr><td>ends the flow</td></tr>'}</table>
      <div class="wfs-mut">Prompt and logic of the block: next step of the prototype.</div>`;
    return;
  }
  // history
  const runs = runsIn || await wfsRuns(_wfs.name);
  if (!runs.length) { box.innerHTML = '<div class="wfs-mut">No runs yet.</div>'; return; }
  box.innerHTML = '<div class="wfs-sh">Runs</div>' + runs.map((r) => {
    const n = Object.values(r.blocks).filter((x) => x === 'done').length, t = Object.keys(r.blocks).length;
    return `<button class="wfs-run ${_wfs.run === r.task ? 'is-sel' : ''}" data-wfsrun="${esc(r.task)}">
      <span class="wfs-run__t">Task #${esc(r.task)}</span>
      <span class="wfs-pill wfs-pill--${r.state}">${wfsStateLabel(r.state)}</span>
      <span class="wfs-run__m">${n}/${t} blocks · ${wfTime(r.ts)}</span></button>`;
  }).join('');
  box.querySelectorAll('[data-wfsrun]').forEach((b) => b.addEventListener('click', () => wfsOpenRun(b.dataset.wfsrun)));
}

function wfRenderDetail() {
  const box = $('#workflowsBody'); if (!box) return;
  const w = _wfList.find((x) => x.name === _wfState.name);
  if (!w) { box.innerHTML = '<div class="ap__empty">Workflow not found.</div>'; return; }
  if (_wfs.name !== w.name) {
    if (_wfs.poll) clearInterval(_wfs.poll);
    _wfs = Object.assign(_wfs, { name: w.name, block: null, run: null, painted: null, poll: null, z: 1, x: 40, y: 30 });
  }
  box.innerHTML = `
    <div class="wfs">
      <div class="wfs-main">
        <div class="wfs-top">
          <div><div class="wf-panel__h">${esc(w.title || w.name)}</div>
            <div class="wf-panel__m">${esc(w.name)}${w.team ? ' · ' + esc(w.team) : ''} · spec ${esc(w.spec_version || '')}</div></div>
          <div class="wfs-tools"><span id="wfsZoom">100%</span><button class="wf-btn wfs-btn--ghost" id="wfsFit">Fit</button></div>
        </div>
        <div class="wfs-runhead" id="wfsRunHead"></div>
        <div class="wfs-canvas"><div class="wfs-inner">${wfsBoard(w)}</div></div>
      </div>
      <aside class="wfs-side">
        <div class="wfs-tabs">
          <button data-wfstab="history">History</button><button data-wfstab="run">Run</button><button data-wfstab="block">Block</button>
        </div>
        <div id="wfsSide"></div>
      </aside>
    </div>`;
  document.querySelectorAll('[data-wfstab]').forEach((b) => b.addEventListener('click', () => { _wfs.tab = b.dataset.wfstab; wfsSide(); }));
  if (window.lucide) lucide.createIcons();
  wfsCanvas();
  // The page can be painted while its view is still hidden, and icons and fonts
  // land later: every size change of the drawing redraws the wires, and the
  // first real size centres it.
  const g = document.querySelector('.wfs-graph');
  let centred = !(_wfs.x === 40 && _wfs.y === 30);
  if (_wfs.ro) _wfs.ro.disconnect();
  _wfs.ro = new ResizeObserver(() => {
    if (!g.offsetWidth) return;
    wfsDraw();
    if (!centred) { wfsCenter(); centred = true; }
  });
  _wfs.ro.observe(g);
  g.querySelectorAll('[data-wfn]').forEach((n) => _wfs.ro.observe(n));
  wfsApplyView();
  if (_wfs.run) wfsOpenRun(_wfs.run);
  wfsSide();
}
/* ==== end workflow studio =============================================== */

/* ---- when a message was written ----
   A small time under each message, and, while the chat is
   scrolled back through days that are not today, a small chip with the day
   (Yesterday, Friday, Oct 14) so the reader knows how old the part on screen is. */
function msgDate(at) {
  if (!at) return null;
  let t = String(at);
  // A timestamp with no zone is the node's UTC clock, not the reader's.
  if (/T\d\d:\d\d(:\d\d(\.\d+)?)?$/.test(t)) t += 'Z';
  const d = new Date(t);
  return isNaN(d) ? null : d;
}
function stampTime(el, at) {
  if (!el) return;
  if (at) el.dataset.at = String(at);
  const old = el.querySelector(':scope > .sn-msg__t');
  if (old) old.remove();
  const d = msgDate(el.dataset.at);
  if (!d) return;
  const t = document.createElement('time');
  t.className = 'sn-msg__t';
  t.dateTime = d.toISOString();
  t.textContent = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  el.appendChild(t);
}
function dayLabel(d) {
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const now = new Date();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff <= 0) return null;
  if (diff === 1) return 'Yesterday';
  if (diff < 7) return d.toLocaleDateString('en-US', { weekday: 'long' });
  return d.toLocaleDateString('en-US', d.getFullYear() === now.getFullYear()
    ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
}
(function dayChip() {
  if (document.getElementById('snMsgTimeCss')) return;
  const css = document.createElement('style');
  css.id = 'snMsgTimeCss';
  css.textContent = `
    .sn-msg__t { display: block; margin-top: 4px; font-size: 10.5px; line-height: 1; opacity: .32;
      text-align: right; user-select: none; -webkit-user-select: none; font-variant-numeric: tabular-nums; }
    .sn-daychip { position: fixed; z-index: 30; left: 50%; transform: translateX(-50%); padding: 3px 11px;
      border-radius: 999px; font-size: 12px; line-height: 1.5; pointer-events: none; opacity: 0;
      transition: opacity .18s ease; color: var(--sn-ink-mute, #777);
      background: var(--sn-surface, #fff); border: 1px solid var(--sn-rule, rgba(0,0,0,.12));
      box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .sn-daychip.is-on { opacity: 1; }`;
  document.head.appendChild(css);
  const chip = document.createElement('div');
  chip.className = 'sn-daychip';
  chip.setAttribute('aria-hidden', 'true');
  document.body.appendChild(chip);
  let raf = 0, hideT = 0;
  function paint() {
    raf = 0;
    const sc = thread && thread.parentElement;
    if (!sc) return;
    const box = sc.getBoundingClientRect();
    let label = null;
    for (const m of thread.children) {
      if (!m.dataset || !m.dataset.at) continue;
      if (m.getBoundingClientRect().bottom > box.top + 4) { const d = msgDate(m.dataset.at); label = d && dayLabel(d); break; }
    }
    if (!label) { chip.classList.remove('is-on'); return; }
    chip.textContent = label;
    chip.style.top = (box.top + 8) + 'px';
    chip.classList.add('is-on');
    clearTimeout(hideT);
    hideT = setTimeout(() => chip.classList.remove('is-on'), 1400);
  }
  const hook = () => {
    const sc = thread && thread.parentElement;
    if (!sc) return;
    sc.addEventListener('scroll', () => { if (!raf) raf = requestAnimationFrame(paint); }, { passive: true });
  };
  hook();
})();
