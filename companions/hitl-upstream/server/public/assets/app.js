// hitl web client: enrolment and decision pages.
// Decryption and signing happen here, on the recipient's device.
'use strict';

// ---- begin: protocol helpers (no page, no network: tests/vectors.mjs runs this block as it is)

const enc = new TextEncoder();
const dec = new TextDecoder();

function b64u(buf) {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function unb64u(s) {
  if (typeof s !== 'string' || !/^[A-Za-z0-9_-]*$/.test(s) || s.length % 4 === 1) throw new Error('invalid base64url');
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(s + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0));
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
}

function bytesEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}

function concat(a, b) {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

// JSON Canonicalization Scheme (RFC 8785) for strings, integers, booleans, null, lists, objects.
function jcs(v) {
  if (v === null || typeof v === 'boolean' || typeof v === 'string') return JSON.stringify(v);
  if (typeof v === 'number') {
    if (!Number.isSafeInteger(v)) throw new Error('jcs: only whole numbers of magnitude at most 2^53 - 1');
    return String(v);
  }
  if (Array.isArray(v)) return '[' + v.map(jcs).join(',') + ']';
  const keys = Object.keys(v).filter(k => v[k] !== undefined).sort();
  return '{' + keys.map(k => JSON.stringify(k) + ':' + jcs(v[k])).join(',') + '}';
}

function rfc3339(d) {
  return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

// Unix seconds for an RFC 3339 timestamp in UTC with a Z suffix, the one form signed objects use; else null.
function parseTs(s) {
  if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(s)) return null;
  const t = Date.parse(s);
  // One spelling per instant: hour 24, a 30th of February and the like parse to a time that prints differently.
  return !Number.isNaN(t) && t >= 0 && rfc3339(new Date(t)) === s ? t / 1000 : null;
}

// Every signed object names its type and has exactly these fields (R11): a signature over one kind
// of object can never pass as another, and nothing unsigned-for rides along in a signed one.
const SHAPES = {
  'hitl.request': [2, ['type', 'version', 'request_id', 'caller_id', 'recipient_id', 'config_ct_hash', 'payload_ct_hash',
    'answer_pubkey', 'ttl', 'caller_ref', 'created_at', 'policy', 'blobs']],
  'hitl.response': [2, ['type', 'version', 'request_id', 'creation_hash', 'config_hash', 'payload_hash', 'answer_ct', 'signed_at']],
  'hitl.key-statement': [2, ['type', 'version', 'recipient_id', 'epoch', 'seq', 'prev', 'enc_pubkey', 'credentials', 'issued_at']],
  'hitl.session': [1, ['type', 'version', 'recipient_id', 'session_pubkey', 'issued_at', 'expires_at']],
};

function shaped(o, type, extra = []) {
  if (o === null || typeof o !== 'object' || Array.isArray(o)) return false;
  const [version, fields] = SHAPES[type];
  const want = [...fields, ...extra].sort();
  const got = Object.keys(o).sort();
  return o.type === type && o.version === version && got.length === want.length && got.every((k, i) => k === want[i]);
}

// ISO/IEC 7816-4 padding to a multiple of block, as libsodium's sodium_pad: the length of a sealed
// answer then says nothing about the answer.
function pad(bytes, block) {
  const out = new Uint8Array((Math.floor(bytes.length / block) + 1) * block);
  out.set(bytes);
  out[bytes.length] = 0x80;
  return out;
}

function unpad(bytes, block) {
  if (!bytes.length || bytes.length % block) throw new Error('bad padding');
  let i = bytes.length - 1;
  while (i >= 0 && bytes[i] === 0) i--;
  if (i < 0 || bytes[i] !== 0x80 || bytes.length - i > block) throw new Error('bad padding');
  return bytes.subarray(0, i);
}

// An ECDSA P-256 signature as WebAuthn gives it (DER: SEQUENCE of two INTEGERs) to the r || s form
// WebCrypto verifies. Strict DER only, as the Python implementations accept: what one side takes
// for a valid signature the others must take too.
function ecdsaRaw(der) {
  const need = (ok) => { if (!ok) throw new Error('bad signature encoding'); };
  let i = 0;
  need(der[i++] === 0x30);
  const len = der[i++];
  need(len < 0x80 && len === der.length - i);  // at most 70 bytes of content: the short form is the only form
  const out = new Uint8Array(64);
  for (const at of [0, 32]) {
    need(der[i++] === 0x02);
    const n = der[i++];
    need(n > 0 && n <= 33 && i + n <= der.length);
    let v = der.subarray(i, i + n);
    i += n;
    need(!(v[0] & 0x80));  // a positive integer
    if (v[0] === 0 && n > 1) {
      need(v[1] & 0x80);  // a leading zero only where the sign bit needs it
      v = v.subarray(1);
    }
    need(v.length <= 32);
    out.set(v, at + 32 - v.length);
  }
  need(i === der.length);
  return out;
}

// A credential is ES256 with a P-256 key or RS256 with an RSA key of at least 2048 bits, and is
// verified under the algorithm it was enrolled with, never under whatever its key happens to allow.
async function verifySha256(cred, data, sig) {
  try {
    const spki = unb64u(cred.spki);
    if (cred.alg === -7) {
      const key = await crypto.subtle.importKey('spki', spki, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
      return await crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, key, ecdsaRaw(sig), data);
    }
    if (cred.alg === -257) {
      const key = await crypto.subtle.importKey('spki', spki, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
      return key.algorithm.modulusLength >= 2048 && await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, sig, data);
    }
  } catch (e) {
    return false;
  }
  return false;
}

// Checks a WebAuthn assertion the way the service and the caller library do. Returns null when valid.
async function assertionProblem(a, challenge, cred, origin, rpId) {
  let cdjRaw, ad, sig, cdj;
  try {
    cdjRaw = unb64u(a.client_data_json);
    ad = unb64u(a.authenticator_data);
    sig = unb64u(a.signature);
  } catch (e) {
    return 'assertion_incomplete';
  }
  try {
    cdj = JSON.parse(dec.decode(cdjRaw));
  } catch (e) {
    cdj = null;
  }
  if (cdj === null || typeof cdj !== 'object' || cdj.type !== 'webauthn.get') return 'client_data_type';
  if (cdj.challenge !== b64u(challenge)) return 'challenge_mismatch';
  if (cdj.origin !== origin || cdj.crossOrigin) return 'origin_mismatch';
  if (ad.length < 37 || !bytesEqual(ad.subarray(0, 32), await sha256(enc.encode(rpId)))) return 'rp_id_mismatch';
  if (!(ad[32] & 0x01)) return 'not_user_present';
  if (!(ad[32] & 0x04)) return 'not_user_verified';
  return await verifySha256(cred, concat(ad, await sha256(cdjRaw)), sig) ? null : 'signature_invalid';
}

// The credential list of a key statement, in the one form a statement may give it, or null.
function listedCredentials(st) {
  const creds = st.credentials;
  if (!Array.isArray(creds) || !creds.length) return null;
  let last = null;
  for (const c of creds) {
    if (c === null || typeof c !== 'object' || Object.keys(c).sort().join() !== 'alg,id,label,spki'
        || typeof c.id !== 'string' || !c.id || typeof c.spki !== 'string' || typeof c.label !== 'string'
        || (c.alg !== -7 && c.alg !== -257) || (last !== null && !(last < c.id))) return null;
    last = c.id;
  }
  return creds;
}

// The first 128 bits of SHA-256 of the latest key statement, as 32 hexadecimal digits in groups of four.
function fingerprintOf(hash) {
  return Array.from(hash.subarray(0, 16), b => b.toString(16).padStart(2, '0')).join('').match(/.{4}/g).join(' ');
}

// What a device knows about its own key set without any storage of its own: the anchor inside the
// blob only its authenticator can open names the statement that enrolled it. From there the chain
// is replayed link by link: each statement names the hash of the one before and is signed by a
// credential of the set before it. Returns the verified current key set, or null. A credential
// the service added on its own has no such chain and never appears in the result.
async function replayFromAnchor(recipientId, encPubkey, anchor, items, origin, rpId) {
  let creds = null, head = null, st = null;
  for (const [n, item] of items.entries()) {
    const s = item && item.statement;
    if (!shaped(s, 'hitl.key-statement') || s.recipient_id !== recipientId || s.epoch !== anchor.epoch
        || s.seq !== anchor.seq + n || s.enc_pubkey !== encPubkey || !listedCredentials(s) || parseTs(s.issued_at) === null) return null;
    const hash = await sha256(enc.encode(jcs(s)));
    if (n === 0) {
      if (b64u(hash) !== anchor.hash) return null;
    } else {
      const a = item.assertion || {};
      const signer = creds.find(c => c.id === a.credential_id);
      if (s.prev !== head || !signer || await assertionProblem(a, hash, signer, origin, rpId) !== null) return null;
    }
    creds = s.credentials;
    head = b64u(hash);
    st = s;
  }
  if (!st) return null;
  return { epoch: st.epoch, seq: st.seq, head, credentials: creds, enc_pubkey: encPubkey, fingerprint: fingerprintOf(unb64u(head)) };
}

// The one form of an answer, wherever on the page it is given, and the form the caller library
// accepts: a choice when the schema has choices; fields, as an object, whenever the schema has
// fields and the choice is not cancel, even if none was filled in; a comment only when one was written.
function shapeAnswer(schema, choice, fields, comment) {
  const answer = {};
  if ((schema.choices || []).length || choice === 'cancel') answer.choice = choice;
  if (answer.choice !== 'cancel' && (schema.fields || []).length) answer.fields = fields || {};
  if (schema.comment && typeof comment === 'string' && comment.trim() !== '') {
    answer.comment = comment.trim().slice(0, schema.comment.max_length);
  }
  return answer;
}

const CANCEL = { value: 'cancel', label: 'Cancel' };
function withCancel(choices) {
  return choices.some(c => c.value === 'cancel') ? choices : [...choices, CANCEL];
}

// ---- end: protocol helpers

const app = document.getElementById('app');

class UserError extends Error {}

// The service presented something that contradicts what was sealed or signed. Shown as it is and
// never retried another way: a second gesture would only be asked for the same refusal.
class Refused extends UserError {}

// The request was not sealed to this recipient's key.
class Undecryptable extends UserError {}

const TEXT_LIMIT = 2000;  // a text answer without a max_length of its own; the caller library's limit too

const ANSWER_BLOCK = 4096;

async function api(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new UserError('The service is unreachable. Try again later.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = new UserError(data.error || ('Request failed: HTTP ' + res.status));
    e.status = res.status;  // the service answered; a missing status means it was never reached
    throw e;
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

function show(...nodes) {
  app.replaceChildren(...nodes);
}

function fail(e) {
  console.error(e);
  const msg = e instanceof UserError ? e.message
    : e && e.name === 'NotAllowedError' ? 'The device confirmation was cancelled or timed out. Nothing was sent; press the button again.'
    : 'Something went wrong: ' + (e && e.message ? e.message : e);
  app.append(el('p', { class: 'error', id: 'error' }, msg));
}

function randomBytes(n) {
  return crypto.getRandomValues(new Uint8Array(n));
}

function wrapKey(prf) {
  return sodium.crypto_generichash(32, new Uint8Array([...enc.encode('hitl-wrap-v2'), ...prf]));
}

function assertionJson(a) {
  return {
    credential_id: b64u(a.rawId),
    client_data_json: b64u(a.response.clientDataJSON),
    authenticator_data: b64u(a.response.authenticatorData),
    signature: b64u(a.response.signature),
  };
}

// One authenticator gesture that returns the PRF output of whichever credential was used.
// The challenge may carry meaning (a session delegation); by default it is random.
async function prfGet(creds, challenge) {
  const evalByCredential = {};
  for (const c of creds) evalByCredential[c.id] = { first: unb64u(c.prf_salt) };
  const a = await navigator.credentials.get({ publicKey: {
    challenge: challenge || randomBytes(32),
    rpId: location.hostname,
    userVerification: 'required',
    timeout: 300000,
    allowCredentials: creds.map(c => ({ type: 'public-key', id: unb64u(c.id) })),
    extensions: { prf: { evalByCredential } },
  }});
  const out = a.getClientExtensionResults().prf?.results?.first;
  if (!out) throw new UserError('This authenticator did not return a PRF result, so the key cannot be unlocked.');
  return { id: b64u(a.rawId), prf: new Uint8Array(out), assertion: a };
}

// ---------------------------------------------------------------- device sessions
//
// One device gesture both unlocks the key and signs a session delegation: "session key K acts
// for this recipient until T". K is a non-extractable Ed25519 key made by this browser, kept in
// IndexedDB with the unlocked encryption key (sealed by a non-extractable AES key). While the
// session lives, requests open without a gesture, and answers to requests whose caller policy
// allows sessions are signed by K without one. Callers verify the delegation against the
// device key they pinned, then K's signature, then their own policy (assurance, session age).

const SESSION_CHOICES = [0, 1, 12, 24];

function sessionHours() {
  const h = Number(localStorage.getItem('hitl.sessionHours') ?? '12');
  return SESSION_CHOICES.includes(h) ? h : 12;
}

function idb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open('hitl', 2);
    // Version 2 sessions carry the verified key set; anything older is dropped with the store.
    r.onupgradeneeded = () => {
      if (r.result.objectStoreNames.contains('sessions')) r.result.deleteObjectStore('sessions');
      r.result.createObjectStore('sessions');
    };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.onblocked = () => reject(new Error('the session store is held by another tab'));
  });
}

async function sessionStore(mode, fn) {
  const db = await idb();
  return new Promise((resolve, reject) => {
    const req = fn(db.transaction('sessions', mode).objectStore('sessions'));
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

const sessionGet = (rid) => sessionStore('readonly', s => s.get(rid)).catch(() => null);
const sessionPut = (rid, v) => sessionStore('readwrite', s => s.put(v, rid));
const sessionDelete = (rid) => sessionStore('readwrite', s => s.delete(rid)).catch(() => null);

// An expired session key is deleted whenever a page loads, whichever page it is.
async function sessionPurge() {
  const db = await idb();
  await new Promise((resolve) => {
    const tx = db.transaction('sessions', 'readwrite');
    const cursor = tx.objectStore('sessions').openCursor();
    cursor.onsuccess = () => {
      const c = cursor.result;
      if (!c) return;
      if (!(Date.parse(c.value.delegation.expires_at) > Date.now())) c.delete();
      c.continue();
    };
    tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
  });
}

// A live session for this recipient that the given request may use, or null.
async function usableSession(req, maxAge) {
  const s = await sessionGet(req.recipient.id);
  if (!s) return null;
  const now = Date.now();
  if (Date.parse(s.delegation.expires_at) <= now || s.enc_pubkey !== req.recipient.enc_pubkey
      || !req.credentials.some(c => c.id === s.credential_id)) {
    await sessionDelete(req.recipient.id);
    return null;
  }
  if (maxAge && now - Date.parse(s.delegation.issued_at) > maxAge * 1000) return null;
  return s;
}

async function sessionPrivateKey(s) {
  return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: s.iv }, s.aes, s.wrapped));
}

// What an unlocked page works with: the private key, the public key derived from it (never the one
// the service names) and the key set verified from the device's anchor. A session keeps the anchor,
// which is no secret, and replays the chain each time, so a device added since the unlock is known.
async function sessionKey(s, recipientId) {
  const priv = await sessionPrivateKey(s);
  const keys = await verifiedKeys(recipientId, priv, s.anchor);
  return { credentialId: s.credential_id, priv, pub: unb64u(keys.enc_pubkey), keys };
}

// The key of a live session, or null; a session that no longer verifies is dropped.
async function keyFromSession(s, recipientId) {
  try {
    return await sessionKey(s, recipientId);
  } catch (e) {
    await sessionDelete(recipientId);
    return null;
  }
}

// Device gesture: unlock, and start a session unless sessions are off on this device.
async function unlockWithDevice(req) {
  const hours = sessionHours();
  let pending = null;
  if (hours > 0) {
    try {
      const kp = await crypto.subtle.generateKey({ name: 'Ed25519' }, false, ['sign', 'verify']);
      const pub = new Uint8Array(await crypto.subtle.exportKey('raw', kp.publicKey));
      const now = new Date();
      const delegation = {
        version: 1, type: 'hitl.session', recipient_id: req.recipient.id, session_pubkey: b64u(pub),
        issued_at: rfc3339(now), expires_at: rfc3339(new Date(now.getTime() + hours * 3600 * 1000)),
      };
      pending = { kp, delegation };
    } catch (e) {
      pending = null;  // no Ed25519 in this browser: work without sessions
    }
  }
  const challenge = pending ? await sha256(enc.encode(jcs(pending.delegation))) : undefined;
  const used = await prfGet(req.credentials, challenge);
  const { priv, anchor } = openBlob(req.credentials, used);
  const keys = await verifiedKeys(req.recipient.id, priv, anchor);
  if (pending) {
    const aes = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    const iv = randomBytes(12);
    const wrapped = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aes, priv));
    // Without a place to keep it there is no session; the unlock itself still stands.
    await sessionPut(req.recipient.id, {
      signKey: pending.kp.privateKey, delegation: pending.delegation, assertion: assertionJson(used.assertion),
      credential_id: used.id, aes, iv, wrapped, enc_pubkey: keys.enc_pubkey, anchor,
    }).catch(() => null);
  }
  return { credentialId: used.id, priv, pub: unb64u(keys.enc_pubkey), keys };
}

function sessionSettings(req, keys) {
  const select = el('select', { id: 'session-hours', class: 'inline' },
    ...SESSION_CHOICES.map(h => el('option', { value: String(h) }, h === 0 ? 'off' : h + ' h')));
  select.value = String(sessionHours());
  select.addEventListener('change', () => localStorage.setItem('hitl.sessionHours', select.value));
  const lock = el('button', { class: 'link', id: 'lock-now', onclick: async () => {
    await sessionDelete(req.recipient.id);
    lock.textContent = 'Locked';
  } }, 'Lock now');
  const out = el('div', { class: 'settings' }, el('p', { class: 'note' }, 'Stay unlocked on this device: ', select, ' ', lock));
  // Computed from the chain this device verified from its own anchor, never from a reply of the service.
  if (keys) out.append(el('p', { class: 'note', id: 'key-fingerprint' }, 'Your key fingerprint: ' + keys.fingerprint));
  return out;
}

// The blob a credential's PRF key wraps: the encryption private key and the device's anchor, which
// is the epoch, sequence number and hash of the key statement that enrolled this credential. The
// blob is authenticated, so the service can change neither.
function sealBlob(priv, anchor, nonce, prf) {
  return sodium.crypto_secretbox_easy(enc.encode(jcs({ v: 2, enc_sk: b64u(priv), anchor })), nonce, wrapKey(prf));
}

function openBlob(creds, used) {
  const c = creds.find(x => x.id === used.id);
  if (!c) throw new UserError('Unknown credential.');
  let blob;
  try {
    blob = JSON.parse(dec.decode(sodium.crypto_secretbox_open_easy(unb64u(c.wrapped_key), unb64u(c.wrap_nonce), wrapKey(used.prf))));
  } catch (e) {
    throw new UserError('Could not unlock the encryption key with this device. If it was enrolled before the current version, ask the operator to reset your enrolment.');
  }
  const a = blob.anchor || {};
  if (blob.v !== 2 || !Number.isInteger(a.epoch) || !Number.isInteger(a.seq) || typeof a.hash !== 'string') {
    throw new UserError('This device holds a key in a form this page does not know.');
  }
  return { priv: unb64u(blob.enc_sk), anchor: { epoch: a.epoch, seq: a.seq, hash: a.hash } };
}

// The recipient's current key set as this device can verify it (see replayFromAnchor). The public
// encryption key is derived from the private key just unwrapped, not taken from the service.
async function verifiedKeys(recipientId, priv, anchor) {
  const res = await api('GET', '/api/v1/recipients/' + encodeURIComponent(recipientId) + '/statements?after=' + (anchor.seq - 1));
  const keys = await replayFromAnchor(recipientId, b64u(sodium.crypto_scalarmult_base(priv)), anchor,
    Array.isArray(res.statements) ? res.statements : [], location.origin, location.hostname);
  if (!keys) throw new UserError('The service presented a history of your devices that this device cannot verify. Nothing was unlocked. Tell the operator.');
  return keys;
}

// ---------------------------------------------------------------- enrolment

async function enrolPage(token) {
  const info = await api('GET', '/api/v1/enroll/' + token);
  const label = el('input', { id: 'label', placeholder: 'e.g. Phone, Laptop', maxlength: '80' });
  const adding = !!info.enc_pubkey;
  show(
    el('h1', {}, 'Enrol a device'),
    el('p', {}, 'Recipient: ', el('strong', {}, info.display_name), ' (' + info.recipient_id + ')'),
    el('p', { class: 'note' }, info.via && info.via.startsWith('sso:')
      ? 'Your identity was confirmed by ' + info.via.slice(4) + '.'
      : 'This enrolment link was issued by the operator.'),
    el('p', { class: 'note' }, adding
      ? 'You already have an enrolled device. You will first confirm with an existing device, then create a passkey on the new one.'
      : 'This creates a passkey on this device. Decisions you sign will be verifiable by the programs that ask you.'),
    el('p', { class: 'note' }, 'How this protects you: requests are encrypted to a key that only your device can unlock. The page that does this is served by this service, so it relies on the service delivering unmodified code.'),
    el('label', {}, 'Device name ', label),
    el('button', { id: 'enrol', onclick: () => enrol(token, info, label.value).catch(fail) }, adding ? 'Add this device' : 'Create passkey'),
  );
}

// The devices a key statement will list, shown before the gesture that signs it: the list on the
// screen is the list inside the statement, labels included.
function confirmDevices(credentials, newId, first) {
  return new Promise((resolve) => {
    show(el('h1', {}, first ? 'Confirm your device' : 'Confirm your devices'),
      el('p', {}, 'Signing with ' + (first ? 'this device' : 'your existing device') + ' records exactly this list as your devices:'),
      el('ul', { class: 'devices', id: 'devices' }, ...credentials.map(c => el('li', {}, c.label + (c.id === newId ? ' (new)' : '')))),
      el('p', { class: 'note' }, 'If the list shows a device you do not recognise, stop here and tell the operator.'),
      el('button', { id: 'confirm-devices', onclick: () => resolve() }, 'Sign this list'));
  });
}

async function enrol(token, info, label) {
  await sodium.ready;
  // Cut by characters, not code units, so a label never ends in half of one.
  label = [...label.replace(/[\u0000-\u001f\u007f]/g, '').trim()].slice(0, 80).join('').trim() || 'device';
  let encPriv, verified = null, signerId = null;
  if (info.enc_pubkey) {
    // Adding a device. The existing device unwraps the key and its anchor, replays the statement
    // chain from there, and refuses unless that is exactly the key set the service presents.
    const used = await prfGet(info.credentials);
    const blob = openBlob(info.credentials, used);
    encPriv = blob.priv;
    verified = await verifiedKeys(info.recipient_id, encPriv, blob.anchor);
    const presented = info.credentials.map(c => ({ id: c.id, spki: c.spki, alg: c.alg, label: c.label }));
    if (info.enc_pubkey !== verified.enc_pubkey || jcs(presented) !== jcs(verified.credentials)) {
      throw new UserError('The service lists devices or keys that your existing device never signed for. Nothing was signed. Tell the operator.');
    }
    signerId = used.id;
  } else {
    encPriv = sodium.crypto_box_keypair().privateKey;
  }
  if (!Number.isInteger(info.epoch) || info.epoch < 1) throw new UserError('The service sent an enrolment this page cannot use.');
  const encPub = b64u(sodium.crypto_scalarmult_base(encPriv));
  const salt = randomBytes(32);
  const cred = await navigator.credentials.create({ publicKey: {
    challenge: randomBytes(32),
    rp: { id: location.hostname, name: 'hitl' },
    user: { id: enc.encode(info.recipient_id), name: info.recipient_id, displayName: info.display_name },
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }, { type: 'public-key', alg: -257 }],
    authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
    timeout: 300000,
    excludeCredentials: (verified ? verified.credentials : []).map(c => ({ type: 'public-key', id: unb64u(c.id) })),
    extensions: { prf: { eval: { first: salt } } },
  }});
  const prfExt = cred.getClientExtensionResults().prf;
  if (!prfExt || !(prfExt.enabled || prfExt.results)) {
    throw new UserError('This authenticator does not support the PRF extension, which hitl needs to protect your key. '
      + 'On Android, store passkeys in Google Password Manager (Samsung Pass does not support it). Remove the passkey just created and try again.');
  }
  const credId = b64u(cred.rawId);
  let prf = prfExt.results?.first ? new Uint8Array(prfExt.results.first) : null;
  if (!prf) prf = (await prfGet([{ id: credId, prf_salt: b64u(salt) }])).prf;

  const nonce = sodium.randombytes_buf(sodium.crypto_secretbox_NONCEBYTES);
  const spki = cred.response.getPublicKey();
  if (!spki) throw new UserError('This browser does not expose the credential public key.');
  const alg = cred.response.getPublicKeyAlgorithm();

  // Key statement: the complete key set after this change. A first enrolment lists the new
  // credential and nothing else, whatever the service returned; a later one extends the set the
  // existing device has just verified. Callers and devices accept key changes only along this chain.
  const mine = { id: credId, spki: b64u(spki), alg, label };
  const credentials = [...(verified ? verified.credentials : []), mine].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  const statement = {
    type: 'hitl.key-statement',
    version: 2,
    recipient_id: info.recipient_id,
    epoch: verified ? verified.epoch : info.epoch,
    seq: verified ? verified.seq + 1 : 1,
    prev: verified ? verified.head : '',
    enc_pubkey: encPub,
    credentials,
    issued_at: rfc3339(new Date()),
  };
  const hash = await sha256(enc.encode(jcs(statement)));
  await confirmDevices(credentials, credId, !verified);
  show(el('p', { id: 'status' }, verified ? 'Confirm with your existing device.' : 'Confirm with this device.'));
  const sa = await navigator.credentials.get({ publicKey: {
    challenge: hash,
    rpId: location.hostname,
    userVerification: 'required',
    timeout: 300000,
    allowCredentials: [{ type: 'public-key', id: unb64u(verified ? signerId : credId) }],
  }});

  await api('POST', '/api/v1/enroll/' + token, {
    credential_id: credId,
    spki: b64u(spki),
    alg,
    prf_salt: b64u(salt),
    // The new device's anchor is the statement that enrols it.
    wrapped_key: b64u(sealBlob(encPriv, { epoch: statement.epoch, seq: statement.seq, hash: b64u(hash) }, nonce, prf)),
    wrap_nonce: b64u(nonce),
    enc_pubkey: encPub,
    statement,
    statement_assertion: assertionJson(sa),
  });
  // The fingerprint of the statement this device has just signed; no reply of the service enters it.
  show(
    el('h1', {}, 'Device enrolled'),
    el('p', {}, 'Key fingerprint:'),
    el('p', { class: 'fingerprint', id: 'fingerprint' }, fingerprintOf(hash)),
    el('p', { class: 'note' }, 'When a program starts working with you, its operator will ask you for this fingerprint. Read it from this device, or from any page you have unlocked.'),
  );
}

// ---------------------------------------------------------------- decision

const unsentKey = (id) => 'hitl.unsent.' + id;

// The service operator configures each registered caller's outer chrome. Caller HTML stays
// inside its frame and cannot set these values. Accept only bounded colors and image data.
function applyCallerTheme(caller) {
  const theme = caller.theme && typeof caller.theme === 'object' ? caller.theme : {};
  for (const key of ['background', 'surface', 'ink', 'muted', 'border', 'accent', 'on_accent']) {
    const value = theme[key];
    if (typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value)) {
      document.documentElement.style.setProperty('--hitl-' + key.replace('_', '-'), value);
    }
  }
  return typeof theme.logo === 'string' && theme.logo.length < 90000
    && /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(theme.logo) ? theme.logo : null;
}

// A record this tab built and signed but could not deliver. It is sent again as the same bytes,
// so the service sees one answer however often it arrives. True when it was delivered. It is kept
// while the service cannot be reached or fails, and dropped only when the service refuses it.
async function resendUnsent(requestId) {
  const unsent = sessionStorage.getItem(unsentKey(requestId));
  if (!unsent) return false;
  try {
    await api('POST', '/api/v1/requests/' + requestId + '/response', JSON.parse(unsent));
  } catch (e) {
    if (!(e.status >= 400 && e.status < 500)) throw e;
    sessionStorage.removeItem(unsentKey(requestId));
    return false;
  }
  sessionStorage.removeItem(unsentKey(requestId));
  return true;
}

async function decidePage(short) {
  let req = await api('GET', '/api/v1/requests/' + short);
  // A record this page built and could not deliver is sent again as the same bytes, so the
  // service sees one answer however often it arrives.
  if (req.status === 'pending' && await resendUnsent(req.request_id)) req = await api('GET', '/api/v1/requests/' + short);
  const logo = applyCallerTheme(req.caller);
  // The visible name comes from the service's caller registry. The caller ID is checked against
  // the sealed request facts after unlock and remains available in Technical information.
  const chip = el('div', { class: 'caller', id: 'caller' },
    el('span', { class: 'caller-avatar', 'aria-hidden': 'true' },
      logo ? el('img', { class: 'caller-logo', src: logo, alt: '' }) : req.caller.display_name.slice(0, 1).toUpperCase()),
    el('span', { class: 'caller-copy' },
      el('span', { class: 'caller-label' }, 'Asked by'),
      el('strong', {}, req.caller.display_name)));
  const banner = el('div', { class: 'banner' }, 'This is a decision with consequences, not a message. Read it before you answer.');
  localStorage.setItem('hitl.recipient', req.recipient.id);
  if (req.status === 'signed') return answeredPage(req, chip);
  if (req.status === 'expired') return show(chip, el('p', { id: 'expired' }, 'This request has expired.'), await nextControls(req.recipient.id, req.request_id));
  await sodium.ready;
  // A live session opens the request without a gesture. The policy read here is the service's
  // claim and only decides whether to ask for a gesture now; what may confirm the answer is
  // decided later, from the policy sealed in the manifest.
  const hint = (req.creation_body || {}).policy || {};
  const s = await usableSession(req, hint.max_session_age);
  const key = s ? await keyFromSession(s, req.recipient.id) : null;
  if (key) {
    try {
      return await render(req, chip, banner, key);
    } catch (e) {
      // Only a request this key cannot open falls through to a device unlock. The session stays:
      // anyone registered as a caller can send something undecryptable.
      if (!(e instanceof Undecryptable)) throw e;
    }
  }
  show(chip, banner,
    el('p', {}, 'For ', el('strong', {}, req.recipient.display_name), '.'),
    el('button', { id: 'unlock', onclick: () => unlockWithDevice(req).then(k => render(req, chip, banner, k)).catch(fail) }, 'Unlock with my device'),
    sessionSettings(req));
}

// Decrypts a request and checks everything the service presented about it against what the
// caller sealed (R10). The manifest carries the request facts again, inside the ciphertext: who
// asks, the reference, the policy, the key the answer is sealed to, the lifetime. The page acts
// on that copy and refuses the request on any difference from the creation body the service
// shows, so a service that changes one of them is caught here, not later by the caller.
async function openRequest(req, key, review = false) {
  const refuse = (what) => new Refused('This request does not match what its sender sealed (' + what + '). It is not shown and cannot be answered. Tell the sender.');
  const configCt = unb64u(req.config_ct);
  const payloadCt = unb64u(req.payload_ct);
  let manifestBytes, payloadBytes, manifest, payload;
  try {
    manifestBytes = sodium.crypto_box_seal_open(configCt, key.pub, key.priv);
    payloadBytes = sodium.crypto_box_seal_open(payloadCt, key.pub, key.priv);
    manifest = JSON.parse(dec.decode(manifestBytes));
    payload = JSON.parse(dec.decode(payloadBytes));
  } catch (e) {
    throw new Undecryptable('This request could not be decrypted with your key.');
  }
  const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const body = req.creation_body;
  if (!isObject(manifest) || !isObject(payload) || manifest.version !== 3 || !isObject(manifest.request)
      || !isObject(manifest.answers) || !shaped(body, 'hitl.request')) throw refuse('format');
  const facts = manifest.request;
  if (body.config_ct_hash !== b64u(await sha256(configCt)) || body.payload_ct_hash !== b64u(await sha256(payloadCt))) throw refuse('content');
  // A sealed box does not say who sealed it, and every request fact is public, so the facts alone
  // do not show that this manifest is the caller's. Manifest and payload therefore name each other
  // by values only someone who can open them knows: without this a service could put a manifest of
  // its own, with its own answer key, in front of the caller's attachments.
  if (typeof manifest.salt !== 'string' || manifest.salt.length < 43 || payload.manifest_salt !== manifest.salt
      || manifest.payload_hash !== b64u(await sha256(payloadBytes))) throw refuse('content');
  if (Object.keys(facts).sort().join() !== 'answer_pubkey,caller_id,caller_ref,created_at,policy,request_id,ttl') throw refuse('format');
  for (const k of ['request_id', 'caller_id', 'caller_ref', 'answer_pubkey', 'created_at', 'ttl']) {
    if (facts[k] !== body[k]) throw refuse(k);
  }
  const policy = facts.policy;
  if (policy === null || typeof policy !== 'object' || jcs(policy) !== jcs(body.policy)
      || !['fresh', 'session'].includes(policy.assurance) || typeof policy.review !== 'boolean') throw refuse('policy');
  if (req.request_id !== facts.request_id || req.caller.id !== facts.caller_id || body.recipient_id !== req.recipient.id) throw refuse('addressing');
  const created = parseTs(facts.created_at);
  if (created === null || !Number.isInteger(facts.ttl) || typeof facts.caller_ref !== 'string' || unb64u(facts.answer_pubkey).length !== 32) throw refuse('format');
  const expires = created + facts.ttl;
  if (!review && Date.now() / 1000 > expires) throw new UserError('This request has expired.');
  return { req, key, body, facts, policy, manifest, manifestBytes, payload, payloadBytes, expires,
    creationHash: b64u(await sha256(enc.encode(jcs(body)))) };
}

// Drawn by the service from the sealed facts, outside anything the caller can style.
function factsBox(ctx) {
  const box = el('div', { class: 'facts', id: 'facts' });
  if (ctx.facts.caller_ref) box.append(el('p', { id: 'caller-ref' }, 'Reference: ', el('strong', {}, ctx.facts.caller_ref)));
  box.append(el('p', { class: 'note' }, 'Expires ' + new Date(ctx.expires * 1000).toLocaleString() + '. '
    + (ctx.policy.assurance === 'fresh' ? 'The caller asks for a fresh device confirmation on your answer.'
      : 'An unlocked session on this device can confirm your answer.')));
  return box;
}

// Keep the registered sender details and signed request facts available without making
// them compete with the question. This disclosure lives outside caller HTML.
function technicalDetails(ctx, banner) {
  return el('details', { class: 'technical' },
    el('summary', { 'aria-label': 'Technical information', title: 'Technical information' },
      el('span', { class: 'technical-icon', 'aria-hidden': 'true' }, 'i')),
    el('div', { class: 'technical-content' },
      el('p', {}, 'Registered sender: ', el('strong', {}, ctx.req.caller.display_name)),
      el('p', {}, 'Caller ID: ', el('code', {}, ctx.req.caller.id)),
      el('p', {}, 'Registered domain: ', el('strong', {}, ctx.req.caller.display_domain)),
      banner,
      ctx.manifest.title ? el('p', {}, 'Request: ', el('strong', {}, ctx.manifest.title)) : '',
      factsBox(ctx)));
}

function requestHeader(ctx, chip, banner) {
  return el('div', { class: 'topbar' },
    el('div', { class: 'topbar-inner' }, chip, technicalDetails(ctx, banner)));
}

function closeControl() {
  const note = el('p', { class: 'note', id: 'close-note' });
  const button = el('button', { id: 'close', class: 'cancel', onclick: () => {
    window.close();
    setTimeout(() => { note.textContent = 'Your browser keeps this tab open. You can close it now.'; }, 300);
  } }, 'Close');
  return el('div', { class: 'close-control' }, button, note);
}

function receiptMark() {
  const mark = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [name, value] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) mark.setAttribute(name, value);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M20 6 9 17l-5-5');
  mark.append(path);
  return el('div', { class: 'receipt-mark', 'aria-hidden': 'true' }, mark);
}

// ---------------------------------------------------------------- an answered request
//
// The caller's policy decides whether an answered link can be opened again ("review"). If so, the
// person unlocks it as before and sees the request with their signed answer, read-only. Nothing is
// sent. The answer in the signed record is sealed to the caller; the page shows the copy sealed to
// the recipient's own key, and only after it has verified the record's signature against the key
// set this device verified from its anchor.

async function answeredPage(req, chip) {
  const next = await nextControls(req.recipient.id, req.request_id);
  const present = (reviewAction = '') => {
    document.body.classList.remove('full');
    show(el('section', { class: 'receipt-page', 'aria-labelledby': 'done' },
      el('div', { class: 'receipt' }, receiptMark(),
        el('h1', { id: 'done' }, 'Already answered'),
        el('p', { class: 'receipt-copy' }, 'This decision has already been signed.'),
        reviewAction, next, closeControl())));
  };
  if (!((req.creation_body || {}).policy || {}).review || !req.response) return present();
  await sodium.ready;
  const open = (k) => renderReview(req, chip, k, next);
  const s = await usableSession(req, null);
  const key = s ? await keyFromSession(s, req.recipient.id) : null;
  if (key) {
    try {
      return await open(key);
    } catch (e) {
      if (!(e instanceof Undecryptable)) throw e;
    }
  }
  present(el('button', { id: 'show-answer', onclick: () => unlockWithDevice(req).then(open).catch(fail) }, 'Show the request and my answer'));
}

function answerLines(schema, answer) {
  const lines = [];
  if (answer.choice !== undefined) {
    const c = (schema.choices || []).find(x => x.value === answer.choice);
    lines.push('You chose: ' + (c ? c.label : answer.choice === 'cancel' ? CANCEL.label : answer.choice));
  }
  for (const f of schema.fields || []) {
    const v = (answer.fields || {})[f.name];
    if (v === undefined) continue;
    const label = x => ((f.options || []).find(o => o.value === x) || {}).label || x;
    lines.push(f.label + ': ' + (Array.isArray(v) ? v.map(label).join(', ') : v === true ? 'yes' : v === false ? 'no' : label(v)));
  }
  if (answer.comment) lines.push('Comment: ' + answer.comment);
  return lines;
}

function showAnswerInDesign(d, answer) {
  for (const n of d.querySelectorAll('[data-hitl-choice]')) {
    if (n.getAttribute('data-hitl-choice') === answer.choice) n.classList.add('hitl-selected');
  }
  const fields = answer.fields || {};
  for (const n of d.querySelectorAll('[data-hitl-field]')) {
    const v = fields[n.getAttribute('data-hitl-field')];
    const type = (n.type || '').toLowerCase();
    if (type === 'radio') n.checked = v !== undefined && String(v) === n.value;
    else if (type === 'checkbox') n.checked = Array.isArray(v) ? v.includes(n.value) : v === true;
    else if (v !== undefined && 'value' in n) n.value = Array.isArray(v) ? v.join(', ') : String(v);
    n.disabled = true;
  }
}

// The recipient's own copy of the answer, after the signed record has been verified: it answers
// this request, it matches what was decrypted, and it was signed by a credential of the verified
// key set (directly, or through a session that credential delegated).
async function reviewedAnswer(ctx) {
  const refuse = () => new Refused('The stored answer does not verify against your keys, so it is not shown. Tell the operator.');
  const { record, assertion } = ctx.req.response || {};
  if (!ctx.policy.review || !shaped(record, 'hitl.response', ['answer_self_ct']) || assertion === null || typeof assertion !== 'object') throw refuse();
  if (record.request_id !== ctx.facts.request_id || record.creation_hash !== ctx.creationHash
      || record.config_hash !== b64u(await sha256(ctx.manifestBytes)) || record.payload_hash !== b64u(await sha256(ctx.payloadBytes))) throw refuse();
  const cred = ctx.key.keys.credentials.find(c => c.id === assertion.credential_id);
  if (!cred) throw refuse();
  const recordBytes = enc.encode(jcs(record));
  if (assertion.kind === 'session') {
    const d = assertion.delegation;
    if (!shaped(d, 'hitl.session') || d.recipient_id !== ctx.body.recipient_id
        || await assertionProblem(assertion.delegation_assertion || {}, await sha256(enc.encode(jcs(d))), cred, location.origin, location.hostname) !== null) throw refuse();
    let ok = false;
    try {
      const k = await crypto.subtle.importKey('raw', unb64u(d.session_pubkey), { name: 'Ed25519' }, false, ['verify']);
      ok = await crypto.subtle.verify({ name: 'Ed25519' }, k, unb64u(assertion.signature), recordBytes);
    } catch (e) {
      ok = false;
    }
    if (!ok) throw refuse();
  } else if (await assertionProblem(assertion, await sha256(recordBytes), cred, location.origin, location.hostname) !== null) {
    throw refuse();
  }
  try {
    return JSON.parse(dec.decode(unpad(sodium.crypto_box_seal_open(unb64u(record.answer_self_ct), ctx.key.pub, ctx.key.priv), ANSWER_BLOCK)));
  } catch (e) {
    throw refuse();
  }
}

async function renderReview(req, chip, key, next) {
  const ctx = await openRequest(req, key, true);
  const answer = await reviewedAnswer(ctx);
  const { manifest, payload } = ctx;
  const record = req.response.record;
  const content = [];
  for (const b of blocksOf(manifest)) {
    const node = renderBlock(b, payload);
    if (node.tagName === 'IFRAME' && manifest.layout === 'full') {
      node.addEventListener('load', () => {
        const d = node.contentDocument;
        if (!d) return;
        loadDesignMedia(d, payload);
        showAnswerInDesign(d, answer);
      });
    }
    content.push(node);
  }
  const box = el('div', { class: 'review', id: 'review' },
    el('h2', {}, 'Your answer'),
    ...answerLines(manifest.answers || {}, answer).map(t => el('p', {}, t)),
    el('p', { class: 'note' }, 'Signed ' + new Date(record.signed_at).toLocaleString()
      + '. This page only shows the answer; it cannot be changed.'));
  if (manifest.layout === 'full') {
    document.body.classList.add('full');
    const top = requestHeader(ctx, chip, '');
    return show(top, ...content, el('div', { class: 'extra' }, box, closeControl(), next));
  }
  show(requestHeader(ctx, chip, ''), manifest.title ? el('h1', { id: 'title' }, manifest.title) : '',
    ...content, box, closeControl(), next);
}

// ---------------------------------------------------------------- content blocks

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

function blocksOf(manifest) {
  return Array.isArray(manifest.blocks) ? manifest.blocks : [];
}

// An attachment is either inline in the payload or a separately stored blob: a file the caller
// encrypted in chunks (secretstream) with a key that travels inside this encrypted payload.
// The blob's ciphertext hash is in the payload too, and the payload hash is in the signed
// record, so a swapped blob is refused here and could never be approved.
async function loadFile(payload, ref, progress) {
  const f = payload.files && payload.files[ref];
  if (!f) throw new UserError('The request refers to a missing attachment: ' + ref);
  if (!f.blob) return { ...f, bytes: unb64u(f.data) };
  const label = f.name || ref;
  const res = await fetch('/api/v1/blobs/' + encodeURIComponent(f.blob.id));
  if (!res.ok) throw new UserError('Could not download ' + label + ' (HTTP ' + res.status + ').');
  const reader = res.body.getReader();
  const ct = new Uint8Array(f.blob.size);
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (got + value.length > ct.length) throw new UserError(label + ' is larger than the caller declared.');
    ct.set(value, got);
    got += value.length;
    if (progress) progress(got / ct.length);
  }
  const hex = Array.from(await sha256(ct), b => b.toString(16).padStart(2, '0')).join('');
  if (got !== ct.length || hex !== f.blob.sha256) throw new UserError(label + ' does not match what the caller sent.');
  const S = sodium;
  const state = S.crypto_secretstream_xchacha20poly1305_init_pull(unb64u(f.blob.header), unb64u(f.blob.key));
  const step = f.blob.chunk + S.crypto_secretstream_xchacha20poly1305_ABYTES;
  const out = new Uint8Array(f.size);
  let w = 0;
  let final = false;
  for (let i = 0; i < ct.length; i += step) {
    const r = S.crypto_secretstream_xchacha20poly1305_pull(state, ct.subarray(i, i + step));
    if (!r || final) throw new UserError(label + ' could not be decrypted.');
    out.set(r.message, w);
    w += r.message.length;
    final = r.tag === S.crypto_secretstream_xchacha20poly1305_TAG_FINAL;
  }
  if (!final || w !== f.size) throw new UserError(label + ' is incomplete.');
  return { mime: f.mime, name: f.name, bytes: out };
}

// A placeholder that fills in once the attachment is downloaded and decrypted.
function deferred(b, payload, build) {
  const name = b.name || 'Attachment';
  const status = el('p', { class: 'note' }, name + ', loading...');
  const box = el('div', { class: 'attachment', 'data-ref': b.ref }, status);
  loadFile(payload, b.ref, p => { status.textContent = name + ', ' + Math.round(p * 100) + '%'; })
    .then(f => box.replaceChildren(build(f)))
    .catch(e => box.replaceChildren(el('p', { class: 'error' }, e.message || String(e))));
  return box;
}

// Grows a content frame to its document's height, and keeps it fitted as images load.
function fitHeight(frame) {
  const fit = () => {
    const d = frame.contentDocument;
    if (d && d.documentElement) frame.style.height = (d.documentElement.scrollHeight + 2) + 'px';
  };
  frame.addEventListener('load', () => {
    fit();
    const d = frame.contentDocument;
    if (d && window.ResizeObserver) new ResizeObserver(fit).observe(d.documentElement);
  });
}

const MEDIA_TYPES = {
  video: ['video/mp4', 'video/webm', 'video/ogg', 'video/quicktime'],
  audio: ['audio/mpeg', 'audio/mp4', 'audio/aac', 'audio/ogg', 'audio/webm', 'audio/wav', 'audio/x-wav'],
};

function renderMedia(kind, f, b) {
  if (!MEDIA_TYPES[kind].includes(f.mime)) return renderFile(f, b);
  const m = el(kind, { class: kind, controls: '', preload: 'metadata', 'data-ref': b.ref });
  if (kind === 'video') m.setAttribute('playsinline', '');
  m.src = URL.createObjectURL(new Blob([f.bytes], { type: f.mime }));
  const box = el('div', {}, m, renderFile(f, b));
  return b.caption ? el('figure', {}, box, el('figcaption', {}, b.caption)) : box;
}

// Downloads are served as octet-stream so the browser never renders an attachment in this origin.
function renderFile(f, b) {
  const name = f.name || b.name || 'attachment';
  const a = el('a', { class: 'file', download: name }, 'Download ' + name);
  a.href = URL.createObjectURL(new Blob([f.bytes], { type: 'application/octet-stream' }));
  return el('p', {}, a);
}

let pdfjsPromise = null;
function loadPdfjs() {
  if (!pdfjsPromise) {
    pdfjsPromise = (async () => {
      const cfg = document.body.dataset;
      // The shell preloads this module with its SRI value, so a modified file is refused.
      const lib = await import(cfg.pdfjs);
      // Workers take no integrity attribute: fetch with SRI, then start it from the checked bytes.
      const res = await fetch(cfg.pdfjsWorker, { integrity: cfg.pdfjsWorkerSri });
      const url = URL.createObjectURL(new Blob([await res.blob()], { type: 'text/javascript' }));
      lib.GlobalWorkerOptions.workerPort = new Worker(url, { type: 'module' });
      return lib;
    })();
  }
  return pdfjsPromise;
}

function renderPdf(f, b) {
  const name = f.name || b.name || 'PDF';
  const box = el('div', { class: 'pdf', 'data-ref': b.ref }, el('p', { class: 'note' }, name + ', loading...'));
  (async () => {
    const lib = await loadPdfjs();
    const doc = await lib.getDocument({ data: f.bytes.slice(), isEvalSupported: false }).promise;
    box.replaceChildren(el('p', { class: 'note' }, name + ', ' + doc.numPages + ' page(s)'));
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const base = page.getViewport({ scale: 1 });
      const scale = Math.min(2, (box.clientWidth || 600) / base.width) * (window.devicePixelRatio || 1);
      const viewport = page.getViewport({ scale });
      const canvas = el('canvas', { class: 'pdf-page' });
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      box.append(canvas);
      await page.render({ canvas, canvasContext: canvas.getContext('2d'), viewport }).promise;
    }
    box.dataset.pages = String(doc.numPages);
    box.append(renderFile(f, b));
  })().catch(e => {
    box.replaceChildren(el('p', { class: 'error' }, 'Could not display the PDF: ' + e.message), renderFile(f, b));
  });
  return box;
}

function renderBlock(b, payload) {
  switch (b.type) {
    case 'text':
      return el('p', { class: 'text' }, String(b.text || ''));
    case 'html': {
      // Scripts, forms, popups and navigation of the page are all off (sandbox without allow-scripts);
      // allow-same-origin only lets this page measure the frame to fit its height. The inherited CSP
      // permits the fragment's own CSS but no remote images, fonts or requests.
      const frame = (html) => {
        const f = el('iframe', { sandbox: 'allow-same-origin', class: 'fragment', title: b.title || 'Request content' });
        f.srcdoc = html;
        fitHeight(f);
        return f;
      };
      return b.html !== undefined ? frame(b.html) : deferred(b, payload, f => frame(dec.decode(f.bytes)));
    }
    case 'image':
      return deferred(b, payload, f => {
        if (!IMAGE_TYPES.includes(f.mime)) return renderFile(f, b);
        const img = el('img', { class: 'image', alt: b.alt || f.name || 'image', 'data-ref': b.ref });
        img.src = URL.createObjectURL(new Blob([f.bytes], { type: f.mime }));
        return b.caption ? el('figure', {}, img, el('figcaption', {}, b.caption)) : img;
      });
    case 'pdf':
      return deferred(b, payload, f => renderPdf(f, b));
    case 'video':
    case 'audio':
      return deferred(b, payload, f => renderMedia(b.type, f, b));
    case 'file':
      return deferred(b, payload, f => renderFile(f, b));
    default:
      return el('p', { class: 'note' }, 'This page cannot show content of type "' + b.type + '".');
  }
}

// ---------------------------------------------------------------- dictation
// No voice input: keep call sites unchanged while removing browser speech access.
function dictateButton() { return ''; }

// ---------------------------------------------------------------- answer form

function renderForm(schema) {
  const inputs = {};
  const nodes = (schema.fields || []).map(f => {
    const id = 'field-' + f.name;
    let input;
    switch (f.type) {
      case 'textarea': input = el('textarea', { id, rows: '3' }); break;
      case 'number': input = el('input', { id, type: 'number', step: '1' }); break;
      case 'date': input = el('input', { id, type: 'date' }); break;
      case 'select':
        input = el('select', { id }, el('option', { value: '' }, 'Choose...'),
          ...f.options.map(o => el('option', { value: o.value }, o.label)));
        break;
      case 'multiselect':
        input = el('div', { id, class: 'multi' }, ...f.options.map(o =>
          el('label', { class: 'check' }, el('input', { type: 'checkbox', value: o.value }), ' ' + o.label)));
        break;
      case 'checkbox': input = el('input', { id, type: 'checkbox' }); break;
      default: input = el('input', { id, type: 'text' });
    }
    if (f.type === 'number') {
      if (f.min !== undefined) input.min = String(f.min);
      if (f.max !== undefined) input.max = String(f.max);
    }
    if (['text', 'textarea', undefined].includes(f.type)) input.maxLength = f.max_length || TEXT_LIMIT;
    inputs[f.name] = { f, input };
    const label = f.label + (f.required ? ' *' : '');
    if (f.type === 'checkbox') return el('label', { class: 'check' }, input, ' ' + label);
    return f.type === 'textarea' ? el('label', {}, label, input, dictateButton(input)) : el('label', {}, label, input);
  });
  return { nodes, read: () => readForm(inputs) };
}

function readForm(inputs) {
  const out = {};
  for (const [name, { f, input }] of Object.entries(inputs)) {
    if (f.type === 'checkbox') {
      if (f.required && !input.checked) throw new UserError('Please confirm: ' + f.label);
      out[name] = input.checked;
      continue;
    }
    let v;
    if (f.type === 'multiselect') v = [...input.querySelectorAll('input:checked')].map(i => i.value);
    else if (f.type === 'number') v = input.value === '' ? null : Number(input.value);
    else v = input.value.trim();
    if (v === null || v === '' || (Array.isArray(v) && v.length === 0)) {
      if (f.required) throw new UserError('Please fill in: ' + f.label);
      continue;
    }
    if (f.type === 'number' && !Number.isSafeInteger(v)) throw new UserError(f.label + ' must be a whole number.');
    if (typeof v === 'string' && ['text', 'textarea', undefined].includes(f.type) && [...v].length > (f.max_length || TEXT_LIMIT)) {
      throw new UserError(f.label + ' is too long.');
    }
    if (f.type === 'number' && ((f.min !== undefined && v < f.min) || (f.max !== undefined && v > f.max))) {
      throw new UserError(f.label + ' is out of range.');
    }
    out[name] = v;
  }
  return out;
}

// ---------------------------------------------------------------- unlock and answer

async function render(req, chip, banner, key) {
  const ctx = await openRequest(req, key);
  const { manifest, payload } = ctx;
  const schema = manifest.answers;
  if (manifest.layout === 'full') return renderFull(ctx, chip, banner);

  const content = [];
  if (manifest.title) content.push(el('h1', { id: 'title' }, manifest.title));
  for (const b of blocksOf(manifest)) content.push(renderBlock(b, payload));
  const form = renderForm(schema);
  content.push(...form.nodes);
  let comment = null;
  if (schema.comment) {
    comment = el('textarea', { id: 'comment', maxlength: String(schema.comment.max_length), rows: '3' });
    content.push(el('label', {}, 'Comment (optional)', comment, dictateButton(comment)));
  }
  const choices = withCancel(schema.choices && schema.choices.length ? schema.choices : [{ value: null, label: 'Submit' }]);
  const submit = async (c) => {
    const fields = c.value !== 'cancel' && (schema.fields || []).length ? form.read() : null;
    await sign(ctx, shapeAnswer(schema, c.value, fields, comment && comment.value), c.label);
  };
  const buttons = choices.map(c => el('button', {
    class: c.value === 'cancel' ? 'choice cancel' : 'choice',
    'data-choice': c.value === null ? 'submit' : c.value,
    onclick: () => submit(c).catch(fail),
  }, c.label));
  if (new URLSearchParams(location.search).has('queue')) {
    buttons.push(el('button', { class: 'choice skip', id: 'skip', onclick: () => skipAndGo(req).catch(fail) }, 'Skip for now'));
  }
  show(requestHeader(ctx, chip, banner), ...content,
    el('div', { class: 'choices' }, ...buttons), sessionSettings(req, key.keys));
}

// ---------------------------------------------------------------- designed pages (layout "full")
//
// The caller's HTML fills the page. It answers through ordinary controls marked
// data-hitl-field="<field>" (radio, checkbox, select, text, number, date, textarea) and elements
// marked data-hitl-choice="<value>"; media come from the encrypted attachments via
// src="hitl:<ref>" or poster="hitl:<ref>". The design runs no scripts: this page reads its
// controls. Whatever will be signed is shown in the service's own confirm bar first.

// The same rules the caller applies, so the person sees a problem before signing.
function answerProblem(schema, answer) {
  const choices = (schema.choices || []).map(c => c.value);
  if (answer.choice === 'cancel') return null;
  if (choices.length && !choices.includes(answer.choice)) return 'Choose one of the options.';
  for (const f of schema.fields || []) {
    const v = (answer.fields || {})[f.name];
    const missing = v === undefined || v === '' || (Array.isArray(v) && !v.length) || (f.type === 'checkbox' && v !== true && f.required);
    if (missing) {
      if (f.required) return 'Please fill in: ' + f.label;
      continue;
    }
    const options = (f.options || []).map(o => o.value);
    if (f.type === 'number' && (!Number.isSafeInteger(v) || (f.min !== undefined && v < f.min) || (f.max !== undefined && v > f.max))) return f.label + ' is out of range.';
    if (['text', 'textarea', undefined].includes(f.type) && (typeof v !== 'string' || [...v].length > (f.max_length || TEXT_LIMIT))) return f.label + ' is too long.';
    if (f.type === 'select' && !options.includes(v)) return f.label + ': choose one of the options.';
    if (f.type === 'multiselect' && v.some(x => !options.includes(x))) return f.label + ': unknown option.';
    if (f.type === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(v)) return f.label + ' must be a date.';
  }
  return null;
}

function readDesign(frames, schema, state) {
  const fields = {};
  for (const frame of frames) {
    const d = frame.contentDocument;
    if (!d) continue;
    for (const node of d.querySelectorAll('[data-hitl-field]')) {
      const name = node.getAttribute('data-hitl-field');
      const def = (schema.fields || []).find(x => x.name === name);
      if (!def) continue;
      const type = (node.type || '').toLowerCase();
      if (type === 'radio') {
        if (node.checked) fields[name] = def.type === 'number' ? Number(node.value) : node.value;
      } else if (type === 'checkbox') {
        if (def.type === 'multiselect') {
          fields[name] = fields[name] || [];
          if (node.checked) fields[name].push(node.value);
        } else {
          fields[name] = node.checked;
        }
      } else if (node.tagName === 'SELECT') {
        if (node.multiple) fields[name] = [...node.selectedOptions].map(o => o.value);
        else if (node.value !== '') fields[name] = node.value;
      } else if ('value' in node) {
        const v = String(node.value).trim();
        if (v !== '') fields[name] = def.type === 'number' ? Number(v) : v;
      }
    }
    const radio = d.querySelector('input[data-hitl-choice]:checked');
    if (radio) state.choice = radio.value || radio.getAttribute('data-hitl-choice');
  }
  for (const k of Object.keys(fields)) if (Array.isArray(fields[k]) && !fields[k].length) delete fields[k];
  return fields;
}

function loadDesignMedia(d, payload) {
    for (const node of d.querySelectorAll('[src^="hitl:"], [poster^="hitl:"]')) {
      for (const attr of ['src', 'poster']) {
        const v = node.getAttribute(attr);
        if (!v || !v.startsWith('hitl:')) continue;
        const ref = v.slice(5);
        node.removeAttribute(attr);
        loadFile(payload, ref).then(f => {
          node.setAttribute(attr, URL.createObjectURL(new Blob([f.bytes], { type: f.mime })));
          if (node.tagName === 'SOURCE' && node.parentElement && node.parentElement.load) node.parentElement.load();
        }).catch(() => node.setAttribute('data-hitl-missing', ref));
      }
    }
}

function prepareDesignFrame(frame, payload, onChange, onChoice) {
  frame.addEventListener('load', () => {
    const d = frame.contentDocument;
    if (!d) return;
    loadDesignMedia(d, payload);
    for (const t of d.querySelectorAll('textarea[data-hitl-field]')) {
      const b = dictateButton(t, d);
      if (b) t.insertAdjacentElement('afterend', b);
    }
    d.addEventListener('change', onChange, true);
    d.addEventListener('input', onChange, true);
    d.addEventListener('click', (e) => {
      const t = e.target && e.target.closest ? e.target.closest('[data-hitl-choice]') : null;
      if (t && t.tagName !== 'INPUT') {
        e.preventDefault();
        for (const x of d.querySelectorAll('.hitl-selected')) x.classList.remove('hitl-selected');
        t.classList.add('hitl-selected');
        onChoice(t.getAttribute('data-hitl-choice'));
      }
      onChange();
    }, true);
    onChange();
  });
}

function renderFull(ctx, chip, banner) {
  const { req, key, manifest, payload } = ctx;
  document.body.classList.add('full');
  const schema = manifest.answers;
  const state = { choice: null };
  const choices = schema.choices || [];
  const labelled = withCancel(choices);
  const frames = [];
  const content = [];
  for (const b of blocksOf(manifest)) {
    const node = renderBlock(b, payload);
    if (node.tagName === 'IFRAME') frames.push(node);
    content.push(node);
  }
  const actionIcon = (path) => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [name, value] of Object.entries({ class: 'action-icon', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) svg.setAttribute(name, value);
    const mark = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    mark.setAttribute('d', path);
    svg.append(mark);
    return svg;
  };
  const cancelChoice = labelled.find(c => c.value === 'cancel');
  const cancelText = cancelChoice.label.length <= 28 ? cancelChoice.label : 'Cancel';
  const confirmLabel = el('span', { class: 'action-label' }, 'Confirm');
  const confirm = el('button', { id: 'confirm', disabled: '' },
    actionIcon('M20 6 9 17l-5-5'), confirmLabel);
  const cancel = el('button', { id: 'cancel', class: 'cancel' },
    actionIcon('M18 6 6 18 M6 6l12 12'),
    el('span', { class: 'action-label' }, cancelText));
  cancel.setAttribute('aria-label', cancelChoice.label);
  const fallbackChoices = el('div', { class: 'fallback-choices', role: 'group', 'aria-label': 'Choose an option' });
  const extra = el('div', { class: 'extra' });
  const bar = el('div', { class: 'confirmbar' },
    el('div', { class: 'confirmbar-inner' },
      el('div', { class: 'confirm-actions' }, cancel, confirm)));
  let unbound = null;
  let comment = null;
  let current = null;

  const refresh = () => {
    const fields = readDesign(frames, schema, state);
    if (unbound) Object.assign(fields, unbound.values());
    const answer = shapeAnswer(schema, state.choice, fields, comment && comment.value);
    const choice = labelled.find(c => c.value === state.choice);
    for (const b of fallbackChoices.children) {
      const selected = b.dataset.value === state.choice;
      b.classList.toggle('selected', selected);
      b.setAttribute('aria-pressed', String(selected));
    }
    const problem = answerProblem(schema, answer);
    confirm.disabled = problem !== null;
    confirmLabel.textContent = choice && choice.value !== 'cancel' && choice.label.length <= 28
      ? choice.label : 'Confirm';
    confirm.setAttribute('aria-label', choice && choice.value !== 'cancel'
      ? 'Confirm: ' + choice.label : 'Confirm');
    confirm.title = problem || '';
    current = { answer, label: choice ? choice.label : 'Submitted' };
  };
  const choose = (value) => { state.choice = value; refresh(); };

  for (const f of frames) prepareDesignFrame(f, payload, refresh, choose);
  // Fields and choices the design does not provide are offered by the service.
  const boundFields = new Set();
  let boundChoices = false;
  const scanBindings = () => {
    for (const f of frames) {
      const d = f.contentDocument;
      if (!d) continue;
      for (const n of d.querySelectorAll('[data-hitl-field]')) boundFields.add(n.getAttribute('data-hitl-field'));
      if (d.querySelector('[data-hitl-choice]')) boundChoices = true;
    }
    const missing = (schema.fields || []).filter(f => !boundFields.has(f.name));
    if (missing.length && !unbound) {
      const form = renderForm({ fields: missing });
      unbound = { values: () => { try { return form.read(); } catch (e) { return {}; } } };
      extra.prepend(...form.nodes);
      for (const n of form.nodes) n.addEventListener('input', refresh), n.addEventListener('change', refresh);
    }
    if (choices.length && !boundChoices && !fallbackChoices.children.length) {
      for (const c of choices.filter(c => c.value !== 'cancel')) fallbackChoices.append(el('button', {
        type: 'button', 'data-value': c.value, 'data-choice': c.value,
        'aria-pressed': 'false', onclick: () => choose(c.value),
      }, c.label));
      extra.prepend(fallbackChoices);
    }
    refresh();
  };
  Promise.all(frames.map(f => new Promise(r => f.addEventListener('load', r, { once: true })))).then(scanBindings);
  if (!frames.length) setTimeout(scanBindings, 0);
  if (schema.comment) {
    comment = el('textarea', { id: 'comment', maxlength: String(schema.comment.max_length), rows: '2' });
    comment.addEventListener('input', refresh);
    extra.append(el('label', {}, 'Comment (optional)', comment, dictateButton(comment)));
  }
  extra.append(sessionSettings(req, key.keys));
  confirm.addEventListener('click', () => {
    refresh();
    if (confirm.disabled) return;
    sign(ctx, current.answer, current.label)
      .then(() => { document.body.classList.remove('full'); })
      .catch(fail);
  });
  cancel.addEventListener('click', () => {
    sign(ctx, shapeAnswer(schema, 'cancel', {}, null), cancelChoice.label)
      .then(() => { document.body.classList.remove('full'); })
      .catch(fail);
  });
  setInterval(refresh, 700);  // controls changed without an event (autofill, restored state)
  const top = requestHeader(ctx, chip, banner);
  show(top, ...content, extra, bar);
}

// The response record (R11): every field is something this page computed itself. The answer is
// never in it in clear, nor is a hash of it: it is padded, so its length shows nothing, and
// sealed to the answer key the caller made for this request; with review, a second copy is
// sealed to the recipient's own key.
async function buildRecord(ctx, answer) {
  const padded = pad(enc.encode(jcs(answer)), ANSWER_BLOCK);
  const record = {
    type: 'hitl.response',
    version: 2,
    request_id: ctx.facts.request_id,
    creation_hash: ctx.creationHash,
    config_hash: b64u(await sha256(ctx.manifestBytes)),
    payload_hash: b64u(await sha256(ctx.payloadBytes)),
    answer_ct: b64u(sodium.crypto_box_seal(padded, unb64u(ctx.facts.answer_pubkey))),
    signed_at: rfc3339(new Date()),
  };
  if (ctx.policy.review) record.answer_self_ct = b64u(sodium.crypto_box_seal(padded, ctx.key.pub));
  return record;
}

// Keeps the built message until the service has acknowledged it; a retry resends the same bytes.
async function deliver(ctx, message) {
  const id = ctx.facts.request_id;
  sessionStorage.setItem(unsentKey(id), JSON.stringify(message));
  try {
    await api('POST', '/api/v1/requests/' + id + '/response', message);
  } catch (e) {
    if (e.status >= 400 && e.status < 500) sessionStorage.removeItem(unsentKey(id));
    throw e;
  }
  sessionStorage.removeItem(unsentKey(id));
}

// Sends an answer confirmed by the device session; false when the sealed policy does not allow it.
async function submitWithSession(ctx, record) {
  const s = ctx.policy.assurance === 'session' ? await usableSession(ctx.req, ctx.policy.max_session_age) : null;
  if (!s) return false;
  const sig = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, s.signKey, enc.encode(jcs(record))));
  await deliver(ctx, { record, session: { delegation: s.delegation, assertion: s.assertion, signature: b64u(sig) } });
  return true;
}

async function sign(ctx, answer, label) {
  // An answer already signed in this tab and not yet delivered goes first, as it was signed: a
  // second, different record for the same request would only be refused.
  if (await resendUnsent(ctx.facts.request_id)) {
    return showReceipt(ctx, null, null, true);
  }
  const record = await buildRecord(ctx, answer);
  if (!await submitWithSession(ctx, record)) {
    const a = await navigator.credentials.get({ publicKey: {
      challenge: await sha256(enc.encode(jcs(record))),
      rpId: location.hostname,
      userVerification: 'required',
      timeout: 300000,
      allowCredentials: [{ type: 'public-key', id: unb64u(ctx.key.credentialId) }],
    }});
    await deliver(ctx, { record, assertion: assertionJson(a) });
  }
  await showReceipt(ctx, answer, label);
}

async function showReceipt(ctx, answer, label, recovered = false) {
  const next = await nextControls(ctx.req.recipient.id, ctx.req.request_id);
  document.body.classList.remove('full');
  show(el('section', { class: 'receipt-page', 'aria-labelledby': 'done' },
    el('div', { class: 'receipt' },
      receiptMark(),
      el('h1', { id: 'done' }, recovered ? 'Decision delivered' : 'Decision recorded'),
      el('p', { class: 'receipt-copy' }, recovered
        ? 'Your earlier signed response has now been delivered.'
        : 'Your response was signed and sent.'),
      answer && answer.choice !== undefined
        ? el('div', { class: 'receipt-choice' },
          el('span', {}, 'Your response'), el('strong', {}, label)) : '',
      next, closeControl())));
}

// ---------------------------------------------------------------- queue and inbox
//
// The list of pending requests is released only to a live device session: the page signs
// {type: "hitl.inbox", recipient_id, ts} with the session key and sends the device-signed
// delegation along. The service returns metadata only; titles are decrypted here.

async function inboxFetch(rid) {
  const s = rid ? await sessionGet(rid) : null;
  if (!s || Date.parse(s.delegation.expires_at) <= Date.now()) return null;
  const msg = { type: 'hitl.inbox', recipient_id: rid, ts: rfc3339(new Date()) };
  const sig = new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, s.signKey, enc.encode(jcs(msg))));
  const res = await api('POST', '/api/v1/inbox', { ...msg, delegation: s.delegation, assertion: s.assertion, signature: b64u(sig) });
  return res.requests;
}

const skippedIds = () => JSON.parse(sessionStorage.getItem('hitl.skipped') || '[]');

async function nextControls(rid, currentId) {
  const box = el('div', { class: 'queue', id: 'queue' });
  const list = await inboxFetch(rid).catch(() => null);
  if (!list) return box;
  const pending = list.filter(r => r.request_id !== currentId);
  const ahead = pending.filter(r => !skippedIds().includes(r.request_id));
  if (ahead.length) {
    box.append(el('button', { id: 'next', onclick: () => { location.href = '/r/' + ahead[0].short + '?queue=1'; } },
      el('span', { class: 'queue-next-copy' },
        el('span', { class: 'queue-next-label' }, 'Next decision · ' + ahead.length + ' left'),
        el('strong', {}, ahead[0].caller.display_name)),
      el('span', { class: 'queue-arrow', 'aria-hidden': 'true' }, '›')));
  } else if (pending.length) {
    box.append(el('button', { id: 'back-to-skipped', onclick: () => {
      sessionStorage.removeItem('hitl.skipped');
      location.href = '/r/' + pending[0].short + '?queue=1';
    } }, el('span', { class: 'queue-next-copy' },
      el('span', { class: 'queue-next-label' }, pending.length + ' skipped'),
      el('strong', {}, 'Return to skipped decisions')),
    el('span', { class: 'queue-arrow', 'aria-hidden': 'true' }, '›')));
  } else {
    box.append(el('p', { id: 'all-done' }, 'You are all caught up.'));
  }
  if (pending.length) box.append(el('p', {}, el('a', { href: '/inbox', id: 'show-all' }, 'Show all pending requests')));
  return box;
}

async function skipAndGo(req) {
  sessionStorage.setItem('hitl.skipped', JSON.stringify([...new Set([...skippedIds(), req.request_id])]));
  const controls = await nextControls(req.recipient.id, req.request_id);
  const go = controls.querySelector('#next, #back-to-skipped');
  if (go) go.click();
  else show(controls);
}

// A short label for a request: its title, else the start of its first text.
function titleOf(manifest) {
  if (manifest.title) return manifest.title;
  const text = (blocksOf(manifest).find(b => b.type === 'text') || {}).text || '';
  return text ? (text.length > 80 ? text.slice(0, 80) + '...' : text) : '(no title)';
}

async function inboxPage() {
  await sodium.ready;
  const rid = localStorage.getItem('hitl.recipient');
  const heading = el('h1', {}, 'Pending requests');
  if (!rid) return show(heading, el('p', {}, 'Open any decision link first; this device then remembers who you are.'));
  const list = await inboxFetch(rid).catch(() => null);
  if (!list) {
    const info = await api('GET', '/api/v1/recipients/' + encodeURIComponent(rid) + '/unlock');
    const keyReq = { recipient: { id: rid, enc_pubkey: info.enc_pubkey }, credentials: info.credentials };
    return show(heading,
      el('p', { class: 'note' }, sessionHours() ? 'Unlock to see what is waiting for you.' : 'Turn on "Stay unlocked" below to use this page.'),
      el('button', { id: 'unlock', onclick: () => unlockWithDevice(keyReq).then(() => inboxPage()).catch(fail) }, 'Unlock with my device'),
      sessionSettings(keyReq));
  }
  if (!list.length) return show(heading, el('p', { id: 'empty' }, 'Nothing is waiting for you.'));
  const key = await sessionKey(await sessionGet(rid), rid);
  const rows = [];
  for (const item of list) {
    // Each request is opened with the same checks as on its own page; one that fails them is
    // listed as unreadable and cannot be answered from here.
    try {
      const ctx = await openRequest(await api('GET', '/api/v1/requests/' + item.short), key);
      rows.push({ item, ctx, manifest: ctx.manifest });
    } catch (e) {
      rows.push({ item, ctx: null, manifest: null });
    }
  }
  // Bulk answers only where one button press is a complete answer and a session may confirm it.
  const bulkable = r => r.ctx && r.ctx.policy.assurance === 'session'
    && !(r.manifest.answers.fields || []).some(f => f.required) && (r.manifest.answers.choices || []).length > 0;
  const selected = new Set();
  const bulk = el('div', { class: 'bulk', id: 'bulk' });
  const refreshBulk = () => {
    const chosen = rows.filter(r => selected.has(r.item.request_id));
    bulk.replaceChildren();
    if (!chosen.length) return;
    const key = c => c.value + '\u0000' + c.label;
    let common = chosen[0].manifest.answers.choices.map(key);
    for (const r of chosen.slice(1)) common = common.filter(k => r.manifest.answers.choices.map(key).includes(k));
    if (!common.length) {
      bulk.append(el('p', { class: 'note' }, 'The selected requests have no answer in common; answer them one by one.'));
      return;
    }
    for (const k of common) {
      const [value, label] = k.split('\u0000');
      bulk.append(el('button', { class: value === 'cancel' ? 'choice cancel' : 'choice', 'data-bulk': value, onclick: async () => {
        const failed = [];
        for (const r of chosen) {
          const record = await buildRecord(r.ctx, shapeAnswer(r.manifest.answers, value, {}, null));
          if (!await submitWithSession(r.ctx, record).catch(() => false)) failed.push(titleOf(r.manifest));
        }
        if (failed.length) alert('Not sent (the session is too old for these; open them one by one): ' + failed.join(', '));
        await inboxPage();
      } }, label + ' (' + chosen.length + ' selected)'));
    }
  };
  const items = rows.map(r => {
    const box = el('input', { type: 'checkbox', 'data-request': r.item.request_id });
    if (bulkable(r)) {
      box.addEventListener('change', () => { box.checked ? selected.add(r.item.request_id) : selected.delete(r.item.request_id); refreshBulk(); });
    } else {
      box.disabled = true;
    }
    const policy = r.ctx && r.ctx.policy.assurance === 'session' ? 'session' : 'fresh confirmation';
    return el('div', { class: 'inbox-row' }, box,
      el('div', {},
        el('a', { href: '/r/' + r.item.short + '?queue=1' }, r.manifest ? titleOf(r.manifest) : '(cannot be opened here)'),
        el('p', { class: 'note' }, r.item.caller.display_name + ' . expires ' + new Date(r.item.expires_at).toLocaleString() + ' . ' + policy)));
  });
  show(el('h1', {}, 'Pending requests (' + rows.length + ')'),
    el('button', { id: 'start', onclick: () => { sessionStorage.removeItem('hitl.skipped'); location.href = '/r/' + rows[0].item.short + '?queue=1'; } }, 'Answer one by one'),
    el('div', { class: 'inbox', id: 'inbox' }, ...items), bulk,
    el('p', { class: 'note' }, 'Tick requests to answer them together; requests that need a fresh confirmation or a form are answered one by one.'),
    sessionSettings({ recipient: { id: rid } }, key.keys));
}

// ---------------------------------------------------------------- start

(async () => {
  if (!window.PublicKeyCredential) throw new UserError('This browser does not support passkeys.');
  const { page, key } = document.body.dataset;
  await sessionPurge().catch(() => null);
  if (page === 'enrol') await enrolPage(key);
  else if (page === 'enrol-sso') await enrolPage((await api('GET', '/api/v1/enroll-sso')).token);
  else if (page === 'decide') await decidePage(key);
  else if (page === 'inbox') await inboxPage();
})().catch(e => { document.getElementById('status')?.remove(); fail(e); });
