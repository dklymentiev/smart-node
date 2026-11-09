// The Studio as an installable app: the manifest and its icons are there, the
// shell links them, and the service worker keeps nothing but its offline page.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const pub = join(dirname(fileURLToPath(import.meta.url)), '..', 'public');
const read = (p) => readFileSync(join(pub, p));

// A PNG's width and height sit at bytes 16..23 of its IHDR chunk.
const pngSize = (buf) => [buf.readUInt32BE(16), buf.readUInt32BE(20)];

test('the manifest names the app and opens it standalone at the root', () => {
  const m = JSON.parse(read('manifest.webmanifest'));
  assert.equal(m.name, 'Smart Node');  // the installed app carries the product's name
  assert.equal(m.short_name, 'Smart Node');
  assert.equal(m.start_url, '/');
  assert.equal(m.scope, '/');
  assert.equal(m.display, 'standalone');
});

test('every icon in the manifest exists at the size it claims', () => {
  const m = JSON.parse(read('manifest.webmanifest'));
  const sizes = new Set();
  for (const i of m.icons) {
    const [w, h] = pngSize(read(i.src.slice(1)));
    assert.equal(`${w}x${h}`, i.sizes, i.src);
    sizes.add(i.sizes);
  }
  assert.ok(sizes.has('192x192') && sizes.has('512x512'));
  assert.ok(m.icons.some((i) => i.purpose === 'maskable'));
  assert.deepEqual(pngSize(read('icons/apple-touch-icon.png')), [180, 180]);
});

test('the shell links the manifest and its icons and registers the worker', () => {
  const html = read('next2/index.html').toString();
  assert.match(html, /<link rel="manifest" href="\/manifest.webmanifest">/);
  assert.match(html, /<meta name="theme-color"/);
  assert.match(html, /<link rel="apple-touch-icon" href="\/icons\/apple-touch-icon.png">/);
  assert.match(html, /navigator\.serviceWorker\.register\('\/sw\.js'\)/);
});

test('the worker keeps only its offline page and answers only failed page loads', () => {
  const sw = read('sw.js').toString();
  assert.ok(existsSync(join(pub, 'offline.html')));
  // The one thing it stores; nothing it fetches is put in a cache.
  assert.match(sw, /c\.add\(new Request\(OFFLINE/);
  assert.doesNotMatch(sw, /\.put\(|addAll\(/);
  assert.match(sw, /if \(e\.request\.mode !== 'navigate'\) return;/);
  assert.match(sw, /fetch\(e\.request\)\.catch\(\(\) => caches\.match\(OFFLINE\)\)/);
});
