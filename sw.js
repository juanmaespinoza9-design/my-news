// Service worker de Mi Diario: red primero, caché como respaldo.
// La app siempre muestra lo más fresco si hay conexión, y la última edición si no la hay.
const CACHE = "midiario-v1";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./data/articles.json",
  "./data/summaries.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return resp;
      })
      .catch(() =>
        caches.match(e.request).then((hit) => hit || (e.request.mode === "navigate" ? caches.match("./index.html") : undefined))
      )
  );
});
