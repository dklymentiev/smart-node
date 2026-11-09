// Studio's service worker. It makes the Studio installable on a phone or a
// computer and shows a page of its own when the node cannot be reached. It keeps
// nothing else: the shell, tasks, chats and every /api answer come from the node
// each time, behind its sign-in, so an installed Studio never shows stale data
// or what an earlier sign-in saw.
const CACHE = 'studio-offline-v1';
const OFFLINE = '/offline.html';

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE)
    .then((c) => c.add(new Request(OFFLINE, { cache: 'reload' })))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Only a page load is answered here, and only when the network fails. Any other
// request is left to the browser as if there were no service worker.
self.addEventListener('fetch', (e) => {
  if (e.request.mode !== 'navigate') return;
  e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
});
