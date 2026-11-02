/* Program screens: shared helpers (scoreboard feed, promo reel, clock).
   Needs ../config.js and ../common.js loaded first. Local only: the one network
   address used is the scoreboard helper on 127.0.0.1:49125. Never throws. */
(function () {
  var S = window.STREAM || {};
  var DEF = S.programDefaults || {};
  var SB = (window.OV && OV.param("sb")) || "http://127.0.0.1:49125/events";
  var DIGITS_MAX = 7;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // Config paths are relative to the repo root; program pages live one folder down.
  function asset(p) {
    if (!p) return "";
    return /^([a-z]+:|\/)/i.test(p) ? p : "../" + p;
  }

  function mmss(sec) {
    var s = Math.max(0, Math.floor(sec));
    return OV.pad(Math.floor(s / 60)) + ":" + OV.pad(s % 60);
  }

  // ---- scoreboard feed: SSE with manual reconnect and backoff -----------------
  // onState(state|null): called with each state, and with null while unreachable.
  function connect(onState) {
    var delay = 1000, es = null, timer = null;
    function safe(v) { try { onState(v); } catch (e) { /* a page bug must not kill the feed */ } }
    function retry() {
      safe(null);
      if (timer) return;
      timer = setTimeout(function () { timer = null; open(); }, delay);
      delay = Math.min(delay * 2, 30000);
    }
    function open() {
      try {
        es = new EventSource(SB);
        es.onmessage = function (ev) {
          var st;
          try { st = JSON.parse(ev.data); } catch (e) { return; }
          if (st && typeof st === "object") { delay = 1000; safe(st); }
        };
        es.onerror = function () { try { es.close(); } catch (e) { /* closed already */ } retry(); };
      } catch (e) { retry(); }
    }
    safe(null);
    open();
  }

  // ---- animated text swap (stage tag, labels) ----------------------------------
  function setText(node, text) {
    if (!node || node.textContent === text) return;
    node.textContent = text;
    node.classList.remove("swap-in");
    void node.offsetWidth;
    node.classList.add("swap-in");
  }

  // ---- filled clock: digits slide on change ----------------------------------
  function makeClock(root) {
    var cells = [];
    var current = "";
    function build(str) {
      root.textContent = "";
      cells = [];
      for (var i = 0; i < str.length; i++) {
        var colon = str.charAt(i) === ":";
        var cell = el("span", colon ? "colon" : "dg");
        if (!colon) cell.appendChild(el("span", "", str.charAt(i)));
        else cell.textContent = ":";
        root.appendChild(cell);
        cells.push(cell);
      }
    }
    function swap(cell, ch) {
      var old = cell.firstChild;
      var neu = el("span", "enter", ch);
      cell.appendChild(neu);
      if (old) {
        old.className = "leave";
        setTimeout(function () { if (old.parentNode) old.parentNode.removeChild(old); }, 340);
      }
    }
    function set(str) {
      if (str === current) return;
      if (str.length !== current.length || str.length > DIGITS_MAX) { build(str); current = str; return; }
      for (var i = 0; i < str.length; i++) {
        if (str.charAt(i) !== current.charAt(i) && str.charAt(i) !== ":") swap(cells[i], str.charAt(i));
      }
      current = str;
    }
    return { set: set };
  }

  // ---- promo reel: one card at a time, slide/fade every intervalMs --------------
  function cardNode(p) {
    var c = el("div", "card");
    if (p.tag) c.appendChild(el("span", "ctag", p.tag));
    var t = el("div", "ctext");
    t.appendChild(el("b", "", p.text || ""));
    if (p.sub) t.appendChild(el("i", "", p.sub));
    c.appendChild(t);
    if (p.logo) {
      var img = el("img");
      img.alt = "";
      img.src = asset(p.logo);
      c.appendChild(img);
    }
    return c;
  }

  function makeReel(root, promos, intervalMs) {
    var list = (promos || []).filter(function (p) { return p && p.text; });
    if (!list.length) return;
    var ticks = el("div", "reel-ticks");
    list.forEach(function () { ticks.appendChild(el("i")); });
    root.appendChild(ticks);
    var idx = (parseInt(OV.param("card"), 10) || 1) - 2, shown = null;  // ?card=N starts on card N (testing)
    function next() {
      idx = (idx + 1) % list.length;
      var prev = shown;
      shown = cardNode(list[idx]);
      root.insertBefore(shown, ticks);
      if (prev) {
        prev.classList.add("out");
        setTimeout(function () { if (prev.parentNode) prev.parentNode.removeChild(prev); }, 520);
      }
      Array.prototype.forEach.call(ticks.children, function (t, i) { t.classList.toggle("on", i === idx); });
    }
    next();
    if (list.length > 1) setInterval(next, intervalMs || 8000);
  }

  // ---- shared header pieces ------------------------------------------------------
  // Event name with the last word in amber ("YOUR EVENT" + "26").
  function fillWordmark(node) {
    if (!node) return;
    var name = DEF.eventName || "YOUR EVENT 26";
    var i = name.lastIndexOf(" ");
    node.textContent = "";
    node.appendChild(document.createTextNode(i > 0 ? name.slice(0, i + 1) : name));
    if (i > 0) node.appendChild(el("span", "a", name.slice(i + 1)));
  }

  // "A | B" becomes A, an amber bar, B.
  function fillSub(node, text) {
    if (!node) return;
    node.textContent = "";
    String(text || "").split("|").forEach(function (part, i) {
      if (i) node.appendChild(el("i", "bar"));
      node.appendChild(document.createTextNode(part.trim()));
    });
  }

  // The sponsor is OPTIONAL. config.js `sponsor: null` (or an object with no name and no logo)
  // means "no sponsor": every PRESENTED BY spot is removed from the page.
  function hasSponsor() {
    var sp = S.sponsor;
    return !!(sp && (sp.logo || sp.lockup || (sp.name && String(sp.name).trim())));
  }

  // Remove the whole sponsor spot (label, image and the box around it) so no empty
  // label or gap is left behind.
  function dropSpot(node) {
    var spot = node.closest(".presented, .sponsor") || node;
    if (spot.parentNode) spot.parentNode.removeChild(spot);
  }

  // Sponsor image, or its name as text when no logo is configured; removes the spot when no
  // sponsor is configured. big = true picks the larger lockup (large spots), falling back to logo.
  function fillSponsor(node, big) {
    if (!node) return;
    if (!hasSponsor()) { dropSpot(node); return; }
    var sp = S.sponsor;
    var src = (big && sp.lockup) || sp.logo || sp.lockup;
    if (src) node.src = asset(src);
    else node.replaceWith(el("b", "", String(sp.name).trim()));
  }

  // Shrink text until it fits its box (never truncates; stops at min px).
  function fit(box, node, max, min) {
    var size = max;
    var cs = getComputedStyle(box);
    var room = box.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    node.style.fontSize = size + "px";
    while (size > min && node.offsetWidth > room) {
      size -= 2;
      node.style.fontSize = size + "px";
    }
  }

  // ---- stage + event text from state or config -----------------------------------
  function stageOf(state) {
    var p = state && state.program;
    return (p && typeof p.stage === "string" && p.stage.trim()) || DEF.stage || "";
  }

  // Entrance animations are finite; snap leftovers so a still capture is the final frame.
  // Live OBS never reaches the delay with anything pending, so this is a no-op there.
  // ?still=1 repeats the snap every 250 ms (stills of live-changing digits); never used in OBS.
  function finishEntrances(ms) {
    function snap() {
      try {
        document.getAnimations().forEach(function (a) {
          var t = a.effect && a.effect.getComputedTiming();
          if (t && t.iterations !== Infinity) a.finish();
        });
      } catch (e) { /* not supported: ignore */ }
    }
    setTimeout(snap, ms || 3600);
    if (OV.param("still")) setInterval(snap, 250);
  }

  window.PROG = {
    S: S, DEF: DEF, el: el, asset: asset, mmss: mmss, connect: connect, setText: setText,
    makeClock: makeClock, makeReel: makeReel, stageOf: stageOf, finishEntrances: finishEntrances,
    fillWordmark: fillWordmark, fillSub: fillSub, fillSponsor: fillSponsor, hasSponsor: hasSponsor, fit: fit
  };
})();
