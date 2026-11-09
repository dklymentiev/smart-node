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
const body = async (req) => {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : null;
};

test('a chat mention reaches the colleagues; a message for colleagues only skips the chat agent', async () => {
  const mentions = [];
  const brinCalls = [];
  // core-api: ann and alex are colleagues, bob is no agent, foreman is the chat's own.
  const core = createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.method === 'POST' && req.url.endsWith('/mentions')) {
      const b = await body(req);
      mentions.push(b);
      if (b.agent === 'bob') { res.statusCode = 404; return res.end(JSON.stringify({ detail: 'no active agent' })); }
      if (b.agent === 'foreman') { res.statusCode = 409; return res.end(JSON.stringify({ detail: 'own agent' })); }
      res.statusCode = 201;
      return res.end(JSON.stringify({ message_id: mentions.length, agent: b.agent }));
    }
    res.end(JSON.stringify({ desks: [] }));
  });
  const brin = createServer(async (req, res) => {
    brinCalls.push(await body(req));
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ response: 'ok' }));
  });
  const corePort = await listen(core);
  const brinPort = await listen(brin);
  const reservation = createServer();
  const studioPort = await listen(reservation);
  await close(reservation);
  const avatars = mkdtempSync(join(tmpdir(), 'studio-mention-'));
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
    const post = (path, payload) => fetch(url + path, { method: 'POST',
      headers: { 'Remote-User': 'alice', 'Content-Type': 'application/json' },
      body: JSON.stringify(payload) });

    // For colleagues only: each told directly, the first row is the turn, brin untouched.
    const r = await post('/reply', { agents: ['ann', 'bob', 'alex', 'ann'], message: '@ann @bob @alex which of you?' });
    assert.equal(r.status, 201);
    assert.deepEqual((await r.json()).agents, ['ann', 'alex']);
    assert.deepEqual(mentions.map((m) => [m.agent, m.direct, m.turn]),
      [['ann', true, true], ['bob', true, false], ['alex', true, false]]);
    assert.equal(brinCalls.length, 0);

    // Nobody to tell: core-api's answer comes back.
    mentions.length = 0;
    assert.equal((await post('/reply', { agent: 'bob', message: 'hi' })).status, 404);

    // A mention inside an ordinary message: the colleague is told, and the chat's
    // agent still gets the message.
    mentions.length = 0;
    const c = await post('/chat', { message: 'Foreman, ask @ann and @foreman about it' });
    assert.equal(c.status, 200);
    assert.equal(c.headers.get('x-sn-mentioned'), 'ann');
    assert.deepEqual(mentions.map((m) => [m.agent, !!m.direct]), [['ann', false], ['foreman', false]]);
    assert.equal(brinCalls.length, 1);

    // A chat message that starts with @colleague: for the colleagues only, as a
    // /reply would be. The chat's agent is not asked (2026-10-29: both answered).
    mentions.length = 0;
    const d = await post('/chat', { message: '@ann @alex how tall is Everest?' });
    assert.equal(d.status, 201);
    assert.deepEqual((await d.json()).agents, ['ann', 'alex']);
    assert.deepEqual(mentions.map((m) => [m.agent, m.direct, m.turn]),
      [['ann', true, true], ['alex', true, false]]);
    assert.equal(brinCalls.length, 1);

    // It starts with the chat's own agent, or with no agent: the chat's message.
    for (const message of ['@foreman and @ann, look', '@bob hi']) {
      const e = await post('/chat', { message });
      assert.equal(e.status, 200, message);
    }
    assert.equal(brinCalls.length, 3);
  } finally {
    if (child.exitCode === null) {
      child.kill();
      await new Promise((r) => child.once('exit', r));
    }
    await close(core);
    await close(brin);
    rmSync(avatars, { recursive: true, force: true });
  }
});
