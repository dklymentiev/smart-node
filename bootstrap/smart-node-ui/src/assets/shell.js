// Smart Node panel shell.

// ------------------------------------------------ signed out
// When the sign-in session ends, forward auth answers every /api call with a
// redirect to the login page on another origin. A fetch that follows it fails
// CORS and rejects with a bare TypeError, so a save simply did nothing and only
// a reload showed the login page. Every same-origin call is now made with
// redirect:'manual' (the panel's API never redirects), the bounce comes back as
// an 'opaqueredirect' response, and the page says plainly that it is signed out.
(function () {

  // One way to say what went wrong. core-api answers {detail: "text"}, or on a 422
  // {detail: [{loc, msg}]} naming the fields; a body with neither (a proxy's
  // HTML page, no answer at all) falls back to what the status means.
  var STATUS_TEXT = {
    401: 'You are signed out. Reload the page to sign in again.',
    403: 'You are not allowed to do this.',
    404: 'Not found. It may have been removed; reload the page.',
    409: 'It changed meanwhile. Reload the page and try again.',
    413: 'Too large.',
    429: 'Too many requests. Wait a moment and try again.'
  };
  function snErr(status, body) {
    var d = body && (body.detail != null ? body.detail : (body.error || body.message));
    if (Array.isArray(d)) {
      d = d.map(function (x) {
        if (!x || typeof x !== 'object') return String(x);
        var at = (x.loc || []).filter(function (p) { return p !== 'body' && p !== 'query' && p !== 'path'; }).join('.');
        return (at ? at + ': ' : '') + (x.msg || JSON.stringify(x));
      }).join('; ');
    } else if (d && typeof d === 'object') {
      d = d.message || d.msg || JSON.stringify(d);
    }
    if (d) return String(d);
    if (!status) return 'The node did not answer. Check the connection and try again.';
    if (STATUS_TEXT[status]) return STATUS_TEXT[status];
    if (status >= 500) return 'The node failed to do this (HTTP ' + status + '). Its log is on Systems.';
    return 'HTTP ' + status;
  }
  // For a fetch chain: a response that is not ok becomes an Error with snErr's text.
  function snFail(r) {
    return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(snErr(r.status, j)); });
  }
  window.snErr = snErr;
  // One short line at the bottom of the screen for an outcome (saved, discarded, failed):
  // the kit's .sn-toast. Not for progress ("loading...").
  var toastEl = null, toastTimer = 0;
  window.snToast = function (text) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'sn-toast';
      toastEl.setAttribute('role', 'status');
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove('is-on'); }, 2600);
  };
  window.snFail = snFail;
  var raw = window.fetch.bind(window);
  var shown = false;
  function sameOrigin(input) {
    try { return new URL(typeof input === 'string' ? input : input.url, location.href).origin === location.origin; }
    catch (e) { return false; }
  }
  function bounced(r) { return !!r && (r.type === 'opaqueredirect' || r.status === 401); }
  function wall() {
    if (shown) return;
    shown = true;
    var el = document.createElement('div');
    el.className = 'modal-scrim';
    el.setAttribute('role', 'alertdialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML = '<div class="modal-panel"><div class="modal-head"><h2>You have been signed out</h2></div>'
      + '<div class="modal-form"><p style="margin:0 0 16px;color:var(--ink-2);line-height:1.5">Your session ended, so nothing on this page can be saved. '
      + 'Sign in again to continue.</p><div style="display:flex;justify-content:flex-end"><button class="btn-primary" type="button">Sign in again</button></div></div></div>';
    el.querySelector('button').addEventListener('click', function () { location.reload(); });
    document.body.appendChild(el);
  }
  window.fetch = function (input, init) {
    if (sameOrigin(input) && !(init && init.redirect)) init = Object.assign({}, init, { redirect: 'manual' });
    return raw(input, init).then(function (r) { if (bounced(r)) wall(); return r; });
  };
  // A tab left open learns it when it comes back, before anything is typed.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || shown) return;
    raw('/version.txt', { redirect: 'manual', cache: 'no-store' })
      .then(function (r) { if (bounced(r)) wall(); })
      .catch(function () { /* offline is not signed out */ });
  });
})();

(function () {
  // ---------------------------------------------------- chrome (menu/user)
  function initChrome() {
    var toggle = document.getElementById('menuToggle');
    var nav = document.getElementById('topbarNav');
    var scrim = document.getElementById('menuScrim');

    function toggleMenu() {
      if (!nav || !scrim) return;
      var open = nav.classList.toggle('open');
      scrim.classList.toggle('open', open);
    }
    if (toggle && nav && scrim) {
      toggle.addEventListener('click', toggleMenu);
      scrim.addEventListener('click', toggleMenu);
      nav.querySelectorAll('.nav-item').forEach(function (el) {
        el.addEventListener('click', function () { if (window.innerWidth <= 920) toggleMenu(); });
      });
    }

    // A section a module brings shows only where that module is installed, as the
    // Studio does: a node without them listed Memory, Planner and Workflows that
    // led to empty pages or errors (look audit, 2026-10-30). The installer's
    // records of what is installed come with the services list.
    var NEEDS = { memory: 'memory', planner: 'tracking', workflows: 'workflows' };
    if (nav) {
      fetch('/api/system/services', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          var on = (d.companions || []).map(function (c) { return c.name; });
          // hidden until known (shell.css): shown when the module is there, so
          // the menu never draws long and then shrinks
          nav.querySelectorAll('.nav-item').forEach(function (el) {
            var need = NEEDS[el.getAttribute('data-group')];
            if (need && on.indexOf(need) >= 0) el.classList.add('is-installed');
          });
          // remembered for the next page: ready.js puts it on <html> before the first
          // paint, so the menu does not grow on every load
          var mods = Object.keys(NEEDS).filter(function (g) { return on.indexOf(NEEDS[g]) >= 0; }).join(' ');
          document.documentElement.setAttribute('data-mods', mods);
          try { localStorage.setItem('sn-panel-mods', mods); } catch (e) {}
        }).catch(function () {});
    }

    var userMenu = document.getElementById('userMenu');
    if (userMenu) {
      userMenu.addEventListener('click', function (e) {
        userMenu.classList.toggle('open');
        e.stopPropagation();
      });
      document.addEventListener('click', function () { userMenu.classList.remove('open'); });
    }
  }

  // ---------------------------------------------------- helpers
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
  function fmtTs(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { hour12: false, month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  // systemd writes "Sat 2026-10-31 02:00:47 UTC": the same moment on the person's
  // clock, in the panel's one format ("Oct 31, 21:00")
  function fmtSince(t) {
    var m = /(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC/.exec(String(t || ''));
    var d = m ? new Date(m[1] + 'T' + m[2] + 'Z') : new Date(t);
    return isNaN(d.getTime()) ? String(t || '') : d.toLocaleString(undefined, { hour12: false, month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function fmtUptime(sec) {
    if (!sec || sec < 0) return '-';
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }
  function bootIso(sec) {
    if (!sec) return '-';
    return new Date(Date.now() - sec * 1000).toLocaleString(undefined, { hour12: false, month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function fmtBytes(mb) {
    if (mb >= 1024) return (mb / 1024).toFixed(1) + ' GB';
    return mb + ' MB';
  }
  function setText(node, txt) { if (node) node.textContent = txt; }

  // ---------------------------------------------------- live-dot helper
  function setLive(generatedAt) {
    var dot = $('#liveDot'); var last = $('#lastRefresh');
    if (dot) dot.classList.remove('off');   // any successful render clears the offline state
    if (last) setText(last, generatedAt ? fmtTs(generatedAt) : '-');
    if (!dot || !generatedAt) return;
    var age = (Date.now() - new Date(generatedAt).getTime()) / 1000;
    dot.classList.remove('live', 'stale');
    if (age < 90) dot.classList.add('live');
    else dot.classList.add('stale');
  }

  // Backend unreachable / non-2xx: show a clear "not connected" state instead of
  // sitting forever on the initial "Loading …" placeholders.
  function setOffline(reason) {
    var dot = $('#liveDot');
    if (dot) { dot.classList.remove('live', 'stale'); dot.classList.add('off'); }
    setText($('#lastRefresh'), reason || 'not connected');
    setText($('#nextRefresh'), '-');
    $$('.empty-row td, .provider-empty, .budgets-empty, .agent-empty').forEach(function (el) {
      if (/loading/i.test(el.textContent)) el.textContent = 'Not connected';
    });
  }

  // ---------------------------------------------------- overview
  function renderOverview(d) {
    setText($('[data-field="uptime"]'), fmtUptime(d.uptime_seconds));
    setText($('[data-field="uptime-since"]'), 'since ' + bootIso(d.uptime_seconds));

    var l = d.load || {};
    setText($('[data-field="load1"]'), l['1m'] != null ? l['1m'].toFixed(2) : '-');
    setText($('[data-field="load-breakdown"]'), '1m ' + (l['1m']||0).toFixed(2) + ' · 5m ' + (l['5m']||0).toFixed(2) + ' · 15m ' + (l['15m']||0).toFixed(2));

    var m = d.mem || {};
    var memPct = m.total_mb ? Math.round((m.used_mb / m.total_mb) * 100) : 0;
    setText($('[data-field="mem-used"]'), fmtBytes(m.used_mb || 0));
    setText($('[data-field="mem-of"]'), 'of ' + fmtBytes(m.total_mb || 0) + ' · ' + memPct + '%');
    var memBar = $('[data-field="mem-bar"]'); if (memBar) memBar.style.width = memPct + '%';
    var memCard = $('[data-stat="memory"]'); if (memCard) memCard.classList.toggle('warn', memPct >= 85);

    var disk = d.disk || {};
    setText($('[data-field="disk-pct"]'), (disk.pct != null ? disk.pct : '-') + '%');
    setText($('[data-field="disk-of"]'), (disk.used_gb || '-') + ' GB of ' + (disk.total_gb || '-') + ' GB');
    var dBar = $('[data-field="disk-bar"]'); if (dBar) dBar.style.width = (disk.pct || 0) + '%';
    var diskCard = $('[data-stat="disk"]'); if (diskCard) diskCard.classList.toggle('warn', (disk.pct || 0) >= 85);

    var c = d.containers || {};
    // core cannot always see the container engine; an unknown count is not a card
    var cCard = $('[data-stat="containers"]'); if (cCard) cCard.hidden = c.running == null;
    setText($('[data-field="containers"]'), c.running != null ? c.running : '-');
    setText($('[data-field="containers-foot"]'), 'running');
    var strip = $('#containerStrip'); var listEl = $('#containerNames');
    if (listEl && c.names) {
      listEl.innerHTML = '';
      (c.names || []).forEach(function (n) {
        var li = document.createElement('li'); li.textContent = n; listEl.appendChild(li);
      });
      if (strip) strip.hidden = (c.names || []).length === 0;
    }

    setText($('[data-field="events24h"]'), d.events_24h != null ? d.events_24h : '-');

    setLive(d.generated_at);
  }

  // ---------------------------------------------------- Overview switches
  // Agents (the runner) and Models (gateway + voice: every unit that
  // talks to a model provider). core-api admits only a signed-in
  // session and hands start/stop to admin-runner, which wants a confirm
  // first: the first POST returns token(s), the second one spends them.
  function initOverviewSwitches() {
    Array.prototype.forEach.call(document.querySelectorAll('.switch-block'), initSwitchBlock);
  }
  // A question that must be answered, in the kit's dialog in the middle of the
  // screen: a short title, a line on what follows, the action named on its
  // button; resolves true for the action, false for Cancel or Escape.
  // A message with one button (an error a person must see), in the same dialog.
  function snTell(title, note) {
    var d = document.createElement('dialog');
    d.className = 'sn-dialog';
    d.innerHTML = '<h2 class="sn-dialog__q"></h2><p class="sn-dialog__note"></p><div class="sn-dialog__row">'
      + '<button type="button" class="sn-btn sn-btn--primary">OK</button></div>';
    d.querySelector('.sn-dialog__q').textContent = title;
    d.querySelector('.sn-dialog__note').textContent = note || '';
    d.querySelector('.sn-dialog__note').hidden = !note;
    d.querySelector('button').addEventListener('click', function () { d.close(); });
    d.addEventListener('close', function () { d.remove(); });
    document.body.appendChild(d);
    d.showModal();
  }
  function snAsk(question, note, yes, danger) {
    return new Promise(function (resolve) {
      var d = document.createElement('dialog');
      d.className = 'sn-dialog';
      d.innerHTML = '<h2 class="sn-dialog__q"></h2><p class="sn-dialog__note"></p><div class="sn-dialog__row">'
        + '<button type="button" class="sn-btn sn-btn--ghost" data-no>Cancel</button>'
        + '<button type="button" class="sn-btn ' + (danger ? 'sn-btn--danger' : 'sn-btn--primary') + '" data-yes></button></div>';
      d.querySelector('.sn-dialog__q').textContent = question;
      d.querySelector('.sn-dialog__note').textContent = note || '';
      d.querySelector('.sn-dialog__note').hidden = !note;
      d.querySelector('[data-yes]').textContent = yes;
      var answer = false;
      d.querySelector('[data-no]').addEventListener('click', function () { d.close(); });
      d.querySelector('[data-yes]').addEventListener('click', function () { answer = true; d.close(); });
      d.addEventListener('close', function () { d.remove(); resolve(answer); });
      document.body.appendChild(d);
      d.showModal();
    });
  }
  function initSwitchBlock(block) {
    var q = function (s) { return block.querySelector(s); };
    var sw = q('.js-switch'), errBox = q('.js-error');
    var url = block.getAttribute('data-endpoint');
    var pending = null;   // {action, proof} while the question is open
    function showError(msg) { if (errBox) { errBox.textContent = msg || ''; errBox.hidden = !msg; } }
    function render(s) {
      var active = s.active || 'unknown';
      var on = active === 'active';
      var moving = active === 'activating' || active === 'deactivating';
      sw.classList.toggle('on', on);
      sw.setAttribute('aria-checked', on ? 'true' : 'false');
      sw.disabled = moving || !!pending || active === 'unknown';
      var label = on ? 'running' : (active === 'inactive' || active === 'failed' ? 'stopped' : active);
      if (block.getAttribute('data-switch') === 'runner' && label === 'stopped') label = 'paused';
      setText(q('.js-state'), label);
      var since = s.since ? (on ? 'running since ' : label + ' since ') + fmtSince(s.since) : '';
      if (active === 'partial' && s.units) {
        since = s.units.map(function (u) { return u.unit.replace(/^smart-node-|\.service$/g, '') + ' ' + u.active; }).join(' · ');
      }
      setText(q('.js-since'), since);
      var dot = q('.js-dot');
      if (dot) { dot.classList.remove('ok', 'warn', 'fail'); dot.classList.add(on ? 'ok' : (moving || active === 'partial' ? 'warn' : 'fail')); }
    }
    function load() {
      return fetch(url, { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(render)
        .catch(function () { render({}); });
    }
    function post(body) {
      return fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error(snErr(r.status, j));
          return j;
        });
      });
    }
    sw.addEventListener('click', function () {
      if (sw.disabled) return;
      showError('');
      var action = sw.classList.contains('on') ? 'stop' : 'start';
      sw.disabled = true;
      post({ action: action }).then(function (j) {
        if (!j.needs_confirm) { render(j.state || {}); return; }
        // /runner answers one token, /models one per unit; pass back whichever came.
        var proof = j.confirm_tokens ? { confirm_tokens: j.confirm_tokens } : { confirm_token: j.confirm_token };
        pending = { action: action, proof: proof };
        var yes = action === 'start' ? 'Start' : (block.getAttribute('data-switch') === 'runner' ? 'Pause' : 'Stop');
        snAsk(block.getAttribute('data-ask-' + action), block.getAttribute('data-note-' + action), yes, action === 'stop').then(function (ok) {
          if (ok) confirmed(); else { pending = null; sw.disabled = false; }
        });
      }).catch(function (e) { showError(e.message); sw.disabled = false; });
    });
    function confirmed() {
      var p = pending, body = { action: p.action };
      showError('');
      Object.keys(p.proof).forEach(function (k) { body[k] = p.proof[k]; });
      post(body).then(function (j) {
        pending = null;
        var res = j.result || {};
        if (res.exit_code) showError('systemctl ' + p.action + ' failed: ' + (res.stderr || ('exit ' + res.exit_code)));
        if (j.failed && j.failed.length) {
          showError(j.failed.map(function (u) { var r = (j.results || {})[u] || {}; return u + ': ' + (r.stderr || ('exit ' + r.exit_code)); }).join('; '));
        }
        render(j.state || {});
      }).catch(function (e) { pending = null; showError(e.message); load(); });
    }
    load();
    setInterval(function () { if (!pending) load(); }, 10000);
  }

  function startOverviewPoll() {
    var INTERVAL = 30; // seconds
    var countdown = INTERVAL;

    function refresh() {
      fetch('/api/system/overview', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { renderOverview(d); countdown = INTERVAL; })
        .catch(function () { setOffline('not connected'); });
    }

    function tick() {
      countdown -= 1;
      if (countdown <= 0) { refresh(); return; }
      setText($('#nextRefresh'), String(countdown));
    }

    refresh();
    setInterval(tick, 1000);
  }

  // ---------------------------------------------------- events
  var seenIds = new Set();
  function renderEvents(d) {
    // First paint: nothing seen yet, so nothing is "new" -- only later refreshes flash.
    var firstPaint = seenIds.size === 0;
    var rows = d.events || [];
    var body = $('#eventsBody'); if (!body) return;

    // populate filters
    var typeSel = $('#filterType'); var actorSel = $('#filterActor');
    if (typeSel && actorSel) {
      var types = new Set(); var actors = new Set();
      rows.forEach(function (r) { if (r.event_type) types.add(r.event_type); if (r.actor) actors.add(r.actor); });
      function ensure(sel, set) {
        var have = new Set(Array.from(sel.options).map(function(o){return o.value;}));
        Array.from(set).sort().forEach(function (v) {
          if (!have.has(v)) {
            var o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o);
          }
        });
      }
      ensure(typeSel, types); ensure(actorSel, actors);
    }

    var ft = (typeSel && typeSel.value) || '';
    var fa = (actorSel && actorSel.value) || '';
    var filtered = rows.filter(function (r) {
      if (ft && r.event_type !== ft) return false;
      if (fa && r.actor !== fa) return false;
      return true;
    });

    if (filtered.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="5">No events match the current filter.</td></tr>';
    } else {
      body.innerHTML = '';
      filtered.forEach(function (r) {
        var tr = document.createElement('tr');
        var key = r.ts + '|' + r.event_type + '|' + (r.entity_id || '');
        var fresh = !firstPaint && !seenIds.has(key); seenIds.add(key);
        if (fresh) tr.classList.add('flash');

        var entity = r.entity_type ? (r.entity_type + (r.entity_id ? '/' + r.entity_id : '')) : '-';
        var payload = r.payload && Object.keys(r.payload).length ? JSON.stringify(r.payload) : '-';

        tr.innerHTML =
          '<td class="col-ts">' + fmtTs(r.ts) + '</td>' +
          '<td class="col-actor">' + escapeHtml(r.actor || '-') + '</td>' +
          '<td class="col-type">' + escapeHtml(r.event_type || '-') + '</td>' +
          '<td class="col-entity">' + escapeHtml(entity) + '</td>' +
          '<td class="col-payload" title="' + escapeAttr(payload) + '">' + escapeHtml(payload) + '</td>';
        body.appendChild(tr);
        if (fresh) setTimeout(function () { tr.classList.remove('flash'); }, 1400);
      });
    }

    setText($('#rowCount'), filtered.length + (filtered.length === rows.length ? '' : ' / ' + rows.length));
    setLive(d.generated_at);
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s).replace(/'/g, '&#39;'); }

  function startEventsPoll() {
    var INTERVAL = 15 * 1000;
    var lastData = null;
    function refresh() {
      fetch('/api/events?limit=200', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { lastData = d; renderEvents(d); })
        .catch(function () { setOffline('not connected'); });
    }
    refresh();
    setInterval(refresh, INTERVAL);

    $('#filterType') && $('#filterType').addEventListener('change', function () { if (lastData) renderEvents(lastData); });
    $('#filterActor') && $('#filterActor').addEventListener('change', function () { if (lastData) renderEvents(lastData); });
  }

  // ---------------------------------------------------- messages
  // Clicking a thread label narrows the table to that one conversation.
  var msgThread = '';
  var msgLast = null;

  // A short, readable label for a thread id: 'desk:3f2a...' -> 'desk 3f2a',
  // 'web:web:owner' -> 'web owner', an email Message-ID -> 'mail 7182', a bare
  // Telegram chat id -> 'chat 9396'. The full id is in the tooltip.
  function msgThreadLabel(t) {
    if (!t) return '';
    var raw = String(t).replace(/^<|>$/g, '');
    if (raw.indexOf('@') > 0 && raw.indexOf(':') < 0) {
      var local = raw.split('@')[0].replace(/[^A-Za-z0-9]/g, '');
      return 'mail ' + local.slice(-4);
    }
    if (/^-?\d+$/.test(raw)) return 'chat ' + raw.slice(-4);
    var parts = raw.split(':').filter(Boolean);
    var head = parts[0] || '';
    var tail = parts.length > 1 ? parts[parts.length - 1] : '';
    if (tail.length > 8) tail = tail.slice(0, 4);
    return tail && tail !== head ? head + ' ' + tail : head;
  }

  // Delivery state per row. 'processed' is the normal case and stays quiet;
  // 'blocked' and 'failed' carry a word, not colour alone, and the reason on hover.
  function msgStatusCell(r) {
    var st = r.status || '';
    if (!st) return '<td class="col-status">-</td>';
    var cls = st === 'failed' ? 'badge-err' : (st === 'blocked' ? 'badge-warn' : '');
    var tip = r.error ? ' title="' + escapeAttr(r.error) + '"' : '';
    return '<td class="col-status"' + tip + '>' +
      (cls ? '<span class="badge ' + cls + '">' + escapeHtml(st) + '</span>'
           : '<span class="msg-status-ok">' + escapeHtml(st) + '</span>') +
      '</td>';
  }

  function msgSetThread(t) {
    msgThread = t || '';
    var chip = $('#threadChip');
    if (chip) {
      chip.hidden = !msgThread;
      chip.innerHTML = msgThread
        ? '<span class="filter-label">Thread</span><span title="' + escapeAttr(msgThread) + '">' +
          escapeHtml(msgThreadLabel(msgThread)) +
          '</span><button type="button" aria-label="clear thread filter">&times;</button>'
        : '';
      var x = chip.querySelector('button');
      if (x) x.addEventListener('click', function () { msgSetThread(''); });
    }
    if (msgLast) renderMessages(msgLast);
  }

  function renderMessages(d) {
    msgLast = d;
    // First paint: nothing seen yet, so nothing is "new" -- only later refreshes flash.
    var firstPaint = seenIds.size === 0;
    var rows = d.messages || [];
    var body = $('#messagesBody'); if (!body) return;

    var chanSel = $('#filterChannel');
    if (chanSel) {
      var chans = new Set();
      rows.forEach(function (r) { if (r.channel_type) chans.add(r.channel_type); });
      var have = new Set(Array.from(chanSel.options).map(function(o){return o.value;}));
      Array.from(chans).sort().forEach(function (v) {
        if (!have.has(v)) {
          var o = document.createElement('option'); o.value = v; o.textContent = v; chanSel.appendChild(o);
        }
      });
    }

    var fc = (chanSel && chanSel.value) || '';
    var fd = ($('#filterDirection') && $('#filterDirection').value) || '';
    var fq = ($('#filterSearch') && $('#filterSearch').value || '').toLowerCase().trim();
    var fs = ($('#filterStatus') && $('#filterStatus').value) || '';

    var filtered = rows.filter(function (r) {
      if (fc && r.channel_type !== fc) return false;
      if (fd && r.direction !== fd) return false;
      if (fs && r.status !== fs) return false;
      if (msgThread && r.thread_id !== msgThread) return false;
      if (fq) {
        var hay = ((r.sender||'') + ' ' + (r.from_name||'') + ' ' + (r.recipient||'') + ' ' +
                   (r.subject||'') + ' ' + (r.content||'') + ' ' + (r.thread_id||'')).toLowerCase();
        if (hay.indexOf(fq) === -1) return false;
      }
      return true;
    });

    if (filtered.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="9">No messages match the current filter.</td></tr>';
    } else {
      body.innerHTML = '';
      filtered.forEach(function (r) {
        var tr = document.createElement('tr');
        var key = 'm' + r.id;
        var fresh = !firstPaint && !seenIds.has(key); seenIds.add(key);
        if (fresh) tr.classList.add('flash');
        if (r.direction === 'inbound')  tr.classList.add('row-warn');
        if (r.direction === 'outbound') tr.classList.add('row-ok');

        var rawSender = r.sender || '-';
        var rawRecipient = r.recipient || '-';
        var senderDisplay = rawSender;
        if (r.channel_type === 'email' && r.direction === 'inbound' && r.from_name) {
          senderDisplay = r.from_name;
        }
        var dirShort = r.direction === 'inbound' ? 'in'
                      : r.direction === 'outbound' ? 'out'
                      : (r.direction || '-');

        var payloadCell;
        var fullText = r.content || '';

        if (r.channel_type === 'email') {
          var subj = (r.subject || '').trim();
          var isReply = !!r.in_reply_to || /^re:/i.test(subj);
          var displaySubj = subj || '(no subject)';
          if (displaySubj.length > 80) displaySubj = displaySubj.slice(0, 80) + '…';
          var badge = isReply
            ? '<span class="msg-badge msg-reply" title="Reply">re</span> '
            : '';
          payloadCell =
            '<td class="col-payload" title="' + escapeAttr(subj || fullText) + '">' +
              badge + '<span class="msg-subject">' + escapeHtml(displaySubj) + '</span>' +
            '</td>';
        } else {
          var preview = fullText.replace(/\s+/g, ' ').trim();
          if (preview.length > 220) preview = preview.slice(0, 220) + '…';
          payloadCell =
            '<td class="col-payload" title="' + escapeAttr(fullText) + '">' +
              escapeHtml(preview || '-') +
            '</td>';
        }

        tr.innerHTML =
          '<td class="col-id">' + escapeHtml(String(r.id)) + '</td>' +
          '<td class="col-ts">' + fmtTs(r.received_at) + '</td>' +
          '<td class="col-actor">' + escapeHtml(r.channel_type || '-') + '</td>' +
          '<td class="col-type col-dir-' + escapeAttr(r.direction || '') + '">' + escapeHtml(dirShort) + '</td>' +
          msgStatusCell(r) +
          '<td class="col-sender" title="' + escapeAttr(rawSender) + '">' + escapeHtml(senderDisplay) + '</td>' +
          '<td class="col-recipient" title="' + escapeAttr(rawRecipient) + '">' + escapeHtml(rawRecipient) + '</td>' +
          (r.thread_id
            ? '<td class="col-thread"><button type="button" class="msg-thread" data-thread="' + escapeAttr(r.thread_id) +
              '" title="' + escapeAttr(r.thread_id) + ' (click: show only this conversation)">' +
              escapeHtml(msgThreadLabel(r.thread_id)) + '</button></td>'
            : '<td class="col-thread">-</td>') +
          payloadCell;
        body.appendChild(tr);
        if (fresh) setTimeout(function () { tr.classList.remove('flash'); }, 1400);
      });
    }

    setText($('#rowCount'), filtered.length + (filtered.length === rows.length ? '' : ' / ' + rows.length));
    setLive(d.generated_at);
  }

  function startMessagesPoll() {
    var INTERVAL = 15 * 1000;
    var lastData = null;
    function refresh() {
      fetch('/api/messages?limit=200', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { lastData = d; renderMessages(d); })
        .catch(function () { setOffline('not connected'); });
    }
    refresh();
    setInterval(refresh, INTERVAL);

    $('#filterChannel')   && $('#filterChannel').addEventListener('change',   function () { if (lastData) renderMessages(lastData); });
    $('#filterDirection') && $('#filterDirection').addEventListener('change', function () { if (lastData) renderMessages(lastData); });
    $('#filterSearch')    && $('#filterSearch').addEventListener('input',     function () { if (lastData) renderMessages(lastData); });
    $('#filterStatus')    && $('#filterStatus').addEventListener('change',    function () { if (lastData) renderMessages(lastData); });
    // One delegated handler: rows are rebuilt on every refresh.
    $('#messagesBody')    && $('#messagesBody').addEventListener('click', function (ev) {
      var b = ev.target.closest && ev.target.closest('.msg-thread');
      if (b) msgSetThread(b.getAttribute('data-thread'));
    });
  }

  // ---------------------------------------------------- services
  function renderServices(d) {
    var s = d.summary || {};
    setText($('[data-field="companions"]'),  s.companions_total != null ? s.companions_total : '-');
    setText($('[data-field="containers"]'),  s.containers_running != null ? (s.containers_running + ' / ' + s.containers_total) : '-');
    setText($('[data-field="systemd"]'),     s.systemd_active != null ? (s.systemd_active + ' / ' + s.systemd_total) : '-');
    setText($('[data-field="issues"]'),      s.issues != null ? s.issues : '-');
    var issuesCard = $('#issuesCard');
    if (issuesCard) {
      issuesCard.classList.remove('warn', 'fail');
      if (s.issues > 0) issuesCard.classList.add(s.issues >= 3 ? 'fail' : 'warn');
    }

    var rows = ([]).concat(d.companions || [], d.containers || [], d.systemd || []);
    var fk = ($('#filterKind')   || {}).value || '';
    var fh = ($('#filterHealth') || {}).value || '';
    var filtered = rows.filter(function (r) {
      if (fk && r.kind !== fk) return false;
      if (fh && r.health !== fh) return false;
      return true;
    });

    // Stable sort: health (fail > warn > ok), then kind, then name
    var order = { fail: 0, warn: 1, ok: 2 };
    filtered.sort(function (a, b) {
      var ao = order[a.health] != null ? order[a.health] : 9;
      var bo = order[b.health] != null ? order[b.health] : 9;
      if (ao !== bo) return ao - bo;
      if (a.kind !== b.kind) return a.kind.localeCompare(b.kind);
      return (a.name || '').localeCompare(b.name || '');
    });

    var body = $('#servicesBody'); if (!body) return;
    if (filtered.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="7">No services match the current filter.</td></tr>';
    } else {
      body.innerHTML = '';
      filtered.forEach(function (r) {
        var tr = document.createElement('tr');
        if (r.health === 'warn') tr.classList.add('row-warn');
        if (r.health === 'fail') tr.classList.add('row-fail');
        var detail = '';
        if (r.kind === 'companion') {
          detail = (r.deps && r.deps.length ? 'deps: ' + r.deps.join(', ') : '') + (r.ts ? ' · ' + fmtTs(r.ts) : '');
        } else if (r.kind === 'container') {
          detail = '';
        } else if (r.kind === 'systemd') {
          detail = r.desc || '';
        }
        // Where it lives on the host (the collector decides). The hover adds
        // what the path alone does not say: a unit's file, a container's mounts.
        var pathTip = r.kind === 'systemd' && r.unit_file ? 'unit file: ' + r.unit_file
                    : r.kind === 'container' && r.mounts && r.mounts.length ? 'mounts: ' + r.mounts.join(', ')
                    : (r.path || '');
        tr.innerHTML =
          '<td class="col-health"><span class="health-dot ' + escapeAttr(r.health || 'warn') + '" title="' + escapeAttr(r.health || '') + '"></span></td>' +
          '<td class="col-kind">' + escapeHtml(r.kind || '-') + '</td>' +
          '<td class="col-name">' + escapeHtml(r.name || '-') + '</td>' +
          '<td class="col-path" title="' + escapeAttr(pathTip) + '">' + (r.path ? '<code>' + escapeHtml(r.path) + '</code>' : '-') + '</td>' +
          '<td class="col-version">' + escapeHtml(r.version || '-') + '</td>' +
          '<td class="col-status">' + escapeHtml(r.status || r.state || '-') + '</td>' +
          '<td class="col-detail">' + escapeHtml(detail || '-') + '</td>';
        body.appendChild(tr);
      });
    }
    setText($('#rowCount'), filtered.length + (filtered.length === rows.length ? '' : ' / ' + rows.length));
    setLive(d.generated_at);
  }

  function startServicesPoll() {
    var INTERVAL = 30 * 1000;
    var lastData = null;
    function refresh() {
      fetch('/api/system/services', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { lastData = d; renderServices(d); })
        .catch(function () { setOffline('not connected'); });
    }
    refresh();
    setInterval(refresh, INTERVAL);
    $('#filterKind')   && $('#filterKind').addEventListener('change',   function () { if (lastData) renderServices(lastData); });
    $('#filterHealth') && $('#filterHealth').addEventListener('change', function () { if (lastData) renderServices(lastData); });
  }

  // ---------------------------------------------------- crons
  // Schedules in words: 'every 5 min', 'daily 04:17', 'Sun 05:00', 'at boot'.
  // The raw expression stays in the cell's tooltip.
  var CRON_DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function humanizeCron(expr) {
    var f = expr.trim().split(/\s+/);
    if (f.length !== 5) return expr;
    var mi = f[0], h = f[1], dom = f[2], mon = f[3], dow = f[4];
    var num = function (x) { return /^\d+$/.test(x); };
    if (dom !== '*' || mon !== '*') return expr;
    if (mi === '*' && h === '*' && dow === '*') return 'every minute';
    var step = mi.match(/^\*\/(\d+)$/);
    if (step && h === '*' && dow === '*') return 'every ' + step[1] + ' min';
    if (num(mi) && h === '*' && dow === '*') return 'hourly at :' + pad2(+mi);
    if (num(mi) && num(h)) {
      var at = pad2(+h) + ':' + pad2(+mi);
      if (dow === '*') return 'daily ' + at;
      if (num(dow)) return CRON_DOW[+dow] + ' ' + at;
    }
    return expr;
  }
  function humanizeSched(s) {
    if (!s) return '-';
    s = String(s).trim();
    if (s === '@reboot') return 'at boot';
    if (/^@(hourly|daily|weekly|monthly|yearly|annually)$/.test(s)) return s.slice(1);
    // systemd wraps its value: { OnUnitActiveUSec=2s ; next_elapse=... }
    var m = s.match(/On(?:UnitActive|UnitInactive|Boot|Startup)USec=([^ ;]+)/);
    if (m) return 'every ' + m[1];
    var c = s.match(/OnCalendar=([^;}]+)/);
    if (c) {
      var cal = c[1].trim();
      var d = cal.match(/^\*-\*-\* (\d\d):(\d\d):\d\d$/);
      if (d) return 'daily ' + d[1] + ':' + d[2];
      var w = cal.match(/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\S* \*-\*-\* (\d\d):(\d\d):\d\d$/);
      if (w) return w[1] + ' ' + w[2] + ':' + w[3];
      return cal;
    }
    return humanizeCron(s);
  }

  // Which of the three groups a job belongs to: the node's own, ours (/work,
  // /backup: what we run on this server beside the product), or the OS.
  function cronGroup(r) {
    var src = String(r.source || ''), name = String(r.name || ''), cmd = String(r.target || '');
    if (r.scope === 'smart-node' || /smart-node|\/sn-/.test(src) || /^(smart-node-|sn-)/.test(name) ||
        /\/(opt|usr\/local\/lib)\/smart-node\//.test(cmd) || /\/srv\/tools\//.test(cmd)) return 'node';
    if (/(^|\s)\/(work|backup)\//.test(cmd)) return 'ours';
    return 'system';
  }

  // A command line is shown as written, except for anything that looks like a
  // credential: key=value pairs named like secrets, and long opaque tokens. A
  // token is one unbroken run of letters AND digits: hyphenated unit names like
  // prometheus-node-exporter-mellanox-hca-temp are words, not keys.
  function maskSecrets(cmd) {
    return String(cmd || '')
      .replace(/((?:token|password|passwd|secret|api[_-]?key|bearer|authorization)\s*[=:]\s*)(\S+)/gi, '$1***')
      .replace(/\b(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{32,}\b/g, '***');
  }

  // A crontab line is named by the script it runs (network-scan.sh), not by the
  // file it sits in: sixteen rows of 'security-monitors' say nothing.
  function cronJobName(r) {
    // cron-runner.sh is the node's wrapper; the job's own name is its first argument.
    var w = String(r.target || '').match(/cron-runner\.sh\s+([\w.\-]+)/);
    if (w) return w[1];
    var m = String(r.target || '').match(/[\w.\-]+\.(?:sh|py|pl|rb|js)\b/);
    return m ? m[0] : String(r.source || '').split('/').pop();
  }
  function relWhen(iso) {
    if (!iso) return '-';
    var t = new Date(iso).getTime();
    if (isNaN(t)) return iso;
    var diff = (t - Date.now()) / 1000;
    var abs = Math.abs(diff);
    var sign = diff >= 0 ? 'in ' : '';
    var ago = diff < 0 ? ' ago' : '';
    if (abs < 60) return sign + Math.round(abs) + 's' + ago;
    if (abs < 3600) return sign + Math.round(abs / 60) + 'm' + ago;
    if (abs < 86400) return sign + Math.round(abs / 3600) + 'h' + ago;
    return sign + Math.round(abs / 86400) + 'd' + ago;
  }
  function renderCrons(d) {
    var timers = d.timers || []; var crontabs = d.crontabs || [];
    var s = d.summary || {};
    setText($('[data-field="timers"]'), s.timers_total != null ? s.timers_total : '-');
    setText($('[data-field="crontabs"]'), s.crontab_entries != null ? s.crontab_entries : '-');

    var all = ([]).concat(timers, crontabs);
    all.forEach(function (r) { r.group = cronGroup(r); });
    var nNode = all.filter(function (r) { return r.group === 'node'; }).length;
    var nOurs = all.filter(function (r) { return r.group === 'ours'; }).length;
    setText($('[data-field="snscope"]'), nNode);
    setText($('[data-field="snours"]'), nOurs + ' ours · ' + (all.length - nNode - nOurs) + ' system');

    setText($('[data-field="nextrun"]'), s.next_run ? fmtTs(s.next_run) : '-');
    setText($('[data-field="nextin"]'), s.next_run ? relWhen(s.next_run) : '-');

    var fs = ($('#filterScope') || {}).value;
    var fk = ($('#filterKind') || {}).value || '';
    var rows = all;
    var filtered = rows.filter(function (r) {
      if (fs === 'mine' && r.group === 'system') return false;
      if (fs && fs !== 'mine' && r.group !== fs) return false;
      if (fk && r.kind !== fk) return false;
      return true;
    });
    // sort: node first, then ours, then system; inside a group by next run
    var gOrder = { node: 0, ours: 1, system: 2 };
    filtered.sort(function (a, b) {
      if (gOrder[a.group] !== gOrder[b.group]) return gOrder[a.group] - gOrder[b.group];
      var an = a.next_iso ? new Date(a.next_iso).getTime() : Infinity;
      var bn = b.next_iso ? new Date(b.next_iso).getTime() : Infinity;
      if (an !== bn) return an - bn;
      return (a.name || '').localeCompare(b.name || '');
    });

    var body = $('#cronsBody'); if (!body) return;
    if (filtered.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="7">No jobs match the current filter.</td></tr>';
    } else {
      body.innerHTML = '';
      filtered.forEach(function (r) {
        var tr = document.createElement('tr');
        var scopeTag = '<span class="scope-tag cron-' + r.group + '">' + escapeHtml(r.group) + '</span>';
        var desc   = (r.description || '').trim();
        var target = maskSecrets(r.target || '-');
        var file = r.kind === 'crontab' && r.source ? ' · ' + String(r.source) : '';
        var whatCell =
          '<div class="what-desc">' + (desc ? escapeHtml(desc) : '<span class="muted">-</span>') + '</div>' +
          '<div class="what-target" title="' + escapeAttr(target) + '">' + escapeHtml(target) + '</div>';
        tr.innerHTML =
          '<td class="col-kind">' + escapeHtml(r.kind || '-') + '</td>' +
          '<td class="col-scope">' + scopeTag + '</td>' +
          '<td class="col-name" title="' + escapeAttr(r.kind === 'crontab' ? String(r.source || '') + ' · runs as ' + (r.name || '?') : (r.name || '')) + '">' +
            escapeHtml(r.kind === 'crontab' ? cronJobName(r) : (r.name || '-').replace(/\.timer$/, '')) + '</td>' +
          '<td class="col-schedule" title="' + escapeAttr(r.schedule || '') + '">' + escapeHtml(humanizeSched(r.schedule)) + '</td>' +
          '<td class="col-what">' + whatCell + '</td>' +
          '<td class="col-last">' + escapeHtml(relWhen(r.last_iso)) + '</td>' +
          '<td class="col-next">' + escapeHtml(relWhen(r.next_iso)) + '</td>';
        body.appendChild(tr);
      });
    }
    setText($('#rowCount'), filtered.length + (filtered.length === rows.length ? '' : ' / ' + rows.length));
    setLive(d.generated_at);
  }
  // ---- crons CRUD (writes the registry; /etc/cron.d projection is render-crontab.py) ----
  var cronsReload = null;

  function openCronModal(existing) {
    var scrim = $('#cronModalScrim'); var form = $('#cronForm');
    if (!scrim || !form) return;
    form.reset();
    $('#cronFormError').textContent = '';
    if (existing) {
      $('#cronModalTitle').textContent = 'Edit cron · ' + (existing.slug || existing.name || ('#' + existing.id));
      form.elements['cron_id'].value = existing.id;
      $('#cronSchedule').value = existing.schedule || '';
      $('#cronCommand').value = existing.command || existing.target || '';
      $('#cronSlug').value = existing.slug || '';
      $('#cronGroup').value = existing.group || 'smart-node';
      $('#cronUser').value = existing.crontab_user || 'root';
      $('#cronEnabled').value = (existing.enabled === false) ? 'false' : 'true';
      $('#cronDesc').value = existing.description || '';
      // group + crontab_user are part of the upsert key; PUT cannot change them.
      $('#cronGroup').readOnly = true; $('#cronUser').readOnly = true;
    } else {
      $('#cronModalTitle').textContent = 'New cron';
      form.elements['cron_id'].value = '';
      $('#cronGroup').readOnly = false; $('#cronUser').readOnly = false;
    }
    scrim.hidden = false;
    setTimeout(function () { $('#cronSchedule').focus(); }, 30);
  }

  function closeCronModal() { var s = $('#cronModalScrim'); if (s) s.hidden = true; }

  function submitCronForm(e) {
    e.preventDefault();
    var form = $('#cronForm');
    var err = $('#cronFormError'); err.textContent = '';
    var btn = $('#cronSubmit'); btn.disabled = true;
    var fd = new FormData(form);
    var id = form.elements['cron_id'].value;
    var isEdit = !!id;
    var url, method, body;
    if (isEdit) {
      // PUT /api/crons/{id} -> CronUpdate (schedule, command, slug, enabled, description)
      url = '/api/crons/' + encodeURIComponent(id); method = 'PUT';
      body = {
        schedule: String(fd.get('schedule') || '').trim(),
        command: String(fd.get('command') || '').trim(),
        slug: String(fd.get('slug') || '').trim() || null,
        enabled: String(fd.get('enabled')) === 'true',
        description: String(fd.get('description') || '')
      };
    } else {
      url = '/api/crons'; method = 'POST';
      body = {
        schedule: String(fd.get('schedule') || '').trim(),
        command: String(fd.get('command') || '').trim(),
        slug: String(fd.get('slug') || '').trim() || null,
        group: String(fd.get('group') || 'other').trim(),
        crontab_user: String(fd.get('crontab_user') || 'root').trim(),
        enabled: String(fd.get('enabled')) === 'true',
        description: String(fd.get('description') || '')
      };
    }
    fetch(url, {
      method: method, cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(
          function (j) { throw new Error(snErr(r.status, j)); },
          function () { throw new Error(snErr(r.status)); });
      }
      closeCronModal();
      if (cronsReload) cronsReload();
    }).catch(function (e2) {
      err.textContent = (e2 && e2.message) ? String(e2.message) : 'Save failed';
    }).then(function () { btn.disabled = false; });
  }

  function deleteCron(id, name) {
    if (!id) return;
    snAsk('Delete cron "' + (name || ('#' + id)) + '"?', 'It stops running. This cannot be undone.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch('/api/crons/' + encodeURIComponent(id), { method: 'DELETE', headers: { 'Accept': 'application/json' } })
        .then(function (r) { if (!r.ok && r.status !== 204) return snFail(r); if (cronsReload) cronsReload(); })
        .catch(function (e) { snTell('Delete failed', (e.message || e)); });
    });
  }

  // Agents are woken by ONE service, smart-node-runner, when they have work;
  // this card answers "why did an agent (not) wake" where a cron list cannot.
  function loadRunnerCard() {
    var card = $('#runnerCard'); if (!card) return;
    var since = new Date(Date.now() - 24 * 3600 * 1000).toISOString();
    var hourAgo = Date.now() - 3600 * 1000;
    var count = function (type) {
      return fetch('/api/events?event_type=' + encodeURIComponent(type) + '&since=' + encodeURIComponent(since) + '&limit=1000',
                   { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : { events: [] }; })
        .then(function (d) { return d.events || []; })
        .catch(function () { return null; });
    };
    Promise.all([
      fetch('/api/system/services', { cache: 'no-store', credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; }),
      count('agent.woken'), count('agent.wake_capped'), count('agent.wake_failed')
    ]).then(function (res) {
      var unit = (res[0].systemd || []).filter(function (u) { return u.name === 'smart-node-runner.service'; })[0];
      var woken = res[1], capped = res[2], failed = res[3];
      var state = unit ? unit.status : 'unknown';
      var on = unit && unit.state === 'active';
      setText($('[data-field="runner-state"]'), on ? 'running' : (unit ? unit.state : 'unknown'));
      setText($('[data-field="runner-state-foot"]'), state);
      var dot = $('#runnerDot'); if (dot) { dot.classList.remove('ok', 'warn', 'fail'); dot.classList.add(on ? 'ok' : 'fail'); }
      var lastHour = woken ? woken.filter(function (e) { return Date.parse(e.ts) > hourAgo; }).length : null;
      setText($('[data-field="runner-woken"]'), woken ? woken.length : '-');
      setText($('[data-field="runner-woken-foot"]'), lastHour != null ? lastHour + ' in the last hour' : '-');
      setText($('[data-field="runner-capped"]'), capped ? capped.length : '-');
      setText($('[data-field="runner-failed"]'), failed ? failed.length : '-');
    });
  }

  function startCronsPoll() {
    var INTERVAL = 30 * 1000;
    var lastData = null;
    function refresh() {
      // Timers and every crontab on the server, as core-api reports them (it
      // reads the host collector; the panel never does). Agents are not
      // on cron any more: the runner wakes them, see the card above.
      fetch('/api/system/crons', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { lastData = d; renderCrons(d); })
        .catch(function () { setOffline('not connected'); });
      loadRunnerCard();
    }
    cronsReload = refresh;
    refresh();
    setInterval(refresh, INTERVAL);
    $('#filterScope') && $('#filterScope').addEventListener('change', function () { if (lastData) renderCrons(lastData); });
    $('#filterKind')  && $('#filterKind') .addEventListener('change', function () { if (lastData) renderCrons(lastData); });

    // create / edit / delete wiring
    var applyBtn = $('#cronApplyBtn');
    if (applyBtn) applyBtn.addEventListener('click', function () {
      var orig = applyBtn.textContent; applyBtn.disabled = true; applyBtn.textContent = 'Applying…';
      fetch('/api/crons/render', { method: 'POST', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
        .then(function (res) {
          if (!res.ok) throw new Error(snErr(res.status, res.j));
          applyBtn.textContent = 'Applied · ' + res.j.jobs + ' jobs';
          setTimeout(function () { applyBtn.textContent = orig; }, 2500);
        })
        .catch(function (e) { snTell('Apply failed', (e.message || e)); applyBtn.textContent = orig; })
        .then(function () { applyBtn.disabled = false; });
    });

    var newBtn = $('#cronNewBtn'); if (newBtn) newBtn.addEventListener('click', function () { openCronModal(null); });
    $('#cronModalClose')  && $('#cronModalClose').addEventListener('click', closeCronModal);
    $('#cronModalCancel') && $('#cronModalCancel').addEventListener('click', closeCronModal);
    $('#cronModalScrim')  && $('#cronModalScrim').addEventListener('click', function (e) { if (e.target && e.target.id === 'cronModalScrim') closeCronModal(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { var s = $('#cronModalScrim'); if (s && !s.hidden) closeCronModal(); } });
    var form = $('#cronForm'); if (form) form.addEventListener('submit', submitCronForm);
    var tbody = $('#cronsBody');
    if (tbody) tbody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act');
      var id = btn.getAttribute('data-id');
      if (act === 'delete') return deleteCron(id, btn.getAttribute('data-name'));
      if (act === 'edit') {
        fetch('/api/crons/' + encodeURIComponent(id), { cache: 'no-store', headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (rec) { if (rec) openCronModal(rec); });
      }
    });
  }

  // ---------------------------------------------------- ai gateway
  // Budget limits are edited in place here: this is where the owner looks when
  // he wants to know what is being spent, so this is where he changes it.
  // /api/ai/status carries neither the budget id nor the agent's name, so both
  // come from /api/budgets and /api/agents, loaded once and again after a save.
  var budgetMeta = { ids: {}, slugs: {}, loaded: false };
  var budgetEditing = false;   // a 30 s refresh must not wipe an open edit
  var budgetLast = [];

  function budgetKey(b) { return b.scope_type + ':' + b.scope_id + ':' + b.period; }

  // The context-handoff threshold is the agent's other budget, counted in
  // tokens instead of dollars, so it sits at the bottom of the same card. The
  // gateway already prefers an agent row over the node default
  // (context_handoff.py). The slider walks uneven steps because the useful
  // range is 50k-300k: on a linear 0-1M track that range would be a sliver.
  var CTX_STEPS = [10000, 50000, 100000, 150000, 200000, 300000, 500000, 1000000];
  var ctxState = { loaded: false, def: 120000, enabled: true, own: {}, reload: null, error: '' };

  function ctxFmt(n) { return n >= 1000000 ? (n / 1000000) + 'M' : Math.round(n / 1000) + 'k'; }

  function nearestStep(v) {
    var best = 0;
    CTX_STEPS.forEach(function (s, i) {
      if (Math.abs(s - v) < Math.abs(CTX_STEPS[best] - v)) best = i;
    });
    return best;
  }

  function ctxRequest(url, opts, note) {
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(snErr(r.status, j)); return j;
      });
    }).then(function () {
      budgetEditing = false;
      if (ctxState.reload) ctxState.reload();
    }).catch(function (e) {
      budgetEditing = false;
      note.textContent = 'failed: ' + e.message;
    });
  }

  function ctxPut(url, body, note) {
    note.textContent = 'saving...';
    ctxRequest(url, {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    }, note);
  }

  function saveCtx(slug, value, note) {
    if (!value || value < 10000 || value > 1000000) { note.textContent = '10,000 to 1,000,000'; return; }
    if (slug) {
      ctxPut('/api/session-context-limits/agents/' + encodeURIComponent(slug),
             { threshold_tokens: value, enabled: ctxState.enabled, reason: 'set per agent in node panel' }, note);
    } else {
      ctxPut('/api/session-context-limits/global',
             { threshold_tokens: value, enabled: ctxState.enabled, reason: 'node default set in node panel' }, note);
    }
  }

  function resetAgentCtx(slug, note) {
    note.textContent = 'resetting...';
    ctxRequest('/api/session-context-limits/agents/' + encodeURIComponent(slug) +
               '?reason=' + encodeURIComponent('reset to node default in node panel'),
               { method: 'DELETE', credentials: 'same-origin', headers: { 'Accept': 'application/json' } }, note);
  }

  // slug = an agent's own threshold; slug null = the node default, which also
  // carries the on/off switch for the whole handoff mechanism.
  function ctxLine(slug) {
    var isNode = !slug;
    var own = isNode ? null : ctxState.own[slug];
    var val = own ? Number(own.threshold_tokens) : ctxState.def;
    var el = document.createElement('div');
    el.className = 'budget-ctx' + (isNode || own ? '' : ' is-default');
    var state = isNode
      ? '<button type="button" class="switch' + (ctxState.enabled ? ' on' : '') + '" role="switch" aria-checked="' +
        (ctxState.enabled ? 'true' : 'false') + '" aria-label="context handoff enabled"><span class="knob"></span></button>'
      : '<span class="budget-ctx-state">' + (own ? 'own' : 'default') + '</span>' +
        '<button type="button" class="budget-ctx-reset"' + (own ? '' : ' hidden') + '>reset</button>';
    el.innerHTML =
      '<div class="budget-ctx-head">' +
        '<span class="budget-ctx-label" title="When the latest provider response reaches this context size, the agent writes a handoff to Mesh and the next message starts a clean session.">' +
          (isNode ? 'context, default' : 'context') + '</span>' +
        '<span class="budget-ctx-val">' + ctxFmt(val) + '</span>' +
        state +
      '</div>' +
      '<div class="budget-ctx-edit">' +
        '<input type="range" class="budget-ctx-range" min="0" max="' + (CTX_STEPS.length - 1) +
          '" step="1" value="' + nearestStep(val) + '" aria-label="' + escapeHtml(slug || 'node default') + ' context threshold">' +
        '<input type="number" class="budget-ctx-num" min="10000" max="1000000" step="1000" value="' + val + '" aria-label="' + escapeHtml(slug || 'node default') + ' context threshold, tokens">' +
      '</div>' +
      '<div class="budget-ctx-note"></div>';
    var range = el.querySelector('.budget-ctx-range');
    var num = el.querySelector('.budget-ctx-num');
    var shown = el.querySelector('.budget-ctx-val');
    var note = el.querySelector('.budget-ctx-note');
    // While the owner drags or types, the 30 s refresh must leave the cards alone.
    range.addEventListener('input', function () {
      budgetEditing = true;
      num.value = CTX_STEPS[Number(range.value)];
      shown.textContent = ctxFmt(Number(num.value));
    });
    range.addEventListener('change', function () { saveCtx(slug, Number(num.value), note); });
    num.addEventListener('focus', function () { budgetEditing = true; });
    num.addEventListener('blur', function () { if (!note.textContent) budgetEditing = false; });
    num.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { budgetEditing = false; drawBudgets(budgetLast); return; }
      if (ev.key !== 'Enter') return;
      var v = parseInt(num.value, 10);
      range.value = nearestStep(v);
      saveCtx(slug, v, note);
    });
    if (isNode) {
      el.querySelector('.switch').addEventListener('click', function () {
        ctxState.enabled = !ctxState.enabled;
        ctxPut('/api/session-context-limits/global',
               { threshold_tokens: ctxState.def, enabled: ctxState.enabled,
                 reason: 'context handoff ' + (ctxState.enabled ? 'enabled' : 'disabled') + ' in node panel' }, note);
      });
    } else {
      el.querySelector('.budget-ctx-reset').addEventListener('click', function () { resetAgentCtx(slug, note); });
    }
    return el;
  }

  function loadBudgetMeta() {
    var opts = { cache: 'no-store', credentials: 'same-origin', headers: { 'Accept': 'application/json' } };
    return Promise.all([
      fetch('/api/budgets', opts).then(function (r) { return r.ok ? r.json() : {}; }),
      fetch('/api/agents', opts).then(function (r) { return r.ok ? r.json() : []; })
    ]).then(function (res) {
      var rows = (res[0] && res[0].budgets) || [];
      var agents = Array.isArray(res[1]) ? res[1] : ((res[1] && res[1].agents) || []);
      budgetMeta.ids = {};
      rows.forEach(function (r) { budgetMeta.ids[budgetKey(r)] = r.id; });
      budgetMeta.slugs = {};
      agents.forEach(function (a) { budgetMeta.slugs[String(a.id)] = a.slug; });
      budgetMeta.loaded = true;
    }).catch(function () { /* cards stay read-only; the edit says why */ });
  }

  function renderBudgets(budgets) {
    budgetLast = budgets || [];
    if (budgetEditing) return;
    if (!budgetMeta.loaded) {
      loadBudgetMeta().then(function () { if (!budgetEditing) drawBudgets(budgetLast); });
    }
    drawBudgets(budgetLast);
  }

  function saveBudgetLimit(b, value, foot) {
    var id = budgetMeta.ids[budgetKey(b)];
    if (!id) { foot.textContent = 'cannot save: budget id not known, reload the page'; return; }
    foot.textContent = 'saving...';
    fetch('/api/budgets/' + encodeURIComponent(id), {
      method: 'PATCH', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ limit_usd: value })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(snErr(r.status, j)); return j;
      });
    }).then(function (j) {
      b.limit_usd = Number(j.limit_usd != null ? j.limit_usd : value);
      b.pct = b.limit_usd > 0 ? Math.round(b.current_spend_usd / b.limit_usd * 10000) / 100 : 0;
      b.over = b.limit_usd > 0 && b.current_spend_usd > b.limit_usd;
      budgetEditing = false;
      drawBudgets(budgetLast);
    }).catch(function (e) {
      foot.textContent = 'save failed: ' + e.message;
    });
  }

  function editBudgetLimit(b, limitEl, foot) {
    if (budgetEditing) return;
    budgetEditing = true;
    var input = document.createElement('input');
    input.type = 'number'; input.min = '0'; input.step = '1';
    input.className = 'budget-limit-input';
    input.value = String(Math.round(b.limit_usd * 100) / 100);
    limitEl.replaceWith(input);
    foot.textContent = 'Enter to save, Esc to cancel';
    input.focus(); input.select();
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { budgetEditing = false; drawBudgets(budgetLast); return; }
      if (ev.key !== 'Enter') return;
      var v = Number(input.value);
      if (!isFinite(v) || v < 0) { foot.textContent = 'the limit is a dollar amount, 0 or more'; return; }
      saveBudgetLimit(b, v, foot);
    });
  }

  // Budgets are read in dollars and cents; a whole limit reads as $100, not $100.00.
  function costFmt(v) {
    var n = Math.round((v || 0) * 100) / 100;
    return '$' + (n % 1 === 0 ? String(n) : n.toFixed(2));
  }

  // One money budget (one period of one scope) inside a card.
  function moneyBlock(b) {
    var el = document.createElement('div');
    var pct = Math.min(100, Math.max(0, Number(b.pct) || 0));
    el.className = 'budget-money' + (b.over ? ' is-over' : (b.pct >= 80 ? ' is-warn' : ''));
    var hardBadge = b.hard
      ? '<span class="badge badge-err" title="hard limit -- gateway blocks over-limit requests with 402">hard</span>'
      : '<span class="badge badge-mute" title="soft limit -- tracking only, never blocks">soft</span>';
    el.innerHTML =
      '<div class="budget-money-head">' +
        '<span class="budget-period">' + escapeHtml(b.period || '') + '</span>' + hardBadge +
      '</div>' +
      '<div class="budget-numbers">' +
        '<span class="budget-spent">' + costFmt(b.current_spend_usd) + '</span>' +
        '<span class="budget-sep">of</span>' +
        '<span class="budget-limit">' + costFmt(b.limit_usd) + '</span>' +
        '<span class="budget-pct">' + Math.round(pct) + '%</span>' +
      '</div>' +
      '<div class="budget-bar"><div class="budget-bar-fill" style="width:' + pct + '%"></div></div>' +
      '<div class="budget-foot">resets ' + escapeHtml(relWhen(b.period_reset_at) || '-') + '</div>';
    return el;
  }

  function budgetCard(label, blocks, ctxSlug, isNode) {
    var card = document.createElement('article');
    var worst = blocks.some(function (b) { return b.over; }) ? ' is-over'
              : (blocks.some(function (b) { return b.pct >= 80; }) ? ' is-warn' : '');
    card.className = 'budget-card' + worst + (isNode ? ' is-node' : '');
    card.innerHTML = '<div class="budget-card-head"><span class="budget-scope">' + escapeHtml(label) + '</span></div>';
    blocks.forEach(function (b) { card.appendChild(moneyBlock(b)); });
    if (ctxState.loaded && ctxSlug !== false && (isNode || ctxSlug)) card.appendChild(ctxLine(isNode ? null : ctxSlug));
    if (isNode && ctxState.error) {
      var err = document.createElement('div');
      err.className = 'budget-ctx-note';
      err.textContent = ctxState.error;
      card.appendChild(err);
    }
    return card;
  }

  function drawBudgets(budgets) {
    var grid = $('#budgetsGrid');
    if (!grid) return;
    budgets = budgets || [];
    var node = budgets.filter(function (b) { return b.scope_type === 'tenant'; });
    var rest = budgets.filter(function (b) { return b.scope_type !== 'tenant'; });
    if (!budgets.length && !ctxState.loaded) {
      grid.innerHTML = '<div class="budgets-empty">No budgets configured.</div>';
      return;
    }
    grid.innerHTML = '';
    // The node card first: its money and the context default every agent inherits.
    grid.appendChild(budgetCard('whole node', node, false, true));
    rest.forEach(function (b) {
      var slug = b.scope_type === 'agent' ? budgetMeta.slugs[String(b.scope_id)] : null;
      var label = b.scope_type === 'agent' ? (slug || 'agent #' + b.scope_id) : 'meta.' + b.scope_id;
      grid.appendChild(budgetCard(label, [b], false, false));
    });
    var ctxGrid = $('#contextGrid');
    if (ctxGrid && ctxState.loaded) {
      ctxGrid.textContent = '';
      ctxGrid.appendChild(budgetCard('node default', [], null, true));
      Object.keys(budgetMeta.slugs).forEach(function (id) {
        var agent = budgetMeta.slugs[id];
        ctxGrid.appendChild(budgetCard(agent, [], agent, false));
      });
    }
  }

  function renderSessionDetail(r) {
    // Inline session detail row: metadata bar + message list.
    // Messages are pre-embedded in ai.json (last 30 per session, content
    // truncated to 800 chars + '…'). No additional fetch.
    var u = r.usage || {};
    var costStr = (u.cost_usd != null)
      ? '$' + (Math.round(u.cost_usd * 10000) / 10000).toFixed(4)
      : '-';
    var tokStr = (u.input_tokens != null || u.output_tokens != null)
      ? ((u.input_tokens || 0) + ' in / ' + (u.output_tokens || 0) + ' out')
      : '-';
    var estBadge = u.estimated ? ' <span class="badge badge-warn" title="usage estimated (opaque backend)">est</span>' : '';
    var opaqueBadge = r.is_opaque ? ' <span class="badge badge-mute" title="gateway cannot observe internal tool calls">opaque</span>' : '';
    var statusBadge = '<span class="badge badge-' + (r.status === 'active' ? 'ok' : (r.status === 'failed' ? 'err' : 'mute')) + '">' + escapeHtml(r.status || '-') + '</span>';
    var resets = r.context_resets || [];
    var resetMeta = resets.length
      ? '<span class="meta-kv"><span class="meta-k">context resets</span><span class="meta-v">' + resets.length + '</span></span>'
      : '';

    var head =
      '<div class="session-detail-head">' +
        '<div class="session-detail-meta">' +
          '<span class="meta-kv"><span class="meta-k">id</span><code class="meta-v session-id-full">' + escapeHtml(r.session_id || '') + '</code></span>' +
          '<span class="meta-kv"><span class="meta-k">status</span>' + statusBadge + estBadge + opaqueBadge + '</span>' +
          '<span class="meta-kv"><span class="meta-k">tokens</span><span class="meta-v">' + escapeHtml(tokStr) + '</span></span>' +
          '<span class="meta-kv"><span class="meta-k">cost</span><span class="meta-v">' + escapeHtml(costStr) + '</span></span>' +
          '<button type="button" class="btn-ghost" data-calls-sid="' + escapeAttr(r.session_id || '') + '">Model calls</button>' +
          '<span class="meta-kv"><span class="meta-k">calls</span><span class="meta-v">' + escapeHtml(String(u.calls || 0)) + '</span></span>' +
          resetMeta +
        '</div>' +
      '</div>';

    var resetHtml = resets.map(function (x) {
      var before = Number(x.before_context_tokens || 0).toLocaleString();
      var after = Number(x.after_context_tokens || 0).toLocaleString();
      return '<div class="msg msg-system"><div class="msg-head"><span class="msg-role">context reset</span><span class="msg-ts">' + escapeHtml(fmtTs(x.ts)) + '</span></div>' +
        '<pre class="msg-body">' + escapeHtml(before + ' to ' + after + ' tokens · handoff ' + (x.handoff_doc_id || '-')) + '</pre></div>';
    }).join('');

    var msgs = r.messages || [];
    var msgsHtml;
    if (!msgs.length && !resetHtml) {
      msgsHtml = '<div class="session-detail-empty">No messages.</div>';
    } else {
      msgsHtml = '<div class="session-msgs">' + msgs.map(function (m) {
        var mu = m.usage || {};
        var muTxt = '';
        if (m.role === 'assistant' && (mu.input_tokens || mu.output_tokens || mu.cost_usd)) {
          var c = (mu.cost_usd != null) ? ' · $' + (Math.round(mu.cost_usd * 10000) / 10000).toFixed(4) : '';
          muTxt = '<span class="msg-usage">' + (mu.input_tokens || 0) + ' in / ' + (mu.output_tokens || 0) + ' out' + c + '</span>';
        }
        return '<div class="msg msg-' + escapeAttr(m.role || 'unknown') + '">' +
                 '<div class="msg-head">' +
                   '<span class="msg-seq">#' + escapeHtml(String(m.seq)) + '</span>' +
                   '<span class="msg-role">' + escapeHtml(m.role || '-') + '</span>' +
                   '<span class="msg-ts">' + escapeHtml(fmtTs(m.created_at) || '') + '</span>' +
                   muTxt +
                 '</div>' +
                 '<pre class="msg-body">' + escapeHtml(m.content || '') + '</pre>' +
               '</div>';
      }).join('') + resetHtml + '</div>';
    }

    return head + msgsHtml;
  }

  // ---------------------------------------------------- ai gateway: session timeline
  // One bar per session, from its start to its last activity, packed into lanes
  // so bars never overlap. Colour is the model: a fixed slot per model (validated
  // for colour-blind separation on both panel surfaces), never cycled, so a model
  // keeps its colour whatever the window shows. Bars end at the LAST ACTIVITY,
  // not at "now": many sessions stay 'active' for hours without a turn, and
  // drawing them to the right edge would turn the chart into solid stripes.
  var TL_MODELS = [
    { key: 'opus',   label: 'opus',   light: '#2a78d6', dark: '#3987e5' },
    { key: 'sonnet', label: 'sonnet', light: '#eb6834', dark: '#d95926' },
    { key: 'gemini', label: 'gemini', light: '#1baf7a', dark: '#199e70' },
    { key: 'other',  label: 'other',  light: '#898781', dark: '#898781' }
  ];
  var tl = { chart: null, hours: 24, sessions: [], observer: null };
  try { var _h = Number(localStorage.getItem('sn-ai-timeline-hours')); if ([1, 6, 24].indexOf(_h) >= 0) tl.hours = _h; } catch (e) {}

  function tlModel(model) {
    var m = String(model || '').toLowerCase();
    for (var i = 0; i < TL_MODELS.length - 1; i++) if (m.indexOf(TL_MODELS[i].key) >= 0) return TL_MODELS[i];
    return TL_MODELS[TL_MODELS.length - 1];
  }

  function tlDark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }

  function tlToken(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function tlDuration(ms) {
    var m = Math.round(ms / 60000);
    if (m < 1) return '<1m';
    if (m < 60) return m + 'm';
    return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
  }

  // Greedy interval packing: each bar takes the first lane free at its start.
  function tlLanes(items) {
    var gap = 60 * 1000;   // one minute of air between bars in a lane
    var ends = [];
    items.sort(function (a, b) { return a.start - b.start; });
    items.forEach(function (it) {
      for (var i = 0; i < ends.length; i++) {
        if (ends[i] + gap <= it.start) { it.lane = i; ends[i] = it.end; return; }
      }
      it.lane = ends.length; ends.push(it.end);
    });
    return ends.length;
  }

  function tlOption(now) {
    var dark = tlDark();
    var ink = tlToken('--sn-ink', dark ? '#f2f2f0' : '#0a0a0a');
    var inkMute = tlToken('--sn-ink-mute', '#76767a');
    var rule = tlToken('--sn-rule', dark ? 'rgba(255,255,255,.075)' : '#e7e6e1');
    var surface = tlToken('--sn-surface', dark ? '#131419' : '#ffffff');
    var mono = tlToken('--sn-mono', 'JetBrains Mono, monospace');
    var from = now - tl.hours * 3600 * 1000;

    var items = [];
    tl.sessions.forEach(function (s) {
      var start = Date.parse(s.created_at);
      var end = Date.parse(s.last_activity || s.created_at);
      if (!isFinite(start)) return;
      if (!isFinite(end) || end < start) end = start;
      if (end < from) return;
      items.push({ s: s, start: start, end: end, model: tlModel(s.model) });
    });
    var lanes = Math.max(1, tlLanes(items));

    var series = TL_MODELS.map(function (m) {
      return {
        type: 'custom', name: m.label, color: dark ? m.dark : m.light,
        encode: { x: [1, 2], y: 0 },
        data: items.filter(function (it) { return it.model === m; }).map(function (it) {
          return { value: [it.lane, it.start, it.end], session: it.s };
        }),
        renderItem: function (params, api) {
          var a = api.coord([api.value(1), api.value(0)]);
          var b = api.coord([api.value(2), api.value(0)]);
          var h = Math.max(4, Math.min(10, api.size([0, 1])[1] - 3));
          var cs = params.coordSys;
          // A session of seconds is still a visible, hoverable mark.
          var rect = echarts.graphic.clipRectByRect(
            { x: a[0], y: a[1] - h / 2, width: Math.max(b[0] - a[0], 4), height: h },
            { x: cs.x, y: cs.y, width: cs.width, height: cs.height });
          return rect && {
            type: 'rect',
            shape: { x: rect.x, y: rect.y, width: rect.width, height: rect.height, r: Math.min(h / 2, 4) },
            style: { fill: api.visual('color') },
            emphasis: { style: { stroke: ink, lineWidth: 1 } }
          };
        }
      };
    });
    // "now" as a hairline on the right edge, on its own empty series so that
    // switching a model off in the legend never takes the line with it.
    series.push({ type: 'custom', name: '_now', data: [], renderItem: function () { return null; } });
    series[series.length - 1].markLine = {
      silent: true, symbol: 'none',
      lineStyle: { color: inkMute, width: 1, type: 'solid' },
      label: { show: false },   // the right edge is now; a rotated word only got clipped
      data: [{ xAxis: now }]
    };

    return {
      animationDuration: 300,
      grid: { left: 8, right: 16, top: 30, bottom: 26, containLabel: false },
      legend: {
        top: 0, left: 0, itemWidth: 10, itemHeight: 10, icon: 'roundRect', itemGap: 16,
        textStyle: { color: inkMute, fontFamily: mono, fontSize: 11 },
        data: TL_MODELS.filter(function (m) { return items.some(function (it) { return it.model === m; }); })
                       .map(function (m) { return m.label; })
      },
      tooltip: {
        trigger: 'item', confine: true,
        backgroundColor: surface, borderColor: rule, borderWidth: 1, padding: [8, 10],
        textStyle: { color: ink, fontFamily: mono, fontSize: 11.5 },
        extraCssText: 'box-shadow: 0 6px 24px rgba(0,0,0,.18); border-radius: 8px;',
        formatter: function (p) {
          var s = p.data.session, u = s.usage || {};
          var dur = Date.parse(s.last_activity || s.created_at) - Date.parse(s.created_at);
          var row = function (k, v) {
            return '<div style="display:flex;gap:14px;justify-content:space-between"><span style="color:' + inkMute + '">' +
                   k + '</span><span>' + escapeHtml(String(v)) + '</span></div>';
          };
          return '<div style="min-width:220px">' +
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;font-weight:600">' +
              '<span style="width:10px;height:10px;border-radius:3px;background:' + p.color + '"></span>' +
              escapeHtml(String(s.session_id || '').slice(0, 8)) + '</div>' +
            row('model', s.model || '-') + row('service', s.service || '-') + row('status', s.status || '-') +
            row('started', new Date(Date.parse(s.created_at)).toLocaleTimeString()) +
            row('duration', tlDuration(dur)) + row('messages', s.message_count != null ? s.message_count : '-') +
            row('cost', u.cost_usd != null ? '$' + (Math.round(u.cost_usd * 100) / 100).toFixed(2) : '-') +
            '</div>';
        }
      },
      xAxis: {
        type: 'time', min: from, max: now,
        axisLine: { lineStyle: { color: rule } }, axisTick: { show: false },
        axisLabel: { color: inkMute, fontFamily: mono, fontSize: 10.5, hideOverlap: true,
                     formatter: { day: '{MMM} {d}', hour: '{HH}:{mm}', minute: '{HH}:{mm}' } },
        splitLine: { show: true, lineStyle: { color: rule, width: 1 } }
      },
      yAxis: { type: 'value', min: -0.6, max: lanes - 0.4, inverse: true, show: false },
      dataZoom: [{ type: 'inside', xAxisIndex: 0, filterMode: 'weakFilter', minValueSpan: 5 * 60 * 1000 }],
      series: series,
      meta: { lanes: lanes, count: items.length }
    };
  }

  function drawTimeline() {
    var el = $('#aiTimeline');
    if (!el || typeof echarts === 'undefined') return;
    var now = Date.now();
    var opt = tlOption(now);
    var meta = opt.meta; delete opt.meta;   // ours, not an ECharts option
    // Height follows the number of lanes, so a quiet hour is a short strip and a
    // busy day gets room, within bounds.
    var h = Math.max(120, Math.min(360, 30 + 26 + meta.lanes * 12));
    if (el.style.height !== h + 'px') { el.style.height = h + 'px'; if (tl.chart) tl.chart.resize(); }
    if (!tl.chart) {
      tl.chart = echarts.init(el, null, { renderer: 'svg' });
      window.addEventListener('resize', function () { if (tl.chart) tl.chart.resize(); });
      // Clicking a bar opens that session's row in the table below.
      tl.chart.on('click', function (p) {
        var sid = p.data && p.data.session && p.data.session.session_id;
        var detail = sid && document.querySelector('.session-detail-row[data-sid="' + sid + '"]');
        if (detail && detail.previousElementSibling) {
          if (detail.style.display === 'none') detail.previousElementSibling.click();
          detail.previousElementSibling.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
      // The panel's theme toggle flips data-theme on <html>: repaint with the
      // other mode's steps, which were validated for that surface separately.
      tl.observer = new MutationObserver(function () { drawTimeline(); });
      tl.observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
    tl.chart.setOption(opt, { replaceMerge: ['series'] });
    var foot = $('#aiTimelineFoot');
    if (foot) foot.textContent = meta.count + ' sessions in the last ' + tl.hours + 'h · scroll to zoom · click a bar to open it below' +
      (tl.sessions.length >= 100 ? ' · the newest 100 sessions are shown' : '');
    $$('.timeline-ranges button').forEach(function (b) {
      b.classList.toggle('is-on', Number(b.getAttribute('data-range')) === tl.hours);
    });
  }

  function renderTimeline(sessions) {
    tl.sessions = sessions || [];
    drawTimeline();
  }

  function wireTimelineRanges() {
    $$('.timeline-ranges button').forEach(function (b) {
      b.addEventListener('click', function () {
        tl.hours = Number(b.getAttribute('data-range')) || 24;
        try { localStorage.setItem('sn-ai-timeline-hours', String(tl.hours)); } catch (e) {}
        if (tl.chart) tl.chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 });
        drawTimeline();
      });
    });
  }

  function renderAi(d) {
    var s = d.summary || {};

    var statusText = (s.status || 'unknown').toUpperCase();
    setText($('[data-field="overall-status"]'), statusText);
    setText($('[data-field="overall-foot"]'),
      s.status === 'healthy'  ? 'all systems operational' :
      s.status === 'degraded' ? 'degraded · some providers down' :
      s.status === 'unknown'  ? 'health not reported yet' :
                                 'gateway unreachable');
    var statusCard = $('[data-stat="status"]');
    if (statusCard) {
      statusCard.classList.remove('ok', 'warn', 'fail');
      // 'unknown' = core-api has no live gateway health probe yet -> stay neutral,
      // NOT a red failure (the gateway service itself may well be up).
      if (s.status === 'healthy')  statusCard.classList.add('ok');
      else if (s.status === 'degraded') statusCard.classList.add('warn');
      else if (s.status && s.status !== 'unknown') statusCard.classList.add('fail');
    }

    setText($('[data-field="active-sessions"]'), s.active_sessions != null ? s.active_sessions : '-');
    setText($('[data-field="providers-ok"]'),    s.providers_ok    != null ? s.providers_ok    : '-');
    setText($('[data-field="providers-foot"]'), 'of ' + (s.providers_total != null ? s.providers_total : '-') + ' configured');

    // Prompt cache: share of input tokens the provider served from its cache.
    var tok = function (n) {
      if (n == null) return '-';
      return n >= 1e9 ? (Math.round(n / 1e8) / 10) + 'B' : n >= 1e6 ? (Math.round(n / 1e5) / 10) + 'M'
           : n >= 1e3 ? Math.round(n / 1e3) + 'k' : String(n);
    };
    var hp = s.cache_hit_pct != null ? s.cache_hit_pct : 0;
    setText($('[data-field="cache-pct"]'), (s.cache_hit_pct != null ? s.cache_hit_pct + '%' : '-'));
    setText($('[data-field="cache-foot"]'),
      (s.cache_window || '24h') + ' · ' + tok(s.cache_hits) + ' of ' + tok(s.cache_lookups) +
      ' input tokens · ' + (s.cache_entries != null ? s.cache_entries : '-') + ' sessions');
    var cBar = $('[data-field="cache-bar"]'); if (cBar) cBar.style.width = hp + '%';

    var grid = $('#providerGrid');
    if (grid) {
      var provs = d.providers || [];
      if (provs.length === 0) {
        grid.innerHTML = '<div class="provider-empty">No providers configured.</div>';
      } else {
        grid.innerHTML = '';
        provs.forEach(function (p) {
          var card = document.createElement('article');
          card.className = 'provider-card' + ((p.runtime_status === 'ok') ? '' : ' unavailable');

          var dotCls = 'fail';
          if (p.runtime_status === 'ok' || p.runtime_status === 'configured') dotCls = 'ok';
          else if (p.runtime_status === 'not_configured') dotCls = 'warn';

          // No model list on the card: an agent may run any model of a provider,
          // and the list here was the price table's rows, stale ones included
          // (gemini-2.0-flash). The card is the provider and its key.

          var foot;
          if (p.runtime_status === 'ok') {
            foot = (p.active_sessions != null ? p.active_sessions + ' active · ' : '') + (p.type || '-');
          } else if (p.runtime_status === 'configured') {
            // Key/credential is set; the gateway has no live health probe yet, so be
            // honest -- this is NOT a failure.
            foot = 'configured · health pending';
          } else if (p.runtime_status === 'not_configured') {
            foot = 'not configured';
          } else {
            foot = (p.error || 'health check failed');
          }

          // Owner-gated config control: API-key providers take a key; CLI providers
          // (claude) are authorized via the Connect OAuth flow. Status 'configured'
          // means a key/credential is stored.
          var pidAttr = escapeAttr(p.id || '');
          var stateStyle = 'font-family:\'JetBrains Mono\',ui-monospace,monospace;font-size:10.5px;color:#2e7d32';
          var btnStyle = 'padding:5px 12px';   // compact in the card row; text keeps the one button size
          // 'ok' is the gateway saying the provider answers, which implies it is
          // set up: it must read as connected, not offer Connect / Set key again.
          var isSet = (p.runtime_status === 'configured' || p.runtime_status === 'ok');
          var cfgHtml;
          if (p.type === 'cli') {
            cfgHtml = isSet
              ? '<span style="' + stateStyle + '">connected</span><button type="button" class="btn-ghost" style="' + btnStyle + '" data-cfg="connect" data-pid="' + pidAttr + '">Reconnect</button>'
              : '<button type="button" class="btn-primary" style="' + btnStyle + '" data-cfg="connect" data-pid="' + pidAttr + '">Connect</button>';
          } else {
            cfgHtml = isSet
              ? '<span style="' + stateStyle + '">key set</span><button type="button" class="btn-ghost" style="' + btnStyle + '" data-cfg="clearkey" data-pid="' + pidAttr + '">Clear</button>'
              : '<button type="button" class="btn-primary" style="' + btnStyle + '" data-cfg="setkey" data-pid="' + pidAttr + '">Set key</button>';
          }

          // Head / body / foot: the body takes the slack, so the foot sits on the
          // bottom edge of every card in a row whatever the number of models.
          card.innerHTML =
            '<div class="provider-card-head">' +
              '<span class="health-dot ' + dotCls + '"></span>' +
              '<span class="provider-card-name">' + escapeHtml(p.name || p.id || '-') + '</span>' +
              '<span class="provider-card-type">' + escapeHtml(p.type || '') + '</span>' +
            '</div>' +
            '<div class="provider-card-body">' +
              '<div class="provider-card-id">' + escapeHtml(p.id || '') + '</div>' +
            '</div>' +
            '<div class="provider-card-foot">' +
              '<div class="provider-foot' + (isSet ? '' : ' err') + '">' + escapeHtml(foot) + '</div>' +
              '<div class="provider-cfg">' + cfgHtml + '</div>' +
            '</div>';
          grid.appendChild(card);
        });
      }
    }

    var body = $('#aiSessionsBody');
    if (body) {
      var rows = d.sessions || [];
      if (rows.length === 0) {
        body.innerHTML = '<tr class="empty-row"><td colspan="6">No active sessions.</td></tr>';
      } else {
        // Preserve which session was expanded across refreshes (poll every 30s).
        var openSid = body.getAttribute('data-open') || '';
        body.innerHTML = '';
        rows.forEach(function (r) {
          var tr = document.createElement('tr');
          tr.className = 'session-row';
          tr.setAttribute('data-sid', r.session_id || '');
          var sidShort = (r.session_id || '').slice(0, 8) + '…';
          tr.innerHTML =
            '<td class="col-sid" title="' + escapeAttr(r.session_id || '') + '">' + escapeHtml(sidShort) + '</td>' +
            '<td class="col-source">' + escapeHtml(r.service || '-') + '</td>' +
            '<td class="col-model">' + escapeHtml(r.model || '-') + '</td>' +
            '<td class="col-created">' + escapeHtml(fmtTs(r.created_at)) + '</td>' +
            '<td class="col-active">' + escapeHtml(relWhen(r.last_activity)) + '</td>' +
            '<td class="col-msgs">' + escapeHtml(String(r.message_count != null ? r.message_count : '-')) + '</td>';
          body.appendChild(tr);

          // Detail row -- collapsed by default; restored if this session was
          // expanded before the 30s poll-driven re-render.
          var detail = document.createElement('tr');
          detail.className = 'session-detail-row';
          detail.setAttribute('data-sid', r.session_id || '');
          var open = (openSid === r.session_id);
          detail.style.display = open ? 'table-row' : 'none';
          detail.innerHTML = '<td colspan="6" class="session-detail-cell">' + renderSessionDetail(r) + '</td>';
          body.appendChild(detail);

          tr.style.cursor = 'pointer';
          tr.addEventListener('click', function () {
            var nowOpen = (detail.style.display === 'none');
            // One-at-a-time: collapse any other open detail before opening this
            var others = body.querySelectorAll('.session-detail-row');
            for (var i = 0; i < others.length; i++) { others[i].style.display = 'none'; }
            detail.style.display = nowOpen ? 'table-row' : 'none';
            body.setAttribute('data-open', nowOpen ? (r.session_id || '') : '');
          });
        });
      }
    }

    renderBudgets(d.budgets || []);
    renderTimeline(d.sessions || []);
    var sNote = $('#aiSessionsNote');
    if (sNote) {
      var shown = (d.sessions || []).length, all = d.sessions_total;
      sNote.textContent = (all != null && all > shown)
        ? 'newest ' + shown + ' of ' + Number(all).toLocaleString() + ' sessions'
        : shown + ' sessions';
    }

    setLive(d.generated_at);
  }

  // ---------------------------------------------------- ai: model calls tab
  // Every call the door passed (core-api /api/ai/model-calls): the tokens each step
  // of a turn sends and gets back. Read when the tab opens and on each refresh while
  // it is shown; a session's own calls from its detail row.
  var aiCallsSid = '';
  function aiTab(name) {
    document.querySelectorAll('#aiTabs [data-tab]').forEach(function (b) {
      b.classList.toggle('is-on', b.getAttribute('data-tab') === name);
      b.setAttribute('aria-selected', b.getAttribute('data-tab') === name ? 'true' : 'false');
    });
    document.querySelectorAll('.ai-sessions-wrap [data-pane]').forEach(function (p) {
      p.hidden = p.getAttribute('data-pane') !== name;
    });
    if (name === 'calls') loadAiCalls();
  }
  function aiCallsShown() { var p = $('.ai-sessions-wrap [data-pane="calls"]'); return !!p && !p.hidden; }
  function fmtTok(n) { return n == null ? '-' : Number(n).toLocaleString(); }
  function fmtMs(ms) { return ms == null ? '-' : (ms < 1000 ? ms + ' ms' : (Math.round(ms / 100) / 10) + ' s'); }
  function loadAiCalls() {
    var body = $('#aiCallsBody'); if (!body) return;
    var f = $('#aiCallsFilter');
    if (f) {
      f.hidden = !aiCallsSid;
      if (aiCallsSid) {
        f.innerHTML = 'session <code>' + escapeHtml(aiCallsSid.slice(0, 8)) + '\u2026</code> <button type="button" title="All sessions" aria-label="All sessions">&times;</button>';
        f.querySelector('button').addEventListener('click', function () { aiCallsSid = ''; loadAiCalls(); });
      }
    }
    var url = '/api/ai/model-calls?limit=200' + (aiCallsSid ? '&session_id=' + encodeURIComponent(aiCallsSid) : '');
    fetch(url, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (d) {
        var rows = d.calls || [];
        if (!rows.length) { body.innerHTML = '<tr class="empty-row"><td colspan="9">No model calls.</td></tr>'; }
        else {
          body.innerHTML = rows.map(function (c) {
            var ended = c.stop_reason || c.outcome || c.status || '-';
            if (c.tools && c.tools.length) ended += ': ' + c.tools.join(', ');
            return '<tr>' +
              '<td title="' + escapeAttr(c.ts || '') + '">' + escapeHtml(relWhen(c.ts)) + '</td>' +
              '<td>' + escapeHtml(c.agent || '-') + '</td>' +
              '<td>' + escapeHtml(c.model || '-') + '</td>' +
              '<td class="num">' + fmtTok(c.input_tokens) + '</td>' +
              '<td class="num">' + fmtTok(c.cache_read) + '</td>' +
              '<td class="num">' + fmtTok(c.cache_write) + '</td>' +
              '<td class="num">' + fmtTok(c.output_tokens) + '</td>' +
              '<td class="num">' + fmtMs(c.duration_ms) + '</td>' +
              '<td class="ended" title="' + escapeAttr(ended) + '">' + escapeHtml(ended) + '</td>' +
              '</tr>';
          }).join('');
        }
        var sum = function (k) { return rows.reduce(function (a, c) { return a + (c[k] || 0); }, 0); };
        var note = $('#aiCallsNote');
        if (note) note.textContent = rows.length
          ? 'newest ' + rows.length + ' calls \u00b7 sent ' + fmtTok(sum('input_tokens') + sum('cache_read') + sum('cache_write'))
            + ' tokens (' + fmtTok(sum('cache_read')) + ' from the cache) \u00b7 returned ' + fmtTok(sum('output_tokens'))
          : '';
      })
      .catch(function () { body.innerHTML = '<tr class="empty-row"><td colspan="9">Model calls are not available.</td></tr>'; });
  }
  function wireAiTabs() {
    document.querySelectorAll('#aiTabs [data-tab]').forEach(function (b) {
      b.addEventListener('click', function () { aiTab(b.getAttribute('data-tab')); });
    });
    // "Model calls" in a session's detail row: that session's calls
    var sb = $('#aiSessionsBody');
    if (sb) sb.addEventListener('click', function (e) {
      var b = e.target.closest('[data-calls-sid]'); if (!b) return;
      e.stopPropagation();
      aiCallsSid = b.getAttribute('data-calls-sid') || '';
      aiTab('calls');
    }, true);
  }

  function startAiPoll() {
    var INTERVAL = 30;
    var countdown = INTERVAL;
    function refresh() {
      fetch('/api/ai/status', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (d) { renderAi(d); countdown = INTERVAL; if (aiCallsShown()) loadAiCalls(); })
        .catch(function () { setOffline('not connected'); });
    }
    function tick() {
      countdown -= 1;
      if (countdown <= 0) { refresh(); return; }
      setText($('#nextRefresh'), String(countdown));
    }
    refresh();
    setInterval(tick, 1000);
  }

  // ---------------------------------------------------- ai gateway: provider config
  // Owner-gated. Calls core-api directly: API keys -> /api/gateway/secrets(+/delete);
  // CLI -> /api/gateway/cli/<id>/login/{start,complete}. The CLI OAuth token is stored
  // server-side by core-api and never returned to the browser.
  var gwMode = null, gwPid = null;

  function aiReloadOnce() {
    fetch('/api/ai/status', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) renderAi(d); })
      .catch(function () {});
  }

  function gwShow(id, on) { var el = $('#' + id); if (el) { if (on) el.removeAttribute('hidden'); else el.setAttribute('hidden', ''); } }
  function gwResult(html) { var el = $('#gwModalResult'); if (el) el.innerHTML = html || ''; }
  function gwOpen() { gwShow('gwModalScrim', true); }
  function gwClose() { gwShow('gwModalScrim', false); aiReloadOnce(); }

  function openKeyModal(pid) {
    gwMode = 'setkey'; gwPid = pid;
    setText($('#gwModalTitle'), 'Set API key · ' + pid);
    setText($('#gwModalStep'), 'Paste the provider API key. It is stored encrypted on this node and never shown again.');
    gwShow('gwKeyWrap', true); gwShow('gwUrlWrap', false); gwShow('gwCodeWrap', false);
    gwResult(''); var inp = $('#gwKeyInput'); if (inp) inp.value = '';
    var sub = $('#gwModalSubmit'); if (sub) { sub.textContent = 'Save'; sub.removeAttribute('hidden'); sub.disabled = false; }
    gwOpen(); if (inp) inp.focus();
  }

  function openConnect(pid) {
    gwMode = 'connect'; gwPid = pid;
    setText($('#gwModalTitle'), 'Connect · ' + pid);
    setText($('#gwModalStep'), 'Requesting authorization URL…');
    gwShow('gwKeyWrap', false); gwShow('gwUrlWrap', false); gwShow('gwCodeWrap', false);
    gwResult(''); var code = $('#gwCodeInput'); if (code) code.value = '';
    var sub = $('#gwModalSubmit'); if (sub) { sub.textContent = 'Submit code'; sub.setAttribute('hidden', ''); sub.disabled = false; }
    gwOpen();
    fetch('/api/gateway/cli/' + encodeURIComponent(pid) + '/login/start',
      { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, body: '{}', credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
      .then(function (o) {
        if (!o.ok) throw new Error(snErr(o.status, o.j));
        var url = (o.j.result && o.j.result.auth_url) || o.j.auth_url;
        if (!url) throw new Error('no auth_url in response');
        setText($('#gwModalStep'), 'Authorize in your browser, then paste the code below.');
        var u = $('#gwAuthUrl'); if (u) u.value = url;
        var open = $('#gwAuthOpen'); if (open) open.href = url;
        gwShow('gwUrlWrap', true); gwShow('gwCodeWrap', true);
        if (sub) sub.removeAttribute('hidden');
        if (code) code.focus();
      })
      .catch(function (e) { setText($('#gwModalStep'), 'Failed to start: ' + e.message); });
  }

  function gwSaveKey() {
    var inp = $('#gwKeyInput'); var val = (inp && inp.value || '').trim();
    if (!val) { gwResult('<span style="color:#c93b3b">enter a key</span>'); return; }
    var sub = $('#gwModalSubmit'); if (sub) sub.disabled = true;
    gwResult('saving…');
    fetch('/api/gateway/secrets',
      { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, credentials: 'same-origin',
        body: JSON.stringify({ ref: 'gateway/' + gwPid + '-key', value: val, provider: gwPid }) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
      .then(function (o) {
        if (!o.ok) throw new Error(snErr(o.status, o.j));
        gwResult('<span style="color:#2e7d32">saved · encrypted on node</span>');
        aiReloadOnce();   // the card says configured now, not after a reload
        setTimeout(gwClose, 700);
      })
      .catch(function (e) { gwResult('<span style="color:#c93b3b">save error: ' + e.message + '</span>'); if (sub) sub.disabled = false; });
  }

  function gwClearKey(pid) {
    snAsk('Clear the API key for ' + pid + '?', 'Agents using this provider cannot reach it until a new key is set.', 'Clear key', true).then(function (ok) {
      if (!ok) return;
      fetch('/api/gateway/secrets/delete',
        { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, credentials: 'same-origin',
          body: JSON.stringify({ ref: 'gateway/' + pid + '-key' }) })
        .then(function (r) { if (!r.ok) return r.json().then(function (j) { throw new Error(snErr(r.status, j)); }); })
        .then(function () { aiReloadOnce(); })
        .catch(function (e) { snTell('Could not clear the key', e.message); });
    });
  }

  function gwSubmitCode() {
    var c = $('#gwCodeInput'); var code = (c && c.value || '').trim();
    if (!code) { gwResult('<span style="color:#c93b3b">enter the code first</span>'); return; }
    var sub = $('#gwModalSubmit'); if (sub) sub.disabled = true;
    gwResult('submitting…');
    fetch('/api/gateway/cli/' + encodeURIComponent(gwPid) + '/login/complete',
      { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, credentials: 'same-origin',
        body: JSON.stringify({ code: code }) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
      .then(function (o) {
        if (!o.ok) throw new Error(snErr(o.status, o.j));
        var res = o.j.result || {};
        if (res.ok && res.stored !== false) {
          gwResult('<span style="color:#2e7d32">authorized and stored (encrypted).</span>');
          setText($('#gwModalStep'), 'Done.');
          gwShow('gwUrlWrap', false); gwShow('gwCodeWrap', false);
          if (sub) sub.setAttribute('hidden', '');
          setTimeout(gwClose, 900);
        } else if (res.ok && res.stored === false) {
          gwResult('<span style="color:#c93b3b">authorized, but storing the token failed (check core-api).</span>');
          if (sub) sub.disabled = false;
        } else {
          var tail = (res.output_tail || '').replace(/[<>]/g, '');
          gwResult('<span style="color:#c93b3b">not authorized.</span>' + (tail ? '<br><span style="color:var(--ink-mute)">' + escapeHtml(tail) + '</span>' : ''));
          if (sub) sub.disabled = false;
        }
      })
      .catch(function (e) { gwResult('<span style="color:#c93b3b">error: ' + e.message + '</span>'); if (sub) sub.disabled = false; });
  }

  function wireGwModal() {
    var grid = $('#providerGrid');
    if (grid) grid.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-cfg]');
      if (!btn) return;
      var pid = btn.getAttribute('data-pid'); var act = btn.getAttribute('data-cfg');
      if (act === 'setkey') return openKeyModal(pid);
      if (act === 'clearkey') return gwClearKey(pid);
      if (act === 'connect') return openConnect(pid);
    });
    var close = $('#gwModalClose'); if (close) close.addEventListener('click', gwClose);
    var cancel = $('#gwModalCancel'); if (cancel) cancel.addEventListener('click', gwClose);
    var scrim = $('#gwModalScrim'); if (scrim) scrim.addEventListener('click', function (e) { if (e.target === scrim) gwClose(); });
    var sub = $('#gwModalSubmit'); if (sub) sub.addEventListener('click', function () { if (gwMode === 'setkey') gwSaveKey(); else gwSubmitCode(); });
    var codeInp = $('#gwCodeInput'); if (codeInp) codeInp.addEventListener('keydown', function (e) { if (e.key === 'Enter') gwSubmitCode(); });
    var keyInp = $('#gwKeyInput'); if (keyInp) keyInp.addEventListener('keydown', function (e) { if (e.key === 'Enter') gwSaveKey(); });
  }

  // ---------------------------------------------------- ai gateway: context handoff policy
  // The context threshold has no block of its own any more: the node default
  // sits in the "whole node" budget card and each agent's value in its card.
  // This only loads the numbers and redraws the cards.
  function wireContextLimit() {
    function load() {
      fetch('/api/session-context-limits', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) return snFail(r); return r.json(); })
        .then(function (j) {
          var g = j.global || {};
          ctxState.def = g.threshold_tokens || 120000;
          ctxState.enabled = g.enabled !== false;
          ctxState.own = {};
          (j.agent_overrides || []).forEach(function (o) { if (o.agent_slug) ctxState.own[o.agent_slug] = o; });
          ctxState.loaded = true;
          ctxState.error = '';
          if (!budgetEditing) drawBudgets(budgetLast);
        })
        .catch(function (e) {
          ctxState.error = 'context limits: ' + e.message;
          if (!budgetEditing) drawBudgets(budgetLast);
        });
    }
    ctxState.reload = load;
    load();
  }

  // ---------------------------------------------------- agents
  // Health data from /api/agents/health, keyed by agent slug. Updated alongside
  // the agent list. Survives across re-renders so health is not lost if the agent
  // list refreshes before the health fetch completes.
  var agentHealthBySlug = {};
  // The process table from /api/agents/processes, keyed by agent slug. This
  // is what "working now" means on a card: the runtime's live processes, not
  // a lease row. Refreshed every few seconds and painted in place.
  var agentProcessesBySlug = {};

  function fmtDur(secs) {
    secs = Math.max(0, Math.floor(secs || 0));
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    return Math.floor(secs / 3600) + 'h ' + Math.floor((secs % 3600) / 60) + 'm';
  }

  function agentNowMarkup(slug, status) {
    var procs = agentProcessesBySlug[slug] || [];
    if (!procs.length) {
      return status === 'disabled'
        ? '<span class="muted">stopped</span>'
        : '<span class="muted">no process</span>';
    }
    return procs.map(function (p) {
      var line;
      if (p.turn && p.turn.in_progress) {
        line = '<span class="health-dot ok"></span> working ' + escapeHtml(fmtDur(p.turn.elapsed));
        if (p.turn.action) {
          line += ' · ' + escapeHtml(p.turn.action) + (p.turn.detail ? ': ' + escapeHtml(String(p.turn.detail).slice(0, 60)) : '');
        }
        if (p.stopping) line += ' <span class="muted">(stopping)</span>';
      } else {
        line = '<span class="health-dot warn"></span> idle ' + escapeHtml(fmtDur(p.idle_seconds));
      }
      if (p.last_call && p.last_call.at) {
        line += ' <span class="muted">· model ' + escapeHtml(relWhen(p.last_call.at)) + '</span>';
      }
      var where = (p.conversation || '').indexOf('desk:') >= 0 ? 'chat' : (p.session_type === 'person' ? 'person' : 'background');
      return '<div class="agent-now-line" title="pid ' + escapeAttr(String(p.pid)) + ' · ' + escapeAttr(p.conversation || p.session_id) + '">' +
        line + ' <span class="muted">· ' + escapeHtml(where) + '</span></div>';
    }).join('');
  }

  function paintAgentNow() {
    document.querySelectorAll('[data-agent-now]').forEach(function (dd) {
      var slug = dd.getAttribute('data-agent-now');
      var card = dd.closest('.agent-card');
      dd.innerHTML = agentNowMarkup(slug, card ? card.getAttribute('data-status') : '');
    });
  }

  function fetchAgentProcesses() {
    return fetch('/api/agents/processes', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var map = {};
        (d.processes || []).forEach(function (p) {
          var k = p.agent || '';
          (map[k] = map[k] || []).push(p);
        });
        agentProcessesBySlug = map;
      })
      .catch(function () {});
  }

  function renderAgents(d) {
    var s = d.summary || {};
    setText($('[data-field="agents-total"]'),    s.agents_total    != null ? s.agents_total    : '-');
    setText($('[data-field="agents-active"]'),   s.agents_active   != null ? s.agents_active   : '-');
    setText($('[data-field="agents-disabled"]'), s.agents_disabled != null ? s.agents_disabled : '-');

    var grid = $('#agentGrid');
    if (grid) {
      var agents = d.agents || [];
      if (agents.length === 0) {
        grid.innerHTML =
          '<div class="agent-empty">' +
            '<strong>No agents installed yet.</strong><br>' +
            'Clone the template to add one:<br>' +
            '<code>cp -r /srv/smart-node/agents/_template /srv/smart-node/agents/&lt;name&gt;</code><br>' +
            'then edit <code>agent.yaml</code> + <code>CLAUDE.md</code> inside. ' +
            'Schema reference: <code>/srv/smart-node/agents/README.md</code>.' +
          '</div>';
      } else {
        grid.innerHTML = '';
        agents.forEach(function (a) {
          var card = document.createElement('article');
          card.className = 'agent-card';
          card.setAttribute('data-status', a.status || 'active');
          if (a.source === 'legacy') card.classList.add('legacy');

          var title = a.title || a.name || '-';
          var mark = (title || '?').charAt(0);
          var dotCls = (a.status === 'active') ? 'ok' : 'warn';

          var descMarkup = a.description
            ? '<div class="agent-card-desc">' + escapeHtml(a.description) + '</div>'
            : '<div class="agent-card-desc muted">No description set.</div>';

          var sched = (a.schedule && String(a.schedule).trim()) ? escapeHtml(a.schedule) : '<span class="muted">manual</span>';
          var last  = a.last_run_iso ? escapeHtml(fmtTs(a.last_run_iso)) + ' · ' + escapeHtml(relWhen(a.last_run_iso)) : '<span class="muted">never</span>';
          var path  = a.path ? escapeHtml(a.path) : '<span class="muted">-</span>';

          // model badge
          var modelBadge = '';
          if (a.model && a.model !== 'none' && a.model.trim()) {
            // The badge colour is keyed on the model part of <runtime>/<model>
            // The badge text shows the whole id, runtime included.
            var mName = a.model.indexOf('/') > 0 ? a.model.slice(a.model.indexOf('/') + 1) : a.model;
            modelBadge = '<span class="agent-model agent-model-' + escapeAttr(mName) + '" title="' + escapeAttr(a.model) + '">' + escapeHtml(a.model) + '</span>';
          } else if (a.model === 'none') {
            modelBadge = '<span class="agent-model agent-model-none">no LLM</span>';
          }

          // department row (only if set)
          var deptRow = a.department
            ? '<dt>Department</dt><dd>' + escapeHtml(a.department) + '</dd>'
            : '';

          // version footer
          var verFoot = '';
          if (a.version || a.schema_version) {
            verFoot = '<footer class="agent-card-foot">' +
              (a.version ? 'v' + escapeHtml(a.version) : '') +
              (a.version && a.schema_version ? ' · ' : '') +
              (a.schema_version ? 'schema ' + escapeHtml(a.schema_version) : '') +
            '</footer>';
          }

          // warning banner (legacy / mismatch)
          var warnRow = a.warning
            ? '<div class="agent-card-warn" title="' + escapeAttr(a.warning) + '">' + escapeHtml(a.warning) + '</div>'
            : '';

          // health section from agentHealthBySlug
          var healthRow = '';
          var hd = agentHealthBySlug[a.name || a.slug || ''];
          if (hd) {
            var aggCls = hd.aggregate === 'fail' ? 'fail' : hd.aggregate === 'ok' ? 'ok' : 'warn';
            var aggLabel = hd.aggregate === 'fail' ? 'fail' : hd.aggregate === 'ok' ? 'ok' : 'untestable';
            // Build per-grant drill-down rows
            var grantRows = '';
            (hd.grants || []).forEach(function (g) {
              var gCls = g.status === 'fail' ? 'fail' : g.status === 'ok' ? 'ok' : 'warn';
              var gAge = g.last_checked_at ? relWhen(g.last_checked_at) : 'never';
              var gErr = (g.status === 'fail' && g.error_detail)
                ? ' <span class="muted" style="font-size:0.78em">' + escapeHtml(g.error_detail) + '</span>'
                : '';
              var gNote = (g.status === 'untestable')
                ? ' <span class="muted" style="font-size:0.78em">Cannot be verified externally</span>'
                : '';
              grantRows +=
                '<div class="health-grant-row">' +
                  '<span class="health-dot ' + gCls + '" title="' + escapeAttr(g.status) + '"></span> ' +
                  '<span class="health-grant-name">' + escapeHtml(g.name) + '</span>' +
                  '<span class="muted" style="font-size:0.8em;margin-left:4px">' + escapeHtml(g.type) + '</span>' +
                  gErr + gNote +
                  '<span class="health-grant-age muted">' + escapeHtml(gAge) + '</span>' +
                '</div>';
            });
            healthRow =
              '<dt>Health</dt>' +
              '<dd>' +
                '<details class="health-detail">' +
                  '<summary>' +
                    '<span class="health-dot ' + aggCls + '" title="' + escapeAttr(aggLabel) + '"></span> ' +
                    escapeHtml(aggLabel) +
                    (hd.grants && hd.grants.length ? ' <span class="muted" style="font-size:0.8em">(' + hd.grants.length + ' grants)</span>' : '') +
                  '</summary>' +
                  '<div class="health-grants">' + grantRows + '</div>' +
                '</details>' +
              '</dd>';
          } else {
            healthRow = '<dt>Health</dt><dd><span class="muted">no data</span></dd>';
          }

          // Stop ends the agent's processes now and disables it; Enable is
          // the way back. Both call core-api; the card repaints after.
          var actionsRow = '<div class="agent-card-actions">'
            + '<a class="btn-row" href="/agents/edit/?name=' + encodeURIComponent(a.name || '') + '#tools">Tools and permissions</a>'
            + (a.status === 'active'
            ? '<button type="button" class="btn-row btn-danger" data-agent-stop="' + escapeAttr(a.name || '') + '">Stop</button>'
            : '<button type="button" class="btn-row" data-agent-enable="' + escapeAttr(a.name || '') + '">Enable</button>') +
            '</div>';

          card.innerHTML =
            '<header class="agent-card-head">' +
              '<span class="agent-mark">' + escapeHtml(mark) + '</span>' +
              '<div class="agent-card-title">' +
                '<span class="agent-card-name">' + escapeHtml(title) + '</span>' +
                '<span class="agent-card-id">' + escapeHtml(a.name || '') + '</span>' +
              '</div>' +
              '<span class="agent-card-status"><span class="health-dot ' + dotCls + '" title="' + escapeAttr(a.status || '') + '"></span></span>' +
            '</header>' +
            descMarkup +
            warnRow +
            '<dl class="agent-card-meta">' +
              // The model sits with the other facts, not in the header: a
              // <runtime>/<model> id is long enough to squeeze the
              // name and id into a column (seen on the http-agent card).
              '<dt>Model</dt><dd>' + (modelBadge || '<span class="muted">not set</span>') + '</dd>' +
              '<dt>Schedule</dt><dd>' + sched + '</dd>' +
              '<dt>Last run</dt><dd>' + last + '</dd>' +
              '<dt>Now</dt><dd class="agent-now" data-agent-now="' + escapeAttr(a.name || '') + '">' + agentNowMarkup(a.name || '', a.status) + '</dd>' +
              deptRow +
              '<dt>Path</dt><dd>' + path + '</dd>' +
              healthRow +
            '</dl>' +
            verFoot +
            actionsRow;
          grid.appendChild(card);
        });
      }
    }

    setLive(d.generated_at);
  }

  function adaptApiAgent(a) {
    return {
      name: a.slug,
      title: a.title,
      description: a.description,
      role: a.role,
      model: a.model,
      version: a.version,
      department: a.department_id ? ('id:' + a.department_id) : null,
      status: a.enabled ? 'active' : 'disabled',
      path: '/srv/agents/' + a.slug,   // the agent's folder on the node (was /srv/smart-node/agents, which does not exist)
      source: 'db'
    };
  }

  function fetchAgentsHealth() {
    return fetch('/api/agents/health', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        var map = {};
        (d.health || []).forEach(function (h) { map[h.slug] = h; });
        agentHealthBySlug = map;
      })
      .catch(function () {});
  }

  function startAgentsPoll() {
    var INTERVAL = 30;
    var countdown = INTERVAL;
    function refresh() {
      Promise.all([
        fetch('/api/agents', { cache: 'no-store' })
          .then(function (r) { return r.ok ? r.json() : null; }),
        fetchAgentsHealth(),
        fetchAgentProcesses(),
      ]).then(function (results) {
        var d = results[0];
        if (!d) return;
        var rows = (d.agents || []).map(adaptApiAgent);
        var active   = rows.filter(function (a) { return a.status === 'active'; }).length;
        var disabled = rows.filter(function (a) { return a.status === 'disabled'; }).length;
        renderAgents({
          summary: { agents_total: rows.length, agents_active: active, agents_disabled: disabled },
          agents: rows,
          generated_at: new Date().toISOString()
        });
        countdown = INTERVAL;
      }).catch(function () {});
    }
    function tick() {
      countdown -= 1;
      if (countdown <= 0) { refresh(); return; }
      setText($('#nextRefresh'), String(countdown));
    }

    // Refresh health button: run the probe immediately, wait ~3 s for it to land,
    // then re-render. The button is disabled while the probe is running.
    var btn = $('#healthRefreshBtn');
    if (btn) {
      btn.addEventListener('click', function () {
        btn.disabled = true;
        btn.textContent = 'Refreshing...';
        fetch('/api/agents/health/refresh', { method: 'POST', cache: 'no-store' })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function () {
            // Wait for the probe to write its results before fetching health.
            return new Promise(function (res) { setTimeout(res, 3000); });
          })
          .then(function () { return fetchAgentsHealth(); })
          .then(function () {
            // Re-render with the same agent list, updated health data.
            return fetch('/api/agents', { cache: 'no-store' })
              .then(function (r) { return r.ok ? r.json() : null; });
          })
          .then(function (d) {
            if (d) {
              var rows = (d.agents || []).map(adaptApiAgent);
              var active   = rows.filter(function (a) { return a.status === 'active'; }).length;
              var disabled = rows.filter(function (a) { return a.status === 'disabled'; }).length;
              renderAgents({
                summary: { agents_total: rows.length, agents_active: active, agents_disabled: disabled },
                agents: rows,
                generated_at: new Date().toISOString()
              });
            }
          })
          .catch(function () {})
          .finally(function () {
            btn.disabled = false;
            btn.textContent = 'Refresh health';
          });
      });
    }

    // Stop / Enable on a card. Stop asks once: it ends processes mid-turn.
    var grid = $('#agentGrid');
    if (grid) {
      grid.addEventListener('click', function (e) {
        var stopBtn = e.target.closest && e.target.closest('[data-agent-stop]');
        var enableBtn = e.target.closest && e.target.closest('[data-agent-enable]');
        if (!stopBtn && !enableBtn) return;
        e.preventDefault();
        var button = stopBtn || enableBtn;
        var slug = button.getAttribute(stopBtn ? 'data-agent-stop' : 'data-agent-enable');
        (stopBtn ? snAsk('Stop ' + slug + '?', 'Its processes end now, a turn in flight included, and it does not wake until enabled again.', 'Stop', true) : Promise.resolve(true)).then(function (ok) {
          if (!ok) return;
          button.disabled = true;
          var call = stopBtn
            ? fetch('/api/agents/' + encodeURIComponent(slug) + '/stop', { method: 'POST', cache: 'no-store',
                    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'stopped from the panel' }) })
            : fetch('/api/agents/' + encodeURIComponent(slug), { method: 'PUT', cache: 'no-store',
                    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: true }) });
          call.then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, j: j }; }); })
            .then(function (res) {
              if (!res.ok) {
                snTell((stopBtn ? 'Stop' : 'Enable') + ' failed', snErr(res.status, res.j));
              } else if (stopBtn && res.j.gateway && res.j.gateway !== 'ok') {
                snTell(slug + ' is disabled', 'Its processes could not be ended: gateway ' + res.j.gateway);
              }
            })
            .catch(function (err) { snTell((stopBtn ? 'Stop' : 'Enable') + ' failed', String(err)); })
            .finally(function () { refresh(); });
        });
      });
    }

    refresh();
    setInterval(tick, 1000);
    // The process table moves faster than the agent list: repaint the Now
    // cells in place, so an open Health drawer is not closed by a re-render.
    setInterval(function () { fetchAgentProcesses().then(paintAgentNow); }, 5000);
  }

  // ---------------------------------------------------- tools (CRUD)
  var toolsState = { rows: [], filterKind: '' };

  function fetchTools() {
    var q = toolsState.filterKind ? ('?kind=' + encodeURIComponent(toolsState.filterKind)) : '';
    return fetch('/api/tools' + q, { cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('GET /api/tools -> HTTP ' + r.status);
        return r.json();
      });
  }

  function renderTools() {
    var rows = toolsState.rows.slice().sort(function (a, b) {
      if (a.kind !== b.kind) return a.kind.localeCompare(b.kind);
      return (a.name || '').localeCompare(b.name || '');
    });

    // Stat cards
    var total = rows.length;
    var avail = rows.filter(function (r) { return r.status === 'available'; }).length;
    var broken = rows.filter(function (r) { return r.status === 'broken'; }).length;
    var stub = rows.filter(function (r) { return r.status === 'stub'; }).length;
    setText($('[data-field="tools-total"]'), total);
    setText($('[data-field="tools-available"]'), avail);
    setText($('[data-field="tools-broken"]'), broken);
    setText($('[data-field="tools-stub"]'), stub);
    var brokenCard = $('[data-stat="tools-broken"]');
    if (brokenCard) brokenCard.classList.toggle('warn', broken > 0);

    var body = $('#toolsBody'); if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">No tools registered.</td></tr>';
    } else {
      body.innerHTML = '';
      rows.forEach(function (r) {
        var tr = document.createElement('tr');
        var statusDot = 'ok';
        if (r.status === 'broken') statusDot = 'fail';
        else if (r.status === 'stub') statusDot = 'warn';
        var tags = (r.tags || []).map(function (t) {
          return '<span class="tag-chip">' + escapeHtml(t) + '</span>';
        }).join('');
        tr.innerHTML =
          '<td class="col-name"><code>' + escapeHtml(r.name) + '</code></td>' +
          '<td class="col-kind"><span class="kind-tag kind-' + escapeAttr(r.kind) + '">' + escapeHtml(r.kind) + '</span></td>' +
          '<td class="col-title">' + escapeHtml(r.title || '-') + '</td>' +
          '<td class="col-status"><span class="health-dot ' + statusDot + '"></span> ' + escapeHtml(r.status) + '</td>' +
          '<td class="col-tags">' + (tags || '<span class="muted">-</span>') + '</td>' +
          '<td class="col-actions">' +
            '<button type="button" class="btn-row" data-act="edit" data-name="' + escapeAttr(r.name) + '">Edit</button> ' +
            '<button type="button" class="btn-row btn-danger" data-act="delete" data-name="' + escapeAttr(r.name) + '">Delete</button>' +
          '</td>';
        body.appendChild(tr);
      });
    }

    setText($('#lastRefresh'), new Date().toLocaleTimeString(undefined, { hour12: false }));
    var dot = $('#liveDot'); if (dot) { dot.classList.remove('stale'); dot.classList.add('live'); }
  }

  function refreshTools() {
    return fetchTools()
      .then(function (d) { toolsState.rows = d.tools || []; renderTools(); })
      .catch(function (err) {
        var body = $('#toolsBody');
        if (body) body.innerHTML = '<tr class="empty-row"><td colspan="6">Failed to load: ' + escapeHtml(err.message || err) + '</td></tr>';
      });
  }

  function openToolModal(existing) {
    var scrim = $('#toolModalScrim');
    var form = $('#toolForm');
    if (!scrim || !form) return;
    form.reset();
    $('#toolFormError').textContent = '';
    if (existing) {
      $('#toolModalTitle').textContent = 'Edit tool · ' + existing.name;
      $('#toolName').value = existing.name;
      $('#toolName').readOnly = true;
      form.elements['original_name'].value = existing.name;
      $('#toolKind').value = existing.kind;
      $('#toolStatus').value = existing.status || 'available';
      $('#toolTitle').value = existing.title || '';
      $('#toolDesc').value = existing.description || '';
      $('#toolInstallUri').value = existing.install_uri || '';
      $('#toolTags').value = (existing.tags || []).join(', ');
    } else {
      $('#toolModalTitle').textContent = 'New tool';
      $('#toolName').readOnly = false;
      form.elements['original_name'].value = '';
    }
    scrim.hidden = false;
    setTimeout(function () { (existing ? $('#toolTitle') : $('#toolName')).focus(); }, 30);
  }

  function closeToolModal() {
    var scrim = $('#toolModalScrim');
    if (scrim) scrim.hidden = true;
  }

  function readToolForm() {
    var form = $('#toolForm');
    var fd = new FormData(form);
    var tagsRaw = String(fd.get('tags') || '').trim();
    var tags = tagsRaw ? tagsRaw.split(',').map(function (t) { return t.trim(); }).filter(Boolean) : [];
    return {
      name: String(fd.get('name') || '').trim(),
      kind: String(fd.get('kind') || ''),
      title: String(fd.get('title') || '').trim(),
      description: String(fd.get('description') || ''),
      install_uri: String(fd.get('install_uri') || '').trim(),
      status: String(fd.get('status') || 'available'),
      tags: tags
    };
  }

  function submitToolForm(e) {
    e.preventDefault();
    var form = $('#toolForm');
    var err = $('#toolFormError'); err.textContent = '';
    var submitBtn = $('#toolSubmit'); submitBtn.disabled = true;

    var origName = form.elements['original_name'].value;
    var body = readToolForm();
    var isEdit = !!origName;
    var url = isEdit ? '/api/tools/' + encodeURIComponent(origName) : '/api/tools';
    var method = isEdit ? 'PUT' : 'POST';
    // PUT uses ToolUpdate (no name field)
    if (isEdit) { delete body.name; }

    fetch(url, {
      method: method,
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) {
          throw new Error(snErr(r.status, j));
        }, function () { throw new Error(snErr(r.status)); });
      }
      closeToolModal();
      return refreshTools();
    }).catch(function (e2) {
      err.textContent = (e2 && e2.message) ? String(e2.message) : 'Save failed';
    }).then(function () { submitBtn.disabled = false; });
  }

  function deleteTool(name) {
    if (!name) return;
    snAsk('Delete tool "' + name + '"?', 'Agents lose it. This cannot be undone.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch('/api/tools/' + encodeURIComponent(name), {
        method: 'DELETE',
        headers: { 'Accept': 'application/json' }
      }).then(function (r) {
        if (!r.ok && r.status !== 204) return snFail(r);
        return refreshTools();
      }).catch(function (e) {
        snTell('Delete failed', (e.message || e));
      });
    });
  }

  function startToolsPage() {
    var kindSel = $('#filterToolKind');
    if (kindSel) {
      kindSel.addEventListener('change', function () {
        toolsState.filterKind = kindSel.value;
        refreshTools();
      });
    }

    var newBtn = $('#toolNewBtn');
    if (newBtn) newBtn.addEventListener('click', function () { openToolModal(null); });

    $('#toolModalClose')  && $('#toolModalClose').addEventListener('click', closeToolModal);
    $('#toolModalCancel') && $('#toolModalCancel').addEventListener('click', closeToolModal);
    $('#toolModalScrim')  && $('#toolModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'toolModalScrim') closeToolModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var scrim = $('#toolModalScrim');
        if (scrim && !scrim.hidden) closeToolModal();
      }
    });

    var form = $('#toolForm');
    if (form) form.addEventListener('submit', submitToolForm);

    var body = $('#toolsBody');
    if (body) body.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act');
      var name = btn.getAttribute('data-name');
      if (act === 'delete') return deleteTool(name);
      if (act === 'edit') {
        var row = (toolsState.rows || []).find(function (r) { return r.name === name; });
        if (row) openToolModal(row);
      }
    });

    refreshTools();
  }

  function wireAgentCardClicks() {
    // Make each rendered agent card clickable -> navigate to /agents/edit/?name=…
    var grid = $('#agentGrid');
    if (!grid) return;
    grid.addEventListener('click', function (e) {
      var card = e.target.closest && e.target.closest('.agent-card');
      if (!card) return;
      // The Health grants list opens in place; a click there (or on any link or
      // button inside the card) is not a click on the card.
      if (e.target.closest('details, summary, a, button')) return;
      var name = (card.querySelector('.agent-card-id') || {}).textContent;
      if (!name) return;
      window.location.href = '/agents/edit/?name=' + encodeURIComponent(name.trim());
    });
    // make cards look clickable
    var style = document.createElement('style');
    style.textContent = '.agent-card { cursor: pointer; } .agent-card:hover { border-color: var(--ink-faint); }';
    document.head.appendChild(style);
  }

  // ---------------------------------------------------- agent edit page
  var aeState = {
    name: null,
    tools: [],         // all tools from /api/tools
    grants: {},        // tool_name -> "rw-" / etc.
    sources: [],       // ordered list of source keys
    activeSources: {}  // {source_key: true} for chips currently ON
  };

  function aeParseSource(t) {
    // B2-Integrations: integration_slug is the source of truth.
    if (t.integration_slug) return t.integration_slug;
    // Legacy fallback for any row that pre-dates the migration / wasn't
    // backfilled. The tag path will be removed once phase 7 ships.
    var tags = t.tags || [];
    for (var i = 0; i < tags.length; i++) if (tags[i].indexOf('source:') === 0) return tags[i].slice(7);
    return t.kind === 'builtin' ? 'builtin' : 'other';
  }

  function aeSourceLabel(key) {
    // B2-Integrations: prefer the human label from the integrations
    // registry, fall back to the legacy heuristics on the source: tag.
    var byslug = aeState.integrationsByslug;
    if (byslug && byslug[key] && byslug[key].label) return byslug[key].label;
    if (key === 'builtin' || key === 'system') return 'System';
    if (key === 'other') return 'Other';
    if (key.indexOf('component:') === 0) {
      var s = key.slice(10);
      return s ? s.charAt(0).toUpperCase() + s.slice(1) : key;
    }
    return key.charAt(0).toUpperCase() + key.slice(1);
  }

  function aeQueryName() {
    var p = new URLSearchParams(window.location.search);
    return p.get('name') || '';
  }

  function aeRenderHeader(meta) {
    setText($('#agentTitle'), meta.title || meta.name || '-');
    setText($('#agentName'), meta.name || '-');
    setText($('#agentRole'), meta.role || meta.department || '-');
    setText($('#agentModel'), meta.model || '-');
    document.title = 'Smart Node · ' + (meta.title || meta.name || 'agent') + ' · edit';
  }

  function aeRenderSources() {
    var groups = {};
    aeState.tools.forEach(function (t) {
      var g = aeParseSource(t);
      (groups[g] = groups[g] || []).push(t);
    });
    var keys = Object.keys(groups).sort(function (a, b) {
      if (a === 'builtin') return -1;
      if (b === 'builtin') return 1;
      if (a === 'other') return 1;
      if (b === 'other') return -1;
      return a.localeCompare(b);
    });
    aeState.sources = keys.map(function (k) { return { name: k, tools: groups[k] }; });

    // Auto-activate chips for sources that already have grants
    aeState.sources.forEach(function (s) {
      if (aeState.activeSources[s.name]) return;
      var hasGrant = s.tools.some(function (t) {
        var m = aeState.grants[t.name];
        return m && m !== '---';
      });
      if (hasGrant) aeState.activeSources[s.name] = true;
    });

    var row = $('#aeSrcChips');
    if (!row) return;
    row.innerHTML = '';
    aeState.sources.forEach(function (s) {
      var on = !!aeState.activeSources[s.name];
      var grantedCount = s.tools.filter(function (t) {
        var m = aeState.grants[t.name];
        return m && m !== '---';
      }).length;
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (on ? ' chip-on' : '');
      chip.setAttribute('data-source', s.name);
      chip.innerHTML =
        '<span class="chip-label">' + escapeHtml(aeSourceLabel(s.name)) + '</span>' +
        '<span class="chip-meta">' + grantedCount + '/' + s.tools.length + '</span>';
      row.appendChild(chip);
    });
  }

  function aeActiveSourceObjs() {
    return aeState.sources.filter(function (s) { return aeState.activeSources[s.name]; });
  }

  function aeRenderTools() {
    var body = $('#aeToolsBody');
    if (!body) return;
    var actives = aeActiveSourceObjs();
    if (!actives.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="5">Pick at least one source above.</td></tr>';
      return;
    }
    body.innerHTML = '';
    actives.forEach(function (s) {
      var label = aeSourceLabel(s.name);
      var header = document.createElement('tr');
      header.className = 'ae-tools-grouphead';
      header.innerHTML = '<td colspan="5">' + escapeHtml(label) + ' <span class="muted">· ' + s.tools.length + ' tools</span></td>';
      body.appendChild(header);
      s.tools.slice().sort(function (a, b) { return a.name.localeCompare(b.name); }).forEach(function (t) {
        var mode = aeState.grants[t.name] || '---';
        var tr = document.createElement('tr');
        tr.setAttribute('data-tool', t.name);
        tr.setAttribute('data-source', s.name);
        tr.innerHTML =
          '<td class="ae-col-tool"><code>' + escapeHtml(t.name) + '</code><div class="ae-tool-title">' + escapeHtml(t.title || '') + '</div></td>' +
          '<td class="ae-col-kind"><span class="kind-tag kind-' + escapeAttr(t.kind) + '">' + escapeHtml(t.kind) + '</span></td>' +
          '<td class="ae-col-mode"><input type="checkbox" data-pos="0" ' + (mode[0] === 'r' ? 'checked' : '') + '></td>' +
          '<td class="ae-col-mode"><input type="checkbox" data-pos="1" ' + (mode[1] === 'w' ? 'checked' : '') + '></td>' +
          '<td class="ae-col-mode"><input type="checkbox" data-pos="2" ' + (mode[2] === 'd' ? 'checked' : '') + '></td>';
        body.appendChild(tr);
      });
    });
  }

  function aeCollectFromTable() {
    // Capture current visible table into state.grants (so switching source doesn't lose changes)
    var rows = $$('#aeToolsBody tr[data-tool]');
    rows.forEach(function (tr) {
      var name = tr.getAttribute('data-tool');
      var inputs = $$('input[type="checkbox"]', tr);
      var mode = (inputs[0].checked ? 'r' : '-') +
                 (inputs[1].checked ? 'w' : '-') +
                 (inputs[2].checked ? 'd' : '-');
      if (mode === '---') delete aeState.grants[name];
      else aeState.grants[name] = mode;
    });
  }

  function aeUpdateCount() {
    var n = Object.keys(aeState.grants).filter(function (k) { return aeState.grants[k] !== '---'; }).length;
    setText($('#aeGrantsCount'), n + ' grant' + (n === 1 ? '' : 's'));
  }

  function aeBulkMode(mode) {
    aeCollectFromTable();
    var actives = aeActiveSourceObjs();
    if (!actives.length) return;
    actives.forEach(function (src) {
      src.tools.forEach(function (t) {
        if (mode === '---') delete aeState.grants[t.name];
        else aeState.grants[t.name] = mode;
      });
    });
    aeRenderTools();
    aeRenderSources();
    aeUpdateCount();
  }

  function aeWire() {
    // Source chip click · toggle that source in/out of view
    var chipsRow = $('#aeSrcChips');
    if (chipsRow) chipsRow.addEventListener('click', function (e) {
      var chip = e.target.closest && e.target.closest('.chip[data-source]');
      if (!chip) return;
      aeCollectFromTable();
      var key = chip.getAttribute('data-source');
      if (aeState.activeSources[key]) delete aeState.activeSources[key];
      else aeState.activeSources[key] = true;
      aeRenderSources();
      aeRenderTools();
      aeUpdateCount();
    });

    // Per-cell checkbox flips trigger source count refresh
    if ($('#aeToolsBody')) $('#aeToolsBody').addEventListener('change', function (e) {
      if (e.target && e.target.matches('input[type="checkbox"]')) {
        aeCollectFromTable();
        aeRenderSources();
        aeUpdateCount();
      }
    });

    // Bulk buttons
    if ($('#aeAllR')) {
      $('#aeAllR').addEventListener('click',  function () { aeBulkMode('r--'); });
      $('#aeAllW').addEventListener('click',  function () { aeBulkMode('-w-'); });
      $('#aeAllRW').addEventListener('click', function () { aeBulkMode('rw-'); });
      $('#aeClear').addEventListener('click', function () { aeBulkMode('---'); });
    }

    aeWireConfig();

    // Save: Settings + Access via PUT /api/agents/<slug>, then Tools grants
    $('#aeSave').addEventListener('click', function () {
      aeCollectFromTable();
      var btn = $('#aeSave'); btn.disabled = true;
      var grantsPayload = { grants: {} };
      Object.keys(aeState.grants).forEach(function (k) {
        if (aeState.grants[k] && aeState.grants[k] !== '---') grantsPayload.grants[k] = aeState.grants[k];
      });
      var mfPayload = aeReadManifestForm();
      mfPayload.change_note = 'edit via panel';

      // Save sequentially (config, then grants): both rebuild CLAUDE.md from the
      // registry, so the grants save must run last to compose with the fresh role
      // AND the fresh tool grants. Parallel saves would race on the file.
      var savedMeta;
      fetch('/api/agents/' + encodeURIComponent(aeState.name), {
        method: 'PUT', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(mfPayload)
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error('manifest: ' + (snErr(r.status, j))); });
        return r.json();
      }).then(function (meta) {
        savedMeta = meta;
        return fetch('/api/agents/' + encodeURIComponent(aeState.name) + '/grants', {
          method: 'PUT', cache: 'no-store',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify(grantsPayload)
        });
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error('grants: ' + (snErr(r.status, j))); });
        return r.json();
      }).then(function () {
        var meta = savedMeta;
        aeState.meta = meta;
        aePopulateManifest(meta);    // refresh version badge etc
        aeMarkClean();
        window.snToast('Saved');
      }).catch(function (e) {
        window.snToast('Save failed: ' + (e.message || e));
      }).then(function () { btn.disabled = false; });
    });
  }

  // ---------------------------------------------------- agent-edit: dirty state + preview
  function aeMarkDirty()  { aeState.dirty = true;  var d = $('#aeDirtyDot'); if (d) d.hidden = false; }
  function aeMarkClean()  { aeState.dirty = false; var d = $('#aeDirtyDot'); if (d) d.hidden = true; }

  // Preview shows the prompt core-api composes, byte for byte what the
  // agent reads from /srv/agents/<slug>/CLAUDE.md. It used to be assembled here
  // from a template (aeBuildPrompt) that resembled nothing the agent receives.
  // Fetched only while the tab is open; other tabs call this on every change.
  function aeRenderPreview() {
    var panel = document.querySelector('[data-tab-panel="preview"]');
    if (!panel || panel.hidden || !aeState.name) return;
    aeLoadPrompt();
  }
  function aeLoadPrompt() {
    var box = $('#aePrompt');
    setText($('#aePromptStats'), 'loading…');
    return fetch('/api/agents/' + encodeURIComponent(aeState.name) + '/system-prompt', { cache: 'no-store' })
      .then(function (r) {
        return r.text().then(function (t) {
          if (!r.ok) throw new Error(snErr(r.status));
          return t;
        });
      })
      .then(function (t) {
        if (box) box.value = t;
        setText($('#aePromptStats'), t.length + ' chars · ' + t.split('\n').length + ' lines · built by the node');
      })
      .catch(function (e) { setText($('#aePromptStats'), 'load failed: ' + (e.message || e)); });
  }
  function aeGeneratePrompt() {
    var btn = $('#aePromptGenerate');
    if (btn) btn.disabled = true;
    setText($('#aePromptStats'), 'generating…');
    fetch('/api/agents/' + encodeURIComponent(aeState.name) + '/regenerate', { method: 'POST', cache: 'no-store' })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error(snErr(r.status, j));
          return j;
        });
      })
      .then(function (j) {
        return aeLoadPrompt().then(function () {
          var s = $('#aePromptStats');
          if (s) s.textContent += ' · generated, ' + (j.sessions_recycled || 0) + ' session(s) restarted';
        });
      })
      .catch(function (e) { setText($('#aePromptStats'), 'generate failed: ' + (e.message || e)); })
      .then(function () { if (btn) btn.disabled = false; });
  }

  function aeWireConfig() {
    var gen = $('#aePromptGenerate');
    if (gen) gen.addEventListener('click', aeGeneratePrompt);

    var copy = $('#aePromptCopy');
    if (copy) copy.addEventListener('click', function () {
      var t = $('#aePrompt') ? $('#aePrompt').value : '';
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(t).then(function () {
          copy.textContent = 'Copied';
          setTimeout(function () { copy.textContent = 'Copy'; }, 1200);
        });
      } else {
        var ta = $('#aePrompt'); ta.select(); document.execCommand('copy');
        copy.textContent = 'Copied'; setTimeout(function () { copy.textContent = 'Copy'; }, 1200);
      }
    });

    // mark dirty on tools grants change
    var toolsBody = $('#aeToolsBody');
    if (toolsBody) toolsBody.addEventListener('change', function (e) {
      if (e.target && e.target.type === 'checkbox') { aeMarkDirty(); aeRenderPreview(); }
    });
    ['#aeAllR','#aeAllW','#aeAllRW','#aeClear'].forEach(function (sel) {
      var b = $(sel); if (b) b.addEventListener('click', function () { setTimeout(function () { aeMarkDirty(); aeRenderPreview(); }, 0); });
    });
  }

  // ---------------------------------------------------- agent: offered to the model
  // Each MCP server in the agent's .mcp.json, with its size in tokens, and a switch;
  // under it, its tools with theirs. core-api keeps the list (PUT withheld-tools) and
  // the runtime passes it as --disallowedTools from the next process start.
  var aeMt = { name: '', data: null, open: {} };
  function aeMtTok(n) {
    if (n == null) return 'size unknown';
    return '~' + (n >= 1000 ? (Math.round(n / 100) / 10) + 'k' : String(n)) + ' tokens';
  }
  function aeMtList() {
    var d = aeMt.data, out = [];
    (d.servers || []).forEach(function (s) {
      if (s.hidden) { out.push('mcp__' + s.server); return; }
      (s.tools || []).forEach(function (t) { if (t.hidden) out.push('mcp__' + s.server + '__' + t.name); });
    });
    return out;
  }
  // One tree for the Tools tab: the agent's MCP servers with the
  // switch "offered to the model", their size and the grants (R W D) on the server and
  // on each tool; the node's own tools (System, core-api, planner...) with their grants;
  // and the other MCP servers, folded, to give one. aeState.grants stays the one record
  // of the grants: Save configuration sends it whole, as before, so a grant nobody
  // touched is sent back as it came. The switches save at once (aeMtSave).
  function aeRwd(name) {
    if (!name) return '<span class="ae-rwd is-none"><label><input type="checkbox" disabled>R</label><label><input type="checkbox" disabled>W</label><label><input type="checkbox" disabled>D</label></span>';
    var m = aeState.grants[name] || '---';
    return '<span class="ae-rwd">' + ['R', 'W', 'D'].map(function (l, i) {
      return '<label title="' + ['Read', 'Write', 'Delete'][i] + '"><input type="checkbox" data-g="' + escapeAttr(name) + '" data-pos="' + i + '"' +
        (m[i] && m[i] !== '-' ? ' checked' : '') + '>' + l + '</label>';
    }).join('') + '</span>';
  }
  function aeHasGrant(name) { var m = aeState.grants[name]; return !!m && m !== '---'; }
  function aeTree() {
    var reg = aeState.tools || [];
    var mine = {};
    ((aeMt.data && aeMt.data.servers) || []).forEach(function (x) { mine[x.server] = x; });
    var all = (aeMt.allServers || []).slice();
    Object.keys(mine).forEach(function (k) { if (all.indexOf(k) < 0) all.push(k); });
    var bylen = all.slice().sort(function (a, b) { return b.length - a.length; });
    var tools = {}, row = {}, rest = [];
    all.forEach(function (k) { tools[k] = []; });
    reg.forEach(function (t) {
      if (all.indexOf(t.name) >= 0) { row[t.name] = t; return; }
      for (var i = 0; i < bylen.length; i++) {
        if (t.name.indexOf(bylen[i] + '-') === 0) { tools[bylen[i]].push(t); return; }
      }
      rest.push(t);
    });
    var groups = {};
    rest.forEach(function (t) { var g = aeParseSource(t); (groups[g] = groups[g] || []).push(t); });
    return { mine: mine, all: all.sort(), tools: tools, row: row, groups: groups };
  }
  function aeMtRender() {
    var box = $('#aeModelTools'); if (!box || !aeMt.data || !aeState.toolsLoaded) return;
    var T = aeTree(), html = [];
    var short = function (srv, name) { return name.slice(srv.length + 1); };
    var granted = function (list) { return list.filter(function (t) { return aeHasGrant(t.name); }).length; };
    var more = function (key, n) {
      return n ? '<button type="button" class="ae-mt-more" data-mt-open="' + escapeAttr(key) + '">' + n + ' tools ' + (aeMt.open[key] ? '&#9662;' : '&#9656;') + '</button>' : '<span class="ae-mt-more"></span>';
    };
    // 1. the agent's MCP servers
    var mineKeys = T.all.filter(function (k) { return T.mine[k]; });
    if (mineKeys.length) html.push('<div class="ae-mt-sec">MCP servers of this agent</div>');
    mineKeys.forEach(function (k) {
      var m = T.mine[k], off = m.hidden, sizes = {};
      (m.tools || []).forEach(function (t) { sizes[t.name] = t; });
      var list = T.tools[k].slice().sort(function (a, b) { return a.name.localeCompare(b.name); });
      html.push('<div class="ae-mt-srv' + (off ? ' is-off' : '') + '"><div class="ae-mt-row">' +
        '<span class="ae-mt-sw"><label class="sn-switch"><input type="checkbox" role="switch" data-mt-srv="' + escapeAttr(k) + '"' +
          (off ? '' : ' checked') + (m.locked ? ' disabled title="The agent answers chats through it"' : '') + '></label></span>' +
        '<span class="ae-mt-name">' + escapeHtml(k) + (m.locked ? ' <span class="cap-soon-tag">always on</span>' : '') + '</span>' +
        '<span class="ae-mt-gr">' + (list.length ? granted(list) + '/' + list.length + ' granted' : '') + '</span>' +
        '<span class="ae-mt-tok">' + escapeHtml(aeMtTok(m.tokens)) + '</span>' +
        aeRwd(T.row[k] ? k : null) + more(k, list.length) + '</div>');
      if (aeMt.open[k] && list.length) {
        html.push('<div class="ae-mt-tools">' + list.map(function (t) {
          var n = short(k, t.name), z = sizes[n] || {}, hid = !!z.hidden;
          return '<div class="ae-mt-row' + (hid || off ? ' is-off' : '') + '">' +
            '<span class="ae-mt-sw"><label class="sn-switch"><input type="checkbox" role="switch" data-mt-tool="' + escapeAttr(k + '__' + n) + '"' +
              (hid ? '' : ' checked') + (m.locked || off ? ' disabled' : '') + '></label></span>' +
            '<span class="ae-mt-name" title="' + escapeAttr(t.title || '') + '">' + escapeHtml(n) + '</span>' +
            '<span class="ae-mt-gr"></span><span class="ae-mt-tok">' + escapeHtml(aeMtTok(z.tokens)) + '</span>' + aeRwd(t.name) +
            '<span class="ae-mt-more"></span></div>';
        }).join('') + '</div>');
      }
      html.push('</div>');
    });
    // 2. the node's own tools
    var gkeys = Object.keys(T.groups).sort(function (a, b) {
      if (a === 'builtin' || a === 'system') return -1;
      if (b === 'builtin' || b === 'system') return 1;
      return aeSourceLabel(a).localeCompare(aeSourceLabel(b));
    });
    if (gkeys.length) html.push('<div class="ae-mt-sec">Node tools</div>');
    gkeys.forEach(function (g) {
      var list = T.groups[g].slice().sort(function (a, b) { return a.name.localeCompare(b.name); }), key = 'g:' + g;
      html.push('<div class="ae-mt-srv"><div class="ae-mt-row"><span class="ae-mt-sw"></span>' +
        '<span class="ae-mt-name">' + escapeHtml(aeSourceLabel(g)) + '</span>' +
        '<span class="ae-mt-gr">' + granted(list) + '/' + list.length + ' granted</span><span class="ae-mt-tok"></span>' +
        aeRwd(null) + more(key, list.length) + '</div>');
      if (aeMt.open[key]) {
        html.push('<div class="ae-mt-tools">' + list.map(function (t) {
          return '<div class="ae-mt-row"><span class="ae-mt-sw"></span>' +
            '<span class="ae-mt-name" title="' + escapeAttr(t.title || '') + '">' + escapeHtml(t.name) + '</span>' +
            '<span class="ae-mt-gr"></span><span class="ae-mt-tok"></span>' + aeRwd(t.name) + '<span class="ae-mt-more"></span></div>';
        }).join('') + '</div>');
      }
      html.push('</div>');
    });
    // 3. the other MCP servers: a grant on the server gives it on Save
    var others = T.all.filter(function (k) { return !T.mine[k]; });
    if (others.length) {
      html.push('<div class="ae-mt-sec"><button type="button" class="ae-mt-more" data-mt-open="__others">Other MCP servers (' + others.length + ') ' +
        (aeMt.open.__others ? '&#9662;' : '&#9656;') + '</button></div>');
      if (aeMt.open.__others) others.forEach(function (k) {
        var list = T.tools[k].slice().sort(function (a, b) { return a.name.localeCompare(b.name); });
        html.push('<div class="ae-mt-srv is-other"><div class="ae-mt-row"><span class="ae-mt-sw"></span>' +
          '<span class="ae-mt-name">' + escapeHtml(k) + '</span>' +
          '<span class="ae-mt-gr">' + (list.length ? granted(list) + '/' + list.length + ' granted' : 'not given') + '</span>' +
          '<span class="ae-mt-tok"></span>' + aeRwd(T.row[k] ? k : null) + more(k, list.length) + '</div>');
        if (aeMt.open[k] && list.length) {
          html.push('<div class="ae-mt-tools">' + list.map(function (t) {
            return '<div class="ae-mt-row"><span class="ae-mt-sw"></span><span class="ae-mt-name" title="' + escapeAttr(t.title || '') + '">' +
              escapeHtml(short(k, t.name)) + '</span><span class="ae-mt-gr"></span><span class="ae-mt-tok"></span>' + aeRwd(t.name) +
              '<span class="ae-mt-more"></span></div>';
          }).join('') + '</div>');
        }
        html.push('</div>');
      });
    }
    box.innerHTML = html.join('');
    var d = aeMt.data, offered = 0, taken = 0;
    (d.servers || []).forEach(function (x) {
      (x.tools || []).forEach(function (t) { if (x.hidden || t.hidden) taken += t.tokens || 0; else offered += t.tokens || 0; });
    });
    var ng = Object.keys(aeState.grants).filter(function (k) { return aeHasGrant(k); }).length;
    var note = $('#aeModelToolsNote');
    if (note) note.textContent = ng + ' grant' + (ng === 1 ? '' : 's') + ' · tools offered: ' + aeMtTok(offered) +
      (taken ? ' · switched off: ' + aeMtTok(taken) : '') +
      (d.last_request_tokens ? ' · last request sent ' + Number(d.last_request_tokens).toLocaleString() + ' tokens in all' : '') +
      (aeMt.saved ? ' · ' + aeMt.saved : '');
  }
  function aeMtSave() {
    var list = aeMtList();
    aeMt.saved = 'saving';
    aeMtRender();
    fetch('/api/agents/' + encodeURIComponent(aeMt.name) + '/withheld-tools', {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tools: list })
    }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || ('HTTP ' + r.status)); });
      aeMt.saved = 'saved, applies from the next start';
      aeMtRender();
    }).catch(function (e) { aeMt.saved = 'not saved: ' + e.message; aeMtLoad(aeMt.name); });
  }
  function aeMtLoad(name) {
    aeMt.name = name;
    fetch('/api/mcp/servers', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { servers: [] }; })
      .then(function (j) { aeMt.allServers = (j.servers || []).filter(function (x) { return x.enabled !== false; }).map(function (x) { return x.slug; }); aeMtRender(); })
      .catch(function () {});
    fetch('/api/agents/' + encodeURIComponent(name) + '/model-tools', { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { aeMt.data = d; aeMtRender(); })
      .catch(function (e) { var b = $('#aeModelTools'); if (b) b.innerHTML = '<span class="muted">Not available (' + escapeHtml(e.message) + ').</span>'; });
  }
  function aeMtWire() {
    var box = $('#aeModelTools'); if (!box) return;
    box.addEventListener('click', function (e) {
      var b = e.target.closest('[data-mt-open]'); if (!b) return;
      var k = b.getAttribute('data-mt-open'); aeMt.open[k] = !aeMt.open[k]; aeMtRender();
    });
    box.addEventListener('change', function (e) {
      var t = e.target; if (!t || !aeMt.data) return;
      var g = t.getAttribute('data-g');
      if (g) {
        var boxes = t.closest('.ae-rwd').querySelectorAll('input[data-g]');
        var mode = (boxes[0].checked ? 'r' : '-') + (boxes[1].checked ? 'w' : '-') + (boxes[2].checked ? 'd' : '-');
        if (mode === '---') delete aeState.grants[g]; else aeState.grants[g] = mode;
        aeMarkDirty(); aeUpdateCount(); aeMtRender(); aeRenderPreview();
        return;
      }
      var srv = t.getAttribute('data-mt-srv'), tool = t.getAttribute('data-mt-tool');
      aeMt.data.servers.forEach(function (s) {
        if (srv && s.server === srv) s.hidden = !t.checked;
        if (tool) (s.tools || []).forEach(function (x) { if (s.server + '__' + x.name === tool) x.hidden = !t.checked; });
      });
      aeMtSave();
    });
  }

  function startAgentEditPage() {
    var name = aeQueryName();
    if (!name) {
      setText($('#agentTitle'), 'Missing ?name=…');
      return;
    }
    aeState.name = name;
    aeWireTabs();
    aeWire();
    aeWireManifestAndSpecialist();
    aeLoadChannels(name);
    aeMtWire();
    aeMtLoad(name);
    Promise.all([
      fetch('/api/agents/' + encodeURIComponent(name) + '?body=true', { cache: 'no-store' })
        .then(function (r) {
          if (!r.ok) throw new Error('agent not found in DB (HTTP ' + r.status + ')');
          return r.json();
        }),
      fetch('/api/tools', { cache: 'no-store' }).then(function (r) { return r.json(); }),
      fetch('/api/agents/' + encodeURIComponent(name) + '/grants', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : { grants: {} }; }),
      fetch('/api/integrations', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : { integrations: [] }; })
        .catch(function () { return { integrations: [] }; })
    ]).then(function (results) {
      var meta = results[0];
      aeState.meta = meta;
      aeRenderHeader(meta);
      aePopulateManifest(meta);
      aeState.tools = results[1].tools || [];
      aeState.grants = results[2].grants || {};
      var integrations = (results[3] && results[3].integrations) || [];
      aeState.integrations = integrations;
      aeState.integrationsByslug = {};
      integrations.forEach(function (i) { aeState.integrationsByslug[i.slug] = i; });
      // Render chips (auto-activates sources that already have grants)
      aeRenderSources();
      aeRenderTools();
      aeUpdateCount();
      aeState.toolsLoaded = true;
      aeMtRender();
      aeRenderPreview();
    }).catch(function (e) {
      window.snToast('Load failed: ' + (e.message || e));
    });
  }

  // Model <select>: the enabled chat rows of the catalog, one group per runtime,
  // value = the id the agent stores, <runtime>/<model>. The markup carries
  // only "none"; listing models there is how the panel drifted from the catalog.
  // A current value the catalog no longer offers is kept and marked, so opening
  // and saving an agent never changes its model behind the owner's back.
  function fillModelSelect(sel, wanted) {
    if (!sel) return;
    fetch('/api/models', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : { models: [] }; })
      .then(function (j) {
        var list = (j && j.models) || [];
        var none = sel.querySelector('option[value="none"]');
        sel.innerHTML = '';
        var groups = {};
        list.forEach(function (m) { (groups[m.provider] = groups[m.provider] || []).push(m); });
        Object.keys(groups).forEach(function (prov) {
          var og = document.createElement('optgroup'); og.label = prov;
          groups[prov].forEach(function (m) {
            var o = document.createElement('option'); o.value = m.id;
            o.textContent = m.label + ' - ' + m.vendor + (m.is_default ? ' (default)' : '');
            og.appendChild(o);
          });
          sel.appendChild(og);
        });
        if (none) sel.appendChild(none);
        selectModel(sel, wanted);
      }).catch(function () { selectModel(sel, wanted); });
  }
  function selectModel(sel, v) {
    if (!sel || !v) return;
    var has = [].slice.call(sel.options).some(function (o) { return o.value === v; });
    if (!has) { var o = document.createElement('option'); o.value = v; o.textContent = v + ' (current)'; sel.appendChild(o); }
    sel.value = v;
  }

  // ---------------------------------------------------- Manifest + Specialist (DB-backed)
  function aePopulateManifest(meta) {
    var t = $('#aeMfTitle');       if (t) t.value = meta.title || '';
    var r = $('#aeMfRole');        if (r) r.value = meta.role || '';
    var m = $('#aeMfModel');       if (m) fillModelSelect(m, meta.model || 'claude-cli/sonnet');
    var e = $('#aeMfEnabled');     if (e) e.checked = !!meta.enabled;
    var d = $('#aeMfDescription'); if (d) d.value = meta.description || '';
    var ou = $('#aeMfOsUser');   if (ou) ou.value = meta.os_user || '';
    var sc = $('#aeMfSchedule'); if (sc) sc.value = meta.schedule || '';
    var gr = $('#aeMfGroups');   if (gr) gr.value = (meta.os_groups || []).join(', ');
    var ar = $('#aeAcRead');     if (ar) ar.value = (meta.fs_read || []).join('\n');
    var aw = $('#aeAcWrite');    if (aw) aw.value = (meta.fs_write || []).join('\n');
    var af = $('#aeAcForbidFs'); if (af) af.value = (meta.forbidden_fs || []).join('\n');
    var ab = $('#aeAcForbidBehavior'); if (ab) ab.value = (meta.forbidden_behavior || []).join('\n');
    var v = $('#agentVersionBadge'); if (v) v.textContent = 'v' + (meta.version || '0.1.0');
  }
  function aeWireTabs() {
    var tabs = document.querySelectorAll('#aeTabs .ae-tab');
    if (!tabs.length) return;
    tabs.forEach(function (btn) {
      if (btn.dataset.wired) return;
      btn.dataset.wired = '1';
      btn.addEventListener('click', function () {
        aeActivateTab(btn.getAttribute('data-tab'));
      });
    });
    var initial = (tabs[0] && tabs[0].getAttribute('data-tab')) || 'instructions';
    var active = document.querySelector('#aeTabs .ae-tab.is-active');
    var asked = window.location.hash.slice(1);
    if (asked && document.querySelector('#aeTabs .ae-tab[data-tab="' + asked.replace(/[^a-z-]/g, '') + '"]')) {
      aeActivateTab(asked.replace(/[^a-z-]/g, ''));
      return;
    }
    aeActivateTab(active ? active.getAttribute('data-tab') : initial);
  }

  function aeActivateTab(key) {
    if (!key) return;
    var tabs = document.querySelectorAll('#aeTabs .ae-tab');
    tabs.forEach(function (b) {
      var on = b.getAttribute('data-tab') === key;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    var panels = document.querySelectorAll('[data-tab-panel]');
    panels.forEach(function (p) {
      var on = p.getAttribute('data-tab-panel') === key;
      p.hidden = !on;
    });
    // "Save configuration" writes Tools, Access and Settings only.
    // Instructions and Memory have their own Save, Channels saves on toggle,
    // Preview saves nothing; two Save buttons on one screen was guesswork.
    var footSave = $('#aeSave');
    if (footSave) footSave.hidden = ['tools', 'access', 'manifest'].indexOf(key) < 0;
    if (key === 'preview' && typeof aeRenderPreview === 'function') {
      aeRenderPreview();
    }
  }

  // ---------------------------------------------------- agent-edit: channels (LIVE sn_config.agent_channels)
  // Ported from the retired Studio configurator. Backend: GET /api/agents/:slug/channels,
  // PUT /api/agents/:slug/channels/:channelSlug {instance_ref, enabled} (writes immediately).
  var AE_CH_PER_AGENT = ['web-chat', 'voice', 'email', 'telegram'];
  function aeChNeedsRef(s) { return s === 'email' || s === 'telegram'; }
  // The suggested address must belong to THIS node. The panel is served at
  // panel.<node-domain>, so the node's domain is the hostname minus its first
  // label. Hardcoding one is not a cosmetic slip: whichever node the feature was
  // built on then appears as the example on every install that never heard of it.
  function aeNodeDomain() {
    var h = (window.location && window.location.hostname) || '';
    var dot = h.indexOf('.');
    return dot > 0 ? h.slice(dot + 1) : 'example.com';
  }
  function aeChRefPlaceholder(s) {
    if (s === 'email') return (aeState.name || 'agent') + '@' + aeNodeDomain();
    if (s === 'telegram') return '-100… or @route';
    return '';
  }

  function aePutChannel(channelSlug, instanceRef, enabled, statusEl) {
    statusEl.textContent = 'saving…';
    fetch('/api/agents/' + encodeURIComponent(aeState.name) + '/channels/' + encodeURIComponent(channelSlug), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ instance_ref: instanceRef || null, enabled: enabled })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        statusEl.textContent = r.ok ? 'saved' : ('failed: ' + snErr(r.status, j));
      });
    }).catch(function (e) { statusEl.textContent = 'error: ' + e.message; });
  }

  function aeChSubLabel(c) {
    if (c.channel_slug === 'email') return 'Email alias';
    if (c.channel_slug === 'telegram') return 'Telegram chat id or route';
    if (c.channel_slug === 'voice') return 'Deep link: voice page ?agent=' + (aeState.name || '');
    if (c.channel_slug === 'web-chat') return 'Deep link: chat ?agent=' + (aeState.name || '');
    return '';
  }

  function aeRenderChannelRow(c) {
    var row = document.createElement('div');
    row.className = 'cap-switch-row';

    var left = document.createElement('div');
    var name = document.createElement('div');
    name.textContent = c.channel_label || c.channel_slug;
    var sub = document.createElement('div');
    sub.className = 'muted'; sub.style.fontSize = '11px'; sub.style.marginTop = '2px';
    sub.textContent = aeChSubLabel(c);
    left.appendChild(name); left.appendChild(sub);
    row.appendChild(left);

    var right = document.createElement('div');
    right.style.display = 'flex'; right.style.alignItems = 'center'; right.style.gap = '12px';

    var refInput = null;
    if (aeChNeedsRef(c.channel_slug)) {
      refInput = document.createElement('input');
      refInput.type = 'text'; refInput.className = 'ae-ch-ref'; refInput.style.maxWidth = '220px';
      refInput.placeholder = aeChRefPlaceholder(c.channel_slug);
      refInput.value = c.instance_ref || '';
      right.appendChild(refInput);
    }

    var sw = document.createElement('button');
    sw.type = 'button';
    sw.className = 'switch' + (c.enabled ? ' on' : '');
    sw.setAttribute('role', 'switch');
    sw.setAttribute('aria-checked', c.enabled ? 'true' : 'false');
    var knob = document.createElement('span'); knob.className = 'knob';
    sw.appendChild(knob);
    right.appendChild(sw);

    var st = document.createElement('span');
    st.className = 'muted'; st.style.fontSize = '11px'; st.style.minWidth = '52px';
    st.textContent = c.bound ? 'saved' : '';
    right.appendChild(st);
    row.appendChild(right);

    function commit() {
      var on = sw.classList.contains('on');
      aePutChannel(c.channel_slug, refInput ? refInput.value.trim() : '', on, st);
    }
    sw.addEventListener('click', function () {
      sw.classList.toggle('on');
      sw.setAttribute('aria-checked', sw.classList.contains('on') ? 'true' : 'false');
      commit();
    });
    if (refInput) refInput.addEventListener('change', function () { if (sw.classList.contains('on')) commit(); });
    return row;
  }

  function aeLoadChannels(name) {
    var per = $('#aeChPerAgent'), shared = $('#aeChShared'), note = $('#aeChNote');
    if (!per) return;
    note.textContent = 'loading…';
    fetch('/api/agents/' + encodeURIComponent(name) + '/channels', { cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : { channels: [] }; })
      .then(function (j) {
        var chans = j.channels || [];
        per.innerHTML = ''; shared.innerHTML = '';
        var pc = 0, sc = 0;
        chans.forEach(function (c) {
          if (AE_CH_PER_AGENT.indexOf(c.channel_slug) >= 0 && c.status === 'available') {
            per.appendChild(aeRenderChannelRow(c)); pc++;
          } else if (c.status === 'available' && c.channel_enabled !== false) {
            // only what this node runs: a planned channel, or one whose code ships but
            // was never set up, is not something the agent has
            var chip = document.createElement('span');
            chip.className = 'chip';
            chip.textContent = (c.channel_label || c.channel_slug) + ' · live';
            shared.appendChild(chip); sc++;
          }
        });
        if (!pc) per.innerHTML = '<span class="muted">No per-agent channels available.</span>';
        var sw = $('#aeChSharedWrap'); if (sw) sw.hidden = !sc;
        note.textContent = pc + ' per-agent' + (sc ? ' · ' + sc + ' shared' : '');
      })
      .catch(function (e) { note.textContent = 'load error: ' + e.message; });
  }

  function aeLines(sel) {
    var el = $(sel); if (!el) return undefined;
    return el.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
  }
  // this Save carries Settings and Access only. It used to send
  // specialist_body from the Specialist tab, which was loaded with the COMPOSED
  // CLAUDE.md, so any save here wrote the whole built prompt back into the
  // agent's instructions and the next build doubled every layer. Name, model,
  // on-duty and the instructions are saved by the Instructions tab; os_user and
  // os_groups are not editable (the node identifies an agent by its os_user).
  // PUT is partial on core-api: a field left out keeps its stored value.
  function aeReadManifestForm() {
    return {
      role: ($('#aeMfRole') || {}).value || undefined,
      description: ($('#aeMfDescription') || {}).value || undefined,
      schedule: ($('#aeMfSchedule') || {}).value || undefined,
      fs_read: aeLines('#aeAcRead'),
      fs_write: aeLines('#aeAcWrite'),
      forbidden_fs: aeLines('#aeAcForbidFs'),
      forbidden_behavior: aeLines('#aeAcForbidBehavior')
    };
  }
  function aeWireManifestAndSpecialist() {
    var sel = ['#aeMfRole','#aeMfDescription','#aeMfSchedule',
               '#aeAcRead','#aeAcWrite','#aeAcForbidFs','#aeAcForbidBehavior'];
    sel.forEach(function (s) {
      var el = $(s); if (!el) return;
      el.addEventListener('change', function () { aeMarkDirty(); aeRenderPreview(); });
      if (el.tagName !== 'SELECT' && el.type !== 'checkbox') {
        el.addEventListener('input', function () { aeMarkDirty(); });
      }
    });
    var del = $('#aeDelete');
    if (del) del.addEventListener('click', function () {
      if (!aeState.name) return;
      snAsk('Delete agent "' + aeState.name + '"?', 'Its directory is archived.', 'Delete', true).then(function (ok) {
        if (!ok) return;
        fetch('/api/agents/' + encodeURIComponent(aeState.name), { method: 'DELETE' })
          .then(function (r) {
            if (r.status === 204) { window.location.href = '/agents/'; return; }
            return r.json().then(function (j) { throw new Error(snErr(r.status, j)); });
          })
          .catch(function (e) { window.snToast('Delete failed: ' + (e.message || e)); });
      });
    });
  }

  function mockReset(lsKey) {
    try { window.localStorage.removeItem(lsKey); } catch (e) {}
  }
  function nextId(rows) {
    var max = 0;
    rows.forEach(function (r) { if (r.id > max) max = r.id; });
    return max + 1;
  }

  // ---------------------------------------------------- policies (real backend)
  // Backed by sn_config.policies via core-api at /api/policies/* (Migration 010).
  // No localStorage; every write hits the server, then we refetch.
  // Form HTML still uses field names `key` / `body` -- mapped to API `slug` /
  // `body_md` on the way in/out, so the existing HTML form keeps working.
  var POL_API = '/api/policies';
  var polState = { rows: [], filter: '' };

  function polApiToRec(p) {
    // map API shape -> JS-internal shape used by render+modal
    return {
      id: p.id,
      key: p.slug,
      title: p.title,
      body: p.body_md,
      category: p.category,
      severity: p.severity,
      enabled: !!p.enabled,
      rationale: p.rationale || '',
      version: p.version
    };
  }

  function polRecToApi(rec) {
    return {
      slug: rec.key,
      title: rec.title,
      body_md: rec.body,
      category: rec.category,
      severity: rec.severity,
      enabled: rec.enabled,
      rationale: rec.rationale
    };
  }

  // core-api says why a write was refused in detail: a string, or for a
  // field that does not validate a list of {loc, msg}.
  function polDetail(j, status) {
    var d = j && j.detail;
    if (Array.isArray(d)) return d.map(function (x) { return ((x.loc || []).slice(-1)[0] || '') + ': ' + String(x.msg || '').replace(/^Value error, /, ''); }).join('; ');
    return d ? String(d) : ('HTTP ' + status);
  }

  function polRefetch() {
    return fetch(POL_API, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { policies: [] }; })
      .then(function (d) {
        polState.rows = (d.policies || []).map(polApiToRec);
        renderPolicies();
      })
      .catch(function () {
        polState.rows = [];
        renderPolicies();
      });
  }

  function renderPolicies() {
    var rows = polState.rows.slice();
    if (polState.filter) rows = rows.filter(function (r) { return r.category === polState.filter; });
    rows.sort(function (a, b) {
      if (a.category !== b.category) return a.category.localeCompare(b.category);
      return (a.key || '').localeCompare(b.key || '');
    });

    var all = polState.rows;
    setText($('[data-field="pol-total"]'), all.length);
    setText($('[data-field="pol-enabled"]'), all.filter(function (r) { return r.enabled; }).length);
    setText($('[data-field="pol-blocking"]'), all.filter(function (r) { return r.severity === 'block'; }).length);
    var cats = {}; all.forEach(function (r) { cats[r.category] = 1; });
    setText($('[data-field="pol-cats"]'), Object.keys(cats).length);

    var body = $('#polBody'); if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">No policies in this view.</td></tr>';
      return;
    }
    body.innerHTML = '';
    rows.forEach(function (r) {
      var sevDot = 'ok';
      if (r.severity === 'block') sevDot = 'fail';
      else if (r.severity === 'warn') sevDot = 'warn';
      var tr = document.createElement('tr');
      if (!r.enabled) tr.style.opacity = '0.45';
      tr.innerHTML =
        '<td class="col-name"><code>' + escapeHtml(r.key) + '</code></td>' +
        '<td class="col-kind"><span class="kind-tag kind-' + escapeAttr(r.category) + '">' + escapeHtml(r.category) + '</span></td>' +
        '<td class="col-title">' + escapeHtml(r.title || '-') + '</td>' +
        '<td class="col-status"><span class="health-dot ' + sevDot + '"></span> ' + escapeHtml(r.severity) + '</td>' +
        '<td class="col-status">' + (r.enabled ? '<span class="health-dot ok"></span> on' : '<span class="health-dot warn"></span> off') + '</td>' +
        '<td class="col-actions">' +
          '<button type="button" class="btn-row" data-act="edit" data-key="' + escapeAttr(r.key) + '">Edit</button> ' +
          '<button type="button" class="btn-row btn-danger" data-act="delete" data-key="' + escapeAttr(r.key) + '">Delete</button>' +
        '</td>';
      body.appendChild(tr);
    });
  }

  function openPolModal(existing) {
    var scrim = $('#polModalScrim');
    var form = $('#polForm');
    if (!scrim || !form) return;
    form.reset();
    $('#polFormError').textContent = '';
    if (existing) {
      $('#polModalTitle').textContent = 'Edit policy · ' + existing.key;
      $('#polKey').value = existing.key;
      $('#polKey').readOnly = true;
      form.elements['original_key'].value = existing.key;
      $('#polTitle').value = existing.title || '';
      $('#polCategory').value = existing.category || 'workflow';
      $('#polSeverity').value = existing.severity || 'warn';
      // #polBody is the table's tbody; the textarea had the same id, so the
      // body never showed and Save was blocked as empty.
      $('#polBodyText').value = existing.body || '';
      $('#polEnabled').checked = !!existing.enabled;
      $('#polRationale').value = existing.rationale || '';
    } else {
      $('#polModalTitle').textContent = 'New policy';
      $('#polKey').readOnly = false;
      form.elements['original_key'].value = '';
      $('#polEnabled').checked = true;
    }
    scrim.hidden = false;
    setTimeout(function () { (existing ? $('#polTitle') : $('#polKey')).focus(); }, 30);
  }

  function closePolModal() {
    var scrim = $('#polModalScrim'); if (scrim) scrim.hidden = true;
  }

  function submitPolForm(e) {
    e.preventDefault();
    var form = $('#polForm');
    var err = $('#polFormError'); err.textContent = '';
    var submitBtn = $('#polSubmit');
    var fd = new FormData(form);
    var origKey = String(form.elements['original_key'].value || '').trim();
    var rec = {
      key: String(fd.get('key') || '').trim(),
      title: String(fd.get('title') || '').trim(),
      category: String(fd.get('category') || 'workflow'),
      severity: String(fd.get('severity') || 'warn'),
      body: String(fd.get('body') || '').trim(),
      enabled: !!fd.get('enabled'),
      rationale: String(fd.get('rationale') || '').trim()
    };
    if (!rec.key || !rec.title || !rec.body || !rec.rationale) {
      err.textContent = 'Key, title, body and why it exists are required.'; return;
    }

    var isUpdate = !!origKey;
    var url = isUpdate ? (POL_API + '/' + encodeURIComponent(origKey)) : POL_API;
    // core-api edits a rule with PATCH; the key names it in the path.
    var method = isUpdate ? 'PATCH' : 'POST';
    var payload = polRecToApi(rec);
    if (isUpdate) delete payload.slug;

    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Saving…'; }

    fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Save'; }
      if (r.ok) {
        closePolModal();
        return polRefetch();
      }
      return r.json().then(function (j) {
        err.textContent = polDetail(j, r.status);
      }).catch(function () {
        err.textContent = 'HTTP ' + r.status;
      });
    }).catch(function (e) {
      if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = 'Save'; }
      err.textContent = 'Network error: ' + (e && e.message ? e.message : 'unknown');
    });
  }

  function deletePolicy(key) {
    if (!key) return;
    snAsk('Delete policy "' + key + '"?', 'Agents stop following it. This cannot be undone.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch(POL_API + '/' + encodeURIComponent(key), { method: 'DELETE' })
        .then(function (r) {
          if (r.ok) return polRefetch();
          return r.json().then(function (j) {
            snTell('Delete failed', (j && j.detail) ? j.detail : ('HTTP ' + r.status));
          });
        })
        .catch(function (e) {
          snTell('Network error', e && e.message ? e.message : 'unknown');
        });
    });
  }

  function startPoliciesPage() {
    var filt = $('#filterPolCat');
    if (filt) filt.addEventListener('change', function () { polState.filter = filt.value; renderPolicies(); });
    var newBtn = $('#polNewBtn'); if (newBtn) newBtn.addEventListener('click', function () { openPolModal(null); });
    // Reset-mock button removed in real-backend mode (HTML may still ship it; if so, hide).
    var resetBtn = $('#polResetBtn'); if (resetBtn) resetBtn.style.display = 'none';
    $('#polModalClose')  && $('#polModalClose').addEventListener('click', closePolModal);
    $('#polModalCancel') && $('#polModalCancel').addEventListener('click', closePolModal);
    $('#polModalScrim')  && $('#polModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'polModalScrim') closePolModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { var s = $('#polModalScrim'); if (s && !s.hidden) closePolModal(); }
    });
    var form = $('#polForm'); if (form) form.addEventListener('submit', submitPolForm);
    var tbody = $('#polBody');
    if (tbody) tbody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act');
      var key = btn.getAttribute('data-key');
      if (act === 'delete') return deletePolicy(key);
      if (act === 'edit') {
        var row = polState.rows.find(function (r) { return r.key === key; });
        if (row) openPolModal(row);
      }
    });
    polRefetch();
  }

  // ---------------------------------------------------- references (real backend)
  // Backed by sn_config.agent_references via core-api at /api/references/*
  // (Migration 014). No localStorage; every write hits the server, then refetch.
  var REF_API = '/api/references';
  var refState = { rows: [], filter: '' };

  function refApiToRec(p) {
    return {
      id: p.id, key: p.key, title: p.title, target_kind: p.target_kind,
      target_id: p.target_id, required: !!p.required
    };
  }
  function refRecToApi(rec) {
    return {
      key: rec.key, title: rec.title, target_kind: rec.target_kind,
      target_id: rec.target_id, required: !!rec.required
    };
  }
  function refRefetch() {
    return fetch(REF_API, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { references: [] }; })
      .then(function (d) { refState.rows = (d.references || []).map(refApiToRec); renderReferences(); })
      .catch(function () { refState.rows = []; renderReferences(); });
  }

  function renderReferences() {
    var rows = refState.rows.slice();
    if (refState.filter) rows = rows.filter(function (r) { return r.target_kind === refState.filter; });
    rows.sort(function (a, b) { return (a.key || '').localeCompare(b.key || ''); });

    var all = refState.rows;
    setText($('[data-field="ref-total"]'), all.length);
    setText($('[data-field="ref-required"]'), all.filter(function (r) { return r.required; }).length);
    setText($('[data-field="ref-doci"]'), all.filter(function (r) { return r.target_kind === 'doci'; }).length);
    setText($('[data-field="ref-other"]'), all.filter(function (r) { return r.target_kind !== 'doci'; }).length);

    var body = $('#refBody'); if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">No references in this view.</td></tr>';
      return;
    }
    body.innerHTML = '';
    rows.forEach(function (r) {
      var tgt = escapeHtml(r.target_id || '');
      if (r.target_kind === 'url') {
        tgt = '<a href="' + escapeAttr(r.target_id) + '" target="_blank" rel="noopener">' + tgt + '</a>';
      } else {
        tgt = '<code>' + tgt + '</code>';
      }
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="col-name"><code>' + escapeHtml(r.key) + '</code></td>' +
        '<td class="col-kind"><span class="kind-tag kind-' + escapeAttr(r.target_kind) + '">' + escapeHtml(r.target_kind) + '</span></td>' +
        '<td class="col-title">' + escapeHtml(r.title || '-') + '</td>' +
        '<td class="col-tags">' + tgt + '</td>' +
        '<td class="col-status">' + (r.required ? '<span class="health-dot warn"></span> required' : '<span class="muted">optional</span>') + '</td>' +
        '<td class="col-actions">' +
          '<button type="button" class="btn-row" data-act="edit" data-key="' + escapeAttr(r.key) + '">Edit</button> ' +
          '<button type="button" class="btn-row btn-danger" data-act="delete" data-key="' + escapeAttr(r.key) + '">Delete</button>' +
        '</td>';
      body.appendChild(tr);
    });
  }

  function openRefModal(existing) {
    var scrim = $('#refModalScrim');
    var form = $('#refForm');
    if (!scrim || !form) return;
    form.reset();
    $('#refFormError').textContent = '';
    if (existing) {
      $('#refModalTitle').textContent = 'Edit reference · ' + existing.key;
      $('#refKey').value = existing.key;
      $('#refKey').readOnly = true;
      form.elements['original_key'].value = existing.key;
      $('#refTitle').value = existing.title || '';
      $('#refKind').value = existing.target_kind || 'doci';
      $('#refTarget').value = existing.target_id || '';
      $('#refRequired').checked = !!existing.required;
    } else {
      $('#refModalTitle').textContent = 'New reference';
      $('#refKey').readOnly = false;
      form.elements['original_key'].value = '';
    }
    scrim.hidden = false;
    setTimeout(function () { (existing ? $('#refTitle') : $('#refKey')).focus(); }, 30);
  }

  function closeRefModal() {
    var scrim = $('#refModalScrim'); if (scrim) scrim.hidden = true;
  }

  function submitRefForm(e) {
    e.preventDefault();
    var form = $('#refForm');
    var err = $('#refFormError'); err.textContent = '';
    var fd = new FormData(form);
    var origKey = form.elements['original_key'].value;
    var rec = {
      key: String(fd.get('key') || '').trim(),
      title: String(fd.get('title') || '').trim(),
      target_kind: String(fd.get('target_kind') || 'doci'),
      target_id: String(fd.get('target_id') || '').trim(),
      required: !!fd.get('required')
    };
    if (!rec.key || !rec.title || !rec.target_id) { err.textContent = 'Key, title and target are required.'; return; }

    var url = origKey ? (REF_API + '/' + encodeURIComponent(origKey)) : REF_API;
    var method = origKey ? 'PUT' : 'POST';
    fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(refRecToApi(rec))
    }).then(function (r) {
      if (r.ok) { closeRefModal(); return refRefetch(); }
      return r.json().catch(function () { return {}; }).then(function (b) {
        err.textContent = snErr(r.status, b);
      });
    }).catch(function () { err.textContent = 'Network error.'; });
  }

  function deleteReference(key) {
    if (!key) return;
    snAsk('Delete reference "' + key + '"?', 'This cannot be undone.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch(REF_API + '/' + encodeURIComponent(key), { method: 'DELETE' })
        .then(function () { return refRefetch(); })
        .catch(function () { /* keep current view on network error */ });
    });
  }

  function startReferencesPage() {
    var filt = $('#filterRefKind');
    if (filt) filt.addEventListener('change', function () { refState.filter = filt.value; renderReferences(); });
    var newBtn = $('#refNewBtn'); if (newBtn) newBtn.addEventListener('click', function () { openRefModal(null); });
    var resetBtn = $('#refResetBtn');
    if (resetBtn) resetBtn.addEventListener('click', function () { refRefetch(); });
    $('#refModalClose')  && $('#refModalClose').addEventListener('click', closeRefModal);
    $('#refModalCancel') && $('#refModalCancel').addEventListener('click', closeRefModal);
    $('#refModalScrim')  && $('#refModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'refModalScrim') closeRefModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { var s = $('#refModalScrim'); if (s && !s.hidden) closeRefModal(); }
    });
    var form = $('#refForm'); if (form) form.addEventListener('submit', submitRefForm);
    var tbody = $('#refBody');
    if (tbody) tbody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act');
      var key = btn.getAttribute('data-key');
      if (act === 'delete') return deleteReference(key);
      if (act === 'edit') {
        var row = refState.rows.find(function (r) { return r.key === key; });
        if (row) openRefModal(row);
      }
    });
    refRefetch();
  }

  // ---------------------------------------------------- agent-new (create form)
  // Capability palette: friendly names over registry slugs (UI-only map, no DB field).
  // available iff built-in (claude-code) or its MCP server exists+enabled in the registry.
  var AN_CAPS = [
    { key: 'memory',   label: 'Memory',           grant: 'mesh',    mcp: true },
    { key: 'planning', label: 'Task planning',    grant: 'planner', mcp: true },
    { key: 'docs',     label: 'Documentation',    grant: 'doci',    mcp: true },
    { key: 'system',   label: 'System utilities', grant: 'bash',    builtin: true },
    { key: 'web',      label: 'Web',              grant: 'fetch',   builtin: true }
  ];

  function startAgentNewPage() {
    var form = $('#anForm'); if (!form) return;
    var err = $('#anError');
    var submit = $('#anSubmit');
    var capState = {};

    // slug auto-suggest from title; os_user placeholder follows slug
    var slugEl = $('#anSlug'), titleEl = $('#anTitle'), osEl = $('#anOsUser');
    var manualSlug = false;
    function syncOs() { if (osEl && slugEl) osEl.placeholder = 'agent-' + (slugEl.value || '<slug>'); }
    if (slugEl) slugEl.addEventListener('input', function () { manualSlug = !!slugEl.value; syncOs(); });
    if (titleEl && slugEl) titleEl.addEventListener('input', function () {
      if (!manualSlug) slugEl.value = titleEl.value.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 41);
      syncOs();
    });

    // Capability tiles (hoisted so a role template can pre-toggle them).
    var mcpEnabled = {};
    function renderCaps() {
      var box = $('#anCaps'); if (!box) return;
      box.innerHTML = '';
      AN_CAPS.forEach(function (c) {
        var available = c.builtin || !!mcpEnabled[c.grant];
        if (!available) capState[c.key] = false;
        var on = !!capState[c.key];
        var row = document.createElement('div');
        row.className = 'cap-switch-row' + (available ? '' : ' cap-soon');
        row.innerHTML = '<span class="cap-label">' + escapeHtml(c.label) +
          (available ? '' : ' <span class="cap-soon-tag">not in registry</span>') + '</span>' +
          '<button type="button" class="switch' + (on ? ' on' : '') + '" role="switch" aria-checked="' +
          (on ? 'true' : 'false') + '"' + (available ? '' : ' disabled') + '><span class="knob"></span></button>';
        if (available) {
          var sw = row.querySelector('.switch');
          sw.addEventListener('click', function () {
            capState[c.key] = !capState[c.key];
            sw.classList.toggle('on', capState[c.key]);
            sw.setAttribute('aria-checked', capState[c.key] ? 'true' : 'false');
          });
        }
        box.appendChild(row);
      });
    }
    fetch('/api/mcp/servers', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : { servers: [] }; })
      .catch(function () { return { servers: [] }; })
      .then(function (d) { (d.servers || []).forEach(function (s) { if (s.enabled) mcpEnabled[s.slug] = 1; }); renderCaps(); });

    // Role templates: click a card -> pre-fill the whole form. Sourced from the
    // specialists registry; sensible default capabilities per role.
    var CAP_DEFAULTS = {
      pm: ['memory', 'planning'],
      'marketing-director': ['memory', 'planning', 'docs'],
      marketer: ['memory', 'planning', 'web', 'docs'],
      'content-director': ['memory', 'planning', 'docs'],
      smm: ['memory', 'planning', 'web'],
      editor: ['memory', 'planning', 'docs'],
      scout: ['memory', 'planning', 'web', 'docs'],
      webmaster: ['memory', 'planning', 'web', 'system'],
      developer: ['memory', 'planning', 'system'],
      tester: ['memory', 'planning', 'system'],
      secretary: ['memory', 'planning', 'docs'],
      sysadmin: ['memory', 'planning', 'system']
    };
    function monogram(name) {
      var words = (name || '?').replace(/\(.*?\)/g, ' ').replace(/[^A-Za-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean);
      if (!words.length) return '?';
      if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
      return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
    }
    function applyTemplate(s) {
      if (titleEl) titleEl.value = s.name || s.slug;
      if (slugEl) { slugEl.value = s.slug; manualSlug = true; syncOs(); }
      var sp = $('#anSpecialist'); if (sp) sp.value = s.slug;
      selectModel($('#anModel'), s.archetype === 'head' ? 'claude-cli/opus' : 'claude-cli/sonnet');
      var mesh = $('#anMesh'); if (mesh) mesh.value = s.slug;
      capState = {};
      (CAP_DEFAULTS[s.slug] || ['memory', 'planning']).forEach(function (k) { capState[k] = true; });
      renderCaps();
    }
    fillModelSelect($('#anModel'), 'claude-cli/sonnet');
    fetch('/api/specialists', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : { specialists: [] }; })
      .then(function (d) {
        var specs = d.specialists || [];
        var sel = $('#anSpecialist');
        if (sel) specs.forEach(function (s) { var o = document.createElement('option'); o.value = s.slug; o.textContent = s.name + ' (' + s.slug + ')'; sel.appendChild(o); });
        var cards = $('#anRoleCards');
        if (!cards) return;
        if (!specs.length) { cards.innerHTML = '<span class="muted">No roles in the registry · add a specialist on /specialists/</span>'; return; }
        cards.innerHTML = '';
        specs.forEach(function (s) {
          var card = document.createElement('button');
          card.type = 'button'; card.className = 'role-card';
          card.innerHTML = '<span class="role-avatar">' + escapeHtml(monogram(s.name)) + '</span>' +
                           '<span class="role-meta"><span class="role-name">' + escapeHtml(s.name) + '</span>' +
                           '<span class="role-sub">' + escapeHtml(s.slug) + '</span></span>';
          card.addEventListener('click', function () {
            cards.querySelectorAll('.role-card').forEach(function (x) { x.classList.remove('sel'); });
            card.classList.add('sel');
            applyTemplate(s);
          });
          cards.appendChild(card);
        });
      }).catch(function () {});

    // Department dropdown from /api/departments
    fetch('/api/departments', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : { departments: [] }; })
      .then(function (d) {
        var sel = $('#anDept'); if (!sel) return;
        (d.departments || []).forEach(function (dep) {
          var o = document.createElement('option'); o.value = dep.id; o.textContent = dep.name; sel.appendChild(o);
        });
      }).catch(function () {});

    function lines(id) { var el = $('#' + id); if (!el) return []; return el.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean); }
    function csv(id) { var el = $('#' + id); if (!el) return []; return el.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean); }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      err.textContent = ''; submit.disabled = true;
      var fd = new FormData(form);
      var slug = String(fd.get('slug') || '').trim();
      var title = String(fd.get('title') || '').trim();
      if (!slug || !title) { err.textContent = 'Title and slug are required.'; submit.disabled = false; return; }
      var specSlug = String(fd.get('specialist_slug') || '').trim();
      var body = String(fd.get('specialist_body') || '');
      var deptRaw = String(fd.get('department_id') || '').trim();
      var payload = {
        slug: slug, title: title,
        description: String(fd.get('description') || '').trim() || null,
        model: String(fd.get('model') || 'claude-cli/sonnet'),
        mesh_workspace: String(fd.get('mesh_workspace') || '').trim() || null,
        os_user: String(fd.get('os_user') || '').trim() || null,
        schedule: String(fd.get('schedule') || '').trim() || null,
        os_groups: csv('anGroups'),
        fs_read: lines('anRead'), fs_write: lines('anWrite'),
        forbidden_fs: lines('anForbidFs'), forbidden_behavior: lines('anForbidBeh')
      };
      if (deptRaw) payload.department_id = parseInt(deptRaw, 10);
      if (specSlug) payload.specialist_slug = specSlug;
      else if (body.trim()) payload.specialist_body = body;

      var grants = {};
      AN_CAPS.forEach(function (c) { if (capState[c.key]) grants[c.grant] = 'rw-'; });

      fetch('/api/agents', {
        method: 'POST', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (j) {
          throw new Error(snErr(r.status, j));
        });
        return r.json();
      }).then(function (a) {
        if (!Object.keys(grants).length) return a;
        return fetch('/api/agents/' + encodeURIComponent(a.slug) + '/grants', {
          method: 'PUT', cache: 'no-store', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ grants: grants })
        }).then(function () { return a; });
      }).then(function (a) {
        window.location.href = '/agents/edit/?name=' + encodeURIComponent(a.slug);
      }).catch(function (e2) {
        err.textContent = 'Create failed: ' + (e2.message || e2); submit.disabled = false;
      });
    });
  }

  // ---------------------------------------------------- mcp servers (real backend)
  // Backed by sn_config.mcp_servers via core-api at /api/mcp/servers/*.
  // POST is upsert-by-slug, so create and edit both POST; no localStorage.
  var MCP_API = '/api/mcp/servers';
  var mcpState = { rows: [], filter: '' };

  function mcpRefetch() {
    return fetch(MCP_API, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { servers: [] }; })
      .then(function (d) { mcpState.rows = d.servers || []; renderMcp(); })
      .catch(function () { mcpState.rows = []; renderMcp(); });
  }

  function renderMcp() {
    var rows = mcpState.rows.slice();
    if (mcpState.filter) rows = rows.filter(function (r) { return r.transport === mcpState.filter; });
    rows.sort(function (a, b) { return (a.slug || '').localeCompare(b.slug || ''); });

    var all = mcpState.rows;
    setText($('[data-field="mcp-total"]'), all.length);
    setText($('[data-field="mcp-enabled"]'), all.filter(function (r) { return r.enabled; }).length);
    // Live status (core-api _live_check): the stored status only moves on a
    // discovery post, which nothing on the node makes.
    setText($('[data-field="mcp-available"]'), all.filter(function (r) { return r.live_status === 'available'; }).length);
    setText($('[data-field="mcp-broken"]'), all.filter(function (r) { return r.live_status === 'broken'; }).length);

    var body = $('#mcpBody'); if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="7">No MCP servers in this view.</td></tr>';
      return;
    }
    body.innerHTML = '';
    rows.forEach(function (r) {
      var ls = r.live_status || 'unknown';
      var statusCls = ls === 'available' ? 'ok' : (ls === 'broken' ? 'fail' : 'warn');
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="col-name"><code>' + escapeHtml(r.slug) + '</code></td>' +
        '<td class="col-title">' + escapeHtml(r.name || '-') + '</td>' +
        '<td class="col-kind"><span class="kind-tag kind-' + escapeAttr(r.transport) + '">' + escapeHtml(r.transport) + '</span></td>' +
        '<td class="col-tags">' + (r.auth_secret_ref ? '<code>' + escapeHtml(r.auth_secret_ref) + '</code>' : '<span class="muted">-</span>') + '</td>' +
        '<td class="col-status" title="' + escapeAttr(r.live_reason || '') + '"><span class="health-dot ' + statusCls + '"></span> ' + escapeHtml(ls) + '</td>' +
        '<td class="col-status">' + (r.enabled ? 'enabled' : '<span class="muted">disabled</span>') + '</td>' +
        '<td class="col-actions">' +
          '<button type="button" class="btn-row" data-act="edit" data-slug="' + escapeAttr(r.slug) + '">Edit</button> ' +
          '<button type="button" class="btn-row btn-danger" data-act="delete" data-slug="' + escapeAttr(r.slug) + '">Delete</button>' +
        '</td>';
      body.appendChild(tr);
    });
  }

  function openMcpModal(existing) {
    var scrim = $('#mcpModalScrim'); var form = $('#mcpForm');
    if (!scrim || !form) return;
    form.reset();
    $('#mcpFormError').textContent = '';
    if (existing) {
      $('#mcpModalTitle').textContent = 'Edit server · ' + existing.slug;
      $('#mcpSlug').value = existing.slug;
      $('#mcpSlug').readOnly = true;
      form.elements['original_slug'].value = existing.slug;
      $('#mcpName').value = existing.name || '';
      $('#mcpTransport').value = existing.transport || 'stdio';
      $('#mcpConfig').value = JSON.stringify(existing.config || {}, null, 2);
      $('#mcpSecretRef').value = existing.auth_secret_ref || '';
      $('#mcpEnabled').checked = !!existing.enabled;
      $('#mcpAutoinstall').checked = !!existing.autoinstall;
    } else {
      $('#mcpModalTitle').textContent = 'New MCP server';
      $('#mcpSlug').readOnly = false;
      form.elements['original_slug'].value = '';
      $('#mcpConfig').value = '{}';
      $('#mcpEnabled').checked = true;
    }
    scrim.hidden = false;
    setTimeout(function () { (existing ? $('#mcpName') : $('#mcpSlug')).focus(); }, 30);
  }

  function closeMcpModal() { var s = $('#mcpModalScrim'); if (s) s.hidden = true; }

  function submitMcpForm(e) {
    e.preventDefault();
    var form = $('#mcpForm');
    var err = $('#mcpFormError'); err.textContent = '';
    var fd = new FormData(form);
    var cfgRaw = String(fd.get('config') || '').trim() || '{}';
    var cfg;
    try { cfg = JSON.parse(cfgRaw); }
    catch (ex) { err.textContent = 'Config is not valid JSON.'; return; }
    if (typeof cfg !== 'object' || Array.isArray(cfg) || cfg === null) {
      err.textContent = 'Config must be a JSON object.'; return;
    }
    var ref = String(fd.get('auth_secret_ref') || '').trim();
    var payload = {
      slug: String(fd.get('slug') || '').trim(),
      name: String(fd.get('name') || '').trim(),
      transport: String(fd.get('transport') || 'stdio'),
      config: cfg,
      auth_secret_ref: ref || null,
      enabled: !!fd.get('enabled'),
      autoinstall: !!fd.get('autoinstall')
    };
    if (!payload.slug || !payload.name) { err.textContent = 'Slug and name are required.'; return; }
    // POST is upsert-by-slug; used for both create and edit.
    fetch(MCP_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (r.ok) { closeMcpModal(); return mcpRefetch(); }
      return r.json().catch(function () { return {}; }).then(function (b) {
        err.textContent = snErr(r.status, b);
      });
    }).catch(function () { err.textContent = 'Network error.'; });
  }

  function deleteMcpServer(slug) {
    if (!slug) return;
    snAsk('Delete MCP server "' + slug + '"?', 'Its tools stay, without a server.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch(MCP_API + '/' + encodeURIComponent(slug), { method: 'DELETE' })
        .then(function () { return mcpRefetch(); })
        .catch(function () { /* keep current view */ });
    });
  }

  function startMcpPage() {
    var filt = $('#filterMcpTransport');
    if (filt) filt.addEventListener('change', function () { mcpState.filter = filt.value; renderMcp(); });
    var newBtn = $('#mcpNewBtn'); if (newBtn) newBtn.addEventListener('click', function () { openMcpModal(null); });
    var resetBtn = $('#mcpResetBtn'); if (resetBtn) resetBtn.addEventListener('click', function () { mcpRefetch(); });
    $('#mcpModalClose')  && $('#mcpModalClose').addEventListener('click', closeMcpModal);
    $('#mcpModalCancel') && $('#mcpModalCancel').addEventListener('click', closeMcpModal);
    $('#mcpModalScrim')  && $('#mcpModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'mcpModalScrim') closeMcpModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { var s = $('#mcpModalScrim'); if (s && !s.hidden) closeMcpModal(); }
    });
    var form = $('#mcpForm'); if (form) form.addEventListener('submit', submitMcpForm);
    var tbody = $('#mcpBody');
    if (tbody) tbody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act'); var slug = btn.getAttribute('data-slug');
      if (act === 'delete') return deleteMcpServer(slug);
      if (act === 'edit') {
        var row = mcpState.rows.find(function (r) { return r.slug === slug; });
        if (row) openMcpModal(row);
      }
    });
    mcpRefetch();
  }

  // ---------------------------------------------------- specialists (real backend)
  // Backed by sn_config.specialists via core-api at /api/specialists/*.
  var SPEC_API = '/api/specialists';
  var specState = { rows: [], filter: '' };

  function specRefetch() {
    return fetch(SPEC_API, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : { specialists: [] }; })
      .then(function (d) { specState.rows = d.specialists || []; renderSpecialists(); })
      .catch(function () { specState.rows = []; renderSpecialists(); });
  }

  function renderSpecialists() {
    var rows = specState.rows.slice();
    if (specState.filter) rows = rows.filter(function (r) { return r.archetype === specState.filter; });
    rows.sort(function (a, b) { return (a.slug || '').localeCompare(b.slug || ''); });

    var all = specState.rows;
    setText($('[data-field="spec-total"]'), all.length);
    setText($('[data-field="spec-specialist"]'), all.filter(function (r) { return r.archetype === 'specialist'; }).length);
    setText($('[data-field="spec-reviewer"]'), all.filter(function (r) { return r.archetype === 'reviewer'; }).length);
    setText($('[data-field="spec-shared"]'), all.filter(function (r) { return r.archetype === 'shared'; }).length);

    var body = $('#specBody'); if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">No specialists in this view.</td></tr>';
      return;
    }
    body.innerHTML = '';
    rows.forEach(function (r) {
      var arch = r.archetype || '';
      var archCell = arch ? '<span class="kind-tag kind-' + escapeAttr(arch) + '">' + escapeHtml(arch) + '</span>' : '<span class="muted">-</span>';
      var size = (r.body_md || '').length;
      var tr = document.createElement('tr');
      tr.innerHTML =
        '<td class="col-name"><code>' + escapeHtml(r.slug) + '</code></td>' +
        '<td class="col-title">' + escapeHtml(r.name || '-') + '</td>' +
        '<td class="col-kind">' + archCell + '</td>' +
        '<td class="col-status"><code>' + escapeHtml(r.version || '0.1.0') + '</code></td>' +
        '<td class="col-tags"><span class="muted">' + size + ' chars</span></td>' +
        '<td class="col-actions">' +
          '<button type="button" class="btn-row" data-act="edit" data-slug="' + escapeAttr(r.slug) + '">Edit</button> ' +
          '<button type="button" class="btn-row btn-danger" data-act="delete" data-slug="' + escapeAttr(r.slug) + '">Delete</button>' +
        '</td>';
      body.appendChild(tr);
    });
  }

  function openSpecModal(existing) {
    var scrim = $('#specModalScrim'); var form = $('#specForm');
    if (!scrim || !form) return;
    form.reset();
    $('#specFormError').textContent = '';
    var bumpRow = $('#specBumpRow');
    if (existing) {
      $('#specModalTitle').textContent = 'Edit specialist · ' + existing.slug + ' (v' + (existing.version || '0.1.0') + ')';
      $('#specSlug').value = existing.slug;
      $('#specSlug').readOnly = true;
      form.elements['original_slug'].value = existing.slug;
      $('#specName').value = existing.name || '';
      $('#specArch').value = existing.archetype || '';
      // #specBody is the table's tbody; the textarea had the same id, so the
      // body went nowhere and a save posted an empty body.
      $('#specBodyMd').value = existing.body_md || '';
      if (bumpRow) bumpRow.hidden = false;
    } else {
      $('#specModalTitle').textContent = 'New specialist';
      $('#specSlug').readOnly = false;
      form.elements['original_slug'].value = '';
      if (bumpRow) bumpRow.hidden = true;
    }
    scrim.hidden = false;
    setTimeout(function () { (existing ? $('#specName') : $('#specSlug')).focus(); }, 30);
  }

  function closeSpecModal() { var s = $('#specModalScrim'); if (s) s.hidden = true; }

  function submitSpecForm(e) {
    e.preventDefault();
    var form = $('#specForm');
    var err = $('#specFormError'); err.textContent = '';
    var fd = new FormData(form);
    var origSlug = form.elements['original_slug'].value;
    var arch = String(fd.get('archetype') || '').trim();
    var name = String(fd.get('name') || '').trim();
    var bodyMd = String(fd.get('body_md') || '');
    if (!name) { err.textContent = 'Name is required.'; return; }

    var url, method, payload;
    if (origSlug) {
      url = SPEC_API + '/' + encodeURIComponent(origSlug);
      method = 'PUT';
      payload = { name: name, body_md: bodyMd, archetype: arch || null };
      var bump = String(fd.get('version_bump') || '');
      if (bump) payload.version_bump = bump;
    } else {
      var slug = String(fd.get('slug') || '').trim();
      if (!slug) { err.textContent = 'Slug is required.'; return; }
      url = SPEC_API;
      method = 'POST';
      payload = { slug: slug, name: name, body_md: bodyMd, archetype: arch || null };
    }
    fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (r.ok) { closeSpecModal(); return specRefetch(); }
      return r.json().catch(function () { return {}; }).then(function (b) {
        err.textContent = snErr(r.status, b);
      });
    }).catch(function () { err.textContent = 'Network error.'; });
  }

  function deleteSpecialist(slug) {
    if (!slug) return;
    snAsk('Delete specialist "' + slug + '"?', 'Agents still pointing at it keep their own copy.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch(SPEC_API + '/' + encodeURIComponent(slug), { method: 'DELETE' })
        .then(function () { return specRefetch(); })
        .catch(function () { /* keep current view */ });
    });
  }

  function startSpecialistsPage() {
    var filt = $('#filterSpecArch');
    if (filt) filt.addEventListener('change', function () { specState.filter = filt.value; renderSpecialists(); });
    var newBtn = $('#specNewBtn'); if (newBtn) newBtn.addEventListener('click', function () { openSpecModal(null); });
    var resetBtn = $('#specResetBtn'); if (resetBtn) resetBtn.addEventListener('click', function () { specRefetch(); });
    $('#specModalClose')  && $('#specModalClose').addEventListener('click', closeSpecModal);
    $('#specModalCancel') && $('#specModalCancel').addEventListener('click', closeSpecModal);
    $('#specModalScrim')  && $('#specModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'specModalScrim') closeSpecModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { var s = $('#specModalScrim'); if (s && !s.hidden) closeSpecModal(); }
    });
    var form = $('#specForm'); if (form) form.addEventListener('submit', submitSpecForm);
    var tbody = $('#specBody');
    if (tbody) tbody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act'); var slug = btn.getAttribute('data-slug');
      if (act === 'delete') return deleteSpecialist(slug);
      if (act === 'edit') {
        var row = specState.rows.find(function (r) { return r.slug === slug; });
        if (row) openSpecModal(row);
      }
    });
    specRefetch();
  }

  // ---------------------------------------------------- secrets (the one store)
  // Backed by sn_config.secrets via core-api at /api/secrets/*: every credential
  // of the node, the gateway's provider keys included. The listing carries refs
  // and their metadata; values are NEVER returned to the panel. Requests are
  // the reads the store refused to agents that still lack the path.
  var SEC_API = '/api/secrets';
  var secState = { items: [], requests: [], available: true, error: '' };

  function secRefetch() {
    return Promise.all([
      fetch(SEC_API + '/refs', { cache: 'no-store' })
        .then(function (r) {
          if (!r.ok) {
            secState.available = false;
            secState.error = r.status === 403
              ? 'Secrets are shown to a signed-in owner only.'
              : 'The store did not answer (HTTP ' + r.status + ').';
            return null;
          }
          secState.available = true; secState.error = '';
          return r.json();
        })
        .catch(function () { secState.available = false; secState.error = 'Network error.'; return null; }),
      fetch(SEC_API + '/requests', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; })
    ]).then(function (res) {
      secState.items = (res[0] && res[0].items) || [];
      secState.requests = (res[1] && res[1].requests) || [];
      renderSecrets();
    });
  }

  function secWho(actor) {
    if (!actor) return '';
    return escapeHtml(String(actor).replace(/^user:/, '').replace(/^os:/, ''));
  }

  function renderSecrets() {
    var banner = $('#secUnavailable');
    if (banner) banner.hidden = secState.available;
    var msg = $('#secUnavailableMsg');
    if (msg && secState.error) msg.textContent = secState.error;

    var items = secState.items.slice().sort(function (a, b) { return a.ref.localeCompare(b.ref); });
    var namespaces = {};
    items.forEach(function (i) { namespaces[i.namespace] = 1; });
    setText($('[data-field="sec-total"]'), items.length);
    setText($('[data-field="sec-namespaces"]'), Object.keys(namespaces).length);
    setText($('[data-field="sec-requests"]'), secState.requests.length);

    var body = $('#secBody'); if (!body) return;
    if (!secState.available) {
      body.innerHTML = '<tr class="empty-row"><td colspan="7">Store unavailable · see banner above.</td></tr>';
    } else if (items.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="7">No secrets stored.</td></tr>';
    } else {
      body.innerHTML = '';
      items.forEach(function (i) {
        var used = (i.used_by || []).length
          ? i.used_by.map(escapeHtml).join('<br>')
          : '<span class="muted">not wired</span>';
        var readers = (i.readers || []).length
          ? i.readers.map(escapeHtml).join(', ')
          : '<span class="muted">no agent</span>';
        var set = i.updated_at
          ? escapeHtml(relWhen(i.updated_at)) + (i.updated_by ? ' <span class="muted">by ' + secWho(i.updated_by) + '</span>' : '')
          : '<span class="muted">-</span>';
        var read = i.last_read_at
          ? escapeHtml(relWhen(i.last_read_at)) + (i.last_read_by ? ' <span class="muted">by ' + secWho(i.last_read_by) + '</span>' : '')
          : '<span class="muted">never</span>';
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td class="col-name"><code title="' + escapeAttr(i.note || '') + '">' + escapeHtml(i.ref) + '</code></td>' +
          '<td class="col-kind"><span class="kind-tag">' + escapeHtml(i.namespace) + '</span></td>' +
          '<td style="font-size:12px">' + used + '</td>' +
          '<td style="font-size:12px">' + readers + '</td>' +
          '<td style="font-size:12px">' + set + '</td>' +
          '<td style="font-size:12px">' + read + '</td>' +
          '<td class="col-actions">' +
            '<button type="button" class="btn-row" data-act="replace" data-ref="' + escapeAttr(i.ref) + '">Replace</button> ' +
            '<button type="button" class="btn-row btn-danger" data-act="delete" data-ref="' + escapeAttr(i.ref) + '">Delete</button>' +
          '</td>';
        body.appendChild(tr);
      });
    }

    var reqBody = $('#secReqBody'); if (!reqBody) return;
    if (!secState.requests.length) {
      reqBody.innerHTML = '<tr class="empty-row"><td colspan="6">No refused reads in the last 30 days.</td></tr>';
      return;
    }
    reqBody.innerHTML = '';
    secState.requests.forEach(function (q) {
      var tr = document.createElement('tr');
      var action = q.exists
        ? '<button type="button" class="btn-row" data-act="grant" data-agent="' + escapeAttr(q.agent) + '" data-ref="' + escapeAttr(q.ref) + '">Grant</button>'
        : '<button type="button" class="btn-row" data-act="create" data-ref="' + escapeAttr(q.ref) + '" title="This path does not exist yet">Create</button>';
      tr.innerHTML =
        '<td class="col-name"><code>' + escapeHtml(q.agent) + '</code></td>' +
        '<td class="col-name"><code>' + escapeHtml(q.ref) + '</code>' + (q.exists ? '' : ' <span class="muted">(no such path)</span>') + '</td>' +
        '<td>' + escapeHtml(String(q.count)) + '</td>' +
        '<td style="font-size:12px">' + (q.last_at ? escapeHtml(relWhen(q.last_at)) : '-') + '</td>' +
        '<td style="font-size:12px" class="muted">' + escapeHtml(q.reason || '') + '</td>' +
        '<td class="col-actions">' + action + '</td>';
      reqBody.appendChild(tr);
    });
  }

  function openSecModal(existingRef, prefillRef) {
    var scrim = $('#secModalScrim'); var form = $('#secForm');
    if (!scrim || !form) return;
    form.reset();
    $('#secFormError').textContent = '';
    var revealEl = $('#secReveal'); if (revealEl) revealEl.checked = false;
    $('#secValue').type = 'password';
    if (existingRef) {
      $('#secModalTitle').textContent = 'Replace value · ' + existingRef;
      $('#secRef').value = existingRef;
      $('#secRef').readOnly = true;
    } else {
      $('#secModalTitle').textContent = 'New secret';
      $('#secRef').readOnly = false;
      if (prefillRef) $('#secRef').value = prefillRef;
    }
    scrim.hidden = false;
    setTimeout(function () { ((existingRef || prefillRef) ? $('#secValue') : $('#secRef')).focus(); }, 30);
  }

  function closeSecModal() { var s = $('#secModalScrim'); if (s) s.hidden = true; }

  function submitSecForm(e) {
    e.preventDefault();
    var form = $('#secForm');
    var err = $('#secFormError'); err.textContent = '';
    var fd = new FormData(form);
    var payload = {
      ref: String(fd.get('ref') || '').trim(),
      value: String(fd.get('value') || '')
    };
    if (!payload.ref || !payload.value) { err.textContent = 'Path and value are required.'; return; }
    fetch(SEC_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (r.ok) { closeSecModal(); return secRefetch(); }
      return r.json().then(function (b) {
        var d = b && b.detail;
        // A path that does not match core-api's pattern comes back as a
        // validation list; say what a path may hold instead of the regex.
        if (Array.isArray(d)) d = d.some(function (x) { return (x.loc || []).indexOf('ref') >= 0; })
          ? 'The path may hold letters, digits, _ and -, in up to six parts joined by / or . (e.g. gateway/gemini-key).'
          : d.map(function (x) { return x.msg; }).join('; ');
        err.textContent = d ? String(d) : ('Save failed (' + r.status + ').');
      });
    }).catch(function () { err.textContent = 'Network error.'; });
  }

  function deleteSecret(ref) {
    if (!ref) return;
    snAsk('Delete secret "' + ref + '"?', 'Whatever uses it can no longer read it. The last value stays readable as a snapshot for 30 days.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch(SEC_API + '/' + ref, { method: 'DELETE' })
        .then(function () { return secRefetch(); })
        .catch(function () { /* keep current view */ });
    });
  }

  function grantSecret(agent, ref) {
    if (!agent || !ref) return;
    snAsk('Let ' + agent + ' read "' + ref + '"?', 'The agent gets read access to secrets and to this path.', 'Allow', false).then(function (ok) {
      if (!ok) return;
      fetch('/api/agents/' + encodeURIComponent(agent) + '/grants?merge=true', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ grants: { secrets: 'r--' }, secret_refs: [ref] })
      }).then(function (r) {
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (b) {
            snTell('Grant failed', snErr(r.status, b));
          });
        }
      }).catch(function (e) { snTell('Grant failed', e); })
        .finally(function () { secRefetch(); });
    });
  }

  function startSecretsPage() {
    var newBtn = $('#secNewBtn'); if (newBtn) newBtn.addEventListener('click', function () { openSecModal(null); });
    var resetBtn = $('#secResetBtn'); if (resetBtn) resetBtn.addEventListener('click', function () { secRefetch(); });
    var revealEl = $('#secReveal');
    if (revealEl) revealEl.addEventListener('change', function () { $('#secValue').type = revealEl.checked ? 'text' : 'password'; });
    $('#secModalClose')  && $('#secModalClose').addEventListener('click', closeSecModal);
    $('#secModalCancel') && $('#secModalCancel').addEventListener('click', closeSecModal);
    $('#secModalScrim')  && $('#secModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'secModalScrim') closeSecModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { var s = $('#secModalScrim'); if (s && !s.hidden) closeSecModal(); }
    });
    var form = $('#secForm'); if (form) form.addEventListener('submit', submitSecForm);
    var tbody = $('#secBody');
    if (tbody) tbody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act'); var ref = btn.getAttribute('data-ref');
      if (act === 'delete') return deleteSecret(ref);
      if (act === 'replace') return openSecModal(ref);
    });
    var reqBody = $('#secReqBody');
    if (reqBody) reqBody.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act'); var ref = btn.getAttribute('data-ref');
      if (act === 'grant') return grantSecret(btn.getAttribute('data-agent'), ref);
      if (act === 'create') return openSecModal(null, ref);
    });
    secRefetch();
  }

  // ---------------------------------------------------- events: activity chart
  // Two stacked-column panels on one time axis, never one chart with two
  // y-scales: node activity by event family on top, problems underneath. The
  // counts come from /api/events/histogram because the table below only holds
  // the newest rows. Family colours are fixed categorical slots validated for
  // stacks on both panel surfaces; problems wear the status colours and always
  // carry their names in the legend and tooltip.
  var EV_FAMILIES = [
    { key: 'tool',    label: 'tools',    light: '#2a78d6', dark: '#3987e5' },
    { key: 'agent',   label: 'agents',   light: '#eb6834', dark: '#d95926' },
    { key: 'session', label: 'sessions', light: '#1baf7a', dark: '#199e70' },
    { key: 'admin',   label: 'admin',    light: '#eda100', dark: '#c98500' },
    { key: 'other',   label: 'other',    light: '#898781', dark: '#898781' }
  ];
  var EV_PROBLEMS = [
    { key: 'secret_denied',   label: 'secret access denied', color: '#d03b3b' },
    { key: 'tool_failed',     label: 'tool failed',          color: '#ec835a' },
    { key: 'message_blocked', label: 'message blocked',      color: '#fab219' }
  ];
  var ev = { chart: null, hours: 24, data: null };
  try { var _eh = Number(localStorage.getItem('sn-events-hours')); if ([1, 6, 24, 168].indexOf(_eh) >= 0) ev.hours = _eh; } catch (e) {}

  function evOption(d) {
    var dark = tlDark();
    var ink = tlToken('--sn-ink', dark ? '#f2f2f0' : '#0a0a0a');
    var inkMute = tlToken('--sn-ink-mute', '#76767a');
    var rule = tlToken('--sn-rule', dark ? 'rgba(255,255,255,.075)' : '#e7e6e1');
    var surface = tlToken('--sn-surface', dark ? '#131419' : '#ffffff');
    var mono = tlToken('--sn-mono', 'JetBrains Mono, monospace');
    var width = d.bucket_seconds * 1000;
    var from = d.buckets.length ? d.buckets[0].t * 1000 : Date.now() - ev.hours * 3600000;
    var to = d.buckets.length ? d.buckets[d.buckets.length - 1].t * 1000 + width : Date.now();
    var axis = function (gridIndex, showLabels) {
      return {
        type: 'time', gridIndex: gridIndex, min: from, max: to,
        axisLine: { lineStyle: { color: rule } }, axisTick: { show: false },
        axisLabel: { show: showLabels, color: inkMute, fontFamily: mono, fontSize: 10.5, hideOverlap: true,
                     formatter: { day: '{MMM} {d}', hour: '{HH}:{mm}', minute: '{HH}:{mm}' } },
        splitLine: { show: false }
      };
    };
    var yAxis = function (gridIndex) {
      return {
        type: 'value', gridIndex: gridIndex, minInterval: 1, splitNumber: gridIndex ? 2 : 4,
        axisLabel: { color: inkMute, fontFamily: mono, fontSize: 10 },
        splitLine: { lineStyle: { color: rule, width: 1 } }
      };
    };
    // Columns sit in the middle of their bucket and use most of it: a hairline
    // of surface between touching columns and between stacked segments.
    var bar = function (name, color, key, axisIndex, stack) {
      return {
        type: 'bar', name: name, stack: stack, xAxisIndex: axisIndex, yAxisIndex: axisIndex,
        barMaxWidth: 24, barCategoryGap: '20%',
        itemStyle: { color: color, borderColor: surface, borderWidth: 1 },
        emphasis: { focus: 'none' },
        // An empty bucket draws nothing, not a hairline on the baseline.
        data: d.buckets.map(function (b) { return [b.t * 1000 + width / 2, b[key] || '-']; })
      };
    };
    var series = EV_FAMILIES.map(function (f) { return bar(f.label, dark ? f.dark : f.light, f.key, 0, 'activity'); })
      .concat(EV_PROBLEMS.map(function (p) { return bar(p.label, p.color, p.key, 1, 'problems'); }));
    return {
      animationDuration: 300,
      legend: [
        { top: 0, left: 0, itemWidth: 10, itemHeight: 10, icon: 'roundRect', itemGap: 14,
          textStyle: { color: inkMute, fontFamily: mono, fontSize: 11 },
          data: EV_FAMILIES.map(function (f) { return f.label; }) },
        { top: '71%', left: 0, itemWidth: 10, itemHeight: 10, icon: 'roundRect', itemGap: 14,
          textStyle: { color: inkMute, fontFamily: mono, fontSize: 11 },
          data: EV_PROBLEMS.map(function (p) { return p.label; }) }
      ],
      grid: [
        { left: 40, right: 12, top: 26, height: '52%' },
        { left: 40, right: 12, top: '79%', bottom: 24 }
      ],
      xAxis: [axis(0, false), axis(1, true)],
      yAxis: [yAxis(0), yAxis(1)],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis', confine: true,
        axisPointer: { type: 'shadow', shadowStyle: { color: dark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.04)' } },
        backgroundColor: surface, borderColor: rule, borderWidth: 1, padding: [8, 10],
        textStyle: { color: ink, fontFamily: mono, fontSize: 11.5 },
        extraCssText: 'box-shadow: 0 6px 24px rgba(0,0,0,.18); border-radius: 8px;',
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var t0 = ps[0].value[0] - width / 2;
          var fmt = function (ms) { return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
          var day = ev.hours > 24 ? new Date(t0).toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' : '';
          var lines = ps.filter(function (p) { return Number(p.value[1]) > 0; }).map(function (p) {
            return '<div style="display:flex;align-items:center;gap:8px;justify-content:space-between">' +
              '<span style="display:flex;align-items:center;gap:6px"><span style="width:9px;height:9px;border-radius:2px;background:' +
              p.color + '"></span><span style="color:' + inkMute + '">' + escapeHtml(p.seriesName) + '</span></span>' +
              '<span>' + p.value[1] + '</span></div>';
          });
          return '<div style="min-width:200px"><div style="font-weight:600;margin-bottom:6px">' +
            day + fmt(t0) + ' - ' + fmt(t0 + width) + '</div>' + (lines.join('') || '<span style="color:' + inkMute + '">no events</span>') + '</div>';
        }
      },
      series: series
    };
  }

  function drawEventsChart() {
    var el = $('#evChart');
    if (!el || typeof echarts === 'undefined' || !ev.data) return;
    if (!ev.chart) {
      ev.chart = echarts.init(el, null, { renderer: 'svg' });
      window.addEventListener('resize', function () { if (ev.chart) ev.chart.resize(); });
      new MutationObserver(function () { drawEventsChart(); })
        .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
    ev.chart.setOption(evOption(ev.data), { notMerge: true });
    var b = ev.data.buckets, sum = function (k) { return b.reduce(function (a, x) { return a + x[k]; }, 0); };
    var all = EV_FAMILIES.reduce(function (a, f) { return a + sum(f.key); }, 0);
    var probs = EV_PROBLEMS.reduce(function (a, p) { return a + sum(p.key); }, 0);
    var per = ev.data.bucket_seconds >= 3600 ? (ev.data.bucket_seconds / 3600) + 'h' : (ev.data.bucket_seconds / 60) + 'm';
    var foot = $('#evChartFoot');
    if (foot) foot.textContent = all.toLocaleString() + ' events and ' + probs.toLocaleString() +
      ' problems in the last ' + (ev.hours === 168 ? '7 days' : ev.hours + 'h') + ' · one column per ' + per;
    $$('#evRanges button').forEach(function (btn) {
      btn.classList.toggle('is-on', Number(btn.getAttribute('data-hours')) === ev.hours);
    });
  }

  function loadEventsChart() {
    fetch('/api/events/histogram?hours=' + ev.hours, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) return snFail(r); return r.json(); })
      .then(function (d) { ev.data = d; drawEventsChart(); })
      .catch(function (e) { var f = $('#evChartFoot'); if (f) f.textContent = 'chart unavailable: ' + e.message; });
  }

  function startEventsChart() {
    if (!$('#evChart')) return;
    $$('#evRanges button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        ev.hours = Number(btn.getAttribute('data-hours')) || 24;
        try { localStorage.setItem('sn-events-hours', String(ev.hours)); } catch (e) {}
        loadEventsChart();
      });
    });
    loadEventsChart();
    setInterval(loadEventsChart, 60000);
  }

  // ---------------------------------------------------- messages: traffic chart
  // Inbound above the axis, outbound mirrored below it: one measure (messages),
  // one y-scale, the sign only says which way they travelled. Colour is the
  // channel, fixed slots validated for stacks on both surfaces; a channel keeps
  // its colour in both directions and one legend entry toggles both. Blocked and
  // failed sit in their own strip underneath in status colours, with names.
  var MSG_CHANNELS = [
    { key: 'web-chat', light: '#2a78d6', dark: '#3987e5' },
    { key: 'telegram', light: '#eb6834', dark: '#d95926' },
    { key: 'email',    light: '#1baf7a', dark: '#199e70' },
    { key: 'voice',    light: '#eda100', dark: '#c98500' },
    { key: 'internal', light: '#e87ba4', dark: '#d55181' },
    { key: 'other',    light: '#898781', dark: '#898781' }
  ];
  var MSG_PROBLEMS = [
    { key: 'failed',  label: 'failed',  color: '#d03b3b' },
    { key: 'blocked', label: 'blocked', color: '#fab219' }
  ];
  var mc = { chart: null, hours: 24, data: null };
  try { var _mh = Number(localStorage.getItem('sn-messages-hours')); if ([1, 6, 24, 168].indexOf(_mh) >= 0) mc.hours = _mh; } catch (e) {}

  // The larger side, rounded up to a clean tick (32 -> 40, 318 -> 400).
  function msgNiceMax(v) {
    var m = Math.max(Math.abs(v.min), Math.abs(v.max), 1);
    var p = Math.pow(10, Math.floor(Math.log(m) / Math.LN10));
    return Math.ceil(m / p) * p;
  }

  function msgChartOption(d) {
    var dark = tlDark();
    var ink = tlToken('--sn-ink', dark ? '#f2f2f0' : '#0a0a0a');
    var inkMute = tlToken('--sn-ink-mute', '#76767a');
    var rule = tlToken('--sn-rule', dark ? 'rgba(255,255,255,.075)' : '#e7e6e1');
    var surface = tlToken('--sn-surface', dark ? '#131419' : '#ffffff');
    var mono = tlToken('--sn-mono', 'JetBrains Mono, monospace');
    var width = d.bucket_seconds * 1000;
    var from = d.buckets.length ? d.buckets[0].t * 1000 : Date.now() - mc.hours * 3600000;
    var to = d.buckets.length ? d.buckets[d.buckets.length - 1].t * 1000 + width : Date.now();
    var present = MSG_CHANNELS.filter(function (c) {
      return d.buckets.some(function (b) { return b['in_' + c.key] || b['out_' + c.key]; });
    });
    var xAxis = function (gi, labels) {
      return { type: 'time', gridIndex: gi, min: from, max: to,
        axisLine: { show: gi === 1, lineStyle: { color: rule } }, axisTick: { show: false },
        axisLabel: { show: labels, color: inkMute, fontFamily: mono, fontSize: 10.5, hideOverlap: true,
                     formatter: { day: '{MMM} {d}', hour: '{HH}:{mm}', minute: '{HH}:{mm}' } },
        splitLine: { show: false } };
    };
    var bar = function (name, color, gi, stack, values) {
      // barGap -100%: the inbound and outbound stacks of a bucket share one slot,
      // one above the axis and one below it, instead of standing side by side.
      return { type: 'bar', name: name, stack: stack, xAxisIndex: gi, yAxisIndex: gi,
        barMaxWidth: 24, barCategoryGap: '20%', barGap: '-100%',
        itemStyle: { color: color, borderColor: surface, borderWidth: 1 },
        data: d.buckets.map(function (b, i) { return [b.t * 1000 + width / 2, values[i] || '-']; }) };
    };
    var series = [];
    present.forEach(function (c) {
      var col = dark ? c.dark : c.light;
      series.push(bar(c.key, col, 0, 'in', d.buckets.map(function (b) { return b['in_' + c.key]; })));
      series.push(bar(c.key, col, 0, 'out', d.buckets.map(function (b) { return -b['out_' + c.key]; })));
    });
    MSG_PROBLEMS.forEach(function (p) {
      series.push(bar(p.label, p.color, 1, 'problems', d.buckets.map(function (b) { return b[p.key]; })));
    });
    return {
      animationDuration: 300,
      legend: [
        { top: 0, left: 0, itemWidth: 10, itemHeight: 10, icon: 'roundRect', itemGap: 14,
          textStyle: { color: inkMute, fontFamily: mono, fontSize: 11 }, data: present.map(function (c) { return c.key; }) },
        { top: '75%', left: 0, itemWidth: 10, itemHeight: 10, icon: 'roundRect', itemGap: 14,
          textStyle: { color: inkMute, fontFamily: mono, fontSize: 11 }, data: MSG_PROBLEMS.map(function (p) { return p.label; }) }
      ],
      // Which side is which, written on the chart, so the sign never has to be guessed.
      graphic: [
        { type: 'text', left: 44, top: 28, style: { text: 'inbound', fill: inkMute, font: '10px ' + mono } },
        { type: 'text', left: 44, top: '62%', style: { text: 'outbound', fill: inkMute, font: '10px ' + mono } }
      ],
      grid: [
        { left: 40, right: 12, top: 26, height: '56%' },
        { left: 40, right: 12, top: '83%', bottom: 24 }
      ],
      xAxis: [xAxis(0, false), xAxis(1, true)],
      yAxis: [
        // Symmetric around zero, so in and out are read against the same distance.
        { type: 'value', gridIndex: 0, minInterval: 1, splitNumber: 4,
          min: function (v) { return -msgNiceMax(v); },
          max: function (v) { return msgNiceMax(v); },
          axisLabel: { color: inkMute, fontFamily: mono, fontSize: 10, formatter: function (v) { return Math.abs(v); } },
          splitLine: { lineStyle: { color: rule, width: 1 } } },
        { type: 'value', gridIndex: 1, minInterval: 1, splitNumber: 1,
          axisLabel: { color: inkMute, fontFamily: mono, fontSize: 10 }, splitLine: { lineStyle: { color: rule, width: 1 } } }
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis', confine: true,
        axisPointer: { type: 'shadow', shadowStyle: { color: dark ? 'rgba(255,255,255,.04)' : 'rgba(0,0,0,.04)' } },
        backgroundColor: surface, borderColor: rule, borderWidth: 1, padding: [8, 10],
        textStyle: { color: ink, fontFamily: mono, fontSize: 11.5 },
        extraCssText: 'box-shadow: 0 6px 24px rgba(0,0,0,.18); border-radius: 8px;',
        formatter: function (ps) {
          if (!ps || !ps.length) return '';
          var t0 = ps[0].value[0] - width / 2;
          var fmt = function (ms) { return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); };
          var day = mc.hours > 24 ? new Date(t0).toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' : '';
          var row = function (color, name, v) {
            return '<div style="display:flex;align-items:center;gap:8px;justify-content:space-between">' +
              '<span style="display:flex;align-items:center;gap:6px"><span style="width:9px;height:9px;border-radius:2px;background:' +
              color + '"></span><span style="color:' + inkMute + '">' + escapeHtml(name) + '</span></span><span>' + v + '</span></div>';
          };
          var inRows = [], outRows = [], probRows = [];
          ps.forEach(function (p) {
            var v = Number(p.value[1]); if (!v) return;
            if (p.seriesName === 'blocked' || p.seriesName === 'failed') probRows.push(row(p.color, p.seriesName, v));
            else if (v > 0) inRows.push(row(p.color, p.seriesName, v));
            else outRows.push(row(p.color, p.seriesName, -v));
          });
          var sect = function (title, rows) {
            return rows.length ? '<div style="color:' + inkMute + ';margin:6px 0 2px">' + title + '</div>' + rows.join('') : '';
          };
          var body = sect('inbound', inRows) + sect('outbound', outRows) + sect('problems', probRows);
          return '<div style="min-width:200px"><div style="font-weight:600">' + day + fmt(t0) + ' - ' + fmt(t0 + width) + '</div>' +
            (body || '<div style="color:' + inkMute + ';margin-top:6px">no messages</div>') + '</div>';
        }
      },
      series: series
    };
  }

  function drawMessagesChart() {
    var el = $('#msgChart');
    if (!el || typeof echarts === 'undefined' || !mc.data) return;
    if (!mc.chart) {
      mc.chart = echarts.init(el, null, { renderer: 'svg' });
      window.addEventListener('resize', function () { if (mc.chart) mc.chart.resize(); });
      new MutationObserver(function () { drawMessagesChart(); })
        .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    }
    mc.chart.setOption(msgChartOption(mc.data), { notMerge: true });
    var b = mc.data.buckets, inN = 0, outN = 0, bad = 0;
    b.forEach(function (x) {
      mc.data.channels.forEach(function (c) { inN += x['in_' + c]; outN += x['out_' + c]; });
      bad += x.blocked + x.failed;
    });
    var per = mc.data.bucket_seconds >= 3600 ? (mc.data.bucket_seconds / 3600) + 'h' : (mc.data.bucket_seconds / 60) + 'm';
    var foot = $('#msgChartFoot');
    if (foot) foot.textContent = inN.toLocaleString() + ' in · ' + outN.toLocaleString() + ' out · ' + bad.toLocaleString() +
      ' blocked or failed in the last ' + (mc.hours === 168 ? '7 days' : mc.hours + 'h') + ' · one column per ' + per;
    $$('#msgRanges button').forEach(function (btn) {
      btn.classList.toggle('is-on', Number(btn.getAttribute('data-hours')) === mc.hours);
    });
  }

  function loadMessagesChart() {
    fetch('/api/messages/histogram?hours=' + mc.hours, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) return snFail(r); return r.json(); })
      .then(function (d) { mc.data = d; drawMessagesChart(); })
      .catch(function (e) { var f = $('#msgChartFoot'); if (f) f.textContent = 'chart unavailable: ' + e.message; });
  }

  function startMessagesChart() {
    if (!$('#msgChart')) return;
    $$('#msgRanges button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        mc.hours = Number(btn.getAttribute('data-hours')) || 24;
        try { localStorage.setItem('sn-messages-hours', String(mc.hours)); } catch (e) {}
        loadMessagesChart();
      });
    });
    loadMessagesChart();
    setInterval(loadMessagesChart, 60000);
  }

  // The footer version comes from /version.txt, which the image build copies
  // from bootstrap/smart-node-ui/VERSION. Hand-written per page it had drifted
  // to v0.0.3 on fourteen pages while the panel was 0.6.2. If the file is
  // missing the footer keeps whatever the page says.
  function wireFooterVersion() {
    var metas = $$('.page-footer .footer-meta');
    if (!metas.length) return;
    fetch('/version.txt', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (v) {
        v = (v || '').trim();
        if (!/^\d+\.\d+\.\d+$/.test(v)) return;
        metas.forEach(function (m) { m.textContent = 'Smart Node panel · v' + v; });
      })
      .catch(function () {});
  }

  // ---------------------------------------------------- boot
  function init() {
    initChrome();
    wireFooterVersion();
    var page = document.body.getAttribute('data-page');
    if (page === 'overview') { startOverviewPoll(); initOverviewSwitches(); }
    // The events TABLE belongs to foundation.js (server-side filters). This file
    // used to poll and draw the same table too; the two overwrote each other every
    // 30 s and this one painted every row "new" (green) on load. Chart only here.
    if (page === 'events')   startEventsChart();
    if (page === 'messages') { startMessagesPoll(); startMessagesChart(); }
    if (page === 'services') startServicesPoll();
    if (page === 'crons')    startCronsPoll();
    if (page === 'ai')       { wireTimelineRanges(); wireAiTabs(); startAiPoll(); wireGwModal(); wireContextLimit(); }
    if (page === 'agents')   { startAgentsPoll(); wireAgentCardClicks(); }
    if (page === 'agent-edit') startAgentEditPage();
    if (page === 'agent-new')  startAgentNewPage();
    if (page === 'tools')    startToolsPage();
    if (page === 'integrations') startIntegrationsPage();
    if (page === 'runtimes') startRuntimesPage();
    if (page === 'policies') startPoliciesPage();
    if (page === 'references') startReferencesPage();
    if (page === 'mcp') startMcpPage();
    if (page === 'specialists') startSpecialistsPage();
    if (page === 'secrets') startSecretsPage();
  }

  // ---------------------------------------------------- integrations page (B2-Integrations phase 5)
  var integrationsState = { rows: [], filterStatus: '' };

  function fetchIntegrations() {
    // The filter works on the live status, which core-api computes per request;
    // the stored status column is no longer shown.
    return fetch('/api/integrations', { cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) return snFail(r); return r.json(); });
  }

  function renderIntegrations() {
    var all = integrationsState.rows.slice().sort(function (a, b) { return a.slug.localeCompare(b.slug); });
    var total = all.length;
    var avail = all.filter(function (r) { return r.live_status === 'available'; }).length;
    var broken = all.filter(function (r) { return r.live_status === 'broken'; }).length;
    var toolsSum = all.reduce(function (s, r) { return s + (r.helper_tools_count || 0); }, 0);
    var rows = integrationsState.filterStatus
      ? all.filter(function (r) { return r.live_status === integrationsState.filterStatus; }) : all;
    setText($('[data-field="integrations-total"]'), total);
    setText($('[data-field="integrations-available"]'), avail);
    setText($('[data-field="integrations-broken"]'), broken);
    setText($('[data-field="integrations-tools"]'), toolsSum);
    var brokenCard = $('[data-stat="integrations-broken"]');
    if (brokenCard) brokenCard.classList.toggle('warn', broken > 0);

    var body = $('#integrationsBody'); if (!body) return;
    if (rows.length === 0) {
      body.innerHTML = '<tr class="empty-row"><td colspan="8">' +
        (total ? 'No integration is ' + escapeHtml(integrationsState.filterStatus) + '.' : 'No integrations registered.') + '</td></tr>';
    } else {
      body.innerHTML = '';
      rows.forEach(function (r) {
        var tr = document.createElement('tr');
        // Live status from the host collector (core-api _live_status): quiet when
        // available, loud when broken, plain "registered" when nothing on the host
        // can be checked. The reason is on hover.
        var ls = r.live_status || 'unknown';
        var statusCell;
        if (ls === 'broken') {
          statusCell = '<span class="health-dot fail"></span> broken';
        } else if (ls === 'available') {
          statusCell = '<span class="muted">ok</span>';
        } else if (ls === 'registered') {
          statusCell = '<span class="muted">registered</span>';
        } else {
          statusCell = '<span class="health-dot warn"></span> ' + escapeHtml(ls);
        }
        statusCell = '<span title="' + escapeAttr(r.live_reason || '') + '">' + statusCell + '</span>';
        var mcpCell = r.mcp_server
          ? '<code>' + escapeHtml(r.mcp_server.slug) + '</code> <span class="muted">' + escapeHtml(r.mcp_server.transport || '') +
            (r.mcp_server.enabled ? '' : ' · off') + '</span>'
          : '<span class="muted">-</span>';
        var canDelete = r.slug !== 'system';
        var managedBadge = r.companion_slug
          ? '<span class="kind-tag" title="managed by companion installer: ' + escapeAttr(r.companion_slug) + '">managed</span> '
          : '';
        var pathCell = r.install_path
          ? '<code>' + escapeHtml(r.install_path) + '</code>'
          : '<span class="muted">-</span>';
        tr.innerHTML =
          '<td class="col-name"><code>' + escapeHtml(r.slug) + '</code></td>' +
          '<td class="col-title">' + (r.purpose ? escapeHtml(r.purpose) : '<span class="muted">-</span>') + '</td>' +
          '<td class="col-title">' + pathCell + '</td>' +
          '<td class="col-kind"><span class="kind-tag kind-' + escapeAttr(r.kind) + '">' + escapeHtml(r.kind) + '</span></td>' +
          '<td class="col-status">' + statusCell + '</td>' +
          '<td class="col-title">' + mcpCell + '</td>' +
          '<td class="col-kind">' + (r.helper_tools_count || '<span class="muted">0</span>') + '</td>' +
          '<td class="col-actions">' +
            managedBadge +
            '<button type="button" class="btn-row" data-act="edit" data-slug="' + escapeAttr(r.slug) + '">Edit</button> ' +
            (canDelete
              ? '<button type="button" class="btn-row btn-danger" data-act="delete" data-slug="' + escapeAttr(r.slug) + '">Delete</button>'
              : '<button type="button" class="btn-row" disabled title="system integration cannot be removed">Delete</button>') +
          '</td>';
        body.appendChild(tr);
      });
    }

    setText($('#lastRefresh'), new Date().toLocaleTimeString(undefined, { hour12: false }));
    var dot = $('#liveDot'); if (dot) { dot.classList.remove('stale'); dot.classList.add('live'); }
  }

  function refreshIntegrations() {
    return fetchIntegrations()
      .then(function (d) { integrationsState.rows = d.integrations || []; renderIntegrations(); })
      .catch(function (err) {
        var body = $('#integrationsBody');
        if (body) body.innerHTML = '<tr class="empty-row"><td colspan="8">Failed to load: ' + escapeHtml(err.message || err) + '</td></tr>';
      });
  }

  function openIntegrationModal(existing) {
    var scrim = $('#integrationModalScrim');
    var form = $('#integrationForm');
    if (!scrim || !form) return;
    form.reset();
    $('#integrationFormError').textContent = '';
    var note = $('#integrationCompanionNote');
    if (existing) {
      $('#integrationModalTitle').textContent = 'Edit integration · ' + existing.slug;
      $('#integrationSlug').value = existing.slug;
      $('#integrationSlug').readOnly = true;
      form.elements['original_slug'].value = existing.slug;
      $('#integrationLabel').value = existing.label || '';
      $('#integrationPurpose').value = existing.purpose || '';
      $('#integrationKind').value = existing.kind || 'app';
      $('#integrationStatus').value = existing.status || 'available';
      $('#integrationDesc').value = existing.description || '';
      $('#integrationInstallUri').value = existing.install_uri || '';
      $('#integrationInstallPath').value = existing.install_path || '';
      $('#integrationCompanionSlug').value = existing.companion_slug || '';
      var isCompanion = !!existing.companion_slug;
      if (note) note.hidden = !isCompanion;
      // Lock companion-managed fields except status (and purpose, which is metadata-y).
      $('#integrationLabel').readOnly        = isCompanion;
      $('#integrationPurpose').readOnly      = false;
      $('#integrationKind').disabled         = isCompanion;
      $('#integrationDesc').readOnly         = isCompanion;
      $('#integrationInstallUri').readOnly   = isCompanion;
      $('#integrationInstallPath').readOnly  = isCompanion;
      $('#integrationCompanionSlug').readOnly = isCompanion;
    } else {
      $('#integrationModalTitle').textContent = 'New integration';
      $('#integrationSlug').readOnly = false;
      form.elements['original_slug'].value = '';
      if (note) note.hidden = true;
      $('#integrationLabel').readOnly        = false;
      $('#integrationPurpose').readOnly      = false;
      $('#integrationKind').disabled         = false;
      $('#integrationDesc').readOnly         = false;
      $('#integrationInstallUri').readOnly   = false;
      $('#integrationInstallPath').readOnly  = false;
      $('#integrationCompanionSlug').readOnly = false;
    }
    scrim.hidden = false;
    setTimeout(function () { (existing ? $('#integrationLabel') : $('#integrationSlug')).focus(); }, 30);
  }

  function closeIntegrationModal() {
    var scrim = $('#integrationModalScrim');
    if (scrim) scrim.hidden = true;
  }

  function readIntegrationForm() {
    var form = $('#integrationForm');
    var fd = new FormData(form);
    return {
      slug: String(fd.get('slug') || '').trim(),
      label: String(fd.get('label') || '').trim(),
      purpose: String(fd.get('purpose') || '').trim(),
      kind: String(fd.get('kind') || 'app'),
      status: String(fd.get('status') || 'available'),
      description: String(fd.get('description') || ''),
      install_uri: String(fd.get('install_uri') || '').trim(),
      install_path: String(fd.get('install_path') || '').trim(),
      companion_slug: String(fd.get('companion_slug') || '').trim() || null
    };
  }

  function submitIntegrationForm(e) {
    e.preventDefault();
    var form = $('#integrationForm');
    var err = $('#integrationFormError'); err.textContent = '';
    var submitBtn = $('#integrationSubmit'); submitBtn.disabled = true;

    var origSlug = form.elements['original_slug'].value;
    var body = readIntegrationForm();
    var isEdit = !!origSlug;
    var url = isEdit ? '/api/integrations/' + encodeURIComponent(origSlug) : '/api/integrations';
    var method = isEdit ? 'PUT' : 'POST';
    if (isEdit) { delete body.slug; }

    fetch(url, {
      method: method,
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (j) {
          throw new Error(snErr(r.status, j));
        }, function () { throw new Error(snErr(r.status)); });
      }
      closeIntegrationModal();
      return refreshIntegrations();
    }).catch(function (e2) {
      err.textContent = (e2 && e2.message) ? String(e2.message) : 'Save failed';
    }).then(function () { submitBtn.disabled = false; });
  }

  function deleteIntegration(slug) {
    if (!slug) return;
    snAsk('Delete integration "' + slug + '"?', 'Its tools stay, without an integration.', 'Delete', true).then(function (ok) {
      if (!ok) return;
      fetch('/api/integrations/' + encodeURIComponent(slug), {
        method: 'DELETE',
        headers: { 'Accept': 'application/json' }
      }).then(function (r) {
        if (!r.ok && r.status !== 204) {
          return r.json().then(function (j) {
            throw new Error(snErr(r.status, j));
          }, function () { throw new Error(snErr(r.status)); });
        }
        return refreshIntegrations();
      }).catch(function (e) {
        snTell('Delete failed', (e.message || e));
      });
    });
  }

  function startIntegrationsPage() {
    var statusSel = $('#filterIntegrationStatus');
    if (statusSel) statusSel.addEventListener('change', function () {
      integrationsState.filterStatus = statusSel.value;
      renderIntegrations();
    });

    var newBtn = $('#integrationNewBtn');
    if (newBtn) newBtn.addEventListener('click', function () { openIntegrationModal(null); });

    $('#integrationModalClose')  && $('#integrationModalClose').addEventListener('click', closeIntegrationModal);
    $('#integrationModalCancel') && $('#integrationModalCancel').addEventListener('click', closeIntegrationModal);
    $('#integrationModalScrim')  && $('#integrationModalScrim').addEventListener('click', function (e) {
      if (e.target && e.target.id === 'integrationModalScrim') closeIntegrationModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var scrim = $('#integrationModalScrim');
        if (scrim && !scrim.hidden) closeIntegrationModal();
      }
    });

    var form = $('#integrationForm');
    if (form) form.addEventListener('submit', submitIntegrationForm);

    var body = $('#integrationsBody');
    if (body) body.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('button[data-act]');
      if (!btn || btn.disabled) return;
      var act = btn.getAttribute('data-act');
      var slug = btn.getAttribute('data-slug');
      if (act === 'delete') return deleteIntegration(slug);
      if (act === 'edit') {
        var row = (integrationsState.rows || []).find(function (r) { return r.slug === slug; });
        if (row) openIntegrationModal(row);
      }
    });

    refreshIntegrations();
  }

  // ---------------------------------------------------- runtimes page
  // Agent runtimes = what drives an agent's turn loop: a coding CLI, or brin,
  // the node's own loop, for an agent on a model API. Model APIs themselves are
  // the gateway's providers, on AI -> Providers. Read live
  // from /api/runtimes/live: the old registry table was empty, nothing
  // wrote it. Read-only.
  var runtimesState = { rows: [] };

  function renderRuntimes() {
    var rows = runtimesState.rows;
    var avail = rows.filter(function (r) { return r.live_status === 'available'; });
    setText($('[data-field="runtimes-total"]'), rows.length);
    setText($('[data-field="runtimes-available"]'), avail.length);
    setText($('[data-field="runtimes-agents"]'), rows.reduce(function (s, r) { return s + (r.agents || 0); }, 0));
    setText($('[data-field="runtimes-sessions"]'), rows.reduce(function (s, r) { return s + (r.sessions_7d || 0); }, 0));
    var body = $('#runtimesBody'); if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<tr class="empty-row"><td colspan="6">No runtimes reported.</td></tr>';
      return;
    }
    body.innerHTML = '';
    rows.forEach(function (r) {
      var ls = r.live_status || 'unknown';
      var dot = ls === 'available' ? 'ok' : (ls === 'not installed' ? '' : 'warn');
      var tr = document.createElement('tr');
      if (ls === 'not installed') tr.className = 'row-muted';
      tr.innerHTML =
        '<td class="col-name" style="white-space:nowrap"><strong>' + escapeHtml(r.label) + '</strong></td>' +
        '<td class="col-kind" style="white-space:nowrap"><code class="muted">' + escapeHtml(r.slug) + '</code></td>' +
        '<td class="col-title"' + (r.binary ? ' title="' + escapeAttr(r.binary) + '"' : '') + '>' +
          (r.version ? '<code>' + escapeHtml(r.version) + '</code>' : '<span class="muted">-</span>') + '</td>' +
        '<td class="col-status" title="' + escapeAttr(r.live_reason || '') + '">' +
          (dot ? '<span class="health-dot ' + dot + '"></span> ' : '') + escapeHtml(ls) + '</td>' +
        '<td class="col-kind" title="' + escapeAttr((r.agent_slugs || []).join(', ')) + '">' +
          (r.agents || '<span class="muted">0</span>') + '</td>' +
        '<td class="col-kind">' + (r.sessions_7d || '<span class="muted">0</span>') + '</td>';
      body.appendChild(tr);
    });
    setText($('#lastRefresh'), new Date().toLocaleTimeString(undefined, { hour12: false }));
    var live = $('#liveDot'); if (live) { live.classList.remove('stale'); live.classList.add('live'); }
  }

  function refreshRuntimes() {
    return fetch('/api/runtimes/live', { cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) return snFail(r); return r.json(); })
      .then(function (d) { runtimesState.rows = d.runtimes || []; renderRuntimes(); })
      .catch(function (err) {
        var body = $('#runtimesBody');
        if (body) body.innerHTML = '<tr class="empty-row"><td colspan="6">Failed to load: ' + escapeHtml(err.message || err) + '</td></tr>';
      });
  }

  function startRuntimesPage() {
    refreshRuntimes();
    setInterval(refreshRuntimes, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* Theme toggle button -- panel shipped with bootstrap only, no UI control */
(function () {
  function mount() {
    var right = document.querySelector('.topbar-right');
    if (!right || document.getElementById('themeToggle')) return;
    var b = document.createElement('button');
    b.id = 'themeToggle'; b.className = 'theme-toggle'; b.type = 'button';
    b.title = 'Light / dark'; b.setAttribute('aria-label', 'Toggle theme');
    b.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="4.4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6l1.4 1.4M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4l1.4-1.4M18 6l1.4-1.4"/></svg>';
    b.addEventListener('click', function () {
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('sn-theme', next); document.cookie='sn-theme='+next+'; domain='+location.hostname.replace(/^[^.]+\./,'')+'; path=/; max-age=31536000; SameSite=Lax'; } catch (e) {}
    });
    right.insertBefore(b, right.firstChild);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
