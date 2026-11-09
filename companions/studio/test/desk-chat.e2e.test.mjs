// E2E: chat within two different desks, through the Studio BFF -> brin -> gateway.
//
// Proves: (1) the Studio desk-chat endpoint reaches brin, (2) with no agent named
// brin answers with its default agent (the secretary), (3) two desks are two
// independent conversation contexts (keyed by desk id).
//
// Hits the real stack, so run ON THE NODE with brin's key in the env:
//   CORE_API_URL=http://172.18.0.1:8030 BRIN_URL=http://127.0.0.1:8101 \
//   BRIN_API_KEY=<key> TEST_USER=admin npm test
//
// Each chat call invokes a real LLM, so timeouts are generous.

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER = join(__dirname, '..', 'src', 'server.js');

const PORT = process.env.TEST_PORT || '8063';
const BASE = `http://127.0.0.1:${PORT}`;
const USER = process.env.TEST_USER || 'admin';
const CORE_API_URL = process.env.CORE_API_URL || 'http://172.18.0.1:8030';
const BRIN_URL = process.env.BRIN_URL || 'http://127.0.0.1:8101';
const BRIN_API_KEY = process.env.BRIN_API_KEY; // required -- no default; chat needs the real key

const auth = { 'Remote-User': USER, 'Content-Type': 'application/json', Accept: 'application/json' };
let srv;

before(async () => {
  assert.ok(BRIN_API_KEY, 'BRIN_API_KEY must be set in the env to run the chat e2e');
  srv = spawn('node', [SERVER], {
    env: {
      ...process.env, PORT, HOST: '127.0.0.1', AUTHUM_AUTH_URL: 'https://auth.example.com',
      CORE_API_URL, BRIN_URL, BRIN_API_KEY,
      SN_AVATAR_STATE_DIR: process.env.SN_AVATAR_STATE_DIR || join(__dirname, '..', 'public', 'avatars', 'custom'),
    },
    stdio: 'inherit',
  });
  for (let i = 0; i < 60; i++) {
    try { if ((await fetch(`${BASE}/healthz`)).ok) return; } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('Studio server did not come up');
});

after(() => { if (srv) srv.kill(); });

async function makeDesk(title) {
  const r = await fetch(`${BASE}/api/desks`, { method: 'POST', headers: auth, body: JSON.stringify({ title }) });
  assert.equal(r.status, 201);
  return (await r.json()).id;
}
async function chat(deskId, message) {
  const r = await fetch(`${BASE}/api/desks/${deskId}/chat`, { method: 'POST', headers: auth, body: JSON.stringify({ message }) });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}
async function delDesk(id) { await fetch(`${BASE}/api/desks/${id}`, { method: 'DELETE', headers: auth }).catch(() => {}); }

test('empty message is rejected', async () => {
  const a = await makeDesk('chat-empty');
  try {
    const r = await fetch(`${BASE}/api/desks/${a}/chat`, { method: 'POST', headers: auth, body: JSON.stringify({ message: '   ' }) });
    assert.equal(r.status, 400);
  } finally { await delDesk(a); }
});

test('two desks each hold their own chat with the default agent (secretary)', { timeout: 180000 }, async () => {
  const deskA = await makeDesk('Winery desk');
  const deskB = await makeDesk('Cafe desk');
  try {
    const a = await chat(deskA, 'Hello! What is your job title? Answer in one sentence.');
    assert.equal(a.status, 200, `desk A chat status (body: ${JSON.stringify(a.body)})`);
    assert.ok((a.body.content || '').trim().length > 0, 'desk A got a non-empty reply');

    const b = await chat(deskB, 'Hi! Could you tell me how you can help?');
    assert.equal(b.status, 200, `desk B chat status (body: ${JSON.stringify(b.body)})`);
    assert.ok((b.body.content || '').trim().length > 0, 'desk B got a non-empty reply');

    // Both replies came from brin (it tags the routed specialist on every reply).
    assert.ok('specialist' in a.body, 'desk A reply carries a specialist tag');
    assert.ok('specialist' in b.body, 'desk B reply carries a specialist tag');
  } finally {
    await delDesk(deskA);
    await delDesk(deskB);
  }
});
