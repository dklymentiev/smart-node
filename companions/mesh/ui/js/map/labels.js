import * as THREE from 'three';
import { state } from './state.js';
import { camera } from './scene.js';

export function updateGalaxyLabels() {
  if (state.viewMode !== 'galaxy') return;

  // Category labels: visible at overview, hide when very close
  Object.entries(state.clusterInfo).forEach(([cid, info]) => {
    const el = document.getElementById('clbl-' + cid);
    if (!el) return;
    const pos3d = new THREE.Vector3(info._cx, info._cy, info._cz);
    const dist = camera.position.distanceTo(pos3d);
    if (dist > 2500 || dist < 400) { el.style.display = 'none'; return; }
    const v = pos3d.clone().project(camera);
    const x = (v.x * 0.5 + 0.5) * state.W;
    const y = (-v.y * 0.5 + 0.5) * state.H + 45;
    if (v.z > 1 || x < -100 || x > state.W + 100 || y < 0 || y > state.H + 100) {
      el.style.display = 'none';
    } else {
      el.style.display = '';
      el.style.left = x + 'px';
      el.style.top = y + 'px';
      // Fade out when approaching doc-card range (<400)
      const fadeOpacity = dist < 600 ? (dist - 400) / 200 : 1;
      el.style.opacity = Math.max(0, Math.min(1, fadeOpacity)).toFixed(2);
    }
  });

  // Subcategory labels: visible only when close to parent category
  Object.entries(state.subcategoryInfo).forEach(([catId, subs]) => {
    const center = state.catCenters[catId];
    if (!center) return;
    const catPos = new THREE.Vector3(center.x, center.y, center.z);
    const distToCat = camera.position.distanceTo(catPos);

    subs.forEach(sub => {
      const el = document.getElementById('sublbl-' + catId + '-' + sub.id);
      if (!el) return;
      if (distToCat > 600 || distToCat < 30) { el.style.display = 'none'; return; }
      const pos3d = new THREE.Vector3(sub._cx, sub._cy, sub._cz);
      const v = pos3d.clone().project(camera);
      const x = (v.x * 0.5 + 0.5) * state.W;
      const y = (-v.y * 0.5 + 0.5) * state.H + 45;
      if (v.z > 1 || x < -100 || x > state.W + 100 || y < 0 || y > state.H + 100) {
        el.style.display = 'none';
      } else {
        el.style.display = 'block';
        el.style.left = x + 'px';
        el.style.top = y + 'px';
        const opacity = 1 - (distToCat - 200) / 400;
        el.style.opacity = Math.max(0.2, Math.min(1, opacity)).toFixed(2);
      }
    });
  });
}
