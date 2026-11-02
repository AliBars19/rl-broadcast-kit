/* Program panel of the control dock (control.html).
   Reads state from the "sb-state" event, sends countdown / program commands to POST /cmd. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var FIELDS = ["pgStage", "pgHeadline", "pgSubline", "pgBlue", "pgOrange"];
  var MAX_MINUTES = 120;
  var program = null, connected = false, msgTimer = null;

  function showError(text) {
    $("pgMsg").textContent = text;
    clearTimeout(msgTimer);
    if (text) msgTimer = setTimeout(function () { $("pgMsg").textContent = ""; }, 6000);
  }

  // Resolves to an error string, or "" when the server accepted the command.
  function post(action, value) {
    var body = { action: action };
    if (value !== undefined) body.value = value;
    return fetch("/cmd", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) {
        if (r.ok) return "";
        return r.json().then(function (j) { return j.error || ("HTTP " + r.status); }, function () { return "HTTP " + r.status; });
      })
      .catch(function () { return "Could not reach the server"; });
  }

  function run(action, value) {
    showError("");
    return post(action, value).then(function (err) { if (err) showError("Rejected: " + err); return !err; });
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function formatRemaining(ms) {
    var s = Math.ceil(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return (h ? h + ":" + pad(m) : pad(m)) + ":" + pad(s % 60);
  }

  function renderReadout() {
    var el = $("pgReadout");
    if (!program || program.endsAt === null) {
      el.textContent = "No countdown";
      el.className = "readout idle";
      return;
    }
    var left = program.endsAt - Date.now();
    el.className = "readout";
    el.textContent = left > 0 ? formatRemaining(left) : "LIVE";
  }

  function fillFields(p) {
    var values = {
      pgStage: p.stage,
      pgHeadline: p.headline || "",
      pgSubline: p.subline || "",
      pgBlue: p.firstUp.blue,
      pgOrange: p.firstUp.orange
    };
    FIELDS.forEach(function (id) {
      var el = $(id);
      if (el.dataset.dirty !== "1" && document.activeElement !== el) el.value = values[id];
    });
  }

  function onState(st) {
    program = st && st.program ? st.program : null;
    if (!program) return;
    fillFields(program);
    $("pgClear").disabled = !connected || program.endsAt === null;
    renderReadout();
  }

  function collect() {
    return {
      stage: $("pgStage").value,
      headline: $("pgHeadline").value,
      subline: $("pgSubline").value,
      firstUp: { blue: $("pgBlue").value, orange: $("pgOrange").value }
    };
  }

  function save() {
    run("program", collect()).then(function (ok) {
      if (!ok) return;
      FIELDS.forEach(function (id) { delete $(id).dataset.dirty; });
      if (program) fillFields(program);
    });
  }

  function startCustom() {
    var raw = $("pgMin").value.trim(), n = Number(raw);
    if (raw === "" || !Number.isInteger(n) || n < 0 || n > MAX_MINUTES) {
      showError("Minutes must be a whole number from 0 to " + MAX_MINUTES);
      return;
    }
    run("countdown", n * 60);
  }

  function bind() {
    $("pgQuick").addEventListener("click", function (e) {
      var b = e.target.closest ? e.target.closest("button") : null;
      if (b && !b.disabled) run("countdown", Number(b.dataset.min) * 60);
    });
    $("pgStart").addEventListener("click", startCustom);
    $("pgClear").addEventListener("click", function () { run("countdownClear"); });
    $("pgSave").addEventListener("click", save);
    FIELDS.forEach(function (id) {
      $(id).addEventListener("input", function () { $(id).dataset.dirty = "1"; });
      $(id).addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); save(); } });
    });
    $("pgMin").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); startCustom(); } });
    document.addEventListener("sb-state", function (e) { onState(e.detail); });
    document.addEventListener("sb-conn", function (e) { connected = !!e.detail; });
  }

  bind();
  setInterval(renderReadout, 250);
})();
