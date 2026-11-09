import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const serverJs = join(dirname(fileURLToPath(import.meta.url)), '..', 'src/server.js');
const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => server.close(resolve));

test('steer and live text are visible only to the desk owner', async () => {
  const calls = [];
  const core = createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ desks: [{ id: req.headers['remote-user'] === 'alice' ? 'desk-a' : 'desk-b' }] }));
  });
  const brin = createServer(async (req, res) => {
    calls.push({ method: req.method, url: req.url });
    res.setHeader('Content-Type', 'application/json');
    if (req.method === 'POST') {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString());
      if (req.url.endsWith('/cancel-queued')) {
        assert.equal(body.client_turn_id, '123e4567-e89b-42d3-a456-426614174000');
        res.end(JSON.stringify({ status: 'cancelled' }));
      } else {
        assert.equal(body.text, 'change course');
        res.end(JSON.stringify({ status: 'accepted', request_id: 'r1' }));
      }
    } else res.end(JSON.stringify({ in_progress: true, text: 'A full live answer' }));
  });
  const corePort = await listen(core);
  const brinPort = await listen(brin);
  const reservation = createServer();
  const studioPort = await listen(reservation);
  await close(reservation);
  const avatars = mkdtempSync(join(tmpdir(), 'studio-steer-'));
  const child = spawn(process.execPath, [serverJs], {
    env: { ...process.env, PORT: String(studioPort), HOST: '127.0.0.1',
      AUTHUM_AUTH_URL: 'https://auth.example.com', CORE_API_URL: `http://127.0.0.1:${corePort}`,
      BRIN_URL: `http://127.0.0.1:${brinPort}`, BRIN_API_KEY: 'test-key',
      DOCI_URL: 'http://127.0.0.1:1', SN_AVATAR_STATE_DIR: avatars },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += String(chunk); });
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try {
        const r = await fetch(`http://127.0.0.1:${studioPort}/healthz`);
        if (r.ok) { ready = true; break; }
      } catch {}
      await new Promise((r) => setTimeout(r, 50));
    }
    assert.ok(ready, `Studio started: ${stderr}`);
    const url = `http://127.0.0.1:${studioPort}/api/desks/desk-a`;
    const post = (user) => fetch(url + '/steer', { method: 'POST',
      headers: { 'Remote-User': user, 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'change course' }) });
    assert.equal((await post('bob')).status, 404);
    assert.equal((await fetch(url + '/activity', { headers: { 'Remote-User': 'bob' } })).status, 404);
    const cancel = (user) => fetch(url + '/cancel-queued', { method: 'POST',
      headers: { 'Remote-User': user, 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_turn_id: '123e4567-e89b-42d3-a456-426614174000' }) });
    assert.equal((await cancel('bob')).status, 404);
    assert.equal(calls.length, 0);
    const accepted = await post('alice');
    assert.equal(accepted.status, 200);
    assert.equal((await accepted.json()).status, 'accepted');
    const activity = await fetch(url + '/activity', { headers: { 'Remote-User': 'alice' } });
    assert.equal((await activity.json()).text, 'A full live answer');
    assert.equal((await (await cancel('alice')).json()).status, 'cancelled');
    assert.equal(calls.length, 3);
  } finally {
    if (child.exitCode === null) {
      const exited = new Promise((r) => child.once('exit', r));
      child.kill();
      await exited;
    }
    core.closeAllConnections();
    brin.closeAllConnections();
    await Promise.all([close(core), close(brin)]);
    rmSync(avatars, { recursive: true, force: true });
  }
});
