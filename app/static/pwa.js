/**
 * Register Accologise service worker (PWA).
 * Safe no-op when unsupported or offline registration fails.
 */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator)) {
    return;
  }
  window.addEventListener("load", function () {
    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch(function () {
        /* ignore — e.g. insecure context */
      });
  });
})();
