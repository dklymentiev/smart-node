// chat-authors.js: loaded after app.js, whose top-level names it uses and
// wraps (sendChat). Chat @mentions.
//
// Studio chat: who speaks in a chat. As in a
// modern messenger: the agent's avatar sits left of its messages at the bottom
// corner of the LAST message of a run, and the name is a small line over the
// first one. A colleague's answer relayed into the chat ("Answer from john: ...",
// an @mention answered) is shown as John's own message, and the relay line is
// dropped. An agent's deliberate silence (NO_REPLY) is not drawn. The avatar is
// the bubble's ::before, fed by a CSS variable and data attributes, so code that
// rewrites a bubble's content (streaming, Stop, copy) cannot remove it; the name
// line is a sibling of the bubble. One observer repaints after any change.
// Reply (v3): a control under Copy on an agent's message. Replying to the chat's
// own agent is an ordinary message with the quote on top. Replying to a
// colleague goes to THAT agent only (Studio POST /api/desks/:id/reply, a direct
// mention): the chat's agent does not receive it, the colleague's answer comes
// back into the chat, and the reply is the person's turn in the history.
(() => {
  const host = document.getElementById('thread');
  if (!host) return;
  host.classList.add('sn-chat--av');
  const css = document.createElement('style');
  css.textContent = `
    .sn-chat--av > .sn-msg--agent { margin-left: 38px; }
    .sn-chat--av > .sn-msg--agent.sn-av-end { margin-bottom: 6px; }
    .sn-chat--av > .sn-msg--agent[data-av-for]:not(.sn-av-end):hover::before { opacity: .6; }
    .sn-chat--av > .sn-msg--agent.sn-av-end::before,
    .sn-chat--av > .sn-msg--agent[data-av-for]:hover::before { content: attr(data-initial);
      position: absolute; left: -38px; bottom: -1px; width: 30px; height: 30px; border-radius: 50%;
      display: grid; place-items: center; font: 600 12px/1 var(--sn-font); color: var(--sn-ink-mute);
      background: var(--sn-av, none) center / cover no-repeat, var(--sn-inset);
      border: 1px solid var(--sn-rule); box-sizing: border-box; }
    .sn-msg--agent > :has(+ .msg-copy), .sn-msg--agent > :has(+ .msg-re) { margin-bottom: 0; }
    .sn-uquote { white-space: normal; margin: 0 0 8px; padding: 7px 10px; border-radius: var(--sn-r-md, 8px);
      background: var(--sn-inset); color: var(--sn-ink-mute); font-size: 12.5px; line-height: 1.4; }
    .sn-uquote b { display: block; color: var(--sn-ink); font-weight: 600; margin-bottom: 1px; }
    .sn-uquote__tx { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    /* A long path or URL with no space broke out of an agent's bubble; a user bubble already wraps anywhere. Code blocks keep their
       own horizontal scroll, wide tables get one. */
    .sn-msg--agent { overflow-wrap: anywhere; min-width: 0; }
    .sn-msg--agent table { display: block; max-width: 100%; overflow-x: auto; }
    /* On a phone an agent's message runs from the avatar to the right edge
      : the 80% cap left a wide empty strip on the right. */
    @media (max-width: 760px) { .sn-chat--av > .sn-msg--agent { max-width: none; } }
    .sn-long.sn-clip > .sn-body { max-height: 15em; overflow: hidden;
      -webkit-mask-image: linear-gradient(to bottom, #000 72%, transparent);
      mask-image: linear-gradient(to bottom, #000 72%, transparent); }
    .sn-more { display: block; margin: 6px 0 0; padding: 0; border: 0; background: none; cursor: pointer;
      font: 600 12.5px/1.3 var(--sn-font); color: var(--sn-ink-mute); }
    .sn-more:hover { color: var(--sn-ink); }
    .sn-more::after { content: attr(data-label); } /* not text: Copy reads innerText */
    /* Copy and Reply in one row UNDER an agent's message, from its left edge
      : beside it they sat low on a one-line message and
       off the screen once the message reached the right edge. */
    .sn-chat--av > .sn-msg--agent > .msg-copy { top: 100%; left: 0; padding: 3px 0 0; z-index: 2; }
    .sn-chat--av > .sn-msg--agent > .msg-re { top: 100%; left: 30px; padding: 3px 0 0; z-index: 2; }
    .sn-chat--av > .sn-msg--agent > .msg-copy .msg-copy__btn,
    .sn-chat--av > .sn-msg--agent > .msg-re .msg-copy__btn { background: var(--sn-bg); }
    .sn-chat--av > .sn-msg--user > .msg-copy { top: 100%; right: 0; left: auto; padding: 3px 0 0; z-index: 2; }
    .sn-chat--av > .sn-msg--user > .msg-re { top: 100%; right: 30px; left: auto; padding: 3px 0 0; z-index: 2; }
    .sn-chat--av > .sn-msg--user > .msg-copy .msg-copy__btn,
    .sn-chat--av > .sn-msg--user > .msg-re .msg-copy__btn { background: var(--sn-bg); }
    .sn-chat--av > .sn-msg--user > .msg-copy .msg-copy__menu { top: 30px; right: 0; left: auto; }
    .sn-quote-sel { position: fixed; z-index: 250; display: flex; align-items: center; gap: 6px;
      padding: 6px 11px; border: 1px solid var(--sn-rule); border-radius: 999px; background: var(--sn-bg-2);
      color: var(--sn-ink); font: 600 12.5px/1 var(--sn-font); box-shadow: var(--sn-shadow); cursor: pointer; }
    .sn-quote-sel svg { width: 14px; height: 14px; }
    .sn-chat--av > .sn-msg.sn-selectable { -webkit-user-select: text; user-select: text; -webkit-touch-callout: default; }
    .sn-at { position: fixed; z-index: 260; max-height: min(320px, 45vh); overflow-y: auto; padding: 5px;
      background: var(--sn-bg-2); border: 1px solid var(--sn-rule); border-radius: 12px; box-shadow: var(--sn-shadow); }
    .sn-at__it { display: flex; align-items: center; gap: 10px; width: 100%; padding: 6px 8px; border: 0;
      border-radius: 8px; background: none; color: var(--sn-ink); font: 400 14px/1.2 var(--sn-font);
      text-align: left; cursor: pointer; }
    .sn-at__it.is-on, .sn-at__it:hover { background: var(--sn-inset); }
    .sn-at__av { width: 28px; height: 28px; flex: 0 0 28px; border-radius: 50%; display: grid; place-items: center;
      background: var(--sn-surface-2); font: 600 12px/1 var(--sn-font); overflow: hidden; }
    .sn-at__av { position: relative; }
    .sn-at__av:has(> .av-img) { color: transparent; }
    .sn-at__av .av-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; border-radius: 50%; }
    .sn-at__nm { font-weight: 600; }
    .sn-at__sl { color: var(--sn-ink-mute); font-size: 12.5px; }
    .sn-at__role { margin-left: auto; color: var(--sn-ink-faint); font-size: 12px; white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis; max-width: 45%; }
    /* Copy and Reply are a message's menu now: the right button on a computer,
       a long press on a phone. The row under a long message
       sat by the composer, far from where it was being read. Studio's Copy
       control stays in the message, hidden: the menu copies through it. */
    .sn-chat--av > .sn-msg > .msg-copy, .sn-chat--av > .sn-msg > .msg-re { display: none; }
    .sn-cm { position: fixed; z-index: 310; min-width: 210px; max-width: 320px; padding: 5px;
      background: var(--sn-bg-2); border: 1px solid var(--sn-rule); border-radius: 10px;
      box-shadow: var(--sn-shadow); display: flex; flex-direction: column; }
    .sn-cm button { display: flex; align-items: center; gap: 10px; min-height: 32px; padding: 0 10px; border: 0;
      border-radius: 7px; background: none; color: var(--sn-ink); font: 400 13.5px/1.2 var(--sn-font);
      text-align: left; cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .sn-cm button:hover, .sn-cm button:focus-visible { background: var(--sn-inset); outline: none; }
    .sn-cm button svg { width: 15px; height: 15px; flex: 0 0 15px; color: var(--sn-ink-mute); }
    .sn-cm__sep { height: 1px; margin: 4px 6px; background: var(--sn-rule); }
    /* A touch screen has no hover: a long press opens the actions instead. */
    @media (hover: none) {
      .sn-chat--av > .sn-msg { -webkit-touch-callout: none; -webkit-user-select: none; user-select: none; }
    }
    .sn-msg.sn-pressed { filter: brightness(1.25); transition: filter .15s; }
    .sn-acts-scrim { position: fixed; inset: 0; z-index: 300; background: rgba(0,0,0,.45); }
    .sn-acts { position: fixed; left: 8px; right: 8px; bottom: 8px; z-index: 301; padding: 6px;
      background: var(--sn-bg-2); border: 1px solid var(--sn-rule); border-radius: 14px;
      box-shadow: var(--sn-shadow); display: flex; flex-direction: column; }
    .sn-acts button { display: flex; align-items: center; gap: 12px; height: 50px; padding: 0 14px; border: 0;
      border-radius: 10px; background: none; color: var(--sn-ink); font: 400 15.5px/1 var(--sn-font);
      text-align: left; cursor: pointer; }
    .sn-acts button:active { background: var(--sn-inset); }
    .sn-acts button svg { width: 18px; height: 18px; color: var(--sn-ink-mute); }
    .sn-acts__sep { height: 1px; margin: 4px 8px; background: var(--sn-rule); }
    .sn-acts__note { padding: 8px 14px 4px; font-size: 12.5px; color: var(--sn-ink-mute); }
    /* An event, not a message: a centred pill, as a messenger's service line. */
    .sn-note { align-self: center; margin: 2px 0; padding: 3px 10px; border-radius: 999px;
      background: var(--sn-inset); color: var(--sn-ink-mute); font: 400 12px/1.4 var(--sn-font); }
    .sn-msg--user.sn-ug-next { margin-top: -6px; border-top-right-radius: 5px; }
    .sn-qchip { display: block; text-align: right; margin: 4px -2px -3px 0; font-size: 11.5px;
      line-height: 1.2; color: var(--sn-ink-mute); white-space: nowrap; }
    .sn-qchip button { border: 0; background: none; color: inherit; cursor: pointer; font-size: 14px;
      line-height: 1; padding: 0 2px; vertical-align: -1px; }
    .sn-qchip button:hover { color: var(--sn-ink); }
    .msg-re { position: absolute; top: 32px; left: 100%; padding-left: 6px; opacity: 0; }
    .sn-msg:hover > .msg-re, .msg-re:focus-within { opacity: 1; }
    .sn-reply { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; padding: 6px 10px;
      background: var(--sn-inset); border-radius: var(--sn-r-md, 8px);
      font: 400 12.5px/1.35 var(--sn-font); color: var(--sn-ink-mute); }
    .sn-reply b { color: var(--sn-ink); font-weight: 600; }
    .sn-reply__tx { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .sn-reply__x { border: 0; background: none; color: var(--sn-ink-mute); cursor: pointer;
      font-size: 16px; line-height: 1; padding: 0 2px; }
    .sn-reply__x:hover { color: var(--sn-ink); }
    .sn-who { align-self: flex-start; margin: 8px 0 -6px 50px;
      font: 600 12px/1.2 var(--sn-font); color: var(--sn-ink-mute); }
  `;
  document.head.appendChild(css);
  const RELAY = /^\s*Answer from ([A-Za-z0-9][A-Za-z0-9._-]{0,63}):\s*/;
  const silent = (t) => /^\W*NO_REPLY\W*$/i.test(String(t || '').trim());
  const deskSlug = () => {
    try { return deskAgent(desks.find((x) => x.id === activeId)) || ''; } catch { return ''; }
  };
  function dropRelayLine(el) {
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      if (!n.nodeValue.trim()) continue;
      n.nodeValue = n.nodeValue.replace(RELAY, '');
      const p = n.parentElement;
      if (p && p !== el && !p.textContent.trim()) p.remove();
      return;
    }
  }
  function authorOf(el) {
    if (el.dataset.author != null) return el.dataset.author;
    const m = String(el._raw != null ? el._raw : el.textContent).match(RELAY);
    if (m) { el.dataset.author = m[1].toLowerCase(); dropRelayLine(el); }
    else el.dataset.author = deskSlug();
    return el.dataset.author;
  }
  const nameOf = (who) => { const a = agentBySlug(who); return (a && a.title) || who; };
  const BELL_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>';
  function nameRow(el, who) {
    const p = el.previousElementSibling;
    let row = p && p.classList.contains('sn-who') ? p : null;
    if (who == null) { if (row) row.remove(); return; }
    if (!row) { row = document.createElement('div'); row.className = 'sn-who'; el.before(row); }
    // /next2: the run's head is the avatar, the name
    // and the time of its first message on one line; the text follows under the name.
    const a = agentBySlug(who), src = a ? avResolve(a.avatar) : null;
    const d = typeof msgDate === 'function' ? msgDate(el.dataset.at) : null;
    const at = d ? String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0') : '';
    // The system's line has no face and is nobody's words.
    const sys = who === 'system';
    const html = (sys ? '' : `<span class="sn-who__av"${src ? ` style="--sn-av:url(&quot;${esc(src)}&quot;)"` : ''}>`
      + `${src ? '' : esc((nameOf(who) || '?').trim().charAt(0).toUpperCase())}</span>`)
      + `<span class="sn-who__nm">${sys ? 'System' : esc(nameOf(who))}</span>`
      + (el.dataset.reminder ? `<span class="sn-who__rem" title="Reminder" aria-label="Reminder">${BELL_ICON}</span>` : '')
      + `${at ? `<span class="sn-who__at">${at}</span>` : ''}`;
    row.dataset.who = who;
    if (row._html !== html) { row.innerHTML = html; row._html = html; }
  }
  // A quiet line across the thread where a new day starts or after an hour of
  // silence: "TODAY · 19:59". Kept in place when it
  // is already right, so redrawing the thread does not churn it.
  function dayLine(el, label) {
    const head = el.previousElementSibling && el.previousElementSibling.classList.contains('sn-who') ? el.previousElementSibling : el;
    const p = head.previousElementSibling;
    const has = p && p.classList.contains('sn-sep') ? p : null;
    if (!label) { if (has) has.remove(); return; }
    if (has) { if (has.textContent !== label) has.textContent = label; return; }
    const s = document.createElement('div');
    s.className = 'sn-sep';
    s.textContent = label;
    head.before(s);
  }
  function dayLines() {
    let prev = null;
    const today = new Date().toDateString(), yest = new Date(Date.now() - 864e5).toDateString();
    for (const el of Array.from(host.children)) {
      if (el.classList.contains('sn-sep')) {
        const n = el.nextElementSibling;
        if (!n || !(n.classList.contains('sn-msg') || n.classList.contains('sn-who'))) el.remove();
        continue;
      }
      if (!el.classList.contains('sn-msg') || el.hidden) continue;
      const d = typeof msgDate === 'function' ? msgDate(el.dataset.at) : null;
      if (!d) continue;
      const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
      const day = d.toDateString() === today ? 'Today' : d.toDateString() === yest ? 'Yesterday'
        : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', ...(d.getFullYear() !== new Date().getFullYear() ? { year: 'numeric' } : {}) });
      const fresh = !prev || prev.toDateString() !== d.toDateString() || d - prev >= 3600e3;
      dayLine(el, fresh ? `${day} · ${hm}` : null);
      prev = d;
    }
  }
  // Every message of a run carries its author's avatar (shown on hover); only
  // the last one shows it all the time. who=null: no avatar at all (hidden).
  function avatar(el, who, last) {
    if (who == null) {
      if (el.classList.contains('sn-av-end')) el.classList.remove('sn-av-end');
      if (el.dataset.avFor != null) delete el.dataset.avFor;
      return;
    }
    if (el.dataset.avFor !== who) {
      const a = agentBySlug(who);
      const src = a ? avResolve(a.avatar) : null;
      el.dataset.avFor = who;
      el.dataset.initial = src ? '' : (nameOf(who) || '?').trim().charAt(0).toUpperCase();
      if (src) el.style.setProperty('--sn-av', `url("${src.replace(/"/g, '%22')}")`);
      else el.style.removeProperty('--sn-av');
      el.title = nameOf(who);
    }
    if (last !== el.classList.contains('sn-av-end')) el.classList.toggle('sn-av-end', last);
  }
  // ---- Colleagues at work on a question from this chat ----
  // A colleague answers outside this chat's own turn, so the chat had no sign of
  // it (only the agent list did). core-api knows: GET /api/desks/:id/waiting
  // lists the open chat mentions of this desk, one per agent, with what the agent
  // is doing (working, busy with something taken earlier, queued). Each one is a
  // pending bubble in that agent's name at the end of the chat, until its row
  // closes, i.e. it answered or dismissed. Server truth, so it holds for an
  // @mention in the text, a Reply, and after a reload alike.
  let _waiting = { desk: null, list: [] };
  const _wEls = {};
  const waitLabel = (w) => (w.state === 'busy' ? 'Busy with another task; this is next'
    : w.state === 'queued' ? 'Waiting for ' + nameOf(w.agent) + ' to start' : 'working');
  function dropWait(agent) {
    const x = _wEls[agent];
    if (!x) return;
    if (x.think) x.think.stop();
    if (x.stopBtn) x.stopBtn.remove();
    if (x.el) x.el.remove();
    delete _wEls[agent];
  }
  // /next2: the colleague's own turn, pushed through this chat's live stream
  // once the gateway knows the wake works for this chat. Its line says what she does, the
  // text so far and the recent actions show as for the chat's own agent, and its
  // Stop, in her head, stops her turn by name.
  window.snColleagueLive = (agent, j) => {
    const x = _wEls[agent];
    if (!x || !x.el || !x.el.isConnected) return;
    x.liveAt = Date.now();
    x.live = x.live || { steps: [], liveText: '' };
    x.live.el = x.el;
    const act = j.action ? j.action + (j.detail ? ': ' + j.detail : '') : null;
    const lab = act ? 'working: ' + act : 'working';
    if (x.label !== lab && x.label !== 'stopping') { x.label = lab; x.think.setLabel(lab); }
    if (typeof j.text === 'string' && j.text !== x.live.liveText) { x.live.liveText = j.text; renderLiveText(x.live); }
    if (Array.isArray(j.steps)) { x.live.steps = j.steps.slice(-40); renderSteps(x.live); }
    colleagueStop(x, agent);
  };
  function colleagueStop(x, agent) {
    const head = typeof runHeadOf === 'function' ? runHeadOf(x.el) : null;
    if (!head) return;
    if (!x.stopBtn) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'turn-stop';
      b.title = 'Stop ' + nameOf(agent);
      b.setAttribute('aria-label', b.title);
      b.innerHTML = '<span class="turn-stop__sq"></span>';
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        b.disabled = true;
        x.label = 'stopping'; x.think.setLabel('stopping');
        fetch(`/api/desks/${encodeURIComponent(activeId)}/abort`, { method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ agent }) }).catch(() => {});
      });
      x.stopBtn = b;
    }
    if (x.stopBtn.parentElement !== head) head.appendChild(x.stopBtn);
  }
  function paintPending() {
    const list = _waiting.desk === activeId ? _waiting.list : [];
    for (const agent of Object.keys(_wEls)) {
      if (!list.some((w) => w.agent === agent)) dropWait(agent);
    }
    for (const w of list) {
      let x = _wEls[w.agent];
      if (x && x.el && x.el.isConnected) {
        // the pushed line (snColleagueLive) is fresher than this 4 s poll's word
        const pushed = x.liveAt && Date.now() - x.liveAt < 6000;
        if (!pushed && x.label !== waitLabel(w)) { x.label = waitLabel(w); x.think.setLabel(x.label); }
        if (x.stopBtn) colleagueStop(x, w.agent);
        continue;
      }
      if (x && x.think) x.think.stop();
      const el = bubble('agent', '<span class="sn-thinking"></span>');
      if (!el) continue;
      el.dataset.pending = '1';
      el.dataset.author = w.agent;
      const t0 = Date.parse(w.working_since || w.since) || Date.now();
      x = _wEls[w.agent] = { el, think: thinkingIndicator(el, t0), label: waitLabel(w) };
      x.think.setLabel(x.label);
    }
  }
  async function pollWaiting() {
    const id = activeId;
    if (!id || document.hidden) return;
    try {
      const r = await fetch(`/api/desks/${encodeURIComponent(id)}/waiting`, { headers: { Accept: 'application/json' } });
      if (!r.ok) return;
      const list = ((await r.json()).waiting) || [];
      if (id !== activeId) return;
      _waiting = { desk: id, list };
      schedule();
    } catch {}
  }
  async function waitFor() { await pollWaiting(); }
  // no poll: who waits changes with a message or a turn (live stream)
  document.addEventListener('sn:event', (e) => { const t = String((e.detail || {}).type);
    if (t.startsWith('message.') || t.startsWith('agent.') || (e.detail || {}).desk === activeId) pollWaiting(); });
  document.addEventListener('sn:resync', pollWaiting);
  // A message just sent may have mentioned someone: ask at once, not in 4 s.
  const _send1 = sendChat;
  sendChat = async function () {
    const p = _send1();
    setTimeout(pollWaiting, 1500);
    return p;
  };
  // A reply carries its quote as the first line, "> Name: excerpt". In the
  // person's bubble it is drawn as a quote (muted block, two lines at most), and
  // the text under it as typed.
  const QUOTE = /^> ([^:\n]{1,60}): ([^\n]*)\n+/;
  function quoteOf(el) {
    if (el.dataset.q) return;
    el.dataset.q = '1';
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      if (!n.nodeValue.trim()) continue;
      const m = n.nodeValue.match(QUOTE);
      if (!m) return;
      const q = document.createElement('div');
      q.className = 'sn-uquote';
      q.innerHTML = `<b>${esc(m[1])}</b><span class="sn-uquote__tx">${esc(m[2])}</span>`;
      n.nodeValue = n.nodeValue.slice(m[0].length);
      n.parentNode.insertBefore(q, n);
      return;
    }
  }
  // ---- Every new message rests in the middle of the view ----
  // The rule a sent message already follows: it glides up
  // until its lower edge is on the middle of the view, or its top on the top
  // when it is taller than half the view, and the room under it is kept free.
  // Now the same for a message an agent writes: a
  // colleague's answer, which the relay poll adds below the fold, and any other
  // that arrives on its own. A reply streamed into the room under the question
  // is already in place and is left alone. The room is an empty block at the
  // end of the thread, sized to what is missing below; it survives the poll's
  // rebuild of the thread and goes when the person sends again.
  let _arr = { desk: null, n: -1 };
  let _shownAt = 0;   // when the open chat was first painted
  let _room = { desk: null, h: 0 };
  function keepRoom() {
    let r = host.querySelector(':scope > .sn-room');
    if (_room.desk !== activeId || !_room.h) { if (r) r.remove(); return; }
    if (!r) { r = document.createElement('div'); r.className = 'sn-room'; r.setAttribute('aria-hidden', 'true'); }
    if (host.lastElementChild !== r) host.appendChild(r);
    const h = _room.h + 'px';
    if (r.style.height !== h) r.style.height = h;
  }
  function place(first, last) {
    if (!first.isConnected || !last.isConnected) return;
    const sc = host.parentElement;
    const usable = sc.clientHeight;
    const scTop = sc.getBoundingClientRect().top;
    const top = first.getBoundingClientRect().top - scTop;
    const height = last.getBoundingClientRect().bottom - first.getBoundingClientRect().top;
    const want = Math.max(16, Math.floor(usable * 0.5 - height));
    const target = Math.max(0, Math.round(sc.scrollTop + top - want));
    const r = host.querySelector(':scope > .sn-room');
    const without = sc.scrollHeight - (r ? r.offsetHeight : 0);
    _room = { desk: activeId, h: Math.max(0, Math.ceil(target - (without - usable))) };
    keepRoom();
    sc.scrollTo({ top: target, behavior: 'smooth' });
  }
  // A thread built anew for another chat (a switch, or the first load) is not
  // news: every message in it would read as just arrived, and the view glided
  // from the end to the first of them. While such a load
  // runs nothing is counted; when it ends, what is there is the baseline.
  let _fresh = null;
  {
    const _lh = loadHistory;
    loadHistory = async function (id) {
      const fresh = _threadFor !== id;
      if (fresh) _fresh = id;
      try {
        return await _lh.apply(this, arguments);
      } finally {
        if (fresh && _fresh === id) {
          _fresh = null;
          _arr = { desk: null, n: -1 };
          arrivals();
        }
      }
    };
  }
  function arrivals() {
    keepRoom();
    if (_fresh && _fresh === activeId) return;
    const msgs = [];
    for (const e of host.children) {
      if (!e.classList.contains('sn-msg--agent') || e.hidden || e.dataset.pending) continue;
      if (e.querySelector('.sn-think, .sn-thinking')) { e.dataset.wasTurn = '1'; continue; }
      msgs.push(e);
    }
    const prev = _arr;
    _arr = { desk: activeId, n: msgs.length };
    if (prev.desk !== activeId) _shownAt = Date.now();
    if (prev.desk !== activeId || prev.n < 0 || msgs.length <= prev.n) return;
    const first = msgs[prev.n];
    if (first.dataset.wasTurn) return;
    // Only a message written after the chat was opened has arrived. A count taken
    // over a still empty thread made the whole history read as new when it was
    // drawn, and the view glided to its first message (phone audit, 2026-10-30).
    if (first.dataset.at && new Date(first.dataset.at).getTime() < _shownAt - 5000) return;
    // The poll would otherwise drop the view to the very end of the thread.
    _tailDesk = activeId;
    setTimeout(() => place(first, msgs[msgs.length - 1]), 120);
  }
  {
    const _send2 = sendChat;
    sendChat = async function () {
      _room = { desk: null, h: 0 };
      keepRoom();
      return _send2();
    };
  }
  // ---- Messages sent in a row ----
  // Several messages typed one after another while the agent is busy each got
  // a "Queued" reply bubble of their own between them. Now
  // the person's messages in a row stack as one group, the queued reply bubble
  // is not drawn, and the message carries a small "queued" mark with the cancel
  // that the bubble had. When its turn starts, the bubble is drawn again, as
  // the reply in progress.
  const isQueued = (el) => !!el.querySelector('.turn-stop--queued');
  function prevUser(el) {
    let x = el.previousElementSibling;
    while (x && !x.classList.contains('sn-msg')) x = x.previousElementSibling;
    return x && x.classList.contains('sn-msg--user') ? x : null;
  }
  function queuedMark(u, bub) {
    let c = u.querySelector(':scope > .sn-qchip');
    if (!bub) { if (c) c.remove(); return; }
    if (!c) {
      c = document.createElement('span');
      c.className = 'sn-qchip';
      c.innerHTML = 'queued <button type="button" title="Cancel queued message" aria-label="Cancel queued message">&times;</button>';
      c.querySelector('button').addEventListener('click', (e) => {
        e.stopPropagation();
        const b = c._bub && c._bub.querySelector('.turn-stop');
        if (b) b.click();
      });
      u.appendChild(c);
    }
    c._bub = bub;
  }
  function groupUsers() {
    let prev = null;
    for (const el of host.children) {
      if (el.hidden || !el.classList.contains('sn-msg')) {
        if (el.classList.contains('sn-msg') || el.classList.contains('sn-who')) continue;
        continue;
      }
      const user = el.classList.contains('sn-msg--user');
      const next = user && prev && prev.classList.contains('sn-msg--user');
      if (el.classList.contains('sn-ug-next') !== !!next) el.classList.toggle('sn-ug-next', !!next);
      prev = el;
      // A mark whose reply bubble is gone or running again goes.
      const c = user && el.querySelector(':scope > .sn-qchip');
      if (c && !(c._bub && c._bub.isConnected && isQueued(c._bub))) c.remove();
    }
  }
  // ---- A long message folded to ten lines on a phone ----
  // Agents write long answers, and on a phone one filled several screens. A settled agent message taller than ten lines shows its first
  // ten, faded out, with "Show more"; open, it has "Show less". The content is
  // moved into one inner block, so the fade does not cut the avatar (drawn
  // outside the bubble) or the Copy and Reply controls. What was opened stays
  // open when the poll rebuilds the thread. A message still being written is
  // left alone. Desktop: not folded.
  const _opened = new Set();
  const keyOf = (el) => { const t = String(el._raw != null ? el._raw : el.textContent); return t.length + ':' + t.slice(0, 120); };
  function fold(el) {
    // /next2: folded on every screen, not on a phone only.
    const live = el.dataset.pending || el.querySelector('.sn-think, .sn-thinking, .turn-stop');
    let body = el.querySelector(':scope > .sn-body');
    let btn = el.querySelector(':scope > .sn-more');
    if (live || el.hidden) {
      if (el.classList.contains('sn-long')) el.classList.remove('sn-long', 'sn-clip');
      if (btn) btn.remove();
      return;
    }
    if (!body) {
      body = document.createElement('div');
      body.className = 'sn-body';
      for (const n of Array.from(el.childNodes)) {
        if (n.nodeType === 1 && n.matches('.msg-copy, .msg-re, .sn-more')) continue;
        body.appendChild(n);
      }
      el.insertBefore(body, el.firstChild);
    }
    const lh = parseFloat(getComputedStyle(body).lineHeight) || 21;
    const long = body.scrollHeight > lh * 13;
    if (!long) {
      if (el.classList.contains('sn-long')) el.classList.remove('sn-long', 'sn-clip');
      if (btn) btn.remove();
      return;
    }
    const open = _opened.has(keyOf(el));
    if (!el.classList.contains('sn-long')) el.classList.add('sn-long');
    if (el.classList.contains('sn-clip') === open) el.classList.toggle('sn-clip', !open);
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'sn-more';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const k = keyOf(el);
        const opening = !_opened.has(k);
        if (opening) _opened.add(k); else _opened.delete(k);
        el.classList.toggle('sn-clip', !opening);
        btn.dataset.label = opening ? 'Show less' : 'Show more';
        if (!opening) {
          // Folding back from far below: bring the message's start into view.
          const sc = host.parentElement;
          const top = el.getBoundingClientRect().top - sc.getBoundingClientRect().top;
          if (top < 0) sc.scrollBy({ top: top - 12, behavior: 'smooth' });
        }
      });
      body.after(btn);
    }
    const label = open ? 'Show less' : 'Show more';
    if (btn.dataset.label !== label) btn.dataset.label = label;
  }
  if (typeof _phone !== 'undefined') _phone.addEventListener('change', () => schedule());
  // A colleague who was asked and stayed silent (NO_REPLY) is one muted line,
  // not nothing: the person saw "thinking" and then no answer at all. The chat's own agent staying silent is still not drawn.
  function silentNote(el, who) {
    let n = el.nextElementSibling;
    if (!(n && n.classList.contains('sn-note'))) n = null;
    if (!who) { if (n) n.remove(); return; }
    if (!n) { n = document.createElement('div'); n.className = 'sn-note'; el.after(n); }
    const t = nameOf(who) + ' saw the message and did not reply';
    if (n.textContent !== t) n.textContent = t;
  }
  let queued = false;
  function paint() {
    queued = false;
    const runs = [];
    let run = null;
    for (const el of Array.from(host.children)) {
      if (el.classList.contains('sn-who')) {
        if (!(el.nextElementSibling && el.nextElementSibling.classList.contains('sn-msg--agent'))) el.remove();
        continue;
      }
      if (el.classList.contains('sn-sep')) continue;
      if (!el.classList.contains('sn-msg')) continue;
      if (!el.classList.contains('sn-msg--agent')) {
        if (el.classList.contains('sn-msg--user')) quoteOf(el);
        run = null; continue;
      }
      if (isQueued(el)) {
        el.hidden = true; el.dataset.queuedHidden = '1';
        nameRow(el, null); avatar(el, null);
        const u = prevUser(el);
        if (u) queuedMark(u, el);
        continue;
      }
      if (el.dataset.queuedHidden) { delete el.dataset.queuedHidden; el.hidden = false; }
      if (silent(el.textContent) && !el.querySelector('.sn-thinking')) {
        el.hidden = true; nameRow(el, null); avatar(el, null);
        const rm = String(el._raw != null ? el._raw : el.textContent).match(RELAY);
        const sw = rm ? rm[1].toLowerCase() : el.dataset.author;
        silentNote(el, sw && sw !== deskSlug() ? sw : null);
        continue;
      }
      el.hidden = false;
      const who = authorOf(el);
      // A reminder heads its own run, under a bell: run into the agent's last
      // answer, it read as a stray text with no sender (tester, 2026-10-30).
      const rem = !!el.dataset.reminder;
      if (!run || run.who !== who || rem || run.rem) { run = { who, els: [], rem }; runs.push(run); }
      run.els.push(el);
    }
    for (const r of runs) {
      r.els.forEach((el, i) => {
        nameRow(el, i === 0 && r.who ? r.who : null);
        // /next2: the avatar ends the run, at its lower-left corner
        avatar(el, r.who && r.who !== 'system' ? r.who : null, i === r.els.length - 1);
        fold(el);
      });
    }
    dayLines();
    // A turn's Stop belongs in its run's head, which exists only from this paint
    // on: put it there now, not at the line's next change.
    if (typeof paintChatControls === 'function') paintChatControls();
    paintPending();
    arrivals();
    groupUsers();
  }
  // ---- Reply to one message ----
  const RE_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
    + 'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>';
  let _reply = null;
  const excerptOf = (el) => {
    let t = el._raw;
    if (t == null) {
      const c = el.cloneNode(true);
      c.querySelectorAll('.msg-copy, .msg-re, .sn-more, .sn-uquote, .sn-qchip, .turn-stop, .sn-steer-status').forEach((n) => n.remove());
      t = c.textContent;
    }
    t = String(t).replace(RELAY, '');
    const flat = t.replace(/\s+/g, ' ').trim();
    return flat.length > 160 ? flat.slice(0, 157) + '...' : flat;
  };
  function clearReply() {
    _reply = null;
    const c = document.querySelector('#composer .sn-reply');
    if (c) c.remove();
  }
  // Reply to the person's own message goes to the chat's
  // agent with that message quoted under the person's name. A fragment, when one
  // was selected, is quoted instead of the whole message.
  // The account name is a login ("admin"), not a person's name: the quote says "Me".
  const meName = () => 'Me';
  function setReply(el, frag) {
    const mine = el.classList.contains('sn-msg--user');
    const who = mine ? deskSlug() : authorOf(el);
    if (!who) return;
    let excerpt = excerptOf(el);
    if (frag) {
      const f = String(frag).replace(/\s+/g, ' ').trim();
      if (f) excerpt = f.length > 400 ? f.slice(0, 397) + '...' : f;
    }
    _reply = { deskId: activeId, who, excerpt, name: mine ? meName() : nameOf(who), mine };
    let c = document.querySelector('#composer .sn-reply');
    if (!c) {
      c = document.createElement('div');
      c.className = 'sn-reply';
      const row = document.querySelector('#composer .cmp__row');
      row.parentElement.insertBefore(c, row);
    }
    const own = who === deskSlug();
    // /next2: two lines: who and when, then the start of the
    // message. A reply to a colleague still says it goes to them only.
    const at = (el.querySelector(':scope > .sn-msg__t') || {}).textContent || '';
    c.innerHTML = `<span class="sn-reply__tx"><span class="sn-reply__hd"><b>${mine ? 'You' : esc(nameOf(who))}</b>`
      + `${at ? `<span>${esc(at)}</span>` : ''}${own || mine ? '' : `<span>only to ${esc(nameOf(who))}</span>`}</span>`
      + `<span class="sn-reply__ex">${esc(_reply.excerpt)}</span></span>`
      + '<button type="button" class="sn-reply__x" title="Cancel reply" aria-label="Cancel reply">&times;</button>';
    c.querySelector('.sn-reply__x').onclick = () => clearReply();
    _cmpTa.focus();
  }
  function ensureReply(el) {
    if (!el || el.hidden || el.dataset.pending || !el.classList.contains('sn-msg')) return;
    if (el.querySelector(':scope > .msg-re') || el.querySelector('.sn-think, .sn-thinking')) return;
    const c = document.createElement('span');
    c.className = 'msg-re';
    c.innerHTML = `<button type="button" class="msg-copy__btn" title="Reply" aria-label="Reply">${RE_ICON}</button>`;
    c.querySelector('button').addEventListener('click', (e) => { e.stopPropagation(); setReply(el); });
    el.appendChild(c);
  }
  host.addEventListener('mouseover', (e) => {
    const m = e.target.closest ? e.target.closest('.sn-msg') : null;
    if (m && host.contains(m)) ensureReply(m);
  });
  // ---- Long press on a touch screen: the message's actions ----
  // A phone has no hover, so Copy and Reply were out of reach.
  // As in a messenger: hold a message for half a second, a sheet slides up from
  // the bottom with Reply (an agent's message), Copy as text, Copy as Markdown.
  // The copies go through Studio's own Copy control, so they copy exactly
  // what the desktop button does. A move or a scroll cancels the press.
  let _press = null;
  function closeActs() {
    document.querySelectorAll('.sn-acts, .sn-acts-scrim').forEach((n) => n.remove());
  }
  function copyVia(el, as) {
    ensureCopy(el);
    const b = el.querySelector(`:scope > .msg-copy button[data-as="${as}"]`);
    if (b) b.click();
  }
  function openActs(el) {
    closeActs();
    const agent = el.classList.contains('sn-msg--agent');
    const scrim = document.createElement('div');
    scrim.className = 'sn-acts-scrim';
    const box = document.createElement('div');
    box.className = 'sn-acts';
    const COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
    const SEL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h14M5 20h14M12 4v16"/></svg>';
    box.innerHTML = `<button type="button" data-a="re">${RE_ICON.replace('width="14" height="14"', '')} ${agent ? 'Reply to ' + esc(nameOf(authorOf(el))) : 'Reply'}</button>`
      + `<button type="button" data-a="sel">${SEL} Select text</button><div class="sn-acts__sep"></div>`
      + `<button type="button" data-a="text">${COPY} Copy as text</button>`
      + (typeof el._raw === 'string' && el._raw.trim() ? `<button type="button" data-a="md">${COPY} Copy as Markdown</button>` : '');
    scrim.addEventListener('click', closeActs);
    box.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-a]');
      if (!b) return;
      const a = b.dataset.a;
      if (a === 're') { closeActs(); setReply(el); return; }
      if (a === 'sel') {
        // Native selection in this one message; the Quote chip takes the fragment.
        closeActs();
        el.classList.add('sn-selectable');
        const body = el.querySelector(':scope > .sn-body') || el;
        const r = document.createRange();
        r.selectNodeContents(body);
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
        return;
      }
      copyVia(el, a);
      b.innerHTML = 'Copied';
      setTimeout(closeActs, 450);
    });
    document.body.append(scrim, box);
  }
  // Decided by what pressed, not by the screen: a laptop with a touch screen
  // has both, and a mouse on a phone is a mouse.
  let _pointer = 'mouse';
  host.addEventListener('pointerdown', (e) => { _pointer = e.pointerType || 'mouse'; }, true);
  const byTouch = () => _pointer === 'touch' || _pointer === 'pen';
  host.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'touch' && e.pointerType !== 'pen') return;
    const el = e.target.closest('.sn-msg');
    if (!el || !host.contains(el) || el.hidden || el.dataset.pending) return;
    if (e.target.closest('a, button, summary, input, textarea')) return;
    if (el.querySelector('.sn-think, .sn-thinking')) return;
    const x = e.clientX, y = e.clientY;
    const t = setTimeout(() => {
      _press = null;
      el.classList.add('sn-pressed');
      setTimeout(() => el.classList.remove('sn-pressed'), 250);
      if (navigator.vibrate) navigator.vibrate(10);
      openActs(el);
    }, 480);
    _press = { t, x, y };
  });
  const cancelPress = () => { if (_press) { clearTimeout(_press.t); _press = null; } };
  host.addEventListener('pointermove', (e) => {
    if (_press && Math.hypot(e.clientX - _press.x, e.clientY - _press.y) > 8) cancelPress();
  });
  host.addEventListener('pointerup', cancelPress);
  host.addEventListener('pointercancel', cancelPress);
  host.parentElement.addEventListener('scroll', cancelPress, { passive: true });
  // Android's own long-press menu would open over ours.
  host.addEventListener('contextmenu', (e) => { if (byTouch() && e.target.closest('.sn-msg')) e.preventDefault(); });

  // ---- The right button on a computer: the message's menu ----
  // As in a messenger's desktop app: right-click a message
  // for Reply, Copy as text and Copy as Markdown at the pointer; with part of
  // the message selected, Quote and Copy for that part come first. A link, and
  // a right-click with Shift, keep the browser's own menu.
  let _cm = null;
  function closeMenu() {
    if (!_cm) return;
    _cm.remove();
    _cm = null;
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', closeMenu);
    window.removeEventListener('blur', closeMenu);
  }
  function outside(e) { if (_cm && !_cm.contains(e.target)) closeMenu(); }
  function onKey(e) {
    if (!_cm) return;
    const items = [..._cm.querySelectorAll('button')];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); closeMenu(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
  }
  // The text selected inside this one message, if any.
  function selectedIn(el) {
    const sel = getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return '';
    const r = sel.getRangeAt(0);
    if (!el.contains(r.startContainer) || !el.contains(r.endContainer)) return '';
    return sel.toString().trim();
  }
  async function copyText(t) {
    try { await navigator.clipboard.writeText(t); return true; } catch { return document.execCommand('copy'); }
  }
  function openMenu(el, x, y) {
    closeMenu();
    // The menu has Quote selection: the chip under the selection would sit under it.
    dropQuoteChip();
    const agent = el.classList.contains('sn-msg--agent');
    const frag = selectedIn(el);
    const COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
    const RE = RE_ICON.replace('width="14" height="14"', '');
    const md = typeof el._raw === 'string' && el._raw.trim();
    const m = document.createElement('div');
    m.className = 'sn-cm';
    m.setAttribute('role', 'menu');
    m.innerHTML = (frag
      ? `<button type="button" role="menuitem" data-a="quote">${RE} Quote selection</button>`
        + `<button type="button" role="menuitem" data-a="frag">${COPY} Copy selection</button><div class="sn-cm__sep"></div>`
      : '')
      + `<button type="button" role="menuitem" data-a="re">${RE} ${agent ? 'Reply to ' + esc(nameOf(authorOf(el))) : 'Reply'}</button>`
      + `<button type="button" role="menuitem" data-a="text">${COPY} Copy as text</button>`
      + (md ? `<button type="button" role="menuitem" data-a="md">${COPY} Copy as Markdown</button>` : '');
    // Keep the selection: a mousedown in the menu would collapse it.
    m.addEventListener('mousedown', (e) => e.preventDefault());
    m.addEventListener('contextmenu', (e) => e.preventDefault());
    m.addEventListener('click', async (e) => {
      const b = e.target.closest('button[data-a]');
      if (!b) return;
      const a = b.dataset.a;
      if (a === 're' || a === 'quote') {
        closeMenu();
        if (a === 'quote') getSelection().removeAllRanges();
        dropQuoteChip();
        setReply(el, a === 'quote' ? frag : undefined);
        return;
      }
      // Closed at once. The label "Copied" was set before the copy finished, so it
      // said so even when the copy failed, and it replaced the icon.
      closeMenu();
      if (a === 'frag') await copyText(frag);
      else copyVia(el, a);
    });
    document.body.appendChild(m);
    _cm = m;
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
    m.style.top = Math.max(8, y + h > window.innerHeight - 8 ? y - h : y) + 'px';
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', closeMenu);
    window.addEventListener('blur', closeMenu);
  }
  host.addEventListener('contextmenu', (e) => {
    if (byTouch() || e.shiftKey) return;
    const el = e.target.closest('.sn-msg');
    if (!el || !host.contains(el) || el.hidden || el.dataset.pending) return;
    if (e.target.closest('a[href], input, textarea')) return;
    if (el.querySelector('.sn-think, .sn-thinking')) return;
    e.preventDefault();
    openMenu(el, e.clientX, e.clientY);
  });
  host.parentElement.addEventListener('scroll', closeMenu, { passive: true });

  // ---- Quote a selected fragment ----
  // Select part of a message (with the mouse, or "Select text" on a phone) and a
  // Quote chip appears under the selection: the reply quotes only that part.
  let _qs = null;
  function dropQuoteChip() { if (_qs) { _qs.remove(); _qs = null; } }
  document.addEventListener('selectionchange', () => {
    const sel = getSelection();
    const text = sel && !sel.isCollapsed ? sel.toString().trim() : '';
    const node = text && sel.anchorNode;
    const a = node && (node.nodeType === 1 ? node : node.parentElement);
    const el = a && a.closest && a.closest('.sn-msg');
    const f = sel && sel.focusNode && (sel.focusNode.nodeType === 1 ? sel.focusNode : sel.focusNode.parentElement);
    if (!el || !host.contains(el) || !f || f.closest('.sn-msg') !== el) {
      dropQuoteChip();
      host.querySelectorAll('.sn-msg.sn-selectable').forEach((m) => { if (m !== el) m.classList.remove('sn-selectable'); });
      return;
    }
    // With a mouse the right-click menu already has Quote selection, and the chip
    // popped up under every selection. A touch screen has no
    // right click, so the chip stays there.
    if (!matchMedia('(pointer: coarse)').matches) { dropQuoteChip(); return; }
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    if (!_qs) {
      _qs = document.createElement('button');
      _qs.type = 'button';
      _qs.className = 'sn-quote-sel';
      _qs.innerHTML = RE_ICON + ' Quote';
      _qs.addEventListener('mousedown', (e) => e.preventDefault());
      document.body.appendChild(_qs);
    }
    _qs.onclick = () => {
      setReply(el, text);
      dropQuoteChip();
      getSelection().removeAllRanges();
      el.classList.remove('sn-selectable');
    };
    // Under the selection: above it a phone puts its own Copy/Select menu.
    const w = _qs.offsetWidth || 80;
    const top = Math.min(rect.bottom + 8, window.innerHeight - 120);
    _qs.style.top = Math.max(8, top) + 'px';
    _qs.style.left = Math.max(8, Math.min(rect.left + rect.width / 2 - w / 2, window.innerWidth - w - 8)) + 'px';
  });
  host.parentElement.addEventListener('scroll', dropQuoteChip, { passive: true });

  // ---- @ in the composer lists the agents ----
  // Typing @ opened nothing: the person had to know the agent's handle. Now @ and the letters after it list the agents whose name or
  // handle starts with them, the chat's own agent left out (it reads the chat
  // anyway). Arrows and Enter or Tab pick, a click or tap picks, Esc closes; the
  // pick puts "@handle " in place of what was typed.
  let _at = null;
  const atToken = () => {
    const v = _cmpTa.value, i = _cmpTa.selectionStart;
    if (i !== _cmpTa.selectionEnd) return null;
    const m = /(^|[\s(])@([A-Za-z0-9._-]{0,32})$/.exec(v.slice(0, i));
    return m ? { start: i - m[2].length - 1, end: i, q: m[2].toLowerCase() } : null;
  };
  function atClose() { if (_at) { _at.box.remove(); _at = null; } }
  function atPick(a) {
    const t = atToken();
    if (!t) { atClose(); return; }
    const v = _cmpTa.value;
    const ins = '@' + a.slug + ' ';
    _cmpTa.value = v.slice(0, t.start) + ins + v.slice(t.end);
    const c = t.start + ins.length;
    _cmpTa.setSelectionRange(c, c);
    atClose();
    _cmpTa.focus();
    _cmpTa.dispatchEvent(new Event('input', { bubbles: true }));
  }
  function atPaint() {
    _at.box.querySelectorAll('.sn-at__it').forEach((b, i) => {
      b.classList.toggle('is-on', i === _at.sel);
      if (i === _at.sel) b.scrollIntoView({ block: 'nearest' });
    });
  }
  function atOpen() {
    const t = atToken();
    if (!t || !Array.isArray(agentsList) || !agentsList.length) { atClose(); return; }
    const own = deskSlug();
    const list = agentsList.filter((a) => a.slug !== own && (!t.q
      || a.slug.toLowerCase().startsWith(t.q)
      || String(a.title || '').toLowerCase().startsWith(t.q))).slice(0, 30);
    if (!list.length) { atClose(); return; }
    if (!_at) {
      const box = document.createElement('div');
      box.className = 'sn-at';
      box.setAttribute('role', 'listbox');
      box.addEventListener('mousedown', (e) => e.preventDefault()); // keep the caret in the box
      document.body.appendChild(box);
      _at = { box, list: [], sel: 0 };
    }
    _at.list = list;
    _at.sel = Math.min(_at.sel, list.length - 1);
    _at.box.innerHTML = list.map((a, i) => {
      const av = avInner(avResolve(a.avatar), (a.title || a.slug || '?').trim().charAt(0).toUpperCase());
      return `<button type="button" class="sn-at__it" data-i="${i}" role="option">`
        + `<span class="sn-at__av">${av}</span><span class="sn-at__nm">${esc(a.title || a.slug)}</span>`
        + `<span class="sn-at__sl">@${esc(a.slug)}</span>`
        + (a.role ? `<span class="sn-at__role">${esc(a.role)}</span>` : '') + '</button>';
    }).join('');
    _at.box.querySelectorAll('.sn-at__it').forEach((b) => b.addEventListener('click', () => atPick(_at.list[+b.dataset.i])));
    const r = (document.querySelector('#composer .cmp__row') || _cmpTa).getBoundingClientRect();
    const w = Math.min(360, r.width);
    _at.box.style.width = w + 'px';
    _at.box.style.left = r.left + 'px';
    _at.box.style.bottom = (window.innerHeight - r.top + 6) + 'px';
    atPaint();
  }
  _cmpTa.addEventListener('input', atOpen);
  _cmpTa.addEventListener('click', atOpen);
  _cmpTa.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== _cmpTa) atClose(); }, 150));
  // Capture, so Enter picks and does not send.
  _cmpTa.parentElement.addEventListener('keydown', (e) => {
    if (!_at || e.target !== _cmpTa) return;
    const n = _at.list.length;
    if (e.key === 'ArrowDown') { _at.sel = (_at.sel + 1) % n; atPaint(); }
    else if (e.key === 'ArrowUp') { _at.sel = (_at.sel - 1 + n) % n; atPaint(); }
    else if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') atPick(_at.list[_at.sel]);
    else if (e.key === 'Escape') atClose();
    else return;
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);

  // A message that starts with @colleague is for the named colleagues only
  //. Every agent named in it, the chat's own left out, gets it
  // directly, the same way as a Reply to a colleague; the chat's agent does not.
  // A message that names a colleague later in the text still goes to the chat's
  // agent as before, and the Studio tells the colleague as well.
  const AT_RE = /(?<![\w@./-])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})/g;
  function leadingColleagues(v) {
    const lead = /^\s*@([A-Za-z0-9][A-Za-z0-9._-]{0,63})/.exec(v);
    const own = deskSlug();
    const ok = (n) => { const a = agentBySlug(n); return a && a.slug !== own ? a.slug : null; };
    if (!lead || !ok(lead[1].replace(/\.+$/, '').toLowerCase())) return null;
    const out = [];
    let fenced = false;
    for (const line of v.split('\n')) {
      if (/^\s*```/.test(line)) { fenced = !fenced; continue; }
      if (fenced || /^\s*>/.test(line)) continue;
      for (const m of line.replace(/`[^`]*`/g, '').matchAll(AT_RE)) {
        const slug = ok(m[1].replace(/\.+$/, '').toLowerCase());
        if (slug && !out.includes(slug)) out.push(slug);
      }
    }
    return out.slice(0, 5);
  }
  const namesOf = (list) => {
    const n = list.map(nameOf);
    return n.length < 2 ? n.join('') : n.slice(0, -1).join(', ') + ' and ' + n[n.length - 1];
  };
  const _send0 = sendChat;
  sendChat = async function () {
    const r = _reply;
    if (!r || r.deskId !== activeId) {
      clearReply();
      const to = leadingColleagues(_cmpTa.value);
      if (!to || !to.length) return _send0();
      if (cmpUploading()) return;
      const { message } = cmpCompose();
      if (!message) return;
      return sendDirect(activeId, message, message, to);
    }
    const quote = '> ' + (r.name || nameOf(r.who)) + ': ' + r.excerpt + '\n\n';
    if (r.who === deskSlug()) {
      if (!_cmpTa.value.trim()) return;
      clearReply();
      _cmpTa.value = quote + _cmpTa.value;
      return _send0();
    }
    if (cmpUploading()) return;
    const { message } = cmpCompose();
    if (!message) return;
    clearReply();
    return sendDirect(activeId, quote + message, message, [r.who]);
  };
  async function sendDirect(deskId, text, message, to) {
    cmpReset();
    draftForget(deskId);
    // The same room for the answer as any sent message: it glides up, and the
    // space under it is kept for the reply.
    _room = { desk: null, h: 0 };
    keepRoom();
    _tailDesk = deskId;
    const el = bubble('user', userHtml(text, deskId), { arrive: true });
    const note = document.createElement('div');
    note.className = 'sn-steer-status';
    note.textContent = 'Sending to ' + namesOf(to) + '...';
    if (el) el.appendChild(note);
    try {
      const resp = await fetch(`/api/desks/${encodeURIComponent(deskId)}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ agents: to, message: text }),
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const told = ((await resp.json().catch(() => ({}))).agents) || to;
      note.textContent = 'Sent to ' + namesOf(told) + ' only';
      await waitFor(deskId, told[0]);
    } catch (e) {
      note.textContent = 'Could not send to ' + namesOf(to) + ' (' + e.message + '); the text is back in the box';
      _cmpTa.value = message;
      cmpGrow();
    }
  }
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(paint); } };
  // /next2: app.js calls this right after it rebuilds the thread, so the heads
  // and day lines are there in the same step and the thread is never shown bare
  // for a frame.
  window.snAuthorsPaint = () => { queued = true; paint(); };
  new MutationObserver(schedule).observe(host, { childList: true, subtree: true, characterData: true });
  schedule();
})();
