import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SERVER = join(__dirname, '..', 'src', 'server.js');

let stateDir;
let studio;
let upstream;
let base;
let relayed;

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return server.address().port;
}

before(async () => {
  stateDir = await mkdtemp(join(tmpdir(), 'studio-avatar-state-'));
  await writeFile(join(stateDir, 'http-agent.webp'), Buffer.from('durable-picture'));

  upstream = createServer((req, res) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => {
      relayed = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks) };
      res.writeHead(201, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ key: 'custom/http-agent-new.png', url: '/avatars/custom/http-agent-new.png' }));
    });
  });
  const upstreamPort = await listen(upstream);

  // Reserve an ephemeral port, then release it for the child. The very small
  // race is preferable to a fixed port that makes parallel test runs collide.
  const reservation = createServer();
  const studioPort = await listen(reservation);
  await new Promise((resolve) => reservation.close(resolve));
  base = `http://127.0.0.1:${studioPort}`;
  const mockUrl = `http://127.0.0.1:${upstreamPort}`;
  studio = spawn('node', [SERVER], {
    env: {
      ...process.env,
      PORT: String(studioPort),
      HOST: '127.0.0.1',
      AUTHUM_AUTH_URL: mockUrl,
      CORE_API_URL: mockUrl,
      BRIN_URL: mockUrl,
      BRIN_API_KEY: 'test-key',
      DOCI_URL: mockUrl,
      SN_AVATAR_STATE_DIR: stateDir,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      if ((await fetch(`${base}/healthz`)).ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('isolated Studio server did not start');
});

after(async () => {
  if (studio) studio.kill();
  if (upstream) await new Promise((resolve) => upstream.close(resolve));
  if (stateDir) await rm(stateDir, { recursive: true, force: true });
});

test('custom avatar bytes and catalog entry are served from durable state', async () => {
  const image = await fetch(`${base}/avatars/custom/http-agent.webp`);
  assert.equal(image.status, 200);
  assert.equal(await image.text(), 'durable-picture');

  const catalog = await (await fetch(`${base}/api/avatars`)).json();
  assert.ok(catalog.some((item) => item.key === 'custom/http-agent.webp'
    && item.url === '/avatars/custom/http-agent.webp'));
});

test('Studio relays avatar bytes and authenticated identity to core-api', async () => {
  const response = await fetch(`${base}/api/agents/http-agent/avatar?ext=png`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'Remote-User': 'owner@example.com' },
    body: Buffer.from('new-picture'),
  });
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), {
    key: 'custom/http-agent-new.png', url: '/avatars/custom/http-agent-new.png',
  });
  assert.equal(relayed.method, 'POST');
  assert.equal(relayed.url, '/api/agents/http-agent/avatar?ext=png');
  assert.equal(relayed.headers['remote-user'], 'owner@example.com');
  assert.deepEqual(relayed.body, Buffer.from('new-picture'));
});
