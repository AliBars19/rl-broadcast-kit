#!/usr/bin/env python3
"""live_relay.py: phone live socket to MPEG-TS over local UDP, for the OBS media source.

Reads the framed stream the app serves (through `adb forward`), wraps every access unit into
MPEG-TS with tsmux.py and sends it as 1316 byte datagrams to udp://127.0.0.1:49400, the same
input OBS reads today. No ffmpeg is involved. Every frame carries the phone's capture time,
so the relay also logs how old each frame is when it arrives (capture to PC receive).

Usage: python3 live_relay.py [--port 49401] [--udp-port 49400] [--status-file PATH]
Exit codes: 0 stopped by signal, 3 the connection to the phone was lost (the supervisor restarts it).
"""
import argparse
import json
import os
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_client  # noqa: E402
import proto  # noqa: E402
import tsmux  # noqa: E402

UDP_DATAGRAM = 1316  # 7 TS packets, the pkt_size the old ffmpeg output used
PTS_START = 90000
STATS_WINDOW = 600
STALL_S = 5.0   # no frame for this long counts as a dead feed
POLL_S = 0.5
LOG_EVERY_S = 30
STATUS_EVERY_S = 5
EXIT_LOST = 3
HEVC_PARAMS = 32
H264_PARAMS = 7


def log(message: str) -> None:
    sys.stdout.write("%s live-relay: %s\n" % (time.strftime("%F %T"), message))
    sys.stdout.flush()


def percentile(values: list, fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


class Stats:
    """Rolling latency samples in milliseconds."""

    FIELDS = ("encode", "transport", "total")

    def __init__(self):
        self._data = {name: [] for name in self.FIELDS}
        self.frames = 0

    def add(self, encode_ms: float, transport_ms: float) -> None:
        self.frames += 1
        for name, value in zip(self.FIELDS, (encode_ms, transport_ms, encode_ms + transport_ms)):
            samples = self._data[name]
            samples.append(value)
            if len(samples) > STATS_WINDOW:
                del samples[0]

    def summary(self) -> dict:
        return {
            name: {"p50": round(percentile(v, 0.5), 1), "p95": round(percentile(v, 0.95), 1),
                   "max": round(max(v), 1) if v else 0.0}
            for name, v in self._data.items()
        }


def has_parameter_sets(codec: str, data: bytes) -> bool:
    """True when the access unit already starts with SPS/VPS (the app asks the encoder for that)."""
    head = data[:64]
    pos = head.find(b"\x00\x00\x01")
    while pos != -1 and pos + 3 < len(head):
        byte = head[pos + 3]
        nal = byte & 0x1F if codec == "h264" else (byte >> 1) & 0x3F
        if nal == (H264_PARAMS if codec == "h264" else HEVC_PARAMS):
            return True
        pos = head.find(b"\x00\x00\x01", pos + 3)
    return False


class Relay:
    def __init__(self, sock: socket.socket, udp: socket.socket, dest: tuple, status_file: str | None,
                 trailing_aud: bool = True, stall_s: float = STALL_S):
        self.trailing_aud = trailing_aud
        self.stall_s = stall_s
        self.reader = live_client.PacketReader(sock)
        self.sock = sock
        self.udp = udp
        self.dest = dest
        self.status_file = status_file
        self.sync = live_client.ClockSync()
        self.stats = Stats()
        self.csd = b""
        self.first_pts_us = None
        self.mux = None
        self.codec = ""
        self._last_log = self._last_status = time.monotonic()

    def run(self) -> None:
        hello = self.reader.read_hello()
        self.codec = proto.CODEC_NAMES[hello.codec]
        self.mux = tsmux.TsMuxer(self.codec, self.trailing_aud)
        log("phone stream: %s %dx%d@%d %d kbps" % (self.codec, hello.width, hello.height, hello.fps,
                                                  hello.bitrate // 1000))
        self.sock.sendall(self.sync.make_ping())
        self.sock.settimeout(POLL_S)
        last_frame = time.monotonic()
        while True:
            try:
                packet = self.reader.read_packet()
            except TimeoutError:
                packet = None
            if packet is not None:
                self.handle(packet)
                if packet.header.type == proto.TYPE_FRAME:
                    last_frame = time.monotonic()
            if time.monotonic() - last_frame > self.stall_s:
                # No error, but no pictures either (camera stuck, app frozen): leave, so the supervisor heals it.
                raise ConnectionError("no frame from the phone for %.0f s" % self.stall_s)
            if self.sync.ping_due():
                self.sock.sendall(self.sync.make_ping())
            self.report()

    def handle(self, packet: live_client.Packet) -> None:
        kind = packet.header.type
        if kind == proto.TYPE_CONFIG:
            self.csd = packet.payload
        elif kind == proto.TYPE_FRAME:
            self.send_frame(packet)
        elif kind == proto.TYPE_PONG:
            self.sync.on_pong(packet)

    def send_frame(self, packet: live_client.Packet) -> None:
        header, data = packet.header, packet.payload
        if self.first_pts_us is None:
            self.first_pts_us = header.pts_us
        # Parameter sets go in front of every frame (about 100 bytes), not just keyframes, so a
        # reader that joins mid-GOP and probes only 32 KB still finds them and can open the decoder.
        if self.csd and not has_parameter_sets(self.codec, data):
            data = self.csd + data
        pts = PTS_START + (header.pts_us - self.first_pts_us) * 9 // 100
        out = self.mux.mux(pts, data, header.is_key)
        for offset in range(0, len(out), UDP_DATAGRAM):
            self.udp.sendto(out[offset:offset + UDP_DATAGRAM], self.dest)
        if self.sync.ready:
            encode_ms = (header.send_us - header.pts_us) / 1000
            transport_ms = (packet.recv_us - self.sync.phone_to_pc_us(header.send_us)) / 1000
            self.stats.add(encode_ms, transport_ms)

    def report(self) -> None:
        now = time.monotonic()
        if now - self._last_status >= STATUS_EVERY_S:
            self._last_status = now
            self.write_status()
        if now - self._last_log >= LOG_EVERY_S:
            self._last_log = now
            log("latency ms %s, clock rtt %.1f ms" % (json.dumps(self.stats.summary()), self.sync.rtt_us / 1000))

    def write_status(self) -> None:
        if not self.status_file:
            return
        body = {"time": time.time(), "frames": self.stats.frames, "latency_ms": self.stats.summary(),
                "clock_rtt_ms": round(self.sync.rtt_us / 1000, 2), "codec": self.codec}
        tmp = self.status_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
            os.replace(tmp, self.status_file)
        except OSError as exc:
            log("cannot write %s: %s" % (self.status_file, exc))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=49401, help="live port (adb forward)")
    parser.add_argument("--udp-port", type=int, default=49400, help="UDP port OBS listens on")
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--status-file", default=None)
    parser.add_argument("--aud", choices=("trail", "lead"), default="trail",
                        help="where the access unit delimiter goes (trail avoids one frame of parser delay)")
    return parser.parse_args(argv)


def main(argv) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock = live_client.connect(args.host, args.port, args.connect_timeout, log)
        Relay(sock, udp, ("127.0.0.1", args.udp_port), args.status_file, args.aud == "trail").run()
    except (ConnectionError, OSError, proto.ProtocolError) as exc:
        log("connection ended: %s" % exc)
        return EXIT_LOST
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
