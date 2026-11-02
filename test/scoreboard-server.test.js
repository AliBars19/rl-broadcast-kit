"use strict";
// Run with: node --test test/
// Unknown keys: sanitize() (the state file) ignores them; the `program` command rejects them.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const net = require("net");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

const { defaults, sanitize, apply, BadCmd } = require("../scoreboard-server.js");

const NOW = 1_800_000_000_000;
const run = (cmd, state) => apply(cmd, state || defaults(), NOW);

test("defaults include the program object", () => {
  assert.deepStrictEqual(defaults().program, {
    endsAt: null, label: "KICKOFF", stage: "", headline: "", subline: "",
    firstUp: { blue: "", orange: "" }
  });
});

test("defaults return a fresh object each call", () => {
  const a = defaults(); a.program.firstUp.blue = "x";
  assert.strictEqual(defaults().program.firstUp.blue, "");
});

test("sanitize fills program when the state file predates it", () => {
  const s = sanitize({ blue: { wins: 1 }, orange: { wins: 0 }, bestOf: 5, tab: "t", presets: [] });
  assert.deepStrictEqual(s.program, defaults().program);
  assert.strictEqual(s.blue.wins, 1);
});

test("sanitize trims and clamps program strings", () => {
  const s = sanitize({ program: {
    label: "  " + "L".repeat(30) + "  ", stage: "S".repeat(60), headline: "H".repeat(100),
    subline: "  ok  ", firstUp: { blue: "B".repeat(40), orange: " O " }
  } });
  assert.strictEqual(s.program.label, "L".repeat(20));
  assert.strictEqual(s.program.stage, "S".repeat(40));
  assert.strictEqual(s.program.headline, "H".repeat(70));
  assert.strictEqual(s.program.subline, "ok");
  assert.strictEqual(s.program.firstUp.blue, "B".repeat(28));
  assert.strictEqual(s.program.firstUp.orange, "O");
});

test("sanitize rejects wrong types by falling back to defaults", () => {
  const s = sanitize({ program: {
    endsAt: "soon", label: 5, stage: null, headline: {}, subline: [], firstUp: "blue vs orange"
  } });
  assert.deepStrictEqual(s.program, defaults().program);
});

test("sanitize accepts only non-negative integer endsAt", () => {
  assert.strictEqual(sanitize({ program: { endsAt: NOW } }).program.endsAt, NOW);
  for (const bad of [-5, 1.5, NaN, Infinity, "12", true]) {
    assert.strictEqual(sanitize({ program: { endsAt: bad } }).program.endsAt, null, String(bad));
  }
});

test("sanitize ignores unknown program keys", () => {
  const s = sanitize({ program: { label: "A", extra: "x", firstUp: { blue: "B", green: "G" } } });
  assert.strictEqual(s.program.label, "A");
  assert.ok(!("extra" in s.program));
  assert.ok(!("green" in s.program.firstUp));
});

test("sanitize survives garbage input", () => {
  assert.deepStrictEqual(sanitize(null), defaults());
  assert.deepStrictEqual(sanitize("x"), defaults());
  assert.deepStrictEqual(sanitize({ program: 7 }).program, defaults().program);
});

test("countdown sets endsAt to now plus seconds", () => {
  assert.strictEqual(run({ action: "countdown", value: 300 }).program.endsAt, NOW + 300000);
  assert.strictEqual(run({ action: "countdown", value: 0 }).program.endsAt, NOW);
  assert.strictEqual(run({ action: "countdown", value: 7200 }).program.endsAt, NOW + 7200000);
});

test("countdown defaults to the real clock", () => {
  const before = Date.now();
  const at = apply({ action: "countdown", value: 10 }, defaults()).program.endsAt;
  assert.ok(at >= before + 10000 && at <= Date.now() + 10000);
});

test("countdown rejects out-of-range and non-integer values", () => {
  for (const bad of [-1, 7201, 1.5, "60", null, undefined, NaN, Infinity, [60], {}]) {
    assert.throws(() => run({ action: "countdown", value: bad }), BadCmd, String(bad));
  }
});

test("countdownClear sets endsAt to null", () => {
  const withTimer = run({ action: "countdown", value: 60 });
  assert.strictEqual(run({ action: "countdownClear" }, withTimer).program.endsAt, null);
});

test("program merges a partial object and keeps the rest", () => {
  const s1 = run({ action: "program", value: { headline: "  Opening match  ", label: "NEXT GAME" } });
  assert.strictEqual(s1.program.headline, "Opening match");
  assert.strictEqual(s1.program.label, "NEXT GAME");
  assert.strictEqual(s1.program.stage, "");
  const s2 = run({ action: "program", value: { firstUp: { blue: "Hawks" } } }, s1);
  assert.deepStrictEqual(s2.program.firstUp, { blue: "Hawks", orange: "" });
  const s3 = run({ action: "program", value: { firstUp: { orange: "Owls" } } }, s2);
  assert.deepStrictEqual(s3.program.firstUp, { blue: "Hawks", orange: "Owls" });
  assert.strictEqual(s3.program.headline, "Opening match");
});

test("program accepts empty strings to clear a field", () => {
  const s1 = run({ action: "program", value: { subline: "x" } });
  assert.strictEqual(run({ action: "program", value: { subline: "" } }, s1).program.subline, "");
});

test("program accepts endsAt as epoch ms or null", () => {
  const s1 = run({ action: "program", value: { endsAt: NOW + 5 } });
  assert.strictEqual(s1.program.endsAt, NOW + 5);
  assert.strictEqual(run({ action: "program", value: { endsAt: null } }, s1).program.endsAt, null);
});

test("program rejects over-long strings with a clear message", () => {
  const cases = [["label", 21, 20], ["stage", 41, 40], ["headline", 71, 70], ["subline", 91, 90]];
  for (const [k, n, max] of cases) {
    assert.throws(() => run({ action: "program", value: { [k]: "x".repeat(n) } }), (e) =>
      e instanceof BadCmd && e.message.includes(k) && e.message.includes(String(max)));
  }
  for (const side of ["blue", "orange"]) {
    assert.throws(() => run({ action: "program", value: { firstUp: { [side]: "x".repeat(29) } } }),
      (e) => e instanceof BadCmd && e.message.includes(side) && e.message.includes("28"));
  }
});

test("program measures length after trimming", () => {
  const s = run({ action: "program", value: { label: "  " + "x".repeat(20) + "  " } });
  assert.strictEqual(s.program.label, "x".repeat(20));
});

test("program rejects wrong types", () => {
  const bad = [
    { label: 5 }, { stage: null }, { headline: {} }, { subline: [] },
    { firstUp: "a vs b" }, { firstUp: null }, { firstUp: { blue: 1 } },
    { endsAt: "soon" }, { endsAt: -1 }, { endsAt: 1.5 }
  ];
  for (const value of bad) assert.throws(() => run({ action: "program", value }), BadCmd, JSON.stringify(value));
  for (const value of [null, "x", 5, [], [1]]) {
    assert.throws(() => run({ action: "program", value }), BadCmd, JSON.stringify(value));
  }
});

test("program rejects unknown keys and empty objects", () => {
  assert.throws(() => run({ action: "program", value: { nope: "x" } }), /unknown program field: nope/);
  assert.throws(() => run({ action: "program", value: { firstUp: { green: "x" } } }), /unknown firstUp field: green/);
  assert.throws(() => run({ action: "program", value: {} }), /no fields/);
});

test("a rejected program command changes nothing", () => {
  const before = run({ action: "program", value: { headline: "keep" } });
  const snapshot = JSON.stringify(before);
  assert.throws(() => run({ action: "program", value: { headline: "ok", label: "x".repeat(99) } }, before), BadCmd);
  assert.strictEqual(JSON.stringify(before), snapshot);
});

test("apply never mutates its input", () => {
  const s = defaults();
  const snapshot = JSON.stringify(s);
  run({ action: "program", value: { headline: "a", firstUp: { blue: "b" } } }, s);
  run({ action: "countdown", value: 5 }, s);
  assert.strictEqual(JSON.stringify(s), snapshot);
});

test("old actions still work", () => {
  let s = run({ action: "blue+" });
  assert.strictEqual(s.blue.wins, 1);
  s = run({ action: "blue+" }, s);
  s = run({ action: "blue+" }, s);
  assert.strictEqual(s.blue.wins, 2, "capped at wins needed for bo3");
  s = run({ action: "swap" }, s);
  assert.deepStrictEqual([s.blue.wins, s.orange.wins], [0, 2]);
  s = run({ action: "bestOf", value: 5 }, s);
  assert.strictEqual(s.bestOf, 5);
  s = run({ action: "orange-" }, s);
  assert.strictEqual(s.orange.wins, 1);
  s = run({ action: "tab", value: "  SERIES A  " }, s);
  assert.strictEqual(s.tab, "SERIES A");
  assert.deepStrictEqual(s.presets, ["SERIES A"]);
  s = run({ action: "reset" }, s);
  assert.deepStrictEqual([s.blue.wins, s.orange.wins], [0, 0]);
  assert.throws(() => run({ action: "bestOf", value: 4 }), BadCmd);
  assert.throws(() => run({ action: "nonsense" }), /unknown action/);
});

test("old actions keep the program object intact", () => {
  const s1 = run({ action: "program", value: { headline: "keep" } });
  assert.strictEqual(run({ action: "blue+" }, s1).program.headline, "keep");
});

// ---- integration -----------------------------------------------------------

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => { const p = srv.address().port; srv.close(() => resolve(p)); });
    srv.on("error", reject);
  });
}

function request(port, method, urlPath, body) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : JSON.stringify(body);
    const req = http.request({
      host: "127.0.0.1", port, method, path: urlPath,
      headers: data ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } : {}
    }, (res) => {
      let buf = "";
      res.on("data", (c) => { buf += c; });
      res.on("end", () => resolve({ status: res.statusCode, json: JSON.parse(buf) }));
    });
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

function startServer(port, stateFile) {
  const child = spawn(process.execPath, [path.join(__dirname, "..", "scoreboard-server.js")], {
    env: Object.assign({}, process.env, { SB_PORT: String(port), SB_STATE: stateFile }),
    stdio: ["ignore", "pipe", "pipe"]
  });
  return new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("exit", (code) => reject(new Error("server exited early: " + code)));
    child.stdout.on("data", (c) => { if (String(c).includes("scoreboard server on")) resolve(child); });
  });
}

test("integration: POST program commands then read GET /state", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sb-test-"));
  const stateFile = path.join(dir, "state.json");
  const port = await freePort();
  const child = await startServer(port, stateFile);
  try {
    child.removeAllListeners("exit");
    const before = Date.now();
    let r = await request(port, "POST", "/cmd", { action: "program", value: { headline: "Hello", firstUp: { blue: "Hawks", orange: "Owls" } } });
    assert.strictEqual(r.status, 200);
    r = await request(port, "POST", "/cmd", { action: "countdown", value: 120 });
    assert.strictEqual(r.status, 200);
    const s = (await request(port, "GET", "/state")).json;
    assert.strictEqual(s.program.headline, "Hello");
    assert.deepStrictEqual(s.program.firstUp, { blue: "Hawks", orange: "Owls" });
    assert.ok(s.program.endsAt >= before + 120000 && s.program.endsAt <= Date.now() + 120000);
    const bad = await request(port, "POST", "/cmd", { action: "countdown", value: 9999 });
    assert.strictEqual(bad.status, 400);
    assert.match(bad.json.error, /countdown/);
    const full = await request(port, "POST", "/cmd", { action: "program", value: {
      label: "L".repeat(20), stage: "S".repeat(40), headline: "H".repeat(70), subline: "U".repeat(90),
      firstUp: { blue: "B".repeat(28), orange: "O".repeat(28) }
    } });
    assert.strictEqual(full.status, 200, "a maximal payload fits MAX_BODY");
    await request(port, "POST", "/cmd", { action: "countdownClear" });
    assert.strictEqual((await request(port, "GET", "/state")).json.program.endsAt, null);
    const saved = JSON.parse(fs.readFileSync(stateFile, "utf8"));
    assert.strictEqual(saved.program.headline, "H".repeat(70));
  } finally {
    child.kill("SIGTERM");
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
