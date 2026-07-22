/**
 * Voice-activated notes for debt chasing (Web Speech API).
 * Works in Chromium browsers (Chrome, Edge). Gracefully degrades elsewhere.
 */
(function () {
  "use strict";

  function speechSupported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  function initBar(bar) {
    var targetId = bar.getAttribute("data-voice-target");
    var channelId = bar.getAttribute("data-voice-channel");
    var ta = targetId ? document.getElementById(targetId) : null;
    var channelEl = channelId ? document.getElementById(channelId) : null;
    var startBtn = bar.querySelector("[data-voice-start]");
    var stopBtn = bar.querySelector("[data-voice-stop]");
    var status = bar.querySelector("[data-voice-status]");
    if (!ta || !startBtn) return;

    if (!speechSupported()) {
      if (status) {
        status.textContent = "Speech recognition not available in this browser.";
      }
      startBtn.disabled = true;
      return;
    }

    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var rec = new SR();
    rec.lang = "en-GB";
    rec.continuous = true;
    rec.interimResults = true;
    var listening = false;
    var finalChunk = "";

    rec.onstart = function () {
      listening = true;
      finalChunk = "";
      if (startBtn) startBtn.hidden = true;
      if (stopBtn) stopBtn.hidden = false;
      if (status) status.textContent = "Listening… speak clearly.";
      if (channelEl) channelEl.value = "voice";
    };

    rec.onerror = function (ev) {
      if (status) {
        status.textContent = "Voice error: " + (ev.error || "unknown");
      }
    };

    rec.onend = function () {
      listening = false;
      if (startBtn) startBtn.hidden = false;
      if (stopBtn) stopBtn.hidden = true;
      if (status && status.textContent.indexOf("error") === -1) {
        status.textContent = finalChunk
          ? "Captured. Review notes then log."
          : "Stopped.";
      }
    };

    rec.onresult = function (event) {
      var interim = "";
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var t = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          finalChunk += t + " ";
        } else {
          interim += t;
        }
      }
      var base = ta.dataset.voiceBase != null ? ta.dataset.voiceBase : ta.value;
      if (ta.dataset.voiceBase == null) {
        ta.dataset.voiceBase = ta.value || "";
        base = ta.dataset.voiceBase;
      }
      var sep = base && !/\s$/.test(base) ? " " : "";
      ta.value = (base + sep + finalChunk + interim).replace(/\s+/g, " ").trimStart();
    };

    startBtn.addEventListener("click", function () {
      try {
        ta.dataset.voiceBase = ta.value || "";
        finalChunk = "";
        rec.start();
      } catch (e) {
        if (status) status.textContent = "Could not start microphone.";
      }
    });

    if (stopBtn) {
      stopBtn.addEventListener("click", function () {
        try {
          rec.stop();
        } catch (e) {
          /* ignore */
        }
      });
    }
  }

  function boot() {
    document.querySelectorAll(".voice-bar").forEach(initBar);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
