// Smart Node Studio -- unified end-user client.
//
// Serves the static shell (public/) and acts as a backend-for-frontend (BFF):
// section data is proxied to the existing node services, forwarding the
// Authum-injected `Remote-User` identity. Auth lives at the edge (Traefik +
// Authum forwardAuth); we never authenticate here, only read who Authum says
// the user is and proxy on their behalf.

import express from 'express';
import { WebSocketServer } from 'ws';
import { readFileSync, createWriteStream, existsSync, unlink, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';
import { execFile } from 'node:child_process';
import https from 'node:https';
import http from 'node:http';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Build stamp -- changes every restart (i.e. every deploy). Injected into the
// shell's asset URLs (?v=BUILD) so the browser always fetches fresh CSS/JS and
// we stop fighting the cache.
const BUILD = String(Date.now());

// Required config -- fail loud if missing (no silent fallbacks).
const PORT = process.env.PORT;
const HOST = process.env.HOST;
const AUTHUM_AUTH_URL = process.env.AUTHUM_AUTH_URL; // e.g. https://auth.example.com
const CORE_API_URL = process.env.CORE_API_URL;       // node controller (set by installer)
const BRIN_URL = process.env.BRIN_URL;               // e.g. http://127.0.0.1:8101 (agent router)
const BRIN_API_KEY = process.env.BRIN_API_KEY;       // X-API-Key for brin /api/*
// No PLANNER_URL. Tasks go through core-api -- see proxyPlanner below.
// Optional: the documents companion may not be installed (studio.manifest.json).
// Empty means "no documents on this node", and /api/docs says so with a 404.
const DOCI_URL = process.env.DOCI_URL || '';          // documents backend (set by installer)
const AVATAR_STATE_DIR = process.env.SN_AVATAR_STATE_DIR; // durable custom uploads (read-only here)
const MESH_API_URL = process.env.MESH_API_URL || 'https://mesh.local'; // agent memory store (https, internal CA)
// The domain the node's OWN mailboxes live on (the same value core-api knows as
// SN_MSGID_DOMAIN). Optional: when unset the UI simply labels fewer addresses as
// agents. It must never be a literal -- every node has its own mail domain, and
// a literal here is wrong on every node but the one it was written on.
const SN_MAIL_DOMAIN = (process.env.SN_MAIL_DOMAIN || '').trim().toLowerCase();
// The voice interface's public address, for the chat card's Voice button.
// Resolved the way companions/voice.sh publishes the route: the environment, then
// routing.env (B03), then voice.<domain>. Never a literal: example-node's is line.,
// not voice. null when the voice companion is not installed, so the UI offers no
// button that leads nowhere.
const VOICE_URL = (() => {
  if (!existsSync('/opt/smart-node/voice')) return null;
  const env = {};
  try {
    for (const l of readFileSync('/etc/smart-node/routing.env', 'utf8').split('\n')) {
      const m = l.match(/^\s*([A-Z0-9_]+)=["']?([^"'\s]*)["']?\s*$/);
      if (m) env[m[1]] = m[2];
    }
  } catch { /* no routing contract: fall through to the environment only */ }
  const host = (process.env.SMART_NODE_VOICE_HOSTNAME || env.SMART_NODE_VOICE_HOSTNAME
    || (env.SMART_NODE_DOMAIN ? 'line.' + env.SMART_NODE_DOMAIN : '')).trim();
  // a node under .localhost is served on plain http (bootstrap/lib.sh web_local)
  const local = /(^|\.)localhost$/.test(host);
  return host ? (local ? 'http://' : 'https://') + host + '/' : null;
})();
// Proxy proof. Studio sits BEHIND smart-node-auth, so a request that
// reaches it has already had its session authenticated by Authum -- but Studio
// then relays to core-api directly over the docker0 gateway, bypassing Traefik
// and therefore the header Traefik would have stamped. Without this, an owner
// approving a task in Studio is indistinguishable from an agent on the node
// asserting `Remote-User: admin`, and core-api correctly refuses both.
//
// Optional on purpose: a Studio from before this change, or a node whose
// core-api has no secret, keeps working for everything that is not gated.
// What it cannot do is approve, and the refusal says so.
const PROXY_AUTH_SECRET = process.env.SN_PROXY_AUTH_SECRET || '';
for (const [k, v] of Object.entries({ PORT, HOST, AUTHUM_AUTH_URL, CORE_API_URL, BRIN_URL, BRIN_API_KEY, SN_AVATAR_STATE_DIR: AVATAR_STATE_DIR })) {
  if (!v) {
    console.error(`[studio] missing required env ${k} -- refusing to start`);
    process.exit(1);
  }
}

// Present is not the same as usable. A drop-in that derives an address can hand
// us something non-empty and meaningless -- we ran for weeks on
// DOCI_URL=http:///bin/bash, which passes the check above, starts cleanly, and
// serves an empty documents page while every request lands somewhere that is
// not the documents backend.
//
// Note that new URL() does NOT catch it: WHATWG parsing collapses the empty
// authority, so `http:///bin/bash` becomes host `bin`, path `/bash` -- a
// perfectly valid URL to a host that does not exist. The tell is in the raw
// string: an empty authority, `scheme:///`. That is never something anyone
// writes on purpose; it is what you get when a substitution produced nothing,
// which is exactly the failure mode we hit. Refusing here turns a silent wrong
// answer into a startup failure that names the variable.
for (const [k, v] of Object.entries({ AUTHUM_AUTH_URL, CORE_API_URL, BRIN_URL, ...(DOCI_URL ? { DOCI_URL } : {}) })) {
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/\//.test(v)) {
    console.error(`[studio] env ${k} has an empty host: ${v} -- refusing to start`);
    process.exit(1);
  }
  let u;
  try {
    u = new URL(v);
  } catch {
    console.error(`[studio] env ${k} is not a URL: ${v} -- refusing to start`);
    process.exit(1);
  }
  if (!/^https?:$/.test(u.protocol) || !u.hostname) {
    console.error(`[studio] env ${k} has no usable host: ${v} -- refusing to start`);
    process.exit(1);
  }
}

const app = express();
app.use(express.json());

// Read the identity Authum forwarded. Behind the edge it is always present; if
// absent the auth layer was not applied -- surface that rather than fake it.
function remoteUser(req, res) {
  const u = req.headers['remote-user'];
  if (!u) {
    res.status(401).json({ error: 'missing Remote-User (auth not applied)' });
    return null;
  }
  return u;
}

// The headers every relay to core-api carries: WHO, and how strongly we know
// it. One function so a new relay cannot forget the second half -- the reason
// Studio needs this at all is that it is a second door onto the same API, and
// two doors with different rules is the recurring shape of every one of these
// defects.
//
// The proof is attached to the identity Authum gave us and to nothing else:
// remoteUser() above refuses a request without one, so Studio can never stamp
// a session onto an identity a caller chose for itself.
function relayHeaders(user, extra = {}) {
  const h = { 'Remote-User': user, Accept: 'application/json', ...extra };
  if (PROXY_AUTH_SECRET) h['X-SN-Proxy-Auth'] = PROXY_AUTH_SECRET;
  return h;
}

// Proxy a request to core-api on behalf of the logged-in user, forwarding the
// method and (for writes) a JSON body. Read-only callers pass no opts.
async function proxyCore(req, res, path, { method = 'GET', body } = {}) {
  const user = remoteUser(req, res);
  if (!user) return;
  const init = { method, headers: relayHeaders(user) };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  try {
    const upstream = await fetch(`${CORE_API_URL}${path}`, init);
    const text = await upstream.text();
    res.status(upstream.status).type(upstream.headers.get('content-type') || 'application/json').send(text);
  } catch (e) {
    res.status(502).json({ error: 'core-api unreachable', detail: String(e) });
  }
}

// The planner names a reviewer by the person's handle (sn_config.users.handle),
// while Authum stamps the login (Remote-User). They are different strings for the
// same person, so the panel cannot tell "you are the reviewer" from the login
// alone. core-api resolves login -> person; a miss or an outage leaves handle
// and role null and the panel behaves exactly as before. The role says whether
// Settings offers the node panel (an admin's); the panel checks for itself.
async function mePerson(user) {
  if (!user) return {};
  try {
    const r = await fetch(
      `${CORE_API_URL}/api/users/resolve-identity?identity=${encodeURIComponent(user)}`,
      { headers: relayHeaders(user) },
    );
    if (!r.ok) return {};
    const j = await r.json();
    if (!j || !j.found) return {};
    return { handle: j.handle ? String(j.handle) : null, role: j.role ? String(j.role) : null };
  } catch (e) {
    return {};
  }
}

// Who may see a command line. A tool step's text carries the command itself: a
// path, the name of a secret. Only the people who run the node read it; everyone
// else gets the step's plain description and the tool's name. Role is cached for
// a minute: the chat polls this route several times a second.
const _roleCache = new Map();
async function isNodeAdmin(user) {
  const hit = _roleCache.get(user);
  if (hit && Date.now() - hit.at < 60000) return hit.admin;
  const person = await mePerson(user);
  const admin = ['super-admin', 'admin'].includes(person.role);
  // A failed lookup (no role) is not cached: the next poll asks again.
  if (person.role) _roleCache.set(user, { admin, at: Date.now() });
  return admin;
}
function withoutCommands(data) {
  if (!data || !Array.isArray(data.steps)) return data;
  return { ...data, steps: data.steps.map((st) => st && st.kind === 'tool' && typeof st.text === 'string'
    ? { ...st, text: st.text.split(':')[0] } : st) };
}

app.get('/api/me', async (req, res) => {
  const user = req.headers['remote-user'] || null;
  const person = await mePerson(user);
  res.json({
    user,
    handle: person.handle || null,
    role: person.role || null,
    authUrl: AUTHUM_AUTH_URL,
    // So the UI can tell one of this node's own mailboxes from an outsider's
    // without a domain written into its source. null is valid.
    mailDomain: SN_MAIL_DOMAIN || null,
    voiceUrl: VOICE_URL,
  });
});

// My Studio -- real agents from core-api (read-only for now).
app.get('/api/agents', (req, res) => proxyCore(req, res, '/api/agents'));

// The contacts registry flattened to handle -> display name, for feed labels.
//
// WHY THIS EXISTS. The panel used to mask every value containing '@' to a
// channel-generic word ("A customer", "Website visitor"). That is the else
// branch of a refusal to print addresses, not a statement about who wrote: on a
// node with real correspondence a client, the owner's own second address, a
// supplier and a bounce all rendered identically, and the owner could not tell
// them apart. Studio is served behind smart-node-auth, so everyone who reaches
// this panel is already signed in -- inside the product the mask was hiding the
// owner's own correspondence from the owner. The generic words belong on the
// public landing panel, where the viewer really is a stranger.
//
// Resolution happens here because contacts live in core-api's database and the
// browser has no way to ask. A MISS IS NOT AN ERROR: an address nobody has
// entered as a contact renders as itself, which is still the truth and still
// more than the mask said. So core-api being down costs names, never the feed.
const DIRECTORY_TTL_MS = 60_000;
let directoryCache = { at: 0, handles: {} };

async function directoryHandles(user) {
  const now = Date.now();
  if (directoryCache.at && now - directoryCache.at < DIRECTORY_TTL_MS) return directoryCache.handles;
  try {
    const upstream = await fetch(`${CORE_API_URL}/api/contacts`, { headers: relayHeaders(user) });
    if (!upstream.ok) throw new Error('http ' + upstream.status);
    const data = await upstream.json();
    const handles = {};
    for (const c of data.contacts || []) {
      const name = String(c.display_name || '').trim();
      if (!name || c.enabled === false) continue;
      for (const ch of c.channels || []) {
        if (ch.enabled === false) continue;
        const h = String(ch.handle || '').trim().toLowerCase();
        if (h) handles[h] = name;
      }
    }
    directoryCache = { at: now, handles };
    return handles;
  } catch (e) {
    // Serve the last good map rather than nothing; empty is a valid answer.
    console.warn('[studio] contacts unavailable, labels fall back to addresses:', String(e));
    return directoryCache.handles;
  }
}

app.get('/api/directory', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  res.json({ handles: await directoryHandles(user) });
});

// Avatar library -- the static catalog under public/avatars/. Files ARE the
// catalog (no DB table): sub-dir + filename -> a key an agent's `avatar` field
// can point at. `crew/` is package content; `custom/` is durable node state.
// They deliberately share the legacy /avatars/<key> URL namespace so moving the
// bytes does not rewrite any agent row. Assigning an avatar = PUT that key onto
// the agent (core-api), a pure data change.
const AVATARS_ROOT = join(__dirname, '..', 'public', 'avatars');
const AVATAR_EXT = new Set(['.webp', '.png', '.jpg', '.jpeg', '.svg', '.gif']);
app.get('/api/avatars', (_req, res) => {
  const out = [];
  // Every folder of public/avatars is a pack (crew, knot, bot, ...; order and labels in
  // public/avatars/packs.json), plus the uploaded ones.
  const packs = existsSync(AVATARS_ROOT)
    ? readdirSync(AVATARS_ROOT, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort()
    : [];
  for (const [group, dir] of [...packs.map((p) => [p, join(AVATARS_ROOT, p)]), ['custom', AVATAR_STATE_DIR]]) {
    if (!existsSync(dir)) continue;
    for (const file of readdirSync(dir).sort()) {
      const dot = file.lastIndexOf('.');
      if (dot < 1 || !AVATAR_EXT.has(file.slice(dot).toLowerCase())) continue;
      const key = `${group}/${file}`;
      out.push({ key, group, name: file.slice(0, dot), url: `/avatars/${key}` });
    }
  }
  res.json(out);
});

// Uploads are raw bytes (the browser already uses application/octet-stream).
// Studio authenticates the session and relays them; core-api is the only
// application process allowed to write avatar state or change the DB pointer.
app.post('/api/agents/:slug/avatar', express.raw({ type: () => true, limit: '3mb' }), async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  try {
    const ext = encodeURIComponent(String(req.query.ext || 'png'));
    const upstream = await fetch(`${CORE_API_URL}/api/agents/${encodeURIComponent(req.params.slug)}/avatar?ext=${ext}`, {
      method: 'POST',
      headers: relayHeaders(user, { 'Content-Type': 'application/octet-stream' }),
      body: req.body,
    });
    const text = await upstream.text();
    res.status(upstream.status).type(upstream.headers.get('content-type') || 'application/json').send(text);
  } catch (e) {
    res.status(502).json({ error: 'core-api unreachable', detail: String(e) });
  }
});
app.get('/api/workflows', (req, res) => proxyCore(req, res, '/api/workflows'));
app.post('/api/workflows/:name/run', (req, res) => proxyCore(req, res, `/api/workflows/${encodeURIComponent(req.params.name)}/run`, { method: 'POST', body: req.body ?? {} }));
app.get('/api/events', (req, res) => proxyCore(req, res, req.originalUrl));
app.get('/api/context-status', (req, res) => proxyCore(req, res, req.originalUrl));
// Work layer: facts over the event log. /now is the live board; /map is the
// weekly picture served to the standalone /work-map page below.
app.get('/api/work/now', (req, res) => proxyCore(req, res, req.originalUrl));
app.get('/api/work/map', (req, res) => proxyCore(req, res, req.originalUrl));

// Desks -- the user's workspaces. List/create/delete proxied to core-api, which
// scopes them to the forwarded identity (owner + tenant).
app.get('/api/desks', (req, res) => proxyCore(req, res, '/api/desks'));
app.get('/api/desks/unread', (req, res) => proxyCore(req, res, '/api/desks/unread'));
app.post('/api/desks', (req, res) => proxyCore(req, res, '/api/desks', { method: 'POST', body: req.body ?? {} }));
app.delete('/api/desks/:id', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}`, { method: 'DELETE' }));
app.patch('/api/desks/:id', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}`, { method: 'PATCH', body: req.body ?? {} }));

// A desk's attachments: stored by core-api in the node's one
// attachment store, the desk as the parent. The browser sends the raw bytes as
// application/octet-stream and names the real type in X-Content-Type. Sent as
// its own Content-Type, a .json file would be taken by express.json() above as a
// request body instead of reaching core-api as a file.
app.post('/api/desks/:id/attachments', express.raw({ type: () => true, limit: '26mb' }), async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  try {
    const upstream = await fetch(`${CORE_API_URL}/api/desks/${encodeURIComponent(req.params.id)}/attachments`, {
      method: 'POST',
      headers: relayHeaders(user, {
        'Content-Type': req.headers['x-content-type'] || 'application/octet-stream',
        'X-Filename': req.headers['x-filename'] || '',
      }),
      body: req.body,
    });
    const text = await upstream.text();
    res.status(upstream.status).type(upstream.headers.get('content-type') || 'application/json').send(text);
  } catch (e) {
    res.status(502).json({ error: 'core-api unreachable', detail: String(e) });
  }
});
app.get('/api/desks/:id/attachments', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}/attachments`));
// Bytes, not text: proxyCore would decode an image as a string and ruin it.
// The headers that decide how a browser treats the file (type, inline or
// download, nosniff) are passed through exactly as core-api set them.
app.get('/api/desks/:id/attachments/:aid/content', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  try {
    const upstream = await fetch(
      `${CORE_API_URL}/api/desks/${encodeURIComponent(req.params.id)}/attachments/${encodeURIComponent(req.params.aid)}/content`,
      { headers: relayHeaders(user) });
    res.status(upstream.status);
    for (const h of ['content-type', 'content-disposition', 'x-content-type-options', 'cache-control']) {
      const v = upstream.headers.get(h);
      if (v) res.set(h, v);
    }
    res.send(Buffer.from(await upstream.arrayBuffer()));
  } catch (e) {
    res.status(502).json({ error: 'core-api unreachable', detail: String(e) });
  }
});
app.delete('/api/desks/:id/attachments/:aid', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}/attachments/${encodeURIComponent(req.params.aid)}`, { method: 'DELETE' }));

// Desk chat -- proxy one message to brin (the agent router), scoped to the desk.
// The desk id is the conversation key, so each desk is its own thread; no agent
// is named, so brin replies with its configured default agent (the secretary).
// @name in a chat message: the Studio writes one message on the bus for that
// agent (POST /api/desks/:id/mentions), the same primitive as a mention in a task
// comment, and the agent's answer comes back into this chat through the reply
// relay. The rules are the planner's (tracking/src/planner/mentions.py): `@` not
// glued to a word, outside fenced code, quoted lines and inline code. Every name
// that is an agent other than the chat's own gets its own message, five at most
//. core-api says which names are not agents (404) or
// are the chat's own agent (409); those are skipped. A failure here never stops
// the message reaching the chat.
const MENTION_RE = /(?<![\w@./-])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})/g;
function mentionCandidates(text) {
  const names = [];
  let fenced = false;
  for (const line of String(text || '').split('\n')) {
    const s = line.trimStart();
    if (s.startsWith('```') || s.startsWith('~~~')) { fenced = !fenced; continue; }
    if (fenced || s.startsWith('>')) continue;
    for (const m of line.replace(/`[^`]*`/g, '').matchAll(MENTION_RE)) {
      const name = m[1].replace(/\.+$/, '');
      if (name && !names.includes(name)) names.push(name);
    }
  }
  return names;
}
const MENTION_MAX = 5;
async function writeMention(user, deskId, message) {
  const told = [];
  for (const name of mentionCandidates(message)) {
    if (told.length >= MENTION_MAX) break;
    try {
      const r = await fetch(`${CORE_API_URL}/api/desks/${encodeURIComponent(deskId)}/mentions`, {
        method: 'POST',
        headers: relayHeaders(user, { 'Content-Type': 'application/json' }),
        body: JSON.stringify({ agent: name, content: message, typed: '@' + name }),
      });
      if (r.ok) {
        const agent = (await r.json()).agent || name;
        if (!told.includes(agent)) told.push(agent);
        continue;
      }
      if (r.status !== 404 && r.status !== 409) console.warn(`mention @${name} in desk ${deskId}: core-api ${r.status}`);
    } catch (e) {
      console.warn(`mention @${name} in desk ${deskId}: ${e}`);
    }
  }
  return told;
}

// A reply to one colleague's message in this chat: to that agent ONLY. The
// chat's own agent gets nothing (core-api answers 409 if it is named), and the
// chat shows the text as the person's turn from the bus row.
// A message for colleagues only: a reply to one of a colleague's messages, or a
// message that starts with @colleague. Each named agent gets its
// own bus row and the chat's agent gets nothing; the first row is the person's
// turn in the chat, the others say turn=false so the message shows once.
async function tellDirect(user, deskId, message, agents, told = []) {
  let last = null;
  for (const agent of agents) {
    try {
      const r = await fetch(`${CORE_API_URL}/api/desks/${encodeURIComponent(deskId)}/mentions`, {
        method: 'POST',
        headers: relayHeaders(user, { 'Content-Type': 'application/json' }),
        body: JSON.stringify({ agent, content: message, typed: '@' + agent, direct: true,
          turn: told.length === 0 }),
      });
      if (r.ok) { told.push((await r.json()).agent || agent); continue; }
      last = { status: r.status, body: await r.text(), type: r.headers.get('content-type') };
    } catch (e) {
      last = { status: 502, body: JSON.stringify({ error: 'core-api unreachable', detail: String(e) }) };
    }
  }
  return { told, last };
}

app.post('/api/desks/:id/reply', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const message = ((req.body && req.body.message) || '').trim();
  const b = req.body || {};
  const agents = [...new Set((Array.isArray(b.agents) ? b.agents : [b.agent])
    .map((a) => String(a || '').trim()).filter(Boolean))].slice(0, MENTION_MAX);
  if (!message || !agents.length) return res.status(400).json({ error: 'message and agent are required' });
  const { told, last } = await tellDirect(user, req.params.id, message, agents);
  if (told.length) return res.status(201).json({ agent: told[0], agents: told });
  res.status(last.status).type(last.type || 'application/json').send(last.body);
});

// Idempotency cache for chat POSTs keyed by (user, desk, client_turn_id). A
// browser retry, a double-tap send, or a socket drop that fires the same turn
// again within the window gets the first response back instead of creating a
// second inbound message. Only successful upstream responses (2xx) are cached:
// a brin error is a legitimate retry opportunity. Size-bounded; stale rows are
// evicted on access.
const _chatTurnCache = new Map();
const _chatTurnMax = 2000;
const _chatTurnTtlMs = 60 * 1000;
function _chatTurnKey(user, deskId, turnId) { return `${user}|${deskId}|${turnId}`; }
function _chatTurnGet(key) {
  const row = _chatTurnCache.get(key);
  if (!row) return null;
  if (Date.now() - row.at > _chatTurnTtlMs) { _chatTurnCache.delete(key); return null; }
  return row;
}
function _chatTurnPut(key, row) {
  if (_chatTurnCache.size >= _chatTurnMax) {
    const firstKey = _chatTurnCache.keys().next().value;
    if (firstKey) _chatTurnCache.delete(firstKey);
  }
  _chatTurnCache.set(key, { ...row, at: Date.now() });
}

app.post('/api/desks/:id/chat', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const message = ((req.body && req.body.message) || '').trim();
  if (!message) return res.status(400).json({ error: 'empty message' });
  const agent = (req.body && req.body.agent) || undefined;
  const clientTurnId = req.body && req.body.client_turn_id;
  if (clientTurnId && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(clientTurnId)) {
    return res.status(400).json({ error: 'invalid client turn id' });
  }
  const dedupKey = clientTurnId ? _chatTurnKey(user, req.params.id, clientTurnId) : null;
  if (dedupKey) {
    const cached = _chatTurnGet(dedupKey);
    if (cached) {
      res.set('X-SN-Dedup', 'hit');
      return res.status(cached.status).type(cached.type || 'application/json').send(cached.body);
    }
  }
  // A message that starts with @colleague is for the colleagues it names, not
  // for the chat's agent. The old workspace decided that in the browser, so the
  // new one sent "@otto ..." here and both the chat's agent and otto answered
  // (2026-10-29). The first name decides: the chat's own agent (409) or no
  // agent (404) leaves the message the chat's.
  const names = /^\s*@/.test(message) ? mentionCandidates(message) : [];
  if (names.length) {
    const { told } = await tellDirect(user, req.params.id, message, names.slice(0, 1));
    if (told.length) {
      await tellDirect(user, req.params.id, message, names.slice(1, MENTION_MAX), told);
      return res.status(201).json({ agent: told[0], agents: told });
    }
  }
  const mentioned = await writeMention(user, req.params.id, message);
  if (mentioned.length) res.set('X-SN-Mentioned', mentioned.join(','));
  try {
    const upstream = await fetch(`${BRIN_URL}/api/message`, {
      method: 'POST',
      headers: { 'X-API-Key': BRIN_API_KEY, 'Content-Type': 'application/json', Accept: 'application/json' },
      // channel: a person is waiting in this chat, so its session takes a
      // person slot. Without it brin filed every Studio chat as background
      // work, and five open chats used up the background pool (2026-10-29).
      body: JSON.stringify({ message, agent, client_turn_id: clientTurnId, channel: 'web-chat',
        stream: false, context: { user_id: `desk:${req.params.id}`, studio_user: user } }),
    });
    const out = await upstream.text();
    const ct = upstream.headers.get('content-type') || 'application/json';
    if (dedupKey && upstream.status >= 200 && upstream.status < 300) {
      _chatTurnPut(dedupKey, { status: upstream.status, type: ct, body: out });
    }
    res.status(upstream.status).type(ct).send(out);
  } catch (e) {
    res.status(502).json({ error: 'brin unreachable', detail: String(e) });
  }
});

app.post('/api/desks/:id/steer', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const text = ((req.body && req.body.text) || '').trim();
  if (!text || text.length > 8000) return res.status(400).json({ status: 'invalid_text' });
  try {
    const mine = await fetch(`${CORE_API_URL}/api/desks`, { headers: relayHeaders(user) });
    if (!mine.ok) return res.status(mine.status).json({ status: 'unavailable' });
    if (!(((await mine.json()).desks) || []).some((d) => d.id === req.params.id)) {
      return res.status(404).json({ status: 'no_such_chat' });
    }
    const upstream = await fetch(`${BRIN_URL}/api/desks/${encodeURIComponent(req.params.id)}/steer`, {
      method: 'POST',
      headers: { 'X-API-Key': BRIN_API_KEY, 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ text }),
    });
    res.status(upstream.status).type('application/json').send(await upstream.text());
  } catch (e) {
    res.status(502).json({ status: 'unavailable' });
  }
});

app.post('/api/desks/:id/cancel-queued', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const clientTurnId = req.body && req.body.client_turn_id;
  if (typeof clientTurnId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(clientTurnId)) {
    return res.status(400).json({ status: 'invalid_turn_id' });
  }
  try {
    const mine = await fetch(`${CORE_API_URL}/api/desks`, { headers: relayHeaders(user) });
    if (!mine.ok) return res.status(mine.status).json({ status: 'unavailable' });
    if (!(((await mine.json()).desks) || []).some((d) => d.id === req.params.id)) {
      return res.status(404).json({ status: 'no_such_chat' });
    }
    const upstream = await fetch(`${BRIN_URL}/api/desks/${encodeURIComponent(req.params.id)}/cancel-queued`, {
      method: 'POST',
      headers: { 'X-API-Key': BRIN_API_KEY, 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ client_turn_id: clientTurnId }),
    });
    res.status(upstream.status).type('application/json').send(await upstream.text());
  } catch (e) {
    res.status(502).json({ status: 'unavailable' });
  }
});

// Colleagues still working on a mention from this chat (core-api reads the bus).
app.get('/api/desks/:id/waiting', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}/waiting`));
// ?after=<created_at>: only the messages after the newest one the page has
app.get('/api/desks/:id/history', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}/history`
    + (typeof req.query.after === 'string' ? `?after=${encodeURIComponent(req.query.after)}` : '')));
app.get('/api/desks/:id/sessions', (req, res) =>
  proxyCore(req, res, `/api/desks/${encodeURIComponent(req.params.id)}/sessions`));

// Live turn activity for a desk -- proxied to brin which proxies to the gateway.
// Returns {in_progress, elapsed, action} while a CLI turn is running for this desk.
// Falls back to {in_progress: false} on any error so the caller can always parse it.
// Polled every 2s by the chat UI to update the status label.
app.get('/api/desks/:id/activity', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  try {
    const mine = await fetch(`${CORE_API_URL}/api/desks`, { headers: relayHeaders(user) });
    if (!mine.ok) return res.status(mine.status).json({ in_progress: false });
    if (!(((await mine.json()).desks) || []).some((d) => d.id === req.params.id)) {
      return res.status(404).json({ in_progress: false });
    }
    // `since` is the caller's steps cursor -- without it every poll
    // would hand back the whole ring and the chat would redraw what it has.
    const since = Number(req.query.since) || 0;
    const r = await fetch(`${BRIN_URL}/api/desks/${encodeURIComponent(req.params.id)}/activity?since=${since}`,
      { headers: { 'X-API-Key': BRIN_API_KEY, Accept: 'application/json' } });
    const data = await r.json().catch(() => ({}));
    res.status(r.status).json((await isNodeAdmin(user)) ? data : withoutCommands(data));
  } catch (e) {
    res.json({ in_progress: false });
  }
});

// The same activity pushed as it changes: the chat keeps this open while
// it is on screen and shows what the agent does and the text so far, with no
// polling. Only the chat's owner may watch it, as for /activity.
app.get('/api/desks/:id/live', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const ac = new AbortController();
  req.on('close', () => ac.abort());
  try {
    const mine = await fetch(`${CORE_API_URL}/api/desks`, { headers: relayHeaders(user) });
    if (!mine.ok) return res.status(mine.status).end();
    if (!(((await mine.json()).desks) || []).some((d) => d.id === req.params.id)) return res.status(404).end();
    const upstream = await fetch(`${BRIN_URL}/api/desks/${encodeURIComponent(req.params.id)}/live`,
      { headers: { 'X-API-Key': BRIN_API_KEY, Accept: 'text/event-stream' }, signal: ac.signal });
    if (!upstream.ok) return res.status(upstream.status).end();
    res.status(200).set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' });
    res.flushHeaders();
    for await (const chunk of upstream.body) res.write(chunk);
    res.end();
  } catch (e) {
    if (!res.headersSent) res.status(502).end(); else res.end();
  }
});

// An agent's console for its card: every turn in flight, whatever
// started it (runner, desk, ...), with its steps, and the turn finished last.
// Proxied like desk activity; an error reads as nothing in flight.
app.get('/api/agents/:slug/console', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const slug = req.params.slug;
  if (!(await isNodeAdmin(user))) return res.status(403).json({ error: 'the console is for node admins' });
  try {
    const r = await fetch(`${BRIN_URL}/api/agents/${encodeURIComponent(slug)}/console`,
      { headers: { 'X-API-Key': BRIN_API_KEY, Accept: 'application/json' } });
    const data = await r.json().catch(() => ({}));
    res.status(r.status).json(data);
  } catch (e) {
    res.json({ agent: slug, turns: [], last: null });
  }
});

// Reload a desk's agent session: restart its gateway CLI process so it re-reads
// its .claude config (CLAUDE.md, .mcp.json, settings) + system prompt, keeping
// the conversation (gateway uses --resume). Resolve the session id from core-api,
// then ask brin to restart it.
app.post('/api/desks/:id/reload', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  try {
    const sresp = await fetch(`${CORE_API_URL}/api/desks/${encodeURIComponent(req.params.id)}/sessions`,
      { headers: relayHeaders(user) });
    if (!sresp.ok) return res.status(sresp.status).json({ error: 'could not list desk sessions' });
    const sid = (((await sresp.json()).sessions) || [])[0]?.id;
    if (!sid) return res.status(409).json({ error: 'no active session for this desk yet' });
    const rresp = await fetch(`${BRIN_URL}/api/sessions/${encodeURIComponent(sid)}/restart`,
      { method: 'POST', headers: { 'X-API-Key': BRIN_API_KEY, Accept: 'application/json' } });
    const out = await rresp.text();
    res.status(rresp.status).type(rresp.headers.get('content-type') || 'application/json').send(out);
  } catch (e) {
    res.status(502).json({ error: 'reload failed', detail: String(e) });
  }
});

// Start this chat with a genuinely clean provider context. Unlike Reload this
// closes the current gateway session instead of resuming its transcript. The
// next message makes brin create a new session for the same desk; chat history,
// Mesh and Documents are separate stores and remain intact.
app.post('/api/desks/:id/clean-context', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const deskId = encodeURIComponent(req.params.id);
  try {
    const statusResp = await fetch(
      `${CORE_API_URL}/api/context-status?scope=desks&desk_id=${deskId}`,
      { headers: relayHeaders(user) });
    const statusBody = await statusResp.json().catch(() => ({}));
    if (!statusResp.ok) {
      return res.status(statusResp.status).json({ error: 'could not read context status', detail: statusBody.detail });
    }
    const current = (statusBody.contexts || [])[0];
    if (!current || !current.session_id) {
      return res.status(409).json({ error: 'no active session for this desk yet' });
    }
    const cleanResp = await fetch(
      `${BRIN_URL}/api/sessions/${encodeURIComponent(current.session_id)}/clean`,
      {
        method: 'POST',
        headers: { 'X-API-Key': BRIN_API_KEY, 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ requested_by: user, reason: 'studio_manual_clean' }),
      });
    const out = await cleanResp.text();
    res.status(cleanResp.status).type(cleanResp.headers.get('content-type') || 'application/json').send(out);
  } catch (e) {
    res.status(502).json({ error: 'clean context failed', detail: String(e) });
  }
});

// Stop the turn running in a desk. brin asks the gateway to interrupt the
// CLI turn in place: the running tool is killed, the turn ends, the conversation
// and its context stay. The turn's own chat request then comes back with the
// partial answer marked stopped. The person who pressed Stop is recorded.
//
// This used to take sessions[0] from core-api and DELETE it. That list matched a
// conversation key brin no longer writes, so it was always empty and nothing was
// stopped; and had it found the session, killing the process made the gateway
// treat it as a crash and send the same message again.
app.post('/api/desks/:id/abort', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  // One line per press, so a Stop that "did nothing" can be told from one that
  // never arrived: the page shows no sign of either.
  console.log(`[abort] ${user} desk=${req.params.id} received`);
  try {
    // Only the desk's owner stops its turn: brin and the gateway act on any desk
    // id they are given, so the check that it is this person's belongs here.
    // One cheap read of this desk (core-api answers 404 to anyone who may not
    // read it), not the list of every desk: that list took seconds on a node
    // with hundreds of chats, and Stop waited behind it.
    const mine = await fetch(`${CORE_API_URL}/api/desks/${encodeURIComponent(req.params.id)}/attachments`,
      { headers: relayHeaders(user) });
    if (mine.status === 404) { console.log(`[abort] desk=${req.params.id} not this user's chat (404)`); return res.status(404).json({ ok: false, error: 'no such chat' }); }
    if (!mine.ok) { console.log(`[abort] desk=${req.params.id} ownership check ${mine.status}`); return res.status(mine.status).json({ ok: false, error: 'could not check the chat' }); }
    const aresp = await fetch(`${BRIN_URL}/api/desks/${encodeURIComponent(req.params.id)}/stop`, {
      method: 'POST',
      headers: { 'X-API-Key': BRIN_API_KEY, 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ requested_by: user, agent: (req.body && req.body.agent) || undefined }),
    });
    const out = await aresp.json().catch(() => ({}));
    console.log(`[abort] desk=${req.params.id} brin ${aresp.status} interrupted=${out.interrupted}`);
    res.status(aresp.status).json({ ok: aresp.ok && !!out.interrupted, ...out });
  } catch (e) {
    res.status(502).json({ ok: false, error: String(e) });
  }
});

// Which chats have a turn running right now: one poll for every tab.
// { desks: [{desk_id, agent, elapsed, action, queued, stopping}] }, or
// { desks: null } when brin or the gateway could not say, which the page reads as
// "unknown" and leaves the marks as they were. The gateway knows every desk on
// the node; a person is shown only their own, the set core-api lists for them.
app.get('/api/desks/in-progress', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  try {
    const [r, mine] = await Promise.all([
      fetch(`${BRIN_URL}/api/desks/in-progress`,
        { headers: { 'X-API-Key': BRIN_API_KEY, Accept: 'application/json' } }),
      fetch(`${CORE_API_URL}/api/desks`, { headers: relayHeaders(user) }),
    ]);
    const data = await r.json().catch(() => ({ desks: null }));
    if (!r.ok || !mine.ok || !Array.isArray(data.desks)) return res.json({ desks: null });
    const own = new Set((((await mine.json()).desks) || []).map((d) => d.id));
    res.json({ desks: data.desks.filter((d) => own.has(d.desk_id)) });
  } catch (e) {
    res.json({ desks: null });
  }
});

// Who is working right now. Declared before /api/agents/:slug so 'state' is not
// read as an agent slug, the same ordering core-api keeps for its own route.
app.get('/api/agents/state', (req, res) => proxyCore(req, res, '/api/agents/state'));
// Last seen -- when each agent last did anything, for the sidebar. One request per
// event type that only an agent's own work produces (tool calls, turn starts, wakes,
// sent messages), newest first, reduced to the latest timestamp per agent. The
// runtime row's updated_at is the fallback for an agent absent from every list,
// which on a quiet node means it has not acted for a while.
app.get('/api/agents/last-seen', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const kinds = ['tool.invoked', 'agent.turn_started', 'agent.woken', 'message.sent'];
    const lists = await Promise.all(kinds.map(async (k) => {
      const r = await fetch(`${CORE_API_URL}/api/events?event_type=${encodeURIComponent(k)}&limit=500`, { headers: relayHeaders(user) });
      if (!r.ok) return [];
      return (await r.json()).events || [];
    }));
    const last = {};
    const note = (slug, ts) => { if (slug && (!last[slug] || Date.parse(ts) > Date.parse(last[slug]))) last[slug] = ts; };
    for (const list of lists) {
      for (const e of list) {
        const a = String(e.actor || '');
        if (a.startsWith('agent:')) note(a.slice(6), e.ts);
        else if (e.entity_type === 'agent') note(e.entity_id, e.ts);
      }
    }
    try {
      const r = await fetch(`${CORE_API_URL}/api/agents/state`, { headers: relayHeaders(user) });
      if (r.ok) for (const a of ((await r.json()).agents || [])) if (!last[a.slug] && a.updated_at) last[a.slug] = a.updated_at;
    } catch (_) { /* the events alone are still an answer */ }
    res.json({ last_seen: last });
  } catch (e) { res.status(502).json({ error: 'core-api unreachable', detail: String(e) }); }
});

// ?body=true asks for the full instructions file the agent reads (agent page)
app.get('/api/agents/:slug', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}${req.query.body === 'true' ? '?body=true' : ''}`));
app.get('/api/agents/:slug/channels', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/channels`));
app.put('/api/agents/:slug/channels/:channel', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/channels/${encodeURIComponent(req.params.channel)}`, { method: 'PUT', body: req.body ?? {} }));
app.put('/api/agents/:slug', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}`, { method: 'PUT', body: req.body ?? {} }));

// Agent knowledge -- curated docs the agent reads (Knowledge tab). Kinds list is
// gated by integrations in core-api; text is the only fetched kind so far.
app.get('/api/knowledge/kinds', (req, res) => proxyCore(req, res, '/api/knowledge/kinds'));
app.get('/api/agents/:slug/knowledge', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/knowledge`));
app.post('/api/agents/:slug/knowledge', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/knowledge`, { method: 'POST', body: req.body ?? {} }));
app.delete('/api/agents/:slug/knowledge/:kid', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/knowledge/${encodeURIComponent(req.params.kid)}`, { method: 'DELETE' }));

// The agent page of the Studio: what the agent remembers about you (its runtime
// memory, forget one or all), what it may reach (grants, tools, the
// integrations catalog) and what it thinks with (the models catalog).
const agentPath = (req, tail) => `/api/agents/${encodeURIComponent(req.params.slug)}${tail}`;
app.get('/api/agents/:slug/remembers', (req, res) => proxyCore(req, res, agentPath(req, '/remembers')));
app.delete('/api/agents/:slug/remembers', (req, res) => proxyCore(req, res, agentPath(req, '/remembers'), { method: 'DELETE' }));
app.delete('/api/agents/:slug/remembers/:id', (req, res) =>
  proxyCore(req, res, agentPath(req, `/remembers/${encodeURIComponent(req.params.id)}`), { method: 'DELETE' }));
app.get('/api/agents/:slug/grants', (req, res) => proxyCore(req, res, agentPath(req, '/grants')));
app.put('/api/agents/:slug/grants', (req, res) => proxyCore(req, res, agentPath(req, '/grants'), { method: 'PUT', body: req.body ?? {} }));
app.get('/api/agents/:slug/tools', (req, res) => proxyCore(req, res, agentPath(req, '/tools')));
// What is waiting for the agent, for its card in the workspace.
app.get('/api/agents/:slug/queue', (req, res) => proxyCore(req, res, agentPath(req, '/queue')));
app.get('/api/integrations', (req, res) => proxyCore(req, res, '/api/integrations'));
app.get('/api/models', (req, res) => proxyCore(req, res, '/api/models'));
// The person's live events (core GET /api/events/stream), passed through
// as it arrives: proxyCore reads the whole body and would never answer.
app.get('/api/events/stream', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const ac = new AbortController();
  req.on('close', () => ac.abort());
  try {
    const upstream = await fetch(`${CORE_API_URL}/api/events/stream`,
      { headers: relayHeaders(user, { Accept: 'text/event-stream' }), signal: ac.signal });
    if (!upstream.ok) { res.status(upstream.status).send(await upstream.text()); return; }
    res.status(200).set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no' });
    res.flushHeaders();
    for await (const chunk of upstream.body) res.write(chunk);
    res.end();
  } catch (e) {
    if (!ac.signal.aborted && !res.headersSent) res.status(502).json({ error: 'core-api unreachable', detail: String(e) });
    else res.end();
  }
});
// What a chat says about each tool while an agent uses it (core tools registry).
app.get('/api/tools/labels', (req, res) => proxyCore(req, res, '/api/tools/labels'));
// Reminders the agents will say in the chats (the Studio calendar): list and cancel.
app.get('/api/reminders', (req, res) =>
  proxyCore(req, res, `/api/reminders${req.originalUrl.includes('?') ? req.originalUrl.slice(req.originalUrl.indexOf('?')) : ''}`));
app.put('/api/reminders/timezone', (req, res) =>
  proxyCore(req, res, '/api/reminders/timezone', { method: 'PUT', body: req.body ?? {} }));
app.post('/api/reminders', (req, res) =>
  proxyCore(req, res, '/api/reminders', { method: 'POST', body: req.body ?? {} }));
app.delete('/api/reminders/:id', (req, res) =>
  proxyCore(req, res, `/api/reminders/${encodeURIComponent(req.params.id)}`, { method: 'DELETE' }));

// Agent memory (read-only): documents in the agent's mesh workspace. Studio
// reaches mesh over https with the internal SF VPN CA, so this one call opts out
// of cert verification (internal host) instead of pulling in a CA bundle.
function meshFetch(pathQuery, workspace) {
  return new Promise((resolve, reject) => {
    const u = new URL(pathQuery, MESH_API_URL);
    const mod = u.protocol === 'https:' ? https : http;
    const r = mod.get(u, { headers: { 'X-Workspace': workspace, Accept: 'application/json' }, rejectUnauthorized: false }, (resp) => {
      let data = '';
      resp.on('data', (c) => { data += c; });
      resp.on('end', () => { try { resolve({ status: resp.statusCode, json: JSON.parse(data) }); } catch (_) { resolve({ status: resp.statusCode, json: null }); } });
    });
    r.on('error', reject);
    r.setTimeout(8000, () => r.destroy(new Error('mesh timeout')));
  });
}
async function agentWorkspace(req, slug) {
  const r = await fetch(`${CORE_API_URL}/api/agents/${encodeURIComponent(slug)}`, { headers: relayHeaders(req.headers['remote-user']) });
  if (!r.ok) return null;
  return (await r.json()).mesh_workspace || null;
}
// A document write changes what the agent should know, so it takes effect at once:
// core-api's regenerate recomposes CLAUDE.md AND restarts the agent's open sessions
// (a session reads its prompt only when it starts). previously an upload sat in
// mesh unseen until some unrelated settings change happened to recompose. Never
// fails the write it follows; a miss is logged and reported as recomposed:false.
async function recomposeAgent(req, slug) {
  try {
    const r = await fetch(`${CORE_API_URL}/api/agents/${encodeURIComponent(slug)}/regenerate`,
      { method: 'POST', headers: relayHeaders(req.headers['remote-user']) });
    if (!r.ok) { console.warn(`[documents] recompose ${slug} failed: http ${r.status}`); return false; }
    return true;
  } catch (e) { console.warn(`[documents] recompose ${slug} failed: ${e}`); return false; }
}
app.get('/api/agents/:slug/memory', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.json({ workspace: null, count: 0, docs: [] });
    const limit = Math.min(parseInt(req.query.limit, 10) || 100, 500);
    const out = await meshFetch(`/browse?limit=${limit}`, ws);
    if (!out.json) return res.status(502).json({ error: 'mesh browse failed' });
    res.json({ workspace: ws, count: out.json.count, docs: out.json.docs || [] });
  } catch (e) { res.status(502).json({ error: 'memory unreachable', detail: String(e) }); }
});
app.get('/api/agents/:slug/memory/:guid', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(404).json({ error: 'no workspace' });
    const out = await meshFetch(`/${encodeURIComponent(req.params.guid)}`, ws);
    if (!out.json) return res.status(502).json({ error: 'mesh get failed' });
    res.json(out.json);
  } catch (e) { res.status(502).json({ error: 'memory unreachable', detail: String(e) }); }
});

// Mesh write/delete (same internal-CA opt-out as meshFetch) for documents.
function meshSend(method, pathQuery, workspace, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(pathQuery, MESH_API_URL);
    const mod = u.protocol === 'https:' ? https : http;
    const payload = body !== undefined ? JSON.stringify(body) : null;
    const headers = { 'X-Workspace': workspace, Accept: 'application/json' };
    if (payload) headers['Content-Type'] = 'application/json';
    const r = mod.request(u, { method, headers, rejectUnauthorized: false }, (resp) => {
      let data = '';
      resp.on('data', (c) => { data += c; });
      resp.on('end', () => { try { resolve({ status: resp.statusCode, json: JSON.parse(data) }); } catch (_) { resolve({ status: resp.statusCode, json: null }); } });
    });
    r.on('error', reject);
    r.setTimeout(8000, () => r.destroy(new Error('mesh timeout')));
    if (payload) r.write(payload);
    r.end();
  });
}
// Documents = the agent's mesh workspace (knowledge + memory unified). Origin is
// carried by tags: docs added here are tagged `via:ui`; the agent's own writes
// keep their source tags. List/get reuse the memory browse; create/delete write.
app.get('/api/agents/:slug/documents', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.json({ workspace: null, count: 0, docs: [] });
    // Documents are what a person attached (via:ui, or an upload / Google link),
    // the same rule the prompt composer and the panel use. The agent's own writes
    // (notes, context handoffs) stay in /memory; listing them here showed 24
    // "Session handoff" tiles on alex's knowledge page.
    const out = await meshFetch('/browse?limit=500', ws);
    if (!out.json) return res.status(502).json({ error: 'mesh browse failed' });
    const isDoc = (t) => (t || []).some((x) => String(x).startsWith('via:ui') || x === 'source:google-drive' || x === 'source:upload');
    const docs = (out.json.docs || []).filter((d) => isDoc(d.t));
    res.json({ workspace: ws, count: docs.length, docs });
  } catch (e) { res.status(502).json({ error: 'documents unreachable', detail: String(e) }); }
});
app.get('/api/agents/:slug/documents/:guid', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(404).json({ error: 'no workspace' });
    const out = await meshFetch(`/${encodeURIComponent(req.params.guid)}`, ws);
    if (!out.json) return res.status(502).json({ error: 'mesh get failed' });
    res.json(out.json);
  } catch (e) { res.status(502).json({ error: 'documents unreachable', detail: String(e) }); }
});
app.post('/api/agents/:slug/documents', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(409).json({ error: 'agent has no memory workspace' });
    const title = ((req.body && req.body.title) || '').trim();
    const text = ((req.body && req.body.content) || '').trim();
    if (!text) return res.status(400).json({ error: 'content required' });
    const content = title ? `${title}\n\n${text}` : text;
    const out = await meshSend('PUT', '/', ws, { content, tags: ['via:ui'], source: 'studio' });
    if (out.status >= 300 || !out.json) return res.status(502).json({ error: 'mesh create failed' });
    const recomposed = await recomposeAgent(req, req.params.slug);
    res.status(201).json({ ...out.json, recomposed });
  } catch (e) { res.status(502).json({ error: 'documents unreachable', detail: String(e) }); }
});
// File upload -> extract text to markdown (host-utils: markitdown) -> write to the
// agent's mesh memory. Studio runs on the host and shells out to the pinned
// extractor from the host-utils layer. The raw file is streamed to a
// temp path (no multipart lib); only the extracted markdown is persisted -- the
// binary is discarded. Filename becomes the doc title (first line); source is a
// tag. Preflights the extractor and degrades gracefully when it is absent.
const MARKITDOWN = '/srv/tools/host-utils/venv/bin/markitdown';
const UPLOAD_MAX_BYTES = 25 * 1024 * 1024;
app.post('/api/agents/:slug/documents/upload', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  let tmp = null;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(409).json({ error: 'agent has no memory workspace' });
    if (!existsSync(MARKITDOWN)) return res.status(503).json({ error: 'extractor unavailable (host-utils not provisioned)' });
    const name = String(req.query.name || 'upload').replace(/[\r\n]/g, '').slice(0, 200);
    const ext = (name.split('.').pop() || '').toLowerCase().replace(/[^a-z0-9]/g, '').slice(0, 8);
    tmp = join(tmpdir(), `snup-${Date.now()}-${Math.random().toString(36).slice(2)}${ext ? '.' + ext : ''}`);
    // stream the raw request body to the temp file, capped at UPLOAD_MAX_BYTES
    await new Promise((resolve, reject) => {
      const out = createWriteStream(tmp);
      let bytes = 0, over = false;
      req.on('data', (c) => { bytes += c.length; if (bytes > UPLOAD_MAX_BYTES && !over) { over = true; out.destroy(); req.destroy(); reject(new Error('file too large')); } });
      req.pipe(out);
      out.on('finish', resolve); out.on('error', reject); req.on('error', reject);
    });
    // extract -> markdown via the pinned host-utils binary
    const md = await new Promise((resolve, reject) => {
      execFile(MARKITDOWN, [tmp], { maxBuffer: 32 * 1024 * 1024, timeout: 60000 }, (err, stdout) => {
        if (err) reject(err); else resolve(String(stdout || ''));
      });
    });
    const text = md.trim();
    if (!text) return res.status(422).json({ error: 'no text could be extracted from this file' });
    const content = `${name}\n\n${text}`;
    const tags = ['via:ui', 'source:upload']; if (ext) tags.push(`format:${ext}`);
    const meshOut = await meshSend('PUT', '/', ws, { content, tags, source: 'studio-upload' });
    if (meshOut.status >= 300 || !meshOut.json) return res.status(502).json({ error: 'mesh create failed' });
    const recomposed = await recomposeAgent(req, req.params.slug);
    res.status(201).json({ ...meshOut.json, recomposed });
  } catch (e) {
    const tooBig = String(e && e.message) === 'file too large';
    res.status(tooBig ? 413 : 502).json({ error: tooBig ? 'file too large' : 'upload failed', detail: String((e && e.message) || e) });
  } finally {
    if (tmp) unlink(tmp, () => {});
  }
});
// Create a Google Doc from the Studio UI and link it into the agent's knowledge.
// core-api owns the connected Google account and creates the doc in the designated
// folder; we store only a thin POINTER in mesh (title + link, source:google-drive),
// not the content -- the source of truth stays in Drive. The agent reads
// the live doc via its gdrive_read tool.
app.post('/api/agents/:slug/documents/gdrive-create', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(409).json({ error: 'agent has no memory workspace' });
    const title = ((req.body && req.body.title) || '').trim();
    const content = (req.body && req.body.content) || '';
    if (!title) return res.status(400).json({ error: 'title required' });
    const r = await fetch(`${CORE_API_URL}/api/integrations/google/docs`, {
      method: 'POST',
      headers: relayHeaders(req.headers['remote-user'], { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ title, content }),
    });
    if (!r.ok) {
      let detail = 'http ' + r.status;
      try { const j = await r.json(); detail = j.detail || j.error || detail; } catch (_) {}
      return res.status(502).json({ error: 'google create failed', detail });
    }
    const doc = await r.json();
    const pointer = `${doc.name}\n\n${doc.link}`;
    const out = await meshSend('PUT', '/', ws, { content: pointer, tags: ['via:ui', 'source:google-drive'], source: 'studio-gdrive' });
    if (out.status >= 300 || !out.json) return res.status(502).json({ error: 'linked but mesh pointer failed', link: doc.link });
    const recomposed = await recomposeAgent(req, req.params.slug);
    res.status(201).json({ link: doc.link, name: doc.name, guid: out.json.guid, recomposed });
  } catch (e) { res.status(502).json({ error: 'gdrive create failed', detail: String(e) }); }
});
// Google Picker config for the Studio UI (proxies core-api; owner-gated).
app.get('/api/agents/:slug/gdrive/picker-config', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const r = await fetch(`${CORE_API_URL}/api/integrations/google/picker-config`, {
      headers: { 'Remote-User': req.headers['remote-user'], Accept: 'application/json' } });
    if (!r.ok) return res.status(502).json({ error: 'picker-config failed' });
    res.json(await r.json());
  } catch (e) { res.status(502).json({ error: 'picker-config failed', detail: String(e) }); }
});
// Link an existing Google doc the owner selected in the Picker (which grants the
// app drive.file access to it). We store only a thin pointer; the source of truth
// stays in Drive and the agent reads it live via gdrive_read.
app.post('/api/agents/:slug/documents/gdrive-link', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(409).json({ error: 'agent has no memory workspace' });
    const name = ((req.body && req.body.name) || '').trim() || 'Google Doc';
    const url = ((req.body && req.body.url) || '').trim();
    if (!/^https?:\/\/\S+/.test(url)) return res.status(400).json({ error: 'a Google Drive url is required' });
    const out = await meshSend('PUT', '/', ws, { content: `${name}\n\n${url}`, tags: ['via:ui', 'source:google-drive'], source: 'studio-gdrive' });
    if (out.status >= 300 || !out.json) return res.status(502).json({ error: 'mesh pointer failed' });
    const recomposed = await recomposeAgent(req, req.params.slug);
    res.status(201).json({ link: url, name, guid: out.json.guid, recomposed });
  } catch (e) { res.status(502).json({ error: 'gdrive link failed', detail: String(e) }); }
});
app.delete('/api/agents/:slug/documents/:guid', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const ws = await agentWorkspace(req, req.params.slug);
    if (!ws) return res.status(404).json({ error: 'no workspace' });
    const out = await meshSend('DELETE', `/${encodeURIComponent(req.params.guid)}`, ws);
    if (out.status >= 300) return res.status(502).json({ error: 'mesh delete failed' });
    await recomposeAgent(req, req.params.slug);
    res.status(204).end();
  } catch (e) { res.status(502).json({ error: 'documents unreachable', detail: String(e) }); }
});

// User notification channels -- where the owner gets notified (email, telegram,
// whatsapp, sms). Read + upsert, proxied to core-api on behalf of the forwarded
// identity. Core-api scopes the user by the Remote-User it receives.
app.get('/api/users/:id/notify-channels', (req, res) =>
  proxyCore(req, res, `/api/users/${encodeURIComponent(req.params.id)}/notify-channels`));
app.put('/api/users/:id/notify-channels/:channel', (req, res) =>
  proxyCore(req, res, `/api/users/${encodeURIComponent(req.params.id)}/notify-channels/${encodeURIComponent(req.params.channel)}`, { method: 'PUT', body: req.body ?? {} }));
app.get('/api/agents/:slug/policies', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/policies`));
app.put('/api/agents/:slug/policies/:policy', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/policies/${encodeURIComponent(req.params.policy)}`, { method: 'PUT', body: req.body ?? {} }));
// Pin/unpin a document: pinned -> content pre-loaded into the prompt.
app.put('/api/agents/:slug/documents/:guid/pin', (req, res) =>
  proxyCore(req, res, `/api/agents/${encodeURIComponent(req.params.slug)}/documents/${encodeURIComponent(req.params.guid)}/pin`, { method: 'PUT', body: req.body ?? {} }));

// Agent mailbox (read-only): the agent's conversations on the bus, grouped by the
// other party (internal messages carry no thread_id, so we group by counterpart).
// Owner-gated by the Authum edge; read-only. Returns threads newest-first, each
// with its full message list (both directions) so the client opens it with no
// second request.
app.get('/api/agents/:slug/mail', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  const slug = req.params.slug;
  try {
    const r = await fetch(`${CORE_API_URL}/api/messages?limit=300`, { headers: relayHeaders(user) });
    if (!r.ok) { res.status(r.status).send(await r.text()); return; }
    const all = (await r.json()).messages || [];
    const mine = all.filter((m) => m.sender === slug || m.recipient === slug);
    const conv = {};
    for (const m of mine) {
      const other = m.sender === slug ? m.recipient : m.sender;
      (conv[other] = conv[other] || []).push(m);
    }
    const threads = Object.keys(conv).map((other) => {
      const msgs = conv[other].sort((a, b) => (a.id || 0) - (b.id || 0));
      const last = msgs[msgs.length - 1];
      return { counterpart: other, count: msgs.length, last_id: last.id, last_at: last.received_at, last_from: last.sender, snippet: (last.content || '').slice(0, 160), messages: msgs };
    }).sort((a, b) => (b.last_id || 0) - (a.last_id || 0));
    res.json({ agent: slug, threads });
  } catch (e) { res.status(502).json({ error: 'core-api unreachable', detail: String(e) }); }
});

// Activity -- message-flow events involving this agent, normalized for the
// Flow view. Pulls a window of the event journal, keeps only message.* chain
// events where this agent is sender or recipient, projects each to {from,to}.
app.get('/api/agents/:slug/activity', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  const slug = req.params.slug;
  try {
    const r = await fetch(`${CORE_API_URL}/api/events?limit=1000`, { headers: relayHeaders(user) });
    if (!r.ok) { res.status(r.status).send(await r.text()); return; }
    const KINDS = new Set(['message.inbound', 'message.sent', 'message.reply']);
    const raw = (await r.json()).events || [];
    const events = [];
    for (const e of raw) {
      if (!KINDS.has(e.event_type)) continue;
      const p = e.payload || {};
      const from = p.from || (e.actor || '').replace(/^agent:/, '');
      const to = p.to || '';
      if (from !== slug && to !== slug) continue;
      events.push({ id: e.id, ts: e.ts, type: e.event_type, from, to, preview: p.preview || '' });
    }
    res.json({ agent: slug, events });
  } catch (e) { res.status(502).json({ error: 'core-api unreachable', detail: String(e) }); }
});

// Timeline -- everything this agent did, for the agent page. The Flow view above keeps
// message chains; this is the other question: what has the agent been doing. It asks
// core-api for this agent's events directly (actor = agent:<slug>, and the Runner's
// wake events whose entity is the agent) instead of reading a window of the whole
// node and filtering it, which is how the agent page said "last action 55 min" while
// the agent was writing task comments that minute (field node, 2026-09-14): the
// window was the newest 1000 events of every agent, and only message events counted.
// Inbound messages are the adapter's events, so they are fetched by type (the inbound event rule:
// fetch per type, never filter after a blind window) and kept when addressed here.
app.get('/api/agents/:slug/timeline', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  const slug = req.params.slug;
  const q = async (qs) => {
    const r = await fetch(`${CORE_API_URL}/api/events?${qs}`, { headers: relayHeaders(user) });
    if (!r.ok) throw Object.assign(new Error('core-api'), { status: r.status, body: await r.text() });
    return (await r.json()).events || [];
  };
  try {
    const [own, wakes, inbound] = await Promise.all([
      q(`actor=${encodeURIComponent('agent:' + slug)}&limit=400`),
      q(`entity_type=agent&entity_id=${encodeURIComponent(slug)}&limit=100`),
      q(`event_type=message.inbound&limit=300`),
    ]);
    const seen = new Set();
    const events = [];
    const add = (e) => {
      if (seen.has(e.id)) return; seen.add(e.id);
      if (e.event_type === 'tool.succeeded') return; // tool.invoked carries the path; the outcome only matters when it failed
      const p = e.payload || {};
      events.push({ id: e.id, ts: e.ts, type: e.event_type, op: p.op || null, path: p.path || null,
        via: p.via || null, ref: p.ref || null, from: p.from || null, to: p.to || null, tool: p.tool || null,
        // the why of a line: who stopped it or changed it, why a wake failed, when a reminder is due
        actor: e.actor || null, stopped_by: p.stopped_by || null, reason: p.reason || null,
        retry_in: p.retry_in_seconds || null, due_at: p.due_at || null,
        preview: p.preview || '', error: p.error ? (p.error.title || p.error.detail || String(p.error)) : null });
    };
    own.forEach(add);
    wakes.forEach(add);
    inbound.filter((e) => (e.payload || {}).to === slug).forEach(add);
    events.sort((a, b) => (Date.parse(b.ts) || 0) - (Date.parse(a.ts) || 0) || (b.id - a.id));
    res.json({ agent: slug, events: events.slice(0, 200) });
  } catch (e) { res.status(e.status || 502).json({ error: 'core-api unreachable', detail: String(e.body || e) }); }
});

// Node-wide curated activity -- the REAL stream behind the Dashboard "What your
// Studio did" feed (WS4). The raw /api/events firehose is ~97% infra noise
// (cron.*, admin.op.*), so we whitelist only agent-facing message events that
// carry a who + what (inbound = someone reached the Studio, sent/reply = an
// agent responded). message.transmitted is deliberately excluded: its actor is
// component:send-outbound with no agent or preview, so it just repeats
// "email delivered" rows that add no story.
app.get('/api/activity', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    // FETCH PER TYPE, DO NOT FILTER AFTER A BLIND WINDOW.
    //
    // This used to be one `?limit=400` and a filter applied afterwards, which
    // made the feed's depth a measure of how noisy the node was rather than of
    // what the Studio did. It is not a slow leak: reported from a live node the
    // day this shipped, an owner's OPEN DASHBOARD polls the planner about twelve
    // times a minute, so the newest 400 events covered the last half hour and
    // were 379/400 `planner.list` reads. That node had done 34 owner-facing
    // things between 09:28 and 15:27 and NOT ONE of them was inside the window.
    // The card was empty on a busy day: the act of watching the feed produced
    // the noise that evicted its contents.
    //
    // core-api's event_type filter takes ONE value, so this fans out and merges
    // newest-first. Each type then gets its own depth and message events are
    // immune to read-poll volume, whoever is doing the polling.
    const KIND_LIST = ['message.inbound', 'message.sent', 'message.reply',
                       'message.transmitted', 'tool.succeeded', 'message.routing_denied'];
    const per = await Promise.all(KIND_LIST.map(async (k) => {
      const u = `${CORE_API_URL}/api/events?event_type=${encodeURIComponent(k)}&limit=80`;
      const rr = await fetch(u, { headers: relayHeaders(user) });
      if (!rr.ok) throw Object.assign(new Error('core-api'), { status: rr.status, body: await rr.text() });
      return (await rr.json()).events || [];
    })).catch((e) => e);
    if (!Array.isArray(per)) { res.status(per.status || 502).send(per.body || String(per)); return; }
    // Merge newest-first. id is monotonic, so it orders the streams against each
    // other without trusting six separately-formatted timestamps.
    const raw = per.flat().sort((a, b) => Number(b.id) - Number(a.id));
    // Deliveries are addressed by CHANNEL, never by the raw destination, so no
    // email/phone/number ever reaches the client.
    const chanOf = (to) => { const s = String(to || ''); if (/^\d+$/.test(s)) return 'telegram'; if (s.startsWith('#')) return 'slack'; if (s.includes('@')) return 'email'; return ''; };
    // What the Studio did + what the node actually delivered to the owner
    // (transmitted = email/telegram/slack), tasks created (tool), and held
    // messages waiting on the owner (routing_denied).
    const KINDS = new Set(['message.inbound', 'message.sent', 'message.reply', 'message.transmitted', 'tool.succeeded', 'message.routing_denied']);
    // A SUCCESSFUL READ IS NOT SOMETHING THE STUDIO DID. the read path already stopped
    // journalling reads that found nothing; a read that found rows is still a
    // pair of events, and on this feed it rendered as "<agent> created planner"
    // -- because entity_id for a planner call is the literal string 'planner',
    // and the action itself only ever lived in payload.op, which this endpoint
    // did not forward. So every poll looked like a write, and the two or three
    // real things an agent did were buried in them. Measured on a live node over 48h:
    // 8965 of 8986 tool.invoked rows were GETs, against 21 for every write the
    // node performed.
    //
    // Classify by op, which _op_label() builds precisely so the ledger stays
    // readable, and list the READS rather than the writes: an op nobody has
    // taught this endpoint about is shown, not hidden. Hiding real work to
    // reduce noise is the worse failure of the two.
    // ASK THE EVENT WHAT IT WAS, do not recognise it by name. This used to be a
    // hardcoded set of planner read-ops, which put planner knowledge in the feed
    // and meant a new read operation would render as work until somebody
    // remembered to add it here. The emitter knows the method and now says so.
    //
    // An event with no method is SHOWN. Rows written before this shipped have
    // none, and the op-name fallback covers the ones we can still recognise;
    // beyond that, showing something that turns out to be a poll is better than
    // hiding something that turns out to be work.
    const READ_METHODS = new Set(['GET', 'HEAD']);
    const LEGACY_READ_OPS = new Set(['planner.list', 'planner.get', 'planner.search', 'planner.comments']);
    const isRead = (op, method) => method
      ? READ_METHODS.has(String(method).toUpperCase())
      : (LEGACY_READ_OPS.has(op) || /\.read$/.test(op || ''));
    const events = [];
    const denied = new Map();   // reason|channel -> the row that carries the count
    for (const e of raw) {
      if (!KINDS.has(e.event_type)) continue;
      const p = e.payload || {};
      let agent = String(e.actor || '').replace(/^agent:/, '').replace(/^component:/, '');
      let channel = p.channel || '', to = p.to || '';
      const op = p.op || '';
      if (e.event_type === 'tool.succeeded' && isRead(op, p.method)) continue;
      // A system doorbell is how the node wakes an agent, not something anyone
      // did. It carries no sender a person would recognise, so it rendered as
      // "A customer contacted the Studio" -- machinery wearing a customer's
      // face, and the loudest single source of noise on a working node.
      if (e.event_type === 'message.inbound' && channel === 'system') continue;
      // A HELD MESSAGE IS NEWS ONCE, NOT 337 TIMES. Measured on a live node over 7
      // days: 413 message.routing_denied rows, 337 of them the same verdict --
      // a voice call arriving on a channel instance with no agent bound. Each
      // one rendered "Node held a message (needs your input)", so the single
      // thing the owner could actually act on was stated hundreds of times and
      // buried everything he could not.
      //
      // Collapsed, not dropped. The verdict IS a real configuration gap and
      // hiding it would leave the owner unable to see why calls go unanswered;
      // what he cannot use is the repetition. Raw is newest-first, so the first
      // of each kind is the freshest and the rest only raise its count.
      if (e.event_type === 'message.routing_denied') {
        const key = `${p.reason || ''}|${channel}`;
        const seen = denied.get(key);
        if (seen) { seen.count++; continue; }
        const row = { key, count: 1 };
        denied.set(key, row);
        events.push(Object.assign(row, {
          id: e.id, ts: e.ts, type: e.event_type,
          agent, from: p.from || '', to, channel,
          entity: e.entity_id || '', op,
          reason: p.reason || '',
          preview: p.preview || '',
        }));
        if (events.length >= 40) break;
        continue;
      }
      if (e.event_type === 'message.transmitted') {
        channel = chanOf(p.to); if (!channel) continue;   // skip internal/unknown transmits
        to = ''; agent = 'Studio';
      }
      events.push({
        id: e.id, ts: e.ts, type: e.event_type,
        agent, from: p.from || '', to, channel,
        entity: e.entity_id || '',
        op,
        preview: p.preview || '',
      });
      if (events.length >= 40) break;
    }
    res.json({ events });
  } catch (e) { res.status(502).json({ error: 'core-api unreachable', detail: String(e) }); }
});

// Node-wide inbound inbox -- the REAL stream behind the Dashboard "Recent emails"
// and "Recent messages" panels. Outbound is mostly system task-notifications, so
// we only return inbound (someone reaching the Studio). The client splits by
// channel: email -> "Recent emails", everything else (web-chat/telegram/internal)
// -> "Recent messages".
app.get('/api/inbox', async (req, res) => {
  const user = remoteUser(req, res); if (!user) return;
  try {
    const r = await fetch(`${CORE_API_URL}/api/messages?limit=200`, { headers: relayHeaders(user) });
    if (!r.ok) { res.status(r.status).send(await r.text()); return; }
    const body = await r.json();
    const raw = Array.isArray(body) ? body : (body.messages || body.items || []);
    const messages = [];
    for (const m of raw) {
      // Both directions now: Studio's outbound mail (quotes, recaps,
      // notifications) is real activity the owner wants to see, not just inbound.
      messages.push({
        id: m.id,
        channel: m.channel_type || 'email',
        direction: m.direction || 'inbound',
        from: m.sender || '',
        to: m.recipient || '',
        subject: (m.metadata && m.metadata.subject) || '',
        preview: String(m.content || '').replace(/\s+/g, ' ').trim().slice(0, 120),
        ts: m.received_at || null,
      });
      if (messages.length >= 40) break;
    }
    res.json({ messages });
  } catch (e) { res.status(502).json({ error: 'core-api unreachable', detail: String(e) }); }
});

// Tasks -- proxied to the planner THROUGH core-api (companion `tracking` behind
// the `core-api` door).
//
// It used to go straight to the planner, on the reasoning that the planner ran
// without a credential and the Authum edge was the auth. Both halves of that
// were wrong. The planner now requires a key, so a direct call answers
// 401; and the Authum edge never covered this path anyway, because Studio
// reaches the planner over the docker bridge where Traefik is not in the way.
//
// Routing through core-api means Studio needs no planner credential of its own:
// core-api holds the node's one delegating key and names the signed-in user as
// the author, so an entry made in Studio belongs to the person who made it.
// It also puts every task call, read or write, on the door that keeps the
// ledger -- and empty reads are not journalled, so this costs no noise.
async function proxyPlanner(req, res, path, opts = {}) {
  return proxyCore(req, res, `/api/planner${path}`,
                   { method: opts.method || 'GET', body: opts.body });
}
app.get('/api/tasks', (req, res) => proxyPlanner(req, res, '/api/v1/tasks'));
// Every task, for the planner overview. /api/tasks is the planner's default page
// of 100, i.e. the last few days, and the overview computed every ring and count
// from it: each goal older than that read 0/0. Paged here in the
// planner's largest page, descriptions dropped (the overview never shows them and
// they are most of the bytes), and held for 30 s per user so a filter click does
// not refetch thousands of rows. Declared before /api/tasks/:id, which would
// otherwise take "overview" for an id.
const _overviewCache = new Map();
app.get('/api/tasks/overview', async (req, res) => {
  const user = remoteUser(req, res);
  if (!user) return;
  const hit = _overviewCache.get(user);
  if (hit && Date.now() - hit.at < 30000) return res.json(hit.rows);
  try {
    const rows = [];
    // Bounded: a planner that ignored offset would otherwise loop here forever.
    for (let offset = 0; offset < 100000; offset += 500) {
      const up = await fetch(`${CORE_API_URL}/api/planner/api/v1/tasks?limit=500&offset=${offset}`,
                             { headers: relayHeaders(user) });
      if (!up.ok) return res.status(up.status).type(up.headers.get('content-type') || 'application/json').send(await up.text());
      const page = await up.json();
      if (!Array.isArray(page)) return res.status(502).json({ error: 'planner answered a non-list' });
      for (const t of page) { const { description, external_detail, ...slim } = t; rows.push(slim); }
      if (page.length < 500) break;
    }
    _overviewCache.set(user, { at: Date.now(), rows });
    res.json(rows);
  } catch (e) {
    res.status(502).json({ error: 'core-api unreachable', detail: String(e) });
  }
});
app.get('/api/projects', (req, res) => proxyPlanner(req, res, '/api/v1/projects'));
app.get('/api/objectives', (req, res) => proxyPlanner(req, res, '/api/v1/objectives'));
app.get('/api/workflows', (req, res) => proxyCore(req, res, '/api/workflows'));
app.get('/api/tasks/:id', (req, res) => proxyPlanner(req, res, `/api/v1/tasks/${encodeURIComponent(req.params.id)}`));
app.get('/api/tasks/:id/comments', (req, res) => proxyPlanner(req, res, `/api/v1/tasks/${encodeURIComponent(req.params.id)}/comments`));
// Owner approve/reject (and any note) is a posted comment; the Runner's approval
// pass reads it. This is the decision the approval gate turns on, and the approval gate put
// it on core-api so it travels the one door that checks how strongly the
// identity is known and writes the decision to the ledger. It now shares that
// door with everything else here rather than being the exception.
app.post('/api/tasks/:id/comments', (req, res) => proxyPlanner(
  req, res, `/api/v1/tasks/${encodeURIComponent(req.params.id)}/comments`,
  { method: 'POST', body: req.body ?? {} }));
// The owner's decision also advances the task itself (awaiting_approval ->
// in_progress on approve, rejected on reject). This was the half left behind by
// the comment moved onto core-api and the status change that accompanies
// it kept going straight to the planner, so one owner action travelled two
// paths with two different guarantees. It goes through the same door now.
app.patch('/api/tasks/:id', (req, res) => proxyPlanner(req, res, `/api/v1/tasks/${encodeURIComponent(req.params.id)}`, { method: 'PATCH', body: req.body ?? {} }));
// The owner's decision on a task that is waiting for one.
// Three answers, three doors, all of them the planner's own: approve stamps
// approved_at itself, modify records what to change without pretending the work
// was approved, reject requires a reason. Studio used to assemble this as a
// PATCH carrying {status, approved_at}, which the planner's schema forbids, so
// every press failed after its comment had been written.
for (const decision of ['approve', 'modify', 'reject']) {
  app.post(`/api/tasks/:id/${decision}`, (req, res) => proxyPlanner(
    req, res, `/api/v1/tasks/${encodeURIComponent(req.params.id)}/${decision}`,
    { method: 'POST', body: req.body ?? {} }));
}
// local: status model v1: the planner's two dictionaries (wait reasons, close
// outcomes), read by the task filter and edited in Settings. Same door as the
// tasks: core-api forwards the viewer's identity, the planner lets only the
// owner write.
app.get('/api/dictionaries/:dict', (req, res) => proxyPlanner(
  req, res, `/api/v1/dictionaries/${encodeURIComponent(req.params.dict)}?include_disabled=true`));
app.post('/api/dictionaries/:dict', (req, res) => proxyPlanner(
  req, res, `/api/v1/dictionaries/${encodeURIComponent(req.params.dict)}`,
  { method: 'POST', body: req.body ?? {} }));
app.patch('/api/dictionaries/:dict/:code', (req, res) => proxyPlanner(
  req, res, `/api/v1/dictionaries/${encodeURIComponent(req.params.dict)}/${encodeURIComponent(req.params.code)}`,
  { method: 'PATCH', body: req.body ?? {} }));
app.delete('/api/dictionaries/:dict/:code', (req, res) => proxyPlanner(
  req, res, `/api/v1/dictionaries/${encodeURIComponent(req.params.dict)}/${encodeURIComponent(req.params.code)}`,
  { method: 'DELETE' }));

// Documents -- proxy to the doci backend. Unlike planner, doci enforces its own
// auth, so we MUST forward the Authum identity (Remote-User) like proxyCore does;
// doci trusts it because Studio sits on a trusted-proxy IP. Read-only for now.
async function proxyDocs(req, res, upstreamPath) {
  const user = remoteUser(req, res);
  if (!user) return;
  if (!DOCI_URL) return res.status(404).json({ error: 'documents are not installed on this node' });
  try {
    const upstream = await fetch(`${DOCI_URL}${upstreamPath}`, {
      headers: { 'Remote-User': user, Accept: 'application/json' },
    });
    const text = await upstream.text();
    res.status(upstream.status).type(upstream.headers.get('content-type') || 'application/json').send(text);
  } catch (e) {
    res.status(502).json({ error: 'documents backend unreachable', detail: String(e) });
  }
}
app.get('/api/docs', (req, res) => {
  const qs = new URLSearchParams();
  if (req.query.recent) qs.set('recent', String(req.query.recent));
  if (req.query.q) qs.set('q', String(req.query.q));
  const suffix = qs.toString() ? `?${qs.toString()}` : '';
  proxyDocs(req, res, `/api/list.php${suffix}`);
});
app.get('/api/docs/doc', (req, res) => {
  const qs = new URLSearchParams({ content: '1' });
  if (req.query.guid) qs.set('guid', String(req.query.guid));
  if (req.query.path) qs.set('path', String(req.query.path));
  proxyDocs(req, res, `/api/documents.php?${qs.toString()}`);
});
app.get('/api/docs/versions', (req, res) =>
  proxyDocs(req, res, `/api/versions.php?document=${encodeURIComponent(String(req.query.document || ''))}`));
app.get('/api/docs/threads', (req, res) =>
  proxyDocs(req, res, `/api/thread.php?document=${encodeURIComponent(String(req.query.document || ''))}`));

app.get('/healthz', (_req, res) => res.type('text').send('ok'));
// Current build stamp, for the client's stale-tab self-refresh check.
// `release` is the node release the installer recorded. It is read when asked (a 70-byte file), not once at start-up:
// on a fresh install the record is written after this service has started, and a value read once was empty until the
// next restart (found on a stand, 2026-11-04). A node without the file says nothing rather than guessing.
const releaseNow = () => {
  try { return (readFileSync('/etc/smart-node/release', 'utf8').match(/^release=(.+)$/m) || [])[1]?.trim() || ''; }
  catch { return ''; }
};
app.get('/api/version', (_req, res) => { res.setHeader('Cache-Control', 'no-store'); res.json({ build: BUILD, release: releaseNow() }); });
// What this node has installed, from the installer's own records (one <name>.json per
// companion). The client hides the sections whose companion is absent, so a minimal
// node does not offer Workflows, Tasks or Documents that lead nowhere.
const COMPANIONS_DIR = '/etc/smart-node/companions.d';
const MANIFESTS_DIR = '/etc/smart-node/manifests';
app.get('/api/node/components', (_req, res) => {
  let installed = [];
  try {
    installed = readdirSync(COMPANIONS_DIR).filter((f) => f.endsWith('.json')).map((f) => f.slice(0, -5));
  } catch (err) {
    return res.status(503).json({ error: `cannot read ${COMPANIONS_DIR}: ${err.code || err.message}` });
  }
  // The release's modules, for Settings: a node without the catalogue still answers.
  let modules = [];
  try {
    modules = readdirSync(MANIFESTS_DIR).filter((f) => f.endsWith('.manifest.json'))
      .map((f) => JSON.parse(readFileSync(join(MANIFESTS_DIR, f), 'utf8')))
      .filter((m) => !m.core)
      .map((m) => ({ name: m.name, title: m.title || m.name, provides: m.provides || '', installed: installed.includes(m.name) }));
  } catch { /* no catalogue */ }
  res.setHeader('Cache-Control', 'no-store');
  res.json({ installed, modules });
});

// First-run page (/welcome). The installer prints one link, /welcome?token=..., and the
// person goes account -> AI -> their agent -> the workspace, without the Admin panel.
// /welcome/* is the one path of this host Traefik serves without a session (see the
// Studio route), so the page, its script and the UI kit it uses all live under it.
// Steps after the account need a session, and use the ordinary authenticated /api.
app.use('/welcome/ui', express.static(join(__dirname, '..', 'public', 'ui'), {
  setHeaders: (res) => res.setHeader('Cache-Control', 'no-store'),
}));
app.get('/welcome/api/auth', (_req, res) => res.json({ auth_url: AUTHUM_AUTH_URL }));
// The first operator, from the one-time setup token. core-api's /api/setup/complete is
// session-free by design; this only relays it so the page stays on its own host.
app.post('/welcome/api/setup', async (req, res) => {
  try {
    const upstream = await fetch(`${CORE_API_URL}/api/setup/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // no password: core-api picks one nobody sees and returns a one-time sign-in token
      body: JSON.stringify({ token: String(req.body?.token || ''), username: 'admin' }),
    });
    res.status(upstream.status).type('application/json').send(await upstream.text());
  } catch (e) {
    res.status(502).json({ detail: 'core-api unreachable' });
  }
});
// Sign in with the person's Claude subscription: start returns the sign-in link, complete
// takes the code shown after sign-in; core-api stores the token and never returns it.
app.post('/api/gateway/cli/:slug/login/start', (req, res) =>
  proxyCore(req, res, `/api/gateway/cli/${encodeURIComponent(req.params.slug)}/login/start`, { method: 'POST', body: {} }));
app.post('/api/gateway/cli/:slug/login/complete', (req, res) =>
  proxyCore(req, res, `/api/gateway/cli/${encodeURIComponent(req.params.slug)}/login/complete`, { method: 'POST', body: req.body ?? {} }));
// Sign in with the person's ChatGPT plan: same two steps, but OpenAI sends the browser to an address on
// the person's own machine that cannot load, so the person pastes that address instead of a code.
app.post('/api/gateway/chatgpt/login/start', (req, res) =>
  proxyCore(req, res, '/api/gateway/chatgpt/login/start', { method: 'POST', body: {} }));
app.post('/api/gateway/chatgpt/login/complete', (req, res) =>
  proxyCore(req, res, '/api/gateway/chatgpt/login/complete', { method: 'POST', body: req.body ?? {} }));
// custom provider from the first-run AI step (drift item 50); core-api checks the owner
app.post('/api/gateway/cli', (req, res) =>
  proxyCore(req, res, '/api/gateway/cli', { method: 'POST', body: req.body ?? {} }));
// the models an address offers for a key, listed on the same step
app.post('/api/gateway/cli/models', (req, res) =>
  proxyCore(req, res, '/api/gateway/cli/models', { method: 'POST', body: req.body ?? {} }));

// Serve the shell with the build stamp injected, so every CSS/JS URL carries
// ?v=BUILD and the browser refetches them whenever we redeploy.
// The Studio is the new shell (public/next2, rebuilt 2026-10-30) at the root of
// its host, as the path contract says the final build runs. The
// old shell (public/index.html) is no longer served.
const SHELL = readFileSync(join(__dirname, '..', 'public', 'next2', 'index.html'), 'utf8').replaceAll('__V__', BUILD);
app.get(['/', '/index.html'], (_req, res) =>
  res.type('html').setHeader('Cache-Control', 'no-store').send(SHELL));

// /tasks (and a task deep-link) serve the SAME shell; app.js detects the path
// and opens the Tasks view inside the app. Not a separate page.
// /work-map joins the list: it lives inside the Studio shell so the sidebar
// wraps it like every other screen. The SPA fallback below catches it too,
// listing it here is for clarity.
app.get(['/tasks', '/tasks/:id', '/dashboard', '/docs', '/docs/:slug', '/chats/:id', '/settings', '/work-map'], (_req, res) =>
  res.type('html').setHeader('Cache-Control', 'no-store').send(SHELL));

// /next2/... is where the new shell grew side by side with the old one; links made
// then keep opening it. Its own scripts and styles stay under /next2.
app.get(/^\/next2(\/(?!js\/|css\/).*)?$/, (_req, res) =>
  res.type('html').setHeader('Cache-Control', 'no-store').send(SHELL));

// No-store during active development so iterating on the UI never serves a stale
// asset from the browser cache. (Revisit with hashed filenames before launch.)
app.use('/avatars/custom', express.static(AVATAR_STATE_DIR, {
  setHeaders: (res) => res.setHeader('Cache-Control', 'no-store'),
}));
app.use(express.static(join(__dirname, '..', 'public'), {
  setHeaders: (res) => res.setHeader('Cache-Control', 'no-store'),
}));

// SPA fallback: any other GET that is not an /api call and is not a static
// file serves the shell, so deep links and hard reloads (e.g.
// /agents/<slug>/mail, /agents/<slug>/activity) work instead of 'Cannot GET'.
// API paths are excluded so their 404s stay JSON.
app.get(/^(?!\/api\/).*/, (_req, res) =>
  res.type('html').setHeader('Cache-Control', 'no-store').send(SHELL));

const server = app.listen(Number(PORT), HOST, () => {
  console.log(`[studio] listening on ${HOST}:${PORT} (auth: ${AUTHUM_AUTH_URL}, core: ${CORE_API_URL})`);
});

// ---- /api/live: one WebSocket per page for everything live ----
// The page used to hold two server-sent-event streams (the bus, and the open
// chat's turn). On plain http a browser keeps six connections to one host and
// counts those streams among them, so with a few tabs open a sent message waited
// in the browser's queue until a stream closed (tester, 2026-10-30: "Enter did
// nothing for 25 s"). WebSockets are not counted in that limit. One socket now
// carries both: {kind: "event", ...} from core's bus, and {kind: "live", desk,
// data} for the chat the page watches ({watch: <desk id> | null} from the page).
// Who it is comes from the upgrade request, which Traefik's forward auth checks
// like any other; the streams behind it are read with that person's rights.
async function* sseData(body) {
  let buf = '';
  for await (const chunk of body) {
    buf += Buffer.from(chunk).toString('utf8');
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2);
      const data = block.split('\n').filter((l) => l.startsWith('data: ')).map((l) => l.slice(6)).join('\n');
      if (data) yield data;
    }
  }
}
const live = new WebSocketServer({ noServer: true });
server.on('upgrade', (req, socket, head) => {
  if (!req.url.startsWith('/api/live') || !req.headers['remote-user']) { socket.destroy(); return; }
  live.handleUpgrade(req, socket, head, (ws) => openLive(ws, String(req.headers['remote-user'])));
});
function openLive(ws, user) {
  const send = (o) => { if (ws.readyState === 1) ws.send(JSON.stringify(o)); };
  const bus = new AbortController();
  let watch = null;   // { desk, ac } for the chat whose turn the page follows
  (async () => {
    try {
      const up = await fetch(`${CORE_API_URL}/api/events/stream`,
        { headers: relayHeaders(user, { Accept: 'text/event-stream' }), signal: bus.signal });
      if (!up.ok) { ws.close(4001, 'bus refused'); return; }
      send({ kind: 'open' });
      for await (const d of sseData(up.body)) { try { send({ kind: 'event', ...JSON.parse(d) }); } catch {} }
    } catch {}
    ws.close(1011, 'bus ended');   // the page opens it again and reads everything once
  })();
  async function follow(desk) {
    if (watch) { watch.ac.abort(); watch = null; }
    if (!desk) return;
    const ac = new AbortController();
    watch = { desk, ac };
    try {
      const mine = await fetch(`${CORE_API_URL}/api/desks`, { headers: relayHeaders(user), signal: ac.signal });
      if (!mine.ok || !(((await mine.json()).desks) || []).some((d) => d.id === desk)) return;   // only one's own chats
      const up = await fetch(`${BRIN_URL}/api/desks/${encodeURIComponent(desk)}/live`,
        { headers: { 'X-API-Key': BRIN_API_KEY, Accept: 'text/event-stream' }, signal: ac.signal });
      if (!up.ok) return;
      for await (const d of sseData(up.body)) { try { send({ kind: 'live', desk, data: JSON.parse(d) }); } catch {} }
    } catch {}
  }
  ws.on('message', (m) => {
    let o; try { o = JSON.parse(String(m)); } catch { return; }
    if ('watch' in o) follow(typeof o.watch === 'string' ? o.watch : null);
  });
  // a quiet socket is kept open through proxies
  // The tick is a message the PAGE can see (a protocol ping is answered by the browser itself, so a page whose
  // socket died quietly never learns it): no tick for 70 s and the page opens a new socket.
  const ping = setInterval(() => { if (ws.readyState === 1) { ws.ping(); send({ kind: 'tick' }); } }, 25000);
  ws.on('close', () => { clearInterval(ping); bus.abort(); if (watch) watch.ac.abort(); });
}
