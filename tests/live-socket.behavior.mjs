// Runs the page's live-socket block against a fake WebSocket and a fake clock: a socket that goes quiet is
// replaced, a socket that keeps ticking is left alone, and a tab that returns with a stale socket gets a new one.
import fs from 'node:fs';
import vm from 'node:vm';

const src = fs.readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('let _ws = null;');
const b = src.indexOf('/* ---- Studio views');
if (a < 0 || b < 0) { console.log('FAIL: live socket block not found'); process.exit(1); }
const block = src.slice(a, b);

let now = 1_000_000, fails = 0;
const timers = [];   // {at, fn, every}
const sockets = [];
class FakeWS {
  constructor(url) { this.url = url; this.readyState = 1; sockets.push(this); }
  send() {} close() { this.readyState = 3; if (this.onclose) this.onclose(); }
  say(o) { this.onmessage({ data: JSON.stringify(o) }); }
}
const doc = { hidden: false, listeners: {}, addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }, dispatchEvent() {} };
const win = { listeners: {}, addEventListener(t, f) { (this.listeners[t] ||= []).push(f); } };
const calls = { resync: 0 };
const ctx = vm.createContext({
  WebSocket: FakeWS, document: doc, window: win, location: { protocol: 'https:', host: 'x' },
  Date: { now: () => now }, CustomEvent: class {}, JSON,
  setTimeout: (fn, ms) => { const t = { at: now + ms, fn }; timers.push(t); return t; },
  clearTimeout: (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); },
  setInterval: (fn, ms) => { timers.push({ at: now + ms, fn, every: ms }); return 1; },
  loadDesks() { calls.resync++; }, loadAgentState() {}, loadDeskWork() {}, loadLastSeen() {}, refreshOpenChat() {},
  loadContextStatuses() {}, onScreen: () => false, dshLoadLive() {}, soon() {}, watchDeskLive() {}, onDeskLive() {},
  _live: {}, activeId: null, _peerTurn: new Map(), _dshRecent: {}, _dshDesks: new Map(), dshLoadNow() {},
});
function advance(ms) {
  const end = now + ms;
  for (;;) {
    timers.sort((x, y) => x.at - y.at);
    const t = timers[0];
    if (!t || t.at > end) break;
    now = t.at; timers.shift();
    if (t.every) timers.push({ at: now + t.every, fn: t.fn, every: t.every });
    t.fn();
  }
  now = end;
}
const ok = (name, cond) => { console.log((cond ? '  ok   ' : '  FAIL ') + name); if (!cond) fails++; };

vm.runInContext(block + '\nthis.__t = { reopen: () => _liveReopen(), healthy: () => _liveHealthy() };', ctx);
const T = ctx.__t;
ok('one socket is opened at start', sockets.length === 1);
sockets[0].say({ kind: 'open' });

// a socket that keeps ticking is left alone for ten minutes
for (let i = 0; i < 24; i++) { advance(25000); sockets[sockets.length - 1].say({ kind: 'tick' }); }
ok('a ticking socket is never replaced', sockets.length === 1);
ok('a ticking socket counts as healthy', T.healthy() === true);

// the socket goes quiet while the page is in view: replaced within about 85 s
const before = sockets.length;
advance(60000);
ok('60 s of silence does not replace it yet', sockets.length === before);
advance(30000);
ok('after 90 s of silence a new socket is open', sockets.length === before + 1);
ok('the quiet socket was closed', sockets[before - 1].readyState === 3);
sockets[sockets.length - 1].say({ kind: 'open' });
ok('the new socket is healthy once it speaks', T.healthy() === true);

// a hidden page does not churn sockets
doc.hidden = true;
const hiddenAt = sockets.length;
advance(300000);
ok('a hidden page does not open sockets', sockets.length === hiddenAt);

// back in view with a stale socket: the reopen is asked for, and it makes exactly one new socket
doc.hidden = false;
ok('a stale socket is not healthy', T.healthy() === false);
T.reopen();
ok('reopen gives exactly one new socket', sockets.length === hiddenAt + 1);
ok('reopen closed the stale one', sockets[hiddenAt - 1].readyState === 3);

console.log(fails ? 'FAIL' : 'PASS: live socket behaviour');
process.exit(fails ? 1 : 0);
