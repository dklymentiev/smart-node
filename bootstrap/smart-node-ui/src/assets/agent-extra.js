/* Panel agent-edit: Instructions + Memory tabs, ported from the (retired) Studio
   configurator. Self-contained: its own DOM ids (id- ag- kw- bd- kn- prefixes),
   slug from ?name=, hits the same core-api. Coexists with Panel's existing tabs
   (which use ae- ids). */
(function () {
  'use strict';
  if (document.body.getAttribute('data-page') !== 'agent-edit') return;
  var slug = new URLSearchParams(location.search).get('name') || '';
  var instr = document.getElementById('ag-instructions');
  if (!slug || !instr) return;

  function $(id) { return document.getElementById(id); }
  function esc(t) { var d = document.createElement('div'); d.textContent = (t == null ? '' : String(t)); return d.innerHTML; }
  function jget(url) { return fetch(url, { headers: { 'Accept': 'application/json' }, cache: 'no-store' }); }
  function jsend(url, method, body) {
    return fetch(url, { method: method, headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, body: JSON.stringify(body) });
  }

  // The save bars keep their status text for screen readers (hidden on screen); an
  // outcome in it (saved, discarded, failed, an error) also shows as a toast.
  ['save-note', 'kn-note', 'ctx-agent-note'].forEach(function (id) {
    var el = document.getElementById(id); if (!el || !window.MutationObserver) return;
    new MutationObserver(function () {
      var t = (el.textContent || '').trim();
      if (/^(saved|save failed|save error|discarded|load error|threshold)/.test(t) && window.snToast) window.snToast(t);
    }).observe(el, { childList: true, characterData: true, subtree: true });
  });
  var modelSel = $('ag-model'), note = $('save-note'), btnSave = $('btn-save'), btnDiscard = $('btn-discard'),
      specSel = $('ag-specialist'), dirtyDot = $('dirty-dot'), resetLink = $('reset-instr'),
      idTitle = $('id-title'), idDept = $('id-dept'), idEnabled = $('id-enabled');

  var lastWantedModel = null, loaded = { model: '', instructions: '' }, specBodies = {}, currentRoleSlug = '', agentObj = null;

  function setModel(v) {
    if (v) lastWantedModel = v;
    var opt = [].slice.call(modelSel.options).filter(function (o) { return o.value === v; })[0];
    if (opt) modelSel.value = v;
    else if (v) { var o = document.createElement('option'); o.value = v; o.textContent = v + ' (current)'; modelSel.appendChild(o); modelSel.value = v; }
  }
  function flashInstr() { instr.classList.remove('flash'); void instr.offsetWidth; instr.classList.add('flash'); }
  function applyCurrentSpecialist() {
    var sel = currentRoleSlug && specSel.querySelector('option[value="' + (window.CSS && CSS.escape ? CSS.escape(currentRoleSlug) : currentRoleSlug) + '"]');
    specSel.value = sel ? currentRoleSlug : '';
  }
  function snapIdentity() { return { title: idTitle.value, department: idDept.value, enabled: idEnabled.classList.contains('on') }; }
  function updateDirty() {
    var d = (modelSel.value !== loaded.model) || (instr.value !== loaded.instructions);
    if (loaded.identity) { var id = loaded.identity; d = d || idTitle.value !== id.title || idDept.value !== id.department || idEnabled.classList.contains('on') !== id.enabled; }
    dirtyDot.classList.toggle('show', d);
  }

  modelSel.addEventListener('change', updateDirty);
  instr.addEventListener('input', updateDirty);
  idTitle.addEventListener('input', updateDirty);
  idDept.addEventListener('change', updateDirty);
  idEnabled.addEventListener('click', function () { idEnabled.classList.toggle('on'); idEnabled.setAttribute('aria-checked', idEnabled.classList.contains('on') ? 'true' : 'false'); updateDirty(); });

  // model catalog: one group per runtime (the provider column), the vendor on each
  // option, and the value is the id the agent stores, <runtime>/<model>.
  jget('/api/models').then(function (r) { return r.ok ? r.json() : {}; }).then(function (j) {
    var list = (j && j.models) || []; if (!list.length) return;
    var groups = {}; list.forEach(function (m) { (groups[m.provider] = groups[m.provider] || []).push(m); });
    modelSel.innerHTML = '';
    Object.keys(groups).forEach(function (prov) {
      var og = document.createElement('optgroup'); og.label = prov;
      groups[prov].forEach(function (m) { var o = document.createElement('option'); o.value = m.id; o.textContent = m.label + ' - ' + m.vendor + (m.is_default ? ' (default)' : ''); og.appendChild(o); });
      modelSel.appendChild(og);
    });
    if (lastWantedModel) setModel(lastWantedModel);
  }).catch(function () {});

  // specialists
  jget('/api/specialists').then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
    if (!data) return;
    var list = Array.isArray(data) ? data : (data.specialists || []);
    specSel.innerHTML = '<option value="">keep current</option>';
    var groups = {};
    list.forEach(function (sp) { if (!sp || !sp.slug) return; specBodies[sp.slug] = (sp.body_md != null) ? sp.body_md : ''; var c = sp.category || 'Other'; (groups[c] = groups[c] || []).push(sp); });
    Object.keys(groups).sort().forEach(function (cat) {
      var og = document.createElement('optgroup'); og.label = cat;
      groups[cat].sort(function (a, b) { return (a.name || a.slug || '').localeCompare(b.name || b.slug || ''); }).forEach(function (sp) { var o = document.createElement('option'); o.value = sp.slug; o.textContent = sp.name || sp.title || sp.slug; og.appendChild(o); });
      specSel.appendChild(og);
    });
    applyCurrentSpecialist();
  }).catch(function () {});
  specSel.addEventListener('change', function () { var sl = specSel.value; if (sl && (sl in specBodies)) { instr.value = specBodies[sl]; flashInstr(); updateDirty(); note.textContent = 'loaded role: ' + sl + ' - review, then Save'; } });
  resetLink.addEventListener('click', function () { instr.value = (currentRoleSlug && (currentRoleSlug in specBodies)) ? specBodies[currentRoleSlug] : loaded.instructions; flashInstr(); updateDirty(); note.textContent = 'reset to role default'; });

  // departments
  jget('/api/departments').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
    if (!j) return; var list = Array.isArray(j) ? j : (j.departments || []);
    list.forEach(function (d) { var o = document.createElement('option'); o.value = d.id; o.textContent = d.name || d.slug; idDept.appendChild(o); });
    if (agentObj && agentObj.department_id != null) idDept.value = String(agentObj.department_id);
  }).catch(function () {});

  // load + save agent
  function loadAgent() {
    note.textContent = 'loading...';
    jget('/api/agents/' + encodeURIComponent(slug) + '?body=true').then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(function (a) {
      agentObj = a;
      idTitle.value = a.title || '';
      idDept.value = (a.department_id != null) ? String(a.department_id) : '';
      idEnabled.classList.toggle('on', a.enabled !== false);
      idEnabled.setAttribute('aria-checked', a.enabled !== false ? 'true' : 'false');
      if (a.specialist_slug) currentRoleSlug = a.specialist_slug;
      applyCurrentSpecialist();
      setModel(a.model);
      instr.value = (a.specialist_body != null) ? a.specialist_body : '';
      loaded = { model: a.model || '', instructions: instr.value, identity: snapIdentity() };
      updateDirty();
      note.textContent = 'loaded - v' + (a.version || '?');
      hydrateMemory(a);
      loadAgentRules();
    }).catch(function (e) { note.textContent = 'load error: ' + e.message; });
  }
  function save() {
    var payload = { model: modelSel.value, specialist_body: instr.value, title: idTitle.value.trim() || slug, enabled: idEnabled.classList.contains('on') };
    if (idDept.value !== '') payload.department_id = parseInt(idDept.value, 10);
    note.textContent = 'saving...'; btnSave.disabled = true;
    jsend('/api/agents/' + encodeURIComponent(slug), 'PUT', payload).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); }).then(function (res) {
      if (!res.ok) note.textContent = 'save failed: ' + window.snErr(res.status, res.j);
      else { note.textContent = 'saved - v' + (res.j.version || '?'); loaded = { model: modelSel.value, instructions: instr.value, identity: snapIdentity() }; applyCurrentSpecialist(); updateDirty(); }
    }).catch(function (e) { note.textContent = 'save error: ' + e.message; }).then(function () { btnSave.disabled = false; });
  }
  btnSave.addEventListener('click', save);
  btnDiscard.addEventListener('click', function () {
    if (!agentObj) return; var a = agentObj;
    setModel(loaded.model); instr.value = loaded.instructions;
    idTitle.value = a.title || ''; idDept.value = (a.department_id != null) ? String(a.department_id) : '';
    idEnabled.classList.toggle('on', a.enabled !== false);
    applyCurrentSpecialist(); updateDirty(); note.textContent = 'discarded changes';
  });

  // the composed prompt is shown on the Preview tab (shell.js), from
  // /api/agents/<slug>/system-prompt. The block that stood here called /prompt (404).

  // rules
  var bdRules = $('bd-rules'), bdNote = $('bd-note'), msOpen = false;
  function renderRules(data) {
    var pols = (data && data.policies) || [];
    var applied = pols.filter(function (p) { return p.applied; });
    bdRules.style.position = 'relative';
    var field = applied.length
      ? applied.map(function (p) { return '<span class="sx-tag">' + esc(p.title) + '</span>'; }).join('')
      : '<span style="color:var(--ink-mute);font-size:13px">No rules applied - click to choose</span>';
    function group(scope, label) {
      var arr = pols.filter(function (p) { return (p.scope === 'agent' ? 'agent' : 'global') === scope; });
      if (!arr.length) return '';
      var h = '<div class="sx-grp">' + label + '</div>';
      arr.forEach(function (p) {
        h += '<label class="sx-ck" title="' + (p.body_md || '').replace(/"/g, '&quot;') + '">'
          + '<input type="checkbox" data-slug="' + p.slug + '" data-scope="' + p.scope + '"' + (p.applied ? ' checked' : '') + '>'
          + '<span>' + esc(p.title) + '</span></label>';
      });
      return h;
    }
    var list = pols.length ? (group('global', 'Global - shared, reusable') + group('agent', 'This agent - its own')) : '<div class="sx-hint">No rules yet. Add one below.</div>';
    bdRules.innerHTML = '<div id="ms-field" class="sx-msfield" tabindex="0">' + field + '<span class="sx-caret">&#9662;</span></div>'
      + '<div id="ms-panel" class="sx-mspanel" style="display:' + (msOpen ? 'block' : 'none') + '">' + list + '</div>';
    var fieldEl = $('ms-field'), panel = $('ms-panel');
    fieldEl.addEventListener('click', function () { msOpen = !msOpen; panel.style.display = msOpen ? 'block' : 'none'; });
    [].slice.call(panel.querySelectorAll('input[type=checkbox]')).forEach(function (cb) { cb.addEventListener('change', function (e) { e.stopPropagation(); onRuleToggle(cb); }); });
  }
  function onRuleToggle(cb) {
    var ruleSlug = cb.getAttribute('data-slug'), scope = cb.getAttribute('data-scope'), nowOn = cb.checked;
    var action = nowOn ? (scope === 'agent' ? 'clear' : 'include') : (scope === 'agent' ? 'exclude' : 'clear');
    cb.disabled = true; msOpen = true;
    jsend('/api/agents/' + encodeURIComponent(slug) + '/policies/' + encodeURIComponent(ruleSlug), 'PUT', { action: action }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { bdNote.textContent = 'rule update failed: ' + window.snErr(r.status, j); });
      bdNote.textContent = 'rule updated'; return loadAgentRules();
    }).catch(function (e) { bdNote.textContent = 'rule update error: ' + e.message; });
  }
  function loadAgentRules() {
    return jget('/api/agents/' + encodeURIComponent(slug) + '/policies').then(function (r) {
      if (!r.ok) { bdRules.innerHTML = '<div class="sx-hint">could not load rules: HTTP ' + r.status + '</div>'; return; }
      bdNote.textContent = 'loaded';
      return r.json().then(renderRules);
    }).catch(function (e) { bdRules.innerHTML = '<div class="sx-hint">rules load error: ' + e.message + '</div>'; });
  }
  // new rule modal
  var nrTitle = $('nr-title'), nrCat = $('nr-cat'), nrScope = $('nr-scope'), nrBody = $('nr-body'), nrWhy = $('nr-why'),
      nrAdd = $('nr-add'), nrNote = $('nr-note'), nrOpen = $('nr-open'), nrBack = $('nr-back'), nrCancel = $('nr-cancel');
  function nrShow() { nrNote.style.display = 'none'; nrNote.textContent = ''; nrBack.style.display = 'flex'; nrTitle.focus(); }
  function nrHide() { nrBack.style.display = 'none'; }
  function nrErr(m) { nrNote.style.display = 'block'; nrNote.textContent = m; }
  if (nrOpen) {
    nrOpen.addEventListener('click', nrShow);
    nrCancel.addEventListener('click', nrHide);
    nrBack.addEventListener('click', function (e) { if (e.target === nrBack) nrHide(); });
    nrAdd.addEventListener('click', function () {
      var title = (nrTitle.value || '').trim(), body = (nrBody.value || '').trim() || title;
      var why = (nrWhy.value || '').trim();
      if (!title) { nrErr('the rule text is required'); return; }
      if (!why) { nrErr('say why the rule exists: the date and what went wrong without it'); return; }
      nrNote.style.display = 'none'; nrAdd.disabled = true; nrAdd.textContent = 'Adding...';
      var payload = { title: title, category: nrCat.value, body_md: body, rationale: why };
      if (nrScope.value === 'agent') payload.agent_slug = slug;
      jsend('/api/policies', 'POST', payload).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); }).then(function (res) {
        var d = res.j.detail;
        if (Array.isArray(d)) d = d.map(function (x) { return String(x.msg || '').replace(/^Value error, /, ''); }).join('; ');
        if (!res.ok) { nrErr('create failed: ' + (d || res.j.error || res.status)); return; }
        return jsend('/api/agents/' + encodeURIComponent(slug) + '/policies/' + encodeURIComponent(res.j.slug), 'PUT', { action: 'include' }).then(function () {
          bdNote.textContent = (nrScope.value === 'agent' ? 'added for this agent: ' : 'added globally: ') + res.j.slug;
          nrTitle.value = ''; nrBody.value = ''; nrWhy.value = ''; msOpen = true; nrHide(); loadAgentRules();
        });
      }).catch(function (e) { nrErr('error: ' + e.message); }).then(function () { nrAdd.disabled = false; nrAdd.textContent = 'Add'; });
    });
  }

  // documents: the agent's attachments in mesh, the same list the Studio
  // shows, via core-api /api/agents/<slug>/documents. Replaces the agent_knowledge
  // card (retired by the 2026-06-23 design). Every change is recomposed server side.
  var kwNote = $('kw-note'), kwChips = $('kw-chips'), kwAddBtn = $('kw-add'), kwUpBtn = $('kw-upload'), kwFile = $('kw-file'),
      kwBack = $('kw-back'), kwTitleInp = $('kw-mtitleinp'), kwContent = $('kw-mcontent'), kwErr = $('kw-merr');
  var KW_ABBR = { text: 'TEXT', file: 'FILE', drive: 'DRIVE' };
  var DOCS = '/api/agents/' + encodeURIComponent(slug) + '/documents';
  function kwTag(kind) { return '<span class="sx-kwtag sx-kw-' + esc(kind) + '">' + (KW_ABBR[kind] || esc(kind)) + '</span>'; }
  function kwFail(r) { return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(window.snErr(r.status, j)); }); }
  function kwRenderChips(list) {
    if (!list.length) { kwChips.innerHTML = '<div class="sx-hint">No documents yet. Add a note or upload a file.</div>'; return; }
    kwChips.innerHTML = list.map(function (d) {
      return '<div class="sx-kwrow">' + kwTag(d.kind)
        + '<span class="sx-kwttl kw-open" data-guid="' + esc(d.guid) + '" title="Open ' + esc(d.title) + '">' + esc(d.title) + '</span>'
        + '<span class="sx-kwst">' + (d.pinned ? 'in prompt' : 'read on demand') + '</span>'
        + '<button type="button" class="kw-pin sx-kwpin' + (d.pinned ? ' on' : '') + '" data-guid="' + esc(d.guid) + '" data-on="' + (d.pinned ? '1' : '') + '">' + (d.pinned ? 'Pinned' : 'Pin') + '</button>'
        + '<button type="button" class="kw-del sx-kwdel" data-guid="' + esc(d.guid) + '" title="Remove">&times;</button></div>';
    }).join('');
    [].slice.call(kwChips.querySelectorAll('.kw-del')).forEach(function (b) { b.addEventListener('click', function () { kwDelete(b.getAttribute('data-guid')); }); });
    [].slice.call(kwChips.querySelectorAll('.kw-pin')).forEach(function (b) { b.addEventListener('click', function () { kwPin(b, !b.getAttribute('data-on')); }); });
    [].slice.call(kwChips.querySelectorAll('.kw-open')).forEach(function (b) { b.addEventListener('click', function () { kwView(b.getAttribute('data-guid')); }); });
  }
  // viewer: the document's text exactly as the agent has it (extracted markdown)
  var kvBack = $('kv-back'), kvTitle = $('kv-title'), kvMeta = $('kv-meta'), kvText = $('kv-text');
  function kwView(guid) {
    if (!kvBack) return;
    kvTitle.textContent = 'Document'; kvMeta.textContent = 'loading...'; kvText.textContent = '';
    kvBack.style.display = 'flex';
    jget(DOCS + '/' + encodeURIComponent(guid)).then(function (r) { if (!r.ok) return kwFail(r); return r.json(); }).then(function (d) {
      var body = d.content || '', nl = body.indexOf('\n');
      kvTitle.textContent = d.title || 'Document';
      kvText.textContent = (nl >= 0 ? body.slice(nl + 1) : body).replace(/^\s+/, '') || '(empty)';
      kvMeta.textContent = (d.pinned ? 'pinned: copied into the prompt' : 'not pinned: the agent reads it when needed') + ' · ' + body.length + ' chars';
    }).catch(function (e) { kvMeta.textContent = 'load error: ' + e.message; });
  }
  if (kvBack) {
    $('kv-close').addEventListener('click', function () { kvBack.style.display = 'none'; });
    kvBack.addEventListener('click', function (e) { if (e.target === kvBack) kvBack.style.display = 'none'; });
  }
  function kwLoadChips() {
    if (!kwChips) return Promise.resolve();
    kwNote.textContent = 'loading...';
    return jget(DOCS).then(function (r) { if (!r.ok) return kwFail(r); return r.json(); }).then(function (j) {
      var list = j.documents || []; kwRenderChips(list); kwNote.textContent = list.length + ' document' + (list.length === 1 ? '' : 's');
    }).catch(function (e) { kwNote.textContent = 'load error: ' + e.message; });
  }
  function kwShowErr(m) { kwErr.textContent = m; kwErr.style.display = m ? '' : 'none'; }
  function kwOpenModal() { kwShowErr(''); kwTitleInp.value = ''; kwContent.value = ''; kwBack.style.display = 'flex'; setTimeout(function () { kwTitleInp.focus(); }, 30); }
  function kwCloseModal() { kwBack.style.display = 'none'; }
  function kwDelete(guid) {
    kwNote.textContent = 'removing...';
    jsend(DOCS + '/' + encodeURIComponent(guid), 'DELETE', undefined).then(function (r) { if (r.status !== 204 && !r.ok) return kwFail(r); return kwLoadChips(); }).catch(function (e) { kwNote.textContent = 'delete error: ' + e.message; });
  }
  function kwPin(btn, on) {
    btn.disabled = true; kwNote.textContent = on ? 'pinning...' : 'unpinning...';
    jsend(DOCS + '/' + encodeURIComponent(btn.getAttribute('data-guid')) + '/pin', 'PUT', { pinned: on }).then(function (r) { if (!r.ok) return kwFail(r); return kwLoadChips(); }).catch(function (e) { kwNote.textContent = 'pin error: ' + e.message; btn.disabled = false; });
  }
  if (kwAddBtn) {
    kwAddBtn.addEventListener('click', function () { kwOpenModal(); });
    $('kw-mcancel').addEventListener('click', kwCloseModal);
    kwBack.addEventListener('click', function (e) { if (e.target === kwBack) kwCloseModal(); });
    $('kw-madd').addEventListener('click', function () {
      var title = kwTitleInp.value.trim(), content = kwContent.value.trim(), btn = this;
      if (!content) { kwShowErr('Text is required.'); kwContent.focus(); return; }
      kwShowErr(''); btn.disabled = true; btn.textContent = 'Adding...';
      jsend(DOCS, 'POST', { title: title, content: content }).then(function (r) { if (!r.ok) return kwFail(r); }).then(function () { kwCloseModal(); return kwLoadChips(); }).catch(function (e) { kwShowErr('Add failed: ' + e.message); }).then(function () { btn.disabled = false; btn.textContent = 'Add'; });
    });
    kwUpBtn.addEventListener('click', function () { kwFile.value = ''; kwFile.click(); });
    kwFile.addEventListener('change', function () {
      var f = kwFile.files && kwFile.files[0]; if (!f) return;
      kwUpBtn.disabled = true; kwNote.textContent = 'uploading ' + f.name + '...';
      fetch(DOCS + '/upload?name=' + encodeURIComponent(f.name), { method: 'POST', body: f, headers: { 'Content-Type': 'application/octet-stream', 'Accept': 'application/json' } })
        .then(function (r) { if (!r.ok) return kwFail(r); return kwLoadChips(); })
        .catch(function (e) { kwNote.textContent = 'upload failed: ' + e.message; })
        .then(function () { kwUpBtn.disabled = false; });
    });
    // documents live in mesh: without the memory module the list could only
    // fail (a 502 per visit, tester's map, 2026-10-31), so the card stays hidden
    fetch('/api/system/services', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var on = ((d && d.companions) || []).some(function (c) { return c.name === 'memory'; });
        if (!on) return;
        $('kw-card').hidden = false; kwLoadChips();
      }).catch(function () {});
  }

  // memory (mesh_workspace)
  var knMesh = $('kn-mesh'), knNote = $('kn-note'), knSave = $('kn-save'), knDiscard = $('kn-discard'), knDirty = $('kn-dirty'), knLoaded = '';
  function knUpdateDirty() { if (knDirty) knDirty.classList.toggle('show', knMesh.value !== knLoaded); }
  function hydrateMemory(a) { if (!knMesh) return; knMesh.value = a.mesh_workspace || ''; knLoaded = knMesh.value; knUpdateDirty(); knNote.textContent = 'loaded'; }
  if (knMesh) {
    knMesh.addEventListener('input', knUpdateDirty);
    knSave.addEventListener('click', function () {
      knNote.textContent = 'saving...'; knSave.disabled = true;
      jsend('/api/agents/' + encodeURIComponent(slug), 'PUT', { mesh_workspace: knMesh.value.trim() || null }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); }).then(function (res) {
        if (!res.ok) knNote.textContent = 'save failed: HTTP ' + res.status; else { knNote.textContent = 'saved - v' + (res.j.version || '?'); knLoaded = knMesh.value; knUpdateDirty(); }
      }).catch(function (e) { knNote.textContent = 'save error: ' + e.message; }).then(function () { knSave.disabled = false; });
    });
    knDiscard.addEventListener('click', function () { if (agentObj) hydrateMemory(agentObj); knNote.textContent = 'discarded changes'; });
  }

  // per-agent context handoff override
  var ctxOverride = $('ctx-agent-override'), ctxTokens = $('ctx-agent-tokens'),
      ctxEnabled = $('ctx-agent-enabled'), ctxNote = $('ctx-agent-note'),
      ctxEffective = $('ctx-agent-effective'), ctxDirty = $('ctx-agent-dirty'),
      ctxSave = $('ctx-agent-save'), ctxDiscard = $('ctx-agent-discard');
  var ctxLoaded = { override: false, tokens: 120000, enabled: true, global: 120000 };

  function ctxSnapshot() {
    return { override: !!ctxOverride.checked, tokens: parseInt(ctxTokens.value, 10) || 0, enabled: !!ctxEnabled.checked };
  }
  function ctxRenderDirty() {
    var s = ctxSnapshot();
    var dirty = s.override !== ctxLoaded.override || s.tokens !== ctxLoaded.tokens || s.enabled !== ctxLoaded.enabled;
    ctxDirty.classList.toggle('show', dirty);
    ctxTokens.disabled = !s.override; ctxEnabled.disabled = !s.override;
    ctxEffective.textContent = s.override ? ('effective: ' + s.tokens.toLocaleString() + ' tokens') : ('node default: ' + ctxLoaded.global.toLocaleString() + ' tokens');
  }
  function ctxApplyLoaded() {
    ctxOverride.checked = ctxLoaded.override;
    ctxTokens.value = ctxLoaded.tokens;
    ctxEnabled.checked = ctxLoaded.enabled;
    ctxRenderDirty();
  }
  function ctxLoad() {
    if (!ctxOverride) return;
    ctxNote.textContent = 'loading…';
    jget('/api/session-context-limits').then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status); return r.json();
    }).then(function (j) {
      var g = j.global || { threshold_tokens: 120000, enabled: true };
      var own = (j.agent_overrides || []).filter(function (x) { return x.agent_slug === slug; })[0];
      ctxLoaded = {
        override: !!own,
        tokens: own ? own.threshold_tokens : g.threshold_tokens,
        enabled: own ? own.enabled : g.enabled,
        global: g.threshold_tokens || 120000
      };
      ctxApplyLoaded(); ctxNote.textContent = own ? 'agent override' : 'using node default';
    }).catch(function (e) { ctxNote.textContent = 'load error: ' + e.message; });
  }
  if (ctxOverride) {
    [ctxOverride, ctxTokens, ctxEnabled].forEach(function (el) { el.addEventListener('change', ctxRenderDirty); });
    ctxTokens.addEventListener('input', ctxRenderDirty);
    ctxDiscard.addEventListener('click', function () { ctxApplyLoaded(); ctxNote.textContent = 'discarded changes'; });
    ctxSave.addEventListener('click', function () {
      var s = ctxSnapshot();
      if (s.override && (s.tokens < 10000 || s.tokens > 1000000)) { ctxNote.textContent = 'threshold must be 10,000–1,000,000'; return; }
      ctxSave.disabled = true; ctxNote.textContent = 'saving…';
      var request = s.override
        ? jsend('/api/session-context-limits/agents/' + encodeURIComponent(slug), 'PUT', { threshold_tokens: s.tokens, enabled: s.enabled, reason: 'updated in agent settings' })
        : jsend('/api/session-context-limits/agents/' + encodeURIComponent(slug) + '?reason=' + encodeURIComponent('agent override removed in agent settings'), 'DELETE', undefined);
      request.then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(window.snErr(r.status, j)); }); })
        .then(function () { ctxNote.textContent = 'saved · audited'; ctxLoad(); })
        .catch(function (e) { ctxNote.textContent = 'save error: ' + e.message; })
        .then(function () { ctxSave.disabled = false; });
    });
    ctxLoad();
  }

  loadAgent();
})();
