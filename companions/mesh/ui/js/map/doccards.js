import * as THREE from 'three';
import { state } from './state.js';
import { SKIP_RE, DOC_CARD_POOL_SIZE } from './constants.js';
import { camera } from './scene.js';
import { getCategoryId, getCatColor, getCatName, extractTagLabel } from './taxonomy.js';

const docCardsContainer = document.getElementById('doc-cards');

export function initDocCardPool() {
  docCardsContainer.innerHTML = '';
  state.docCardPool = [];
  for (let i = 0; i < DOC_CARD_POOL_SIZE; i++) {
    const el = document.createElement('div');
    el.className = 'doc-card';
    docCardsContainer.appendChild(el);
    state.docCardPool.push(el);
  }
}

function formatCardDate(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  const m = d.toLocaleString('en-CA', { month: 'short' });
  return d.getDate() + ' ' + m + ' ' + d.getFullYear();
}

function getDocTags(doc) {
  return (doc.tags || []).filter(t => !SKIP_RE.test(t)).map(t => extractTagLabel(t));
}

export function updateDocCards() {
  if (state.viewMode !== 'galaxy' || !state.particleSystem) {
    state.docCardPool.forEach(el => { el.style.display = 'none'; });
    return;
  }
  // Throttle: update every 3 frames
  if (++state.docCardThrottle % 3 !== 0) return;

  const positions = state.particleSystem.geometry.attributes.position.array;
  const camPos = camera.position;

  // Find docs close to camera, sort by distance
  const nearby = [];
  for (let i = 0; i < state.allDocs.length; i++) {
    const dx = positions[i*3] - camPos.x;
    const dy = positions[i*3+1] - camPos.y;
    const dz = positions[i*3+2] - camPos.z;
    const dist = Math.sqrt(dx*dx + dy*dy + dz*dz);
    if (dist < 300 && dist > 15) {
      nearby.push({ i, dist });
    }
  }
  nearby.sort((a, b) => a.dist - b.dist);

  const visible = nearby.slice(0, DOC_CARD_POOL_SIZE);

  for (let p = 0; p < DOC_CARD_POOL_SIZE; p++) {
    const el = state.docCardPool[p];
    if (p >= visible.length) { el.style.display = 'none'; continue; }

    const { i, dist } = visible[p];
    const d = state.allDocs[i];
    const pos3d = new THREE.Vector3(positions[i*3], positions[i*3+1], positions[i*3+2]);
    const v = pos3d.clone().project(camera);
    const x = (v.x * 0.5 + 0.5) * state.W;
    const y = (-v.y * 0.5 + 0.5) * state.H + 45;

    if (v.z > 1 || x < -50 || x > state.W + 50 || y < 0 || y > state.H + 50) {
      el.style.display = 'none'; continue;
    }

    // Build content (only rebuild if doc changed)
    if (el.dataset.guid !== d.guid) {
      el.dataset.guid = d.guid;
      const dateStr = formatCardDate(d.created_at);
      const tags = getDocTags(d).slice(0, 3);
      const tagsHtml = tags.length
        ? '<div class="dc-tags">' + tags.map(t => '<span class="dc-tag">' + t + '</span>').join('') + '</div>'
        : '';
      const catId = getCategoryId(d);
      const catColor = getCatColor(catId);
      const catName = getCatName(catId);
      const docType = d.type || '';
      el.innerHTML =
        '<div class="dc-date">' + dateStr + '</div>' +
        '<div><span class="dc-cat" style="background:' + catColor + '">' + catName + '</span>' +
        (docType ? '<span class="dc-type">' + docType + '</span>' : '') + '</div>' +
        tagsHtml;
      el.style.borderColor = catColor + '30';
    }

    el.style.display = 'block';
    el.style.left = x + 'px';
    el.style.top = y + 'px';
    const opacity = 1 - (dist - 50) / 250;
    el.style.opacity = Math.max(0.2, Math.min(1, opacity)).toFixed(2);
  }
}
