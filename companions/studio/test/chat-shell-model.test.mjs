import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// chat-shell-model.js is a classic browser script: run it and take what it
// defines on window, as the page does.
const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'public/next2/js/chat-shell-model.js'), 'utf8');
// In this realm, so its arrays compare with deepStrictEqual.
globalThis.window = {};
vm.runInThisContext(src);
const M = globalThis.window.SnChatShell;
delete globalThis.window;

test('a relayed answer names its author; anything else names nobody', () => {
  assert.equal(M.relayAuthor('Answer from John:\n\n6'), 'john');
  assert.equal(M.relayAuthor('  Answer from ops.desk: done'), 'ops.desk');
  assert.equal(M.relayAuthor('The answer from john: no'), null);
  assert.equal(M.relayAuthor(''), null);
  assert.equal(M.relayAuthor(null), null);
});

test('members: the chat agent first, colleagues once each, strangers left out', () => {
  const agents = new Set(['foreman', 'john', 'ann', 'ops']);
  assert.deepEqual(M.chatMembers('foreman', ['john', 'ann', 'John', 'bob', 'foreman', 'ops'], (s) => agents.has(s)),
    ['foreman', 'john', 'ann', 'ops']);
  assert.deepEqual(M.chatMembers('foreman', [], (s) => agents.has(s)), ['foreman']);
  assert.deepEqual(M.chatMembers(null, ['ann'], (s) => agents.has(s)), ['ann']);
  assert.deepEqual(M.chatMembers('dev', ['ann']), ['dev', 'ann']);
});

test('the list filter: query over name, title and last message; working; unread', () => {
  const rows = [
    { name: 'Dev', title: 'Flint Tetris build', last: 'Built and copied', working: true, unread: 2 },
    { name: 'Ops', title: 'Gateway keys', last: 'rotating the key', working: false, unread: 0 },
    { name: 'Ann', title: 'Q3 sales', last: 'IL and CA done', working: false, unread: 1 },
  ];
  assert.deepEqual(M.filterRows(rows, {}).map((r) => r.name), ['Dev', 'Ops', 'Ann']);
  assert.deepEqual(M.filterRows(rows, { query: 'tetris' }).map((r) => r.name), ['Dev']);
  assert.deepEqual(M.filterRows(rows, { query: '  KEY ' }).map((r) => r.name), ['Ops']);
  assert.deepEqual(M.filterRows(rows, { query: 'ann' }).map((r) => r.name), ['Ann']);
  assert.deepEqual(M.filterRows(rows, { filter: 'working' }).map((r) => r.name), ['Dev']);
  assert.deepEqual(M.filterRows(rows, { filter: 'unread' }).map((r) => r.name), ['Dev', 'Ann']);
  assert.deepEqual(M.filterRows(rows, { filter: 'working', query: 'ops' }), []);
  assert.deepEqual(M.rowCounts(rows), { all: 3, working: 1, unread: 2 });
  assert.deepEqual(M.rowCounts(null), { all: 0, working: 0, unread: 0 });
});

test('an agent runtime row reads as a word', () => {
  assert.deepEqual(M.agentStatus({ status: 'busy', stale: false }), { key: 'working', label: 'working' });
  assert.deepEqual(M.agentStatus({ status: 'free' }), { key: 'free', label: 'free' });
  assert.deepEqual(M.agentStatus({ status: 'busy', stale: true }), { key: 'stale', label: 'not responding' });
  assert.deepEqual(M.agentStatus(null), { key: 'unknown', label: '' });
});

test('open tasks: the agent\'s only, closed left out, in progress first, then newest', () => {
  const tasks = [
    { id: 1, assignee: 'dev', state: 'queued', updated_at: '2026-10-10' },
    { id: 2, assignee: 'dev', state: 'done', updated_at: '2026-10-14' },
    { id: 3, assignee: 'dev', state: 'in_progress', updated_at: '2026-10-01' },
    { id: 4, assignee: 'ops', state: 'queued', updated_at: '2026-10-14' },
    { id: 5, assignee: 'dev', state: 'queued', updated_at: '2026-10-12' },
    { id: 6, assignee: 'dev', status: 'rejected', updated_at: '2026-10-13' },
  ];
  assert.deepEqual(M.openTasks(tasks, 'dev').map((t) => t.id), [3, 5, 1]);
  assert.deepEqual(M.openTasks(tasks, 'dev', 2).map((t) => t.id), [3, 5]);
  assert.deepEqual(M.openTasks(null, 'dev'), []);
});

test('the header names one, three, or two and the rest', () => {
  const nm = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  assert.equal(M.membersTitle(['john'], nm), 'John');
  assert.equal(M.membersTitle(['john', 'ann'], nm), 'John and Ann');
  assert.equal(M.membersTitle(['john', 'ann', 'ops'], nm), 'John, Ann and Ops');
  assert.equal(M.membersTitle(['john', 'ann', 'ops', 'dev', 'sam'], nm), 'John, Ann and 3 more');
  assert.equal(M.membersTitle([], nm), '');
});

test('the unread badge: nothing for 0, the number, then 99+', () => {
  assert.equal(M.badgeText(0), '');
  assert.equal(M.badgeText(-3), '');
  assert.equal(M.badgeText(null), '');
  assert.equal(M.badgeText(1), '1');
  assert.equal(M.badgeText(99), '99');
  assert.equal(M.badgeText(100), '99+');
});

test('a row adds up its chats, less the one being read', () => {
  const unread = { a: 2, b: 3, c: 0 };
  assert.equal(M.rowUnread(['a', 'b', 'c'], unread, null), 5);
  assert.equal(M.rowUnread(['a', 'b'], unread, 'a'), 3);
  assert.equal(M.rowUnread(['a'], unread, 'a'), 0);
  assert.equal(M.rowUnread(['x'], unread, null), 0);
  assert.equal(M.rowUnread(null, unread, null), 0);
});

test('the page title carries the total once', () => {
  assert.equal(M.titleWithCount('Studio', 3), '(3) Studio');
  assert.equal(M.titleWithCount('(3) Studio', 5), '(5) Studio');
  assert.equal(M.titleWithCount('(99+) Studio', 0), 'Studio');
  assert.equal(M.titleWithCount('Studio', 250), '(99+) Studio');
});

test('the read mark: the newest time, only when new or never marked, never twice', () => {
  const at = '2026-10-15T10:00:00.123456Z';
  assert.equal(M.readMark({ unread: 2, last_at: at, read_upto: '2026-10-15T09:00:00Z' }, undefined), at);
  assert.equal(M.readMark({ unread: 2, last_at: at, read_upto: 'x' }, at), null);
  assert.equal(M.readMark({ unread: 0, last_at: at, read_upto: 'x' }, undefined), null);
  // A chat from before read tracking: marked once when shown, so it starts counting.
  assert.equal(M.readMark({ unread: 0, last_at: at, read_upto: null }, undefined), at);
  assert.equal(M.readMark({ unread: 0, last_at: null, read_upto: null }, undefined), null);
  assert.equal(M.readMark(undefined, undefined), null);
});

test('a newer message moves a row; an older or unknown time does not', () => {
  assert.equal(M.newerThan('2026-10-15T10:00:01Z', '2026-10-15T10:00:00Z'), true);
  assert.equal(M.newerThan('2026-10-15T10:00:00Z', '2026-10-15T10:00:00Z'), false);
  assert.equal(M.newerThan('2026-10-15T09:00:00Z', '2026-10-15T10:00:00Z'), false);
  assert.equal(M.newerThan('2026-10-15T09:00:00Z', null), true);
  assert.equal(M.newerThan(null, '2026-10-15T10:00:00Z'), false);
});
