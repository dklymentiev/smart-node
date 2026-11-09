export const state = {
  // Documents
  allDocs: [],
  clusterInfo: {},

  // Taxonomy
  categoryColors: {},
  categoryNames: {},
  taxonomyLoaded: false,
  subcategoryData: {},
  subcategoryInfo: {},
  catCenters: {},

  // Three.js scene objects (set by other modules)
  particleSystem: null,
  nebulaeMeshes: [],
  chainLines: [],

  // View state
  viewMode: 'galaxy',
  posGalaxy: null,
  posTimeline: null,
  transitioning: false,
  transitionStart: 0,
  transitionFrom: null,
  transitionTo: null,

  // Timeline
  timelineLanes: [],
  dateRange: { min: 0, max: 0 },

  // Tooltip / selection
  hoveredIdx: -1,
  pinnedIdx: -1,
  selectedIdx: -1,
  selectedRing: null,

  // Search
  searchHighlight: null,
  originalColors: null,
  searchPositions: null,
  preSearchPositions: null,
  searchTransitioning: false,
  searchTransStart: 0,
  searchTransFrom: null,
  searchTransTo: null,
  searchMode: 'tx',
  localSearchId: 0,

  // Doc cards
  docCardPool: [],
  docCardThrottle: 0,

  // Viewport dimensions
  W: 0,
  H: 0,
};
