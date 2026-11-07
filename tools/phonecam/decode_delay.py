#!/usr/bin/env python3
"""decode_delay.py: how long the PC side adds between a frame arriving and its picture appearing.

Runs the real relay code (live_relay.Relay) against the phone on a private UDP port, decodes it
with ffmpeg using OBS-like low delay options, and matches every decoded picture (ffmpeg showinfo)
to the moment the relay sent the TS packets that carried it. Reports the median and spread.

Usage: python3 decode_delay.py [--port 49401] [--udp-port 49411] [--seconds 10] [--aud trail|lead]
"""
import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_client  # noqa: E402
import live_relay  # noqa: E402
import measure_latency as ml  # noqa: E402
import proto  # noqa: E402

PTS_RE = re.compile(r"pts_time:\s*([0-9.]+)")


class TracingRelay(live_relay.Relay):
    """The real relay, remembering when each PES (by its 90 kHz pts) left for UDP."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_at = []  # (pts_90k, pc_us)

    def send_frame(self, packet):
        if self.first_pts_us is None:
            self.first_pts_us = packet.header.pts_us
        pts = live_relay.PTS_START + (packet.header.pts_us - self.first_pts_us) * 9 // 100
        super().send_frame(packet)
        self.sent_at.append((pts, proto.pc_clock_us()))
        del self.sent_at[:-1500]


def decoder_with_showinfo(udp_port: int) -> subprocess.Popen:
    cmd = ml.decoder_command(udp_port)
    cmd[cmd.index("-loglevel") + 1] = "info"
    cmd.insert(cmd.index("-i"), "-copyts")  # keep the TS pts so pictures can be matched to sends
    vf = cmd.index("-vf") + 1
    cmd[vf] = "scale=%d:%d,format=gray,showinfo" % (ml.FRAME_W, ml.FRAME_H)
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)


def collect_pts(proc: subprocess.Popen, out: list) -> None:
    for line in proc.stderr:
        match = PTS_RE.search(line.decode("utf-8", "replace"))
        if match:
            out.append((round(float(match.group(1)) * 90000), proto.pc_clock_us()))


def match_lags(sent_at: list, shown: list) -> list:
    """Decode lag in ms per shown picture: shown time minus send time of the frame it belongs to.

    With the trailing AUD each picture carries the pts of the PES before its own, so the frame's
    own PES is the first one with a larger pts.
    """
    lags = []
    for pts, shown_us in shown:
        for later_pts, sent_us in sent_at:
            if later_pts > pts:
                lags.append((shown_us - sent_us) / 1000)
                break
    return lags


def run(args) -> dict:
    sock = live_client.connect("127.0.0.1", args.port, 5)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    relay = TracingRelay(sock, udp, ("127.0.0.1", args.udp_port), None, args.aud == "trail")
    threading.Thread(target=lambda: _quiet(relay.run), daemon=True).start()
    proc = decoder_with_showinfo(args.udp_port)
    shown = []
    threading.Thread(target=collect_pts, args=(proc, shown), daemon=True).start()
    threading.Thread(target=lambda: [proc.stdout.read(1 << 16) or time.sleep(0.01) for _ in iter(int, 1)],
                     daemon=True).start()
    time.sleep(args.seconds)
    proc.terminate()
    lags = match_lags(list(relay.sent_at), list(shown)[5:])
    if not lags:
        return {"samples": 0}
    return {"aud": args.aud, "samples": len(lags), "median_ms": round(statistics.median(lags), 1),
            "p95_ms": round(sorted(lags)[int(len(lags) * 0.95)], 1), "min_ms": round(min(lags), 1)}


def _quiet(fn):
    try:
        fn()
    except (OSError, ConnectionError, proto.ProtocolError):
        pass


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=49401)
    parser.add_argument("--udp-port", type=int, default=49411)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--aud", choices=("trail", "lead"), default="trail")
    result = run(parser.parse_args(argv))
    print(json.dumps(result))
    return 0 if result.get("samples") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
