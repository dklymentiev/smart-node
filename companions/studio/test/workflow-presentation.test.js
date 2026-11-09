import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const publicDir = process.env.SN_STUDIO_TEST_PUBLIC ||
  fileURLToPath(new URL('../public/', import.meta.url));
const source = fs.readFileSync(path.join(publicDir, 'next2/js/app.js'), 'utf8');
const css = fs.readFileSync(path.join(publicDir, 'next2/css/app.css'), 'utf8');

function segment(from, to) {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `missing workflow code: ${from}`);
  return source.slice(start, end);
}

function functions() {
  const box = { innerHTML: '' };
  const graph = { offsetWidth: 0, querySelectorAll: () => [] };
  const context = vm.createContext({
    window: { addEventListener() {} },
    document: {
      querySelector: (selector) => selector === '.wfs-graph' ? graph : null,
      querySelectorAll: () => [],
    },
    $: (selector) => selector === '#workflowsBody' ? box : null,
    _wfList: [{ name: 'sample', title: 'Sample', steps: [
      { name: 'prepare', phase: 1 }, { name: 'review', phase: 2, depends_on: ['prepare'] },
    ] }],
    _wfState: { name: 'sample', level: 'detail' },
    ResizeObserver: class { observe() {} disconnect() {} },
    esc: (value) => String(value).replaceAll('&', '&amp;').replaceAll('"', '&quot;'),
    fetch: async () => ({ json: async () => ({ events: [] }) }),
    __box: box,
  });
  vm.runInContext(segment('const WF_ICON =', '/* ---- navigation between'), context);
  if (source.includes('let _wfs =')) {
    vm.runInContext(segment('let _wfs =', '/* ==== end workflow studio'), context);
  }
  return vm.runInContext(`({ wfGraphBoard, wfDrawEdgesIn,
    wfsBoard: typeof wfsBoard === 'function' ? wfsBoard : undefined,
    wfRenderDetail: typeof wfRenderDetail === 'function' ? wfRenderDetail : undefined,
    box: __box })`, context);
}

function rule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]+)\\}`));
  assert.ok(match, `missing CSS rule ${selector}`);
  return match[1];
}

test('branch graph uses rows and draws forward and return wires vertically', () => {
  const f = functions();
  const steps = [{ name: 'first' }, { name: 'second' }, { name: 'third' }];
  const edges = [{ from: 'first', to: 'second', label: '' },
    { from: 'second', to: 'first', label: 'again' }];
  const html = f.wfGraphBoard({}, true, steps, edges, new Set(['first', 'second']));
  assert.match(html, /wf-graph__col/);
  assert.match(rule('.wf-graph'), /flex-direction:\s*column/);
  assert.match(rule('.wf-graph__col'), /flex-direction:\s*row/);

  const svg = { attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, innerHTML: '' };
  const rects = { first: { left: 10, top: 10, width: 80, height: 30 },
    second: { left: 10, top: 100, width: 80, height: 30 } };
  const nodes = Object.keys(rects).map((name) => ({
    getAttribute: () => name, getBoundingClientRect: () => rects[name],
  }));
  f.wfDrawEdgesIn({
    querySelector: () => svg,
    querySelectorAll: () => nodes,
    getAttribute: () => JSON.stringify(edges),
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 150 }),
  });
  assert.match(svg.innerHTML, /M 50 40 C 50 70, 50 70, 50 100/);
  assert.match(svg.innerHTML, /M 90 115 C 150 115, 150 25, 90 25/);
});

test('workflow detail has a studio canvas and reports the run state', async () => {
  const f = functions();
  assert.equal(typeof f.wfsBoard, 'function');
  const board = f.wfsBoard({ steps: [
    { name: 'prepare', phase: 1 },
    { name: 'review', phase: 2, depends_on: ['prepare'] },
  ] });
  assert.match(board, /wfs-graph/);
  assert.match(board, /wfs-row/);
  assert.match(rule('.wfs-canvas'), /touch-action:\s*none/);
  assert.match(rule('.wfs-side'), /display:\s*flex/);
  assert.equal(typeof f.wfRenderDetail, 'function');
  f.wfRenderDetail();
  assert.match(f.box.innerHTML, /class="wfs-canvas"/);
  assert.match(f.box.innerHTML, /data-wfstab="history"/);
  assert.match(f.box.innerHTML, /data-wfstab="run"/);
  assert.match(f.box.innerHTML, /data-wfstab="block"/);

  const context = vm.createContext({
    fetch: async () => ({ json: async () => ({ events: [
      { event_type: 'workflow.step', ts: '2026-01-01T00:00:00Z', payload: {
        flow: 'sample', task_id: 8, blocks: { prepare: 'done', review: 'waiting' },
      } },
      { event_type: 'workflow.awaiting_review', ts: '2026-01-01T00:00:01Z', payload: {
        flow: 'sample', task_id: 8,
      } },
    ] }) }),
    Date, Map, String, Object,
  });
  vm.runInContext(segment('async function wfsRuns(', 'const wfsStateLabel'), context);
  const runs = await vm.runInContext('wfsRuns("sample")', context);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].state, 'review');
  assert.equal(runs[0].blocks.prepare, 'done');
});

test('sidebar agent names can truncate without shrinking the avatar', () => {
  assert.match(rule('.agent .sn-av'), /flex:\s*0\s+0\s+auto/);
  assert.match(rule('.agent > div'), /min-width:\s*0/);
  assert.match(rule('.agent .nm, .agent small'), /text-overflow:\s*ellipsis/);
});
