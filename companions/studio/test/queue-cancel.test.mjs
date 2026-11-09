import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'public/next2/js/app.js'), 'utf8');
const source = src.match(/^async function cancelQueuedTurn\([\s\S]*?^}/m)?.[0];
const requestSource = src.match(/^async function requestQueuedCancellation\([\s\S]*?^}/m)?.[0];
const repaintSource = src.match(/^function paintQueuedHistoryControls\([\s\S]*?^}/m)?.[0];
const removeSource = src.match(/^function removeCancelledTurn\([\s\S]*?^}/m)?.[0];
const replySource = src.match(/^function replyFor\([\s\S]*?^}/m)?.[0];
assert.ok(source, 'queue cancellation handler exists');
assert.ok(requestSource, 'gateway cancellation request exists');
assert.ok(repaintSource, 'reloaded queue controls exist');
assert.ok(removeSource && replySource, 'cancelled turn can update reply matching');

test('cancelling the first identical message keeps the second reply findable', () => {
  const first = { text: 'same text', base: 0 };
  const second = { text: 'same text', base: 1 };
  const turns = [first, second];
  const owned = { 'desk-a': turns };
  const { removeCancelledTurn, replyFor } = new Function(
    'deskTurns', '_turns', '_histLen', 'activeId', 'paintChatControls', 'paintDeskWork',
    `${removeSource}\n${replySource}\nreturn { removeCancelledTurn, replyFor };`,
  )(() => turns, owned, {}, 'other-desk', () => {}, () => {});
  const stored = [
    { role: 'user', content: 'same text' },
    { role: 'assistant', content: 'second answer' },
  ];
  assert.equal(replyFor(stored, second), null);
  removeCancelledTurn('desk-a', first);
  assert.equal(second.base, 0);
  assert.equal(replyFor(stored, second)?.content, 'second answer');
});

test('queued message waits for gateway confirmation before disappearing', async () => {
  const statuses = ['pending', 'cancelled'];
  const calls = [];
  const fetch = async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return { ok: true, json: async () => ({ status: statuses.shift() }) };
  };
  let removed = 0;
  const cancel = new Function('fetch', '_sleep', 'removeCancelledTurn', 'paintTurn',
    `${requestSource}\nreturn (${source});`)(fetch, async () => {}, () => { removed++; }, () => {});
  const turn = { state: 'queued', clientTurnId: '123e4567-e89b-42d3-a456-426614174000' };
  await cancel('desk-a', turn);
  assert.equal(removed, 1);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.client_turn_id, turn.clientTurnId);
});

test('a queued turn already started is kept and can be stopped', async () => {
  let removed = 0;
  const cancel = new Function('fetch', '_sleep', 'removeCancelledTurn', 'paintTurn',
    `${requestSource}\nreturn (${source});`)(async () => ({ ok: true, json: async () => ({ status: 'too_late' }) }),
    async () => {}, () => { removed++; }, () => {});
  const turn = { state: 'queued', clientTurnId: '123e4567-e89b-42d3-a456-426614174000' };
  await cancel('desk-a', turn);
  assert.equal(turn.state, 'running');
  assert.equal(removed, 0);
});

test('a browser reload restores cancellation on the exact stored message', () => {
  const work = { 'desk-a': { queued_turns: [
    { message_id: 42, client_turn_id: 'id-42' },
  ] } };
  const row = { dataset: { messageId: '42' }, button: null,
    querySelector() { return this.button; },
    appendChild(button) { this.button = button; button.remove = () => { this.button = null; }; },
  };
  const document = { createElement: () => ({
    addEventListener(_event, handler) { this.click = handler; },
  }) };
  const thread = { querySelectorAll: () => [row] };
  const called = [];
  const repaint = new Function('_deskWork', 'activeId', 'deskTurns', 'thread', 'document',
    'cancelQueuedHistoryMessage', `return (${repaintSource});`)(
    work, 'desk-a', () => [], thread, document,
    (...args) => called.push(args));
  repaint('desk-a');
  assert.equal(row.button.textContent, 'Cancel queued');
  row.button.click();
  assert.deepEqual(called[0].slice(0, 2), ['desk-a', 'id-42']);
  work['desk-a'].queued_turns = [];
  repaint('desk-a');
  assert.equal(row.button, null);
});
