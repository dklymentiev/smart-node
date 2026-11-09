// The task card in the Studio panel routes its gate on one function:
// tdReviewGate(t, comments, viewer). Three gates land through it -- owner
// approval on awaiting_approval, reviewer close on pending_review, and the
// terminal "sent" / "decided" states -- and until a reviewer gate existed at
// all, a task in pending_review with reviewer=admin sat on the panel with no
// buttons of any kind. The owner could not close it into done from the UI, so
// a broken task gate blocked 1.12.45 close-out until someone shelled a curl at the planner.
// This test locks the four assertions that keep that regression bought.
//
// The routing is done in a pure function on purpose: the render is DOM code
// with no seams, so a UI test would have to spin a browser to catch a mistake
// this small. Extracting tdReviewGate and tdViewerMatches lets a rename take
// the test with it -- if either is renamed and this file is not, the extractor
// FAILS rather than passing over an empty regex, which is the point of the
// dash-labels test file's pattern too.
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

function gate() {
  const body = `
    ${extract('tkStage')}
    ${extract('tdViewerMatches')}
    ${extract('tdReviewGate')}
    return tdReviewGate;
  `;
  return new Function(body)();
}

const REVIEWER = 'admin@acme.test';
const OTHER = 'architect@acme.test';

test('pending_review with the viewer named as reviewer shows the reviewer gate', () => {
  const g = gate();
  const r = g({ status: 'pending_review', reviewer: 'admin', assignee: 'architect' }, [], REVIEWER);
  assert.equal(r.kind, 'reviewerClose', 'reviewer=admin, viewer admin@... on pending_review => close/send-back gate');
});

test('pending_review with a different reviewer shows NO gate to the viewer', () => {
  const g = gate();
  const r = g({ status: 'pending_review', reviewer: 'architect', assignee: 'agent-b' }, [], REVIEWER);
  assert.equal(r.kind, 'none', 'admin looking at a task whose reviewer is architect must not see the close button');
});

test('pending_review with an unset reviewer shows NO gate (an unset field is not a match)', () => {
  const g = gate();
  const r = g({ status: 'pending_review', reviewer: null, assignee: 'agent-b' }, [], REVIEWER);
  assert.equal(r.kind, 'none', 'a card whose reviewer is unset must not grant its buttons to whoever is looking');
});

test('awaiting_approval with no draft comment shows the owner status-approval gate', () => {
  const g = gate();
  const r = g({ status: 'awaiting_approval', reviewer: 'admin', assignee: 'architect' }, [], REVIEWER);
  assert.equal(r.kind, 'statusApproval', 'the existing awaiting_approval gate must keep working after the refactor');
});

test('a draft comment on any task shows the owner draft-approval gate', () => {
  const g = gate();
  const r = g({ status: 'in_progress', assignee: 'agent-b' }, [{ comment_type: 'draft', content: 'hi' }], REVIEWER);
  assert.equal(r.kind, 'draftApproval');
});

test('once a sent comment exists, the gate is terminal', () => {
  const g = gate();
  const r = g({ status: 'in_progress', assignee: 'agent-b' }, [{ comment_type: 'draft', content: 'hi' }, { comment_type: 'sent', content: 'ok' }], REVIEWER);
  assert.equal(r.kind, 'sent');
});

test('a decided approval reads as decided, rejected or not', () => {
  const g = gate();
  const dec = g({ status: 'awaiting_approval' }, [{ comment_type: 'approval', content: 'approve' }], REVIEWER);
  assert.equal(dec.kind, 'decided');
  assert.equal(dec.rejected, false);
  const rej = g({ status: 'awaiting_approval' }, [{ comment_type: 'approval', content: 'reject: too pricey' }], REVIEWER);
  assert.equal(rej.kind, 'decided');
  assert.equal(rej.rejected, true);
});

test('a non-review status with no relevant comments shows no gate', () => {
  const g = gate();
  const r = g({ status: 'in_progress', assignee: 'agent-b', reviewer: 'admin' }, [], REVIEWER);
  assert.equal(r.kind, 'none');
});

test('the reviewer-gate template exposes a Close and a Send-back button', () => {
  // The pure function above says WHETHER the reviewer gate fires; the render
  // still has to draw the buttons a click landing depends on. A rename that
  // drops #tdClose or #tdSendBack would let tdReviewGate keep passing while
  // the panel silently ships without the button, which is the exact shape of
  // the regression this task exists to close. The wiring below hooks
  // the same ids, so the source-level pin is where "you can press it" lives.
  assert.match(src, /id="tdClose"/, '#tdClose must be present in the reviewer-gate template');
  assert.match(src, /id="tdSendBack"/, '#tdSendBack must be present in the reviewer-gate template');
  // The click bindings translate a press into a decision string that gateDecide
  // then routes on. Both must be present, or the button ships and does nothing.
  assert.match(src, /_ask\('close'/, "the Close button must dispatch _ask('close', ...)");
  assert.match(src, /_ask\('send_back'/, "the Send-back button must dispatch _ask('send_back', ...)");
  // And gateDecide must know how to translate those decisions into a PATCH.
  // The two-status mapping lives on one line in gateDecide: close -> done, else
  // (send_back) -> in_progress. Match on that shape rather than assembling one
  // regex per branch, because the branch order is one edit away from moving.
  assert.match(src, /'close'\s*\|\|\s*decision === 'send_back'/, "gateDecide must handle both 'close' and 'send_back'");
  assert.match(src, /decision === 'close'\s*\?\s*'done'\s*:\s*'in_progress'/, "gateDecide must map close->done, send_back->in_progress");
  assert.match(src, /method:\s*'PATCH'/, 'the close/send_back path must issue a PATCH, not a POST');
});

test('the viewer match is by local-part on both sides, not by full address', () => {
  const body = `
    ${extract('tdViewerMatches')}
    return tdViewerMatches;
  `;
  const m = new Function(body)();
  assert.equal(m('admin@acme.test', 'admin'), true, 'address vs slug');
  assert.equal(m('admin', 'admin@acme.test'), true, 'slug vs address');
  assert.equal(m('Admin@Acme.Test', 'admin'), true, 'case-insensitive');
  assert.equal(m('admin@acme.test', 'architect'), false);
  assert.equal(m('', 'admin'), false, 'empty viewer is not a match');
  assert.equal(m('admin', ''), false, 'empty actor is not a match');
});
