// Runs the page's pull-to-refresh block against fake touch events: a long pull from the top refreshes, a short one,
// a pull inside a scrolled list, a pull in a text field and a sideways drag do not.
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('(function pullToRefresh()');
const b = src.indexOf('/* ---- Studio views');
if (a < 0 || b < 0) { console.log('FAIL: pull-to-refresh block not found'); process.exit(1); }
const block = src.slice(a, b);

let fails = 0;
const ok = (name, cond) => { console.log((cond ? '  ok   ' : '  FAIL ') + name); if (!cond) fails++; };
const listeners = {};
const mk = (o = {}) => ({ scrollTop: 0, parentElement: null, closest: () => null, ...o });
const appRoot = mk({ style: {}, appendChild(el) { doc.bar = el; } });
const page = mk(); const doc = { querySelector: (q) => (q === '.app' ? appRoot : null), documentElement: page, body: { appendChild(el) { doc.bar = el; } }, listeners,
  addEventListener(t, f) { (listeners[t] ||= []).push(f); },
  createElement: () => ({ style: { setProperty(k, v) { this[k] = v; } }, setAttribute(k, v) { this['attr:' + k] = v; },
    classList: { toggle(c, on) { this.busy = on; } }, className: '' }) };
const state = { reopen: 0, reloaded: 0, build: '1', timers: [] };
const win = { ontouchstart: null, __BUILD__: '1', location: { reload() { state.reloaded++; } } };
const ctx = vm.createContext({
  document: doc, window: win, location: win.location, Math, Date, String, Number,
  fetch: async () => ({ ok: true, json: async () => ({ build: state.build }) }),
  setTimeout: (f, ms) => { state.timers.push([f, ms]); return 1; },
  _liveReopen: () => { state.reopen++; },
});
Object.defineProperty(ctx, 'ontouchstart', { value: null });
vm.runInContext('var ontouchstart = null;', ctx);
ctx.window.ontouchstart = null;
// 'ontouchstart' in window must be true
const wctx = ctx.window; wctx.ontouchstart = null;

vm.runInContext(block, ctx);
const fire = (t, e) => (listeners[t] || []).forEach((f) => f(e));
const touch = (y, x = 100) => ({ touches: [{ clientY: y, clientX: x }], target: stub });
let stub = mk({ parentElement: page });
const flush = async () => { await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r)); };
async function pull(y1, opts = {}) {
  if (opts.target) stub = opts.target;
  fire('touchstart', touch(100));
  fire('touchmove', touch(y1, opts.x ?? 100));
  fire('touchend', {});
  await flush();
  stub = mk({ parentElement: page });
}

fire('touchstart', touch(100)); fire('touchmove', touch(100 + 60));
ok('the screen follows the finger down while pulling', /translateY\(30px\)/.test(appRoot.style.transform || ''));
fire('touchend', {}); await flush();
state.timers.forEach(([f]) => f());
ok('the screen is put back after a short pull, with no transform left', appRoot.style.transform === '' && appRoot.style.transition === '');

appRoot.style.transform = 'translateY(15px)';   // a pull whose end never arrived
fire('touchstart', touch(100));
ok('a screen left shifted by a lost gesture is put back at the next touch', appRoot.style.transform === '');
fire('touchend', {});

await pull(100 + 40);
ok('a short pull does nothing', state.reopen === 0 && state.reloaded === 0);
state.timers.forEach(([f]) => f());

await pull(100 + 120);
ok('a long pull from the top opens a new live socket', state.reopen === 1);
ok('the same build is not reloaded', state.reloaded === 0);
ok('the screen is held down while it refreshes', /translateY\(/.test(appRoot.style.transform || ''));
ok('a line along the top of the screen shows progress, not words', doc.bar && doc.bar.style['--ptr-fill'] === '1' && !doc.bar.textContent);
state.timers.forEach(([f]) => f());   // the bar goes away, busy clears
state.timers.forEach(([f]) => f());
ok('and put back when it is done', appRoot.style.transform === '');

const before = state.reopen;
await pull(100 + 120, { target: mk({ scrollTop: 300, parentElement: page }) });
ok('a pull inside a list that is scrolled does nothing', state.reopen === before);

await pull(100 + 120, { target: mk({ closest: () => ({}), parentElement: page }) });
ok('a pull that starts in a text field does nothing', state.reopen === before);

await pull(100 + 120, { x: 400 });
ok('a mostly sideways drag does nothing', state.reopen === before);

state.build = '2';
await pull(100 + 120);
ok('a newer build reloads the page', state.reloaded === 1);

console.log(fails ? 'FAIL' : 'PASS: pull to refresh behaviour');
process.exit(fails ? 1 : 0);
