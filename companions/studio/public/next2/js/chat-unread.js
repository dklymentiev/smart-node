// chat-unread.js: what is new in each chat since it was read. Loaded after app.js, phone-chats.js and chat-shell.js; uses
// app.js's top-level names and wraps renderTabs, setActive and
// openAgentTabMenu. The decisions are in chat-shell-model.js.
//
//  - GET /api/desks/unread every few seconds while the page is in view: for each
//    chat, how many messages from others came after its read mark, and the
//    newest of them;
//  - a badge on each chat row (an agent's row adds up its open chats), on the
//    chats in an agent's menu, on the Chats icon, and the total in the page
//    title, "(3) Studio";
//  - the newest message also brings the row's last line and its place in the
//    list up to date: the list itself is read once, when the page loads;
//  - the chat on screen in a focused window is marked read up to its newest
//    message (PATCH read_upto). The mark is kept on the server, so the phone
//    and the computer agree;
//  - chat-shell.js reads the row counts for its Unread filter ('sn-unread').
(() => {
  const M = window.SnChatShell;
  const tabsEl = document.getElementById('tabs');
  const thread = document.getElementById('thread');
  if (!M || !M.badgeText || !tabsEl || !thread || typeof renderTabs !== 'function') return;

  let rows = {};        // desk id -> {unread, last_at, last_text, read_upto}
  const sent = {};      // desk id -> the read mark sent for it
  let busy = false;

  // The chat being read: its thread on screen, in a window that has the focus.
  const reading = () => (activeId && !document.hidden && document.hasFocus()
    && thread.getClientRects().length > 0 ? activeId : null);
  const counts = () => {
    const out = {};
    for (const id in rows) out[id] = rows[id].unread;
    return out;
  };
  // One chat's count, for the agent's chats menu (app.js openAgentTabMenu).
  window.snUnreadOf = (id) => Math.max(0, Math.floor(Number((rows[id] || {}).unread) || 0));
  // The chats a row stands for: an agent's row, every open chat of that agent.
  function idsOf(el) {
    const slug = el.dataset.agent;
    if (!slug) return [el.dataset.id];
    return desks.filter((d) => deskAgent(d) === slug && (tabOpen(d) || d.id === activeId || window.snUnreadOf(d.id) > 0)).map((d) => d.id);
  }
  // A <b>, not a <span>: the rail hides the spans of the section icons.
  function badge(host, n, cls, before) {
    let b = host.querySelector(':scope > .sn-unread');
    if (!n) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement('b');
      b.className = 'sn-unread' + (cls ? ' ' + cls : '');
      host.insertBefore(b, before || null);
    }
    b.textContent = M.badgeText(n);
  }

  function paint() {
    const r = reading();
    const map = counts();
    // every chat counts, as the Recent list does, not only the ones whose row is on screen
    let total = 0;
    for (const id in map) total += M.rowUnread([id], map, r);
    for (const el of tabsEl.querySelectorAll(':scope > .tab[data-id]')) {
      const n = M.rowUnread(idsOf(el), map, r);
      el.dataset.unread = String(n);
      el.classList.toggle('has-unread', n > 0);
      const meta = el.querySelector('.tab__meta');
      badge(meta || el, n, '', meta && meta.querySelector(':scope > .tab__chev'));
    }
    const nav = document.getElementById('navChats');
    if (nav) badge(nav, total, 'sn-unread--nav');
    const t = M.titleWithCount(document.title, total);
    if (t !== document.title) document.title = t;
    document.dispatchEvent(new CustomEvent('sn-unread'));
  }

  // The row's last line, as app.js writes it from the history.
  const lastLine = (s) => String((window.snHumanFailure && window.snHumanFailure(s)) || s || '').replace(/[*_`#>\[\]]/g, '').replace(/\s+/g, ' ').slice(0, 80).trim();

  async function poll() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const r = await fetch('/api/desks/unread', { headers: { Accept: 'application/json' } });
      if (!r.ok) return;
      const list = (await r.json()).desks || [];
      const next = {};
      let moved = false;
      // A chat the node opened itself (the System chat, a reminder's when its own was
      // deleted) is in no list yet: it would stay unseen until a reload.
      if (list.some((x) => !desks.some((k) => k.id === x.id))) {
        const all = await fetch('/api/desks', { headers: { Accept: 'application/json' } });
        if (all.ok) { desks = (await all.json()).desks || desks; moved = true; }
      }
      for (const x of list) {
        next[x.id] = x;
        // An answer that was computed before the mark we sent reached the server still counts the
        // chat as unread; if nothing newer than that mark exists, the chat stays read here.
        if (sent[x.id] && M.newerThan(sent[x.id], x.read_upto) && !M.newerThan(x.last_at, sent[x.id])) {
          next[x.id] = Object.assign({}, x, { unread: 0, read_upto: sent[x.id] });
        }
        const d = desks.find((k) => k.id === x.id);
        // news in a closed chat: its row has to be drawn now
        if (d && next[x.id].unread > 0 && !tabOpen(d) && d.id !== activeId && !((rows[x.id] || {}).unread > 0)) moved = true;
        if (d && M.newerThan(x.last_at, d.last_message_at)) {
          d.last_message_at = x.last_at;
          if (deskIsSystem(d)) delete d.metadata.tab_open;   // its news reopens a closed tab, as the server did
          if (x.last_text) _deskMeta[x.id] = Object.assign({}, _deskMeta[x.id], { lastMsg: lastLine(x.last_text), lastAt: String(x.last_at) });
          moved = true;
        }
      }
      rows = next;
      if (moved) renderTabs(); else paint();
      mark();
    } catch {
      // the next poll tries again
    } finally {
      busy = false;
    }
  }

  async function mark() {
    const id = reading();
    const at = id && M.readMark(rows[id], sent[id]);
    if (!at) return;
    sent[id] = at;
    rows[id] = Object.assign({}, rows[id], { unread: 0, read_upto: at });
    paint();
    try {
      const r = await fetch('/api/desks/' + encodeURIComponent(id), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ read_upto: at }),
      });
      if (!r.ok) throw new Error('http ' + r.status);
    } catch {
      delete sent[id];
    }
  }

  {
    const _rt = renderTabs;
    renderTabs = function () {
      const r = _rt.apply(this, arguments);
      paint();
      return r;
    };
  }
  {
    const _sa = setActive;
    setActive = function () {
      const r = _sa.apply(this, arguments);
      paint();
      mark();
      // At the moment of opening the thread is not drawn yet, so it does not count
      // as read; the poll used to pick that up within 8 s, and at 30 s a chat left
      // sooner came back unread. Mark again once it is drawn.
      setTimeout(mark, 400);
      return r;
    };
  }
  {
    const _menu = openAgentTabMenu;
    openAgentTabMenu = function () {
      const r = _menu.apply(this, arguments);
      const now = reading();
      for (const b of document.querySelectorAll('.pop [data-open]')) {
        badge(b, b.dataset.open === now ? 0 : ((rows[b.dataset.open] || {}).unread || 0), 'sn-unread--pop');
      }
      return r;
    };
  }
  const wake = () => { paint(); poll(); };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) wake(); });
  window.addEventListener('focus', wake);
  window.addEventListener('blur', paint);
  // no poll: the live stream says when a chat changes, and its (re)open reads all
  document.addEventListener('sn:resync', () => poll());
  // the live stream (app.js) says a chat changed: the counts now, not at the next poll
  document.addEventListener('sn:event', () => poll());
  window.addEventListener('focus', () => mark());   // back to the window: the chat on screen is read
  poll();

  const css = document.createElement('style');
  css.textContent = `
    /* the badge itself is the kit's .sn-unread (components.css) */
    .tab.has-unread .tab__nm, .tab.has-unread .m-last { color: var(--sn-ink); }
    /* time above; the badge beside the chats menu, where a messenger has it */
    .tab__meta:has(> .sn-unread) { display: grid; grid-template-columns: auto auto; justify-items: end;
      align-items: center; column-gap: 4px; row-gap: 4px; }
    .tab__meta:has(> .sn-unread) > .tab__age { grid-column: 1 / -1; }
    #navChats { position: relative; }
    /* at the row's end, where Mail keeps a count: over the icon it ran into the
       word ("3Chats"); on the collapsed rail, by the icon */
    .sn-unread--nav { position: absolute; top: 50%; right: 10px; transform: translateY(-50%);
      min-width: 16px; height: 16px; padding: 0 4px; font-size: 10px; line-height: 16px; pointer-events: none; }
    .app.sb-collapsed .sn-unread--nav { top: 2px; right: auto; left: 22px; transform: none; }
    .sn-unread--pop { margin-left: 8px; flex: 0 0 auto; }
  `;
  document.head.appendChild(css);
})();
