// App-shell cache only. /api/* is always network — the shell just needs to
// load instantly and offline; live game state requires the backend.
//
// Bump CACHE when a shell asset changes: `activate` deletes every cache whose
// name doesn't match, so the bump is what evicts the previous copies. This was
// pinned at v1 while styles.css and app.js changed under it, and because the
// old `fetch` handler answered from the cache first, an upgraded install kept
// serving the UI it had cached the very first time it was opened. The
// network-first handler below is the structural fix — a bump alone would only
// have papered over it until the next asset change.
const CACHE = "airlinesim-shell-v6";
const SHELL = ["/", "/app.js", "/styles.css", "/manifest.json",
               "/explore.html", "/explore.js", "/explore.css",
               "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (evt) => {
  // cache: "reload" — seed from the server, never from the browser's HTTP
  // cache. addAll's default fetch can be satisfied by a stale disk-cache
  // entry, which would poison this cache with old assets at install time.
  evt.waitUntil(caches.open(CACHE).then((c) =>
    c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" })))
  ));
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

// Network-first, cache as the offline fallback. The server is on localhost or
// the LAN, so the round trip costs nothing worth optimizing, and a reachable
// server always wins — which is what stops an old styles.css from outliving an
// upgrade. The cache is here to make the shell work offline, not to make it
// fast. Each successful response refreshes the cached copy.
self.addEventListener("fetch", (evt) => {
  const url = new URL(evt.request.url);
  if (url.pathname.startsWith("/api/")) return; // never cache live API calls
  if (evt.request.method !== "GET") return;
  evt.respondWith(
    // cache: "no-cache" — force revalidation with the server instead of
    // letting the HTTP cache answer. Without it "network-first" can be served
    // a stale disk-cache copy without the request ever leaving the browser.
    fetch(evt.request, { cache: "no-cache" })
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(evt.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(evt.request).then((c) => c || Response.error()))
  );
});
