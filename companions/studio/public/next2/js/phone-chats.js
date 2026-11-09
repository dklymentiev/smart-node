// phone-chats.js: loaded after app.js, whose top-level names it uses and
// wraps (setView, renderTabs, syncChatUrl, openNewChatPicker).
//
// The Studio chats on a phone as in a messenger. Two
// screens, under the same 760px breakpoint as the phone shell (_phone):
//  - the list: every chat tab as a full-width row (avatar, name, last message,
//    time), the menu button and New chat above it; the address is "/";
//  - the chat: only that chat's tab is left in the top bar, as its header, with a
//    back arrow to the list in place of the menu button; the address /chats/<id>.
// A tap on a row (its avatar too) or a chat picked in a menu opens the chat; the
// back arrow, the system back gesture and the Chats item of the menu return to
// the list. Opening /chats/<id> directly lands in that chat. The desktop is not
// touched: both classes are set only while the phone breakpoint matches.
(() => {
  const app = document.querySelector('.app');
  const top = document.querySelector('.main__top');
  if (!app || !top || typeof _phone === 'undefined') return;
  let list = _phone.matches && snPath() === '/';
  let view = 'chats';
  function apply() {
    const on = _phone.matches && view === 'chats';
    app.classList.toggle('m-list', on && list);
    app.classList.toggle('m-chat', on && !list);
  }
  function toList(push) {
    list = true;
    if (push && snPath() !== '/') history.pushState({ snList: 1 }, '', '/');
    apply();
  }
  function toChat() {
    if (!list) return;
    list = false;
    apply();
    if (activeId) syncChatUrl();
  }

  // Header pieces: a title for the list, a back arrow for the chat.
  const title = document.createElement('span');
  title.className = 'm-title';
  title.textContent = 'Chats';
  const back = document.createElement('button');
  back.type = 'button';
  back.className = 'm-back';
  back.title = 'All chats';
  back.setAttribute('aria-label', 'All chats');
  back.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>';
  const tabsEl = document.getElementById('tabs');
  top.insertBefore(title, tabsEl);
  top.insertBefore(back, tabsEl);
  back.addEventListener('click', () => {
    // Came from the list: step back to it, so the system back works the same.
    if (history.state && history.state.snFromList) history.back();
    else toList(true);
  });

  // In the list the address stays "/" (the chat's link is given when it opens).
  {
    const _sync = syncChatUrl;
    syncChatUrl = function () { if (list && _phone.matches) return; return _sync(); };
  }
  {
    const _sv = setView;
    setView = function (v) {
      view = v;
      if (v === 'chats' && _phone.matches && snPath() === '/') list = true;
      apply();
      return _sv.apply(this, arguments);
    };
  }
  // A chat opened from the list: mark the step, so back returns to the list.
  {
    const _push = history.pushState.bind(history);
    history.pushState = function (state, t, url) {
      if (list && _phone.matches && typeof url === 'string' && /^\/chats\/[^/]+$/.test(url)) {
        state = Object.assign({}, state || {}, { snFromList: 1 });
        list = false;
        apply();
      }
      return _push(state, t, url);
    };
  }
  // Before the app's own router, which would give "/" the chat's address.
  window.addEventListener('popstate', () => {
    if (!_phone.matches) return;
    list = snPath() === '/';
    apply();
  }, true);

  // Picking a chat or New chat in a menu leaves the list.
  document.addEventListener('click', (e) => {
    if (!list || !_phone.matches) return;
    if (e.target.closest('.pop [data-pick], .pop [data-open], .pop .pop__new, .cs-rec [data-open]')) setTimeout(toChat, 0);
  }, true);

  // The list row shows the last message under the name, as a messenger does.
  {
    const _rt = renderTabs;
    renderTabs = function () {
      const r = _rt.apply(this, arguments);
      for (const el of document.querySelectorAll('#tabs .tab[data-id]')) {
        const m = (typeof _deskMeta !== 'undefined' && _deskMeta[el.dataset.id]) || {};
        // A colleague's relayed answer reads as theirs: "John: ..." (as in the thread).
        const last = String(m.lastMsg || '').replace(/\s+/g, ' ').trim()
          .replace(/^Answer from ([\w.-]+):\s*/i, (x, n) => {
            const a = agentBySlug(n.toLowerCase());
            return ((a && a.title) || n.charAt(0).toUpperCase() + n.slice(1)) + ': ';
          });
        if (!last) continue;
        const rows = el.querySelector('.tab__rows') || el;
        const s = document.createElement('span');
        s.className = 'm-last';
        s.textContent = last;
        rows.appendChild(s);
      }
      return r;
    };
  }

  // ---- New chat as a screen; any popover closes by a tap outside it ----
  // On a phone the agent picker dropped from + over the whole screen, and the only
  // way out was a thin strip under it. Now it is a screen of
  // its own, "New chat" with a close cross, as a messenger's contact picker; the
  // system back closes it too. And any popover on a phone closes on a tap outside
  // it, the header included: the scrim began under the button that opened it.
  {
    const _pick = openNewChatPicker;
    openNewChatPicker = function () {
      const r = _pick.apply(this, arguments);
      if (!_phone.matches || !_pop) return r;
      _pop.classList.add('m-sheet');
      _pop.style.top = '';
      _pop.style.maxHeight = '';
      if (_popArrow) { _popArrow.remove(); _popArrow = null; }
      if (_scrim) _scrim.style.top = '0px';
      const lbl = _pop.querySelector('.pop__lbl');
      const head = document.createElement('div');
      head.className = 'm-sheet__head';
      head.innerHTML = '<span class="m-sheet__t">New chat</span><button type="button" class="m-sheet__x" aria-label="Close" title="Close">&times;</button>';
      _pop.insertBefore(head, _pop.firstChild);
      if (lbl) lbl.remove();
      head.querySelector('.m-sheet__x').addEventListener('click', closeSheet);
      history.pushState({ snSheet: 1 }, '', location.href);
      return r;
    };
  }
  function closeSheet() {
    closePop();
    if (history.state && history.state.snSheet) history.back();
  }
  window.addEventListener('popstate', () => {
    if (_pop && _pop.classList.contains('m-sheet')) closePop();
  }, true);
  document.addEventListener('click', (e) => {
    if (!_phone.matches || !_pop || _pop.contains(e.target)) return;
    if (_scrim && e.target === _scrim) return;
    e.stopPropagation();
    e.preventDefault();
    if (_pop.classList.contains('m-sheet')) closeSheet(); else closePop();
  }, true);

  _phone.addEventListener('change', () => {
    if (_phone.matches) list = snPath() === '/';
    apply();
  });

  const css = document.createElement('style');
  css.textContent = `
    .m-title, .m-back, .m-last { display: none; }
    @media (max-width: 760px) {
      /* the list */
      .app.m-list #task { display: none !important; }
      .app.m-list .main__top { flex: 1 1 auto; min-height: 0; display: grid; position: relative;
        grid-template-columns: auto minmax(0, 1fr); grid-template-rows: 56px minmax(0, 1fr);
        align-items: center; border-bottom: none; }
      .app.m-list .m-title { display: block; font-size: 19px; font-weight: 600; color: var(--sn-ink);
        padding-left: 8px; }
      .app.m-list .main__top .tabs { grid-column: 1 / -1; align-self: stretch; height: auto; flex-direction: column;
        align-items: stretch; gap: 0; padding: 0 0 24px; overflow-x: hidden; overflow-y: auto;
        border-top: 1px solid var(--sn-rule); -webkit-overflow-scrolling: touch; }
      .app.m-list .tabs > .tab-add { position: absolute; top: 10px; right: 12px; width: 36px; height: 36px;
        border: 1px solid var(--sn-rule); background: var(--sn-surface); color: var(--sn-ink); }
      .app.m-list .tabs > .tab { width: auto; max-width: none; flex: 0 0 auto; min-height: 68px; gap: 12px;
        padding: 10px 16px; border: none; border-bottom: 1px solid var(--sn-rule); border-radius: 0;
        background: transparent; box-shadow: none; }
      .app.m-list .tabs > .tab::before, .app.m-chat .tabs > .tab::before { display: none; }
      .app.m-list .tabs > .tab:active { background: var(--sn-surface); }
      .app.m-list .tabs > .tab .tab__av { width: 48px; height: 48px; flex: 0 0 48px; border-radius: 50%; font-size: 19px; }
      .app.m-list .tabs > .tab .tab__ic .lucide { width: 22px; height: 22px; }
      .app.m-list .tab__rows { gap: 3px; }
      .app.m-list .tab--agent .tab__nm { font-size: 15.5px; }
      .app.m-list .tab__title { font-size: 13px; color: var(--sn-ink-mute); }
      .app.m-list .m-last { display: block; font-size: 14px; line-height: 1.3; color: var(--sn-ink-mute);
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
      .app.m-list .tab:has(.m-last) .tab__row--bot { display: none; }
      .app.m-list .tab__age { font-size: 12px; }
      .app.m-list .tab__x, .app.m-list .tab__ctx { display: none; }
      .pop.m-sheet { inset: 0; top: 0; left: 0; right: 0; bottom: 0; width: auto; max-width: none; max-height: none;
        border: none; border-radius: 0; padding: 0 8px 24px; overflow-y: auto; z-index: 202;
        background: var(--sn-bg); box-shadow: none; }
      .m-sheet__head { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; height: 56px;
        margin: 0 -8px 6px; padding: 0 10px 0 18px; background: var(--sn-bg); border-bottom: 1px solid var(--sn-rule); }
      .m-sheet__t { flex: 1; font-size: 19px; font-weight: 600; color: var(--sn-ink); }
      .m-sheet__x { width: 36px; height: 36px; border: none; border-radius: var(--sn-r-sm); background: transparent;
        color: var(--sn-ink); font-size: 26px; line-height: 1; cursor: pointer; }
      .pop.m-sheet .pop__it { min-height: 52px; font-size: 15px; gap: 12px; }
      .pop.m-sheet .pop__av { width: 40px; height: 40px; border-radius: 50%; }
      /* New chat: the team as a grid of faces, as a phone's contact picker
         (a list of twenty-odd agents was a long scroll) */
      .pop.m-sheet .pop__grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(84px, 1fr)); gap: 4px; padding-top: 6px; }
      .pop.m-sheet .pop__grid .pop__it { flex-direction: column; justify-content: flex-start; gap: 7px; min-height: 0;
        padding: 10px 4px; font-size: 13px; text-align: center; }
      .pop.m-sheet .pop__grid .pop__av { width: 56px; height: 56px; font-size: 22px; }
      .pop.m-sheet .pop__grid .pop__nm { max-width: 100%; }
      /* the chat */
      .app.m-chat .sb-launch { display: none; }
      .app.m-chat .m-back { display: grid; place-items: center; width: 36px; height: 36px; margin: 0 0 0 6px;
        flex: 0 0 auto; border: none; border-radius: var(--sn-r-sm); background: transparent; color: var(--sn-ink);
        cursor: pointer; }
      .app.m-chat .m-back:active { background: var(--sn-surface); }
      .app.m-chat .tabs { padding-left: 2px; overflow: hidden; }
      .app.m-chat .tabs > .tab:not(.is-active), .app.m-chat .tabs > .tab-add { display: none; }
      .app.m-chat .tabs > .tab.is-active { flex: 1 1 0; min-width: 0; width: auto; max-width: none; background: transparent;
        border-color: transparent; box-shadow: none; }
      .app.m-chat .tabs > .tab .tab__x { display: none; }
    }`;
  document.head.appendChild(css);
  apply();
})();
