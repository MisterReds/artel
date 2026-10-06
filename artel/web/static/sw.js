/* Artel service worker: caches only the app shell (page, fonts, icons) so the installed app opens fast and
   shows its frame offline. Live data is never cached: /api/* (JSON and the SSE event stream), the login
   endpoints and any non-GET request always go straight to the network. */
const SHELL = 'artel-shell-v1';
const ASSETS = [
  '/',
  '/manifest.webmanifest',
  '/static/favicon.svg',
  '/static/mark.svg',
  '/static/fonts/fonts.css',
  '/static/fonts/Onest-cyrillic-dea2c9.woff2',
  '/static/fonts/Onest-latin-f5814c.woff2',
  '/static/fonts/Unbounded-cyrillic-92a538.woff2',
  '/static/fonts/Unbounded-latin-f2b2b2.woff2',
  '/static/fonts/JetBrainsMono-cyrillic-028edb.woff2',
  '/static/fonts/JetBrainsMono-latin-0a7197.woff2',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', e => {
  // one by one: a missing or redirected entry (e.g. '/' before login) must not fail the whole install
  e.waitUntil(caches.open(SHELL).then(c => Promise.all(ASSETS.map(u => fetch(u, {credentials: 'same-origin'})
    .then(r => (r.ok && !r.redirected) ? c.put(u, r) : null).catch(() => null)))).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function neverCache(url, req) {
  return req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/') ||
    url.pathname === '/login' || url.pathname === '/sw.js';
}

self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (neverCache(url, req)) return;  // the browser handles it: network only, nothing stored
  if (req.mode === 'navigate' || url.pathname === '/') {
    // the page: network first (fresh UI, login redirect works), the cached shell only when offline
    e.respondWith(fetch(req).then(r => {
      if (r.ok && !r.redirected && url.pathname === '/') {
        const copy = r.clone();
        caches.open(SHELL).then(c => c.put('/', copy));
      }
      return r;
    }).catch(() => caches.match('/')));
    return;
  }
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    // shell assets: cached copy at once, refreshed in the background
    e.respondWith(caches.open(SHELL).then(c => c.match(req).then(hit => {
      const net = fetch(req).then(r => { if (r.ok) c.put(req, r.clone()); return r; });
      if (hit) { net.catch(() => {}); return hit; }
      return net;
    })));
  }
});
