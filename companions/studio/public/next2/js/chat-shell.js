// chat-shell.js: the Studio chats on a desktop as in a messenger. Loaded after app.js and
// chat-shell-model.js; uses app.js's top-level names and wraps renderTabs,
// paintDeskWork, setView and setActive.
//
//  - the sidebar is the narrow rail of section icons (its collapsed state); the
//    list of all agents is no longer on screen beside the chats;
//  - the chats are a column on the left: the same tabs, as rows (avatar, name,
//    last message, time), with search, New chat and All/Unread/Working
//    filters, in place of the row of tabs at the top (the unread counts are
//    chat-unread.js's, read from each row);
//  - the chat has a header: the agent, or every agent who took part, what they
//    are doing, the agent's other chats and the Chat/Canvas switch;
//  - a panel on the right, folded away with its cross or the (i) button: the
//    agent's card (now, open tasks, chats), or in a chat of several agents who
//    is in it, each opening their own card. A name in the thread opens it too.
// Phones keep their own layout (phone-chats.js): nothing here applies there.
(() => {
  const M = window.SnChatShell;
  const app = document.querySelector('.app');
  const main = document.querySelector('.main');
  const top = document.querySelector('.main__top');
  const task = document.getElementById('task');
  const tabsEl = document.getElementById('tabs');
  const thread = document.getElementById('thread');
  if (!M || !app || !main || !top || !task || !tabsEl || !thread || typeof _phone === 'undefined') return;

  const LS_INFO = 'sn-chat-info';
  const LS_RAIL = 'sn-chat-shell-rail';
  let query = '';
  // all | recent | unread | working; the choice is kept (/next2: Recent)
  const LS_FILTER = 'sn-chat-filter';
  let filter = (() => { try { return localStorage.getItem(LS_FILTER) || 'all'; } catch { return 'all'; } })();
  let card = null;                 // the agent whose card shows in a chat of several
  let members = [];
  // Closed until the person opens it (the i, or an agent's name): open by default
  // it came and went with the chat on screen and the layout jumped (tester 2026-10-30)
  let infoOpen = (() => { try { return localStorage.getItem(LS_INFO) === '1'; } catch { return false; } })();
  let states = {};                 // slug -> runtime row, GET /api/agents/state
  let tasks = null;
  let tasksAt = 0;

  // The chats view is the one whose #task is shown (setView hides it on every
  // other). Read from the page, not remembered: a link to a task opens that view
  // before this script loads, and a remembered 'chats' kept the column and its
  // empty header over the task.
  const on = () => !_phone.matches && task.style.display !== 'none';
  // The list's search and chips work on the phone's list screen too; the rest of the shell is desktop only.
  const listOn = () => on() || (_phone.matches && app.classList.contains('m-list'));
  const nameOf = (s) => { const a = agentBySlug(s); return (a && a.title) || s; };
  const isAgent = (s) => !!agentBySlug(s);
  const svg = (d, w) => `<svg viewBox="0 0 24 24" width="${w || 16}" height="${w || 16}" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const I_COMPOSE = svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>', 16);
  const I_SEARCH = svg('<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>', 15);
  const I_DOWN = svg('<path d="M6 9l6 6 6-6"/>', 14);
  // a chevron, never a text arrow
  const I_BACK = svg('<path d="M15 6l-6 6 6 6"/>', 14);
  const I_INFO = svg('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>', 18);
  const I_GEAR = svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>', 17);
  function avHtml(s, cls) {
    const a = agentBySlug(s);
    const n = ((a && a.title) || s || '?').trim();
    return `<span class="cs-av ${cls || ''}" data-s="${esc(s || '')}">`
      + `${avInner(a ? avResolve(a.avatar) : null, n.charAt(0).toUpperCase())}</span>`;
  }
  const sysAv = (cls) => `<span class="cs-av ${cls || ''}">${SN_MARK}</span>`;
  const curDesk = () => desks.find((d) => d.id === activeId) || null;
  const statusOf = (s) => {
    const d = curDesk();
    if (d && deskAgent(d) === s && deskIsWorking(d.id)) return { key: 'working', label: 'working' };
    return M.agentStatus(states[s]);
  };

  // ---- the list column: header, search, filters ----
  const head = document.createElement('div');
  head.className = 'cs-head';
  // New chat is one labelled button at the top of the list, where a messenger puts
  // it; a bare + in a corner was not found.
  head.innerHTML = `<div class="cs-head__row"><span class="cs-head__t">Chats</span></div>`
    + `<button type="button" class="sn-btn sn-btn--primary cs-new">${I_COMPOSE}New chat</button>`
    + `<label class="cs-search">${I_SEARCH}<input type="search" placeholder="Search chats" aria-label="Search chats"></label>`
    + '<div class="cs-chips" role="tablist"></div>';
  top.insertBefore(head, tabsEl);
  head.querySelector('.cs-new').addEventListener('click', (e) => {
    e.stopPropagation();
    // with one agent there is nobody to choose
    if (agentsList.length === 1) return createDeskFor(agentsList[0].slug, 'New chat');
    openNewChatPicker(e.currentTarget);
  });
  head.querySelector('input').addEventListener('input', (e) => { query = e.target.value; applyFilter(); });
  const empty = document.createElement('div');
  empty.className = 'cs-empty';
  function rowsFromTabs() {
    return Array.from(tabsEl.querySelectorAll(':scope > .tab[data-id]')).map((el) => ({
      el,
      name: (el.querySelector('.tab__nm') || el.querySelector('.tab__title') || {}).textContent || '',
      title: (el.querySelector('.tab__title') || {}).textContent || '',
      last: (el.querySelector('.m-last') || {}).textContent || '',
      working: el.classList.contains('is-working'),
      unread: Number(el.dataset.unread) || 0,
    }));
  }
  // Recent: every conversation, newest first, not grouped by agent. Drawn in the list column in place of the agent rows.
  const recentRows = () => {
    const unread = (id) => (id === activeId || !window.snUnreadOf ? 0 : window.snUnreadOf(id));
    const q = query.trim().toLowerCase();
    return desks.slice().sort((p, x) => _deskWhen(x).localeCompare(_deskWhen(p))).map((d) => {
      const slug = deskAgent(d);
      let last = ((_deskMeta[d.id] || {}).lastMsg || d.last_message || '').replace(/\s+/g, ' ').trim();
      // a colleague's relayed answer reads "Nora: ...", not "Answer from nora: ..."
      const by = M.relayAuthor(last);
      if (by) last = nameOf(by) + ': ' + last.replace(/^\s*Answer from [^:]+:\s*/, '');
      return { d, slug, last, unread: unread(d.id), working: deskIsWorking(d.id) };
    }).filter((r) => !q || [r.d.title, r.last, nameOf(r.slug)].some((v) => String(v || '').toLowerCase().includes(q))).slice(0, 80);
  };
  function paintRecent() {
    let box = tabsEl.querySelector(':scope > .cs-rec');
    if (filter !== 'recent' || !listOn()) { if (box) box.remove(); return; }
    if (!box) {
      box = document.createElement('div');
      box.className = 'cs-rec';
      box.addEventListener('click', (e) => {
        // the row's "..." opens Rename, Copy link and Delete (app.js openChatRowMenu)
        const more = e.target.closest('[data-more]');
        if (more) { e.stopPropagation(); return openChatRowMenu(more, more.dataset.more); }
        const b = e.target.closest('[data-open]'); if (b) openDesk(b.dataset.open);
      });
    }
    if (box.parentElement !== tabsEl || tabsEl.lastElementChild !== box) tabsEl.appendChild(box);
    const html = recentRows().map((r) => `<button type="button" class="cs-rec__row${r.d.id === activeId ? ' is-cur' : ''}" data-open="${esc(r.d.id)}">`
      + `${deskIsSystem(r.d) ? sysAv('cs-rec__av') : avHtml(r.slug, 'cs-rec__av')}<span class="cs-rec__t"><b>${esc(r.d.title || 'Chat')}</b>`
      // the avatar says whose chat it is; a name here read as part of the message
      + `<small>${esc(r.last)}</small></span>`
      + `<span class="cs-rec__m"><span class="cs-rec__when"><time>${esc(relTime(_deskWhen(r.d)))}</time><span class="cs-rec__more" data-more="${esc(r.d.id)}" title="More" aria-label="More">&middot;&middot;&middot;</span></span>${r.unread ? `<b class="sn-unread">${M.badgeText(r.unread)}</b>` : r.working ? '<i class="cs-rec__live"></i>' : ''}</span></button>`).join('')
      || `<div class="cs-empty" style="display:block">${query ? 'No chat matches "' + esc(query.trim()) + '"' : 'No chats yet. Press New chat to start one.'}</div>`;
    if (box._html !== html) { box.innerHTML = html; box._html = html; }
  }
  function applyFilter() {
    const rows = rowsFromTabs();
    const recent = filter === 'recent';
    const keep = new Set(M.filterRows(rows, { query, filter: recent ? 'all' : filter }).map((r) => r.el));
    for (const r of rows) r.el.hidden = listOn() && (recent || !keep.has(r.el));
    const n = M.rowCounts(rows);
    // the Unread chip counts messages, as the rows' badges and the Chats icon do
    // (it counted agents: "2" beside badges of 1 and 5)
    const unreadMsgs = rows.reduce((s, r) => s + (r.unread || 0), 0);
    const chips = head.querySelector('.cs-chips');
    const want = [['all', 'All', ''], ['recent', 'Recent', ''], ['unread', 'Unread', unreadMsgs], ['working', 'Working', n.working]];
    const html = want.map(([k, l, c]) => `<button type="button" role="tab" aria-selected="${filter === k}" class="cs-chip${filter === k ? ' is-on' : ''}" data-f="${k}">${l}${c ? `<b class="sn-unread cs-chip__n">${c}</b>` : ''}</button>`).join('');
    if (chips.innerHTML !== html) {
      chips.innerHTML = html;
      chips.querySelectorAll('[data-f]').forEach((b) => b.addEventListener('click', () => {
        filter = b.dataset.f;
        try { localStorage.setItem(LS_FILTER, filter); } catch {}
        applyFilter();
      }));
    }
    paintRecent();
    if (recent) { if (empty.isConnected) empty.remove(); return; }
    const none = listOn() && rows.length && !keep.size;
    empty.textContent = query ? 'No chat matches "' + query.trim() + '"'
      : filter === 'unread' ? 'Nothing unread' : 'No chat is working now';
    if (none && !empty.isConnected) tabsEl.appendChild(empty);
    if (!none && empty.isConnected) empty.remove();
  }

  // ---- the chat header ----
  const chead = document.createElement('div');
  chead.className = 'cs-chead';
  chead.innerHTML = '<span class="cs-chead__av"></span><div class="cs-chead__who" role="button" tabindex="0" title="Show the panel">'
    + '<div class="cs-chead__nm"></div><div class="cs-chead__sub"></div></div>'
    + `<button type="button" class="cs-chats"></button><span class="cs-chead__mode"></span>`
    + `<button type="button" class="sn-btn--bare cs-ibtn" title="Show or hide the panel" aria-label="Show or hide the panel">${I_INFO}</button>`;
  task.insertBefore(chead, task.firstChild);
  chead.querySelector('.cs-ibtn').addEventListener('click', () => setInfo(!infoOpen));
  chead.querySelector('.cs-chead__who').addEventListener('click', () => setInfo(!infoOpen));
  chead.querySelector('.cs-chats').addEventListener('click', (e) => {
    const d = curDesk();
    const slug = d && deskAgent(d);
    if (slug) { e.stopPropagation(); openAgentTabMenu(e.currentTarget, slug); }
  });
  function paintHeader() {
    const d = curDesk();
    chead.hidden = !d;
    // nobody to answer in the System chat
    const composer = document.getElementById('composer');
    if (composer) composer.hidden = deskIsSystem(d);
    chead.querySelector('.cs-chead__mode').hidden = deskIsSystem(d);
    if (!d) return;
    const slug = deskAgent(d);
    const avBox = chead.querySelector('.cs-chead__av');
    const nm = chead.querySelector('.cs-chead__nm');
    const sub = chead.querySelector('.cs-chead__sub');
    let avH, nmT, subH;
    if (members.length > 1) {
      avH = `<span class="cs-stack">${members.slice(0, 3).map((m) => avHtml(m, statusOf(m).key === 'working' ? 'is-working' : '')).join('')}</span>`;
      nmT = M.membersTitle(members, nameOf);
      const busy = members.filter((m) => statusOf(m).key === 'working').map(nameOf);
      subH = `${esc(nameOf(slug))}'s chat &middot; ${members.length} agents`
        + (busy.length ? ` &middot; <em>${esc(busy.join(', '))} working</em>` : '');
    } else if (deskIsSystem(d)) {
      avH = sysAv();
      nmT = d.title;
      subH = '';
    } else if (slug) {
      const st = statusOf(slug);
      const a = agentBySlug(slug);
      avH = avHtml(slug, st.key === 'working' ? 'is-working' : '');
      nmT = nameOf(slug);
      subH = esc((a && a.role) || '') + (st.label ? ` &middot; <em class="cs-st--${esc(st.key)}">${esc(st.label)}</em>` : '');
    } else {
      avH = '';
      nmT = d.title || 'Chat';
      subH = '';
    }
    if (avBox.innerHTML !== avH) avBox.innerHTML = avH;
    if (nm.textContent !== nmT) nm.textContent = nmT;
    if (sub.innerHTML !== subH) sub.innerHTML = subH;
    const btn = chead.querySelector('.cs-chats');
    btn.hidden = !slug;
    if (slug) {
      const n = desks.filter((x) => deskAgent(x) === slug).length;
      const html = `<span class="cs-chats__t">${esc(d.title || 'Chat')}</span>${n > 1 ? `<span class="cs-chats__n">${n} chats</span>` : ''}${I_DOWN}`;
      if (btn.innerHTML !== html) btn.innerHTML = html;
      btn.title = 'Chats with ' + nameOf(slug);
    }
    // The Chat/Canvas switch lives in the header on a desktop; renderTabs puts it
    // back into the tabs row, so it is moved after each render.
    const seg = typeof _modeSeg !== 'undefined' ? _modeSeg : null;
    const box = chead.querySelector('.cs-chead__mode');
    if (seg && on() && seg.parentElement !== box) box.appendChild(seg);
  }

  // ---- who takes part: from the thread ----
  function readMembers() {
    const d = curDesk();
    const authors = [];
    for (const el of thread.querySelectorAll('.sn-msg--agent')) {
      const a = el.dataset.author || M.relayAuthor(el._raw != null ? el._raw : el.textContent);
      if (a) authors.push(a);
    }
    const next = d ? M.chatMembers(deskAgent(d), authors, isAgent) : [];
    if (next.join() === members.join()) return false;
    members = next;
    if (card && !members.includes(card)) card = null;
    return true;
  }
  let _mq = false;
  new MutationObserver(() => {
    if (_mq) return;
    _mq = true;
    requestAnimationFrame(() => { _mq = false; if (readMembers()) { paintHeader(); paintInfo(); } });
  }).observe(thread, { childList: true });

  // ---- the panel ----
  const info = document.createElement('aside');
  info.className = 'cs-info';
  info.setAttribute('aria-label', 'About this chat');
  info.innerHTML = '<button type="button" class="cs-info__x" title="Hide the panel" aria-label="Hide the panel">&times;</button><div class="cs-info__in"></div>';
  main.appendChild(info);
  info.querySelector('.cs-info__x').addEventListener('click', () => setInfo(false));
  const body = info.querySelector('.cs-info__in');
  function setInfo(v) {
    infoOpen = !!v;
    localStorage.setItem(LS_INFO, infoOpen ? '1' : '0');
    apply();
    if (infoOpen) { loadStates(); paintInfo(); }
  }
  async function loadTasks(force) {
    if (!force && tasks && Date.now() - tasksAt < 60000) return;
    tasksAt = Date.now();
    try {
      const r = await tasksFetch('/api/tasks');   // asked only where the tasks module is (app.js)
      if (!r.ok) throw new Error('http ' + r.status);
      const all = await r.json();
      tasks = Array.isArray(all) ? all : (all.tasks || []);
    } catch { tasks = tasks || []; }
    paintInfo();
  }
  async function loadStates() {
    if (!on() || document.hidden) return;
    try {
      const r = await fetch('/api/agents/state', { headers: { Accept: 'application/json' } });
      if (!r.ok) return;
      const j = await r.json();
      const next = {};
      for (const s of (j.agents || [])) next[s.slug] = s;
      states = next;
      paintHeader();
      paintInfo();
    } catch {}
  }
  // no poll: an agent's state changes arrive on the live stream (app.js)
  document.addEventListener('sn:event', (e) => { if (String((e.detail || {}).type).startsWith('agent.')) loadStates(); });
  document.addEventListener('sn:resync', loadStates);
  document.addEventListener('sn-deskwork', () => paintHeader());   // a chat starts or stops working
  function taskRows(list) {
    if (!tasks) return window.snSkelRows ? window.snSkelRows(3, false) : '';
    if (!list.length) return '<div class="cs-muted">No open tasks</div>';
    return list.map((t) => `<button type="button" class="cs-li" data-task="${esc(t.id)}"><span class="cs-li__t">#${esc(t.id)} ${esc(t.title || '')}</span>`
      + `<span class="cs-li__m">${esc(t.assignee && members.length > 1 ? nameOf(t.assignee) : (t.state || t.status || ''))}</span></button>`).join('');
  }
  function agentCard(s) {
    const a = agentBySlug(s) || {};
    const st = statusOf(s);
    const mine = desks.filter((x) => deskAgent(x) === s)
      .sort((p, q) => String(_deskWhen(q)).localeCompare(String(_deskWhen(p)))).slice(0, 5);
    loadNow(s);
    // the agent's page behind a gear in the card's corner,
    // not a link at the bottom
    return (members.length > 1 ? `<button type="button" class="cs-back" data-back>${I_BACK}Everyone in this chat</button>` : '')
      + `<div class="cs-card"><button type="button" class="cs-gear" data-agent="${esc(s)}" title="${esc(a.title || s)}'s page" aria-label="${esc(a.title || s)}'s page">${I_GEAR}</button>`
      + `${avHtml(s, 'cs-av--big' + (st.key === 'working' ? ' is-working' : ''))}`
      + `<div class="cs-card__nm">${esc(a.title || s)}</div><div class="cs-card__role">${esc(a.role || '')}</div></div>`
      + `<div class="cs-blk"><h4>Now</h4>${nowHtml(s, st)}</div>`
      + `<div class="cs-blk"><h4>Waiting for ${esc(a.title || s)}</h4>${queueHtml(s)}</div>`
      // tasks come with the planner module; without it the block led nowhere
      + (window.snInstalled && !window.snInstalled.has('tracking') ? '' : `<div class="cs-blk"><h4>Open tasks</h4>${taskRows(M.openTasks(tasks, s))}</div>`)
      + `<div class="cs-blk"><h4>Chats with ${esc(a.title || s)}</h4>${mine.map((x) => `<button type="button" class="cs-li${x.id === activeId ? ' is-cur' : ''}" data-open="${esc(x.id)}"><span class="cs-li__t">${esc(x.title || 'Chat')}</span><span class="cs-li__m">${esc(relTime(_deskWhen(x)) || '')}</span></button>`).join('') || '<div class="cs-muted">none</div>'}</div>`;
  }
  // "Now" and "Waiting for" (from /next): what the agent is doing (its turn in
  // flight, from the gateway's console) and what waits for it (core-api /queue,
  // chats first, "seen" once a wake took it). Read for the card on screen, at
  // most every 3 s.
  const NOW = {};
  async function loadNow(s) {
    const n = NOW[s];
    if (n && (n.loading || Date.now() - n.at < 3000)) return;
    NOW[s] = { ...(n || {}), loading: true, at: Date.now() };
    const get = (p) => fetch(p, { headers: { Accept: 'application/json' } }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const [con, q] = await Promise.all([get(`/api/agents/${encodeURIComponent(s)}/console`), get(`/api/agents/${encodeURIComponent(s)}/queue`)]);
    NOW[s] = { con, queue: (q && q.waiting) || [], at: Date.now(), loading: false };
    paintInfo();
  }
  function nowHtml(s, st) {
    const n = NOW[s] || {};
    const turn = ((n.con || {}).turns || [])[0];
    if (!turn) return st.key === 'working' ? '<div class="cs-now cs-st--working">working</div>' : '<div class="cs-muted">Free</div>';
    const conv = String(turn.conversation || ''), deskId = conv.includes(':desk:') ? conv.split(':desk:')[1] : '';
    const d = deskId && desks.find((x) => x.id === deskId);
    // a wake for a chat runs outside the chat; the message it took is the seen one
    const taken = (n.queue || []).find((x) => x.seen && x.what === 'chat');
    const where = deskId ? (d ? `in “${d.title}”` : 'in a chat') : taken ? `in “${taken.chat || 'a chat'}”` : 'on its messages';
    const step = (turn.steps || []).slice(-1)[0];
    const doing = turn.action ? doingWords(turn.action + (turn.detail ? ': ' + turn.detail : '')) : step ? plainStep(step.text) : 'Thinking';
    return `<div class="cs-now cs-st--working">Working ${esc(where)}</div><div class="cs-muted">${esc(doing)}</div>`;
  }
  function queueHtml(s) {
    const items = ((NOW[s] || {}).queue || []).slice().sort((x, y) => ((y.what === 'chat') - (x.what === 'chat')) || (x.id - y.id));
    if (!items.length) return '<div class="cs-muted">Nothing</div>';
    const label = (x) => x.what === 'chat' ? `Chat “${x.chat || 'a chat'}”` : x.what === 'reminder' ? 'A reminder'
      : x.what === 'message' ? `Message from ${nameOf(x.from)}` : `${x.what} from ${x.from}`;
    return items.slice(0, 6).map((x, i) => `<button type="button" class="cs-li"${x.desk ? ` data-open="${esc(x.desk)}"` : ' disabled'}>`
      + `<span class="cs-li__t">${i + 1}. ${esc(label(x))}</span><span class="cs-li__m">${x.seen ? 'seen' : ''}</span></button>`).join('')
      + (items.length > 6 ? `<div class="cs-muted">and ${items.length - 6} more</div>` : '');
  }
  function membersView() {
    const d = curDesk();
    const all = members.flatMap((m) => M.openTasks(tasks, m, 3));
    return `<div class="cs-card cs-card--left"><div class="cs-card__nm">${esc((d && d.title) || 'Chat')}</div>`
      + `<div class="cs-card__role">${esc(nameOf(members[0]))}'s chat &middot; ${members.length} agents</div></div>`
      + `<div class="cs-blk"><h4>In this chat</h4>${members.map((m, i) => {
        const st = statusOf(m);
        const a = agentBySlug(m) || {};
        return `<button type="button" class="cs-mem" data-m="${esc(m)}">${avHtml(m, st.key === 'working' ? 'is-working' : '')}`
          + `<span class="cs-mem__b"><span class="cs-mem__nm">${esc(a.title || m)}</span>`
          + `<span class="cs-mem__sub">${st.key === 'working' ? '<em>working</em>' : esc(a.role || st.label || '')}</span></span>`
          + `<span class="cs-mem__tag">${i === 0 ? 'chat agent' : 'joined by @'}</span></button>`;
      }).join('')}</div>`
      + (window.snInstalled && !window.snInstalled.has('tracking') ? '' : `<div class="cs-blk"><h4>Open tasks of the people here</h4>${taskRows(all)}</div>`);
  }
  function paintInfo() {
    if (!on() || !infoOpen) return;
    const d = curDesk();
    let html;
    if (!d) html = '<div class="cs-muted">No chat open</div>';
    else if (members.length > 1 && !card) html = membersView();
    else html = agentCard(card || deskAgent(d) || members[0]);
    if (body.innerHTML !== html) body.innerHTML = html;
    loadTasks();
  }
  body.addEventListener('click', (e) => {
    const t = e.target.closest('[data-m], [data-back], [data-open], [data-task], [data-agent]');
    if (!t) return;
    if (t.dataset.m) { card = t.dataset.m; paintInfo(); }
    else if (t.hasAttribute('data-back')) { card = null; paintInfo(); }
    else if (t.dataset.open) openDesk(t.dataset.open);
    else if (t.dataset.task) openTask(t.dataset.task);
    else if (t.dataset.agent) openAgentPage(t.dataset.agent);
  });
  // A name over a run of messages, or its avatar, opens that agent's card.
  thread.addEventListener('click', (e) => {
    if (!on()) return;
    const who = e.target.closest('.sn-who');
    let slug = who && who.dataset.who;
    if (!slug) {
      const m = e.target.closest('.sn-msg--agent[data-av-for]');
      if (m && e.offsetX < 0 && e.target === m) slug = m.dataset.avFor;
    }
    if (!slug || !isAgent(slug)) return;
    const d = curDesk();
    card = members.length > 1 || (d && deskAgent(d) !== slug) ? slug : null;
    setInfo(true);
  });

  // the card's "Now" and "Waiting for" follow the agent: loadStates repaints it on each change

  // ---- wiring ----
  function apply() {
    const shell = on();
    app.classList.toggle('cs', shell);
    app.classList.toggle('cs-info-on', shell && infoOpen && !!activeId);
    if (!shell) for (const el of tabsEl.querySelectorAll(':scope > .tab[hidden]')) el.hidden = false;
  }
  {
    const _rt = renderTabs;
    renderTabs = function () {
      const r = _rt.apply(this, arguments);
      applyFilter();
      paintHeader();
      return r;
    };
  }
  {
    const _pw = paintDeskWork;
    paintDeskWork = function () {
      const r = _pw.apply(this, arguments);
      applyFilter();
      paintHeader();
      return r;
    };
  }
  {
    const _sv = setView;
    setView = function (v) {
      const r = _sv.apply(this, arguments);
      apply();
      return r;
    };
  }
  {
    const _sa = setActive;
    setActive = function (id) {
      if (id !== activeId) { card = null; members = []; }
      const r = _sa.apply(this, arguments);
      readMembers();
      apply();
      paintHeader();
      paintInfo();
      return r;
    };
  }
  _phone.addEventListener('change', () => { apply(); applyFilter(); paintHeader(); paintInfo(); });
  // the phone switches between its list and a chat with a class on .app
  new MutationObserver(applyFilter).observe(app, { attributes: true, attributeFilter: ['class'] });
  document.addEventListener('sn-unread', applyFilter);
  // The rail: the sidebar's collapsed state, set once for this layout; widening
  // it again with its own control is kept as the person's choice.
  if (!_phone.matches && !localStorage.getItem(LS_RAIL)) {
    localStorage.setItem(LS_RAIL, '1');
    if (typeof _sbSet === 'function' && !app.classList.contains('sb-collapsed')) _sbSet(true);
  }

  const css = document.createElement('style');
  css.textContent = `
    .cs-head, .cs-chead, .cs-info, .cs-empty { display: none; }
    /* the avatar circle, wide screen and phone (Recent rows) */
    .cs-av { position: relative; width: 38px; height: 38px; flex: 0 0 38px; border-radius: 50%; display: inline-grid;
      place-items: center; background: var(--sn-surface-2); border: 1px solid var(--sn-rule); font: 600 14px/1 var(--sn-font);
      color: var(--sn-ink-2); overflow: visible; }
    .cs-av .av-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; border-radius: 50%; }
    .cs-av:has(> .av-img) { color: transparent; border-color: transparent; }
    /* search and chips: the desktop list and the phone's list screen */
    .cs-search { display: flex; align-items: center; gap: 8px; height: 36px; padding: 0 11px; border-radius: 10px;
      background: var(--sn-surface); border: 1px solid var(--sn-rule); color: var(--sn-ink-mute); }
    .cs-search input { flex: 1; min-width: 0; border: 0; outline: 0; background: none; color: var(--sn-ink); font: inherit; font-size: 13.5px; }
    .cs-chips { display: flex; gap: 6px; margin-top: 12px; }
    .cs-chip { position: relative; border: 1px solid var(--sn-rule); background: none; color: var(--sn-ink-mute); border-radius: var(--sn-r-sm);
      padding: 3px 11px; font: 400 12.5px/1.5 var(--sn-font); cursor: pointer; }
    .cs-chip.is-on { background: var(--sn-surface-2); color: var(--sn-ink); }
    /* the count as a badge on the chip's corner, as on an app icon; inside the
       label it read as part of the word */
    .cs-chip__n { position: absolute; top: -7px; right: -6px; min-width: 16px; height: 16px; padding: 0 4px;
      font-size: 10px; line-height: 16px; }
    @media (min-width: 761px) {
      /* The rail: the collapsed sidebar keeps the section icons (Studio and
         Planner), their names as tooltips; projects and agents stay folded. */
      .app.sb-collapsed .sb__list:has(> .agent[id^="nav"]) { display: flex; flex-direction: column;
        align-items: center; gap: 2px; padding: 0; }
      .app.sb-collapsed .sb__list > .agent[id^="nav"] { width: 40px; height: 40px; padding: 0; margin: 0;
        justify-content: center; border-radius: 10px; }
      .app.sb-collapsed .sb__list > .agent[id^="nav"] > span { display: none; }
      .app.sb-collapsed .sb__group:has(> .sb__list > .agent[id^="nav"]) + .sb__group:has(> .sb__list > .agent[id^="nav"]) {
        margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--sn-rule); }
      .app.cs .main { display: grid; grid-template-columns: 320px minmax(0, 1fr) var(--cs-info, 0px);
        grid-template-rows: auto minmax(0, 1fr); }
      .app.cs.cs-info-on .main { --cs-info: 300px; }
      .app.cs .cs-head { display: block; grid-column: 1; grid-row: 1; padding: 14px 14px 8px;
        border-right: 1px solid var(--sn-rule); background: var(--sn-bg); }
      .cs-head__row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
      .cs-head__t { flex: 1; font-size: 19px; font-weight: 650; color: var(--sn-ink); }
      .cs-new { width: 100%; margin-bottom: 10px; }
      .app.cs .main__top #tabs { grid-column: 1; grid-row: 2; display: flex; flex-direction: column; align-items: stretch;
        height: auto; min-height: 0; gap: 2px; padding: 2px 8px 16px; overflow-x: hidden; overflow-y: auto;
        border-bottom: 0; border-right: 1px solid var(--sn-rule); }
      .app.cs #tabs > .tab-add { display: none; }
      .app.cs #tabs > .tab { width: auto; max-width: none; flex: 0 0 auto; min-height: 64px; gap: 12px;
        padding: 9px 10px; border: 0; border-radius: var(--sn-r-sm); background: transparent; box-shadow: none; }
      .app.cs #tabs > .tab::before { display: none; }
      .app.cs #tabs > .tab:hover { background: var(--sn-surface); }
      .app.cs #tabs > .tab.is-active { background: var(--sn-surface-2); }
      .app.cs #tabs > .tab[hidden] { display: none; }
      .app.cs #tabs > .tab .tab__av { width: 46px; height: 46px; flex: 0 0 46px; border-radius: 50%; font-size: 18px; }
      .app.cs #tabs > .tab .tab__ic .lucide { width: 20px; height: 20px; }
      .app.cs .tab__rows { gap: 3px; }
      .app.cs .tab--agent .tab__nm { font-size: 14.5px; }
      .app.cs .tab__title { font-size: 12.5px; }
      .app.cs .m-last { display: block; min-width: 0; font-size: 13px; line-height: 1.3; color: var(--sn-ink-mute);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .app.cs .tab:has(.m-last) .tab__row--bot { display: none; }
      .app.cs .tab__age { font-size: 11.5px; }
      .app.cs .cs-empty { display: block; padding: 18px 10px; color: var(--sn-ink-mute); font-size: 13px; text-align: center; }
      .app.cs #task { grid-column: 2; grid-row: 1 / span 2; min-width: 0; min-height: 0; }
      .app.cs .cs-chead { display: flex; align-items: center; gap: 12px; flex: 0 0 auto; min-height: 60px;
        padding: 8px 14px 8px 18px; border-bottom: 1px solid var(--sn-rule); }
      .app.cs .cs-chead[hidden] { display: none; }
      .cs-chead__who { flex: 1; min-width: 0; cursor: pointer; }
      .cs-chead__nm { font-weight: 650; font-size: 15px; color: var(--sn-ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .cs-chead__sub { color: var(--sn-ink-mute); font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .cs-chead__sub em, .cs-mem__sub em { font-style: normal; color: var(--sn-ink-2); }
      .cs-chead__sub em.cs-st--working, .cs-chead__sub em:not([class]) { color: #8fa3c4; }
      .cs-chats { display: flex; align-items: center; gap: 6px; max-width: 40%; padding: 5px 10px; border-radius: var(--sn-r-sm);
        border: 1px solid var(--sn-rule); background: var(--sn-surface); color: var(--sn-ink-2); cursor: pointer; font: 400 12.5px/1.3 var(--sn-font); }
      .cs-chats[hidden] { display: none; }
      .cs-chats__t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .cs-chats__n { color: var(--sn-ink-faint); white-space: nowrap; }
      .cs-chead__mode:empty { display: none; }
      .cs-chead__mode #mode { margin: 0; }
      .app.cs-info-on .cs-ibtn { color: var(--sn-ink); }
      .cs-av.is-working::after { content: ''; position: absolute; inset: -3px; border-radius: 50%;
        border: 2px solid var(--sn-ink-faint); animation: sn-pulse var(--sn-t-amb, 1.6s) infinite; }
      .cs-stack { display: flex; }
      .cs-stack .cs-av { width: 30px; height: 30px; flex-basis: 30px; box-shadow: 0 0 0 2px var(--sn-bg); }
      .cs-stack .cs-av + .cs-av { margin-left: -10px; }
      .app.cs.cs-info-on .cs-info { display: block; grid-column: 3; grid-row: 1 / span 2; position: relative;
        min-height: 0; overflow-y: auto; border-left: 1px solid var(--sn-rule); background: var(--sn-bg-2); }
      .cs-info__x { position: absolute; top: 10px; right: 10px; z-index: 1; width: 30px; height: 30px; border: 0;
        border-radius: var(--sn-r-sm); background: none; color: var(--sn-ink-mute); font-size: 20px; line-height: 1; cursor: pointer; }
      .cs-info__x:hover { color: var(--sn-ink); background: var(--sn-surface); }
      .cs-info__in { padding: 20px 18px 28px; }
      .cs-card { text-align: center; margin-bottom: 14px; }
      .cs-card--left { text-align: left; padding-right: 30px; }
      .cs-card--left .cs-card__nm { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .cs-av--big { width: 84px; height: 84px; flex-basis: 84px; font-size: 28px; margin-bottom: 10px; }
      .cs-card__nm { font-size: 17px; font-weight: 650; color: var(--sn-ink); }
      .cs-card__role { font-size: 13px; color: var(--sn-ink-mute); }
      .cs-blk { border-top: 1px solid var(--sn-rule); padding: 12px 0; font-size: 13px; color: var(--sn-ink-2); }
      .cs-blk h4 { margin: 0 0 6px; font: 600 11px/1.3 var(--sn-font); letter-spacing: .08em; text-transform: uppercase; color: var(--sn-ink-faint); }
      .cs-now.cs-st--working { color: #8fa3c4; }
      .cs-li { display: flex; justify-content: space-between; gap: 10px; width: 100%; padding: 5px 6px; margin: 0 -6px;
        border: 0; border-radius: 7px; background: none; color: var(--sn-ink-2); font: 400 13px/1.35 var(--sn-font);
        text-align: left; cursor: pointer; box-sizing: content-box; }
      .cs-li:hover { background: var(--sn-surface); color: var(--sn-ink); }
      .cs-li.is-cur { color: var(--sn-ink); }
      .cs-li__t { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .cs-li__m { flex: 0 0 auto; color: var(--sn-ink-faint); font-size: 12px; }
      .cs-muted { color: var(--sn-ink-faint); font-size: 12.5px; }
      .cs-back, .cs-link { border: 0; background: none; padding: 0; color: var(--sn-ink-mute); font: 400 12.5px/1.4 var(--sn-font); cursor: pointer; }
      .cs-back { margin-bottom: 12px; }
      .cs-back:hover, .cs-link:hover { color: var(--sn-ink); }
      .cs-mem { display: flex; align-items: center; gap: 10px; width: 100%; padding: 7px 6px; margin: 0 -6px;
        border: 0; border-radius: 10px; background: none; text-align: left; cursor: pointer; box-sizing: content-box; }
      .cs-mem:hover { background: var(--sn-surface); }
      .cs-mem__b { flex: 1; min-width: 0; display: flex; flex-direction: column; }
      .cs-mem__nm { font: 600 13.5px/1.3 var(--sn-font); color: var(--sn-ink); }
      .cs-mem__sub { font-size: 12px; color: var(--sn-ink-mute); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .cs-mem__tag { flex: 0 0 auto; font-size: 11px; color: var(--sn-ink-faint); }
      .app.cs .sn-who { cursor: pointer; }
      .app.cs .sn-who:hover { color: var(--sn-ink); }
    }`;
  document.head.appendChild(css);
  readMembers();
  apply();
  applyFilter();
  paintHeader();
})();
