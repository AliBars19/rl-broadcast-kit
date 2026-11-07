#!/usr/bin/env python3
"""sync_audio.py: find the time offset between the phone recording and the stream recording.

Both audio tracks hear the same voice, so a cross-correlation finds the shift:
  1. coarse: cross-correlate the loudness envelopes (100 values per second) of the whole files,
  2. fine: GCC-PHAT on short windows of the raw audio around that shift, which gives the offset
     to a fraction of a millisecond, and several windows spread over the recording give the drift.
Result: stream_time = cam_time + offset_s (cam time 0 is the first frame of the first chunk).
Needs numpy and ffmpeg; run with plain python3 (not -I).

Usage: python3 sync_audio.py STREAM_DIR            (uses STREAM_DIR/cam and STREAM_DIR/stream-full.mp4)
       python3 sync_audio.py --cam FILE_OR_DIR --stream FILE [--out PATH] [--stream-track N]
Writes cam/sync.json next to the recording.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunks  # noqa: E402

RATE = 8000
ENV_RATE = 100
WINDOW_S = 20.0
WINDOWS = 9
SEARCH_S = 0.5          # fine search range around the coarse offset
MIN_CONFIDENCE = 3.0    # coarse peak over the next best peak; below this the answer is not trusted


class SyncError(Exception):
    """The files cannot be aligned. The message is safe to print."""


def envelope(audio: np.ndarray, rate: int = RATE, env_rate: int = ENV_RATE) -> np.ndarray:
    """Log loudness per 10 ms frame, zero mean and unit variance."""
    hop = rate // env_rate
    frames = len(audio) // hop
    if frames < 10:
        raise SyncError("audio is too short to align")
    block = audio[:frames * hop].reshape(frames, hop)
    rms = np.sqrt((block.astype(np.float64) ** 2).mean(axis=1))
    env = np.log(rms + 1e-4)
    env -= env.mean()
    std = env.std()
    return env / std if std > 0 else env


def cross_correlate(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """c[k] = sum_t a[t] * b[t + lag(k)], lags -(len(a)-1) .. len(b)-1, by FFT."""
    size = 1 << (len(a) + len(b) - 1).bit_length()
    spectrum = np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size))
    full = np.fft.irfft(spectrum, size)
    return np.concatenate((full[size - (len(a) - 1):], full[:len(b)]))


def coarse_offset(cam: np.ndarray, stream: np.ndarray) -> tuple:
    """Return (offset_s, confidence): stream_time = cam_time + offset_s."""
    env_cam, env_stream = envelope(cam), envelope(stream)
    corr = cross_correlate(env_cam, env_stream)
    peak = int(np.argmax(corr))
    lag = peak - (len(env_cam) - 1)
    exclude = ENV_RATE  # one second either side counts as the same peak
    rest = corr.copy()
    rest[max(0, peak - exclude):peak + exclude + 1] = -np.inf
    second = float(rest.max())
    spread = float(corr.std()) or 1.0
    confidence = (float(corr[peak]) - float(np.median(corr))) / max(second - float(np.median(corr)), 1e-9)
    return lag / ENV_RATE, min(confidence, 99.0) if spread else 0.0


def gcc_phat(ref: np.ndarray, sig: np.ndarray, rate: int, max_shift_s: float) -> float:
    """Delay (s) of `sig` relative to `ref` with sub-sample refinement; positive means sig is later."""
    size = 1 << (len(ref) + len(sig)).bit_length()
    cross = np.fft.rfft(sig, size) * np.conj(np.fft.rfft(ref, size))
    cross /= np.abs(cross) + 1e-12
    corr = np.fft.irfft(cross, size)
    limit = int(max_shift_s * rate)
    window = np.concatenate((corr[-limit:], corr[:limit + 1]))
    peak = int(np.argmax(window))
    shift = peak - limit
    if 0 < peak < len(window) - 1:  # parabolic interpolation around the peak
        left, mid, right = window[peak - 1], window[peak], window[peak + 1]
        denom = left - 2 * mid + right
        if denom != 0:
            shift += 0.5 * (left - right) / denom
    return shift / rate


def pick_windows(cam: np.ndarray, count: int, length_s: float, rate: int = RATE) -> list:
    """Start sample of the loudest `count` non-overlapping windows, spread across the recording."""
    size = int(length_s * rate)
    if len(cam) < size * 2:
        return [0] if len(cam) >= size else []
    slots = np.array_split(np.arange(0, len(cam) - size, size), count)
    starts = []
    for slot in slots:
        if len(slot):
            energies = [np.abs(cam[s:s + size]).mean() for s in slot]
            starts.append(int(slot[int(np.argmax(energies))]))
    return starts


def refine(cam: np.ndarray, stream: np.ndarray, coarse_s: float, rate: int = RATE) -> list:
    """Per-window fine offsets as (cam_time_s, offset_s)."""
    size = int(WINDOW_S * rate)
    rows = []
    for start in pick_windows(cam, WINDOWS, WINDOW_S, rate):
        begin = start + int(coarse_s * rate)
        pad = int(SEARCH_S * rate)
        if begin - pad < 0 or begin + size + pad > len(stream):
            continue
        ref = cam[start:start + size]
        sig = stream[begin - pad:begin + size + pad]
        delay = gcc_phat(np.pad(ref, (pad, pad)), sig, rate, SEARCH_S)
        rows.append((start / rate, coarse_s + delay))
    return rows


def estimate(cam: np.ndarray, stream: np.ndarray, rate: int = RATE) -> dict:
    coarse_s, confidence = coarse_offset(cam, stream)
    if confidence < MIN_CONFIDENCE:
        raise SyncError("no clear match between the two recordings (confidence %.1f, needs %.1f); "
                        "is it the same stream, and does the phone microphone hear the voice?"
                        % (confidence, MIN_CONFIDENCE))
    rows = refine(cam, stream, coarse_s, rate)
    if not rows:
        raise SyncError("could not refine the offset: recordings overlap too little")
    times = np.array([t for t, _ in rows])
    offsets = np.array([o for _, o in rows])
    drift_ppm = 0.0
    if len(rows) >= 4 and times.max() - times.min() > 60:
        drift_ppm = float(np.polyfit(times, offsets, 1)[0] * 1e6)
    return {"offset_s": round(float(np.median(offsets)), 4), "coarse_offset_s": round(coarse_s, 3),
            "drift_ppm": round(drift_ppm, 1), "confidence": round(confidence, 1), "windows": len(rows),
            "spread_ms": round(float(offsets.std() * 1000), 2)}


def load_audio(source: Path, track: int = 0, rate: int = RATE, run=subprocess.run) -> np.ndarray:
    """Decode one audio track to mono float32 at `rate`, padded so t=0 is the container's t=0."""
    cmd = ["nice", "-n", "10", "ffmpeg", "-v", "error", "-nostdin"]
    temp = None
    try:
        if source.is_dir():
            files = chunks.list_local_chunks(source)
            if not files:
                raise SyncError("no chunk files in %s" % source)
            temp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
            temp.write("".join("file '%s'\n" % str(f).replace("'", "'\\''") for f in files))
            temp.close()
            cmd += ["-f", "concat", "-safe", "0", "-i", temp.name]
        else:
            cmd += ["-i", str(source)]
        cmd += ["-map", "0:a:%d" % track, "-vn", "-af", "aresample=async=1:first_pts=0", "-ac", "1", "-ar", str(rate),
                "-f", "f32le", "-"]
        done = run(cmd, capture_output=True)
    finally:
        if temp:
            os.unlink(temp.name)
    if done.returncode != 0 or not done.stdout:
        raise SyncError("ffmpeg could not read audio from %s: %s" % (source, done.stderr.decode("utf-8", "replace")[:200]))
    return np.frombuffer(done.stdout, dtype=np.float32)


def write_result(result: dict, out: Path, cam: Path, stream: Path) -> None:
    body = dict(result, meaning="stream_time = cam_time + offset_s; cam time 0 is the first frame of the first chunk",
                cam=str(cam), stream=str(stream), created=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("stream_dir", nargs="?", type=Path, help="folder with cam/ and stream-full.mp4")
    parser.add_argument("--cam", type=Path)
    parser.add_argument("--stream", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--stream-track", type=int, default=0, help="which audio track of the stream file (0 = first)")
    args = parser.parse_args(argv)
    cam = args.cam or (args.stream_dir / "cam" if args.stream_dir else None)
    stream = args.stream or (args.stream_dir / "stream-full.mp4" if args.stream_dir else None)
    if cam is None or stream is None:
        parser.error("give STREAM_DIR, or both --cam and --stream")
    out = args.out or ((cam if cam.is_dir() else cam.parent) / "sync.json")
    try:
        joined = cam / "cam-joined.mp4"
        result = estimate(load_audio(joined if joined.exists() else cam), load_audio(stream, args.stream_track))
    except SyncError as exc:
        print("sync: %s" % exc, file=sys.stderr)
        return 1
    write_result(result, out, cam, stream)
    print(json.dumps(result))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
