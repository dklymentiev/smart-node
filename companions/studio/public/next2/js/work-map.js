// Work Map view runtime. The template was authored as a standalone page; here
// it lives inside the Studio SPA, so the body-of-template logic is wrapped in
// renderWorkMap() (idempotent, called every time the view opens) and
// teardownWorkMap() (removes the resize listener and hides the floating tip,
// so neither escapes the view when the user navigates away).
(function(){
  let _wmResize = null;
  let _wmAbort = null;
  let _wmStarting = false;

  // Picks the current workMapView section's DOM; using scope-restricted lookups
  // so a future screen that happens to use the same id (e.g. #boot) cannot be
  // confused with the Work Map's own.
  function root(){ return document.getElementById('workMapView'); }
  function q(sel){ const r = root(); return r ? r.querySelector(sel) : null; }
  function qa(sel){ const r = root(); return r ? Array.from(r.querySelectorAll(sel)) : []; }

  async function renderWorkMap(){
    const r = root(); if (!r) return;
    if (_wmStarting) return;
    _wmStarting = true;
    teardownWorkMap({ keepDom: true });

    const boot = q('#boot');
    const app  = q('#app');
    if (boot){ boot.style.display = ''; boot.className = ''; boot.innerHTML = wmSkeleton(); }
    if (app)  { app.style.display = 'none'; }

    let D;
    try {
      _wmAbort = new AbortController();
      const resp = await fetch('/api/work/map?fresh=1', { headers: { accept: 'application/json' }, signal: _wmAbort.signal });
      if (!resp.ok){ if (boot) boot.innerHTML = `<div class="err">Work map data not available (HTTP ${resp.status}).</div>`; _wmStarting = false; return; }
      D = await resp.json();
    } catch(e) {
      if (e && e.name === 'AbortError') { _wmStarting = false; return; }
      if (boot) boot.innerHTML = `<div class="err">Could not fetch /api/work/map: ${String(e)}</div>`;
      _wmStarting = false; return;
    }
    if (!D || !D.H){
      if (boot) boot.innerHTML = `<div class="err">Work map endpoint returned no data.</div>`;
      _wmStarting = false; return;
    }
    if (boot) boot.style.display = 'none';
    if (app)  app.style.display = '';

    const T0 = new Date(D.t0), H = D.H;
    const COLORS = ['#78a7ff','#49d69a','#b993ff','#66d6e7','#ff8fc7','#f29e6b','#9fd36b'];
    const EMERGENT = ['Emergent','others'];
    let mode = 'project', scope = 168, sel = null, feedMode = 'f', query = '', planOpen = false;
    let plan = {};
    try{ plan = JSON.parse(localStorage.getItem('workmap.plan') || '{}'); } catch(e){}
    const savePlan = () => { try{ localStorage.setItem('workmap.plan', JSON.stringify(plan)); } catch(e){} };

    const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
    const hDate = h => new Date(T0.getTime() + h * 3600e3);
    const whenStr = h => {
      const d = hDate(h), now = hDate(H - 1);
      const same = d.toDateString() === now.toDateString();
      return same ? d.toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false})
                  : d.toLocaleString('en-US',{weekday:'short',hour:'2-digit',minute:'2-digit',hour12:false});
    };
    const laneName = n => n === 'Emergent' ? 'Emergent / off plan' : n;
    const laneColor = (L,i) => EMERGENT.includes(L.name) ? '#f2c96b' : COLORS[i % COLORS.length];
    const sum = a => a.reduce((x,y)=>x+y,0);
    function smooth(a){ return a.map((_,i)=>{ let s=0,n=0; for (let j=i-2;j<=i+2;j++){ if (j>=0 && j<a.length){ const w=3-Math.abs(i-j); s+=a[j]*w; n+=w; } } return s/n; }); }

    const AGENT_COLORS = ['#78a7ff','#49d69a','#b993ff','#66d6e7','#ff8fc7','#f29e6b','#9fd36b','#f2c96b','#8d96a6','#ff7676'];
    const agentList = [...new Set(D.task.flatMap(L => L.branches.map(b => b.key)))];
    const agentColor = a => {
      if (a === '__rest') return '#858d9a';
      if (!agentList.includes(a)) agentList.push(a);
      return AGENT_COLORS[agentList.indexOf(a) % AGENT_COLORS.length];
    };

    function renderNow(){
      const box = q('#now'); if (!box) return;
      const now = D.now || [];
      const at = hDate(H-1).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false});
      box.innerHTML = `<span class="k">Now, ${at}</span>` + (now.length ? now.map(n => `
        <div class="chip" data-tid="${n.tid || ''}" title="${esc(n.title || '')}">
          <span class="dot ${n.age < 10 ? 'live' : ''}" style="background:${agentColor(n.a)}"></span>
          <span class="a" style="color:${agentColor(n.a)}">${esc(n.a)}</span>
          <span class="t">${n.tid ? `#${n.tid} ${esc(n.title)}` : 'outside tasks'}</span>
          <span class="ago">${n.age < 1 ? 'just now' : Math.round(n.age) + ' min'}</span></div>`).join('')
        : `<span class="t" style="color:var(--muted)">no agent activity in the last hour</span>`);
      box.querySelectorAll('.chip').forEach(c => c.onclick = () => {
        const tid = c.dataset.tid; if (!tid) return;
        mode = 'task'; sel = D.task.some(L => L.name === '#' + tid) ? '#' + tid : null;
        qa('.mode button').forEach(x => x.classList.toggle('active', x.dataset.mode === 'task'));
        try{ history.replaceState(null, '', snPath() + '#task'); } catch(e){}
        renderAll();
        const el = sel && q(`.lane[data-l="${sel}"]`);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    }

    function shares(ls){
      if (!ls) ls = lanes();
      const hs = ls.map(laneHours);
      const now = hs.map(a => sum(a.slice(H - 24))), base = hs.map(a => sum(a.slice(0, H - 24)));
      const tn = sum(now) || 1, tb = sum(base) || 1;
      const hasPlan = ls === D.project && ls.some(L => plan[L.name] > 0);
      return ls.map((L,i) => {
        const actual = now[i]/tn*100, usual = base[i]/tb*100;
        const p = hasPlan ? (+plan[L.name] || 0) : null;
        return { L, i, actual, usual, ref: p ?? usual, refLabel: p !== null ? 'plan' : 'usual', minNow: now[i], minBase: base[i] };
      });
    }
    function lanes(){ return D[mode]; }
    function laneHours(L){ const out = new Array(H).fill(0); L.branches.forEach(b => b.h.forEach((v,i) => out[i] += v)); return out; }
    const branchColor = (L,li,b) => mode === 'task' ? agentColor(b.key) : laneColor(L, li);

    function renderSignals(){
      const sh = shares(D.project);
      const drift = [...sh].sort((a,b) => (b.actual-b.ref) - (a.actual-a.ref))[0];
      const active = [...sh].filter(s => !EMERGENT.includes(s.L.name)).sort((a,b) => b.L.total - a.L.total)[0];
      const handoffs = L => D.feed.filter(f => f.kind === 'message' && f.h >= H - scope && f.proj === L.name).length;
      const stall = [...sh].filter(s => s.usual >= 3).sort((a,b) => (a.actual/a.usual) - (b.actual/b.usual))[0];
      let br = null;
      const ls = D.project;
      const tn = sum(ls.map(L => sum(laneHours(L).slice(H - 24)))) || 1;
      const tb = sum(ls.map(L => sum(laneHours(L).slice(0, H - 24)))) || 1;
      ls.forEach(L => L.branches.forEach(b => {
        if (b.key === '__rest') return;
        const n = sum(b.h.slice(H - 24))/tn*100, o = sum(b.h.slice(0, H - 24))/tb*100;
        if (o < 3 && (!br || n - o > br.d)) br = { L, b, n, o, d: n - o };
      }));
      const nodesIn = L => L.nodes.filter(n => n.h >= H - scope).length;
      const cards = [
        drift && drift.actual - drift.ref > 5
          ? `<div class="signal drift"><div class="k">Drift</div><div class="v">${esc(laneName(drift.L.name))} is taking attention</div><div class="mini">actual ${drift.actual.toFixed(0)}% today vs ${drift.refLabel} ${drift.ref.toFixed(0)}%${stall ? `, ${esc(laneName(stall.L.name))} slowed` : ''}</div></div>`
          : `<div class="signal"><div class="k">Drift</div><div class="v">Share matches plan</div><div class="mini">no direction drifted by more than 5 pp</div></div>`,
        active ? `<div class="signal"><div class="k">Most active</div><div class="v">${esc(laneName(active.L.name))}</div><div class="mini">${Math.round(active.L.total/60)} h of work over 7d, ${nodesIn(active.L)} task events, ${handoffs(active.L)} messages between agents</div></div>`
               : `<div class="signal"><div class="k">Most active</div><div class="v">none</div></div>`,
        stall ? `<div class="signal"><div class="k">Stalling</div><div class="v">${esc(laneName(stall.L.name))}</div><div class="mini">${stall.minNow < 1 ? 'stalled: zero today' : `down ${Math.round((1 - stall.actual/stall.usual)*100)}% today`}</div></div>`
              : `<div class="signal"><div class="k">Stalling</div><div class="v">no one</div></div>`,
        br ? `<div class="signal"><div class="k">Unexpected branch</div><div class="v" title="${esc(br.b.name)}">${esc(br.b.name)}</div><div class="mini">${esc(laneName(br.L.name))}, grew from ${br.o.toFixed(0)}% to ${br.n.toFixed(0)}%</div></div>`
           : `<div class="signal"><div class="k">Unexpected branch</div><div class="v">none</div></div>`,
      ];
      const el = q('#signals'); if (el) el.innerHTML = cards.join('');
    }

    function renderAxis(){
      const ax = q('#axis'); if (!ax) return;
      if (mode === 'task'){
        ax.innerHTML = `<span style="left:12px;transform:none">Gantt: row = participant, time left to right, segment = one agent turn, coloured arrow = handed off by message, grey arrow = picked up on its own, dashed = pause over 30 min, hover a segment to read the message</span>`;
        const lh = q('#lefthead'); if (lh) lh.textContent = 'TASK';
        return;
      }
      const ticks = scope === 168
        ? [[0,'7d ago'],[1/7,'6d'],[2/7,'5d'],[3/7,'4d'],[4/7,'3d'],[5/7,'2d'],[6/7,'24h']]
        : [[0,'24h ago'],[.25,'18h'],[.5,'12h'],[.75,'6h']];
      ax.innerHTML = ticks.map(([f,t]) => `<span style="left:${f*100}%">${t}</span>`).join('') + `<span style="left:auto;right:8px;transform:none;color:#c5ccd7">now</span>`;
      const lh = q('#lefthead'); if (lh) lh.textContent = { project:'DIRECTION', agent:'AGENT', task:'TASK' }[mode];
    }

    const tip = q('#tip');
    function showTip(ev, html){
      if (!tip) return;
      tip.innerHTML = html;
      tip.style.display = 'block';
      tip.style.left = Math.min(ev.clientX + 14, innerWidth - 360) + 'px';
      tip.style.top  = (ev.clientY + 14) + 'px';
    }
    const hideTip = () => { if (tip) tip.style.display = 'none'; };
    const KIND = { created:['new task','#ffffff'], done:['closed','#49d69a'], pending_review:['under review','#f2c96b'], blocked:['blocked','#ff7676'], rejected:['rejected','#858d9a'] };

    function renderLanes(){
      const sh = shares(); const host = q('#lanes'); if (!host) return;
      const maxV = Math.max(1, ...lanes().flatMap(L => L.branches.flatMap(b => smooth(b.h).slice(H - scope))));
      host.innerHTML = sh.map(s => {
        const L = s.L, c = laneColor(L, s.i), diff = s.actual - s.ref;
        const cls = diff > 5 ? 'hot' : diff < -5 ? 'cold' : '';
        const meta = L.meta || (mode === 'project' ? (L.who.length ? 'agents: ' + L.who.join(', ') : '') : (L.who.length ? 'projects: ' + L.who.slice(0,3).join(', ') : ''));
        const title = L.title ? `${L.name} ${L.title.length > 70 ? L.title.slice(0,68) + '...' : L.title}` : laneName(L.name);
        if (mode === 'task'){
          const lo = H - scope, per = L.branches.map(b => [b, sum(b.h.slice(lo))]), tot = sum(per.map(p => p[1])) || 1;
          const day = sum(L.branches.map(b => sum(b.h.slice(H - 24))));
          return `<div class="lane ${sel === L.name ? 'sel' : sel ? 'dim' : ''}" data-l="${esc(L.name)}">
          <div class="label"><div class="pname" title="${esc(L.title || '')}">${esc(title)}</div><div class="pmeta">${esc(meta)}</div>
            ${L.last != null ? (H - 1 - L.last <= 0.25
              ? `<span class="livebadge">live, ${esc(L.relay && L.relay.steps.length ? L.relay.steps[L.relay.steps.length - 1].a : '')}</span>`
              : `<span class="livebadge old">last ${dur(H - 1 - L.last)} ago</span>`) : ''}
            <div class="stack">${per.map(([b,m]) => `<i title="${esc(b.name)}: ${Math.round(m)} min" style="width:${m/tot*100}%;background:${agentColor(b.key)}"></i>`).join('')}</div>
            <div class="shareline"><span>${scope === 168 ? '7d' : '24h'}: ${(tot/60).toFixed(1)} h</span><span>today ${(day/60).toFixed(1)} h</span></div></div>
          <div class="flow" data-i="${s.i}"></div></div>`;
        }
        return `<div class="lane ${sel === L.name ? 'sel' : sel ? 'dim' : ''}" data-l="${esc(L.name)}">
          <div class="label"><div class="pname" title="${esc(L.title || '')}">${esc(title)}</div><div class="pmeta">${esc(meta)}</div>
            <div class="share"><div style="width:${Math.min(100, s.actual)}%;background:${c}"></div><i style="left:${Math.min(99, s.ref)}%"></i></div>
            <div class="shareline"><span class="${cls}">actual today ${s.actual.toFixed(0)}%</span><span>${s.refLabel} ${s.ref.toFixed(0)}%</span></div></div>
          <div class="flow" data-i="${s.i}"></div></div>`;
      }).join('');
      host.querySelectorAll('.lane').forEach(el => el.onclick = () => {
        sel = sel === el.dataset.l ? null : el.dataset.l;
        renderLanes(); renderFeed(); if (mode === 'task') renderDrift();
      });
      host.querySelectorAll('.flow').forEach(fl => drawLane(fl, lanes()[+fl.dataset.i], +fl.dataset.i, maxV));
    }

    const ST_EN = { in_progress:'to in progress', pending_review:'under review', done:'closed', blocked:'blocked', rejected:'rejected', awaiting_approval:'awaiting approval', open:'opened', escalated:'escalated' };
    const ST_COL = { done:'#49d69a', pending_review:'#f2c96b', blocked:'#ff7676', rejected:'#858d9a', in_progress:'#dce4f1' };
    const hm = h => hDate(h).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false});
    const dur = h => h < 1 ? Math.round(h*60) + ' min' : h < 48 ? (Math.round(h*10)/10) + ' h' : Math.round(h/24) + ' d';

    function drawGantt(fl, L){
      const lo = H - scope, st = (L.relay && L.relay.steps ? L.relay.steps : []).filter(s => s.e >= lo);
      if (!st.length){ fl.innerHTML = `<div class="tag" style="left:12px;top:12px">nothing done on this task in this window</div>`; return; }
      const rows = [...new Set(st.map(s => s.a))].sort((a,b) => (a === 'admin') ? -1 : (b === 'admin') ? 1 : 0);
      const ROW = 30, TOP = 26, LEFT = 74, RIGHT = 18;
      const HT = TOP + rows.length * ROW + 26; fl.style.minHeight = HT + 'px';
      const W = fl.clientWidth, avail = W - LEFT - RIGHT;
      const MIN = Math.min(40, avail/st.length * 0.6), CAP = 0.5;
      const gaps = st.map((s,k) => k === 0 ? 0 : Math.min(Math.max(0, s.s - st[k-1].e), CAP));
      const durs = st.map(s => Math.min(s.e - s.s, CAP));
      const tot = Math.max(sum(gaps) + sum(durs), 1), spare = Math.max(0, avail - MIN * st.length);
      const pos = []; let cx = LEFT;
      st.forEach((s,k) => { cx += spare * gaps[k] / tot; const w = MIN * 0.7 + spare * durs[k] / tot; pos.push([cx, w]); cx += Math.max(w, MIN); });
      const y = a => TOP + rows.indexOf(a) * ROW + ROW/2;
      const col = a => a === 'admin' ? '#f3f5f7' : agentColor(a);
      const svg = d3.select(fl).append('svg').attr('viewBox', `0 0 ${W} ${HT}`);
      const defs = svg.append('defs');
      rows.forEach(a => {
        defs.append('marker').attr('id', `gm-${L.name.slice(1)}-${a}`).attr('viewBox', '0 0 10 10').attr('refX', 9).attr('refY', 5)
          .attr('markerWidth', 5).attr('markerHeight', 5).attr('orient', 'auto').append('path').attr('d', 'M0,0 L10,5 L0,10 z').attr('fill', col(a));
        svg.append('line').attr('x1', LEFT).attr('x2', W - RIGHT).attr('y1', y(a)).attr('y2', y(a)).attr('stroke', 'rgba(255,255,255,.05)');
        svg.append('text').attr('x', 8).attr('y', y(a) + 4).attr('fill', col(a)).attr('font-size', 11).attr('font-weight', 600).text(a === 'admin' ? 'you' : a);
      });
      const placed = [];
      const label = (lx, ly, text, color) => {
        const w = text.length * 5;
        if (placed.some(p => Math.abs(p[1] - ly) < 10 && lx < p[0] + p[2] + 4 && lx + w + 4 > p[0])) return;
        placed.push([lx, ly, w]);
        svg.append('text').attr('x', lx).attr('y', ly).attr('fill', color).attr('font-size', 9).text(text);
      };
      st.forEach((s,k) => {
        const [x, w] = pos[k], yy = y(s.a);
        if (k < st.length - 1){
          const n = st[k+1], [nx] = pos[k+1], ny = y(n.a), gap = n.s - s.e;
          const handed = s.msgs.some(m => m.to === n.a), c = handed ? col(s.a) : '#4a5260';
          const mid = x + w + Math.max(4, (nx - x - w) / 2);
          const d = ny === yy ? `M${x+w},${yy} H${nx-2}` : `M${x+w},${yy} H${mid} V${ny} H${nx-2}`;
          svg.append('path').attr('d', d).attr('fill', 'none').attr('stroke', c).attr('stroke-width', handed ? 1.6 : 1)
            .attr('stroke-dasharray', gap > CAP ? '3 3' : null).attr('marker-end', `url(#gm-${L.name.slice(1)}-${handed ? s.a : n.a})`);
          if (gap > CAP) label(mid + 3, Math.min(yy, ny) + (yy === ny ? -6 : 12), 'pause ' + dur(gap), '#f2c96b');
        }
        svg.append('rect').attr('x', x).attr('y', yy - 6).attr('width', w).attr('height', 12).attr('rx', 3).attr('fill', col(s.a))
          .style('cursor', 'help')
          .on('mousemove', ev => showTip(ev, `<b>${esc(s.a === 'admin' ? 'you' : s.a)}</b>, ${hm(s.s)}${s.e - s.s > 1/60 ? ' to ' + hm(s.e) : ''}<br>${esc(s.what || 'action')}` +
            s.msgs.map(m => `<br><br><b>to ${esc(m.to)}</b>: ${esc(m.text)}`).join('')))
          .on('mouseleave', hideTip);
        const stx = (s.what || '').split(', ').find(t => /under review|closed|to in progress|blocked|rejected/.test(t));
        if (stx) label(x, yy - 10, stx, '#f2c96b');
      });
      st.forEach((s,k) => { if (k === 0 || s.s - st[k-1].e > CAP) svg.append('text').attr('x', pos[k][0]).attr('y', HT - 8).attr('fill', '#636b77').attr('font-size', 10).text(hm(s.s)); });
      const last = st.length - 1;
      svg.append('text').attr('x', pos[last][0]).attr('y', HT - 8).attr('fill', '#636b77').attr('font-size', 10).text(hm(st[last].s));
    }

    function drawLane(fl, L, li, maxV){
      if (mode === 'task') return drawGantt(fl, L);
      const W = fl.clientWidth, HT = Math.max(150, fl.clientHeight), c = laneColor(L, li);
      const lo = H - scope, x = d3.scaleLinear().domain([lo, H - 1]).range([12, W - 40]);
      const nb = L.branches.length, rowY = k => 30 + (HT - 60) * (nb === 1 ? 0.5 : k / (nb - 1));
      const svg = d3.select(fl).append('svg').attr('viewBox', `0 0 ${W} ${HT}`);
      const defs = svg.append('defs');
      const gid = `g${mode}${li}`;
      const gr = defs.append('linearGradient').attr('id', gid).attr('x1', '0').attr('x2', '1');
      gr.append('stop').attr('offset', '0').attr('stop-color', c).attr('stop-opacity', .3);
      gr.append('stop').attr('offset', '1').attr('stop-color', c).attr('stop-opacity', 1);
      const glow = defs.append('filter').attr('id', 'gl' + gid).attr('x', '-20%').attr('y', '-200%').attr('width', '140%').attr('height', '500%');
      glow.append('feGaussianBlur').attr('stdDeviation', 3).attr('result', 'b');
      const mg = glow.append('feMerge'); mg.append('feMergeNode').attr('in', 'b'); mg.append('feMergeNode').attr('in', 'SourceGraphic');
      if (scope === 168) for (let d = 1; d < 7; d++) svg.append('line').attr('x1', x(lo + d*24)).attr('x2', x(lo + d*24)).attr('y1', 0).attr('y2', HT).attr('stroke', 'rgba(255,255,255,.035)');
      if (scope === 168) svg.append('line').attr('x1', x(H - 24)).attr('x2', x(H - 24)).attr('y1', 0).attr('y2', HT).attr('stroke', 'rgba(242,201,107,.28)').attr('stroke-dasharray', '3 4');
      svg.append('line').attr('x1', x(H - 1)).attr('x2', x(H - 1)).attr('y1', 0).attr('y2', HT).attr('stroke', 'rgba(120,167,255,.45)');

      const bY = {};
      L.branches.forEach((b,k) => {
        const y = rowY(k); bY[b.key] = y;
        const bc = branchColor(L, li, b), bg = `${gid}b${k}`;
        const g2 = defs.append('linearGradient').attr('id', bg).attr('x1', '0').attr('x2', '1');
        g2.append('stop').attr('offset', '0').attr('stop-color', bc).attr('stop-opacity', .3);
        g2.append('stop').attr('offset', '1').attr('stop-color', bc).attr('stop-opacity', 1);
        const sm = smooth(b.h), pts = d3.range(lo, H).map(i => [i, sm[i]]);
        const active = pts.filter(p => p[1] > 0.05);
        if (active.length){
          svg.append('line').attr('x1', x(active[0][0])).attr('x2', x(active[active.length - 1][0])).attr('y1', y).attr('y2', y).attr('stroke', bc).attr('stroke-opacity', .18).attr('stroke-width', 1.5);
        }
        const w = v => v <= 0.05 ? 0 : 2 + 12 * Math.sqrt(v / maxV);
        const area = d3.area().curve(d3.curveBasis).x(p => x(p[0])).y0(p => y - w(p[1])/2).y1(p => y + w(p[1])/2);
        svg.append('path').attr('d', area(pts)).attr('fill', `url(#${bg})`).attr('filter', `url(#gl${gid})`)
          .attr('opacity', b.key === '__rest' ? .45 : 1)
          .on('mousemove', ev => {
            const i = Math.round(x.invert(d3.pointer(ev, svg.node())[0]));
            showTip(ev, `<b>${esc(b.name)}</b><br>${whenStr(i)}: ${b.h[i] ? b.h[i].toFixed(0) : 0} min of work<br>total in window: ${Math.round(sum(b.h.slice(lo)))} min${b.top && b.top.length ? `<br><br>${b.top.map(esc).join('<br>')}` : ''}`);
          })
          .on('mouseleave', hideTip);
        svg.append('text').attr('x', active.length ? Math.max(4, x(active[0][0])) : 4).attr('y', y - 11).attr('fill', '#636b77').attr('font-size', 10)
          .text(b.name.length > 58 ? b.name.slice(0, 56) + '...' : b.name);
      });
      const ns = L.nodes.filter(n => n.h >= lo);
      svg.selectAll('circle.n').data(ns).join('circle').attr('class', 'n')
        .attr('cx', n => x(n.h)).attr('cy', n => bY[n.b] ?? bY['__rest'] ?? rowY(0))
        .attr('r', n => n.kind === 'created' ? 3 : 4.5).attr('fill', n => n.kind === 'created' ? '#0e1014' : KIND[n.kind][1])
        .attr('stroke', n => n.kind === 'created' ? '#dce4f1' : '#0e1014').attr('stroke-width', n => n.kind === 'created' ? 1.4 : 2)
        .on('mousemove', (ev, n) => showTip(ev, `<b>#${n.tid}</b> ${esc(n.title)}<br>${KIND[n.kind][0]}, ${esc(n.actor || '?')}, ${whenStr(n.h)}`))
        .on('mouseleave', hideTip);
      const recent = ns.filter(n => n.h >= H - 24);
      const pick = [recent.filter(n => n.kind === 'blocked').pop(), recent.filter(n => n.kind === 'done').pop()].filter(Boolean);
      let prevTop = -99;
      pick.forEach((n,k) => {
        const t = document.createElement('div');
        t.className = 'tag' + (n.kind === 'blocked' ? ' bad' : '');
        t.innerHTML = mode === 'task'
          ? `<b>${KIND[n.kind][0]}</b>, ${esc(n.actor || '?')}, ${whenStr(n.h)}`
          : `<b>#${n.tid}</b> ${esc(n.title.length > 42 ? n.title.slice(0, 40) + '...' : n.title)}`;
        const cx = x(n.h), cy = (bY[n.b] ?? rowY(0));
        let top = k === 0 ? Math.max(2, cy - 34) : Math.min(HT - 26, cy + 10);
        if (Math.abs(top - prevTop) < 26) top = prevTop + 26;
        prevTop = top;
        t.style.top = top + 'px';
        t.style.left = Math.max(0, Math.min(cx - 60, W - 270)) + 'px';
        fl.appendChild(t);
      });
    }

    function renderEffort(box){
      const lo = H - scope, per = {};
      D.task.forEach(L => L.branches.forEach(b => { if (b.key === '__rest') return; per[b.key] = (per[b.key] || 0) + sum(b.h.slice(lo)); }));
      const rows = Object.entries(per).sort((a,b) => b[1] - a[1]), max = Math.max(1, ...rows.map(r => r[1]));
      const sl = sel ? D.task.find(L => L.name === sel) : null;
      const selRows = sl ? sl.branches.map(b => [b.name, sum(b.h.slice(lo)), b.key]).sort((a,b) => b[1] - a[1]) : null;
      box.innerHTML = `<div class="compare">
        ${sl ? `<div class="note" style="margin:0 0 10px">${esc(sl.name)} ${esc(sl.title)}</div>` + selRows.map(([n,m,k]) => `<div class="effrow"><span>${esc(n)}</span><div class="bar"><i style="width:${m / Math.max(1, selRows[0][1]) * 100}%;background:${agentColor(k)}"></i></div><span style="text-align:right">${Math.round(m)} min</span></div>`).join('') + `<div class="note" style="margin-bottom:14px">click the task again to clear</div>` : ''}
        <div class="note" style="margin:0 0 10px">all tasks on screen, ${scope === 168 ? '7 days' : '24 hours'}</div>
        ${rows.map(([a,m]) => `<div class="effrow"><span>${esc(a)}</span><div class="bar"><i style="width:${m / max * 100}%;background:${agentColor(a)}"></i></div><span style="text-align:right">${(m/60).toFixed(1)} h</span></div>`).join('')}
      </div>`;
    }

    function renderDrift(){
      const box = q('#drift'); if (!box) return;
      const planbtn = q('#planbtn'); if (planbtn) planbtn.style.display = mode === 'project' ? '' : 'none';
      const head = q('#drift'); if (head && head.previousElementSibling){ const h3 = head.previousElementSibling.querySelector('h3'); if (h3) h3.textContent = mode === 'task' ? 'EFFORT BY AGENT' : 'ATTENTION DRIFT'; }
      if (mode === 'task'){ renderEffort(box); return; }
      const sh = shares();
      const worst = [...sh].sort((a,b) => (b.actual - b.ref) - (a.actual - a.ref));
      const up = worst[0], down = worst[worst.length - 1];
      const hasPlan = sh[0] && sh[0].refLabel === 'plan';
      const alert = up && up.actual - up.ref > 5
        ? `<div class="alert"><div class="t">${hasPlan ? 'Work is drifting from your plan' : 'Work has drifted from the usual share'}</div><div class="d">Today ${esc(laneName(up.L.name))} ${up.actual.toFixed(0)}% vs ${up.refLabel} ${up.ref.toFixed(0)}%; biggest drop ${esc(laneName(down.L.name))}: ${down.actual.toFixed(0)}% vs ${down.ref.toFixed(0)}%.${hasPlan ? '' : ' No plan set, compared to the last 6 days.'}</div></div>`
        : `<div class="alert ok"><div class="t">Share within range</div><div class="d">No direction drifted more than 5 pp from ${hasPlan ? 'plan' : 'usual'}.</div></div>`;
      const max = Math.max(1, ...sh.map(s => Math.max(s.ref, s.actual)));
      const bars = sh.map(s => `<div class="barrow"><div class="bartop"><span>${esc(laneName(s.L.name))}</span><span>${s.ref.toFixed(0)}% to ${s.actual.toFixed(0)}%</span></div>
         <div class="bars"><div class="bar"><i style="width:${s.ref / max * 100}%"></i></div><div class="bar"><i style="width:${s.actual / max * 100}%;background:${laneColor(s.L, s.i)}"></i></div></div></div>`).join('');
      const editor = planOpen && mode === 'project' ? `<div class="compare" style="border-top:1px solid var(--line)"><div class="note">Your plan: the share of work you want to see in each direction. Stored only in this browser.</div>
         ${sh.map(s => `<div class="planrow"><span>${esc(laneName(s.L.name))}</span><span><input type="number" min="0" max="100" data-p="${esc(s.L.name)}" value="${plan[s.L.name] ?? ''}"> %</span></div>`).join('')}
         <button class="smallbtn" id="planclear">clear plan</button></div>` : '';
      box.innerHTML = alert + `<div class="compare">${bars}<div class="note">left: ${hasPlan ? 'plan' : 'usual share over 6 days'}; right: actual over the last day</div></div>` + editor;
      box.querySelectorAll('input[data-p]').forEach(inp => inp.onchange = () => {
        const v = parseFloat(inp.value); if (isNaN(v)) delete plan[inp.dataset.p]; else plan[inp.dataset.p] = v;
        savePlan(); renderAll();
      });
      const clr = q('#planclear'); if (clr) clr.onclick = () => { plan = {}; savePlan(); renderAll(); };
    }

    function renderFeed(){
      const box = q('#feed'); if (!box) return;
      const qlow = query.toLowerCase();
      let items = feedMode === 'raw' ? D.raw : D.feed;
      items = items.filter(f => f.h >= H - scope);
      if (feedMode !== 'raw' && sel && mode === 'project'){
        const L = lanes().find(l => l.name === sel); const names = new Set([sel]);
        if (sel === 'Emergent') items = items.filter(f => !lanes().some(l => l.name !== 'Emergent' && l.name === f.proj));
        else items = items.filter(f => names.has(f.proj));
      }
      if (feedMode !== 'raw' && sel && mode === 'task'){ const id = +sel.slice(1); items = items.filter(f => (f.tids || []).includes(id)); }
      if (feedMode !== 'raw' && sel && mode === 'agent') items = items.filter(f => (f.who || '').split(' ').includes(sel) || f.who === sel);
      if (qlow) items = items.filter(f => (f.who + ' ' + f.text + ' ' + (f.proj || '')).toLowerCase().includes(qlow));
      box.innerHTML = items.slice(0, 60).map(f => `<div class="event"><div class="etop"><div class="who">${esc(f.who).replace(' -&gt; ', ' to ')}</div><div class="when">${whenStr(f.h)}</div></div>
        <div class="edesc">${esc(f.text)}${f.proj ? ` <span class="p">, ${esc(f.proj)}</span>` : ''}</div></div>`).join('') || `<div class="event"><div class="edesc">nothing</div></div>`;
    }

    function renderMethodNotes(){
      const el = q('#methodnotes'); if (!el) return;
      const notes = (D.attribution && D.attribution.notes) || [];
      if (!notes.length){ el.textContent = ''; return; }
      el.innerHTML = '<br>' + notes.map(n => esc(n)).join('<br>');
    }

    function renderAll(){
      const subEl = q('#sub');
      if (subEl) subEl.textContent = `Structure derived from the node's event feed, not from the task tree. ${D.events} events. ${hDate(0).toLocaleDateString('en-US')} to ${hDate(H - 1).toLocaleString('en-US',{dateStyle:'short',timeStyle:'short'})}`;
      renderNow(); renderSignals(); renderAxis(); renderLanes(); renderDrift(); renderFeed(); renderMethodNotes();
    }

    // What these buttons change is the board, which on a phone sits below the title, the signals and the cards:
    // the press looked like it did nothing. So the board is brought to the top of the screen after a change.
    function showBoard(){
      const m = q('.map'), r = root(); if (!m || !r) return;
      if (m.getBoundingClientRect().top > window.innerHeight * 0.5) m.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    qa('.mode button').forEach(b => b.onclick = () => {
      mode = b.dataset.mode; sel = null;
      qa('.mode button').forEach(x => x.classList.toggle('active', x === b));
      try{ history.replaceState(null, '', snPath() + '#' + b.dataset.mode); } catch(e){}
      renderAll();
      showBoard();
    });
    qa('.scope button[data-scope]').forEach(b => b.onclick = () => {
      scope = +b.dataset.scope;
      qa('.scope button').forEach(x => x.classList.toggle('active', x === b));
      renderAll();
      showBoard();
    });
    qa('[data-feed]').forEach(b => b.onclick = () => {
      feedMode = b.dataset.feed;
      qa('[data-feed]').forEach(x => x.classList.toggle('active', x === b));
      renderFeed();
    });
    const planbtn = q('#planbtn'); if (planbtn) planbtn.onclick = () => { planOpen = !planOpen; renderDrift(); };
    const qin = q('#q'); if (qin) qin.oninput = e => { query = e.target.value; renderFeed(); };

    _wmResize = () => renderLanes();
    window.addEventListener('resize', _wmResize);

    // open with the hash the standalone page understood (#task / #agent),
    // so a bookmark of /work-map#task keeps working.
    const startMode = location.hash.slice(1);
    if (['project','agent','task'].includes(startMode)){
      mode = startMode;
      qa('.mode button').forEach(x => x.classList.toggle('active', x.dataset.mode === startMode));
    }
    renderAll();
    _wmStarting = false;
  }

  function teardownWorkMap(opts){
    if (_wmResize){ window.removeEventListener('resize', _wmResize); _wmResize = null; }
    if (_wmAbort){ try{ _wmAbort.abort(); } catch(e){} _wmAbort = null; }
    const tip = document.querySelector('#workMapView #tip'); if (tip) tip.style.display = 'none';
    if (!opts || !opts.keepDom){ /* leave DOM as-is: the shell markup is static */ }
  }

  // The map's loading state: its own layout (title, now bar, four signal cards, the map and its side cards) shimmering,
// so the screen does not jump when the data lands.
function wmSkeleton(){
  const b = (c, n = 1) => `<div class="skel ${c}"></div>`.repeat(n);
  return `<div class="wsk" role="status" aria-busy="true" aria-label="Loading">`
    + `<div class="wsk__hero">${b('wsk__h1')}${b('wsk__sub')}</div>${b('wsk__now')}`
    + `<div class="wsk__signals">${b('wsk__sig', 4)}</div>`
    + `<div class="wsk__board">${b('wsk__map')}<div class="wsk__side">${b('wsk__card', 3)}</div></div></div>`;
}
window.renderWorkMap   = renderWorkMap;
  window.teardownWorkMap = teardownWorkMap;
})();
