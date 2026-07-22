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

    // Hub chrome only when mobile view is effectively showing
    var mobileForced = pref === "mobile";
    var desktopForced = pref === "desktop";
    var narrow =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 767.98px)").matches;
    var showHub = mobileForced || (!desktopForced && narrow);
    b.classList.toggle("body-hub", showHub);
    b.classList.toggle("body-page", !showHub);
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
