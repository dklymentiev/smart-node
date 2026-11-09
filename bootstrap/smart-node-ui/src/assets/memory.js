/* Memory: what the node's agents keep in memory (mesh), read-only. Search by meaning, or the newest documents, over
   all workspaces or one; a type and a period narrow it; a row opens the whole
   document beside the list. /memory/?ws=<workspace> opens one agent's memory
   (the agent page links here). core-api's /api/memory is the owner's alone. */
(function () {
  'use strict';
  var API = '/api/memory';
  var params = new URLSearchParams(location.search);
  var state = { ws: params.get('ws') || 'all', q: params.get('q') || '', type: params.get('type') || '',
                period: params.get('period') || '', offset: 0 };
  var spaces = [];
  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  async function request(url, options) {
    var r = await fetch(url, Object.assign({ credentials: 'same-origin', cache: 'no-store',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' } }, options || {}));
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'HTTP ' + r.status);
    return data;
  }
  function day(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function whose(ws) {
    var s = spaces.filter(function (x) { return x.workspace === ws; })[0];
    return s && s.agent ? s.agent.title : ws;
  }
  function since() {
    if (!state.period) return null;
    var d = new Date(Date.now() - Number(state.period) * 864e5);
    return d.toISOString().slice(0, 10);
  }
  function keepUrl() {
    var p = new URLSearchParams();
    if (state.ws !== 'all') p.set('ws', state.ws);
    if (state.q) p.set('q', state.q);
    if (state.type) p.set('type', state.type);
    if (state.period) p.set('period', state.period);
    history.replaceState(null, '', location.pathname + (p.toString() ? '?' + p : ''));
  }

  function paintSpaces(total) {
    var box = $('memSpaces');
    var chip = function (ws, label, n, empty) {
      return '<button type="button" class="mem-chip' + (state.ws === ws ? ' is-on' : '') + (empty ? ' is-empty' : '')
        + '" data-ws="' + esc(ws) + '" aria-pressed="' + (state.ws === ws) + '">' + esc(label)
        + ' <b>' + n + '</b></button>';
    };
    box.innerHTML = chip('all', 'All', total, false) + spaces.map(function (s) {
      return chip(s.workspace, s.agent ? s.agent.title : s.workspace, s.documents, !s.documents);
    }).join('');
  }
  function paintStatus(text, bad) {
    var el = $('memStatus');
    el.textContent = text || '';
    el.classList.toggle('is-bad', !!bad);
  }
  function rowHtml(d) {
    var meta = ['<span class="mem-who">' + esc(whose(d.workspace)) + '</span>'];
    if (d.type) meta.push('<span class="mem-type">' + esc(d.type) + '</span>');
    meta.push('<span>' + esc(day(d.created_at)) + '</span>');
    if (d.score != null) meta.push('<span title="How close to the search">' + Math.round(d.score * 100) + '% match</span>');
    return '<button type="button" class="mem-row" data-guid="' + esc(d.guid) + '" data-ws="' + esc(d.workspace) + '">'
      + '<span class="mem-title">' + esc(d.title) + '</span>'
      + '<span class="mem-meta">' + meta.join('') + '</span>'
      + '<span class="mem-preview">' + esc(d.preview || '') + '</span></button>';
  }

  var seq = 0;
  async function load(more) {
    var my = ++seq;
    if (!more) state.offset = 0;
    keepUrl();
    var list = $('memList');
    paintStatus('Loading…');
    try {
      var rows, hasMore = false;
      if (state.q) {
        var body = { query: state.q, limit: 30 };
        if (state.ws !== 'all') body.workspaces = [state.ws];
        if (state.type) body.type = state.type;
        if (since()) body.date_from = since();
        rows = (await request(API + '/search', { method: 'POST', body: JSON.stringify(body) })).results;
      } else {
        var p = new URLSearchParams({ workspace: state.ws, limit: '30', offset: String(state.offset) });
        if (state.type) p.set('type', state.type);
        var data = await request(API + '/documents?' + p);
        rows = data.documents;
        hasMore = data.more;
        var from = since();
        if (from) rows = rows.filter(function (d) { return (d.created_at || '') >= from; });
      }
      if (my !== seq) return;
      var html = rows.map(rowHtml).join('');
      if (more) list.insertAdjacentHTML('beforeend', html); else list.innerHTML = html;
      $('memMore').hidden = !hasMore;
      var shown = list.querySelectorAll('.mem-row').length;
      paintStatus(shown ? (state.q ? shown + ' found' : shown + ' newest') : (state.q ? 'Nothing found.' : 'No documents here.'));
    } catch (e) {
      if (my !== seq) return;
      if (!more) list.innerHTML = '';
      paintStatus('Could not read the memory: ' + e.message, true);
    }
  }

  async function open(guid, ws, btn) {
    document.querySelectorAll('.mem-row.is-open').forEach(function (b) { b.classList.remove('is-open'); });
    if (btn) btn.classList.add('is-open');
    var pane = $('memDoc');
    pane.hidden = false;
    document.body.classList.add('mem-doc-on');
    $('memDocBody').textContent = 'Loading…';
    try {
      var d = await request(API + '/documents/' + encodeURIComponent(guid) + '?workspace=' + encodeURIComponent(ws));
      $('memDocTitle').textContent = d.title;
      $('memDocMeta').innerHTML = '<span class="mem-who">' + esc(whose(d.workspace)) + '</span>'
        + (d.type ? '<span class="mem-type">' + esc(d.type) + '</span>' : '')
        + '<span>' + esc(day(d.created_at)) + '</span>';
      $('memDocTags').innerHTML = (d.tags || []).map(function (t) { return '<span class="chip">' + esc(t) + '</span>'; }).join('');
      $('memDocBody').textContent = d.content || '';
    } catch (e) {
      $('memDocBody').textContent = 'Could not open it: ' + e.message;
    }
  }
  function closeDoc() {
    $('memDoc').hidden = true;
    document.body.classList.remove('mem-doc-on');
    document.querySelectorAll('.mem-row.is-open').forEach(function (b) { b.classList.remove('is-open'); });
  }

  async function start() {
    $('memQuery').value = state.q;
    $('memPeriod').value = state.period;
    try {
      var w = await request(API + '/workspaces');
      spaces = w.workspaces || [];
      paintSpaces(w.total || 0);
      var t = await request(API + '/types');
      $('memType').innerHTML = '<option value="">All types</option>' + (t.types || []).map(function (x) {
        return '<option value="' + esc(x.type) + '">' + esc(x.type) + ' (' + x.count + ')</option>';
      }).join('');
      $('memType').value = state.type;
    } catch (e) {
      paintStatus('Could not read the memory: ' + e.message, true);
      return;
    }
    load(false);
  }

  var timer = null;
  $('memQuery').addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () { state.q = $('memQuery').value.trim(); load(false); }, 400);
  });
  $('memQuery').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { clearTimeout(timer); state.q = $('memQuery').value.trim(); load(false); }
  });
  $('memType').addEventListener('change', function () { state.type = $('memType').value; load(false); });
  $('memPeriod').addEventListener('change', function () { state.period = $('memPeriod').value; load(false); });
  $('memSpaces').addEventListener('click', function (e) {
    var b = e.target.closest('.mem-chip');
    if (!b) return;
    state.ws = b.dataset.ws;
    document.querySelectorAll('.mem-chip').forEach(function (c) {
      c.classList.toggle('is-on', c === b);
      c.setAttribute('aria-pressed', c === b ? 'true' : 'false');
    });
    load(false);
  });
  $('memList').addEventListener('click', function (e) {
    var b = e.target.closest('.mem-row');
    if (b) open(b.dataset.guid, b.dataset.ws, b);
  });
  $('memMore').addEventListener('click', function () { state.offset += 30; load(true); });
  $('memDocClose').addEventListener('click', closeDoc);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !$('memDoc').hidden) closeDoc(); });
  start();
})();
