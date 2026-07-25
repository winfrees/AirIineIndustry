// App-shell cache only. /api/* is always network — the shell just needs to
// load instantly and offline; live game state requires the backend.
const CACHE = "airlinesim-shell-v1";
const SHELL = ["/", "/app.js", "/styles.css", "/manifest.json",
               "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (evt) => {
  evt.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (evt) => {
  evt.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (evt) => {
  const url = new URL(evt.request.url);
  if (url.pathname.startsWith("/api/")) return; // never cache live API calls
  evt.respondWith(
    caches.match(evt.request).then((cached) => cached || fetch(evt.request))
  );
});
