/* Program lower bar (lower-bar.html). Reads state.program from the scoreboard server (SSE).
   Never throws: bad data or an unreachable server falls back to config.js text. */
(function () {
  "use strict";
  var S = window.STREAM || {};
  var SB = S.scoreboard || {};
  var SWAP_MS = 400;
  var FIRST_WAIT_MS = 800;
  var MAX_BACKOFF_MS = 15000;
  var $ = function (id) { return document.getElementById(id); };

  var slots = {};
  ["stage", "blue", "orange", "label", "headline", "subline"].forEach(function (id) {
    slots[id] = { el: $(id), text: null };
  });
  var gold = $("gold"), timeEl = $("time");

  // Brand pieces and the optional sponsor all come from config.js.
  (function brand() {
    var H = S.handles || {};
    var handle = H.twitch || H.youtube || H.tiktok || "";
    $("handle").textContent = handle.toUpperCase();
    if (!handle) $("handle").style.display = "none";
    $("mark").textContent = String(S.mark || (S.channel || "?").charAt(0)).charAt(0).toUpperCase();
    $("bname").textContent = String(S.channel || "").toUpperCase();
    var sp = S.sponsor, src = sp && (sp.logo || sp.lockup);
    if (src) {
      $("sponsorImg").src = /^([a-z]+:|\/)/i.test(src) ? src : "../" + src;
    } else {
      [$("sponsorSpot"), $("sponsorRule")].forEach(function (n) { if (n && n.parentNode) n.parentNode.removeChild(n); });
    }
  })();
  var rendered = false, endsAt = null;

  function fallback() {
    return {
      endsAt: null, label: "KICKOFF", stage: S.event || "", headline: SB.tab || "", subline: S.tagline || "",
      firstUp: { blue: (SB.blue && SB.blue.name) || "BLUE", orange: (SB.orange && SB.orange.name) || "ORANGE" }
    };
  }

  // Shrink the font until the text fits its slot, down to about half size.
  function fit(span) {
    span.style.fontSize = "";
    var size = parseFloat(getComputedStyle(span).fontSize) || 16, min = size * 0.55;
    while (span.scrollWidth > span.clientWidth && size > min) {
      size -= 1;
      span.style.fontSize = size + "px";
    }
  }

  function dropLater(node) {
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, SWAP_MS + 60);
  }

  function setText(id, text, animate) {
    var slot = slots[id];
    if (!slot.el || slot.text === text) return;
    var old = slot.el.querySelector("span:not(.out)");
    var span = document.createElement("span");
    span.textContent = text;
    slot.el.appendChild(span);
    fit(span);
    slot.text = text;
    if (!old) return;
    if (!animate) { old.parentNode.removeChild(old); return; }
    old.classList.add("out");
    span.classList.add("in");
    dropLater(old);
  }

  function refit(id) {
    var span = slots[id].el.querySelector("span:not(.out)");
    if (span) fit(span);
  }

  function pickName(v, fb) { return typeof v === "string" && v.trim() ? v.trim() : fb; }

  function formatRemaining(totalSeconds) {
    var h = Math.floor(totalSeconds / 3600), m = Math.floor((totalSeconds % 3600) / 60), s = totalSeconds % 60;
    return (h ? h + ":" + OV.pad(m) : OV.pad(m)) + ":" + OV.pad(s);
  }

  function tick() {
    if (endsAt === null) return;
    var left = Math.ceil((endsAt - Date.now()) / 1000);
    timeEl.textContent = left > 0 ? formatRemaining(left) : "LIVE";
    timeEl.className = "gtime" + (left >= 3600 ? " long" : "");
  }

  function setMode(hasTimer) {
    var mode = hasTimer ? "timer" : "label";
    if (gold.getAttribute("data-mode") === mode) return;
    gold.setAttribute("data-mode", mode);
    refit("label");
  }

  function render(p, animate) {
    var fb = fallback();
    endsAt = typeof p.endsAt === "number" ? p.endsAt : null;
    setText("stage", p.stage || "", animate);
    setText("blue", pickName(p.firstUp && p.firstUp.blue, fb.firstUp.blue), animate);
    setText("orange", pickName(p.firstUp && p.firstUp.orange, fb.firstUp.orange), animate);
    setText("label", p.label || "KICKOFF", animate);
    setText("headline", p.headline || "", animate);
    setText("subline", p.subline || "", animate);
    setMode(endsAt !== null);
    tick();
    if (!rendered) {
      rendered = true;
      requestAnimationFrame(function () { requestAnimationFrame(function () { document.body.classList.add("ready"); }); });
    }
  }

  function onState(raw) {
    var st;
    try { st = JSON.parse(raw); } catch (e) { return; }
    var p = st && typeof st.program === "object" && st.program ? st.program : null;
    if (!p) return;
    try { render(p, rendered); } catch (e) { console.log("lower bar render failed: " + e.message); }
  }

  function eventsUrl() {
    var sb = OV.param("sb");
    if (sb) return sb;
    var port = parseInt(OV.param("port"), 10);
    return "http://127.0.0.1:" + (port > 0 && port < 65536 ? port : 49125) + "/events";
  }

  var es = null, delay = 1000;
  function retry() {
    if (es) { es.close(); es = null; }
    setTimeout(connect, delay);
    delay = Math.min(delay * 2, MAX_BACKOFF_MS);
  }
  function connect() {
    try { es = new EventSource(eventsUrl()); } catch (e) { retry(); return; }
    es.onopen = function () { delay = 1000; };
    es.onmessage = function (ev) { delay = 1000; onState(ev.data); };
    es.onerror = retry;
  }

  if (OV.param("still") === "1") document.body.classList.add("still");
  setInterval(tick, 250);
  setTimeout(function () { if (!rendered) render(fallback(), false); }, FIRST_WAIT_MS);
  connect();
})();
