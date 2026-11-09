import { state } from './state.js';
import { controls, composer, camera } from './scene.js';
import { TRANSITION_MS, SEARCH_TRANS_MS } from './constants.js';
import { updateGalaxyLabels } from './labels.js?v=5';
import { updateTimelineLanePositions } from './timeline.js';
import { updateDocCards } from './doccards.js?v=5';

export function startAnimationLoop() {
  function animate() {
    requestAnimationFrame(animate);
    controls.update();

    // Transition particles (view switch)
    if (state.transitioning) {
      const t = Math.min((performance.now() - state.transitionStart) / TRANSITION_MS, 1);
      const ease = t < 0.5 ? 2*t*t : -1 + (4 - 2*t)*t;
      const arr = state.particleSystem.geometry.attributes.position.array;
      for (let i = 0; i < arr.length; i++) {
        arr[i] = state.transitionFrom[i] + (state.transitionTo[i] - state.transitionFrom[i]) * ease;
      }
      state.particleSystem.geometry.attributes.position.needsUpdate = true;
      if (t >= 1) state.transitioning = false;
    }

    // Transition particles (search cluster)
    if (state.searchTransitioning) {
      const t = Math.min((performance.now() - state.searchTransStart) / SEARCH_TRANS_MS, 1);
      const ease = t < 0.5 ? 2*t*t : -1 + (4 - 2*t)*t;
      const arr = state.particleSystem.geometry.attributes.position.array;
      for (let i = 0; i < arr.length; i++) {
        arr[i] = state.searchTransFrom[i] + (state.searchTransTo[i] - state.searchTransFrom[i]) * ease;
      }
      state.particleSystem.geometry.attributes.position.needsUpdate = true;
      if (t >= 1) state.searchTransitioning = false;
    }

    if (state.viewMode === 'galaxy') { updateGalaxyLabels(); updateDocCards(); }
    if (state.viewMode === 'timeline') updateTimelineLanePositions();

    // Zoom indicator
    const zi = document.getElementById('zoom-indicator');
    if (zi) zi.textContent = 'dist: ' + camera.position.length().toFixed(0);

    // Keep ring facing camera and following doc position
    if (state.selectedRing) {
      state.selectedRing.lookAt(camera.position);
      if (state.selectedIdx >= 0 && state.particleSystem) {
        const p = state.particleSystem.geometry.attributes.position.array;
        state.selectedRing.position.set(p[state.selectedIdx*3], p[state.selectedIdx*3+1], p[state.selectedIdx*3+2]);
      }
    }

    composer.render();
  }
  animate();
}
