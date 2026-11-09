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

// The agent page shows the full instructions the agent reads (?body=true). The
// proxy dropped the query, so the page always got none (2026-10-30).
test('an agent read passes ?body=true through to core-api', async () => {
  const seen = [];
  const core = createServer((req, res) => {
    seen.push(req.url);
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ slug: 'max', body_md: req.url.includes('body=true') ? '# Max' : null }));
  });
  const corePort = await listen(core);
  const reservation = createServer();
  const studioPort = await listen(reservation);
  await close(reservation);
  const avatars = mkdtempSync(join(tmpdir(), 'studio-body-'));
  const child = spawn(process.execPath, [serverJs], {
    env: { ...process.env, PORT: String(studioPort), HOST: '127.0.0.1',
      AUTHUM_AUTH_URL: 'https://auth.example.com', CORE_API_URL: `http://127.0.0.1:${corePort}`,
      BRIN_URL: 'http://127.0.0.1:1', BRIN_API_KEY: 'test-key',
      DOCI_URL: 'http://127.0.0.1:1', SN_AVATAR_STATE_DIR: avatars },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => { stderr += String(chunk); });
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) {
      try { if ((await fetch(`http://127.0.0.1:${studioPort}/healthz`)).ok) { ready = true; break; } } catch {}
      await new Promise((r) => setTimeout(r, 50));
    }
    assert.ok(ready, `Studio started: ${stderr}`);
    const get = (q) => fetch(`http://127.0.0.1:${studioPort}/api/agents/max${q}`, { headers: { 'Remote-User': 'alice' } }).then((r) => r.json());
    assert.equal((await get('?body=true')).body_md, '# Max');
    assert.equal((await get('')).body_md, null);
    assert.deepEqual(seen, ['/api/agents/max?body=true', '/api/agents/max']);
  } finally {
    if (child.exitCode === null) {
      const exited = new Promise((r) => child.once('exit', r));
      child.kill();
      await exited;
    }
    core.closeAllConnections();
    await close(core);
    rmSync(avatars, { recursive: true, force: true });
  }
});
