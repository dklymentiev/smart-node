#!/usr/bin/env node
import { spawn, spawnSync } from 'node:child_process';
import { createHash, randomBytes, X509Certificate } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { access, mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import tls from 'node:tls';

const HITL_ROOT = '/opt/smart-node/hitl';
const HITL_PYTHON = `${HITL_ROOT}/venv/bin/python`;
let HITL_CALLER = `${HITL_ROOT}/caller/hitl.py`;
const HITL_ADMIN = `${HITL_ROOT}/server/bin/admin.py`;
const POLLER = `${HITL_ROOT}/flow_poller.py`;
const PLANNER_ENV = '/etc/smart-node/planner-key-runner.env';
const HITL_ENV = '/etc/smart-node/hitl.env';
const FLOW_DIR = '/var/lib/smart-node/workflows/flows/content-article-hitl';
const TASK_ROOT = '/var/lib/smart-node/workflows/tasks';

function fail(message) {
  throw new Error(message);
}

function options(argv) {
  const result = { insecure: false };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === '--insecure') result.insecure = true;
    else if (['--url', '--ca-file', '--chrome'].includes(value)) {
      const next = argv[index + 1];
      if (!next || next.startsWith('--')) fail(`missing value for ${value}`);
      result[{ '--url': 'url', '--ca-file': 'caFile', '--chrome': 'chrome' }[value]] = next;
      index += 1;
    } else if (value === '--help' || value === '-h') {
      console.log('Usage: sudo node [--experimental-websocket] signed-answer-e2e.mjs --url https://hitl.example [--ca-file trusted-ca.pem | --insecure] [--chrome /path/to/chrome]');
      console.log('Requires Node.js 22+, or Node.js 20 with --experimental-websocket, plus the installed flow and planner credentials.');
      process.exit(0);
    } else fail(`unknown option: ${value}`);
  }
  if (!result.url) fail('--url is required');
  if (typeof WebSocket !== 'function') fail('use Node.js 22+, or Node.js 20 with --experimental-websocket');
  const parsed = new URL(result.url);
  if (parsed.protocol !== 'https:' || parsed.pathname !== '/' || parsed.search || parsed.hash) {
    fail('--url must be an HTTPS origin without a path, query or fragment');
  }
  result.url = parsed.origin;
  if (result.caFile && result.insecure) fail('choose either --ca-file or --insecure');
  return result;
}

function run(command, args, env = process.env) {
  const result = spawnSync(command, args, { encoding: 'utf8', env });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    fail(`${command} failed (${result.status}): ${(result.stderr || result.stdout).trim()}`);
  }
  return result.stdout.trim();
}

function hitlEnv(command, args, stateDir, settings) {
  const env = { ...process.env, HITL_STATE: stateDir, HITL_URL: settings.url };
  if (settings.caFile) env.SSL_CERT_FILE = settings.caFile;
  if (settings.insecure) env.PYTHONHTTPSVERIFY = '0';
  return run(command, args, env);
}

function caller(args, stateDir, settings) {
  return JSON.parse(hitlEnv(HITL_PYTHON, [HITL_CALLER, ...args], stateDir, settings));
}

function admin(args) {
  return run('bash', ['-c', 'set -e; set -a; source "$1"; set +a; exec "$2" "$3" "${@:4}"',
    'hitl-e2e', HITL_ENV, HITL_PYTHON, HITL_ADMIN, ...args]);
}

function planner(method, path, body) {
  const args = ['-fsS', '--connect-timeout', '5', '--max-time', '30', '-X', method,
    '-H', 'Content-Type: application/json', '-w', '\n%{http_code}'];
  if (body !== undefined) args.push('-d', JSON.stringify(body));
  args.push(`http://127.0.0.1:8080${path}`);
  const shell = 'set -e; set -a; source "$1"; set +a; shift; exec curl "$@"';
  const output = run('bash', ['-c', shell, 'planner-e2e', PLANNER_ENV, ...args]);
  const split = output.lastIndexOf('\n');
  const status = Number(split < 0 ? output : output.slice(split + 1));
  if (status < 200 || status >= 300) fail(`${method} ${path} failed with HTTP ${status}`);
  const raw = split < 0 ? '' : output.slice(0, split);
  return raw ? JSON.parse(raw) : null;
}

function certificatePin(settings) {
  return new Promise((resolve, reject) => {
    const address = new URL(settings.url);
    const socket = tls.connect({
      host: address.hostname,
      port: Number(address.port || 443),
      servername: address.hostname,
      ...(settings.caFile ? { ca: readFileSync(settings.caFile) } : {}),
      rejectUnauthorized: !settings.insecure,
    });
    socket.setTimeout(10000, () => socket.destroy(new Error('TLS connection timed out')));
    socket.once('secureConnect', () => {
      try {
        const certificate = new X509Certificate(socket.getPeerCertificate().raw);
        const spki = certificate.publicKey.export({ type: 'spki', format: 'der' });
        resolve(createHash('sha256').update(spki).digest('base64'));
      } catch (error) {
        reject(error);
      } finally {
        socket.destroy();
      }
    });
    socket.once('error', reject);
  });
}

async function waitFor(read, description, timeout = 30000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    try {
      const value = await read();
      if (value) return value;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  fail(`timed out waiting for ${description}`);
}

class Cdp {
  constructor(socket, sessionId) {
    this.socket = socket;
    this.sessionId = sessionId;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    });
  }

  send(method, params = {}, session = true) {
    const id = this.nextId++;
    const message = { id, method, params };
    if (session && this.sessionId) message.sessionId = this.sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify(message));
    });
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true, userGesture: true,
    });
    if (result.exceptionDetails) fail(result.exceptionDetails.text || 'browser evaluation failed');
    return result.result.value;
  }
}

async function launchBrowser(settings, pin) {
  const directory = await mkdtemp(join(tmpdir(), 'hitl-signed-e2e-'));
  const command = settings.chrome || ['google-chrome', 'chromium', 'chromium-browser']
    .map(name => spawnSync('which', [name], { encoding: 'utf8' }).stdout.trim())
    .find(Boolean);
  if (!command) fail('Chrome or Chromium was not found; pass --chrome');
  const child = spawn(command, [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run',
    '--no-default-browser-check', '--remote-debugging-port=0',
    `--user-data-dir=${directory}`,
    `--ignore-certificate-errors-spki-list=${pin}`,
    'about:blank',
  ], { stdio: 'ignore' });
  child.once('error', error => fail(`could not start Chrome: ${error.message}`));
  const activePort = join(directory, 'DevToolsActivePort');
  const port = await waitFor(async () => {
    try { return Number((await readFile(activePort, 'utf8')).split('\n')[0]); }
    catch { return null; }
  }, 'Chrome DevTools');
  const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(response => response.json());
  const socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  const cdp = new Cdp(socket, null);
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' }, false);
  const attached = await cdp.send('Target.attachToTarget', { targetId, flatten: true }, false);
  cdp.sessionId = attached.sessionId;
  const page = cdp;
  await page.send('Page.enable');
  await page.send('Runtime.enable');
  await page.send('WebAuthn.enable');
  const { authenticatorId } = await page.send('WebAuthn.addVirtualAuthenticator', {
    options: {
      protocol: 'ctap2', ctap2Version: 'ctap2_1', transport: 'internal',
      hasResidentKey: true, hasUserVerification: true, isUserVerified: true,
      hasPrf: true, automaticPresenceSimulation: true,
    },
  });
  return { cdp, page, child, directory, targetId, authenticatorId };
}

async function click(page, selector) {
  const encoded = JSON.stringify(selector);
  return page.evaluate(`(() => { const e = document.querySelector(${encoded}); if (!e) return false; e.click(); return true; })()`);
}

async function enroll(page, link) {
  await page.send('Page.navigate', { url: link });
  await waitFor(() => page.evaluate("Boolean(document.querySelector('#enrol'))"), 'enrolment page');
  await page.evaluate("document.querySelector('#label').value = 'Automated verification'");
  if (!await click(page, '#enrol')) fail('the enrolment button is unavailable');
  await waitFor(() => page.evaluate("Boolean(document.querySelector('#confirm-devices'))"), 'passkey confirmation');
  if (!await click(page, '#confirm-devices')) fail('the passkey confirmation button is unavailable');
  return waitFor(() => page.evaluate("document.querySelector('#fingerprint')?.textContent.trim() || ''"), 'enrolled key fingerprint');
}

async function answer(page, link) {
  await page.send('Page.navigate', { url: link });
  await waitFor(() => page.evaluate(`Boolean(document.querySelector('[data-choice="A"]'))`), 'question page');
  if (!await click(page, '[data-choice="A"]')) fail('the answer button is unavailable');
  await waitFor(() => page.evaluate("document.body.innerText.includes('Your response was signed and sent.')"), 'signed response');
}

async function main() {
  const settings = options(process.argv.slice(2));
  if (process.getuid() !== 0) fail('run as root so the script can use the installed HITL and planner credentials');
  try { await access(HITL_CALLER); }
  catch { HITL_CALLER = `${HITL_ROOT}/app/caller/hitl.py`; }
  for (const path of [HITL_PYTHON, HITL_CALLER, HITL_ADMIN, POLLER, PLANNER_ENV, HITL_ENV,
    `${FLOW_DIR}/content-article-hitl.yaml`, '/opt/smart-node/workflows/venv/bin/python']) {
    await access(path).catch(() => fail(`required installed file is missing: ${path}`));
  }
  const pin = await certificatePin(settings);
  const profile = await mkdtemp(join(tmpdir(), 'hitl-e2e-state-'));
  const stateDir = join(profile, 'caller');
  const taskRoot = join(TASK_ROOT, `.signed-answer-e2e-${randomBytes(6).toString('hex')}`);
  const runDir = join(taskRoot, 'run');
  const artifacts = join(runDir, 'artifacts');
  const callerId = `e2e-${randomBytes(6).toString('hex')}`;
  const recipientId = callerId;
  let browser;
  let taskId;
  let tagAdded = false;

  try {
    await mkdir(stateDir, { recursive: true, mode: 0o700 });
    const key = caller(['keygen', callerId], stateDir, settings);
    admin(['add-caller', callerId, 'Automated verification', 'local', key.pubkey]);
    const enrollment = admin(['enrol-link', recipientId, 'Automated verification', '1']).split('\n').at(-1).trim();
    if (new URL(enrollment).origin !== settings.url) fail('the installed HITL origin does not match --url');

    const task = planner('POST', '/api/v1/tasks', {
      title: `Signed answer verification ${callerId}`,
      description: 'Temporary task used to verify delivery of a signed HITL answer.',
      status: 'open',
    });
    taskId = task.id;
    planner('POST', `/api/v1/tasks/${taskId}/tags`, { tag: 'blocked:approval' });
    tagAdded = true;

    browser = await launchBrowser(settings, pin);
    const fingerprint = await enroll(browser.page, enrollment);
    caller(['pin', recipientId, '--fingerprint', fingerprint], stateDir, settings);
    const question = caller([
      'ask', recipientId, '--title', 'Automated signed-answer check',
      '--text', 'Choose the first answer to confirm the signed response path.',
      '--choices', 'A:Confirm,B:Reject', '--ttl', '1800', '--ref', `task:${taskId}:signed-answer-e2e`,
    ], stateDir, settings);
    if (question.status !== 'created') fail(`question creation failed: ${question.status}`);

    await mkdir(artifacts, { recursive: true, mode: 0o750 });
    await writeFile(join(artifacts, 'inputs.json'), JSON.stringify({ task_id: taskId }) + '\n', { mode: 0o640 });
    await writeFile(join(artifacts, 'hitl-request.json'), JSON.stringify({
      status: 'pending', announced: true, request_id: question.request_id,
    }) + '\n', { mode: 0o640 });
    run('chown', ['-R', 'smart-node-dispatcher:smart-node', taskRoot]);
    run('chown', ['-R', 'smart-node-dispatcher:smart-node', profile]);

    await answer(browser.page, question.link);
    const verified = caller(['check', question.request_id], stateDir, settings);
    if (verified.status !== 'answered' || verified.answer?.choice !== 'A') {
      fail('the caller did not verify the signed answer');
    }

    const pollerShell = [
      'set -e', 'set -a', `source ${HITL_ENV}`, `source ${PLANNER_ENV}`, 'set +a',
      'export PLANNER_API_URL=http://127.0.0.1:8080',
      'export CONTENT_ARTICLE_HITL_FLOW_DIR="$1"',
      'export CONTENT_ARTICLE_HITL_TASKS_DIR="$2"',
      'export CONTENT_ARTICLE_HITL_STATE_DIR="$3"',
      'export HITL_URL="$4"',
      'if [[ -n "$5" ]]; then export SSL_CERT_FILE="$5"; else unset SSL_CERT_FILE; fi',
      'if [[ "$6" == 0 ]]; then export PYTHONHTTPSVERIFY=0; else unset PYTHONHTTPSVERIFY; fi',
      `exec /usr/sbin/runuser --preserve-environment -u smart-node-dispatcher -- ${HITL_ROOT}/venv/bin/python ${POLLER}`,
    ].join('; ');
    run('bash', ['-c', pollerShell, 'signed-answer-e2e', FLOW_DIR, taskRoot, stateDir,
      settings.url, settings.caFile || '', settings.insecure ? '0' : '1']);

    const comments = planner('GET', `/api/v1/tasks/${taskId}/comments`);
    const received = comments.some(item => item.content.includes(`signed answer received for request ${question.request_id}`));
    const tags = planner('GET', `/api/v1/tasks/${taskId}/tags`);
    if (!received || tags.includes('blocked:approval')) fail('the signed answer did not reach and unblock the task');
    tagAdded = false;
    console.log(JSON.stringify({
      status: 'ok', task_id: taskId, request_id: question.request_id,
      test_identity: callerId, answer: verified.answer.choice, delivered_to_task: true,
    }));
  } finally {
    if (browser) {
      try { await browser.page.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId: browser.authenticatorId }); } catch {}
      try { await browser.cdp.send('Target.closeTarget', { targetId: browser.targetId }, false); } catch {}
      browser.page.socket.close();
      browser.child.kill('SIGTERM');
      await rm(browser.directory, { recursive: true, force: true });
    }
    if (taskId) {
      if (tagAdded) {
        try { planner('DELETE', `/api/v1/tasks/${taskId}/tags/blocked%3Aapproval`); } catch {}
      }
      try { planner('DELETE', `/api/v1/tasks/${taskId}`); } catch {}
    }
    await rm(taskRoot, { recursive: true, force: true });
    await rm(profile, { recursive: true, force: true });
  }
}

main().catch(error => {
  console.error(error.message || error);
  process.exitCode = 1;
});
