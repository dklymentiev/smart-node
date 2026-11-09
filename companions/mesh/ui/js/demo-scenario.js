/**
 * Mesh Galaxy Map -- Automated Demo Scenario
 *
 * Load via: map.html?demo=1
 * Or paste in console: import('/ui/js/demo-scenario.js')
 *
 * Sequence:
 *   1. Slow galaxy rotation (4s)
 *   2. Click 3 categories with zoom (2.5s each)
 *   3. Reset to full view (2s)
 *   4. Type text search, see results (3s)
 *   5. Click sidebar cards (2s each)
 *   6. AI semantic search (4s)
 *   7. Click result cards (2s each)
 *   8. Clear and reset (2s)
 */

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function typeText(input, text, delayPerChar = 80) {
  input.focus();
  for (const ch of text) {
    input.value += ch;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await sleep(delayPerChar);
  }
}

function clickElement(el) {
  if (!el) return false;
  el.click();
  return true;
}

function getLegendItems() {
  return Array.from(document.querySelectorAll('.legend-item'));
}

function getSidebarCards() {
  return Array.from(document.querySelectorAll('.sidebar-card'));
}

async function waitForData(maxWait = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxWait) {
    const demo = window.meshDemo;
    if (demo && demo.state && demo.state.allDocs.length > 0 && demo.state.particleSystem) {
      return true;
    }
    await sleep(500);
  }
  console.warn('[demo] Timed out waiting for data');
  return false;
}

async function waitForSidebarCards(minCards = 1, maxWait = 5000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxWait) {
    if (getSidebarCards().length >= minCards) return true;
    await sleep(200);
  }
  return false;
}

async function runDemo() {
  console.log('[demo] Waiting for data to load...');
  if (!await waitForData()) return;
  console.log('[demo] Starting demo scenario');

  const { controls } = window.meshDemo;
  const mapQ = document.getElementById('map-q');
  const clearBtn = document.getElementById('map-q-clear');
  const legendItems = getLegendItems();

  // -- Phase 1: Slow auto-rotation (4s) --
  console.log('[demo] Phase 1: Galaxy rotation');
  controls.autoRotate = true;
  controls.autoRotateSpeed = 1.5;
  await sleep(4000);
  controls.autoRotate = false;

  // -- Phase 2: Click categories (3 clicks, 2.5s each) --
  const categoriesToClick = Math.min(3, legendItems.length);
  for (let i = 0; i < categoriesToClick; i++) {
    const idx = i === 0 ? 0 : i === 1 ? Math.min(3, legendItems.length - 1) : Math.min(6, legendItems.length - 1);
    const item = legendItems[idx];
    console.log('[demo] Phase 2: Click category "' + item.textContent + '"');
    clickElement(item);
    await sleep(2500);
  }

  // -- Phase 3: Reset to full galaxy view --
  console.log('[demo] Phase 3: Reset view');
  clickElement(legendItems[0]); // zoom to first to trigger a move
  await sleep(500);
  // Use clear to reset camera if search was active, otherwise click Galaxy button
  const galaxyBtn = document.querySelector('.view-btn[data-view="galaxy"]');
  if (galaxyBtn && !galaxyBtn.classList.contains('active')) {
    clickElement(galaxyBtn);
  }
  // Fly camera back to default by starting a brief auto-rotate from far
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.5;
  await sleep(2000);
  controls.autoRotate = false;

  // -- Phase 4: Type text search --
  console.log('[demo] Phase 4: Text search "deploy"');
  mapQ.value = '';
  mapQ.focus();
  await typeText(mapQ, 'deploy', 100);
  await sleep(1500);

  // -- Phase 5: Click sidebar cards --
  await waitForSidebarCards(1);
  const cards1 = getSidebarCards();
  if (cards1.length > 0) {
    console.log('[demo] Phase 5: Click first result card');
    clickElement(cards1[0]);
    await sleep(2000);
  }
  if (cards1.length > 2) {
    console.log('[demo] Phase 5: Click third result card');
    clickElement(cards1[2]);
    await sleep(2000);
  }

  // -- Phase 6: Clear and type new query for AI search --
  console.log('[demo] Phase 6: AI search "machine learning models"');
  clickElement(clearBtn);
  await sleep(800);
  mapQ.value = '';
  await typeText(mapQ, 'machine learning models', 60);
  await sleep(500);

  // Press Enter to trigger semantic search
  mapQ.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await sleep(4000);

  // -- Phase 7: Click AI search result cards --
  await waitForSidebarCards(1);
  const cards2 = getSidebarCards();
  if (cards2.length > 0) {
    console.log('[demo] Phase 7: Click first AI result');
    clickElement(cards2[0]);
    await sleep(2500);
  }
  if (cards2.length > 1) {
    console.log('[demo] Phase 7: Click second AI result');
    clickElement(cards2[1]);
    await sleep(2500);
  }

  // -- Phase 8: Clear and reset --
  console.log('[demo] Phase 8: Reset to initial state');
  clickElement(clearBtn);
  await sleep(2000);

  // Final slow rotation
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.8;
  await sleep(3000);
  controls.autoRotate = false;

  console.log('[demo] Done');
}

// Auto-start if loaded via ?demo=1
if (new URLSearchParams(window.location.search).has('demo')) {
  runDemo();
} else {
  console.log('[demo] Script loaded. Call runDemo() or add ?demo=1 to URL');
}

export { runDemo };
