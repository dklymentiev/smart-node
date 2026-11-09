import * as THREE from 'three';
import { state } from './state.js';
import { API, SKIP_RE } from './constants.js';
import { camera, renderer, scene } from './scene.js';
import { getCategoryId, getSubcategoryId, getCatColor, getCatName, extractTagLabel } from './taxonomy.js';

const tooltip = document.getElementById('tooltip');
let hoverTimer = null;
let hoverMouseX = 0, hoverMouseY = 0;
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();

function positionTooltip(ex, ey) {
  tooltip.style.display = 'block';
  const tw = tooltip.offsetWidth;
  const th = tooltip.offsetHeight;
  let x = ex + 15;
  let y = ey - 10;
  if (x + tw > window.innerWidth - 8) x = ex - tw - 15;
  if (y + th > window.innerHeight - 8) y = window.innerHeight - th - 8;
  if (y < 8) y = 8;
  tooltip.style.left = x + 'px';
  tooltip.style.top = y + 'px';
}

function buildTooltipContent(d, pinned) {
  const catId = getCategoryId(d);
  const subId = getSubcategoryId(d);
  const catColor = getCatColor(catId);
  const catName = getCatName(catId);
  const dateStr = d.created_at ? new Date(d.created_at).toLocaleString('en-CA', {dateStyle:'medium', timeStyle:'short'}) : '';
  const tags = (d.tags || []).filter(t => !SKIP_RE.test(t)).map(t => extractTagLabel(t)).join(', ');
  const summaryHtml = d.summary ? '<div class="tt-summary">' + d.summary.replace(/</g, '&lt;') + '</div>' : '';

  let html = '';
  if (pinned) html += '<span class="tt-close">x</span>';
  html += '<div class="tt-content">' +
    '<div class="tt-date">' + dateStr + '</div>' +
    '<div class="tt-type" style="background:' + catColor + '">' + catName + '</div>' +
    (subId ? '<div style="color:#888;font-size:10px">' + subId.split('/').pop() + '</div>' : '') +
    (d.type ? '<div style="color:#777;font-size:10px">' + d.type + '</div>' : '') +
    (tags ? '<div class="tt-tags">' + tags + '</div>' : '') +
    summaryHtml +
    '<div class="tt-preview">' + (d.preview || '').replace(/</g, '&lt;') + '</div>' +
    '<div class="tt-guid">' + d.guid + '</div>';

  if (pinned) {
    const btnLabel = d.summary ? 'Re-summarize' : 'Summarize';
    html += '<div class="tt-actions">' +
      '<button class="btn-summarize" data-guid="' + d.guid + '">' + btnLabel + '</button>' +
      '<a class="tt-link" href="/ui/?guid=' + d.guid + '" target="_blank">Open</a>' +
    '</div>';
  } else {
    html += '<a class="tt-link" href="/ui/?guid=' + d.guid + '" target="_blank">Open</a>';
  }
  html += '</div>';
  return html;
}

export function highlightDoc(docIdx) {
  if (!state.particleSystem) return;
  const colors = state.particleSystem.geometry.attributes.color;
  // Restore previous selected
  if (state.selectedIdx >= 0 && state.originalColors) {
    colors.array[state.selectedIdx*3]   = state.originalColors[state.selectedIdx*3];
    colors.array[state.selectedIdx*3+1] = state.originalColors[state.selectedIdx*3+1];
    colors.array[state.selectedIdx*3+2] = state.originalColors[state.selectedIdx*3+2];
  }
  // Save original if not in search mode
  if (!state.originalColors) {
    state.originalColors = new Float32Array(colors.array);
  }
  // Set bright white
  colors.array[docIdx*3] = 1; colors.array[docIdx*3+1] = 1; colors.array[docIdx*3+2] = 1;
  colors.needsUpdate = true;
  state.selectedIdx = docIdx;

  // Add thin static ring
  if (state.selectedRing) { scene.remove(state.selectedRing); state.selectedRing = null; }
  const pos = state.particleSystem.geometry.attributes.position.array;
  const ringGeo = new THREE.RingGeometry(1, 1.08, 48);
  const ringMat = new THREE.MeshBasicMaterial({
    color: 0xffffff, transparent: true, opacity: 0.6, side: THREE.DoubleSide
  });
  state.selectedRing = new THREE.Mesh(ringGeo, ringMat);
  state.selectedRing.position.set(pos[docIdx*3], pos[docIdx*3+1], pos[docIdx*3+2]);
  state.selectedRing.lookAt(camera.position);
  scene.add(state.selectedRing);
}

function dimSelectedDoc() {
  if (state.selectedIdx < 0 || !state.particleSystem) return;
  const colors = state.particleSystem.geometry.attributes.color;
  if (state.originalColors) {
    colors.array[state.selectedIdx*3]   = Math.min(1, state.originalColors[state.selectedIdx*3] * 1.5 + 0.2);
    colors.array[state.selectedIdx*3+1] = Math.min(1, state.originalColors[state.selectedIdx*3+1] * 1.5 + 0.2);
    colors.array[state.selectedIdx*3+2] = Math.min(1, state.originalColors[state.selectedIdx*3+2] * 1.5 + 0.2);
    colors.needsUpdate = true;
  }
}

export function closePinnedTooltip() {
  tooltip.classList.remove('pinned');
  tooltip.style.display = 'none';
  state.pinnedIdx = -1;
  dimSelectedDoc();
}

async function doSummarize(doc) {
  const btn = tooltip.querySelector('.btn-summarize');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating...'; }

  let loaderEl = tooltip.querySelector('.tt-loader');
  if (!loaderEl) {
    loaderEl = document.createElement('div');
    loaderEl.className = 'tt-loader';
    loaderEl.innerHTML = '<div class="tt-loader-bar"></div>';
    const preview = tooltip.querySelector('.tt-preview');
    if (preview) preview.before(loaderEl);
    else tooltip.appendChild(loaderEl);
  }
  loaderEl.classList.add('active');

  try {
    const r = await fetch(API + '/summarize/' + doc.guid, { method: 'POST' });
    const data = await r.json();
    loaderEl.classList.remove('active');
    if (data.summary) {
      doc.summary = data.summary;
      let el = tooltip.querySelector('.tt-summary');
      if (!el) {
        el = document.createElement('div');
        el.className = 'tt-summary';
        if (loaderEl) loaderEl.before(el);
        else tooltip.appendChild(el);
      }
      el.textContent = data.summary;
      if (btn) { btn.textContent = 'Re-summarize'; btn.disabled = false; }
      // Invalidate doc card cache so it picks up new summary
      state.docCardPool.forEach(el => { if (el.dataset.guid === doc.guid) el.dataset.guid = ''; });
    }
  } catch(e) {
    loaderEl.classList.remove('active');
    if (btn) { btn.textContent = 'Error'; btn.disabled = false; }
    console.error('Summarize failed:', e);
  }
}

export function showPinnedTooltip(docIdx, screenX, screenY) {
  const d = state.allDocs[docIdx];
  if (!d) return;
  tooltip.innerHTML = buildTooltipContent(d, true);
  tooltip.classList.add('pinned');
  positionTooltip(screenX, screenY);
  tooltip.querySelector('.tt-close').addEventListener('click', closePinnedTooltip);
  tooltip.querySelector('.btn-summarize').addEventListener('click', () => doSummarize(d));
  state.pinnedIdx = docIdx;
  state.hoveredIdx = docIdx;
}

export function showPinnedTooltipCentered(docIdx) {
  const d = state.allDocs[docIdx];
  if (!d) return;
  const sidebar = document.getElementById('search-sidebar');
  tooltip.innerHTML = buildTooltipContent(d, true);
  tooltip.classList.add('pinned');
  tooltip.style.display = 'block';
  const sidebarW = sidebar.classList.contains('open') ? 340 : 0;
  const availW = window.innerWidth - sidebarW;
  const tw = tooltip.offsetWidth;
  const th = tooltip.offsetHeight;
  tooltip.style.left = Math.max(8, (availW - tw) / 2) + 'px';
  tooltip.style.top = Math.max(50, (window.innerHeight - th) / 2) + 'px';
  tooltip.querySelector('.tt-close').addEventListener('click', closePinnedTooltip);
  tooltip.querySelector('.btn-summarize').addEventListener('click', () => doSummarize(d));
  state.pinnedIdx = docIdx;
  state.hoveredIdx = docIdx;
}

export function initTooltipListeners() {
  renderer.domElement.addEventListener('mousemove', (e) => {
    const rect = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    hoverMouseX = e.clientX;
    hoverMouseY = e.clientY;

    if (!state.particleSystem || state.pinnedIdx >= 0) return;
    raycaster.setFromCamera(mouse, camera);
    raycaster.params.Points.threshold = 3;
    const intersects = raycaster.intersectObject(state.particleSystem);

    if (intersects.length > 0) {
      const idx = intersects[0].index;
      if (idx !== state.hoveredIdx) {
        clearTimeout(hoverTimer);
        state.hoveredIdx = idx;
        tooltip.style.display = 'none';
        hoverTimer = setTimeout(() => {
          if (state.pinnedIdx >= 0) return;
          const d = state.allDocs[state.hoveredIdx];
          if (!d) return;
          tooltip.innerHTML = buildTooltipContent(d, false);
          tooltip.style.display = 'block';
          positionTooltip(hoverMouseX, hoverMouseY);
        }, 1500);
      }
    } else {
      clearTimeout(hoverTimer);
      state.hoveredIdx = -1;
      tooltip.style.display = 'none';
    }
  });

  renderer.domElement.addEventListener('click', (e) => {
    if (state.hoveredIdx >= 0 && state.allDocs[state.hoveredIdx]) {
      highlightDoc(state.hoveredIdx);
      showPinnedTooltip(state.hoveredIdx, e.clientX, e.clientY);
    } else {
      closePinnedTooltip();
    }
  });
}
