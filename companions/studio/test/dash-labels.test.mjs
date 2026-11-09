// Dashboard labels must not name a particular operator or deployment domain.
//
// The defect: dshMailName() recognised an agent by a regex pinned to ONE mail
// domain and decided who is "You" from literals. On any node but the one it was
// written on, the owner was never "You" and every agent mailbox fell through to
// "A customer" -- the exact label the function exists to avoid. It shipped in
// release 1.10.0 and was fixed in the component repository eleven days earlier
// without the fix ever reaching the installer.
//
// WHY THIS TEST LOOKS ODD. public/next2/js/app.js is browser code with no module
// boundary, so the two functions are extracted from the source and evaluated
// with stubs. If either can no longer be found, this FAILS rather than skips: a
// rename that silently stopped testing them is the outcome worth catching.
//
// The case that matters most is "somebody else's node". Every assertion below
// passes trivially on a node whose owner and agents happen to match the source,
// which is why the second block uses a different owner, a different domain and
// a different roster.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'public', 'next2', 'js', 'app.js'), 'utf8');

function extract(name) {
  const m = src.match(new RegExp(`^function ${name}\\([\\s\\S]*?^}`, 'm'));
  assert.ok(m, `${name}() not found in app.js -- if it was renamed, this test must be renamed with it, not left passing`);
  return m[0];
}

// Rebuilt per call so one case cannot leak identity into the next. `contacts`
// is the handle -> display-name map the server resolves at boot (/api/directory);
// {} is the normal state of a node whose owner has entered no contacts yet.
function labeller({ user, mailDomain, agents, contacts = {} }) {
  const scope = {
    meIdentity: { user, mailDomain },
    agentsList: agents.map((slug) => ({ slug })),
  };
  const body = `
    let meIdentity = ${JSON.stringify(scope.meIdentity)};
    let agentsList = ${JSON.stringify(scope.agentsList)};
    let contactsByHandle = ${JSON.stringify(contacts)};
    const agentBySlug = (s) => agentsList.find((a) => a.slug === s);
    const dshPretty = (s) => String(s).charAt(0).toUpperCase() + String(s).slice(1);
    ${extract('dshContactName')}
    ${extract('dshIsOwner')}
    ${extract('dshMailName')}
    return dshMailName;
  `;
  return new Function(body)();
}

// The same for dshDisplayName, which labels the non-mail channels.
function displayNamer({ contacts = {} } = {}) {
  const body = `
    let contactsByHandle = ${JSON.stringify(contacts)};
    const dshPretty = (s) => String(s).charAt(0).toUpperCase() + String(s).slice(1);
    ${extract('dshContactName')}
    ${extract('dshDisplayName')}
    return dshDisplayName;
  `;
  return new Function(body)();
}

test('on our own node the owner is You and an agent is an agent', () => {
  const name = labeller({ user: 'admin@acme.test', mailDomain: 'acme.test', agents: ['foreman', 'alex'] });
  assert.equal(name('owner'), 'You', "the node's own convention for the human who owns it");
  assert.equal(name('admin@acme.test'), 'You', 'whoever is signed in');
  assert.equal(name('foreman@acme.test'), 'Foreman');
});

test('the domain is a guard: a stranger sharing a slug is not our agent', () => {
  // The pair that gives the previous test its meaning. Without it, "everything
  // is a customer" would satisfy a denial-only test -- which is precisely how
  // the broken version passes a check like this.
  const name = labeller({ user: 'admin@acme.test', mailDomain: 'acme.test', agents: ['foreman'] });
  assert.equal(name('foreman@acme.test'), 'Foreman', 'ours');
  assert.equal(name('foreman@example.org'), 'foreman@example.org', 'not ours, same slug');
  assert.equal(name('someone@elsewhere.org'), 'someone@elsewhere.org');
});

test("on somebody else's node the labels are still right", () => {
  // The whole point. Nothing here resembles the machine this code was written on.
  const name = labeller({ user: 'maria@other.example', mailDomain: 'other.example', agents: ['bookkeeper'] });
  assert.equal(name('maria@other.example'), 'You');
  assert.equal(name('bookkeeper@other.example'), 'Bookkeeper');
  assert.equal(name('foreman@smart-node.app'), 'foreman@smart-node.app',
    'the domain that used to be hardcoded must carry no special meaning');
});

// --------------------------------------------------------------------------
// the panel says WHO, not "A customer".
//
// The mask arrived with the vendored Studio snapshot and was never a decision
// of this project. Studio is served behind smart-node-auth, so the only viewer
// is someone already signed in -- masking hid the owner's own correspondence
// from the owner, and made a client, his own second address, a supplier and a
// bounce render identically. Inside the product: a known contact by name, an
// unknown one as the address it actually is. The generic words stay on the
// public landing panel, where the viewer really is a stranger.

test('a known contact renders by name, on every channel', () => {
  const contacts = { 'buyer@elsewhere.org': 'Jane Buyer', '@shopper': 'Ann Shopper' };
  const name = labeller({ user: 'admin@acme.test', mailDomain: 'acme.test', agents: ['foreman'], contacts });
  assert.equal(name('buyer@elsewhere.org'), 'Jane Buyer');
  assert.equal(name('BUYER@Elsewhere.ORG'), 'Jane Buyer', 'the registry is matched case-insensitively');

  const disp = displayNamer({ contacts });
  assert.equal(disp('buyer@elsewhere.org', 'email'), 'Jane Buyer');
  assert.equal(disp('@shopper', 'telegram'), 'Ann Shopper', 'a handle is a handle, not only an address');
});

test('an unknown correspondent renders as the address, never as a word that fits everybody', () => {
  // The defect this task exists for: two different people must not read alike.
  const name = labeller({ user: 'admin@acme.test', mailDomain: 'acme.test', agents: ['foreman'],
                          contacts: { 'buyer@elsewhere.org': 'Jane Buyer' } });
  assert.notEqual(name('stranger@elsewhere.org'), name('other@elsewhere.org'),
    'two unknown correspondents must be distinguishable');
  assert.equal(name('stranger@elsewhere.org'), 'stranger@elsewhere.org');

  const disp = displayNamer({});
  assert.equal(disp('stranger@elsewhere.org', 'email'), 'stranger@elsewhere.org');
  assert.equal(disp('@someone', 'telegram'), '@someone');
});

test('the owner and this node\'s agents still win over the contacts registry', () => {
  // A contact row naming the owner (his own address is a legitimate contact)
  // must not turn "You" into a third-person name, and an agent stays an agent.
  const name = labeller({
    user: 'admin@acme.test', mailDomain: 'acme.test', agents: ['foreman'],
    contacts: { 'admin@acme.test': 'The Owner', 'foreman@acme.test': 'Some Contact' },
  });
  assert.equal(name('admin@acme.test'), 'You');
  assert.equal(name('foreman@acme.test'), 'Foreman');
});

test('with no contacts resolved the panel still works', () => {
  // core-api down, or a node whose owner has entered nobody. Labels lose names,
  // never the feed -- and an address is still more than the mask ever said.
  const name = labeller({ user: 'admin@acme.test', mailDomain: 'acme.test', agents: ['foreman'], contacts: {} });
  assert.equal(name('someone@elsewhere.org'), 'someone@elsewhere.org');

  const disp = displayNamer({});
  assert.equal(disp('', 'web-chat'), 'Website visitor', 'no address at all is the one case a generic label is true');
  assert.equal(disp('', 'telegram'), 'Telegram user');
  assert.equal(disp('', 'email'), 'Someone');
});

test('an unset mail domain is valid and labels by slug alone', () => {
  const name = labeller({ user: 'admin', mailDomain: null, agents: ['foreman'] });
  assert.equal(name('foreman@anything.test'), 'Foreman',
    'claiming no domain is better than claiming the wrong one');
});

test('no operator identity survives anywhere in the shipped source', () => {
  // The sweep that closed the duplicate desk labels in the first place, kept as a test so the next
  // literal is caught at `npm test` rather than by a re-audit a year later.
  //
  // MATCHED AS PATTERNS, NOT SUBSTRINGS, and that is not fussiness: the original
  // offender lived inside a regex as `smart-node\.app`, so a plain
  // src.includes('smart-node.app') does not see it. Written that way first, this
  // test passed against the very code it exists to reject.
  const mailLabelSource = extract('dshMailName');
  const operatorName = /s\s*===\s*['"](?!owner['"])[a-z][a-z0-9_-]*['"]/i;
  const operatorHandle = /s\.includes\(\s*['"][a-z][a-z0-9_]{5,31}['"]\s*\)/i;
  assert.equal(operatorName.test("if (s === 'sample_operator') return 'You';"), true,
    'the operator-name guard must reject a planted synthetic name');
  assert.equal(operatorHandle.test("if (s.includes('sample_operator')) return 'You';"), true,
    'the operator-handle guard must reject a planted synthetic handle');

  const banned = [
    [src, /smart-node\\?\.app/, 'the mail domain this file was written on'],
    [mailLabelSource, operatorName, 'a hard-coded operator name'],
    [mailLabelSource, operatorHandle, 'a hard-coded operator handle'],
    [src, /===\s*['"]alex['"]/, 'an agent name used as a rule'],
  ];
  for (const [subject, pattern, what] of banned) {
    assert.equal(pattern.test(subject), false, `app.js still contains ${what} (${pattern})`);
  }
});
