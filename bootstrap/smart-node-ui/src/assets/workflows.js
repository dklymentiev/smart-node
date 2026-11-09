// The panel's Workflows page. What each workflow is, its
// steps as text with where each outcome leads, and the owner's two controls:
// on or off (off starts no new run, a run in flight finishes) and which agents
// may start it. Both apply at once, as the panel's other switches do. Runs and
// the drawn graph are in the Studio, at studio.<domain>/workflows/<name>.
(function (root) {
  'use strict';

  function esc(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // An ending: a step whose only route stops the run (done, cancelled).
  function ending(s) {
    var r = s.routing || {}, k = Object.keys(r);
    return k.length === 1 && r[k[0]] === '_stop';
  }

  // A flow's steps are stored in no particular order. Walk them from the ones
  // nothing precedes, along their routes, so they read in the order they run;
  // a step no route reaches follows, and the endings come last.
  function ordered(steps) {
    var by = {}, seen = {}, out = [];
    steps.forEach(function (s) { by[s.name] = s; });
    var queue = steps.filter(function (s) { return !(s.depends_on || []).length; }).map(function (s) { return s.name; });
    while (queue.length) {
      var n = queue.shift();
      if (seen[n] || !by[n]) continue;
      seen[n] = true;
      out.push(by[n]);
      Object.keys(by[n].routing || {}).forEach(function (k) { queue.push(by[n].routing[k]); });
    }
    steps.forEach(function (s) { if (!seen[s.name]) out.push(s); });
    return out.filter(function (s) { return !ending(s); }).concat(out.filter(ending));
  }

  // Where a step's outcomes lead, one entry per next step with the outcomes
  // that lead there, "otherwise" last. Left out: an outcome that stays on the
  // step (waiting, expired: it asks again) and "skipped", which every step has.
  function routes(step) {
    var r = step.routing || {}, out = [], at = {};
    Object.keys(r).forEach(function (k) {
      if (k === '_default' || r[k] === step.name) return;
      if (k === 'skipped' && r[k] === '_stop') return;
      if (!(r[k] in at)) { at[r[k]] = out.length; out.push({ when: [], to: r[k] }); }
      out[at[r[k]]].when.push(k.replace(/_/g, ' '));
    });
    if (r._default != null && r._default !== step.name) {
      if (r._default in at) out[at[r._default]].when.push('otherwise');
      else out.push({ when: out.length ? ['otherwise'] : [], to: r._default });
    }
    return out.map(function (x) { return { when: x.when.join(', '), to: x.to }; });
  }

  function target(to, names) {
    if (to === '_stop') return '<span class="wf-end">end</span>';
    return names[to] && !ending(names[to]) ? '<b>' + esc(to.replace(/_/g, ' ')) + '</b>' : '<span class="wf-end">' + esc(to.replace(/_/g, ' ')) + '</span>';
  }

  function stepsHtml(w) {
    var steps = w.steps || [];
    if (!steps.length) return '';
    var names = {};
    steps.forEach(function (s) { names[s.name] = s; });
    var rows = ordered(steps).map(function (s, i) {
      var rs = routes(s);
      var then = ending(s) ? '<span class="wf-end">the run ends</span>'
        : rs.map(function (x) { return (x.when ? esc(x.when) + ', then ' : 'then ') + target(x.to, names); }).join('<span class="wf-sep">·</span>');
      return '<tr><td class="wf-n">' + (i + 1) + '</td><td class="wf-s">' + esc(String(s.name).replace(/_/g, ' '))
        + (s.specialist ? '<div class="wf-who">' + esc(s.specialist) + '</div>' : '') + '</td><td>' + then + '</td></tr>';
    }).join('');
    return '<details class="wf-steps"><summary>' + steps.length + ' steps</summary><table>' + rows + '</table></details>';
  }

  // agents: [{slug, title}] the node has, for names and the add list.
  function card(w, studio, agents) {
    var meta = [esc(w.name)];
    if (w.team) meta.push('team ' + esc(w.team));
    if (w.trigger) meta.push('starts on ' + esc(w.trigger));
    if (w.spec_version) meta.push('spec ' + esc(w.spec_version));
    var on = w.status === 'available';
    var title = {};
    (agents || []).forEach(function (a) { title[a.slug] = a.title || a.slug; });
    var mine = w.granted_to || [];
    var chips = mine.map(function (s) {
      var n = title[s] || s;
      return '<span class="chip wf-agent">' + esc(n) + '<button type="button" class="wf-x" data-act="drop" data-slug="' + esc(s)
        + '" aria-label="Take ' + esc(n) + ' off this workflow">&times;</button></span>';
    }).join('') || '<span class="wf-none">no agent may start it yet</span>';
    var rest = (agents || []).filter(function (a) { return mine.indexOf(a.slug) < 0; });
    var add = rest.length ? '<select class="wf-add" data-act="add" aria-label="Let another agent start it"><option value="">+ Add an agent</option>'
      + rest.map(function (a) { return '<option value="' + esc(a.slug) + '">' + esc(a.title || a.slug) + '</option>'; }).join('') + '</select>' : '';
    return '<div class="wf-card" data-name="' + esc(w.name) + '">'
      + '<div class="wf-top"><h2>' + esc(w.title || w.name) + '</h2>'
      + '<button type="button" class="switch' + (on ? ' on' : '') + '" role="switch" aria-checked="' + on + '" data-act="status" aria-label="'
      + (on ? 'On: switch it off' : 'Off: switch it on') + '"><span class="knob"></span></button>'
      + '<span class="wf-state">' + (on ? 'On' : 'Off') + '</span></div>'
      + (on ? '' : '<p class="wf-offnote">Off: no new run starts. A run already going finishes.</p>')
      + '<div class="wf-meta">' + meta.join(' · ') + '</div>'
      + (w.description ? '<p class="wf-desc">' + esc(w.description) + '</p>' : '')
      + '<div class="wf-grants"><strong>Agents who may start it:</strong> ' + chips + add + '</div>'
      + '<p class="wf-msg" role="status"></p>'
      + stepsHtml(w)
      + (studio ? '<a class="wf-diagram" href="' + studio + '/workflows/' + encodeURIComponent(w.name) + '" target="_blank" rel="noopener">Runs and the diagram, in the Studio</a>' : '')
      + '</div>';
  }

  var api = { esc: esc, ending: ending, ordered: ordered, routes: routes, card: card };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }

  var STUDIO = /^(admin|panel)\./.test(location.hostname) ? location.protocol + '//' + location.hostname.replace(/^(admin|panel)\./, 'studio.') : '';
  var el = document.getElementById('wfList');
  // A description is the flow's own instructions, often long: three lines, the
  // rest on a click.
  el.addEventListener('click', function (e) {
    var d = e.target.closest && e.target.closest('.wf-desc');
    if (!d) return;
    if (d) d.classList.toggle('open');
  });
  var byName = {}, AGENTS = [];

  function paint(name, msg) {
    var old = el.querySelector('.wf-card[data-name="' + CSS.escape(name) + '"]');
    if (!old) return;
    var open = old.querySelector('details') && old.querySelector('details').open;
    var t = document.createElement('div');
    t.innerHTML = card(byName[name], STUDIO, AGENTS);
    var fresh = t.firstChild;
    if (open && fresh.querySelector('details')) fresh.querySelector('details').open = true;
    if (msg) fresh.querySelector('.wf-msg').textContent = msg;
    old.replaceWith(fresh);
  }

  // One write at a time per card; the card is redrawn from what core-api says.
  function send(name, method, path, body, done) {
    var c = el.querySelector('.wf-card[data-name="' + CSS.escape(name) + '"]');
    c.classList.add('busy');
    fetch('/api/workflows/' + encodeURIComponent(name) + path, {
      method: method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.status === 403) throw new Error('only the owner can change this');
        if (!r.ok) throw new Error((d && (typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail))) || 'HTTP ' + r.status);
        return d;
      });
    }).then(function (d) {
      done(d);
      paint(name, 'Saved.');
    }).catch(function (e) {
      paint(name, 'Not saved: ' + (e.message || e));
    });
  }

  el.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-act]');
    if (!b || b.tagName === 'SELECT') return;
    var c = b.closest('.wf-card'), name = c && c.dataset.name, w = byName[name];
    if (!w || c.classList.contains('busy')) return;
    if (b.dataset.act === 'status') {
      send(name, 'PATCH', '', { status: w.status === 'available' ? 'off' : 'available' }, function (d) { w.status = d.status; });
    } else if (b.dataset.act === 'drop') {
      var keep = (w.granted_to || []).filter(function (s) { return s !== b.dataset.slug; });
      send(name, 'PUT', '/agents', { agents: keep }, function (d) { w.granted_to = d.granted_to; });
    }
  });
  el.addEventListener('change', function (e) {
    var s = e.target;
    if (!s.matches || !s.matches('select[data-act="add"]') || !s.value) return;
    var name = s.closest('.wf-card').dataset.name, w = byName[name];
    send(name, 'PUT', '/agents', { agents: (w.granted_to || []).concat([s.value]) }, function (d) { w.granted_to = d.granted_to; });
  });

  Promise.all([
    fetch('/api/workflows').then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }),
    fetch('/api/agents').then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; })
  ]).then(function (res) {
    var list = res[0].workflows || [];
    AGENTS = (res[1].agents || []).filter(function (a) { return a.enabled !== false; })
      .map(function (a) { return { slug: a.slug, title: a.title }; })
      .sort(function (a, b) { return String(a.title || a.slug).localeCompare(String(b.title || b.slug)); });
    list.forEach(function (w) { byName[w.name] = w; });
    el.className = '';
    el.innerHTML = list.length ? list.map(function (w) { return card(w, STUDIO, AGENTS); }).join('') : '<div class="wf-empty">No workflows registered yet.</div>';
  }).catch(function (e) {
    el.innerHTML = '<div class="wf-empty">Failed to load workflows: ' + esc(e.message || e) + '</div>';
  });
})(this);
