/**
 * HTML5 drag-and-drop for practice groups board.
 * Falls back to Move selects under each chip.
 * Shift+wheel scrolls the board horizontally.
 */
(function () {
  "use strict";
  var board = document.getElementById("group-board");
  if (!board) return;

  // Horizontal scroll with shift+wheel or trackpad deltaX
  board.addEventListener(
    "wheel",
    function (e) {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        // Convert vertical wheel to horizontal when over the board
        if (board.scrollWidth > board.clientWidth) {
          e.preventDefault();
          board.scrollLeft += e.deltaY;
        }
      }
    },
    { passive: false }
  );

  var dragClientId = null;

  board.querySelectorAll(".group-chip[draggable=true]").forEach(function (chip) {
    chip.addEventListener("dragstart", function (e) {
      dragClientId = chip.getAttribute("data-client-id");
      chip.classList.add("is-dragging");
      try {
        e.dataTransfer.setData("text/plain", dragClientId || "");
        e.dataTransfer.effectAllowed = "move";
      } catch (err) {
        /* ignore */
      }
    });
    chip.addEventListener("dragend", function () {
      chip.classList.remove("is-dragging");
      dragClientId = null;
      board.querySelectorAll(".group-drop").forEach(function (z) {
        z.classList.remove("drop-hover");
      });
    });
  });

  board.querySelectorAll(".group-drop").forEach(function (zone) {
    zone.addEventListener("dragover", function (e) {
      e.preventDefault();
      zone.classList.add("drop-hover");
    });
    zone.addEventListener("dragleave", function () {
      zone.classList.remove("drop-hover");
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      zone.classList.remove("drop-hover");
      var cid =
        dragClientId ||
        (e.dataTransfer && e.dataTransfer.getData("text/plain"));
      if (!cid) return;
      var gid = zone.getAttribute("data-group-id") || "";
      var body = new URLSearchParams();
      body.set("client_id", cid);
      body.set("group_id", gid);
      fetch("/groups/move", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded",
          "X-Requested-With": "fetch",
        },
        body: body.toString(),
        credentials: "same-origin",
      })
        .then(function (r) {
          return r.json().catch(function () {
            return { ok: true };
          });
        })
        .then(function () {
          window.location.reload();
        })
        .catch(function () {
          window.location.reload();
        });
    });
  });
})();
