#!/usr/bin/env python3
"""measure_latency.py: camera to decoded-frame latency of the live path, using the phone's torch.

The phone lights its torch on command and reports the phone clock of the request (MARK). The
PC decodes the same UDP feed OBS reads with ffmpeg (OBS-like low delay options) and finds the
first decoded frame whose brightness has crossed halfway to the new level. The clock offset
between phone and PC comes from PING/PONG round trips. The number includes the torch's own
turn-on time (about one to three frames), so it is an upper bound on glass-to-PC latency.

The lens must see something the torch lights up (a desk a few centimetres away is enough).
Needs the relay running (`phone-cam` or live_relay.py --udp-port N) and `adb forward tcp:49401 tcp:49401`.

Usage: python3 measure_latency.py [--port 49401] [--udp-port 49400] [--rounds 10] [--json]
"""
import argparse
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_client  # noqa: E402
import proto  # noqa: E402

FRAME_W, FRAME_H = 16, 9
FRAME_BYTES = FRAME_W * FRAME_H
MIN_STEP = 6.0       # luma units: smaller changes are not trusted
SETTLE_S = 1.2
WINDOW = 12          # frames used for the before/after levels
OBS_LIKE_INPUT = "fflags=nobuffer flags=low_delay probesize=32768 analyzeduration=0 max_delay=0"


def decoder_command(udp_port: int, extra_input: str = OBS_LIKE_INPUT) -> list:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    for item in extra_input.split():
        key, value = item.split("=")
        cmd += ["-" + key, value]
    cmd += ["-i", "udp://127.0.0.1:%d?fifo_size=65536&overrun_nonfatal=1" % udp_port,
            "-vf", "scale=%d:%d,format=gray" % (FRAME_W, FRAME_H), "-fps_mode", "passthrough", "-f", "rawvideo", "-flush_packets", "1", "-"]
    return cmd


def find_transition(samples: list, at_us: int):
    """samples: [(pc_us, mean_luma)] in order. Returns (latency_us, before, after) or None."""
    before = [v for t, v in samples if t < at_us][-WINDOW:]
    after = [v for t, v in samples if t > at_us + SETTLE_S * 1e6][:WINDOW]
    if len(before) < 3 or len(after) < 3:
        return None
    start, end = statistics.median(before), statistics.median(after)
    if abs(end - start) < MIN_STEP:
        return None
    midpoint = (start + end) / 2
    rising = end > start
    for t, v in samples:
        if t >= at_us and (v >= midpoint if rising else v <= midpoint):
            return t - at_us, start, end
    return None


class LumaWatcher(threading.Thread):
    def __init__(self, process: subprocess.Popen):
        super().__init__(daemon=True)
        self.process = process
        self.samples = []
        self.lock = threading.Lock()

    def run(self):
        stream = self.process.stdout
        while True:
            data = stream.read(FRAME_BYTES)
            if len(data) < FRAME_BYTES:
                return
            now = proto.pc_clock_us()
            with self.lock:
                self.samples.append((now, sum(data) / FRAME_BYTES))
                del self.samples[:-2000]

    def snapshot(self) -> list:
        with self.lock:
            return list(self.samples)


class Receiver(threading.Thread):
    """Reads the phone socket continuously so receive times are not delayed by this tool."""

    def __init__(self, reader, sync):
        super().__init__(daemon=True)
        self.reader = reader
        self.sync = sync
        self.marks = queue.Queue()
        self.frames = []  # (recv_us, capture_pts_us)
        self.error = None

    def run(self):
        try:
            for packet in self.reader.packets():
                kind = packet.header.type
                if kind == proto.TYPE_PONG:
                    self.sync.on_pong(packet)
                elif kind == proto.TYPE_MARK:
                    self.marks.put(packet)
                elif kind == proto.TYPE_FRAME:
                    self.frames.append((packet.recv_us, packet.header.pts_us))
                    del self.frames[:-3000]
        except (OSError, ConnectionError, proto.ProtocolError) as exc:
            self.error = str(exc)


def calibrate(sock, sync, rounds=60):
    for _ in range(rounds):
        sock.sendall(sync.make_ping())
        time.sleep(0.03)


def decode_lag_ms(frames: list, outputs: list):
    """Median delay between a frame arriving at the PC and its decoded picture appearing.

    The decoder drops frames until the first keyframe, so the pairing offset is unknown; the
    right one is the offset with the most consistent (smallest spread) non-negative delay.
    """
    recent_out = [t for t, _ in outputs][-300:]
    recent_in = [t for t, _ in frames][-400:]
    best = None
    for shift in range(0, len(recent_in) - len(recent_out) + 1):
        lags = [o - recent_in[i + shift] for i, o in enumerate(recent_out)]
        if min(lags) < 0:
            continue
        spread = statistics.pstdev(lags)
        if best is None or spread < best[0]:
            best = (spread, statistics.median(lags))
    return None if best is None else round(best[1] / 1000, 1)


def run_rounds(sock, receiver, sync, watcher, rounds):
    results = []
    for index in range(rounds * 2):
        want_on = index % 2 == 0
        sock.sendall(proto.encode_command("TORC", 1 if want_on else 0))
        try:
            mark = receiver.marks.get(timeout=2.0)
        except queue.Empty:
            results.append({"torch": want_on, "error": "no MARK from the phone"})
            continue
        at_pc = sync.phone_to_pc_us(mark.header.send_us)
        time.sleep(SETTLE_S + 0.9)
        found = find_transition(watcher.snapshot(), at_pc)
        row = {"torch": "on" if want_on else "off"}
        if found:
            row.update(latency_ms=round(found[0] / 1000, 1), before=round(found[1], 1), after=round(found[2], 1))
        else:
            row["error"] = "no brightness change seen"
        results.append(row)
        time.sleep(0.3)
    return results


def summarize(results: list) -> dict:
    values = [r["latency_ms"] for r in results if "latency_ms" in r]
    if not values:
        return {"samples": 0}
    return {"samples": len(values), "min_ms": min(values), "p50_ms": statistics.median(values),
            "max_ms": max(values), "mean_ms": round(statistics.mean(values), 1)}


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=49401)
    parser.add_argument("--udp-port", type=int, default=49400)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decoder = subprocess.Popen(decoder_command(args.udp_port), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    watcher = LumaWatcher(decoder)
    watcher.start()
    try:
        sock = live_client.connect("127.0.0.1", args.port, 5)
        reader = live_client.PacketReader(sock)
        reader.read_hello()
        sync = live_client.ClockSync()
        receiver = Receiver(reader, sync)
        receiver.start()
        calibrate(sock, sync)
        results = run_rounds(sock, receiver, sync, watcher, args.rounds)
        sock.sendall(proto.encode_command("TORC", 0))
        frames = list(receiver.frames)
        report = {
            "clock_rtt_ms": round(sync.rtt_us / 1000, 2),
            "capture_to_pc_receive_ms": capture_to_receive_ms(frames, sync),
            "decode_lag_ms": decode_lag_ms(frames, watcher.snapshot()),
            "rounds": results,
            "summary": summarize(results),
        }
    finally:
        decoder.terminate()
    print(json.dumps(report, indent=None if args.json else 2))
    return 0 if report["summary"]["samples"] else 1


def capture_to_receive_ms(frames: list, sync) -> float:
    values = [(recv - sync.phone_to_pc_us(pts)) / 1000 for recv, pts in frames]
    return round(statistics.median(values), 1) if values else 0.0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
