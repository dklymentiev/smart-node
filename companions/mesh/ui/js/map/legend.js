import { state } from './state.js';
import { getCatName } from './taxonomy.js';
import { zoomToGalaxyCluster } from './views.js';

export function buildLegend() {
  const entries = Object.entries(state.categoryColors)
    .filter(([id]) => id !== 'uncategorized' || !state.taxonomyLoaded);
  const el = document.getElementById('legend');
  el.innerHTML = entries.map(([id, c]) =>
    '<div class="legend-item" data-cid="' + id + '"><div class="legend-dot" style="background:' + c + '"></div>' + getCatName(id) + '</div>'
  ).join('');
  el.querySelectorAll('.legend-item').forEach(item => {
    item.addEventListener('click', () => {
      const cid = item.dataset.cid;
      if (state.clusterInfo[cid] && state.viewMode === 'galaxy') {
        zoomToGalaxyCluster(state.clusterInfo[cid]);
      }
    });
  });
}
