/**
 * Clickable column headers on list tables.
 * First click: A–Z / lowest–highest · second click: reverse.
 * Auto-enhances table.data (and table.sortable) unless data-no-sort is set.
 */
(function () {
  "use strict";

  var SKIP_LABELS = /^(actions?|edit|view|)$/i;

  function textOf(el) {
    if (!el) return "";
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function parseSortValue(raw) {
    var s = (raw || "").trim();
    if (!s || s === "—" || s === "-" || s === "–") {
      return { type: "empty", value: "" };
    }

    // Currency / numbers: £1,234.56 · +£100 · (1,200) · 12.5%
    var money = s.replace(/[£$€,\s]/g, "").replace(/^\((.+)\)$/, "-$1");
    if (/^[+-]?\d+(\.\d+)?%?$/.test(money)) {
      var n = parseFloat(money.replace("%", ""));
      if (!isNaN(n)) return { type: "number", value: n };
    }

    // UK dates: DD-MM-YYYY or DD/MM/YYYY (with optional time)
    var uk = s.match(
      /^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})(?:\s+(\d{1,2}):(\d{2}))?/
    );
    if (uk) {
      var dd = parseInt(uk[1], 10);
      var mm = parseInt(uk[2], 10) - 1;
      var yy = parseInt(uk[3], 10);
      if (yy < 100) yy += 2000;
      var hh = uk[4] ? parseInt(uk[4], 10) : 0;
      var mi = uk[5] ? parseInt(uk[5], 10) : 0;
      var t = new Date(yy, mm, dd, hh, mi).getTime();
      if (!isNaN(t)) return { type: "date", value: t };
    }

    // ISO dates
    var iso = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (iso) {
      var t2 = new Date(
        parseInt(iso[1], 10),
        parseInt(iso[2], 10) - 1,
        parseInt(iso[3], 10)
      ).getTime();
      if (!isNaN(t2)) return { type: "date", value: t2 };
    }

    return { type: "text", value: s.toLowerCase() };
  }

  function cellValue(row, colIndex) {
    var cells = row.children;
    if (!cells || colIndex >= cells.length) return { type: "empty", value: "" };
    var cell = cells[colIndex];
    // Prefer data-sort when present
    if (cell && cell.getAttribute) {
      var ds = cell.getAttribute("data-sort");
      if (ds != null && ds !== "") return parseSortValue(ds);
    }
    return parseSortValue(textOf(cell));
  }

  function compare(a, b, dir) {
    var typeOrder = { empty: 0, number: 1, date: 1, text: 2 };
    if (a.type === "empty" && b.type !== "empty") return 1; // empties last
    if (b.type === "empty" && a.type !== "empty") return -1;
    if (a.type !== b.type && a.type !== "empty" && b.type !== "empty") {
      // Mixed: try numeric if both parseable as numbers already handled
      if (a.type === "text" || b.type === "text") {
        var as = String(a.value);
        var bs = String(b.value);
        if (as < bs) return -1 * dir;
        if (as > bs) return 1 * dir;
        return 0;
      }
    }
    if (a.value < b.value) return -1 * dir;
    if (a.value > b.value) return 1 * dir;
    return 0;
  }

  function sortTable(table, colIndex, dir) {
    var tbody = table.tBodies && table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (ra, rb) {
      return compare(cellValue(ra, colIndex), cellValue(rb, colIndex), dir);
    });
    var frag = document.createDocumentFragment();
    rows.forEach(function (r) {
      frag.appendChild(r);
    });
    tbody.appendChild(frag);
  }

  function clearIndicators(ths) {
    ths.forEach(function (th) {
      th.classList.remove("is-sorted-asc", "is-sorted-desc");
      th.removeAttribute("aria-sort");
      var ind = th.querySelector(".sort-ind");
      if (ind) ind.textContent = "";
    });
  }

  function enhanceTable(table) {
    if (!table || table.dataset.sortReady === "1") return;
    if (table.hasAttribute("data-no-sort")) return;
    // Skip tiny tables / nested tables without thead
    var thead = table.tHead;
    if (!thead || !thead.rows.length) return;
    var headerRow = thead.rows[0];
    var ths = Array.prototype.slice.call(headerRow.cells);
    if (ths.length < 2) return;

    table.dataset.sortReady = "1";
    table.classList.add("table-sortable");

    var state = { col: -1, dir: 1 };

    ths.forEach(function (th, index) {
      var label = textOf(th);
      // Skip blank action columns and explicit opt-outs
      if (th.hasAttribute("data-no-sort")) return;
      if (!label || SKIP_LABELS.test(label)) return;
      // Skip columns that are only a checkbox / control
      if (th.querySelector("input, button, select")) return;

      th.classList.add("sortable-th");
      th.setAttribute("tabindex", "0");
      th.setAttribute("role", "columnheader");
      th.setAttribute("title", "Click to sort");
      if (!th.querySelector(".sort-ind")) {
        var span = document.createElement("span");
        span.className = "sort-ind";
        span.setAttribute("aria-hidden", "true");
        th.appendChild(span);
      }

      function activate() {
        if (state.col === index) {
          state.dir = -state.dir;
        } else {
          state.col = index;
          state.dir = 1; // first click: A–Z / low→high
        }
        clearIndicators(ths);
        th.classList.add(state.dir === 1 ? "is-sorted-asc" : "is-sorted-desc");
        th.setAttribute("aria-sort", state.dir === 1 ? "ascending" : "descending");
        var ind = th.querySelector(".sort-ind");
        if (ind) ind.textContent = state.dir === 1 ? " \u25B2" : " \u25BC";
        sortTable(table, index, state.dir);
      }

      th.addEventListener("click", function (e) {
        // Don't steal clicks on links inside headers (rare)
        if (e.target && e.target.closest && e.target.closest("a")) return;
        e.preventDefault();
        activate();
      });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate();
        }
      });
    });
  }

  function enhanceAll(root) {
    var scope = root || document;
    var tables = scope.querySelectorAll("table.data, table.sortable");
    tables.forEach(enhanceTable);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      enhanceAll();
    });
  } else {
    enhanceAll();
  }

  // Expose for dynamic fragments
  window.accologiseTableSort = { enhance: enhanceAll, enhanceTable: enhanceTable };
})();
