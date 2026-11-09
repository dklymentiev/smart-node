import { state } from './state.js';
import { API, wsHeaders } from './constants.js';
import { buildScene } from './builder.js';
import { initDocCardPool } from './doccards.js';

const loader = document.getElementById('loader');
const loaderBar = document.getElementById('loader-bar');
const loaderText = document.getElementById('loader-text');

function showLoader(text, pct) {
  loader.classList.remove('hidden');
  loaderText.textContent = text;
  loaderBar.style.width = pct + '%';
}

function hideLoader() { loader.classList.add('hidden'); }

export async function loadData() {
  const limit = parseInt(document.getElementById('ctl-limit').value) || 10000;
  const tag = document.getElementById('ctl-type').value;
  let url = API + '/visualize?limit=' + limit + '&sample=uniform';
  if (tag) url += '&tag=' + encodeURIComponent(tag);

  showLoader('Fetching documents...', 10);

  try {
    const r = await fetch(url, { headers: wsHeaders() });
    showLoader('Parsing response...', 30);
    const data = await r.json();
    if (data.error) {
      document.getElementById('map-stats').textContent = data.error;
      hideLoader();
      return;
    }
    state.allDocs = data.documents || [];
    showLoader('Building galaxy (' + state.allDocs.length + ' docs)...', 50);

    await new Promise(resolve => requestAnimationFrame(resolve));

    const dateMin = state.allDocs.reduce((m, d) => {
      const t = new Date(d.created_at).getTime();
      return t < m ? t : m;
    }, Infinity);
    const dateMax = state.allDocs.reduce((m, d) => {
      const t = new Date(d.created_at).getTime();
      return t > m ? t : m;
    }, -Infinity);
    const rangeLabel = new Date(dateMin).toLocaleDateString('en-CA') + ' .. ' + new Date(dateMax).toLocaleDateString('en-CA');

    showLoader('Computing positions...', 70);
    await new Promise(resolve => setTimeout(resolve, 0));

    buildScene();

    showLoader('Finishing...', 95);
    await new Promise(resolve => setTimeout(resolve, 0));

    const subCount = document.querySelectorAll('.subcategory-label').length;
    const sumCount = state.allDocs.filter(d => d.summary).length;
    document.getElementById('map-stats').textContent =
      state.allDocs.length + ' docs | ' + rangeLabel + ' | ' + subCount + ' subcats | ' + sumCount + ' summaries';
    initDocCardPool();
    hideLoader();
  } catch(e) {
    document.getElementById('map-stats').textContent = 'Failed: ' + e.message;
    console.error('[MAP] buildScene error:', e);
    hideLoader();
  }
}

export function initLoadButton() {
  document.getElementById('btn-load').addEventListener('click', loadData);
}

export function fetchStats() {
  fetch(API + '/stats', { headers: wsHeaders() }).then(r => r.json()).then(d => {
    document.getElementById('st-docs').textContent = d.documents;
    document.getElementById('st-idx').textContent = d.indexed;
  }).catch(() => {});
}
