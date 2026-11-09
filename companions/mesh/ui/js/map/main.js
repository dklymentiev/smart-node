import { loadTaxonomy } from './taxonomy.js';
import { updateSize, camera, controls } from './scene.js';
import { buildLegend } from './legend.js';
import { initViewToggle } from './views.js';
import { loadData, initLoadButton, fetchStats } from './loader.js';
import { initTooltipListeners } from './tooltip.js';
import { initSearchListeners } from './search.js';
import { startAnimationLoop } from './animate.js?v=5';
import { state } from './state.js';
import { API } from './constants.js';

// Populate workspace selector from /admin/workspaces
async function loadWorkspaces() {
  try {
    const r = await fetch(API + '/admin/workspaces');
    const data = await r.json();
    const sel = document.getElementById('ctl-workspace');
    if (!sel || !Array.isArray(data)) return;
    sel.innerHTML = '';
    data.forEach(ws => {
      const opt = document.createElement('option');
      opt.value = ws.workspace === 'default' ? '' : ws.workspace;
      opt.textContent = ws.workspace + ' (' + ws.documents + ')';
      sel.appendChild(opt);
    });
    // Reload galaxy on workspace change
    sel.addEventListener('change', () => {
      fetchStats();
      loadTaxonomy().then(() => { buildLegend(); loadData(); });
    });
  } catch(e) { console.warn('Failed to load workspaces:', e); }
}

// Init listeners (sync)
initViewToggle();
initTooltipListeners();
initSearchListeners();
initLoadButton();

// Start animation loop immediately (renders loading state)
startAnimationLoop();

// Fetch stats (fire-and-forget)
fetchStats();

// Load workspaces, taxonomy, then build legend and load data
loadWorkspaces();
loadTaxonomy().then(() => {
  buildLegend();
  loadData();
});

// Resize handler
window.addEventListener('resize', updateSize);

// Demo automation bridge
window.meshDemo = { camera, controls, state };
