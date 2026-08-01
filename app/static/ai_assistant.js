/**
 * Si — Accologise assistant (chat + voice Web Speech API).
 */
(function () {
  "use strict";

  var history = [];
  var pendingToken = null;
  var listening = false;
  var recognition = null;
  var PLAN_KEY = "accologise_si_plan_token";

  function loadPending() {
    try {
      pendingToken = sessionStorage.getItem(PLAN_KEY) || null;
    } catch (e) {
      pendingToken = null;
    }
  }

  function savePending(token) {
    pendingToken = token || null;
    try {
      if (token) sessionStorage.setItem(PLAN_KEY, token);
      else sessionStorage.removeItem(PLAN_KEY);
    } catch (e) {}
  }

  function isAffirmative(text) {
    var t = (text || "").trim().toLowerCase().replace(/[!.]+$/g, "");
    return /^(yes|y|yeah|yep|yup|ok|okay|sure|confirm|do it|go ahead|please do|affirmative|yeh|yea)(\s+please)?$/.test(
      t
    );
  }

  function isNegative(text) {
    var t = (text || "").trim().toLowerCase().replace(/[!.]+$/g, "");
    return /^(no|n|nope|cancel|stop|don't|do not|never mind|nevermind)$/.test(t);
  }

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function speechSupported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function formatReply(text) {
    // Lightweight markdown: **bold**, newlines → br, • lists stay as text
    var esc = (text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    esc = esc.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    esc = esc.replace(/\n/g, "<br>");
    return esc;
  }

  function appendBubble(role, htmlOrText, isHtml) {
    var log = $("#ai-chat-log");
    if (!log) return;
    var b = el("div", "ai-bubble ai-bubble-" + role);
    if (isHtml) b.innerHTML = htmlOrText;
    else b.innerHTML = formatReply(htmlOrText);
    log.appendChild(b);
    log.scrollTop = log.scrollHeight;
  }

  function appendLinks(links) {
    if (!links || !links.length) return;
    var log = $("#ai-chat-log");
    var wrap = el("div", "ai-links");
    links.forEach(function (L) {
      var a = el("a", "ai-link-chip", L.label || L.href);
      a.href = L.href;
      wrap.appendChild(a);
    });
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  function showPlan(plan) {
    savePending(plan.token);
    var box = $("#ai-plan-card");
    if (!box) return;
    box.hidden = false;
    $("#ai-plan-summary").textContent = plan.summary || "Confirm actions";
    var ul = $("#ai-plan-steps");
    ul.innerHTML = "";
    (plan.steps || []).forEach(function (s) {
      var li = el("li", "", (s.label || s.op) + (s.detail ? " — " + s.detail : ""));
      ul.appendChild(li);
    });
  }

  function hidePlan() {
    savePending(null);
    var box = $("#ai-plan-card");
    if (box) box.hidden = true;
  }

  function setBusy(on) {
    var send = $("#ai-send");
    var input = $("#ai-input");
    if (send) send.disabled = !!on;
    if (input) input.disabled = !!on;
    var st = $("#ai-status");
    if (st && on) st.textContent = "Thinking…";
    if (st && !on && !listening) st.textContent = "";
  }

  function setListening(on) {
    listening = on;
    var fab = $("#ai-mic");
    var st = $("#ai-status");
    var panel = $("#ai-panel");
    if (fab) fab.classList.toggle("is-listening", on);
    if (panel) panel.classList.toggle("is-listening", on);
    if (st) st.textContent = on ? "Listening…" : "";
  }

  async function postJSON(url, body) {
    var res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    var data = {};
    try {
      data = await res.json();
    } catch (e) {
      data = { kind: "message", reply: "Bad response from server (" + res.status + ")" };
    }
    if (!res.ok && !data.reply) {
      data.reply = data.reply || "Request failed (" + res.status + ")";
    }
    return data;
  }

  async function sendMessage(text) {
    text = (text || "").trim();
    if (!text) return;
    appendBubble("user", text, false);
    history.push({ role: "user", content: text });
    var input = $("#ai-input");
    if (input) input.value = "";

    // Voice/type "yes" / "no" while a plan is pending → confirm, don't re-parse
    if (pendingToken && isAffirmative(text)) {
      await confirmPlan(true, true);
      return;
    }
    if (pendingToken && isNegative(text)) {
      await confirmPlan(false, true);
      return;
    }

    // New message that isn't confirm — clear plan card only
    var box = $("#ai-plan-card");
    if (box) box.hidden = true;
    // Keep token until a new plan replaces it or user cancels via no

    setBusy(true);
    try {
      var data = await postJSON("/assistant/chat", {
        message: text,
        history: history.slice(-10),
        page_context: { path: location.pathname },
        plan_token: pendingToken || "",
      });
      appendBubble("assistant", data.reply || "…", false);
      history.push({ role: "assistant", content: data.reply || "" });
      if (data.kind === "plan" && data.plan) {
        showPlan(data.plan);
      } else if (data.kind === "result") {
        hidePlan();
      }
      if (data.links) appendLinks(data.links);
      if (data.navigate && typeof data.navigate === "string") {
        var href = data.navigate;
        setTimeout(function () {
          window.location.href = href;
        }, 450);
      }
    } catch (err) {
      appendBubble("assistant", "Network error: " + (err.message || err), false);
    } finally {
      setBusy(false);
    }
  }

  async function confirmPlan(accepted, skipUserBubble) {
    if (!pendingToken) return;
    var token = pendingToken;
    hidePlan();
    setBusy(true);
    if (!skipUserBubble) {
      appendBubble(
        "user",
        accepted ? "Yes — go ahead" : "No — cancel",
        false
      );
    }
    try {
      var data = await postJSON("/assistant/confirm", {
        token: token,
        accepted: !!accepted,
      });
      appendBubble("assistant", data.reply || "Done.", false);
      history.push({ role: "assistant", content: data.reply || "" });
      if (data.links) appendLinks(data.links);
      // After create/edit, open the record (job/client/task)
      if (accepted && data.navigate && typeof data.navigate === "string") {
        var href = data.navigate;
        setTimeout(function () {
          window.location.href = href;
        }, 600);
      }
    } catch (err) {
      appendBubble("assistant", "Confirm failed: " + (err.message || err), false);
    } finally {
      setBusy(false);
    }
  }

  function openPanel() {
    var panel = $("#ai-panel");
    var fab = $("#ai-fab");
    if (panel) {
      panel.hidden = false;
      panel.setAttribute("aria-hidden", "false");
    }
    if (fab) fab.setAttribute("aria-expanded", "true");
    var input = $("#ai-input");
    if (input) setTimeout(function () { input.focus(); }, 50);
  }

  function closePanel() {
    var panel = $("#ai-panel");
    var fab = $("#ai-fab");
    if (panel) {
      panel.hidden = true;
      panel.setAttribute("aria-hidden", "true");
    }
    if (fab) fab.setAttribute("aria-expanded", "false");
    stopVoice();
  }

  function stopVoice() {
    if (recognition) {
      try {
        recognition.stop();
      } catch (e) {}
    }
    setListening(false);
  }

  function startVoice() {
    if (!speechSupported()) {
      var st = $("#ai-status");
      if (st) st.textContent = "Speech not available in this browser — type instead.";
      return;
    }
    if (listening) {
      stopVoice();
      return;
    }
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new SR();
    recognition.lang = "en-GB";
    recognition.continuous = false;
    recognition.interimResults = true;
    var finalText = "";

    recognition.onstart = function () {
      setListening(true);
      openPanel();
    };
    recognition.onerror = function (ev) {
      var st = $("#ai-status");
      if (st) st.textContent = "Voice: " + (ev.error || "error");
      setListening(false);
    };
    recognition.onend = function () {
      setListening(false);
      if (finalText.trim()) {
        var input = $("#ai-input");
        if (input) input.value = finalText.trim();
        sendMessage(finalText.trim());
      }
    };
    recognition.onresult = function (event) {
      var interim = "";
      for (var i = event.resultIndex; i < event.results.length; i++) {
        var t = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalText += t + " ";
        else interim += t;
      }
      var input = $("#ai-input");
      if (input) input.value = (finalText + interim).trim();
      var st = $("#ai-status");
      if (st) st.textContent = interim ? "Listening… " + interim : "Listening…";
    };
    try {
      recognition.start();
    } catch (e) {
      setListening(false);
    }
  }

  function init() {
    var root = $("#ai-assistant");
    if (!root) return;
    loadPending();

    var fab = $("#ai-fab");
    var closeBtn = $("#ai-close");
    var form = $("#ai-form");
    var mic = $("#ai-mic");
    var yes = $("#ai-plan-yes");
    var no = $("#ai-plan-no");

    if (fab) {
      fab.addEventListener("click", function () {
        var panel = $("#ai-panel");
        if (panel && !panel.hidden) closePanel();
        else openPanel();
      });
    }
    if (closeBtn) closeBtn.addEventListener("click", closePanel);
    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var input = $("#ai-input");
        sendMessage(input ? input.value : "");
      });
    }
    if (mic) {
      if (!speechSupported()) {
        mic.title = "Speech not supported — type instead";
        mic.classList.add("is-disabled");
      }
      mic.addEventListener("click", function (e) {
        e.preventDefault();
        startVoice();
      });
    }
    if (yes) yes.addEventListener("click", function () { confirmPlan(true); });
    if (no) no.addEventListener("click", function () { confirmPlan(false); });

    // Welcome once
    fetch("/assistant/status", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (st) {
        var tip = $("#ai-welcome");
        if (!tip) return;
        if (st.llm) {
          tip.textContent =
            "I’m Si — full CRM co-pilot. Go to WIP · Open client · Create/edit jobs · Fill SAR dates · Mark complete. Yes to confirm.";
        } else if (st.heuristic) {
          tip.textContent =
            "I’m Si. Navigate, find, create/edit jobs & tasks, fill dates (SAR rules), mark complete. Yes confirms. CH only if you ask.";
        } else {
          tip.textContent = "Si isn’t fully configured — add XAI_API_KEY or enable heuristic mode.";
        }
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
