// First-run page: workspace -> AI -> look -> theme -> the workspace chat. Step 1 needs
// no session (the setup token in the link proves the owner, the node signs them in with
// a one-time link); the later steps use the workspace's own authenticated /api.
(function () {
  const $ = (s) => document.querySelector(s);
  const q = new URLSearchParams(location.search);
  const here = location.origin + '/welcome/';
  const ORDER = ['account', 'ai', 'look', 'theme'];
  const STEP_OF = { password: 'account' };   // a screen inside a step
  let authUrl = '';

  // The screen on view fades out before the next one rises in (index.html: leave, rise).
  let swap = 0;
  function showPane(step) {
    const next = document.querySelector(`.pane[data-p="${step}"]`);
    const cur = document.querySelector('.pane.is-on');
    if (!next || cur === next) return;
    const mine = ++swap;
    let done = false;
    const enter = () => {
      if (mine !== swap || done) return;
      done = true;
      document.querySelectorAll('.pane').forEach((p) => p.classList.remove('is-on', 'is-leaving'));
      next.classList.add('is-on');
    };
    if (!cur || matchMedia('(prefers-reduced-motion: reduce)').matches) return enter();
    cur.classList.add('is-leaving');
    cur.classList.remove('is-on');
    cur.addEventListener('animationend', enter, { once: true });
    setTimeout(enter, 400);   // no animationend (a hidden tab): go on anyway
  }
  function show(step) {
    showPane(step);
    $('.steps').hidden = step === 'signin';   // a finished setup is not step 1 of 4
    const at = ORDER.indexOf(step === 'signin' ? 'account' : STEP_OF[step] || step);
    document.querySelectorAll('.steps span').forEach((s) => {
      const i = ORDER.indexOf(s.dataset.s);
      s.classList.toggle('is-on', i === at);
      s.classList.toggle('is-done', i < at);
    });
  }
  function say(el, text, err) { el.textContent = text || ''; el.classList.toggle('is-err', !!err); }
  // A reply that is not JSON (a proxy's error page) says only its status, never
  // "Unexpected token '<'".
  const answerOf = (r) => r.json().catch(() => ({ detail: `the node answered HTTP ${r.status}` }));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function signIn(nextStep) {
    location.href = `${authUrl}/?rd=${encodeURIComponent(`${here}?step=${nextStep}`)}`;
  }
  // Without a session Traefik redirects /api to the sign-in host; read that as "sign in".
  async function api(path, opts = {}) {
    const r = await fetch(path, { credentials: 'same-origin', redirect: 'manual', ...opts });
    if (r.type === 'opaqueredirect' || r.status === 401) throw Object.assign(new Error('signin'), { signin: true });
    return r;
  }

  // ---- 1. workspace: the node creates the owner and signs them in ----
  async function createOwner() {
    show('account');
    const msg = $('#accMsg');
    const r = await fetch('/welcome/api/setup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: q.get('token') || '' }),
    }).catch(() => null);
    if (!r) return say(msg, 'The node did not answer. Is it running?', true);
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.signin_token) {
      const go = () => { location.href = `${authUrl}/?action=onetime&t=${encodeURIComponent(d.signin_token)}`
        + `&rd=${encodeURIComponent(`${here}?step=ai${q.get('way') ? `&way=${q.get('way')}` : ''}`)}`; };
      if (!d.password) return go();
      // the node made the first password: shown here once, then the one-time sign-in
      $('#pwUser').textContent = d.username || 'admin';
      $('#pwValue').textContent = d.password;
      $('#pwCopy').onclick = async () => {
        try { await navigator.clipboard.writeText(d.password); say($('#pwMsg'), 'Copied.'); }
        catch { say($('#pwMsg'), 'Select the password and copy it by hand.', true); }
      };
      $('#pwGo').onclick = go;
      return show('password');
    }
    if (r.status === 409) return show('signin');
    if (r.status === 403) return say(msg, 'This link is no longer valid. Use the link the installer printed.', true);
    say(msg, d.detail || `Could not set up the workspace (${r.status}).`, true);
  }
  $('#signinGo').addEventListener('click', () => signIn('ai'));

  // ---- 2. AI ----
  // What a person can pick; an entry is offered only when the node has its runtime.
  // `needs` is the installer component that makes an entry work; the key-based ones
  // wait for the runtime catalog (drift items 20, 33) and are shown as "soon".
  const PROVIDERS = [
    { id: 'claude-cli', mono: 'CL', name: 'Claude subscription', needs: 'agent-runtimes', g: 'claude',
      note: 'Sign in with your Claude account, Pro or Max. Every agent uses it.' },
    // The ChatGPT plan: Flint agents are sent to it through the gateway (the person signs in with their own plan).
    { id: 'chatgpt', mono: 'GP', name: 'ChatGPT subscription', needs: 'codex', g: 'chatgpt',
      note: 'Sign in with your ChatGPT account. Flint agents use your plan.' },
    // An API key provider is the custom provider with its address filled in: no
    // code of its own (drift item 50). `runtimes`: which agent runtimes can speak
    // to it (Claude Code speaks the Anthropic format, Flint the OpenAI one).
    // `pick`: the provider's balanced line, neither the cheapest nor the largest,
    // chosen in advance from the list the key returns (the newest of it); the
    // person can change it. No match leaves "Pick a model".
    { id: 'anthropic', mono: 'AN', name: 'Anthropic', note: 'Claude models with an API key',
      url: 'https://api.anthropic.com', runtimes: ['claude-code', 'flint'], pick: /sonnet/ },
    { id: 'openai', mono: 'OA', name: 'OpenAI', note: 'GPT models with an API key',
      url: 'https://api.openai.com', runtimes: ['flint'], pick: /^gpt-[\d.]+$/ },
    { id: 'google', mono: 'GG', name: 'Google', note: 'Gemini models with an API key',
      url: 'https://generativelanguage.googleapis.com/v1beta/openai', runtimes: ['flint'], pick: /(^|\/)gemini-[\d.]+-flash$/ },
    { id: 'openrouter', mono: 'OR', name: 'OpenRouter', note: 'Hundreds of models, one key',
      url: 'https://openrouter.ai/api', runtimes: ['flint'], pick: /^openrouter\/auto$/ },
    // any compatible address (a router, a local model server)
    { id: 'custom', mono: 'CU', name: 'Custom provider', runtimes: ['claude-code', 'flint'],
      note: 'Your own address: a model router or a model server. Agents use it.' },
  ];
  // the installer component behind each runtime, chosen at install
  const RT = { 'claude-code': 'agent-runtimes', flint: 'flint' };
  let installed = [];
  let chosenModel = '';   // set by the custom provider; saved on the agent at the end
  async function loadAi() {
    try { installed = (await (await api('/api/node/components')).json()).installed || []; }
    catch (e) { if (e.signin) return signIn('ai'); }
    $('#prov').innerHTML = PROVIDERS.map((p) => {
      // offered when the node has its component, or a runtime that can speak to it
      const ok = p.needs ? installed.includes(p.needs) : p.runtimes.some((r) => installed.includes(RT[r]));
      return `<button data-id="${p.id}" data-g="${p.g || 'api'}" ${ok ? '' : 'disabled'}><span class="mono">${p.mono}</span>
        <span class="t"><b>${p.name}</b><small>${ok ? p.note : `Not on this node yet. Run the installer again and choose ${p.name}.`}</small></span>
        ${ok ? '<span class="tick"></span>' : ''}</button>`;
    }).join('');
    setGroup(curGroup);
    show('ai');
  }
  // One switch on top instead of a long list: a subscription is one card, a key is the providers.
  // the installer asked how the AI works; the page opens on that answer
  // The way in was chosen in the installer, so only the ways this node has are offered:
  // a group shows when one of its cards can work here. One group: no switch at all.
  let curGroup = { claude: 'claude', chatgpt: 'chatgpt', api: 'api' }[q.get('way')] || '';
  let GROUPS = ['claude', 'chatgpt', 'api'];
  function setGroup(g) {
    GROUPS = ['claude', 'chatgpt', 'api'].filter((k) => document.querySelector(`#prov button[data-g="${k}"]:not([disabled])`));
    if (!GROUPS.includes(g)) g = GROUPS[0];
    curGroup = g;
    document.querySelectorAll('#provSeg button').forEach((b) => {
      b.hidden = !GROUPS.includes(b.dataset.g);
      b.classList.toggle('is-on', b.dataset.g === g);
    });
    $('#provSeg').style.display = GROUPS.length < 2 ? 'none' : '';   // the kit's display:grid outranks [hidden]
    $('#provSeg').style.setProperty('--n', GROUPS.length);
    $('#provSeg').style.setProperty('--i', GROUPS.indexOf(g));
    document.querySelectorAll('#prov button').forEach((b) => {
      b.style.display = b.dataset.g === g ? '' : 'none';
      b.classList.remove('is-on');
    });
    $('#codeBox').classList.remove('is-on');
    $('#customBox').classList.remove('is-on');
    say($('#aiMsg'), '');
  }
  $('#provSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (b) setGroup(b.dataset.g);
  });
  // The runtimes are chosen at install (Claude Code, Flint, or both): offer the ones
  // installed that can speak to this provider, and pick the only one when there is one.
  function runtimesFor(p) {
    const btns = [...document.querySelectorAll('#cuRuntime [data-v]')];
    btns.forEach((b) => {
      const has = installed.includes(RT[b.dataset.v]);
      b.disabled = !has || !p.runtimes.includes(b.dataset.v);
      b.title = !has ? 'Not installed on this node' : b.disabled ? `${p.name} does not speak to it` : '';
    });
    const on = btns.find((b) => b.classList.contains('is-on') && !b.disabled) || btns.find((b) => !b.disabled);
    btns.forEach((b) => b.classList.toggle('is-on', b === on));
  }
  let preset = null;
  let runtime = '';
  $('#prov').addEventListener('click', async (e) => {
    const b = e.target.closest('button:not([disabled])'); if (!b) return;
    document.querySelectorAll('#prov button').forEach((x) => x.classList.toggle('is-on', x === b));
    runtime = b.dataset.id;
    const msg = $('#aiMsg');
    preset = PROVIDERS.find((p) => p.id === runtime);
    const keyed = !!preset.runtimes;
    $('#customBox').classList.toggle('is-on', keyed);
    $('#codeBox').classList.remove('is-on');
    if (keyed) {
      say(msg, ''); say($('#cuMsg'), '');
      $('#cuUrlRow').hidden = !!preset.url;
      $('#cuUrl').value = preset.url || '';
      $('#cuKeyLabel').textContent = preset.url ? `${preset.name} API key` : 'Key, if it needs one';
      // a key belongs to its provider: never carried to another address
      $('#cuKey').value = ''; modelList([]); modelsFor = '';
      runtimesFor(preset);
      (preset.url ? $('#cuKey') : $('#cuUrl')).focus();
      return;
    }
    const gpt = runtime === 'chatgpt';
    $('#codeHelp').textContent = gpt
      ? 'A sign-in page opened in a new tab. Sign in and allow access. The page you land on will not load: copy its address from the address bar and paste it here.'
      : 'A sign-in page opened in a new tab. Sign in, then paste the code it shows.';
    $('#code').placeholder = gpt ? 'Paste the address (starts with http://127.0.0.1:1455)' : 'Paste the code';
    say(msg, 'Opening the sign-in page...');
    const tab = window.open('about:blank', '_blank');   // inside the click, so it is not blocked
    try {
      const r = await api(`${loginBase()}/login/start`, { method: 'POST' });
      const d = await answerOf(r);
      const url = d?.result?.auth_url || d?.auth_url;
      if (!r.ok || !url) throw new Error(d.detail || 'no sign-in link');
      if (tab) tab.location = url;
      $('#openAgain').href = url;
      $('#codeBox').classList.add('is-on');
      say(msg, '');
    } catch (err) {
      if (tab) tab.close();
      if (err.signin) return signIn('ai');
      say(msg, `Could not start the sign-in: ${err.message}`, true);
    }
  });
  const loginBase = () => (runtime === 'chatgpt' ? '/api/gateway/chatgpt' : `/api/gateway/cli/${runtime}`);
  $('#codeGo').addEventListener('click', async () => {
    const msg = $('#aiMsg');
    const code = $('#code').value.trim();
    const gpt = runtime === 'chatgpt';
    if (!code) return say(msg, gpt ? 'Paste the address of the page you landed on.' : 'Paste the code from the sign-in page.', true);
    say(msg, 'Connecting...');
    try {
      const r = await api(`${loginBase()}/login/complete`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(gpt ? { url: code } : { code }),
      });
      const d = await answerOf(r);
      if (!r.ok || d?.result?.ok === false) throw new Error(typeof d.detail === 'string' ? d.detail : 'the code was not accepted');
      if (gpt) {
        // the plan's models were added to Flint's list: take the newest plain GPT line, else the first
        const gpts = (d.models || []).filter((m) => /^gpt-[\d.]+$/.test(m)).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
        const pick = gpts.pop() || (d.models || [])[0];
        if (pick) chosenModel = `flint-cli/${pick}`;
        say(msg, `Connected${d.email ? ' as ' + d.email : ''}.`);
        setTimeout(loadLook, 500);
        return;
      }
      // The agent moves onto this runtime: it may have been seeded before the runtime was here.
      const models = await (await api('/api/models')).json();
      const rows = (Array.isArray(models) ? models : models.models || [])
        .filter((m) => m.modality === 'chat' && String(m.id).startsWith(`${runtime}/`));
      chosenModel = (rows.find((m) => m.is_default) || rows[0])?.id || chosenModel;
      say(msg, 'Connected.');
      setTimeout(loadLook, 500);
    } catch (err) {
      if (err.signin) return signIn('ai');
      say(msg, `Not connected: ${err.message}. Try the sign-in again.`, true);
    }
  });
  // The models the address offers for this key: picked from a list, not typed from
  // memory; a refused key is said here, before Connect.
  let modelsFor = '', modelsTimer = 0;
  // A list the address gave is a drop-down to pick from; without one the field
  // is typed (an address that does not list its models).
  function modelList(models) {
    const has = models.length > 0;
    $('#cuModelPick').hidden = !has;
    $('#cuModel').closest('.sn-field').hidden = has;
    $('#cuModel').value = '';
    $('#cuModelSel').innerHTML = has
      ? `<option value="">Pick a model (${models.length})</option>` + models.map((m) => `<option>${esc(m)}</option>`).join('')
      : '';
    const fit = preset?.pick ? models.filter((m) => preset.pick.test(m)) : [];
    if (fit.length) $('#cuModelSel').value = fit.sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).pop();
  }
  const chosenModelName = () => ($('#cuModelPick').hidden ? $('#cuModel').value : $('#cuModelSel').value).trim();
  $('#cuModelSel').addEventListener('change', () => say($('#cuMsg'), ''));
  async function loadModels() {
    const body = { base_url: $('#cuUrl').value.trim(), api_key: $('#cuKey').value || null,
      runtime: $('#cuRuntime .is-on')?.dataset.v || 'claude-code' };
    const k = JSON.stringify(body);
    if (!body.base_url || k === modelsFor) return;
    modelsFor = k;
    try {
      const r = await api('/api/gateway/cli/models', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: k,
      });
      const d = await answerOf(r);
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${r.status}`);
      modelList(d.models);
    } catch (err) {
      if (err.signin) return signIn('ai');
      modelsFor = '';
      say($('#cuMsg'), `Not connected: ${err.message}`, true);
    }
  }
  ['#cuUrl', '#cuKey', '#cuModel'].forEach((sel) => $(sel).addEventListener('input', () => {
    say($('#cuMsg'), '');   // an old error says nothing about the new value
    if (sel === '#cuModel') return;
    if (sel === '#cuUrl') modelList([]);   // another address, another set of models
    clearTimeout(modelsTimer); modelsTimer = setTimeout(loadModels, 700);
  }));
  $('#cuRuntime').addEventListener('click', (e) => {
    const b = e.target.closest('button:not([disabled])'); if (!b) return;
    document.querySelectorAll('#cuRuntime button').forEach((x) => x.classList.toggle('is-on', x === b));
    loadModels();   // the other runtime asks in its own format
  });
  $('#cuGo').addEventListener('click', async () => {
    const msg = $('#cuMsg');
    const body = { base_url: $('#cuUrl').value.trim(), model: chosenModelName(),
      api_key: $('#cuKey').value || null,
      runtime: $('#cuRuntime .is-on')?.dataset.v || 'claude-code',
      ...(preset && preset.url ? { name: `${preset.id}-api` } : {}) };
    if (preset && preset.url && !body.api_key) return say(msg, `Paste your ${preset.name} API key.`, true);
    if (!body.base_url) return say(msg, 'Fill in the address.', true);
    if (!body.model) return say(msg, 'Pick a model.', true);
    say(msg, 'Connecting, and asking the model one question...');
    try {
      const r = await api('/api/gateway/cli', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const d = await answerOf(r);
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : `HTTP ${r.status}`);
      chosenModel = d.model;
      $('#cuKey').value = '';
      say(msg, `Connected, and the model answered. Agents will use ${String(d.model).split('/').pop()}.`);   // the model picked, not the runtime's prefix
      setTimeout(loadLook, 1800);   // long enough to read
    } catch (err) {
      if (err.signin) return signIn('ai');
      say(msg, `Not connected: ${err.message}`, true);
    }
  });
  // no skipping this step: a node whose AI does not answer is not worth entering
  //; Connect checks that the address answers before it saves

  // ---- 3. look: types first, the team of a type inside ----
  let agent = null, avatar = '', packs = [], byPack = {}, open = null, nameTyped = false;
  const faceOf = (key) => Object.values(byPack).flat().find((a) => a.key === key);
  $('#agentName').addEventListener('input', () => { nameTyped = true; });
  async function loadLook() {
    try {
      const list = await (await api('/api/agents')).json();
      const agents = Array.isArray(list) ? list : (list.agents || list.items || []);
      agent = agents.find((a) => !a.archived_at) || agents[0] || null;
      const avs = await (await api('/api/avatars')).json();
      const manifest = await fetch('/avatars/packs.json', { credentials: 'same-origin' }).then((r) => (r.ok ? r.json() : [])).catch(() => []);
      byPack = {};
      avs.filter((a) => a.group !== 'custom').forEach((a) => { (byPack[a.group] ||= []).push(a); });
      const known = manifest.filter((m) => byPack[m.id]);
      const rest = Object.keys(byPack).filter((id) => !known.some((m) => m.id === id)).map((id) => ({ id, label: id }));
      packs = [...known, ...rest];
      // every face gets its name: from packs.json by position, else from the file name
      // (crew: maya.webp -> Maya, node-architect.webp -> Node Architect)
      packs.forEach((p) => byPack[p.id].forEach((a, i) => {
        a.label = (p.names && p.names[i])
          || (a.name === 'pm' ? 'PM' : a.name.split('-').map((w) => w[0].toUpperCase() + w.slice(1)).join(' '));
      }));
      avatar = agent?.avatar || (byPack.knot?.[0] || avs[0] || {}).key || '';
      // a fresh agent takes the name of the face it shows; typing a name stops that
      const fresh = !agent || !agent.avatar;
      $('#agentName').value = fresh ? (faceOf(avatar)?.label || agent?.title || '') : (agent.title || agent.slug);
      nameTyped = !fresh;
      open = null;   // start on the list of types, the chosen one highlighted
      renderLook();
      show('look');
    } catch (err) {
      if (err.signin) return signIn('look');
      say($('#lookMsg'), `Could not load the agent: ${err.message}`, true);
      show('look');
    }
  }
  const urlOf = (key) => `/avatars/${key}`;
  function renderLook() {
    $('#heroImg').src = avatar ? urlOf(avatar) : '';
    const p = packs.find((x) => x.id === open);
    $('#crumb').innerHTML = p ? `<button class="sn-btn sn-btn--ghost back" id="allTypes"><svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>All looks</button> <span>${esc(p.label)}</span>` : 'Character';
    $('#grid').innerHTML = p
      ? byPack[p.id].map((a) => `<button data-key="${esc(a.key)}" class="${a.key === avatar ? 'is-on' : ''}" title="${esc(a.label)}">
          <img src="${a.url}" alt="" loading="lazy">${esc(a.label)}</button>`).join('')
      : packs.map((x) => {
        const team = byPack[x.id];
        const on = avatar.startsWith(`${x.id}/`);
        const cover = team.find((a) => x.cover && a.key === `${x.id}/${x.cover}`) || team[0];
        return `<button data-pack="${esc(x.id)}" class="${on ? 'is-on' : ''}" title="${esc(cover.label)}">
          <img src="${cover.url}" alt="" loading="lazy">${esc(x.label)}<small>${team.length}</small></button>`;
      }).join('');
  }
  $('#crumb').addEventListener('click', (e) => { if (e.target.closest('#allTypes')) { open = null; renderLook(); } });
  $('#grid').addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.pack) { open = b.dataset.pack; renderLook(); return; }
    avatar = b.dataset.key;
    if (!nameTyped) $('#agentName').value = faceOf(avatar)?.label || $('#agentName').value;
    renderLook();
  });
  $('#lookGo').addEventListener('click', () => show('theme'));
  $('#lookBack').addEventListener('click', loadAi);
  $('#themeBack').addEventListener('click', () => show('look'));

  // ---- 4. theme, then into the workspace ----
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem('sn-theme', t); document.cookie='sn-theme='+t+'; domain='+location.hostname.replace(/^[^.]+\./,'')+'; path=/; max-age=31536000; SameSite=Lax'; } catch { /* private mode: the default stays */ }
    document.querySelectorAll('#themes button').forEach((b) => b.classList.toggle('is-on', b.dataset.v === t));
  }
  $('#themes').addEventListener('click', (e) => { const b = e.target.closest('button'); if (b) setTheme(b.dataset.v); });
  $('#finish').addEventListener('click', async () => {
    const msg = $('#themeMsg');
    if (!agent) { location.href = '/'; return; }
    say(msg, 'Saving...');
    try {
      const r = await api(`/api/agents/${encodeURIComponent(agent.slug)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: $('#agentName').value.trim() || agent.title, avatar,
          ...(chosenModel ? { model: chosenModel } : {}) }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      location.href = '/';
    } catch (err) {
      if (err.signin) return signIn('look');
      say(msg, `Could not save: ${err.message}`, true);
    }
  });

  // A reload, or a way back: signed in, this goes on with the setup, or to the workspace
  // when the agent already has its model. Not signed in: how to sign in.
  async function resume() {
    try {
      const d = await (await api('/api/agents')).json();
      const list = Array.isArray(d) ? d : d.agents || [];
      if (list.some((a) => !a.model || a.model === 'none')) return loadAi();
      location.href = '/';
    } catch { show('signin'); }
  }

  // ---- start ----
  fetch('/welcome/api/auth').then((r) => r.json()).then((d) => {
    authUrl = (d.auth_url || '').replace(/\/$/, '');
    $('#passkey').href = `${authUrl}/?action=settings`;
    // A local node (plain http, *.localhost): this browser stays signed in, and
    // the way back is the launcher on this computer, so no passkey is offered.
    if (location.protocol === 'http:') {
      $('#passkeyLine').hidden = true;
      $('#signinHow').textContent = 'On the node run sudo smart-node-signin and open the link it prints. If you have an "Open Smart Node" shortcut, it does the same.';
      $('#signinGo').closest('.row').remove();
    }
    try { const t = (document.cookie.match(/(?:^|; )sn-theme=(light|dark)/)||[])[1] || localStorage.getItem('sn-theme'); if (t) setTheme(t); } catch { /* ignore */ }
    $('#themeTgl').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
    const step = q.get('step');
    if (step === 'ai') return loadAi();
    if (step === 'look' || step === 'agent') return loadLook();
    if (q.get('token')) return createOwner();
    resume();
  });
})();
