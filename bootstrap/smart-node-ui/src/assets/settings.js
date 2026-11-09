// The panel's Settings page: the node's own settings, read only. The node, the rules every
// agent's prompt carries, who can sign in and what each role may do, and the
// departments. Each section reads its own route; one that fails says so and
// the others still show.
(function () {
  'use strict';

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function get(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  function when(s) {
    var d = new Date(s);
    if (isNaN(d)) return esc(s || '');
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function fill(id, html) { document.getElementById(id).innerHTML = html; }
  function failed(id, e) { fill(id, '<p class="st-empty">Could not load: ' + esc(e.message || e) + '</p>'); }
  function rows(pairs) {
    return '<dl class="st-dl">' + pairs.map(function (p) { return '<dt>' + esc(p[0]) + '</dt><dd>' + p[1] + '</dd>'; }).join('') + '</dl>';
  }

  get('/api/node/about').then(function (n) {
    fill('stNode', rows([
      ['Name', esc(n.name || '')],
      ['Domain', n.domain ? '<code>' + esc(n.domain) + '</code>' : '<span class="st-mute">none set</span>'],
      ['Release', esc(n.release || 'unknown') + (n.commit ? ' <span class="st-mute">' + esc(n.commit) + '</span>' : '')],
      ['Installed', n.installed_at ? when(n.installed_at) : '<span class="st-mute">unknown</span>'],
      ['Agents', n.paused
        ? '<span class="st-pill st-pill--warn">paused</span> since ' + when(n.paused_changed_at) + ' <a href="/limits/">Resume on Limits</a>'
        : '<span class="st-pill">running</span> <a href="/limits/">Pause all on Limits</a>']
    ].concat(n.mail ? [['Daily digest', '<button type="button" class="switch" id="stDigest" role="switch" aria-label="Daily digest by e-mail" aria-checked="' + !!n.daily_digest + '"><span class="knob"></span></button>'
      + ' <span class="st-mute">An e-mail each morning with what went wrong the day before.</span>']] : [])));
    var dg = document.getElementById('stDigest');
    if (dg) dg.addEventListener('click', function () {
      var on = dg.getAttribute('aria-checked') !== 'true';
      dg.disabled = true;
      fetch('/api/node/digest', { method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: on }) })
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); dg.setAttribute('aria-checked', String(on)); })
        .catch(function (e) { window.snTell('Not saved', e.message); })
        .then(function () { dg.disabled = false; });
    });
  }).catch(function (e) { failed('stNode', e); });

  get('/api/policies').then(function (d) {
    var by = {};
    (d.policies || []).forEach(function (p) {
      var c = by[p.category] || (by[p.category] = { on: 0, off: 0 });
      if (p.enabled) c.on++; else c.off++;
    });
    var cats = Object.keys(by).sort();
    fill('stRules', cats.length
      ? '<table class="st-table"><thead><tr><th>Category</th><th>On</th><th>Off</th></tr></thead><tbody>'
        + cats.map(function (c) {
          return '<tr><td>' + esc(c) + '</td><td>' + by[c].on + '</td><td>' + (by[c].off || '<span class="st-mute">0</span>') + '</td></tr>';
        }).join('') + '</tbody></table><p><a href="/policies/">Read and edit them on Policies</a></p>'
      : '<p class="st-empty">No rules yet. <a href="/policies/">Add one on Policies</a>.</p>');
  }).catch(function (e) { failed('stRules', e); });

  // A role's permissions open in the panel's modal, grouped by area (the part
  // of the name before the first dot), with a filter: super-admin has 130+.
  var ROLES = {}, opener = null;
  var scrim = document.createElement('div');
  scrim.className = 'modal-scrim';
  scrim.hidden = true;
  scrim.innerHTML = '<div class="modal-panel modal-panel-wide" role="dialog" aria-modal="true" aria-labelledby="stPermsH">'
    + '<div class="modal-head"><h2 id="stPermsH"></h2><button type="button" class="modal-close" aria-label="Close">&times;</button></div>'
    + '<div class="modal-form"><p class="form-hint" id="stPermsWho"></p>'
    + '<div class="form-row"><input type="text" id="stPermsFilter" placeholder="Filter by name or what it does" aria-label="Filter permissions"></div>'
    + '<div class="st-perm-groups" id="stPermsList"></div></div></div>';
  document.body.appendChild(scrim);
  var filter = scrim.querySelector('#stPermsFilter');

  function permsHtml(perms, q) {
    q = q.trim().toLowerCase();
    var groups = {};
    perms.forEach(function (p) {
      if (q && (p.name + ' ' + (p.description || '')).toLowerCase().indexOf(q) < 0) return;
      var g = p.name.split('.')[0];
      (groups[g] = groups[g] || []).push(p);
    });
    var keys = Object.keys(groups).sort();
    if (!keys.length) return '<p class="st-empty">No permission matches.</p>';
    return keys.map(function (g) {
      return '<div class="grants-group"><div class="grants-group-head">/ ' + esc(g) + ' <span class="st-count">' + groups[g].length + '</span></div><ul class="st-perms">'
        + groups[g].map(function (p) {
          return '<li><code>' + esc(p.name) + '</code>' + (p.description ? ' <span class="st-mute">' + esc(p.description) + '</span>' : '') + '</li>';
        }).join('') + '</ul></div>';
    }).join('');
  }
  function openPerms(role, btn) {
    var r = ROLES[role];
    if (!r) return;
    opener = btn;
    scrim.dataset.role = role;
    scrim.querySelector('#stPermsH').textContent = role + ': what it may do';
    scrim.querySelector('#stPermsWho').textContent = r.permissions.length + ' permissions. Held by: '
      + (r.holders.length ? r.holders.map(function (h) { return h.name || h.id; }).join(', ') : (r.role === 'user' ? 'everyone with no other role' : 'nobody')) + '.';
    filter.value = '';
    scrim.querySelector('#stPermsList').innerHTML = permsHtml(r.permissions, '');
    scrim.hidden = false;
    document.body.style.overflow = 'hidden';
    filter.focus();
  }
  function closePerms() {
    if (scrim.hidden) return;
    scrim.hidden = true;
    document.body.style.overflow = '';
    if (opener) opener.focus();
  }
  filter.addEventListener('input', function () {
    var r = ROLES[scrim.dataset.role];
    if (r) scrim.querySelector('#stPermsList').innerHTML = permsHtml(r.permissions, filter.value);
  });
  scrim.addEventListener('click', function (e) {
    if (e.target === scrim || (e.target.closest && e.target.closest('.modal-close'))) closePerms();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePerms(); });
  document.getElementById('stAccess').addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-perms]');
    if (b) openPerms(b.dataset.perms, b);
  });

  get('/api/node/access').then(function (d) {
    var logins = (d.logins || []).map(function (l) {
      return '<tr><td><b>' + esc(l.login) + '</b>'
        + (l.other_names && l.other_names.length ? '<div class="st-mute st-small">other names: ' + l.other_names.map(esc).join(', ') + '</div>' : '')
        + '</td><td><span class="st-pill">' + esc(l.role) + '</span></td><td class="st-mute">' + (l.created ? when(l.created) : '') + '</td></tr>';
    }).join('');
    var roles = (d.roles || []).map(function (r) {
      ROLES[r.role] = r;
      var holders = r.holders.length
        ? r.holders.map(function (h) { return '<span class="chip">' + esc(h.name || h.id) + '</span>'; }).join('')
        : '<span class="st-mute">' + (r.role === 'user' ? 'everyone with no other role' : 'nobody') + '</span>';
      return '<div class="st-role"><div class="st-role__head"><b>' + esc(r.role) + '</b> <span class="st-mute">' + r.permissions.length + ' permissions</span></div>'
        + '<div class="st-role__who">' + holders + '</div>'
        + (r.permissions.length ? '<button type="button" class="st-link" data-perms="' + esc(r.role) + '">What it may do</button>' : '')
        + '</div>';
    }).join('');
    fill('stAccess', '<h3>Who can sign in</h3>'
      + (logins ? '<table class="st-table"><thead><tr><th>Account</th><th>Role</th><th>Since</th></tr></thead><tbody>' + logins + '</tbody></table>' : '<p class="st-empty">No accounts found.</p>')
      + '<h3>Roles</h3><div class="st-roles">' + roles + '</div>');
  }).catch(function (e) { failed('stAccess', e); });

  Promise.all([get('/api/departments'), get('/api/agents').catch(function () { return {}; })]).then(function (res) {
    var names = {};
    (res[1].agents || []).forEach(function (a) { names[a.id] = a.title || a.slug; });
    var deps = (res[0].departments || []).slice().sort(function (a, b) { return String(a.name).localeCompare(String(b.name)); });
    fill('stDeps', deps.length
      ? '<table class="st-table"><thead><tr><th>Department</th><th>Head</th></tr></thead><tbody>'
        + deps.map(function (x) {
          return '<tr><td>' + esc(x.name) + '</td><td>' + (x.head_agent_id ? esc(names[x.head_agent_id] || ('#' + x.head_agent_id)) : '<span class="st-mute">none</span>') + '</td></tr>';
        }).join('') + '</tbody></table>'
      : '<p class="st-empty">No departments.</p>');
  }).catch(function (e) { failed('stDeps', e); });
})();
