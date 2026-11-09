import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', 'public/next2/js/formatter.js'), 'utf8');
const win = {};
new Function('window', 'document', src)(win, {});
const md = (s) => win.snFormatter.markdown(s);
const count = (html, tag) => (html.match(new RegExp(`<${tag}[ >]`, 'g')) || []).length;

test('a numbered list with a blank line between items is one list, numbered 1 2 3', () => {
  const html = md('Ten points.\n\n1. first\n\n2. second\n\n3. third\n');
  assert.equal(count(html, 'ol'), 1, html);
  assert.equal(count(html, 'li'), 3, html);
});

test('a tight list is unchanged', () => {
  const html = md('1. a\n2. b\n');
  assert.equal(html, '<ol><li>a</li><li>b</li></ol>');
});

test('the list ends at the first line that is not another item of it', () => {
  assert.equal(md('1. a\n\n2. b\n\nAfter the list'),
    '<ol><li>a</li><li>b</li></ol><p>After the list</p>');
  assert.equal(md('- a\n\n1. b'), '<ul><li>a</li></ul><ol><li>b</li></ol>');
  const html = md('1. a\n\n   indented, not an item');
  assert.match(html, /^<ol><li>a<\/li><\/ol><p>\s*indented, not an item<\/p>$/, html);
});

test('a list that starts at 3 starts at 3', () => {
  assert.equal(md('3. c\n4. d'), '<ol start="3"><li>c</li><li>d</li></ol>');
  assert.equal(md('1. a'), '<ol><li>a</li></ol>');
});
