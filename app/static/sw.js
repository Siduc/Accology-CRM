/* Accologise CRM — basic service worker (shell + offline fallback) */
/* Bump CACHE_NAME when shipping static asset changes. */
/* Bump this whenever dashboard/WIP UI or CSS changes ship. */
const CACHE_NAME = "accologise-v2-wip-horizons";
const PRECACHE = [
  "/static/style.css?v=wip-horizons2",
  "/static/dashboard_view.js?v=dual1",
  "/static/pwa.js?v=2",
  "/static/offline.html",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-512-maskable.png",
  "/static/icons/apple-touch-icon.png",
  "/static/icons/favicon-32.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") {
    return;
  }

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  // HTML navigations: network first, offline page on failure
  const isNavigate =
    req.mode === "navigate" ||
    (req.headers.get("accept") || "").includes("text/html");

  if (isNavigate) {
    event.respondWith(
      fetch(req)
        .then((res) => res)
        .catch(() =>
          caches.match("/static/offline.html").then(
            (r) =>
              r ||
              new Response("You are offline.", {
                status: 503,
                headers: { "Content-Type": "text/plain" },
              })
          )
        )
    );
    return;
  }

  // Static assets: network first for CSS/JS so deploys are not stuck on old SW cache
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    const isCodeAsset =
      url.pathname.endsWith(".css") ||
      url.pathname.endsWith(".js") ||
      url.pathname.endsWith(".webmanifest");
    if (isCodeAsset) {
      event.respondWith(
        fetch(req)
          .then((res) => {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
            return res;
          })
          .catch(() => caches.match(req))
      );
      return;
    }
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) {
          return cached;
        }
        return fetch(req)
          .then((res) => {
            if (res && res.ok && res.type === "basic") {
              const clone = res.clone();
              caches.open(CACHE_NAME).then((c) => c.put(req, clone));
            }
            return res;
          })
          .catch(() => caches.match(req));
      })
    );
  }
});
