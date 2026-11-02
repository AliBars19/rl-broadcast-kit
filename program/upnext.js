/* Program screens: UP NEXT (upnext.html). Full screen, one bar along the bottom.
   Reads state.program, state.blue.wins, state.orange.wins and state.bestOf from the scoreboard feed,
   with config.js fallbacks. Keeps the last good state while the feed is down. Never throws.
   Loads after program.js. */
(function () {
  "use strict";
  var P = window.PROG;
  if (!P) return;
  var SWAP_MS = 400;
  var TICK_MS = 200;
  var SLOT_IDS = ["nameB", "nameO", "ggame", "headline", "subline"];
  var $ = function (id) { return document.getElementById(id); };

  var slots = {};
  SLOT_IDS.forEach(function (id) { slots[id] = { el: $(id), text: null }; });
  var gold = $("gold"), gtime = $("gtime");
  var state = null;      // last good state, kept while the feed is down
  var rendered = false;

  function whole(v) { return Number.isInteger(v) && v >= 0 ? v : 0; }
  function str(v) { return typeof v === "string" ? v.trim() : ""; }

  // Everything the page shows, resolved from state first and config second.
  function read(st) {
    var cfg = P.S.scoreboard || {};
    var prog = (st && st.program) || {};
    var fu = prog.firstUp || {};
    var bo = st && [3, 5, 7].indexOf(st.bestOf) >= 0 ? st.bestOf : (cfg.bestOf || 3);
    var winsB = st && st.blue ? whole(st.blue.wins) : whole(cfg.blue && cfg.blue.wins);
    var winsO = st && st.orange ? whole(st.orange.wins) : whole(cfg.orange && cfg.orange.wins);
    var game = winsB + winsO + 1;
    var stage = P.stageOf(st).toUpperCase();
    return {
      nameB: (str(fu.blue) || (cfg.blue && cfg.blue.name) || "BLUE").toUpperCase(),
      nameO: (str(fu.orange) || (cfg.orange && cfg.orange.name) || "ORANGE").toUpperCase(),
      game: game,
      endsAt: typeof prog.endsAt === "number" && isFinite(prog.endsAt) ? prog.endsAt : null,
      headline: str(prog.headline) || "GAME " + game + " COMING UP!",
      subline: str(prog.subline) || ["SERIES " + winsB + "-" + winsO, "BEST OF " + bo, stage].filter(Boolean).join(" | ")
    };
  }

  // Shrink the font until the text fits its slot, down to about half size.
  function fit(span) {
    span.style.fontSize = "";
    var size = parseFloat(getComputedStyle(span).fontSize) || 16, min = size * 0.5;
    while (span.scrollWidth > span.clientWidth && size > min) {
      size -= 1;
      span.style.fontSize = size + "px";
    }
  }

  function dropLater(node) {
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, SWAP_MS + 60);
  }

  function setSlot(id, text, animate) {
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

  function refitAll() {
    SLOT_IDS.forEach(function (id) {
      var span = slots[id].el.querySelector("span:not(.out)");
      if (span) fit(span);
    });
  }

  function clockText(sec) {
    var h = Math.floor(sec / 3600);
    return h ? h + ":" + OV.pad(Math.floor((sec % 3600) / 60)) + ":" + OV.pad(sec % 60) : P.mmss(sec);
  }

  // Gold block: BACK IN mm:ss while endsAt is in the future, otherwise GAME N.
  function tick() {
    var d = read(state);
    var left = d.endsAt === null ? 0 : Math.ceil((d.endsAt - Date.now()) / 1000);
    if (left > 0) {
      if (gold.getAttribute("data-mode") !== "timer") gold.setAttribute("data-mode", "timer");
      gtime.textContent = clockText(left);
      gtime.className = "gtime" + (left >= 3600 ? " long" : "");
    } else if (gold.getAttribute("data-mode") !== "game") {
      gold.setAttribute("data-mode", "game");
    }
    setSlot("ggame", "GAME " + d.game, rendered);
  }

  function render() {
    var d = read(state);
    var animate = rendered;
    setSlot("nameB", d.nameB, animate);
    setSlot("nameO", d.nameO, animate);
    setSlot("headline", d.headline, animate);
    setSlot("subline", d.subline, animate);
    tick();
    refitAll();   // both names are in place now, so a squeezed slot gets its final size
    refitAll();   // second pass: a shrunk neighbour frees room, so the first can grow back
    rendered = true;
  }

  function onState(st) {
    if (st) state = st;
    try { render(); } catch (e) { console.log("up next render failed: " + e.message); }
  }

  $("mark").textContent = String(P.S.mark || (P.S.channel || "?").charAt(0)).charAt(0).toUpperCase();
  $("bname").textContent = String(P.S.channel || "").toUpperCase();
  P.fillWordmark($("wordmark"));
  P.fillSponsor($("sponsor"), true);     // centre: the lockup
  P.fillSponsor($("sponsorBar"));        // bottom bar: the wordmark
  render();
  setInterval(tick, TICK_MS);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(refitAll);
  P.connect(onState);
  P.finishEntrances(3600);
})();
