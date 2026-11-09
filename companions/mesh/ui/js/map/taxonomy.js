import { API, CAT_PALETTE, wsHeaders } from './constants.js';
import { state } from './state.js';

export async function loadTaxonomy() {
  try {
    const r = await fetch(API + '/categorizer/taxonomy', { headers: wsHeaders() });
    const data = await r.json();
    const cats = (data.taxonomy && data.taxonomy.categories) || [];
    cats.forEach((c, i) => {
      state.categoryNames[c.id] = c.name;
      state.categoryColors[c.id] = CAT_PALETTE[i % CAT_PALETTE.length];
      if (c.subcategories && c.subcategories.length) {
        state.subcategoryData[c.id] = c.subcategories.map(s => ({
          id: s.id, name: s.name, description: s.description || ''
        }));
      }
    });
    state.categoryNames['uncategorized'] = 'Uncategorized';
    state.categoryColors['uncategorized'] = '#607d8b';
    state.taxonomyLoaded = cats.length > 0;
  } catch(e) { console.warn('Taxonomy not available:', e); }
}

export function getCategoryId(doc) {
  const tags = doc.tags || [];
  for (const t of tags) {
    if (t.startsWith('ai-category:')) return t.slice(12);
  }
  return null;
}

export function getSubcategoryId(doc) {
  const tags = doc.tags || [];
  for (const t of tags) {
    if (t.startsWith('ai-subcategory:')) return t.slice(15);
  }
  return null;
}

export function getProjectGuid(doc) {
  const tags = doc.tags || [];
  for (const t of tags) {
    if (t.startsWith('guid:')) return t.slice(5);
  }
  return null;
}

export function getCatColor(catId) {
  return state.categoryColors[catId] || '#555';
}

export function getCatName(catId) {
  return state.categoryNames[catId] || catId || 'unknown';
}

export function extractTagLabel(t) {
  if (t.startsWith('doci-path:')) return t.split('/').pop().replace(/\.md$/, '');
  if (t.startsWith('topic:')) return t.slice(6);
  if (t.includes(':')) return t.split(':').slice(1).join(':');
  return t;
}
