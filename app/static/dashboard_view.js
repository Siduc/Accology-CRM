/**
 * Accologise dashboard view preference.
 * localStorage key: accologise_dashboard_view = auto | desktop | mobile
 */
(function () {
  var KEY = "accologise_dashboard_view";

  function readPref() {
    try {
      var v = (localStorage.getItem(KEY) || "auto").toLowerCase();
      if (v === "desktop" || v === "mobile" || v === "auto") return v;
    } catch (e) {}
    return "auto";
  }

  function writePref(v) {
    try {
      localStorage.setItem(KEY, v);
    } catch (e) {}
  }

  function applyToBody(pref) {
    var b = document.body;
    if (!b) return;
    b.classList.remove("dash-auto", "dash-force-desktop", "dash-force-mobile");
    if (pref === "desktop") b.classList.add("dash-force-desktop");
    else if (pref === "mobile") b.classList.add("dash-force-mobile");
    else b.classList.add("dash-auto");

    // Dark Live Tiles hub chrome on every small screen (all pages)
    var mobileForced = pref === "mobile";
    var desktopForced = pref === "desktop";
    var narrow =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 767.98px)").matches;
    // Prefer hub on narrow viewports even if user picked "desktop" —
    // small screens stay dark Live Tiles; wide screens can use desk chrome.
    var showHub = narrow || mobileForced;
    if (desktopForced && !narrow) showHub = false;
    b.classList.toggle("body-hub", showHub);
    b.classList.toggle("body-page", !showHub);
  }

  // Re-apply on resize so rotating a tablet flips theme
  if (typeof window.matchMedia === "function") {
    try {
      window
        .matchMedia("(max-width: 767.98px)")
        .addEventListener("change", function () {
          applyToBody(readPref());
        });
    } catch (e) {
      /* older browsers */
    }
  }

  function initDashboard() {
    applyToBody(readPref());
  }

  function initSettingsForm() {
    var form = document.getElementById("dashboard-view-form");
    if (!form) return;
    var pref = readPref();
    var inputs = form.querySelectorAll('input[name="dashboard_view"]');
    inputs.forEach(function (el) {
      el.checked = el.value === pref;
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var selected = form.querySelector('input[name="dashboard_view"]:checked');
      var v = selected ? selected.value : "auto";
      writePref(v);
      applyToBody(v);
      window.location.href = "/dashboard";
    });
  }

  // Expose for optional UI chips
  window.AccologiseView = {
    KEY: KEY,
    read: readPref,
    write: writePref,
    apply: applyToBody,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initDashboard();
      initSettingsForm();
    });
  } else {
    initDashboard();
    initSettingsForm();
  }

  // Re-apply on resize when in auto mode (orientation change)
  var t;
  window.addEventListener("resize", function () {
    clearTimeout(t);
    t = setTimeout(function () {
      if (readPref() === "auto") applyToBody("auto");
    }, 120);
  });
})();
