#!/usr/bin/env python3
"""obsws.py: a tiny command-line client for the OBS WebSocket v5 protocol.

Talks to ws://127.0.0.1:4455 using only the standard library. The WebSocket
handshake and framing, and the obs-websocket v5 Hello/Identify/Request flow,
are implemented here.

Usage:
    python3 -I obsws.py <RequestType> [<json requestData>]

Examples:
    python3 -I obsws.py GetVersion
    python3 -I obsws.py SetCurrentProgramScene '{"sceneName": "Main"}'

On success the response `d` object is printed as pretty JSON on stdout (exit 0).
On failure a message goes to stderr (exit 1). If OBS reports
requestStatus.result false, the requestStatus object is printed to stdout too.

Setup: enable the OBS WebSocket server (Tools > WebSocket Server Settings) and give it a password.
The password is never stored in this repository. It is read at runtime, first match wins:
  1. the OBS_WEBSOCKET_PASSWORD environment variable;
  2. the first line of the file named by OBS_WEBSOCKET_PASSWORD_FILE, or of `.obs-password`
     in the repository root (that file is git-ignored; create it yourself);
  3. the `server_password` field of the obs-websocket config.json named by OBS_WEBSOCKET_CONFIG.
The password is never printed or logged.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
from typing import Any

HOST = "127.0.0.1"
PORT = 4455
TIMEOUT_SECONDS = 10
# git-ignored file in the repository root holding the password on its first line
PASSWORD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".obs-password")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RPC_VERSION = 1

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

FRAME_CONTINUATION = 0x0
FRAME_TEXT = 0x1
FRAME_BINARY = 0x2
FRAME_CLOSE = 0x8
FRAME_PING = 0x9
FRAME_PONG = 0xA

CLOSE_NORMAL = 1000
CLOSE_AUTH_FAILED = 4009


class ObsError(Exception):
    """A client-side or protocol failure. The message is safe to print."""


def eprint(message: str) -> None:
    sys.stderr.write("obsws: %s\n" % message)


# ---------- configuration and authentication ----------

def _first_line(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.readline().strip()


def read_password() -> str:
    """Return the websocket password from the environment or a local file. Never printed."""
    env = os.environ.get("OBS_WEBSOCKET_PASSWORD")
    if env:
        return env
    try:
        path = os.environ.get("OBS_WEBSOCKET_PASSWORD_FILE") or PASSWORD_FILE
        if os.environ.get("OBS_WEBSOCKET_PASSWORD_FILE") or os.path.isfile(path):
            return _first_line(path)
        config_path = os.environ.get("OBS_WEBSOCKET_CONFIG")
        if config_path:
            with open(config_path, encoding="utf-8") as handle:
                return str(json.load(handle).get("server_password", ""))
    except (OSError, ValueError) as exc:
        raise ObsError("cannot read OBS websocket config (%s)" % type(exc).__name__)
    raise ObsError("cannot read OBS websocket config: set OBS_WEBSOCKET_PASSWORD or create "
                   ".obs-password in the repository root (see README)")


def _b64_sha256(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def make_auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 auth: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = _b64_sha256(password + salt)
    return _b64_sha256(secret + challenge)


# ---------- raw WebSocket framing ----------

def recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly `count` bytes or raise if the peer hangs up."""
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise ObsError("OBS closed the connection unexpectedly")
        chunks.extend(chunk)
    return bytes(chunks)


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def send_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    """Send one final frame. Client frames must be masked (RFC 6455)."""
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 1 << 16:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask = os.urandom(4)
    header += mask
    sock.sendall(bytes(header) + _apply_mask(payload, mask))


def read_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    """Read one raw frame. Returns (fin, opcode, unmasked payload)."""
    first, second = recv_exact(sock, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else None
    payload = recv_exact(sock, length)
    if mask:
        payload = _apply_mask(payload, mask)
    return fin, opcode, payload


def receive_message(sock: socket.socket) -> bytes:
    """Return the next complete data message.

    Reassembles fragments, answers pings, ignores pongs and turns a close
    frame into an ObsError.
    """
    fragments: list[bytes] = []
    while True:
        fin, opcode, payload = read_frame(sock)
        if opcode == FRAME_CLOSE:
            code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 1005
            try:
                send_frame(sock, FRAME_CLOSE, payload[:2])
            except OSError:
                pass
            if code == CLOSE_AUTH_FAILED:
                raise ObsError("authentication failed, check server_password")
            raise ObsError("OBS closed the WebSocket (close code %d)" % code)
        if opcode == FRAME_PING:
            send_frame(sock, FRAME_PONG, payload)
            continue
        if opcode == FRAME_PONG:
            continue
        if opcode in (FRAME_TEXT, FRAME_BINARY):
            fragments = [payload]
        elif opcode == FRAME_CONTINUATION and fragments:
            fragments.append(payload)
        else:
            raise ObsError("unexpected WebSocket frame opcode %d" % opcode)
        if fin:
            return b"".join(fragments)


def send_json(sock: socket.socket, op: int, data: dict[str, Any]) -> None:
    send_frame(sock, FRAME_TEXT, json.dumps({"op": op, "d": data}).encode("utf-8"))


def receive_json(sock: socket.socket) -> dict[str, Any]:
    raw = receive_message(sock)
    try:
        message = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ObsError("OBS sent a message that is not valid JSON")
    if not isinstance(message, dict):
        raise ObsError("OBS sent a JSON value that is not an object")
    return message


# ---------- HTTP upgrade handshake ----------

def read_http_head(sock: socket.socket) -> str:
    """Read the HTTP response head byte by byte so no frame data is consumed."""
    head = bytearray()
    while not head.endswith(b"\r\n\r\n"):
        if len(head) > 8192:
            raise ObsError("oversized HTTP handshake response")
        head.extend(recv_exact(sock, 1))
    return head.decode("latin-1")


def websocket_handshake(sock: socket.socket) -> None:
    """Perform the RFC 6455 upgrade with the obsws.json subprotocol."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: obsws.json\r\n"
        "\r\n" % (HOST, PORT, key)
    )
    sock.sendall(request.encode("ascii"))
    lines = read_http_head(sock).split("\r\n")
    if not lines[0].startswith("HTTP/1.1 101"):
        raise ObsError("WebSocket upgrade refused: %s" % lines[0])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if headers.get("sec-websocket-accept") != expected:
        raise ObsError("server returned a bad Sec-WebSocket-Accept header")


# ---------- obs-websocket v5 protocol ----------

def perform_hello(sock: socket.socket) -> dict[str, Any]:
    """Read op 0 Hello and return its `d` object."""
    message = receive_json(sock)
    if message.get("op") != OP_HELLO:
        raise ObsError("expected Hello (op 0) from OBS")
    return message.get("d") or {}


def identify(sock: socket.socket, hello: dict[str, Any], event_subscriptions: int = 0) -> None:
    """Answer Hello with op 1 Identify, then wait for op 2 Identified."""
    data: dict[str, Any] = {"rpcVersion": RPC_VERSION, "eventSubscriptions": event_subscriptions}
    auth = hello.get("authentication")
    if auth:
        salt = auth.get("salt")
        challenge = auth.get("challenge")
        if not salt or not challenge:
            raise ObsError("OBS Hello carried incomplete authentication data")
        data["authentication"] = make_auth_string(read_password(), salt, challenge)
    send_json(sock, OP_IDENTIFY, data)
    while receive_json(sock).get("op") != OP_IDENTIFIED:
        pass


def new_request_id() -> str:
    """Return a random UUIDv4-formatted string built from os.urandom."""
    raw = bytearray(os.urandom(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    hexed = raw.hex()
    return "-".join((hexed[:8], hexed[8:12], hexed[12:16], hexed[16:20], hexed[20:]))


def call_request(sock: socket.socket, request_type: str, request_data: dict[str, Any]) -> dict[str, Any]:
    """Send op 6 Request and return the `d` of the matching op 7 response."""
    request_id = new_request_id()
    send_json(sock, OP_REQUEST, {
        "requestType": request_type,
        "requestId": request_id,
        "requestData": request_data,
    })
    while True:
        message = receive_json(sock)
        data = message.get("d") or {}
        if message.get("op") == OP_REQUEST_RESPONSE and data.get("requestId") == request_id:
            return data


def open_session(event_subscriptions: int = 0, timeout: float | None = TIMEOUT_SECONDS) -> socket.socket:
    """Connect, upgrade to WebSocket and identify. The caller closes the socket
    with close_session. Raises ObsError or OSError; the socket is closed first."""
    sock = socket.create_connection((HOST, PORT), timeout=TIMEOUT_SECONDS)
    try:
        websocket_handshake(sock)
        identify(sock, perform_hello(sock), event_subscriptions)
        sock.settimeout(timeout)
    except BaseException:
        sock.close()
        raise
    return sock


def close_session(sock: socket.socket) -> None:
    """Send a normal close frame (best effort) and close the socket."""
    try:
        send_frame(sock, FRAME_CLOSE, struct.pack(">H", CLOSE_NORMAL))
    except OSError:
        pass
    sock.close()


def run_request(request_type: str, request_data: dict[str, Any]) -> dict[str, Any]:
    """Connect, authenticate, send one request and return the response `d`."""
    sock = open_session()
    try:
        return call_request(sock, request_type, request_data)
    finally:
        close_session(sock)


# ---------- command line ----------

def parse_args(argv: list[str]) -> tuple[str, dict[str, Any]]:
    """Return (request_type, request_data) from argv or raise ObsError."""
    if len(argv) not in (2, 3):
        raise ObsError("usage: python3 -I obsws.py <RequestType> [<json requestData>]")
    request_data: Any = {}
    if len(argv) == 3:
        try:
            request_data = json.loads(argv[2])
        except ValueError:
            raise ObsError("requestData is not valid JSON")
        if not isinstance(request_data, dict):
            raise ObsError("requestData must be a JSON object")
    return argv[1], request_data


def main(argv: list[str]) -> int:
    try:
        request_type, request_data = parse_args(argv)
        response = run_request(request_type, request_data)
    except ObsError as exc:
        eprint(str(exc))
        return 1
    except OSError as exc:
        eprint("cannot reach OBS at ws://%s:%d (%s)" % (HOST, PORT, exc))
        return 1

    status = response.get("requestStatus") or {}
    if status.get("result") is not True:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        eprint("%s failed: code %s %s" % (request_type, status.get("code"), status.get("comment", "")))
        return 1
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
