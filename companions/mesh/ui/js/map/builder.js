import * as THREE from 'three';
import { state } from './state.js';
import { TL } from './constants.js';
import { getCategoryId, getSubcategoryId, getProjectGuid, getCatColor, getCatName } from './taxonomy.js';
import { scene, camera, controls, spriteTexture } from './scene.js';
import { animateCamera, zoomToGalaxyCluster } from './views.js';
import { buildTimelineOverlays } from './timeline.js';

export function buildScene() {
  // Cleanup
  if (state.particleSystem) { scene.remove(state.particleSystem); state.particleSystem = null; }
  state.nebulaeMeshes.forEach(m => scene.remove(m));
  state.nebulaeMeshes = [];
  state.chainLines.forEach(m => scene.remove(m));
  state.chainLines = [];
  document.getElementById('cluster-labels').innerHTML = '';
  document.getElementById('lane-labels').innerHTML = '';
  document.getElementById('date-axis').innerHTML = '';
  state.clusterInfo = {};
  state.timelineLanes = [];

  const count = state.allDocs.length;
  if (!count) return;

  // ---- Compute cluster info from ai-category tags ----
  const clusterMap = {};
  state.allDocs.forEach((d, i) => {
    const catId = getCategoryId(d) || 'uncategorized';
    if (!clusterMap[catId]) clusterMap[catId] = [];
    clusterMap[catId].push({ d, i });
  });

  const clusterEntries = Object.entries(clusterMap)
    .sort((a, b) => b[1].length - a[1].length);

  clusterEntries.forEach(([cid, members]) => {
    const label = getCatName(cid);
    const dates = members.map(({ d }) => new Date(d.created_at).getTime()).sort((a,b) => a - b);
    const medianDate = dates[Math.floor(dates.length / 2)];
    state.clusterInfo[cid] = { label, members, medianDate, size: members.length };
  });

  // ---- Galaxy positions (category-centric layout) ----
  const S = 250;
  state.posGalaxy = new Float32Array(count * 3);

  const catGroups = {};
  state.allDocs.forEach((d, i) => {
    const catId = getCategoryId(d) || 'uncategorized';
    if (!catGroups[catId]) catGroups[catId] = [];
    catGroups[catId].push(i);
  });

  const catLayoutEntries = Object.entries(catGroups).sort((a, b) => b[1].length - a[1].length);
  state.catCenters = {};

  const catRadii = {};
  catLayoutEntries.forEach(([catId, members]) => {
    catRadii[catId] = Math.max(25, 12 * Math.sqrt(members.length));
  });

  // Pack categories: largest at center, others placed tangent to existing
  const placed = [];
  const padding = 35;

  catLayoutEntries.forEach(([catId, members], idx) => {
    const r = catRadii[catId];

    if (idx === 0) {
      state.catCenters[catId] = { x: 0, y: 0, z: 0 };
      placed.push({ x: 0, y: 0, r });
      return;
    }

    let bestPos = null;
    let bestDist = Infinity;
    const numAngles = 72;

    for (const p of placed) {
      const touchDist = r + p.r + padding;
      for (let a = 0; a < numAngles; a++) {
        const angle = (a / numAngles) * Math.PI * 2;
        const x = p.x + Math.cos(angle) * touchDist;
        const y = p.y + Math.sin(angle) * touchDist;

        let overlaps = false;
        for (const q of placed) {
          if (Math.hypot(x - q.x, y - q.y) < r + q.r + padding * 0.8) {
            overlaps = true;
            break;
          }
        }

        if (!overlaps) {
          const d = Math.hypot(x, y);
          if (d < bestDist) {
            bestDist = d;
            bestPos = { x, y };
          }
        }
      }
    }

    if (!bestPos) {
      const angle = idx * 2.399963;
      const spiralR = 150 + idx * 60;
      bestPos = { x: Math.cos(angle) * spiralR, y: Math.sin(angle) * spiralR };
    }

    state.catCenters[catId] = { x: bestPos.x, y: bestPos.y, z: 0 };
    placed.push({ x: bestPos.x, y: bestPos.y, r });
  });

  // Position docs within their category cluster
  catLayoutEntries.forEach(([catId, members]) => {
    const center = state.catCenters[catId];

    let umapCx = 0, umapCy = 0;
    members.forEach(i => {
      umapCx += (state.allDocs[i].x - 0.5) * S * 2;
      umapCy += (state.allDocs[i].y - 0.5) * S * 2;
    });
    umapCx /= members.length;
    umapCy /= members.length;

    let maxDist = 0;
    members.forEach(i => {
      const dx = (state.allDocs[i].x - 0.5) * S * 2 - umapCx;
      const dy = (state.allDocs[i].y - 0.5) * S * 2 - umapCy;
      const d = Math.hypot(dx, dy);
      if (d > maxDist) maxDist = d;
    });

    const targetR = Math.max(20, 12 * Math.sqrt(members.length));
    const shrink = maxDist > 0 ? targetR / maxDist : 1;

    members.forEach(i => {
      const origX = (state.allDocs[i].x - 0.5) * S * 2;
      const origY = (state.allDocs[i].y - 0.5) * S * 2;
      state.posGalaxy[i * 3]     = center.x + (origX - umapCx) * shrink;
      state.posGalaxy[i * 3 + 1] = center.y + (origY - umapCy) * shrink;
      state.posGalaxy[i * 3 + 2] = (Math.random() - 0.5) * targetR * 0.3;
    });
  });

  // ---- Arrange project chains chronologically (within each category) ----
  const projectChains = {};
  state.allDocs.forEach((d, i) => {
    const pg = getProjectGuid(d);
    if (!pg) return;
    const catId = getCategoryId(d) || 'uncategorized';
    const key = catId + '|' + pg;
    if (!projectChains[key]) projectChains[key] = [];
    projectChains[key].push(i);
  });

  Object.entries(projectChains).forEach(([key, indices]) => {
    if (indices.length < 2) return;

    indices.sort((a, b) => {
      const ta = new Date(state.allDocs[a].created_at).getTime();
      const tb = new Date(state.allDocs[b].created_at).getTime();
      return tb - ta;
    });

    let cx = 0, cy = 0, cz = 0;
    indices.forEach(i => {
      cx += state.posGalaxy[i*3]; cy += state.posGalaxy[i*3+1]; cz += state.posGalaxy[i*3+2];
    });
    cx /= indices.length; cy /= indices.length; cz /= indices.length;

    const catId = key.split('|')[0];
    const catCenter = state.catCenters[catId] || { x: 0, y: 0, z: 0 };
    let dx = cx - catCenter.x;
    let dy = cy - catCenter.y;
    const len = Math.hypot(dx, dy) || 1;
    dx /= len; dy /= len;

    const px = -dy, py = dx;
    const spacing = Math.min(5, 30 / indices.length);
    const totalLen = (indices.length - 1) * spacing;
    const startOffset = -totalLen / 2;

    indices.forEach((docIdx, order) => {
      const offset = startOffset + order * spacing;
      state.posGalaxy[docIdx * 3]     = cx + px * offset;
      state.posGalaxy[docIdx * 3 + 1] = cy + py * offset;
      state.posGalaxy[docIdx * 3 + 2] = cz;
    });
  });

  // ---- Build chain connection lines ----
  const chainLineMat = new THREE.LineBasicMaterial({
    color: 0x4fc3f7, transparent: true, opacity: 0.12,
    blending: THREE.AdditiveBlending, depthWrite: false
  });

  Object.entries(projectChains).forEach(([pg, indices]) => {
    if (indices.length < 2) return;
    const points = indices.map(i =>
      new THREE.Vector3(state.posGalaxy[i*3], state.posGalaxy[i*3+1], state.posGalaxy[i*3+2])
    );
    const geo = new THREE.BufferGeometry().setFromPoints(points);
    const line = new THREE.Line(geo, chainLineMat);
    line.visible = state.viewMode === 'galaxy';
    scene.add(line);
    state.chainLines.push(line);
  });

  // ---- Timeline positions ----
  const dates = state.allDocs.map(d => new Date(d.created_at).getTime());
  state.dateRange.min = Math.min(...dates);
  state.dateRange.max = Math.max(...dates);
  const dRange = state.dateRange.max - state.dateRange.min || 1;

  const sortedForLanes = Object.entries(state.clusterInfo)
    .filter(([cid]) => cid !== 'uncategorized')
    .sort((a, b) => a[1].medianDate - b[1].medianDate)
    .slice(0, 30);
  const laneMap = {};
  sortedForLanes.forEach(([cid], idx) => { laneMap[cid] = idx; });
  const numLanes = sortedForLanes.length;
  const totalLaneH = numLanes * TL.laneH;

  state.timelineLanes = sortedForLanes.map(([cid, info], idx) => ({
    cid,
    label: info.label,
    y: (totalLaneH / 2) - idx * TL.laneH,
    size: info.size
  }));

  state.posTimeline = new Float32Array(count * 3);
  const halfX = TL.xSpread / 2;
  state.allDocs.forEach((d, i) => {
    const t = dates[i];
    const x = ((t - state.dateRange.min) / dRange) * TL.xSpread - halfX;
    const cid = getCategoryId(d) || 'uncategorized';
    const lane = laneMap[cid];
    let y;
    if (lane !== undefined) {
      y = (totalLaneH / 2) - lane * TL.laneH + (Math.random() - 0.5) * TL.laneH * 0.5;
    } else {
      y = -(totalLaneH / 2) - 20 + (Math.random() - 0.5) * TL.laneH;
    }
    state.posTimeline[i*3]     = x;
    state.posTimeline[i*3 + 1] = y;
    state.posTimeline[i*3 + 2] = 0;
  });

  // ---- Create particles ----
  const startPos = state.viewMode === 'galaxy' ? state.posGalaxy : state.posTimeline;
  const positions = new Float32Array(startPos);
  const colors = new Float32Array(count * 3);

  state.allDocs.forEach((d, i) => {
    const catId = getCategoryId(d);
    const hex = getCatColor(catId);
    const color = new THREE.Color(hex).multiplyScalar(0.6);
    colors[i*3] = color.r;
    colors[i*3+1] = color.g;
    colors[i*3+2] = color.b;
  });

  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));

  const mat = new THREE.PointsMaterial({
    size: 2.5, map: spriteTexture, vertexColors: true,
    transparent: true, alphaTest: 0.01, sizeAttenuation: true,
    blending: THREE.NormalBlending, depthWrite: false
  });

  state.particleSystem = new THREE.Points(geo, mat);
  scene.add(state.particleSystem);

  // ---- Nebulae (galaxy mode) ----
  const galaxyClusterEntries = Object.entries(state.clusterInfo)
    .filter(([cid, info]) => cid !== 'uncategorized' && info.size >= 3)
    .sort((a,b) => b[1].size - a[1].size)
    .slice(0, 20);

  const labelsContainer = document.getElementById('cluster-labels');

  galaxyClusterEntries.forEach(([cid, info]) => {
    const members = info.members;
    let cx=0, cy=0, cz=0;
    members.forEach(({i}) => {
      cx += state.posGalaxy[i*3]; cy += state.posGalaxy[i*3+1]; cz += state.posGalaxy[i*3+2];
    });
    cx /= members.length; cy /= members.length; cz /= members.length;

    let maxDist = 0;
    members.forEach(({i}) => {
      const dist = Math.hypot(state.posGalaxy[i*3]-cx, state.posGalaxy[i*3+1]-cy, state.posGalaxy[i*3+2]-cz);
      if (dist > maxDist) maxDist = dist;
    });
    const radius = Math.max(maxDist * 0.8, 15);

    const catHex = getCatColor(cid);
    const nebulaColor = new THREE.Color(catHex).multiplyScalar(0.5);

    // Outer shell
    const nebulaMat = new THREE.MeshBasicMaterial({
      color: nebulaColor, transparent: true, opacity: 0.02,
      blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide
    });
    const nebula = new THREE.Mesh(new THREE.SphereGeometry(radius, 24, 24), nebulaMat);
    nebula.position.set(cx, cy, cz);
    nebula.visible = state.viewMode === 'galaxy';
    scene.add(nebula);
    state.nebulaeMeshes.push(nebula);

    // Core
    const coreMat = new THREE.MeshBasicMaterial({
      color: nebulaColor, transparent: true, opacity: 0.03,
      blending: THREE.AdditiveBlending, depthWrite: false
    });
    const core = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.4, 16, 16), coreMat);
    core.position.set(cx, cy, cz);
    core.visible = state.viewMode === 'galaxy';
    scene.add(core);
    state.nebulaeMeshes.push(core);

    // Store for label projection
    info._cx = cx; info._cy = cy; info._cz = cz; info._radius = radius;

    // Galaxy cluster label
    const el = document.createElement('div');
    el.className = 'cluster-label';
    el.id = 'clbl-' + cid;
    el.textContent = info.label;
    el.addEventListener('click', () => zoomToGalaxyCluster(info));
    labelsContainer.appendChild(el);
  });

  // ---- Subcategory labels (galaxy mode) ----
  state.subcategoryInfo = {};
  document.querySelectorAll('.subcategory-label').forEach(el => el.remove());

  Object.entries(state.subcategoryData).forEach(([catId, subs]) => {
    if (!state.clusterInfo[catId]) return;
    const subInfoList = [];

    subs.forEach(sub => {
      const members = [];
      state.allDocs.forEach((d, i) => {
        if (getCategoryId(d) === catId && getSubcategoryId(d) === sub.id) {
          members.push(i);
        }
      });
      if (members.length < 1) return;

      let cx = 0, cy = 0, cz = 0;
      members.forEach(i => {
        cx += state.posGalaxy[i * 3];
        cy += state.posGalaxy[i * 3 + 1];
        cz += state.posGalaxy[i * 3 + 2];
      });
      cx /= members.length;
      cy /= members.length;
      cz /= members.length;

      const info = { id: sub.id, name: sub.name, members, _cx: cx, _cy: cy, _cz: cz, parentCat: catId };
      subInfoList.push(info);

      const el = document.createElement('div');
      el.className = 'subcategory-label';
      el.id = 'sublbl-' + catId + '-' + sub.id;
      el.textContent = sub.name;
      el.style.borderColor = getCatColor(catId) + '40';
      el.addEventListener('click', () => {
        const dist = Math.max(40, 15 * Math.sqrt(members.length));
        animateCamera(
          new THREE.Vector3(cx, cy + dist * 0.3, cz + dist),
          new THREE.Vector3(cx, cy, cz)
        );
      });
      labelsContainer.appendChild(el);
    });

    if (subInfoList.length) state.subcategoryInfo[catId] = subInfoList;
  });

  // ---- Timeline overlays ----
  buildTimelineOverlays();

  // Show correct overlays for current mode
  document.getElementById('cluster-labels').style.display = state.viewMode === 'galaxy' ? '' : 'none';
  document.getElementById('lane-labels').style.display = state.viewMode === 'timeline' ? '' : 'none';
  document.getElementById('date-axis').style.display = state.viewMode === 'timeline' ? '' : 'none';
  state.nebulaeMeshes.forEach(m => { m.visible = state.viewMode === 'galaxy'; });

  // Set camera -- auto-fit based on scene bounding box
  if (state.viewMode === 'galaxy') {
    // Compute bounding sphere of all galaxy positions
    let maxR = 0;
    for (let i = 0; i < count; i++) {
      const x = state.posGalaxy[i*3], y = state.posGalaxy[i*3+1], z = state.posGalaxy[i*3+2];
      const r = Math.sqrt(x*x + y*y + z*z);
      if (r > maxR) maxR = r;
    }
    // Camera distance: fit bounding sphere in view with padding
    const fov = camera.fov * Math.PI / 180;
    const fitDist = Math.max(maxR / Math.tan(fov / 2) * 1.3, 80);
    camera.position.set(0, fitDist * 0.27, fitDist);
  } else {
    camera.position.set(0, 0, Math.max(TL.xSpread * 0.8, totalLaneH * 1.5));
  }
  controls.target.set(0, 0, 0);
  controls.update();
}
