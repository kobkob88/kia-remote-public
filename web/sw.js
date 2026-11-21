// Car Remote service worker: network first, cache only as the offline fallback. Never caches /api/.
const VERSION = 'sportage-v25';   // keep APP_VERSION in app.js in step

const NETWORK_WAIT_MS = 3000;
const DEAD_FOR_MS = 15000;   // after one slow answer, don't wait on the network for the next files
let deadUntil = 0;
const GIVE_UP_MS = 10000;

const SHELL = [
  './',
  'index.html',
  'app.css',
  'app.js',
  'api.js',
  'car.js',
  'climate.js',
  'schedule.js',
  'health.js',
  'activity.js',
  'dom.js',
  'spin.js',
  'diag.js',
  'boot.js',
  'format.js',
  'passkey.js',
  'strings.js',
  'manifest.webmanifest',
  'icons/icon.svg',
  'icons/icon-192.png',
  'icons/icon-512.png',
  'icons/icon-maskable-512.png',
  'icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    // cache: 'reload' skips the HTTP cache so stale bytes never land in a new versioned cache
    await cache.addAll(SHELL.map((url) => new Request(url, { cache: 'reload' })));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== VERSION) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const scopePath = new URL(self.registration.scope).pathname;
  if (url.pathname.startsWith(`${scopePath}api/`) || url.pathname.startsWith('/api/')) return; // live data only

  event.respondWith((async () => {
    const fromCache = async () => (await caches.match(req, { ignoreSearch: true }))
      || (req.mode === 'navigate' ? caches.match('index.html') : undefined);
    // give up on a request that never answers, so it doesn't hold a connection the next try needs
    // (not for a navigation: fetch() refuses a navigate-mode Request with an init)
    const ctrl = new AbortController();
    const abort = setTimeout(() => ctrl.abort(), GIVE_UP_MS);
    const network = (req.mode === 'navigate' ? fetch(req) : fetch(req, { signal: ctrl.signal })).finally(() => clearTimeout(abort)).then(async (fresh) => {
      if (fresh.ok) {
        const cache = await caches.open(VERSION);
        await cache.put(req, fresh.clone());
      }
      return fresh;
    });
    // A phone back from the background can hold a dead connection that never answers. After a few
    // seconds, use the cached copy if there is one (the network answer still refreshes the cache).
    const wait = Date.now() < deadUntil ? 0 : NETWORK_WAIT_MS;
    const slow = new Promise((resolve) => setTimeout(resolve, wait, 'slow'));
    network.then(() => { deadUntil = 0; }, () => {});
    try {
      const first = await Promise.race([network, slow]);
      if (first !== 'slow') return first.status >= 500 ? ((await fromCache()) || first) : first;
      deadUntil = Date.now() + DEAD_FOR_MS;
      const cached = await fromCache();
      if (cached) { event.waitUntil(network.catch(() => {})); return cached; }
      return await network;   // nothing cached: whatever the network says
    } catch {
      return (await fromCache()) || Response.error();
    }
  })());
});
