// rl-relay.js: Rocket League Stats API -> WebSocket bridge for OBS browser sources.
// Runs on the gaming PC, next to Rocket League.
//
// Why: Rocket League's Stats API streams JSON over a local TCP socket. Browser sources can
// only use WebSockets, so this small script reads the TCP stream and re-broadcasts each
// message over a WebSocket. How to enable the Stats API (and its settings) is described on
// the game's developer page: https://www.rocketleague.com/en/developer/stats-api
//
// Run:   node rl-relay.js          (Node.js 18+, no npm packages needed)
// Needs: the Stats API enabled in Rocket League (see the page above).
// Both sides listen on 127.0.0.1 only, so nothing is exposed to your network.

"use strict";
const net = require("net");
const http = require("http");
const crypto = require("crypto");

const RL_HOST = "127.0.0.1";
const RL_PORT = Number(process.env.RL_PORT || 49123);
const WS_PORT = Number(process.env.WS_PORT || 49124);

// ---------- WebSocket server (minimal, text frames only) ----------
const clients = new Set();

function frame(text) {
  const payload = Buffer.from(text, "utf8");
  const len = payload.length;
  let header;
  if (len < 126) header = Buffer.from([0x81, len]);
  else if (len < 65536) { header = Buffer.alloc(4); header[0] = 0x81; header[1] = 126; header.writeUInt16BE(len, 2); }
  else { header = Buffer.alloc(10); header[0] = 0x81; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2); }
  return Buffer.concat([header, payload]);
}

const server = http.createServer((req, res) => {
  res.writeHead(200, { "Content-Type": "text/plain" });
  res.end(`rl-relay running. Rocket League ${rlConnected ? "connected" : "not connected"}. Clients: ${clients.size}\n`);
});

server.on("upgrade", (req, socket) => {
  const key = req.headers["sec-websocket-key"];
  if (!key) return socket.destroy();
  const accept = crypto.createHash("sha1").update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest("base64");
  socket.write("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
               `Sec-WebSocket-Accept: ${accept}\r\n\r\n`);
  clients.add(socket);
  socket.on("data", (buf) => {             // handle close (0x8) and ping (0x9) from the browser
    const op = buf[0] & 0x0f;
    if (op === 0x8) socket.end();
    if (op === 0x9) socket.write(Buffer.from([0x8a, 0]));
  });
  socket.on("close", () => clients.delete(socket));
  socket.on("error", () => clients.delete(socket));
});

server.listen(WS_PORT, "127.0.0.1", () => console.log(`[relay] WebSocket on ws://127.0.0.1:${WS_PORT}`));

function broadcast(text) {
  const f = frame(text);
  for (const c of clients) { try { c.write(f); } catch { clients.delete(c); } }
}

// ---------- Rocket League TCP client with JSON framing ----------
let rlConnected = false;

function connectRL() {
  const sock = net.connect(RL_PORT, RL_HOST);
  let buf = "", depth = 0, inStr = false, esc = false, start = -1;

  sock.setEncoding("utf8");
  sock.on("connect", () => { rlConnected = true; console.log("[relay] connected to Rocket League Stats API"); });
  sock.on("data", (chunk) => {
    // Split the stream into whole JSON objects by tracking brace depth outside strings.
    for (let i = 0; i < chunk.length; i++) {
      const ch = chunk[i];
      buf += ch;
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === "{") { if (depth === 0) start = buf.length - 1; depth++; }
      else if (ch === "}") {
        depth--;
        if (depth === 0 && start >= 0) {
          const text = buf.slice(start);
          buf = ""; start = -1;
          try {
            const msg = JSON.parse(text);
            if (typeof msg.Data === "string") { try { msg.Data = JSON.parse(msg.Data); } catch {} }
            broadcast(JSON.stringify(msg));
          } catch { /* skip malformed */ }
        }
      }
    }
    if (depth === 0 && buf.length > 1e6) buf = "";
  });
  const retry = () => {
    if (rlConnected) console.log("[relay] Rocket League disconnected, retrying...");
    rlConnected = false;
    setTimeout(connectRL, 2000);
  };
  sock.on("error", () => {});
  sock.on("close", retry);
}

connectRL();
console.log(`[relay] waiting for Rocket League on ${RL_HOST}:${RL_PORT} (start a match)`);
