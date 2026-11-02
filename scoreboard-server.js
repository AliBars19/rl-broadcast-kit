#!/usr/bin/env node
// Live scoreboard control server for the OBS overlays.
// Stdlib only. Binds to 127.0.0.1 and nothing else.
//   GET  /        control panel (control.html), add it as an OBS custom browser dock
//   GET  /state   current state as JSON
//   GET  /events  Server-Sent Events: state now, then on every change
//   POST /cmd     {action, value?}
"use strict";
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = parseInt(process.env.SB_PORT, 10) || 49125;
const HOST = "127.0.0.1";
const DIR = __dirname;
const STATE_FILE = process.env.SB_STATE || path.join(DIR, "scoreboard-state.json");
const MAX_BODY = 4096;
const MAX_TAB = 80;
const MAX_PRESETS = 6;
const HEARTBEAT_MS = 15000;
const SELF_ORIGIN = "http://127.0.0.1:" + PORT;

const clients = new Set();

function needed(bestOf) { return Math.floor(bestOf / 2) + 1; }
function clampWins(w, bestOf) { return Math.max(0, Math.min(needed(bestOf), w)); }

// Max lengths for the program fields.
const PROGRAM_LIMITS = { label: 20, stage: 40, headline: 70, subline: 90 };
const TEAM_NAME_MAX = 28;
const COUNTDOWN_MAX_S = 7200;

function defaults() {
  return {
    blue: { wins: 0 }, orange: { wins: 0 }, bestOf: 3, tab: "", presets: [],
    program: {
      endsAt: null, label: "KICKOFF", stage: "", headline: "", subline: "",
      firstUp: { blue: "", orange: "" }
    }
  };
}

// Lenient string for the state file: wrong type keeps the fallback, long text is cut.
function cleanText(v, max, fallback) {
  return typeof v === "string" ? v.trim().slice(0, max) : fallback;
}

function isEpochMs(v) { return Number.isSafeInteger(v) && v >= 0; }

function sanitizeProgram(raw, base) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return base;
  const p = Object.assign({}, base, { firstUp: Object.assign({}, base.firstUp) });
  if (isEpochMs(raw.endsAt)) p.endsAt = raw.endsAt;
  for (const k of Object.keys(PROGRAM_LIMITS)) p[k] = cleanText(raw[k], PROGRAM_LIMITS[k], base[k]);
  const f = raw.firstUp;
  if (f && typeof f === "object" && !Array.isArray(f)) {
    p.firstUp.blue = cleanText(f.blue, TEAM_NAME_MAX, base.firstUp.blue);
    p.firstUp.orange = cleanText(f.orange, TEAM_NAME_MAX, base.firstUp.orange);
  }
  return p;
}

// Build a clean state from untrusted data (the state file), field by field.
function sanitize(raw) {
  const s = defaults();
  if (!raw || typeof raw !== "object") return s;
  if ([3, 5, 7].includes(raw.bestOf)) s.bestOf = raw.bestOf;
  const w = (t) => (t && Number.isInteger(t.wins) ? clampWins(t.wins, s.bestOf) : 0);
  s.blue.wins = w(raw.blue);
  s.orange.wins = w(raw.orange);
  if (typeof raw.tab === "string") s.tab = raw.tab.trim().slice(0, MAX_TAB);
  if (Array.isArray(raw.presets)) {
    for (const p of raw.presets) {
      if (typeof p !== "string") continue;
      const t = p.trim().slice(0, MAX_TAB);
      if (t && !s.presets.includes(t) && s.presets.length < MAX_PRESETS) s.presets.push(t);
    }
  }
  s.program = sanitizeProgram(raw.program, s.program);
  return s;
}

function load() {
  try {
    return sanitize(JSON.parse(fs.readFileSync(STATE_FILE, "utf8")));
  } catch (e) {
    if (e.code !== "ENOENT") console.log("state file unreadable (" + e.message + "), starting fresh");
    const s = defaults();
    save(s);
    return s;
  }
}

function save(s) {
  const tmp = STATE_FILE + ".tmp";
  try {
    fs.writeFileSync(tmp, JSON.stringify(s, null, 2));
    fs.renameSync(tmp, STATE_FILE);
  } catch (e) {
    console.log("could not save state: " + e.message);
  }
}

// Loaded by start(), so requiring this module (tests) never touches the state file.
let state = null;

function broadcast() {
  const msg = "data: " + JSON.stringify(state) + "\n\n";
  for (const res of clients) res.write(msg);
}

class BadCmd extends Error {}

function isPlainObject(v) { return !!v && typeof v === "object" && !Array.isArray(v); }

// Strict string for commands: wrong type or too long is an error, never a silent cut.
function strictText(name, v, max) {
  if (typeof v !== "string") throw new BadCmd(name + " must be a string");
  const t = v.trim();
  if (t.length > max) throw new BadCmd(name + " is longer than " + max + " characters");
  return t;
}

function checkKeys(obj, allowed, what) {
  for (const k of Object.keys(obj)) {
    if (!allowed.includes(k)) throw new BadCmd("unknown " + what + " field: " + k);
  }
}

function mergeFirstUp(current, value) {
  if (!isPlainObject(value)) throw new BadCmd("firstUp must be an object");
  checkKeys(value, ["blue", "orange"], "firstUp");
  const out = Object.assign({}, current);
  for (const side of Object.keys(value)) out[side] = strictText("firstUp." + side, value[side], TEAM_NAME_MAX);
  return out;
}

// Validate every provided field, then return a new program object. Throws before changing anything.
function mergeProgram(current, value) {
  if (!isPlainObject(value)) throw new BadCmd("program value must be an object");
  const keys = Object.keys(value);
  if (!keys.length) throw new BadCmd("program value has no fields");
  checkKeys(value, ["endsAt", "firstUp"].concat(Object.keys(PROGRAM_LIMITS)), "program");
  const out = Object.assign({}, current);
  for (const k of keys) {
    if (k === "endsAt") {
      if (value.endsAt !== null && !isEpochMs(value.endsAt)) {
        throw new BadCmd("endsAt must be null or epoch milliseconds");
      }
      out.endsAt = value.endsAt;
    } else if (k === "firstUp") out.firstUp = mergeFirstUp(current.firstUp, value.firstUp);
    else out[k] = strictText(k, value[k], PROGRAM_LIMITS[k]);
  }
  return out;
}

function countdownEnd(value, now) {
  if (!Number.isInteger(value) || value < 0 || value > COUNTDOWN_MAX_S) {
    throw new BadCmd("countdown value must be whole seconds from 0 to " + COUNTDOWN_MAX_S);
  }
  return now + value * 1000;
}

// Pure: returns the next state for `cmd`, never touches `current`. `now` is injectable for tests.
function apply(cmd, current, now) {
  const next = JSON.parse(JSON.stringify(current));
  const clock = now === undefined ? Date.now() : now;
  const max = needed(next.bestOf);
  switch (cmd.action) {
    case "blue+": next.blue.wins = Math.min(max, next.blue.wins + 1); break;
    case "blue-": next.blue.wins = Math.max(0, next.blue.wins - 1); break;
    case "orange+": next.orange.wins = Math.min(max, next.orange.wins + 1); break;
    case "orange-": next.orange.wins = Math.max(0, next.orange.wins - 1); break;
    case "bestOf":
      if (![3, 5, 7].includes(cmd.value)) throw new BadCmd("bestOf must be 3, 5 or 7");
      next.bestOf = cmd.value;
      next.blue.wins = clampWins(next.blue.wins, next.bestOf);
      next.orange.wins = clampWins(next.orange.wins, next.bestOf);
      break;
    case "reset": next.blue.wins = 0; next.orange.wins = 0; break;
    case "swap": {
      const b = next.blue.wins;
      next.blue.wins = next.orange.wins;
      next.orange.wins = b;
      break;
    }
    case "tab": {
      if (typeof cmd.value !== "string") throw new BadCmd("tab value must be a string");
      const t = cmd.value.trim();
      if (t.length > MAX_TAB) throw new BadCmd("tab text is longer than " + MAX_TAB + " characters");
      next.tab = t;
      if (t) next.presets = [t].concat(next.presets.filter((p) => p !== t)).slice(0, MAX_PRESETS);
      break;
    }
    case "countdown": next.program.endsAt = countdownEnd(cmd.value, clock); break;
    case "countdownClear": next.program.endsAt = null; break;
    case "program": next.program = mergeProgram(next.program, cmd.value); break;
    default: throw new BadCmd("unknown action");
  }
  return next;
}

function sendJson(res, code, obj, extra) {
  const body = JSON.stringify(obj);
  res.writeHead(code, Object.assign({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store"
  }, extra || {}));
  res.end(body);
}

const STATIC = {
  "/overlay.css": ["overlay.css", "text/css; charset=utf-8"],
  "/control-program.js": ["control-program.js", "text/javascript; charset=utf-8"]
};
const FONT_RE = /^\/fonts\/[A-Za-z0-9._-]+\.woff2$/;

function serveFile(res, file, type) {
  fs.readFile(path.join(DIR, file), (err, buf) => {
    if (err) return sendJson(res, 404, { error: "not found" });
    res.writeHead(200, { "Content-Type": type, "Cache-Control": "no-cache" });
    res.end(buf);
  });
}

function hostOk(req) {
  const h = req.headers.host;
  return !h || h === "127.0.0.1:" + PORT || h === "localhost:" + PORT;
}

function handleCmd(req, res) {
  const origin = req.headers.origin;
  if (origin !== undefined && origin !== "null" && origin !== SELF_ORIGIN) {
    return sendJson(res, 403, { error: "origin not allowed" });
  }
  const cors = origin === undefined ? {} : { "Access-Control-Allow-Origin": origin };
  const chunks = [];
  let size = 0, done = false;
  req.on("data", (c) => {
    if (done) return;
    size += c.length;
    if (size > MAX_BODY) {
      done = true;
      sendJson(res, 413, { error: "body too large" }, Object.assign({ Connection: "close" }, cors));
      req.destroy();
      return;
    }
    chunks.push(c);
  });
  req.on("error", () => {});
  req.on("end", () => {
    if (done) return;
    let cmd;
    try { cmd = JSON.parse(Buffer.concat(chunks).toString("utf8")); }
    catch (e) { return sendJson(res, 400, { error: "invalid JSON" }, cors); }
    if (!cmd || typeof cmd !== "object" || Array.isArray(cmd) || typeof cmd.action !== "string") {
      return sendJson(res, 400, { error: "expected {action, value?}" }, cors);
    }
    try {
      state = apply(cmd, state);
    } catch (e) {
      if (e instanceof BadCmd) return sendJson(res, 400, { error: e.message }, cors);
      console.log("command failed: " + e.message);
      return sendJson(res, 500, { error: "internal error" }, cors);
    }
    save(state);
    console.log(cmd.action + (cmd.value !== undefined ? " " + JSON.stringify(cmd.value) : "") +
      " -> blue " + state.blue.wins + " orange " + state.orange.wins + " bo" + state.bestOf +
      " tab " + JSON.stringify(state.tab));
    broadcast();
    sendJson(res, 200, state, cors);
  });
}

const server = http.createServer((req, res) => {
  const url = (req.url || "/").split("?")[0];
  if (!hostOk(req)) return sendJson(res, 403, { error: "host not allowed" });

  if (req.method === "OPTIONS") {
    const origin = req.headers.origin;
    if (url === "/cmd" && origin !== undefined && origin !== "null" && origin !== SELF_ORIGIN) {
      return sendJson(res, 403, { error: "origin not allowed" });
    }
    res.writeHead(204, {
      "Access-Control-Allow-Origin": url === "/cmd" && origin ? origin : "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "600"
    });
    return res.end();
  }

  if (req.method === "GET") {
    if (url === "/") return serveFile(res, "control.html", "text/html; charset=utf-8");
    if (url === "/state") return sendJson(res, 200, state, { "Access-Control-Allow-Origin": "*" });
    if (url === "/events") {
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        Connection: "keep-alive",
        "Access-Control-Allow-Origin": "*"
      });
      res.write("retry: 2000\n\ndata: " + JSON.stringify(state) + "\n\n");
      clients.add(res);
      req.on("close", () => clients.delete(res));
      return;
    }
    if (STATIC[url]) return serveFile(res, STATIC[url][0], STATIC[url][1]);
    if (FONT_RE.test(url)) return serveFile(res, url.slice(1), "font/woff2");
    return sendJson(res, 404, { error: "not found" });
  }

  if (req.method === "POST" && url === "/cmd") return handleCmd(req, res);
  return sendJson(res, 404, { error: "not found" });
});

function start() {
  state = load();
  setInterval(() => { for (const res of clients) res.write(": hb\n\n"); }, HEARTBEAT_MS).unref();
  server.on("error", (e) => { console.log("server error: " + e.message); process.exit(1); });
  server.listen(PORT, HOST, () => console.log("scoreboard server on http://" + HOST + ":" + PORT + "/ (state " + STATE_FILE + ")"));
  for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => process.exit(0));
}

if (require.main === module) start();

module.exports = { defaults, sanitize, apply, BadCmd };
