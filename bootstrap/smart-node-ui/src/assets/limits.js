/* Limit registry editor. Both the owner page and an agent's Limits tab use it. */
(function () {
  'use strict';
  function actionsFor(code) {
    return code === 'turn_seconds' ? ['pause', 'refuse'] : ['refuse', 'pause', 'warn'];
  }
  function cell(row, text) {
    var td = document.createElement('td');
    td.textContent = text == null ? '' : String(text);
    row.appendChild(td);
    return td;
  }
  function input(td, value, step) {
    var el = document.createElement('input');
    el.type = 'number'; el.min = '0.000001'; el.step = step || 'any';
    el.value = value == null ? '' : String(value);
    td.textContent = ''; td.appendChild(el); return el;
  }
  function select(td, value, code) {
    var el = document.createElement('select');
    var actions = actionsFor(code);
    actions.forEach(function (a) {
      var option = document.createElement('option');
      option.value = a; option.textContent = a.replace('_', ' ');
      el.appendChild(option);
    });
    if (value && actions.indexOf(value) === -1) {
      var old = document.createElement('option');
      old.value = ''; old.textContent = value.replace('_', ' ') + ' (unavailable; choose an action)';
      old.disabled = true; el.insertBefore(old, el.firstChild);
      el.value = '';
    } else {
      el.value = value;
    }
    td.textContent = ''; td.appendChild(el); return el;
  }
  // Notify is a switch (kit .sn-switch), as every on/off in the panel
  function check(td, value) {
    var label = document.createElement('label');
    label.className = 'sn-switch';
    var el = document.createElement('input');
    el.type = 'checkbox'; el.setAttribute('role', 'switch'); el.checked = !!value;
    el.setAttribute('aria-label', 'Notify');
    label.appendChild(el);
    td.textContent = ''; td.appendChild(label); return el;
  }
  function button(td, label, fn) {
    var el = document.createElement('button');
    el.type = 'button'; el.className = 'btn-row'; el.textContent = label;
    el.addEventListener('click', fn); td.appendChild(el); return el;
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({ credentials: 'same-origin', cache: 'no-store',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(window.snErr(response.status, data));
    return data;
  }
  function status(id, message) { var el = document.getElementById(id); if (el) el.textContent = message; }
  function profileInput(value, action, warn, notify) {
    var n = Number(value.value), pct = Number(warn.value);
    if (!Number.isFinite(n) || n <= 0) throw new Error('Enter a positive limit');
    if (!action.value) throw new Error('Choose an available action');
    if (!Number.isInteger(pct) || pct < 1 || pct > 100) throw new Error('Warn % must be 1–100');
    return { value: n, action: action.value, warn_at_pct: pct, notify: notify.checked };
  }
  function drawAdmin(data) {
    var pause = document.getElementById('nodePause');
    var paused = data.node_paused;
    pause.textContent = paused ? 'Resume node' : 'Pause node';
    pause.setAttribute('aria-pressed', paused ? 'true' : 'false');
    pause.onclick = async function () {
      pause.disabled = true;
      try {
        await request('/api/limits/pause', { method: 'PUT', body: JSON.stringify({ paused: !paused }) });
        status('limitsStatus', !paused ? 'Node paused; new wakes will wait.' : 'Node resumed.');
        await loadAdmin();
      } catch (e) { status('limitsStatus', e.message); }
      finally { pause.disabled = false; }
    };
    var defs = Object.fromEntries(data.definitions.map(function (d) { return [d.code, d]; }));
    var body = document.getElementById('limitsBody'); body.textContent = '';
    data.profiles.filter(function (p) { return p.scope_type === 'default' || p.scope_type === 'node'; })
      .forEach(function (p) {
        var tr = document.createElement('tr');
        cell(tr, (defs[p.code] || {}).label || p.code);
        cell(tr, p.period);
        var value = input(cell(tr), p.value);
        var action = select(cell(tr), p.action, p.code);
        var warn = input(cell(tr), p.warn_at_pct, '1');
        var notify = check(cell(tr), p.notify);
        cell(tr, p.code === 'node_spend_usd' ? p.usage + ' USD' : '—');
        button(cell(tr), 'Save', async function () {
          try {
            var payload = profileInput(value, action, warn, notify);
            await request('/api/limits/defaults/' + encodeURIComponent(p.code) + '/' + p.period,
              { method: 'PUT', body: JSON.stringify(payload) });
            status('limitsStatus', p.code + ' saved'); await loadAdmin();
          } catch (e) { status('limitsStatus', e.message); }
        });
        body.appendChild(tr);
      });
  }
  async function loadAdmin() {
    try { drawAdmin(await request('/api/limits')); }
    catch (e) { status('limitsStatus', 'Cannot load limits: ' + e.message); }
  }
  function drawAgent(data) {
    var body = document.getElementById('agentLimitsBody'); body.textContent = '';
    data.limits.forEach(function (item) {
      var tr = document.createElement('tr'), base = item.default, own = item.override;
      cell(tr, item.code); cell(tr, item.period);
      var inherited = cell(tr, base ? base.value : 'Not set'); inherited.className = 'limit-inherited';
      var value = input(cell(tr), own ? own.value : null);
      value.placeholder = 'inherit';
      var action = select(cell(tr), own ? own.action : base ? base.action : 'pause', item.code);
      var warn = input(cell(tr), own ? own.warn_at_pct : base ? base.warn_at_pct : 80, '1');
      var notify = check(cell(tr), own ? own.notify : base ? base.notify : true);
      cell(tr, item.usage + ' / ' + (item.effective ? item.effective.value : 'Not set'));
      var controls = cell(tr);
      button(controls, 'Save', async function () {
        try {
          var payload = profileInput(value, action, warn, notify);
          await request('/api/limits/agents/' + encodeURIComponent(data.agent) + '/' +
            encodeURIComponent(item.code) + '/' + item.period,
            { method: 'PUT', body: JSON.stringify(payload) });
          status('agentLimitsStatus', item.code + ' override saved'); await loadAgent();
        } catch (e) { status('agentLimitsStatus', e.message); }
      });
      if (own) button(controls, 'Clear', async function () {
        try {
          await request('/api/limits/agents/' + encodeURIComponent(data.agent) + '/' +
            encodeURIComponent(item.code) + '/' + item.period, { method: 'DELETE' });
          status('agentLimitsStatus', item.code + (base ? ' now inherits the node default' : ' override cleared')); await loadAgent();
        } catch (e) { status('agentLimitsStatus', e.message); }
      });
      body.appendChild(tr);
    });
  }
  async function loadAgent() {
    var slug = new URLSearchParams(location.search).get('name');
    if (!slug) { status('agentLimitsStatus', 'Agent name missing'); return; }
    try { drawAgent(await request('/api/limits/agents/' + encodeURIComponent(slug))); }
    catch (e) { status('agentLimitsStatus', 'Cannot load agent limits: ' + e.message); }
  }
  if (document.getElementById('limitsBody')) loadAdmin();
  if (document.getElementById('agentLimitsBody')) loadAgent();
})();
