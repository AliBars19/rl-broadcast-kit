/* Facecam scene (program/facecam.html): fills the side column from config.js and the
   scoreboard helper. Needs program.js (PROG) loaded first. Never throws; with the helper
   down the page keeps the last values it saw, or the config.js fallbacks. */
(function () {
  "use strict";
  var P = window.PROG;
  if (!P) return;
  var S = P.S, DEF = P.DEF;
  var $ = function (id) { return document.getElementById(id); };
  var state = null;

  // ---- static text: wordmark, sub line, sponsor, socials ----
  function setWordmark() {
    var name = DEF.eventName || "YOUR EVENT 26";
    var i = name.lastIndexOf(" ");
    var w = $("wordmark");
    w.textContent = "";
    w.appendChild(document.createTextNode(i > 0 ? name.slice(0, i + 1) : name));
    if (i > 0) w.appendChild(P.el("span", "a", name.slice(i + 1)));
    $("eventSub").textContent = DEF.eventSub || "";
  }

  function setSponsor() {
    P.fillSponsor($("sponsor"));
  }

  function setSocials() {
    var box = $("socials");
    (window.OV && OV.platforms || []).forEach(function (p) {
      var card = P.el("div", "panel social");
      card.appendChild(P.el("small", "", p.label.toUpperCase()));
      card.appendChild(P.el("b", "", p.handle));
      box.appendChild(card);
    });
  }

  // ---- series card ----
  function whole(v) { return Number.isInteger(v) && v >= 0 ? v : 0; }

  function pips(box, count, wins) {
    box.textContent = "";
    for (var i = 0; i < count; i++) box.appendChild(P.el("i", i < wins ? "on" : ""));
  }

  // shrink a long team name to its slot (down to 20 px, then ellipsis)
  function fit(node) {
    var size = 32;
    node.style.fontSize = size + "px";
    while (node.scrollWidth > node.clientWidth && size > 20) { size -= 2; node.style.fontSize = size + "px"; }
  }

  function renderSeries() {
    var cfg = S.scoreboard || {};
    var p = (state && state.program) || {};
    var fu = p.firstUp || {};
    var nB = (fu.blue || "").trim() || (cfg.blue && cfg.blue.name) || "BLUE";
    var nO = (fu.orange || "").trim() || (cfg.orange && cfg.orange.name) || "ORANGE";
    var bo = state && [3, 5, 7].indexOf(state.bestOf) >= 0 ? state.bestOf : (cfg.bestOf || 3);
    var wB = state && state.blue ? whole(state.blue.wins) : whole(cfg.blue && cfg.blue.wins);
    var wO = state && state.orange ? whole(state.orange.wins) : whole(cfg.orange && cfg.orange.wins);
    var need = Math.floor(bo / 2) + 1;
    P.setText($("nameB"), nB.toUpperCase());
    P.setText($("nameO"), nO.toUpperCase());
    fit($("nameB"));
    fit($("nameO"));
    P.setText($("winsB"), String(wB));
    P.setText($("winsO"), String(wO));
    P.setText($("gameN"), "GAME " + (wB + wO + 1));
    P.setText($("boN"), "BEST OF " + bo);
    pips($("pipsB"), need, wB);
    pips($("pipsO"), need, wO);
  }

  function onState(st) {
    if (st) state = st;
    P.setText($("stage"), P.stageOf(state).toUpperCase());
    renderSeries();
  }

  setWordmark();
  setSponsor();
  setSocials();
  onState(null);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(renderSeries);
  P.connect(onState);
  P.finishEntrances(3600);
})();
