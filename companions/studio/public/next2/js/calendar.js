// calendar.js: everything on the clock (/next2, ported from /next). Today only
// the agents' reminders (core GET /api/reminders); task due dates and connected
// calendars join here when their modules do. A month grid with the reminders in
// their days and the chosen day's list under it. Loaded after app.js, uses its top-level names (setView,
// snPath, agentBySlug, avInner, avResolve, showPop, closePop, esc).
(() => {
  const body = document.getElementById('calBody');
  const nav = document.getElementById('navCalendar');
  if (!body || !nav || typeof setView !== 'function') return;
  const REPEAT = { day: 'every day', week: 'every week', month: 'every month' };
  const CHEV = (d) => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${d}"/></svg>`;
  let rems = [];
  const today = () => new Date(new Date().toDateString());
  let month = new Date(today().getFullYear(), today().getMonth(), 1);   // the month on screen
  let picked = today();                                                  // the day in the drawer
  let drawerOn = false;
  // the picked day's reminders open in a drawer on the right, not as a list
  // under the grid; it lives in the calendar's section, so
  // leaving the calendar hides it with the section
  const view = body.closest('section') || body.parentElement;
  const drawer = document.createElement('aside');
  drawer.className = 'sn-drawer';
  drawer.innerHTML = '<div class="sn-drawer__head"><h3></h3><button type="button" class="sn-drawer__x" data-close aria-label="Close">&times;</button></div><div class="sn-drawer__body"></div>';
  view.appendChild(drawer);

  async function load() {
    try {
      // all, not only the scheduled ones: a reminder that went off stays in its
      // day, as a past event does in a calendar
      const r = await fetch('/api/reminders?status=all', { headers: { Accept: 'application/json' } });
      rems = r.ok ? ((await r.json()).reminders || []).filter((x) => x.status !== 'cancelled') : [];
    } catch { rems = []; }
    paint();
  }
  const dayKey = (d) => d.toDateString();
  // every time a reminder comes due between from and to: once, or stepping by its
  // repeat from its next due time (core moves due_at forward as it fires)
  function occurrences(r, from, to) {
    const out = [];
    // the last time it went off; for a repeating one core keeps only that one
    if (r.sent_at) { const s = new Date(r.sent_at); if (s >= from && s <= to) out.push(s); }
    if (r.status !== 'scheduled') return out;
    let t = new Date(r.due_at);
    for (let i = 0; t <= to && i < 400; i++) {
      if (t >= from) out.push(new Date(t));
      if (!r.repeat) break;
      t = r.repeat === 'day' ? new Date(t.getFullYear(), t.getMonth(), t.getDate() + 1, t.getHours(), t.getMinutes())
        : r.repeat === 'week' ? new Date(t.getFullYear(), t.getMonth(), t.getDate() + 7, t.getHours(), t.getMinutes())
        : new Date(t.getFullYear(), t.getMonth() + 1, t.getDate(), t.getHours(), t.getMinutes());
    }
    return out;
  }
  const hm = (d) => d.toTimeString().slice(0, 5);
  function row(r, at) {
    const a = agentBySlug(r.agent_slug) || { slug: r.agent_slug, title: r.agent_slug };
    const name = a.title || a.slug || '';
    const av = avInner(a.avatar ? avResolve(a.avatar) : null, name.trim().charAt(0).toUpperCase());
    // time first, as a calendar reads; then what and who reminds whom ("Twine"
    // under the text alone did not say who reminds whom)
    const past = at <= new Date();
    // and who made it: the agent, asked in a chat, or a person directly (core-api
    // keeps the principal in created_by)
    const by = !r.created_by ? '' : r.created_by === r.agent_slug ? 'set in a chat'
      : r.created_by === meIdentity.user ? 'added by you' : 'added by ' + r.created_by;
    return `<div class="calv__row${past ? ' is-past' : ''}"><time>${esc(hm(at))}</time>`
      + `<span class="calv__t"><b>${esc(r.text)}</b><small>${esc(name)} ${r.recipient === 'agent' ? (past ? 'was to do this' : 'will do this') : (past ? 'reminded you' : 'will remind you')}${r.repeat ? ', ' + esc(REPEAT[r.repeat] || r.repeat) : ''}${by ? ' · ' + esc(by) : ''}</small></span>`
      + `<span class="calv__av" title="${esc(name)}">${av}</span>`
      + (past ? '' : `<button type="button" class="calv__x" data-cancel="${esc(String(r.id))}" title="Cancel this reminder" aria-label="Cancel this reminder">&times;</button>`) + '</div>';
  }
  function paint() {
    // the grid: six weeks from the Monday on or before the 1st, every month, so
    // the page keeps its height (4, 5 or 6 rows made it jump)
    const first = new Date(month), lead = (first.getDay() + 6) % 7;
    const start = new Date(first.getFullYear(), first.getMonth(), 1 - lead);
    const weeks = 6;
    const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + weeks * 7);
    const byDay = {};
    for (const r of rems) for (const at of occurrences(r, start, end)) (byDay[dayKey(at)] = byDay[dayKey(at)] || []).push({ r, at });
    Object.values(byDay).forEach((l) => l.sort((x, y) => x.at - y.at));
    const t0 = dayKey(today()), pk = dayKey(picked);
    let cells = '';
    for (let i = 0; i < weeks * 7; i++) {
      const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i), k = dayKey(d), its = byDay[k] || [];
      const cls = ['calm__d', d.getMonth() !== month.getMonth() ? 'is-out' : '', k === t0 ? 'is-today' : '', k === pk ? 'is-picked' : ''].filter(Boolean).join(' ');
      cells += `<button type="button" class="${cls}" data-day="${d.getFullYear()}-${d.getMonth()}-${d.getDate()}">`
        + `<span class="calm__n">${d.getDate()}</span>`
        + its.slice(0, 2).map(({ r, at }) => `<span class="calm__it${at <= new Date() ? ' is-past' : ''}"><time>${esc(hm(at))}</time> ${esc(r.text)}</span>`).join('')
        + (its.length > 2 ? `<span class="calm__more">+${its.length - 2}</span>` : '')
        + (its.length ? '<span class="calm__dot"></span>' : '') + '</button>';
    }
    const title = month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    const heads = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((w) => `<span>${w}</span>`).join('');
    const list = byDay[pk] || [];
    const dayTitle = k0 => (k0 === t0 ? 'Today, ' : '') + picked.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
    body.innerHTML = `<div class="calm__bar"><h3>${esc(title)}</h3>`
      + `<button type="button" class="sn-btn--bare" data-step="-1" aria-label="Previous month">${CHEV('M15 6l-6 6 6 6')}</button>`
      + `<button type="button" class="sn-btn--bare" data-step="1" aria-label="Next month">${CHEV('M9 6l6 6-6 6')}</button>`
      + `<button type="button" class="sn-btn sn-btn--ghost calm__today" data-today>Today</button></div>`
      + `<div class="calm__heads">${heads}</div><div class="calm__grid">${cells}</div>`;
    // the drawer: the day's reminders, and for today or a later day a way to add
    // one, as a click on a day in Google's calendar opens a new event (interface
    // map, 2026-10-30: an empty day did nothing). Only reminders: nothing else
    // on this node has a date yet.
    const canAdd = picked >= today() && agentsList.length;
    if (!list.length && !canAdd) drawerOn = false;
    drawer.classList.toggle('is-open', drawerOn);
    if (drawerOn) {
      drawer.querySelector('h3').textContent = dayTitle(pk);
      const host = drawer.querySelector('.sn-drawer__body');
      const rows = list.map(({ r, at }) => row(r, at)).join('');
      // a live repaint must not wipe what the person is typing into the form
      const form = host.querySelector('[data-add]');
      if (form && canAdd && form.dataset.for === pk) host.querySelector('[data-rows]').innerHTML = rows;
      // the form first: under a busy day's list it was screens away
      else host.innerHTML = (canAdd ? addForm() : '') + `<div data-rows>${rows}</div>`;
    }
  }
  // a new reminder on the picked day: the agent that will remind you in its chat
  // (core picks that agent's latest chat), the time on your clock
  function addForm() {
    const now = new Date();
    const at = dayKey(picked) === dayKey(now) ? new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + 1) : new Date(0, 0, 0, 9);
    const opts = agentsList.map((a) => `<option value="${esc(a.slug)}">${esc(a.title || a.slug)}</option>`).join('');
    return `<form class="calv__add" data-add data-for="${esc(dayKey(picked))}"><b>New reminder</b>`
      + `<label class="sn-field"><input name="text" placeholder="What to remind you of" required maxlength="2000"></label>`
      + `<div class="calv__add-row"><label class="sn-field"><input name="time" type="time" value="${hm(at)}" required></label>`
      + `<label class="sn-field"><select name="repeat" aria-label="Repeat"><option value="">Once</option><option value="day">Every day</option><option value="week">Every week</option><option value="month">Every month</option></select></label></div>`
      + `<label class="sn-field"><select name="agent" aria-label="Who reminds you">${opts}</select></label>`
      + `<p class="calv__add-err" hidden></p>`
      + `<button type="submit" class="sn-btn sn-btn--primary">Add reminder</button></form>`;
  }
  view.addEventListener('submit', async (e) => {
    const f = e.target.closest('[data-add]');
    if (!f) return;
    e.preventDefault();
    const [h, m] = f.time.value.split(':').map(Number);
    const at = new Date(picked.getFullYear(), picked.getMonth(), picked.getDate(), h, m);
    const err = f.querySelector('.calv__add-err');
    const btn = f.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      const r = await fetch('/api/reminders', { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ text: f.text.value.trim(), at: at.toISOString(), repeat: f.repeat.value || null, agent: f.agent.value }) });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || 'Could not add the reminder'); }
      f.reset(); err.hidden = true; btn.disabled = false;
      load();
    } catch (x) { err.textContent = x.message; err.hidden = false; btn.disabled = false; }
  });
  function open(push) {
    if (push) history.pushState({}, '', '/calendar');
    setView('calendar');
    load();
  }
  nav.addEventListener('click', () => open(true));
  view.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) { drawerOn = false; return paint(); }
    const st = e.target.closest('[data-step]');
    if (st) { month = new Date(month.getFullYear(), month.getMonth() + Number(st.dataset.step), 1); return paint(); }
    if (e.target.closest('[data-today]')) { picked = today(); month = new Date(picked.getFullYear(), picked.getMonth(), 1); return paint(); }
    const dy = e.target.closest('[data-day]');
    if (dy) {
      const [y, m, d] = dy.dataset.day.split('-').map(Number);
      picked = new Date(y, m, d);
      if (m !== month.getMonth()) month = new Date(y, m, 1);   // a day of the next or last month opens that month
      drawerOn = true;
      return paint();
    }
    // Cancel asks first, in a window in the middle (one stray click once
    // cancelled a reminder, 2026-10-29).
    const b = e.target.closest('[data-cancel]');
    if (!b) return;
    const r = rems.find((x) => String(x.id) === b.dataset.cancel);
    if (!r) return;
    const when = new Date(r.due_at).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const pop = showPop(document.body, `<div class="pop__q">Cancel this reminder?</div>
      <div class="pop__note">${esc(r.text)}, ${esc(when)}${r.repeat ? ', ' + esc(REPEAT[r.repeat] || r.repeat) : ''}</div>
      <div class="pop__row"><button class="pop__btn" type="button" data-no>Keep it</button><button class="pop__btn pop__btn--danger" type="button" data-yes>Cancel reminder</button></div>`, 'pop--modal');
    pop.querySelector('[data-no]').addEventListener('click', closePop);
    pop.querySelector('[data-yes]').addEventListener('click', async () => {
      closePop();
      try { await fetch(`/api/reminders/${encodeURIComponent(r.id)}`, { method: 'DELETE' }); } catch {}
      load();
    });
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && drawerOn) { drawerOn = false; paint(); } });
  // the open calendar follows the bus: a reminder set, fired or cancelled anywhere
  // shows at once, and a dropped stream reads it again
  const onView = () => view.offsetParent !== null;
  document.addEventListener('sn:event', (e) => { if (String((e.detail || {}).type).startsWith('reminder.') && onView()) soon('calendar', load); });
  document.addEventListener('sn:resync', () => { if (onView()) load(); });
  // a reload or a link on /calendar opens it
  if (snPath() === '/calendar') setTimeout(() => open(false), 0);
  window.addEventListener('popstate', () => { if (snPath() === '/calendar') open(false); });
})();
