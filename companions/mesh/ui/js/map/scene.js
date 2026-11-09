import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { state } from './state.js';

export const container = document.getElementById('galaxy');
state.W = container.clientWidth;
state.H = container.clientHeight;

export const scene = new THREE.Scene();
scene.background = new THREE.Color('#0a0a1a');

export const camera = new THREE.PerspectiveCamera(60, state.W / state.H, 1, 5000);
camera.position.set(0, 200, 750);

export const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(state.W, state.H);
renderer.setPixelRatio(1);
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.2;
container.appendChild(renderer.domElement);

export const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.minDistance = 10;
controls.maxDistance = 2000;

// Bloom post-processing
export const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(state.W / 2, state.H / 2), 0.8, 0.5, 0.3);
composer.addPass(bloom);

// Starfield background
(function() {
  const geo = new THREE.BufferGeometry();
  const pos = new Float32Array(1000 * 3);
  for (let i = 0; i < 3000; i++) pos[i] = (Math.random() - 0.5) * 3000;
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  scene.add(new THREE.Points(geo, new THREE.PointsMaterial({ color: 0x334466, size: 0.8, sizeAttenuation: true })));
})();

// Glow sprite texture for particles
export const spriteTexture = (() => {
  const c = document.createElement('canvas');
  c.width = 64; c.height = 64;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(32,32,0, 32,32,32);
  g.addColorStop(0, 'rgba(255,255,255,0.6)');
  g.addColorStop(0.2, 'rgba(255,255,255,0.35)');
  g.addColorStop(0.5, 'rgba(255,255,255,0.08)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0,0,64,64);
  return new THREE.CanvasTexture(c);
})();

export function updateSize() {
  state.W = container.clientWidth;
  state.H = container.clientHeight;
  camera.aspect = state.W / state.H;
  camera.updateProjectionMatrix();
  renderer.setSize(state.W, state.H);
  composer.setSize(state.W, state.H);
}
