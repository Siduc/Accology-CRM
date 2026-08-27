/**
 * Register Accologise service worker (PWA).
 * Skip on this laptop (localhost): a SW + Chrome "no internet"
 * (hotspot down) serves the offline page even when the CRM is running.
 */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator)) {
    return;
  }
  var host = (location.hostname || "").toLowerCase();
  var local = host === "127.0.0.1" || host === "localhost" || host === "[::1]";
  window.addEventListener("load", function () {
    if (local) {
      navigator.serviceWorker.getRegistrations().then(function (regs) {
        regs.forEach(function (r) {
          r.unregister();
        });
      });
      return;
    }
    navigator.serviceWorker
      .register("/sw.js", { scope: "/" })
      .catch(function () {
        /* ignore — e.g. insecure context */
      });
  });
})();
