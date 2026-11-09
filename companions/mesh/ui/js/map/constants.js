export const API = window.location.origin;

export function getWorkspace() {
  const el = document.getElementById('ctl-workspace');
  return el ? el.value : '';
}

export function wsHeaders(extra) {
  const h = extra ? { ...extra } : {};
  const ws = getWorkspace();
  if (ws) h['X-Workspace'] = ws;
  return h;
}

export const CAT_PALETTE = [
  '#4fc3f7','#81c784','#ff8a65','#ba68c8','#ffd54f',
  '#4db6ac','#e57373','#64b5f6','#aed581','#ffb74d',
  '#f06292','#7986cb','#a1887f','#90a4ae','#dce775'
];

export const SKIP_RE = /^(type|date|source|session|agent|status|doci-folder|guid|project|ai-category|ai-subcategory):/;

export const TRANSITION_MS = 1200;
export const SEARCH_TRANS_MS = 800;
export const TL = { xSpread: 700, laneH: 22 };
export const DOC_CARD_POOL_SIZE = 40;
