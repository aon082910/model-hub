// Makes Model Hub installable on a phone or desktop. It deliberately caches nothing from the app
// itself (an upgrade must never leave an old page running); it only answers with a short note
// when the server cannot be reached.
const OFFLINE_PAGE = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
  + '<title>Model Hub</title><body style="font-family:system-ui,sans-serif;background:#14161a;color:#e7e9ee;padding:24px">'
  + '<h1>Model Hub</h1><p>The server cannot be reached right now. Check that you are on the same network '
  + 'as your server (or connected to your VPN), then reload.</p></body>';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.mode !== 'navigate') return;          // everything else goes straight to the network
  event.respondWith(fetch(event.request).catch(() => new Response(OFFLINE_PAGE, {
    status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' },
  })));
});
