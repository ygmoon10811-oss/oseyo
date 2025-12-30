self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open("oseyo-v1").then((cache) => cache.addAll(["/"]))
  );
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    caches.match(event.request).then((r) => r || fetch(event.request))
  );
});
