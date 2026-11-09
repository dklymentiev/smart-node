// chat-shell-model.js: the decisions behind the desktop chat shell
// (chat-shell.js), kept free of the DOM so they are tested on their own
// (test/chat-shell-model.test.mjs). A classic script: it defines
// window.SnChatShell in the browser and is read with node:vm in the tests.
(function (root) {
  'use strict';

  // A colleague's answer relayed into a chat starts "Answer from <slug>:".
  const RELAY = /^\s*Answer from ([A-Za-z0-9][A-Za-z0-9._-]{0,63}):/;

  function relayAuthor(text) {
    const m = RELAY.exec(String(text || ''));
    return m ? m[1].toLowerCase() : null;
  }

  // Who takes part in a chat: the chat's own agent first, then every colleague
  // who wrote into it or is still asked, in order of first appearance. Unknown
  // slugs (not agents on this node) are left out.
  function chatMembers(deskAgent, authors, isAgent) {
    const ok = typeof isAgent === 'function' ? isAgent : () => true;
    const out = [];
    const add = (s) => {
      const slug = String(s || '').toLowerCase();
      if (slug && !out.includes(slug) && ok(slug)) out.push(slug);
    };
    add(deskAgent);
    for (const a of authors || []) add(a);
    return out;
  }

  // The list column: a query matches the agent's name, the chat title or the last
  // message, case-insensitive; the filter is 'all', 'working' or 'unread'.
  function filterRows(rows, opts) {
    const q = String((opts && opts.query) || '').trim().toLowerCase();
    const f = (opts && opts.filter) || 'all';
    return (rows || []).filter((r) => {
      if (f === 'working' && !r.working) return false;
      if (f === 'unread' && !(r.unread > 0)) return false;
      if (!q) return true;
      return [r.name, r.title, r.last].some((v) => String(v || '').toLowerCase().includes(q));
    });
  }

  // The counts on the filter chips.
  function rowCounts(rows) {
    const list = rows || [];
    return {
      all: list.length,
      working: list.filter((r) => r.working).length,
      unread: list.filter((r) => r.unread > 0).length,
    };
  }

  // An agent's runtime row (GET /api/agents/state) as a word for people.
  function agentStatus(state) {
    if (!state) return { key: 'unknown', label: '' };
    if (state.stale) return { key: 'stale', label: 'not responding' };
    if (state.status === 'busy') return { key: 'working', label: 'working' };
    if (state.status === 'free') return { key: 'free', label: 'free' };
    return { key: String(state.status || 'unknown'), label: String(state.status || '') };
  }

  // The open tasks of one agent, the ones in progress first, then the newest.
  const CLOSED = new Set(['done', 'rejected', 'cancelled', 'closed']);
  function openTasks(tasks, slug, limit) {
    const n = limit == null ? 6 : limit;
    const mine = (tasks || []).filter((t) => t && t.assignee === slug
      && !CLOSED.has(String(t.state || '')) && !CLOSED.has(String(t.status || '')));
    const rank = (t) => (t.state === 'in_progress' || t.stage === 'in_progress' ? 0 : 1);
    mine.sort((a, b) => rank(a) - rank(b) || String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    return mine.slice(0, n);
  }

  // The chat header's title: one agent by name; several as "John, Ann and Max",
  // more than three as "John, Ann and 3 more".
  function membersTitle(members, nameOf) {
    const names = (members || []).map((m) => (typeof nameOf === 'function' ? nameOf(m) : m));
    if (names.length <= 1) return names.join('');
    if (names.length <= 3) return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
    return names.slice(0, 2).join(', ') + ' and ' + (names.length - 2) + ' more';
  }

  // ---- unread counts (chat-unread.js) ----

  // The badge: nothing for 0, the number up to 99, then 99+.
  function badgeText(n) {
    const k = Math.max(0, Math.floor(Number(n) || 0));
    if (!k) return '';
    return k > 99 ? '99+' : String(k);
  }

  // A row's count: the unread of the chats it stands for (an agent's row stands
  // for its open chats), less the chat being read on screen.
  function rowUnread(ids, unread, reading) {
    let n = 0;
    for (const id of ids || []) {
      if (id !== reading) n += Math.max(0, Math.floor(Number((unread || {})[id]) || 0));
    }
    return n;
  }

  // The page title with the total in front, "(3) Studio". A count already there
  // is replaced; none is shown for 0.
  function titleWithCount(title, n) {
    const base = String(title || '').replace(/^\(\d+\+?\)\s*/, '');
    const b = badgeText(n);
    return b ? '(' + b + ') ' + base : base;
  }

  // The read mark to send for the chat on screen (GET /api/desks/unread row):
  // the time of its newest message from others, when it has something new or has
  // never been marked, and that time was not sent already. Null: nothing to send.
  function readMark(row, sent) {
    if (!row || !row.last_at) return null;
    if (!(row.unread > 0) && row.read_upto) return null;
    return row.last_at === sent ? null : row.last_at;
  }

  // Whether the newest message makes a row newer than the list knows it: the
  // list is read once at load, the counts every few seconds.
  function newerThan(at, known) {
    const a = Date.parse(at || '');
    if (!Number.isFinite(a)) return false;
    const k = Date.parse(known || '');
    return !Number.isFinite(k) || a > k;
  }

  const M = {
    relayAuthor, chatMembers, filterRows, rowCounts, agentStatus, openTasks, membersTitle,
    badgeText, rowUnread, titleWithCount, readMark, newerThan,
  };
  root.SnChatShell = M;
})(typeof window !== 'undefined' ? window : globalThis);
