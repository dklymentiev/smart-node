import * as THREE from 'three';
import { state } from './state.js';
import { camera } from './scene.js';
import { TL } from './constants.js';
import { zoomToTimelineLane } from './views.js';

export function buildTimelineOverlays() {
  // Lane labels
  const laneContainer = document.getElementById('lane-labels');
  laneContainer.innerHTML = '';
  state.timelineLanes.forEach(lane => {
    const el = document.createElement('div');
    el.className = 'lane-label';
    el.textContent = lane.label + ' (' + lane.size + ')';
    el.title = lane.label;
    el.dataset.cid = lane.cid;
    el.addEventListener('click', () => zoomToTimelineLane(lane));
    laneContainer.appendChild(el);
  });

  // Date axis
  const axisContainer = document.getElementById('date-axis');
  axisContainer.innerHTML = '';
  const dRange = state.dateRange.max - state.dateRange.min || 1;
  const dDays = dRange / (24*60*60*1000);
  const numTicks = Math.min(15, Math.max(4, Math.ceil(dDays / 3)));
  const fmt = dDays > 90
    ? { year: 'numeric', month: 'short' }
    : { month: 'short', day: 'numeric' };
  for (let i = 0; i <= numTicks; i++) {
    const frac = i / numTicks;
    const ts = state.dateRange.min + frac * dRange;
    const date = new Date(ts);
    const label = date.toLocaleDateString('en-CA', fmt);
    const el = document.createElement('div');
    el.className = 'date-tick';
    el.textContent = label;
    el.style.left = (frac * 100) + '%';
    axisContainer.appendChild(el);
  }
}

export function updateTimelineLanePositions() {
  if (state.viewMode !== 'timeline') return;
  const laneContainer = document.getElementById('lane-labels');
  const children = laneContainer.children;
  const viewH = state.H - 45 - 28;

  for (let i = 0; i < children.length; i++) {
    const el = children[i];
    const lane = state.timelineLanes[i];
    if (!lane) continue;
    const v = new THREE.Vector3(-TL.xSpread / 2 - 30, lane.y, 0);
    v.project(camera);
    const screenY = (-v.y * 0.5 + 0.5) * state.H;
    const relY = screenY - 45;
    if (relY < -20 || relY > viewH + 20) {
      el.style.display = 'none';
    } else {
      el.style.display = '';
      el.style.top = relY + 'px';
    }
  }
}
