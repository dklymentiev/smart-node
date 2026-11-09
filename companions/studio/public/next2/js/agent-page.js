// agent-page.js: one agent's page in /next2. The owner's minimum (2026-10-30):
// see its channels and add one, see what it remembers, set it up. Four tabs:
//  - Channels: the Studio and the channels it is reached on;
//  - Knowledge: what its instructions are built from (job text, rules,
//    documents) and the whole text it reads, as the production Knowledge tab;
//  - Memory: what it remembered from conversations;
//  - Settings: avatar, name, role, model, which memory.
// What it is doing now is one line under its name. Everything that is for
// finding out why (its queue, tasks, messages with colleagues, the event log,
// adding a channel, the services it may use, the node's rules, its tools) is on a Debug tab that
// DEBUG hides.
// Chats with the agent are in the chat list and the chat's side card. Built
// from the kit's page grammar (ui/components.css: sn-page, sn-section,
// sn-group, sn-row, sn-tiles, sn-empty), as the mockup the owner approved
// (the 2026-10-30 mockup). Loaded after app.js; replaces its
// openAgentPage, so every link and route lands here.
(() => {
  const root = document.getElementById('agentPage');
  if (!root || typeof openAgentPage !== 'function') return;
  const DEBUG = true;   // false hides the Debug tab and its address
  const TABS = [['channels', 'Channels'], ['knowledge', 'Knowledge'], ['memory', 'Memory'], ['settings', 'Settings']].concat(DEBUG ? [['debug', 'Debug']] : []);
  // earlier tab names, still in links and old addresses
  const dbg = DEBUG ? 'debug' : 'channels';
  const OLD = { knows: 'knowledge', access: 'channels', setup: 'settings', overview: dbg, mail: dbg, chats: dbg, activity: dbg, tasks: dbg };
  const S = { slug: null, tab: 'channels', open: null, memQ: '', forgetAll: false, chAdd: false };
  let D = {};       // what was read for the agent on screen

  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { Accept: 'application/json', ...(opts.body ? { 'Content-Type': 'application/json' } : {}) }, ...opts });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || d.error || 'HTTP ' + r.status);
    return d;
  };
  const enc = encodeURIComponent;
  const installed = (m) => !window.snInstalled || window.snInstalled.has(m);
  const nm = (slug) => { const a = agentBySlug(slug); return (a && (a.title || a.slug)) || slug || ''; };
  const when = (iso) => { const d = new Date(iso); if (isNaN(d)) return '';
    return d.toDateString() === new Date().toDateString() ? d.toTimeString().slice(0, 5) : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); };
  const whenFull = (iso) => { const d = new Date(iso); if (isNaN(d)) return '';
    const days = Math.round((new Date(d.toDateString()) - new Date(new Date().toDateString())) / 864e5), tm = d.toTimeString().slice(0, 5);
    return days === 0 ? `today ${tm}` : days === 1 ? `tomorrow ${tm}` : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${tm}`; };
  const deskTitle = (id) => { const d = id && desks.find((x) => x.id === id); return d ? d.title || 'a chat' : ''; };
  // "user:admin" reads "admin", "agent:nora" reads "Nora"
  const who = (actor) => { const s = String(actor || ''); const i = s.indexOf(':');
    return s.startsWith('agent:') ? nm(s.slice(6)) : i > 0 ? s.slice(i + 1) : s; };

  // ---- the kit's page grammar ----
  const sec = (title, body, extra = '') => `<section class="sn-section"><h2 class="sn-section__title">${title}${extra}</h2>${body}</section>`;
  const group = (rows) => `<div class="sn-group">${rows.join('')}</div>`;
  const empty = (t) => `<div class="sn-empty">${esc(t)}</div>`;
  const ava = (slug, cls = '', work = false) => { const a = agentBySlug(slug) || { slug };
    return `<span class="sn-ava ${cls}">${avInner(a.avatar ? avResolve(a.avatar) : null, nm(slug).charAt(0).toUpperCase())}${work ? '<i class="sn-ava__st"></i>' : ''}</span>`; };
  // a row: a lead (avatar, icon), a title, one line under it, then what stands at its end
  // a tool's description is written for the model; a person reads its first sentence
  const firstSentence = (s) => { s = String(s || '').replace(/\s+/g, ' ').trim(); const i = s.search(/[.!?] /); return i < 0 ? s : s.slice(0, i + 1); };
  function row({ lead = '', title, sub = '', meta = '', end = '', attrs = '', tag = 'div', chev = false, wrap = false, wrapTitle = false }) {
    return `<${tag} class="sn-row"${attrs}>${lead}<span class="sn-row__main"><span class="sn-row__title${wrapTitle ? ' sn-row__title--wrap' : ''}">${esc(title)}</span>`
      + (sub ? `<span class="sn-row__sub${wrap ? ' sn-row__sub--wrap' : ''}">${esc(sub)}</span>` : '') + '</span>'
      + (meta ? `<span class="sn-row__meta">${esc(meta)}</span>` : '') + end + (chev ? '<i class="sn-row__chev"></i>' : '') + `</${tag}>`;
  }
  const xBtn = (attr, id, label) => `<button class="sn-btn sn-btn--icon sn-btn--ghost" ${attr}="${esc(String(id))}" title="${label}" aria-label="${label}">&times;</button>`;
  const toast = (t) => window.snToast(t);   // the kit's toast, from prefix.js
  const more = (attr, on, label) => `<button type="button" ${attr}>${on ? 'Show less' : label}</button>`;

  async function loadLive(slug) {
    const p = `/api/agents/${enc(slug)}`;
    const [tl, con, q, mail] = await Promise.all([api(`${p}/timeline`).catch(() => ({})), api(`${p}/console`).catch(() => ({})),
      api(`${p}/queue`).catch(() => ({})), api(`${p}/mail`).catch(() => ({}))]);
    if (slug !== S.slug) return;
    Object.assign(D, { tl: tl.events || [], con, queue: q.waiting || [], mail: mail.threads || [] });
  }
  async function loadDetails(slug) {
    const p = `/api/agents/${enc(slug)}`, none = (k) => () => ({ [k]: [] });
    const [agent, know, kinds, mem, pol, grants, tools, integ, chans, models, tasks] = await Promise.all([api(p).catch(() => ({})),
      api(`${p}/knowledge`).catch(none('knowledge')), api('/api/knowledge/kinds').catch(none('kinds')),
      api(`${p}/remembers`).catch((e) => ({ items: [], error: e.message })), api(`${p}/policies`).catch(none('policies')),
      api(`${p}/grants`).catch(() => ({ grants: {} })), api(`${p}/tools`).catch(none('tools')),
      api('/api/integrations').catch(none('integrations')), api(`${p}/channels`).catch(none('channels')),
      api('/api/models').catch(none('models')),
      installed('tracking') ? api(`/api/tasks?assignee=${enc(slug)}&status=open`).catch(none('tasks')) : { tasks: [] }]);
    if (slug !== S.slug) return;
    Object.assign(D, { agent, know: know.knowledge || [], kinds: kinds.kinds || [], mem, pol: pol.policies || [], grants: grants.grants || {},
      tools: tools.tools || [], integ: integ.integrations || [], chans: chans.channels || [], models: models.models || [],
      tasks: tasks.tasks || tasks.items || [], details: true });
  }

  // ---- Overview ----
  function nowHtml() {
    const turn = ((D.con || {}).turns || [])[0];
    if (!turn) return empty('Free. Waiting for the next request.');
    const step = (turn.steps || []).slice(-1)[0];
    const doing = turn.action ? doingWords(turn.action + (turn.detail ? ': ' + turn.detail : '')) : step ? plainStep(step.text) : 'Thinking';
    const conv = String(turn.conversation || ''), desk = conv.includes(':desk:') ? conv.split(':desk:')[1] : '';
    // a wake for a chat runs outside the chat; the message it took is the seen one
    const taken = (D.queue || []).find((x) => x.seen && x.what === 'chat');
    const where = desk ? `In “${deskTitle(desk) || 'a chat'}”` : taken ? `In “${taken.chat || 'a chat'}”` : 'On its messages';
    const to = desk || (taken && taken.desk) || '';
    // what it did a moment ago, newest first, so the running turn reads as a short log
    const recent = (turn.steps || []).slice(-9, -1).reverse().filter((x) => x && x.text)
      .map((x) => row({ title: plainStep(x.text), sub: '' }));
    return group([row({ lead: ava(S.slug, 'sn-ava--s', true), title: doing, sub: where + (to ? '. Open the chat to stop it' : ''),
      tag: to ? 'button' : 'div', attrs: to ? ` type="button" data-desk="${esc(to)}"` : '', chev: !!to })].concat(recent));
  }
  function queueHtml() {
    const items = (D.queue || []).slice().sort((x, y) => ((y.what === 'chat') - (x.what === 'chat')) || (x.id - y.id));
    if (!items.length) return empty('Nothing waits.');
    const label = (x) => x.what === 'chat' ? `Chat “${x.chat || 'a chat'}”` : x.what === 'reminder' ? 'A reminder'
      : x.what === 'message' ? `Message from ${nm(x.from)}` : `${x.what} from ${nm(x.from)}`;
    return group(items.slice(0, 8).map((x) => row({ title: label(x), sub: x.seen ? 'Taken, it is on it' : '', tag: x.desk ? 'button' : 'div',
      attrs: x.desk ? ` type="button" data-desk="${esc(x.desk)}"` : '', chev: !!x.desk })));
  }
  // its messages with colleagues: one row per colleague, the newest line; a row
  // opens the conversation in place. Colleagues only: the chat relays
  // (web:owner, desk:<id>) are the chats themselves.
  const threads = () => (D.mail || []).filter((t) => t.counterpart === S.slug || agentBySlug(t.counterpart));
  function mailHtml() {
    const th = threads();
    if (!th.length) return empty('No messages with colleagues yet.');
    return group(th.slice(0, 12).map((t, i) => {
      const self = t.counterpart === S.slug, open = S.open === i;
      const line = String(t.snippet || '').replace(/\s+/g, ' ').trim();
      const body = open ? `<div class="agp-thread">${(t.messages || []).slice(-30).map((m) => `<div class="agp-tmsg${m.sender === S.slug ? ' is-own' : ''}">`
        + `<small>${esc(nm(m.sender))}, ${esc(when(m.received_at))}</small><p>${esc(m.content || '')}</p></div>`).join('')}</div>` : '';
      return row({ lead: ava(self ? S.slug : t.counterpart, 'sn-ava--s'), title: self ? 'Notes to itself' : nm(t.counterpart),
        sub: (t.last_from === S.slug && !self ? nm(S.slug) + ': ' : '') + line, meta: when(t.last_at), tag: 'button',
        attrs: ` type="button" data-thread="${i}" aria-expanded="${open}"`, chev: true }) + body;
    }));
  }
  // what it did: a line says what, the line under it says why or where
  function eventWords(e) {
    // a reminder arrives wrapped in the line the model needs ("Reminder due now,
    // which the person asked for ... 22:50: <text>"); a person needs the text
    const pv = reminderText(String(e.preview || '').replace(/\s+/g, ' ').trim());
    const x = e.type || '';
    if (x.startsWith('auth.') || x === 'agent.turn_started' || x === 'agent.turn_completed' || x === 'agent.woken') return null;
    if (x === 'tool.invoked') return { t: toolDoing(e.op || 'a tool') };
    if (x === 'tool.failed') return { t: `Could not: ${toolDoing(e.op || 'a tool')}`, s: e.error || '' };
    if (x === 'message.inbound') {
      if (e.from === e.to) return { t: 'A reminder came due', s: pv };
      return e.from === S.slug ? { t: `Wrote to ${nm(e.to)}`, s: pv } : { t: `${nm(e.from)} wrote`, s: pv };
    }
    if (x === 'message.sent') return { t: `Sent a message${e.to ? ' to ' + nm(e.to) : ''}`, s: pv };
    if (x === 'agent.turn_stopped') return { t: e.via === 'desk' ? 'Stopped answering in a chat' : 'Stopped', s: e.stopped_by ? `Stopped by ${who(e.stopped_by)}` : '' };
    if (x === 'agent.turn_cancelled') return { t: 'A turn was cancelled', s: 'It ended before an answer' };
    if (x === 'agent.wake_failed') return { t: 'Could not wake up', s: [/^wake \d+$/.test(e.reason || '') ? 'the runner returned an error' : e.reason, e.retry_in ? `tries again in ${e.retry_in} s` : ''].filter(Boolean).join(', ') };
    if (x === 'reminder.set') return { t: 'Set a reminder', s: e.due_at ? `For ${whenFull(e.due_at)}` : '' };
    if (x === 'reminder.fired') return { t: 'A reminder came due', s: pv };
    return { t: x.replace(/[._]/g, ' ') };
  }
  function historyRows() {
    const rows = [];
    for (const e of (D.tl || [])) {   // newest first, as the timeline gives it
      // a change of access says what it was about: one save writes an event per
      // tool, so they fold into one line that names them ("grant revoked x11"
      // said nothing)
      if (/^grant\./.test(e.type || '')) {
        const verb = e.type === 'grant.revoked' ? 'Access removed' : 'Access given', last = rows[rows.length - 1];
        const what = e.tool ? toolDoing(e.tool) : '';
        if (last && last.verb === verb && Math.abs(Date.parse(last.ts) - Date.parse(e.ts)) < 60000) { if (what) last.tools.push(what); continue; }
        rows.push({ verb, tools: what ? [what] : [], s: e.actor ? `Changed by ${who(e.actor)}` : '', ts: e.ts, n: 1 });
      } else {
        const w = eventWords(e);
        if (!w) continue;
        const last = rows[rows.length - 1];
        if (last && last.t === w.t && last.s === (w.s || '')) { last.n++; continue; }
        rows.push({ t: w.t, s: w.s || '', ts: e.ts, n: 1 });
      }
      if (rows.length >= 40) break;
    }
    for (const r of rows) if (r.verb) {
      const t = r.tools;
      r.t = r.verb + (t.length ? ': ' + (t.length > 2 ? t.slice(0, 2).join(', ') + ` and ${t.length - 2} more` : t.join(', ')) : '');
    }
    return rows;
  }
  function tasksHtml() {
    if (!installed('tracking') || !D.details) return '';
    const ts = D.tasks || [];
    return sec('Open tasks', ts.length ? group(ts.slice(0, 8).map((t) => row({ title: t.title || '#' + t.id, meta: '#' + t.id }))) : empty('No open tasks.'));
  }
  // ---- the line under its name: what it is doing now, a link to that chat ----
  function nowLine() {
    const turn = ((D.con || {}).turns || [])[0];
    if (!turn) return null;
    const step = (turn.steps || []).slice(-1)[0];
    const doing = turn.action ? doingWords(turn.action + (turn.detail ? ': ' + turn.detail : '')) : step ? plainStep(step.text) : 'Thinking';
    const conv = String(turn.conversation || ''), desk = conv.includes(':desk:') ? conv.split(':desk:')[1] : '';
    const taken = (D.queue || []).find((x) => x.seen && x.what === 'chat');
    const to = desk || (taken && taken.desk) || '', chat = desk ? deskTitle(desk) : taken ? taken.chat : '';
    return { t: doing + (chat ? ` in “${chat}”` : ''), to };
  }

  // ---- Channels ----
  // a channel lives in a module; without the module it is shown, not offered
  const CHANNEL_NEEDS = { voice: 'voice' }, NEEDS_NAME = { communication: 'Communication', voice: 'Voice' };
  const ADDRESS = { email: 'name@example.com', telegram: '@your_bot', slack: 'Slack channel ID', webchat: 'Site address' };
  const noAddress = (c) => c.channel_slug === 'voice';
  const connected = () => D.chans.filter((c) => c.bound && (c.instance_ref || noAddress(c)));
  function channels() {
    return sec('Channels', group([row({ title: 'Studio', sub: 'Chats on this node. Always on' })].concat(connected().map((c) => row({ tag: 'label',
      title: c.channel_label, sub: c.instance_ref || 'On',
      end: `<span class="sn-switch"><input type="checkbox" role="switch" ${c.enabled ? 'checked' : ''} data-chan="${esc(c.channel_slug)}" data-ref="${esc(c.instance_ref || '')}"></span>` })))));
  }
  // Add channel opens the rest of the catalogue: what can be connected gets an
  // address and Connect, the others say why not. On Debug until a connect has
  // been seen working on a node with the Communication module
  function addChannel() {
    const need = (c) => CHANNEL_NEEDS[c.channel_slug] || 'communication';
    const on = connected(), rest = D.chans.filter((c) => !on.includes(c));
    const pick = S.chAdd ? group(rest.map((c) => {
      if (c.status === 'planned') return row({ title: c.channel_label, sub: 'Planned' });
      if (!installed(need(c))) return row({ title: c.channel_label, sub: `Needs the ${NEEDS_NAME[need(c)] || need(c)} module` });
      return row({ title: c.channel_label, end: (noAddress(c) ? '' : `<span class="sn-field agp-fieldw"><input id="agpCh-${esc(c.channel_slug)}" placeholder="${esc(ADDRESS[c.channel_slug] || 'Address')}"></span>`)
        + `<button class="sn-btn sn-btn--ghost" data-chconnect="${esc(c.channel_slug)}">Connect</button>` });
    })) : '';
    return rest.length ? sec('Add a channel', `<div class="agp-acts"><button class="sn-btn sn-btn--ghost" data-chadd>${S.chAdd ? 'Close' : 'Add channel'}</button></div>` + pick) : '';
  }

  // ---- Knowledge: everything its instructions are built from, as the
  // production page's Knowledge tab ----
  // which memory it is: the runtime's own, the node's, or an external store, as
  // the node names it (GET /remembers: store, runtime)
  const keptBy = () => { const m = D.mem || {};
    return m.store === 'runtime' ? `Runtime built-in (${m.runtime})` : m.store === 'node' ? 'Node memory' : m.store ? `External (${m.store})`
      : `Runtime built-in (${m.runtime || 'unknown'}), not readable by the node`; };
  function knowledge() {
    const can = (k) => (D.kinds.find((x) => x.kind === k) || {}).available;
    const KIND = { text: 'Text', file: 'File', gdrive_file: 'Drive file', doci: 'Document' };
    // the job text is a specialist's: every agent on it shares the edit
    const instr = sec('Instructions', `<label class="sn-field"><textarea id="agpInstr" class="agp-instr" placeholder="Who it is and how it works, in your words">${esc((D.agent || {}).specialist_body || '')}</textarea></label>`
);
    // the node's rules; a dot says it follows one, a click turns it on or off for this agent
    const pol = D.pol || [];
    const rules = sec(`Rules${pol.length ? ` <small>${pol.filter((p) => p.applied).length} of ${pol.length}</small>` : ''}`, pol.length
      ? `<div class="agp-tags">${pol.map((p) => `<button type="button" class="sn-tag${p.applied ? ' sn-tag--on' : ' is-off'}" data-policy="${esc(p.slug)}" title="${esc(p.body_md || p.title || p.slug)}">${esc(p.title || p.slug)}</button>`).join('')}</div>`
        // the rules themselves are the node's, written in the node panel
        + `<p class="agp-note">A green dot: it follows the rule. Click a rule to turn it on or off for this agent. `
        + `<a href="${esc(location.protocol + '//' + location.hostname.replace(/^studio\./, 'admin.') + '/policies/')}" target="_blank" rel="noopener">Add or edit rules in Admin</a></p>`
      : empty('No rules.'));
    const docs = sec('Documents', (D.know.length ? group(D.know.map((k) => row({ title: k.title || k.ref || k.kind,
        sub: `${KIND[k.kind] || 'Item'}. ${k.kind === 'text' ? String(k.content || '').replace(/\s+/g, ' ').slice(0, 140) : k.ref || ''}`,
        end: xBtn('data-unknow', k.id, 'Remove') })))
      : empty('Texts and files it should know.'))
      + (can('text') && S.adding ? `<div class="agp-form"><label class="sn-field"><input id="agpKnTitle" placeholder="Title, for example: How we write reports"></label>`
        + `<label class="sn-field"><textarea id="agpKnText" placeholder="The text it should keep in mind"></textarea></label></div>` : '')
      + `<div class="agp-acts">${can('text') ? `<button class="sn-btn ${S.adding ? 'sn-btn--primary' : 'sn-btn--ghost'}" data-knadd>${S.adding ? 'Save text' : 'Add text'}</button>` : ''}`
      // a file's text is kept in the Memory module; without it the file would be stored and never read
      + `${can('file') && installed('memory') ? '<button class="sn-btn sn-btn--ghost" data-knfile>Add file</button><input type="file" id="agpKnFile" hidden>' : ''}</div>`);
    // Setup is what you edit; Preview is the whole text it reads, built by the
    // node from that plus its name, team and tools. Read only: a hand edit there
    // would be overwritten
    const views = `<div class="agp-sub"><div class="sn-seg">${[['setup', 'Setup'], ['preview', 'Preview']].map(([id, t]) =>
      `<button type="button" data-knview="${id}" class="${(S.full ? 'preview' : 'setup') === id ? 'is-on' : ''}">${t}</button>`).join('')}</div></div>`;
    if (S.full) return views + '<p class="agp-note">The whole text it reads. The node builds it from Setup, its name, its team and its tools.</p>'
      + (D.full == null ? window.snSkelRows(6, false) : `<pre class="agp-full">${esc(D.full || 'No instructions file yet.')}</pre>`);
    return views + instr + rules + docs;
  }

  // ---- Memory: what it learned by itself ----
  function memory() {
    // the newest five, the rest on request (a hundred lines in one ribbon)
    const m = D.mem || {}, q = S.memQ.toLowerCase(), all = m.items || [], items = all.filter((x) => !q || String(x.text).toLowerCase().includes(q));
    const shown = S.memAll || q ? items : items.slice(0, 5), T = 'Remembered from conversations';
    return m.error ? sec(T, empty(`Could not read its memory: ${m.error}`))
      : m.supported === false ? sec(T, empty(`Not shown for agents on ${m.runtime || 'this runtime'}: the node reads only Flint's built-in memory so far.`))
      : sec(`${T}${all.length ? ` <small>${all.length}</small>` : ''}`,
        (all.length ? `<label class="sn-field agp-find"><input id="agpMemQ" placeholder="Find in what it remembers" value="${esc(S.memQ)}"></label>` : '')
        + (shown.length ? group(shown.map((x) => row({ title: x.text, wrapTitle: true, sub: `${x.kind === 'trait' ? 'Noticed' : 'Learned'} ${when(String(x.at).replace(' ', 'T') + 'Z')}`,
          end: xBtn('data-forget', x.id, 'Forget this') })))
          : empty(q ? 'Nothing matches.' : 'It remembers nothing yet. What you tell it in chats shows here.'))
        + (all.length ? `<div class="agp-acts"><button class="sn-btn sn-btn--ghost" data-forgetall>${S.forgetAll ? 'Sure? Click again to forget everything' : 'Forget everything'}</button></div>` : ''),
        !q && items.length > 5 ? more('data-memall', S.memAll, `Show all ${items.length}`) : '');
  }

  // ---- Settings ----
  function settings() {
    const a = agentBySlug(S.slug) || {};
    const field = (id, v, ph = '') => `<span class="sn-field agp-fieldw"><input id="${id}" value="${esc(v)}" placeholder="${esc(ph)}"></span>`;
    const models = D.models.filter((m) => m.modality === 'chat').map((m) => `<option value="${esc(m.id)}" ${m.id === a.model ? 'selected' : ''}>${esc(m.label || m.id)}</option>`).join('');
    return sec('Profile', group([
      row({ lead: ava(S.slug), title: 'Avatar', end: '<button class="sn-btn sn-btn--ghost" data-avatar>Change</button>' }),
      row({ tag: 'label', title: 'Name', end: field('agpTitle', a.title || '') }),
      row({ tag: 'label', title: 'Role', end: field('agpRole', a.role || '', 'What it does, in a few words') }),
      row({ tag: 'label', title: 'Model', sub: 'What it thinks with', end: `<span class="sn-field agp-fieldw"><select id="agpModel">${models}</select></span>` }),
      // which memory it uses. One choice exists today, so nothing to save yet;
      // the others are listed so the place for the switch is known
      row({ tag: 'label', title: 'Memory', sub: 'Where it keeps what it learns', end: `<span class="sn-field agp-fieldw"><select id="agpMemSrc">`
        + `<option selected>${esc(keptBy())}</option><option disabled>Node memory (not available yet)</option><option disabled>External (not connected)</option></select></span>` }),
    ]));
  }
  // the node's services this agent may use; on a minimal node one switch, so it
  // waits on Debug until connecting outside services is a thing
  function services() {
    const svc = D.integ.filter((i) => i.mcp_server);
    return sec('Services it may use', svc.length ? group(svc.map((i) => {
      const key = i.mcp_server.slug, mode = D.grants[key];
      return row({ tag: 'label', title: i.label, sub: (i.purpose || i.description || '') + (mode ? `. ${mode.includes('w') ? 'Can read and change' : 'Read only'}` : ''),
        end: `<span class="sn-switch"><input type="checkbox" role="switch" ${mode ? 'checked' : ''} data-grant="${esc(key)}"></span>` }); }))
      : empty('No services are connected to this node yet.'));
  }

  // ---- Debug: what is there to find out why ----
  function debug() {
    const rows = historyRows(), shown = S.histAll ? rows : rows.slice(0, 6);
    const hist = rows.length ? group(shown.map((r) => row({ title: r.t + (r.n > 1 ? `, ${r.n} times` : ''), sub: r.s, meta: when(r.ts) }))) : empty('Nothing yet.');
    const tl = D.tools || [], shownTools = S.toolsAll ? tl : tl.slice(0, 9);
    const tools = tl.length ? `<div class="sn-tiles">${shownTools.map((x) => `<div class="sn-tile"><b>${esc(x.title || toolDoing(x.name))}</b><small>${esc(firstSentence(x.description))}</small></div>`).join('')}</div>`
      : empty('No tools.');
    const mine = desks.filter((k) => deskAgent(k) === S.slug);
    // each chat's newest session, asked once per visit to this tab
    if (S.sessFor !== S.slug) {
      S.sessFor = S.slug;
      Promise.all(mine.map((k) => loadSession(k.id))).then(() => { root._html = ''; paint(); });
    }
    const sessions = mine.length ? group(mine.map((k) => {
      const c = _contextByDesk[k.id], ss = _deskSess[k.id];
      const sub = ss ? [`Session ${String(ss.id).slice(0, 8)}`, ss.provider || ss.model, c && c.percent != null ? `context ${c.percent}%` : '']
        .filter(Boolean).join(' · ') : 'No session yet';
      return row({ title: esc(k.title || 'Chat'), sub: esc(sub),
        end: ss ? `<button class="sn-btn sn-btn--ghost" data-ctxclean="${esc(k.id)}">Clean context</button>`
          + `<button class="sn-btn sn-btn--ghost" data-reload="${esc(k.id)}">Reload session</button>`
          + `<button class="sn-btn sn-btn--ghost" data-copysess="${esc(ss.id)}">Copy session id</button>` : '' });
    })) : empty('No chats yet.');
    return sec('Now', nowHtml()) + sec(`Waiting for ${esc(nm(S.slug))}`, queueHtml()) + tasksHtml()
      + sec('Chat sessions', sessions)
      + sec('Messages with colleagues', mailHtml())
      + sec('Recent activity', hist, rows.length > 6 ? more('data-histall', S.histAll, 'Show all') : '')
      + (D.details ? addChannel() + services()
        + sec('Memory', group([row({ title: `Kept by: ${keptBy()}`, sub: 'Where “Remembered from conversations” is read from' })]))
        + sec(`Tools${tl.length ? ` <small>${tl.length}</small>` : ''}`, tools, tl.length > 9 ? more('data-toolsall', S.toolsAll, 'Show all') : '') : '');
  }

  function paint() {
    if (!S.slug) return;
    // until the agents arrive the head would show the slug and a letter for a moment
    if (!agentBySlug(S.slug)) { root.innerHTML = `<div class="sn-page">${window.snSkelRows(4, true)}</div>`; root._html = ''; return; }
    const a = agentBySlug(S.slug), now = nowLine();
    const role = a.role || a.description || '';
    const body = S.tab === 'debug' ? debug() : !D.details ? window.snSkelRows(4, false)
      : S.tab === 'knowledge' ? knowledge() : S.tab === 'memory' ? memory() : S.tab === 'settings' ? settings() : channels();
    const status = `<span class="sn-status${now ? ' sn-status--work' : ''}">${esc(role ? role + ', ' : '')}${now ? esc(now.t) : 'available'}</span>`;
    const html = `<div class="sn-page"><button type="button" class="agp-crumb" data-back><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg>Agents</button>`
      + `<div class="agp-head">${ava(S.slug, 'sn-ava--l', !!now)}<div class="agp-head__t"><h1 class="sn-page__title">${esc(nm(S.slug))}</h1>`
      + (now && now.to ? `<button type="button" class="agp-now" data-desk="${esc(now.to)}">${status}</button>` : status) + '</div>'
      + `<button class="sn-btn sn-btn--primary" data-message>Message</button></div>`
      + `<div class="agp-tabs"><div class="sn-seg">${TABS.map(([id, x]) => `<button type="button" data-tab="${id}" class="${S.tab === id ? 'is-on' : ''}">${x}</button>`).join('')}</div></div>`
      + body + '</div>';
    if (root._html === html) return;
    // a repaint keeps the field being typed in, and where its cursor was
    const f = document.activeElement && root.contains(document.activeElement) ? document.activeElement : null;
    const fid = f && f.id, pos = f && 'selectionStart' in f ? f.selectionStart : null;
    if (fid && fid !== 'agpMemQ') return;   // never wipe a form mid-typing
    root.innerHTML = html; root._html = html;
    if (fid) { const el = document.getElementById(fid); if (el) { el.focus(); if (pos != null) el.setSelectionRange(pos, pos); } }
  }

  async function write(fn, note) {
    // the change on screen is the answer; a toast only where nothing visibly
    // changes (Save) or something went wrong
    try { await fn(`/api/agents/${enc(S.slug)}`); if (note) toast(note); } catch (e) { toast('Could not save: ' + e.message); }
    await loadDetails(S.slug); paint();
  }

  async function open(slug, opts) {
    opts = opts || {};
    if (!slug) return;
    closePop();
    const tab = OLD[opts.tab] || opts.tab || 'channels';
    if (slug !== S.slug) { D = {}; Object.assign(S, { open: null, memQ: '', memAll: false, histAll: false, toolsAll: false, rules: false, adding: false, chAdd: false, full: false }); }
    S.slug = slug; S.tab = TABS.some(([id]) => id === tab) ? tab : 'channels';
    setView('agent');
    if (!opts.noHistory) history.pushState({}, '', '/agents/' + enc(slug) + '/' + S.tab);
    if (opts.deskId) setActive(opts.deskId);
    root.scrollTop = 0;
    paint();
    await Promise.all([loadLive(slug), loadDetails(slug)]);
    if (!agentBySlug(slug)) await loadAgents();   // a direct link can land before app.js has them
    paint();
    // the line under its name follows the agent: agent.* on the live stream (below)
  }
  openAgentPage = open;   // eslint-disable-line no-global-assign
  document.addEventListener('sn:event', async (e) => {
    const ev = e.detail || {};
    if (!S.slug || root.style.display === 'none' || !String(ev.type).startsWith('agent.')) return;
    if (ev.agent && ev.agent !== S.slug) return;
    await loadLive(S.slug); paint();
  });
  // app.js routes a loaded /agents/<slug>/<tab> before this file runs: take it over
  { const m = snPath().match(/^\/agents\/([^/]+)(?:\/([^/]+))?/);
    if (m) setTimeout(() => { if (!S.slug) open(decodeURIComponent(m[1]), { tab: m[2], noHistory: true }); }, 0); }

  root.addEventListener('click', async (e) => {
    const x = e.target;
    const tb = x.closest('[data-tab]');
    if (tb) { S.tab = tb.dataset.tab; S.forgetAll = false; S.full = false; S.sessFor = null; history.replaceState({}, '', '/agents/' + enc(S.slug) + '/' + S.tab); return paint(); }
    if (x.closest('[data-back]')) { const n = document.getElementById('navAgents'); if (n) n.click(); return; }
    if (x.closest('[data-message]')) {
      return openAgentChat(S.slug);
    }
    const th = x.closest('[data-thread]');
    if (th) { const i = Number(th.dataset.thread); S.open = S.open === i ? null : i; return paint(); }
    const dk = x.closest('[data-desk]'); if (dk) return openDesk(dk.dataset.desk);
    const cl = x.closest('[data-ctxclean]'); if (cl) return confirmCleanContext(cl, cl.dataset.ctxclean);
    const rl = x.closest('[data-reload]');
    if (rl) {
      const r = await fetch(`/api/desks/${enc(rl.dataset.reload)}/reload`, { method: 'POST', headers: { Accept: 'application/json' } }).catch(() => null);
      return toast(r && r.ok ? 'Session reloaded' : 'Reload failed');
    }
    const cs = x.closest('[data-copysess]');
    if (cs) { try { await navigator.clipboard.writeText(cs.dataset.copysess); toast('Copied'); } catch {} return; }
    if (x.closest('[data-avatar]')) return openAvatarPicker(x.closest('[data-avatar]'), S.slug);
    if (x.closest('[data-memall]')) { S.memAll = !S.memAll; return paint(); }
    if (x.closest('[data-histall]')) { S.histAll = !S.histAll; return paint(); }
    if (x.closest('[data-toolsall]')) { S.toolsAll = !S.toolsAll; return paint(); }
    if (x.closest('[data-chadd]')) { S.chAdd = !S.chAdd; return paint(); }
    const kv = x.closest('[data-knview]');
    if (kv) {
      S.full = kv.dataset.knview === 'preview'; paint();
      if (S.full && D.full == null) { const slug = S.slug, a = await api(`/api/agents/${enc(slug)}?body=true`).catch(() => ({}));
        if (slug === S.slug) { D.full = a.body_md || ''; paint(); } }
      return;
    }
    const cc = x.closest('[data-chconnect]');
    if (cc) {
      const ch = cc.dataset.chconnect, inp = document.getElementById('agpCh-' + ch), ref = inp ? inp.value.trim() : null;
      if (inp && !ref) { inp.focus(); return; }
      S.chAdd = false;
      return write((p) => api(`${p}/channels/${enc(ch)}`, { method: 'PUT', body: JSON.stringify({ enabled: true, instance_ref: ref }) }));
    }
    const fg = x.closest('[data-forget]');
    if (fg) return write((p) => api(`${p}/remembers/${enc(fg.dataset.forget)}`, { method: 'DELETE' }));
    if (x.closest('[data-forgetall]')) {
      if (!S.forgetAll) { S.forgetAll = true; return paint(); }
      S.forgetAll = false; return write((p) => api(`${p}/remembers`, { method: 'DELETE' }));
    }
    const uk = x.closest('[data-unknow]');
    if (uk) return write((p) => api(`${p}/knowledge/${enc(uk.dataset.unknow)}`, { method: 'DELETE' }));
    // the form opens on the first press: an empty form on screen all the time was clutter
    if (x.closest('[data-knadd]') && !S.adding) { S.adding = true; paint(); const t = document.getElementById('agpKnTitle'); if (t) t.focus(); return; }
    if (x.closest('[data-knadd]')) {
      const content = document.getElementById('agpKnText').value.trim(), title = document.getElementById('agpKnTitle').value.trim();
      if (!content) { document.getElementById('agpKnText').focus(); return; }
      S.adding = false;
      return write((p) => api(`${p}/knowledge`, { method: 'POST', body: JSON.stringify({ kind: 'text', title, content }) }));
    }
    if (x.closest('[data-knfile]')) return document.getElementById('agpKnFile').click();
    // a rule on or off for this agent: the dot flips at once, back if the save fails
    const pc = x.closest('[data-policy]');
    if (pc) {
      const pol = (D.pol || []).find((p) => p.slug === pc.dataset.policy); if (!pol) return;
      const on = !pol.applied; pol.applied = on; paint();
      try { await api(`/api/agents/${enc(S.slug)}/policies/${enc(pol.slug)}`, { method: 'PUT', body: JSON.stringify({ action: on ? 'include' : 'exclude' }) }); D.full = null; }
      catch (e) { pol.applied = !on; paint(); toast('Could not save: ' + e.message); }
    }
  });
  root.addEventListener('change', (e) => {
    const el = e.target;
    // a service grant: on = read and change, off = gone; the other grants stay
    if (el.matches('[data-grant]')) {
      const g = { ...D.grants };
      if (el.checked) g[el.dataset.grant] = 'rw-'; else delete g[el.dataset.grant];
      return write((p) => api(`${p}/grants`, { method: 'PUT', body: JSON.stringify({ grants: g }) }));
    }
    if (el.matches('[data-chan]')) return write((p) => api(`${p}/channels/${enc(el.dataset.chan)}`, { method: 'PUT',
      body: JSON.stringify({ enabled: el.checked, instance_ref: el.dataset.ref || null }) }));
    if (el.id === 'agpKnFile' && el.files[0]) {
      const f = el.files[0];
      if (f.size > 10 * 1024 * 1024) return toast('The file is over 10 MB');
      const rd = new FileReader();
      // the row is kept even when the memory service could not take the text;
      // then the agent cannot read it, and that is said, not hidden
      rd.onload = () => write(async (p) => { const r = await api(`${p}/knowledge`, { method: 'POST',
        body: JSON.stringify({ kind: 'file', filename: f.name, title: f.name, file_b64: String(rd.result).split(',')[1] }) });
        if (r.delivered === false) toast('Added, but it cannot read it yet: ' + (r.detail || 'the memory service did not take it')); });
      rd.readAsDataURL(f);
    }
  });
  // Settings and Instructions save themselves: a second after the typing stops, a choice at once
  const FIELD = { agpTitle: 'title', agpRole: 'role', agpModel: 'model', agpInstr: 'specialist_body' };
  let saveTimer = null;
  async function saveField(el) {
    const key = FIELD[el.id], v = key === 'specialist_body' ? el.value : el.value.trim(), a = agentBySlug(S.slug) || {};
    const was = key === 'specialist_body' ? (D.agent || {}).specialist_body || '' : a[key] || '';
    if (v === was || (key === 'title' && !v)) return;   // nothing new, or a name left empty
    await write((p) => api(p, { method: 'PUT', body: JSON.stringify({ [key]: v }) }), 'Saved');
    if (key === 'specialist_body') D.agent = { ...(D.agent || {}), specialist_body: v }; else a[key] = v;
    D.full = null;   // the node rebuilt the text it reads
  }
  root.addEventListener('input', (e) => {
    if (e.target.id === 'agpMemQ') { S.memQ = e.target.value; return paint(); }
    if (FIELD[e.target.id] && e.target.tagName !== 'SELECT') { clearTimeout(saveTimer); const el = e.target; saveTimer = setTimeout(() => saveField(el), 1000); }
  });
  root.addEventListener('change', (e) => { if (FIELD[e.target.id]) { clearTimeout(saveTimer); saveField(e.target); } });
  // an avatar picked in the drawer shows here at once
  { const _ac = afterAvatarChange;
    afterAvatarChange = async function () { const r = await _ac.apply(this, arguments); if (S.slug) paint(); return r; }; }
})();
