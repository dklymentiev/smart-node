import * as THREE from 'three';
import { state } from './state.js';
import { camera, controls } from './scene.js';
import { TRANSITION_MS, TL } from './constants.js';

export function switchView(mode) {
  state.viewMode = mode;
  if (!state.posGalaxy || !state.posTimeline || !state.particleSystem) return;

  const current = state.particleSystem.geometry.attributes.position.array;
  state.transitionFrom = new Float32Array(current);
  state.transitionTo = mode === 'galaxy' ? state.posGalaxy : state.posTimeline;
  state.transitionStart = performance.now();
  state.transitioning = true;

  // Show/hide overlays
  document.getElementById('cluster-labels').style.display = mode === 'galaxy' ? '' : 'none';
  document.getElementById('doc-cards').style.display = mode === 'galaxy' ? '' : 'none';
  document.getElementById('lane-labels').style.display = mode === 'timeline' ? '' : 'none';
  document.getElementById('date-axis').style.display = mode === 'timeline' ? '' : 'none';

  // Show/hide nebulae and chains
  state.nebulaeMeshes.forEach(m => { m.visible = mode === 'galaxy'; });
  state.chainLines.forEach(m => { m.visible = mode === 'galaxy'; });

  // Camera transition
  const camGalaxy = { pos: new THREE.Vector3(0, 200, 750), target: new THREE.Vector3(0, 0, 0) };
  const totalH = state.timelineLanes.length * TL.laneH;
  const camTimeline = {
    pos: new THREE.Vector3(0, 0, Math.max(TL.xSpread * 0.8, totalH * 1.5)),
    target: new THREE.Vector3(0, 0, 0)
  };
  const dest = mode === 'galaxy' ? camGalaxy : camTimeline;
  animateCamera(dest.pos, dest.target);
}

export function animateCamera(toPos, toTarget) {
  const startPos = camera.position.clone();
  const startTarget = controls.target.clone();
  const start = performance.now();
  function step() {
    const t = Math.min((performance.now() - start) / TRANSITION_MS, 1);
    const e = t < 0.5 ? 2*t*t : -1 + (4 - 2*t)*t;
    camera.position.lerpVectors(startPos, toPos, e);
    controls.target.lerpVectors(startTarget, toTarget, e);
    controls.update();
    if (t < 1) requestAnimationFrame(step);
  }
  step();
}

export function zoomToGalaxyCluster(info) {
  const target = new THREE.Vector3(info._cx, info._cy, info._cz);
  const dist = Math.max(info._radius * 3, 60);
  animateCamera(
    new THREE.Vector3(info._cx, info._cy + dist * 0.3, info._cz + dist),
    target
  );
}

export function zoomToTimelineLane(lane) {
  animateCamera(
    new THREE.Vector3(0, lane.y, 200),
    new THREE.Vector3(0, lane.y, 0)
  );
}

export function initViewToggle() {
  document.querySelectorAll('.view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.view;
      if (mode === state.viewMode || state.transitioning) return;
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      switchView(mode);
    });
  });
}
