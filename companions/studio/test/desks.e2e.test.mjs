// E2E: desk create/list/delete through the Studio BFF -> core-api -> Postgres.
//
// Spawns the real Studio server (src/server.js) against a live core-api and
// drives the full lifecycle over HTTP, exactly as the browser does. It hits the
// real stack, so it must run ON THE NODE (core-api reachable, a real user row):
//
//   CORE_API_URL=http://172.18.0.1:8030 TEST_USER=admin npm test
//
// Env knobs: TEST_PORT (default 8062), TEST_USER (default admin),
// CORE_API_URL (default http://172.18.0.1:8030).

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER = join(__dirname, '..', 'src', 'server.js');

const PORT = process.env.TEST_PORT || '8062';
const BASE = `http://127.0.0.1:${PORT}`;
const USER = process.env.TEST_USER || 'admin';
const CORE_API_URL = process.env.CORE_API_URL || 'http://172.18.0.1:8030';

const auth = { 'Remote-User': USER, 'Content-Type': 'application/json', Accept: 'application/json' };
let srv;

before(async () => {
  srv = spawn('node', [SERVER], {
    env: {
      ...process.env, PORT, HOST: '127.0.0.1', AUTHUM_AUTH_URL: 'https://auth.example.com', CORE_API_URL,
      BRIN_URL: process.env.BRIN_URL || 'http://127.0.0.1:8101',
      BRIN_API_KEY: process.env.BRIN_API_KEY || 'test-key',   // desks test never calls brin
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

test('GET /api/desks without identity is rejected (auth not applied)', async () => {
  const r = await fetch(`${BASE}/api/desks`);
  assert.equal(r.status, 401);
});

test('desk lifecycle: create -> list -> delete -> gone -> 404', async () => {
  // create
  const created = await fetch(`${BASE}/api/desks`, {
    method: 'POST', headers: auth, body: JSON.stringify({ title: 'E2E desk' }),
  });
  assert.equal(created.status, 201);
  const desk = await created.json();
  assert.match(desk.id, /^[0-9a-f-]{36}$/, 'desk id is a uuid');
  assert.equal(desk.title, 'E2E desk');

  try {
    // list contains it
    const listed = await fetch(`${BASE}/api/desks`, { headers: auth });
    assert.equal(listed.status, 200);
    const { desks } = await listed.json();
    assert.ok(desks.some((d) => d.id === desk.id), 'created desk appears in list');

    // delete
    const del = await fetch(`${BASE}/api/desks/${desk.id}`, { method: 'DELETE', headers: auth });
    assert.equal(del.status, 204);

    // gone
    const after = await (await fetch(`${BASE}/api/desks`, { headers: auth })).json();
    assert.ok(!after.desks.some((d) => d.id === desk.id), 'desk is gone after delete');

    // deleting again is a 404 (owner-scoped, idempotent-safe)
    const again = await fetch(`${BASE}/api/desks/${desk.id}`, { method: 'DELETE', headers: auth });
    assert.equal(again.status, 404);
  } finally {
    // best-effort cleanup if an assertion above threw before delete
    await fetch(`${BASE}/api/desks/${desk.id}`, { method: 'DELETE', headers: auth }).catch(() => {});
  }
});

test('default title is applied when none is given', async () => {
  const created = await fetch(`${BASE}/api/desks`, { method: 'POST', headers: auth, body: '{}' });
  assert.equal(created.status, 201);
  const desk = await created.json();
  assert.equal(desk.title, 'Untitled desk');
  await fetch(`${BASE}/api/desks/${desk.id}`, { method: 'DELETE', headers: auth });
});
