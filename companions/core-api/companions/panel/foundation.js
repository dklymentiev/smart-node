// Smart Node panel -- Foundation wiring.
// Auto-detects body[data-page] and renders the matching view from core-api.
// Endpoints: GET /api/events, /api/crons, /api/crons/{id}/runs, /api/system/status

(function () {
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  function fmtTs(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function fmtRel(secs) {
    if (secs === null || secs === undefined) return '—';
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
    return Math.floor(secs / 86400) + 'd ago';
  }
  function fmtUptime(sec) {
    if (!sec || sec < 0) return '—';
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
  function pretty(obj) {
    if (!obj || Object.keys(obj).length === 0) return '';
    try { return JSON.stringify(obj); } catch (e) { return String(obj); }
  }
  function liveDot(ok) {
    var d = $('#liveDot');
    if (!d) return;
    d.style.background = ok ? '#4ade80' : '#f87171';
  }
  function setText(field, value) {
    $$('[data-field="' + field + '"]').forEach(function (el) { el.textContent = value; });
  }

  // ----------------------------------------------------------- /events
  function renderEvents() {
    var body = $('#eventsBody');
    var filterType = $('#filterType');
    var filterActor = $('#filterActor');
    var typeSet = new Set();
    var actorSet = new Set();

    function load() {
      var params = new URLSearchParams({ limit: '200' });
      if (filterType && filterType.value) params.set('event_type', filterType.value);
      if (filterActor && filterActor.value) params.set('actor', filterActor.value);
      fetch('/api/events?' + params.toString(), { cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (data) {
          var rows = data.events || data.items || data || [];
          if (!Array.isArray(rows)) rows = [];
          if (rows.length === 0) {
            body.innerHTML = '<tr class="empty-row"><td colspan="5">No events yet.</td></tr>';
          } else {
            body.innerHTML = rows.map(function (e) {
              typeSet.add(e.event_type);
              actorSet.add(e.actor);
              var entity = e.entity_type ? (esc(e.entity_type) + (e.entity_id ? '/' + esc(e.entity_id) : '')) : '';
              return '<tr>' +
                '<td class="col-ts">' + esc(fmtTs(e.ts)) + '</td>' +
                '<td class="col-actor"><code>' + esc(e.actor) + '</code></td>' +
                '<td class="col-type"><code>' + esc(e.event_type) + '</code></td>' +
                '<td class="col-entity">' + entity + '</td>' +
                '<td class="col-payload"><code class="payload">' + esc(pretty(e.payload)) + '</code></td>' +
                '</tr>';
            }).join('');
          }
          if ($('#rowCount')) $('#rowCount').textContent = rows.length;
          if ($('#lastRefresh')) $('#lastRefresh').textContent = fmtTs(new Date().toISOString());
          if (filterType && filterType.options.length <= 1) {
            Array.from(typeSet).sort().forEach(function (t) {
              var o = document.createElement('option');
              o.value = t; o.textContent = t;
              filterType.appendChild(o);
            });
          }
          if (filterActor && filterActor.options.length <= 1) {
            Array.from(actorSet).sort().forEach(function (a) {
              var o = document.createElement('option');
              o.value = a; o.textContent = a;
              filterActor.appendChild(o);
            });
          }
          liveDot(true);
        })
        .catch(function (err) {
          body.innerHTML = '<tr class="empty-row"><td colspan="5">Error: ' + esc(err.message) + '</td></tr>';
          liveDot(false);
        });
    }

    if (filterType) filterType.addEventListener('change', load);
    if (filterActor) filterActor.addEventListener('change', load);
    load();
    setInterval(load, 30000);
  }

  // ----------------------------------------------------------- /crons
  function renderCrons() {
    var body = $('#cronsBody');
    function load() {
      fetch('/api/crons', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : { crons: [] }; })
        .then(function (data) {
          var crons = data.crons || data.items || data || [];
          if (!Array.isArray(crons)) crons = [];
          return Promise.all(crons.map(function (c) {
            return fetch('/api/crons/' + c.id + '/runs?limit=1', { cache: 'no-store' })
              .then(function (r) { return r.ok ? r.json() : null; })
              .then(function (rd) {
                var runs = rd && (rd.runs || rd.items || rd) || [];
                if (!Array.isArray(runs)) runs = [];
                c._last_run = runs[0] || null;
                return c;
              })
              .catch(function () { c._last_run = null; return c; });
          }));
        })
        .then(function (crons) {
          if (crons.length === 0) {
            body.innerHTML = '<tr class="empty-row"><td colspan="7">No crons registered.</td></tr>';
          } else {
            body.innerHTML = crons.map(function (c) {
              var last = c._last_run;
              var lastCell = '—';
              if (last) {
                lastCell = esc(fmtTs(last.started_at || last.finished_at));
                if (last.exit_code === 0) lastCell += ' <span style="color:#4ade80">ok</span>';
                else if (last.exit_code !== null && last.exit_code !== undefined) lastCell += ' <span style="color:#f87171">exit=' + esc(last.exit_code) + '</span>';
              }
              return '<tr>' +
                '<td class="col-kind">crontab</td>' +
                '<td class="col-scope">' + esc(c.group || 'smart-node') + '</td>' +
                '<td class="col-name"><code>' + esc(c.slug || c.command || ('cron-' + c.id)) + '</code></td>' +
                '<td class="col-schedule"><code>' + esc(c.schedule) + '</code></td>' +
                '<td class="col-what">' + esc(c.description || c.command || '') + '</td>' +
                '<td class="col-last">' + lastCell + '</td>' +
                '<td class="col-next">—</td>' +
                '</tr>';
            }).join('');
          }
          if ($('#rowCount')) $('#rowCount').textContent = crons.length;
          if ($('#lastRefresh')) $('#lastRefresh').textContent = fmtTs(new Date().toISOString());
          liveDot(true);
        })
        .catch(function (err) {
          body.innerHTML = '<tr class="empty-row"><td colspan="7">Error: ' + esc(err.message) + '</td></tr>';
          liveDot(false);
        });
    }
    load();
    setInterval(load, 60000);
  }

  // ----------------------------------------------------------- /overview (system status)
  function renderOverview() {
    function load() {
      fetch('/api/system/status', { cache: 'no-store' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (s) {
          if (s.host && s.host.uptime_secs !== undefined) setText('uptime', fmtUptime(s.host.uptime_secs));
          if (s.registry && s.registry.events_total !== null && s.registry.events_total !== undefined) setText('events24h', String(s.registry.events_total));
          var hbEl = document.querySelector('[data-field="heartbeat"]');
          if (hbEl && s.heartbeat) hbEl.textContent = fmtRel(s.heartbeat.age_secs);
        })
        .catch(function () { /* silent */ });
    }
    load();
    setInterval(load, 30000);
  }

  // ----------------------------------------------------------- boot
  document.addEventListener('DOMContentLoaded', function () {
    var page = document.body && document.body.getAttribute('data-page');
    if (page === 'events') renderEvents();
    else if (page === 'crons') renderCrons();
    else if (page === 'overview') renderOverview();
  });
})();
