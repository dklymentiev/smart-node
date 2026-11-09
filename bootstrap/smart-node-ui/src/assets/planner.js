/* Planner settings: the task-status dictionaries, wait reasons and close
   outcomes, edited in place. They belong in the node panel, not in the Studio. The planner answers through core-api's proxy,
   /api/planner/...; it lets only the owner write, and a refusal is shown as it
   came. A row in use cannot be removed, so there is no delete: switch it off. */
(function () {
  'use strict';
  var BASE = '/api/planner/api/v1/dictionaries/';
  var DICTS = [
    { key: 'wait-reasons', body: 'waitBody',
      stages: ['open', 'awaiting_approval', 'pending_review', 'blocked', 'escalated'], fresh: 'blocked' },
    { key: 'close-outcomes', body: 'closeBody', stages: ['done', 'rejected'], fresh: 'rejected' }
  ];
  var EFFECTS_HINT = 'JSON. Keys: notify (assignee|reviewer|owner|none), comment_required, agent_may_set, success';

  async function request(url, options) {
    var response = await fetch(url, Object.assign({ credentials: 'same-origin', cache: 'no-store',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      var d = data && data.detail;
      throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : 'HTTP ' + response.status);
    }
    return data;
  }
  function status(message, bad) {
    var el = document.getElementById('plannerStatus');
    if (!el) return;
    el.textContent = message || '';
    el.classList.toggle('is-bad', !!bad);
  }
  function td(tr) { var c = document.createElement('td'); tr.appendChild(c); return c; }
  function text(tr, field, value, placeholder, cls) {
    var el = document.createElement('input');
    el.type = 'text'; el.dataset.f = field; el.value = value == null ? '' : String(value);
    if (placeholder) el.placeholder = placeholder;
    if (cls) el.className = cls;
    td(tr).appendChild(el);
    return el;
  }
  function stage(tr, stages, value) {
    var el = document.createElement('select');
    el.dataset.f = 'legacy_stage';
    stages.forEach(function (s) {
      var o = document.createElement('option'); o.value = s; o.textContent = s; el.appendChild(o);
    });
    el.value = stages.indexOf(value) >= 0 ? value : stages[0];
    td(tr).appendChild(el);
    return el;
  }
  // On / off is the panel's switch, and it applies at once, as the other
  // switches of the panel do.
  // A refusal puts it back and says why.
  function setSwitch(el, on) {
    el.classList.toggle('on', on);
    el.setAttribute('aria-checked', on ? 'true' : 'false');
  }
  function onOff(tr, d, r) {
    var el = document.createElement('button');
    el.type = 'button'; el.className = 'switch'; el.dataset.f = 'enabled';
    el.setAttribute('role', 'switch');
    el.setAttribute('aria-label', 'On: ' + r.code);
    el.innerHTML = '<span class="knob"></span>';
    setSwitch(el, r.enabled !== false);
    el.addEventListener('click', async function () {
      var on = el.getAttribute('aria-checked') !== 'true';
      setSwitch(el, on);
      el.disabled = true;
      try {
        await request(BASE + d.key + '/' + encodeURIComponent(r.code), { method: 'PATCH', body: JSON.stringify({ enabled: on }) });
        status((on ? 'Switched on ' : 'Switched off ') + r.code + '.');
      } catch (e) {
        setSwitch(el, !on);
        status(e.message, true);
      } finally { el.disabled = false; }
    });
    td(tr).appendChild(el);
    return el;
  }
  function button(tr, label, primary, fn) {
    var el = document.createElement('button');
    el.type = 'button'; el.className = primary ? 'btn-row btn-primary' : 'btn-row'; el.textContent = label;
    el.addEventListener('click', function () { fn(el); });
    td(tr).appendChild(el);
    return el;
  }
  // The row's fields as the planner takes them; effects must be a JSON object.
  function read(tr) {
    var o = {};
    tr.querySelectorAll('[data-f]').forEach(function (el) {
      var f = el.dataset.f;
      if (f === 'enabled') o[f] = el.getAttribute('aria-checked') === 'true';
      else if (f === 'effects') {
        var v;
        try { v = JSON.parse(el.value.trim() || '{}'); } catch (e) { v = null; }
        if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('Effects must be a JSON object, for example {"notify":"assignee"}');
        o[f] = v;
      } else o[f] = el.value.trim();
    });
    return o;
  }
  function rowFor(d, r) {
    var tr = document.createElement('tr');
    tr.dataset.code = r.code;
    var c = td(tr);
    var code = document.createElement('span'); code.className = 'pl-code'; code.textContent = r.code; c.appendChild(code);
    if (r.legacy_default) {
      var m = document.createElement('span'); m.className = 'pl-default'; m.textContent = 'default';
      m.title = 'What a legacy writer gets for this stage'; c.appendChild(m);
    }
    text(tr, 'label', r.label);
    text(tr, 'description', r.description);
    stage(tr, d.stages, r.legacy_stage);
    text(tr, 'effects', JSON.stringify(r.effects || {}), '', 'pl-effects').title = EFFECTS_HINT;
    onOff(tr, d, r);
    button(tr, 'Save', false, async function (b) {
      var o;
      try { o = read(tr); } catch (e) { status(e.message, true); return; }
      b.disabled = true;
      try {
        await request(BASE + d.key + '/' + encodeURIComponent(r.code), { method: 'PATCH', body: JSON.stringify(o) });
        status('Saved ' + r.code + '.');
      } catch (e) { status(e.message, true); }
      finally { b.disabled = false; }
    });
    return tr;
  }
  function newRow(d) {
    var tr = document.createElement('tr');
    tr.className = 'pl-new';
    text(tr, 'code', '', 'new_code', 'pl-code');
    text(tr, 'label', '', 'Label');
    text(tr, 'description', '', 'What it means');
    stage(tr, d.stages, d.fresh);
    text(tr, 'effects', '{}', '', 'pl-effects').title = EFFECTS_HINT;
    td(tr);
    button(tr, 'Add', true, async function (b) {
      var o;
      try { o = read(tr); } catch (e) { status(e.message, true); return; }
      if (!o.code || !o.label) { status('Code and label are required.', true); return; }
      b.disabled = true;
      try {
        await request(BASE + d.key, { method: 'POST', body: JSON.stringify(o) });
        status('Added ' + o.code + '.');
        await load(d);
      } catch (e) { status(e.message, true); b.disabled = false; }
    });
    return tr;
  }
  async function load(d) {
    var body = document.getElementById(d.body);
    if (!body) return;
    try {
      var rows = await request(BASE + d.key + '?include_disabled=true');
      body.textContent = '';
      (Array.isArray(rows) ? rows : []).forEach(function (r) { body.appendChild(rowFor(d, r)); });
      body.appendChild(newRow(d));
    } catch (e) {
      body.innerHTML = '';
      var tr = document.createElement('tr'), c = td(tr);
      c.colSpan = 7; c.textContent = 'Could not load the dictionary (' + e.message + '). The planner may predate migration 0010.';
      body.appendChild(tr);
    }
  }
  DICTS.forEach(load);
})();
