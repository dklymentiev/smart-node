import * as THREE from 'three';
import { state } from './state.js';
import { API, SEARCH_TRANS_MS, wsHeaders } from './constants.js';
import { scene } from './scene.js';
import { getCategoryId, getCatColor, getCatName, getProjectGuid } from './taxonomy.js';
import { animateCamera } from './views.js';
import { highlightDoc, showPinnedTooltipCentered } from './tooltip.js';

const mapQ = document.getElementById('map-q');
const searchStatus = document.getElementById('search-status');
const sidebar = document.getElementById('search-sidebar');
const sidebarList = document.getElementById('sidebar-list');
const sidebarTitle = document.getElementById('sidebar-title');
const searchTimerEl = document.getElementById('search-timer');
const searchTimerMs = searchTimerEl.querySelector('.st-ms');
let searchTimerInterval = null;
let searchDebounce = null;
const semanticCache = new Map(); // key: "query|typeTag" -> { results, ts }

function setSearchMode(mode) {
  state.searchMode = mode;
  document.querySelectorAll('#search-switch .sw-opt').forEach(el => {
    el.classList.toggle('active', el.dataset.mode === mode);
  });
}

function getSelectedType() {
  const v = document.getElementById('ctl-type').value;
  return v && v !== 'all' ? 'type:' + v : null;
}

function startSearchTimer() {
  const t0 = performance.now();
  searchTimerEl.classList.add('active');
  searchTimerEl.classList.remove('done');
  searchTimerMs.textContent = '0 ms';
  clearInterval(searchTimerInterval);
  searchTimerInterval = setInterval(() => {
    searchTimerMs.textContent = Math.round(performance.now() - t0) + ' ms';
  }, 47);
  return t0;
}

function stopSearchTimer(t0) {
  clearInterval(searchTimerInterval);
  const elapsed = Math.round(performance.now() - t0);
  searchTimerMs.textContent = elapsed + ' ms';
  searchTimerEl.classList.remove('active');
  searchTimerEl.classList.add('done');
  searchTimerEl.style.display = 'flex';
  setTimeout(() => {
    if (searchTimerEl.classList.contains('done')) {
      searchTimerEl.style.display = '';
      searchTimerEl.classList.remove('done');
    }
  }, 5000);
  return elapsed;
}

function hideSearchTimer() {
  clearInterval(searchTimerInterval);
  searchTimerEl.classList.remove('active', 'done');
  searchTimerEl.style.display = '';
}

function clearSelection() {
  if (state.selectedRing) { scene.remove(state.selectedRing); state.selectedRing = null; }
  if (state.selectedIdx >= 0 && state.particleSystem && state.originalColors) {
    const colors = state.particleSystem.geometry.attributes.color;
    colors.array[state.selectedIdx*3]   = state.originalColors[state.selectedIdx*3];
    colors.array[state.selectedIdx*3+1] = state.originalColors[state.selectedIdx*3+1];
    colors.array[state.selectedIdx*3+2] = state.originalColors[state.selectedIdx*3+2];
    colors.needsUpdate = true;
  }
  state.selectedIdx = -1;
}

function clearSearchHighlight() {
  searchStatus.textContent = '';
  state.searchHighlight = null;
  sidebar.classList.remove('open');
  clearSelection();
  hideSearchTimer();

  if (!state.particleSystem) return;

  // Restore colors
  if (state.originalColors) {
    const colors = state.particleSystem.geometry.attributes.color;
    colors.array.set(state.originalColors);
    colors.needsUpdate = true;
    state.originalColors = null;
  }

  // Animate positions back
  if (state.preSearchPositions) {
    const positions = state.particleSystem.geometry.attributes.position.array;
    state.searchTransFrom = new Float32Array(positions);
    state.searchTransTo = state.preSearchPositions;
    state.searchTransStart = performance.now();
    state.searchTransitioning = true;
    state.preSearchPositions = null;
  }

  state.searchPositions = null;

  // Restore visibility and fly camera back to default
  if (state.viewMode === 'galaxy') {
    state.nebulaeMeshes.forEach(m => { m.visible = true; });
    state.chainLines.forEach(m => { m.visible = true; });
    document.getElementById('cluster-labels').style.display = '';
    animateCamera(new THREE.Vector3(0, 200, 750), new THREE.Vector3(0, 0, 0));
  }
}

function focusSearchResults(indices) {
  if (!state.particleSystem) return;
  const positions = state.particleSystem.geometry.attributes.position.array;
  const colors = state.particleSystem.geometry.attributes.color;
  const colArr = colors.array;

  // Save originals on first search
  if (!state.originalColors) state.originalColors = new Float32Array(colArr);
  if (!state.preSearchPositions) state.preSearchPositions = new Float32Array(positions);

  state.searchHighlight = new Set(indices);

  // Build relevance rank map
  const rankMap = {};
  indices.forEach((docIdx, k) => { rankMap[docIdx] = k / Math.max(indices.length - 1, 1); });

  // Color: matches bright-to-dim by relevance, others invisible
  for (let i = 0; i < state.allDocs.length; i++) {
    if (state.searchHighlight.has(i)) {
      const brightness = 1.0 - rankMap[i] * 0.65;
      colArr[i*3] = brightness; colArr[i*3+1] = brightness; colArr[i*3+2] = brightness;
    } else {
      colArr[i*3] = state.originalColors[i*3] * 0.03;
      colArr[i*3+1] = state.originalColors[i*3+1] * 0.03;
      colArr[i*3+2] = state.originalColors[i*3+2] * 0.03;
    }
  }
  colors.needsUpdate = true;

  // Hide nebulae, chain lines, labels during search
  state.nebulaeMeshes.forEach(m => { m.visible = false; });
  state.chainLines.forEach(m => { m.visible = false; });
  document.getElementById('cluster-labels').style.display = 'none';

  // Build search cluster positions
  state.searchPositions = new Float32Array(positions.length);
  for (let i = 0; i < state.allDocs.length; i++) {
    if (!state.searchHighlight.has(i)) {
      state.searchPositions[i*3] = positions[i*3];
      state.searchPositions[i*3+1] = positions[i*3+1];
      state.searchPositions[i*3+2] = 5000;
    }
  }

  // Group results by project guid
  const searchByProject = {};
  const soloResults = [];
  indices.forEach((docIdx, rank) => {
    const pg = getProjectGuid(state.allDocs[docIdx]);
    if (pg) {
      if (!searchByProject[pg]) searchByProject[pg] = [];
      searchByProject[pg].push({ docIdx, rank });
    } else {
      soloResults.push({ docIdx, rank });
    }
  });

  const projectGroups = [];
  Object.values(searchByProject).forEach(group => {
    if (group.length >= 2) {
      group.sort((a, b) => {
        if (a.rank !== b.rank) return a.rank - b.rank;
        return new Date(state.allDocs[b.docIdx].created_at).getTime() -
               new Date(state.allDocs[a.docIdx].created_at).getTime();
      });
      projectGroups.push(group);
    } else {
      soloResults.push(group[0]);
    }
  });

  soloResults.sort((a, b) => a.rank - b.rank);

  // Layout: cone shape
  const allSlots = [];
  projectGroups.forEach(g => {
    allSlots.push({ docIdx: g[0].docIdx, rank: Math.min(...g.map(x => x.rank)), group: g });
  });
  soloResults.forEach(s => {
    allSlots.push({ docIdx: s.docIdx, rank: s.rank, group: null });
  });
  allSlots.sort((a, b) => a.rank - b.rank);

  const totalSlots = allSlots.length;
  const coneHeight = Math.max(totalSlots * 2, 80);
  const coneMaxR = coneHeight * 0.4;
  const chainSpacing = 6;
  const golden = 2.399963;

  allSlots.forEach((slot, k) => {
    const level = totalSlots > 1 ? k / (totalSlots - 1) : 0;
    const y = coneHeight / 2 - level * coneHeight;
    const radius = level * coneMaxR;
    const theta = golden * k;
    const x = Math.cos(theta) * radius;
    const z = Math.sin(theta) * radius;

    if (slot.group) {
      slot.group.forEach((item, ci) => {
        state.searchPositions[item.docIdx * 3]     = x;
        state.searchPositions[item.docIdx * 3 + 1] = y - ci * 3;
        state.searchPositions[item.docIdx * 3 + 2] = z - ci * chainSpacing;
      });
    } else {
      state.searchPositions[slot.docIdx * 3]     = x;
      state.searchPositions[slot.docIdx * 3 + 1] = y;
      state.searchPositions[slot.docIdx * 3 + 2] = z;
    }
  });

  // Start transition
  state.searchTransFrom = new Float32Array(positions);
  state.searchTransTo = state.searchPositions;
  state.searchTransStart = performance.now();
  state.searchTransitioning = true;

  // Fly camera to view cone
  const camDist = Math.max(coneHeight * 1.5, 120);
  animateCamera(
    new THREE.Vector3(camDist * 0.6, coneHeight * 0.3, camDist * 0.8),
    new THREE.Vector3(0, 0, 0)
  );
}

function populateSidebar(indices, scores) {
  sidebarList.innerHTML = '';
  sidebarTitle.textContent = indices.length + ' results';
  indices.forEach((docIdx, rank) => {
    const d = state.allDocs[docIdx];
    if (!d) return;
    const catId = getCategoryId(d);
    const catColor = getCatColor(catId);
    const catName = getCatName(catId);
    const dateStr = d.created_at ? new Date(d.created_at).toLocaleDateString('en-CA') : '';
    const preview = (d.preview || '').slice(0, 120).replace(/</g, '&lt;');
    const score = scores && scores[docIdx] != null ? scores[docIdx].toFixed(3) : '';
    const tags = (d.tags || []).filter(t => !/^(ai-|guid:|source:|date:)/.test(t)).slice(0, 3).join(', ');

    const card = document.createElement('div');
    card.className = 'sidebar-card';
    card.innerHTML =
      '<div class="sc-head">' +
        '<span class="sc-date">' + dateStr + '</span>' +
        (score ? '<span class="sc-score">' + score + '</span>' : '<span class="sc-score">#' + (rank+1) + '</span>') +
      '</div>' +
      '<span class="sc-cat" style="background:' + catColor + '">' + catName + '</span>' +
      '<div class="sc-preview">' + preview + '</div>' +
      (tags ? '<div class="sc-tags">' + tags + '</div>' : '');

    card.addEventListener('click', () => {
      sidebarList.querySelectorAll('.sidebar-card').forEach(c => c.classList.remove('active'));
      card.classList.add('active');
      const pos = state.particleSystem.geometry.attributes.position.array;
      const x = pos[docIdx*3], y = pos[docIdx*3+1], z = pos[docIdx*3+2];
      const target = new THREE.Vector3(x, y, z);
      animateCamera(
        new THREE.Vector3(x + 20, y + 10, z + 30),
        target
      );
      highlightDoc(docIdx);
      showPinnedTooltipCentered(docIdx);
    });
    sidebarList.appendChild(card);
  });
  sidebar.classList.add('open');
}

function doLocalSearch(query) {
  const myId = ++state.localSearchId;
  clearSelection();
  hideSearchTimer();
  const typeTag = getSelectedType();
  const q = query.toLowerCase();

  const chunkSize = 1000;
  const scored = [];
  let offset = 0;

  function processChunk() {
    if (myId !== state.localSearchId) return;
    const end = Math.min(offset + chunkSize, state.allDocs.length);
    for (let i = offset; i < end; i++) {
      const d = state.allDocs[i];
      if (typeTag && !(d.tags || []).includes(typeTag)) continue;
      const text = ((d.preview || '') + ' ' + (d.tags || []).join(' ')).toLowerCase();
      if (!text.includes(q)) continue;
      let hits = 0, pos = 0;
      while ((pos = text.indexOf(q, pos)) !== -1) { hits++; pos += q.length; }
      scored.push({ idx: i, hits });
    }
    offset = end;
    if (offset < state.allDocs.length) {
      requestAnimationFrame(processChunk);
      return;
    }
    if (myId !== state.localSearchId) return;
    if (!scored.length) {
      searchStatus.textContent = '0 matches (Enter for semantic)';
      clearSearchHighlight();
      return;
    }
    scored.sort((a, b) => b.hits - a.hits);
    const matchIndices = scored.map(s => s.idx);
    searchStatus.textContent = matchIndices.length + ' matches';
    populateSidebar(matchIndices, null);
    focusSearchResults(matchIndices);
  }
  requestAnimationFrame(processChunk);
}

function applySemanticResults(results, elapsed, fromCache) {
  const guidRank = {};
  const guidScore = {};
  results.forEach((r, rank) => { guidRank[r.guid] = rank; guidScore[r.guid] = r.similarity_score; });
  const matchIndices = [];
  state.allDocs.forEach((d, i) => {
    if (guidRank[d.guid] !== undefined) matchIndices.push(i);
  });

  if (!matchIndices.length) {
    searchStatus.textContent = results.length + ' found (not in view)';
    return;
  }
  matchIndices.sort((a, b) => guidRank[state.allDocs[a].guid] - guidRank[state.allDocs[b].guid]);
  const tag = fromCache ? 'cached' : elapsed + 'ms';
  searchStatus.textContent = matchIndices.length + ' results (' + tag + ')';
  const scoreMap = {};
  matchIndices.forEach(i => { scoreMap[i] = guidScore[state.allDocs[i].guid]; });
  populateSidebar(matchIndices, scoreMap);
  focusSearchResults(matchIndices);
}

async function doSemanticSearch(query) {
  clearSelection();
  searchStatus.textContent = '';
  const typeTag = getSelectedType();
  const cacheKey = query + '|' + (typeTag || '');

  // Check cache (valid for 5 minutes)
  const cached = semanticCache.get(cacheKey);
  if (cached && (Date.now() - cached.ts) < 300000) {
    hideSearchTimer();
    mapQ.classList.remove('searching');
    if (!cached.results.length) {
      searchStatus.textContent = 'No results (cached)';
      clearSearchHighlight();
      return;
    }
    applySemanticResults(cached.results, 0, true);
    return;
  }

  const t0 = startSearchTimer();
  mapQ.classList.add('searching');
  try {
    const body = { query, limit: 50 };
    if (typeTag) body.tags = [typeTag];
    const r = await fetch(API + '/search', {
      method: 'POST',
      headers: wsHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body)
    });
    const data = await r.json();
    const elapsed = stopSearchTimer(t0);
    mapQ.classList.remove('searching');
    const results = data.results || [];

    // Store in cache
    semanticCache.set(cacheKey, { results, ts: Date.now() });

    if (!results.length) {
      searchStatus.textContent = 'No results (' + elapsed + 'ms)';
      clearSearchHighlight();
      return;
    }
    applySemanticResults(results, elapsed, false);
  } catch(e) {
    stopSearchTimer(t0);
    mapQ.classList.remove('searching');
    searchStatus.textContent = 'Error: ' + e.message;
  }
}

export function initSearchListeners() {
  document.getElementById('search-switch').addEventListener('click', e => {
    const opt = e.target.closest('.sw-opt');
    if (!opt) return;
    const mode = opt.dataset.mode;
    setSearchMode(mode);
    const q = mapQ.value.trim();
    if (!q) return;
    if (mode === 'ai') {
      doSemanticSearch(q);
    } else {
      clearSearchHighlight();
      doLocalSearch(q);
    }
  });

  mapQ.addEventListener('input', () => {
    clearTimeout(searchDebounce);
    state.localSearchId++;
    if (state.searchMode === 'ai') { setSearchMode('tx'); clearSearchHighlight(); }
    const q = mapQ.value.trim();
    if (!q) { clearSearchHighlight(); return; }
    searchDebounce = setTimeout(() => doLocalSearch(q), 300);
  });

  mapQ.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      clearTimeout(searchDebounce);
      const q = mapQ.value.trim();
      if (q) {
        setSearchMode('ai');
        doSemanticSearch(q);
      }
    }
    if (e.key === 'Escape') { mapQ.value = ''; setSearchMode('tx'); clearSearchHighlight(); }
  });

  document.getElementById('map-q-clear').addEventListener('click', () => {
    mapQ.value = '';
    setSearchMode('tx');
    clearSearchHighlight();
  });

  document.getElementById('sidebar-close').addEventListener('click', () => {
    sidebar.classList.remove('open');
  });
}
