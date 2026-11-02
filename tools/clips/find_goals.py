#!/usr/bin/env python3
"""Detect goals in a Rocket League spectator recording from the score HUD.

Standard library only. Usage: python3 -I find_goals.py VIDEO [--out DIR]
See README.md for the output layout.
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from collections import namedtuple
from operator import sub

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir

from find_candidates import (ToolError, extract_frame, format_hms, log,  # noqa: E402
                             make_sheet_row, probe_duration, require_tools,
                             run_parallel)

FPS = 4                    # HUD samples per second
MIN_HOLD_SECONDS = 3.0     # a new score must persist this long to count
EVENT_LEAD_SECONDS = 0.25  # reported time is the first new sample minus this
REFERENCE_CAP_SECONDS = 60  # one run votes for at most this much time
MIN_REFERENCE_RUNS = 3     # a scoreboard value shows up in at least this many separate runs
PRESENT_TOL = 30.0         # a box this close to the scoreboard reference is the scoreboard
REFERENCE_TOL = 14.0       # raw box values this close count as one value when voting for the reference
BOX_TOL = 6.0              # glyph signatures this close (0..255 scale) are the same score
GLYPH_ROWS = range(3, 14)  # fingerprint window that holds the digit or hollow square
GLYPH_COLS = range(2, 13)
GLYPH_MARGIN = 25          # brightness above the box background that counts as glyph
GLYPH_GAIN = 3
FLASH_MARGIN_SAMPLES = 2   # samples either side of a change searched for the goal flash
SIMULTANEOUS_SECONDS = 2.0  # blue and orange cannot both score this close together
SERIES_GAP_SECONDS = 300   # games closer than this belong to the same series
STRIP_TOL = 18.0           # same, for the team-name strip used as a structure check
STRIP_WINDOW_SECONDS = 1.5  # strip must stay put this long either side of a change
WINDOW_MERGE_SECONDS = 20  # scoreboard dropouts shorter than this (replay dim) stay in one game
WINDOW_MIN_SECONDS = 120   # shorter scoreboard stretches are not a game

Run = namedtuple("Run", "start end rep")  # end is exclusive; rep is a representative sample
Change = namedtuple("Change", "index start distance before after")


def distance(a, b):
    """Mean absolute difference between two equal-length byte fingerprints."""
    return sum(map(abs, map(sub, a, b))) / len(a)


def split_runs(series, tol):
    """Cut a series into runs whose samples all sit within tol of the run's first sample."""
    runs, start = [], 0
    for i in range(1, len(series) + 1):
        if i == len(series) or distance(series[i], series[start]) > tol:
            runs.append((start, i))
            start = i
    return runs if series else []


def stable_runs(series, tol, min_len, accept=None):
    """Runs lasting at least min_len samples; flicker between equal runs is absorbed.

    Shorter runs (flicker, overlays, fades) are dropped, and neighbours that look
    alike once the flicker is gone are merged back into one run. When `accept` is
    given, runs whose representative sample it rejects (a dimmed replay HUD, game
    footage under the box) are dropped as well.
    """
    kept = []
    for start, end in split_runs(series, tol):
        if end - start < min_len:
            continue
        run = Run(start, end, series[(start + end) // 2])
        if accept is not None and not accept(run.rep):
            continue
        if kept and distance(kept[-1].rep, run.rep) <= tol:
            run = Run(kept[-1].start, run.end, kept[-1].rep)
            kept.pop()
        kept.append(run)
    return kept


def find_changes(series, tol, min_len, accept=None):
    """Persistent changes: consecutive stable runs whose values differ.

    The change index is the first sample after the old value, so it marks the
    moment the old score disappeared even if a replay dim follows right away.
    """
    runs = stable_runs(series, tol, min_len, accept)
    return [Change(before.end, after.start, distance(before.rep, after.rep), before, after)
            for before, after in zip(runs, runs[1:])]


def reference_fingerprint(series, tol, min_len, present_tol, cap=REFERENCE_CAP_SECONDS * FPS,
                          min_runs=MIN_REFERENCE_RUNS):
    """The fingerprint most like the scoreboard box.

    Every stable value votes for the stable value that most others resemble,
    weighted by run length capped at `cap` samples: scoreboard digits recur all
    match long, while one long static stretch (facecam, BRB) is only one run.
    Returns None when no value recurs at least min_runs times: no scoreboard at all.
    """
    runs = stable_runs(series, tol, min_len)
    if not runs:
        return None

    def support(candidate):
        return sum(min(r.end - r.start, cap) for r in runs
                   if distance(candidate, r.rep) <= present_tol)

    best = max((r.rep for r in runs), key=support)
    recurrences = sum(distance(best, r.rep) <= present_tol for r in runs)
    return best if recurrences >= min_runs else None


def presence_flags(series, ref, present_tol):
    """True for each sample that looks like the scoreboard box (near the reference)."""
    return [ref is not None and distance(x, ref) <= present_tol for x in series]


def strip_drift(strip, change, window):
    """Largest strip difference between samples around a change (scene cut detector)."""
    lo = max(0, change.before.end - 1 - window)
    hi = min(len(strip) - 1, change.after.start + window)
    ref_a, ref_b = strip[max(0, change.before.end - 1)], strip[change.after.start]
    return max(distance(ref_a, ref_b),
               max(distance(ref_a, strip[k]) for k in range(lo, change.before.end)),
               max(distance(ref_b, strip[k]) for k in range(change.after.start, hi + 1)))


def goal_confidence(change, drift, box_tol, strip_tol):
    """0..1: half for a confirmed change, half for how far the glyph moved, discounted
    by drift of the rest of the HUD."""
    moved = min(1.0, change.distance / (3 * box_tol))
    steady = max(0.0, 1.0 - drift / strip_tol)
    return round((0.5 + 0.5 * moved) * steady, 3)


def flash_seen(present, change, margin):
    """True if the scoreboard blinked out of its normal look around the change.

    Every real goal flashes the HUD for a moment; the slow tilt of the digit box
    changes the glyph a little without ever doing that.
    """
    lo = max(0, change.before.end - margin)
    hi = min(len(present), change.after.start + margin)
    return any(not ok for ok in present[lo:hi])


def glyph_signature(fp):
    """Brightness of the digit strokes above the box background, glyph window only.

    Ignores the box gradient, border and overall brightness, so only the digit
    (or the hollow square for zero) moves the value.
    """
    background = statistics.median(fp)
    out = bytearray()
    for r in GLYPH_ROWS:
        for c in GLYPH_COLS:
            excess = fp[r * FP_SIDE + c] - background - GLYPH_MARGIN
            out.append(0 if excess <= 0 else min(255, int(excess * GLYPH_GAIN)))
    return bytes(out)


def accepted_changes(box, strip, tol, strip_tol, min_len, window, accept=None):
    """(change, confidence) for each persistent change with a steady HUD structure."""
    out = []
    for change in find_changes(box, tol, min_len, accept):
        drift = strip_drift(strip, change, window)
        if drift <= strip_tol:
            out.append((change, goal_confidence(change, drift, tol, strip_tol)))
    return out


def merge_windows(present, merge_gap, min_len):
    """Contiguous True stretches as (start, end_exclusive); short gaps and blips ignored."""
    spans, start = [], None
    for i, flag in enumerate(list(present) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if spans and start - spans[-1][1] <= merge_gap:
                spans[-1] = (spans[-1][0], i)
            else:
                spans.append((start, i))
            start = None
    return [s for s in spans if s[1] - s[0] >= min_len]


def window_index(windows, sample):
    """Index of the window containing sample, or None."""
    for n, (start, end) in enumerate(windows):
        if start <= sample < end:
            return n
    return None


def count_scores(events, windows):
    """Tag events (dicts with 'sample' and 'side') with match index and running score.

    Events outside every window are dropped. The counter resets per window.
    """
    tally, tagged = {}, []
    for event in sorted(events, key=lambda e: e["sample"]):
        match = window_index(windows, event["sample"])
        if match is None:
            continue
        blue, orange = tally.get(match, (0, 0))
        blue, orange = (blue + 1, orange) if event["side"] == "blue" else (blue, orange + 1)
        tally[match] = (blue, orange)
        tagged.append({**event, "match": match + 1, "blue": blue, "orange": orange})
    return tagged


def drop_simultaneous(events, window):
    """Keep the stronger of two opposite-side events closer than `window` samples.

    A goal tilts the other box too; that echo is smaller than the real change.
    """
    ordered = sorted(events, key=lambda e: e["sample"])
    dropped = set()
    for i, a in enumerate(ordered):
        for j in range(i + 1, len(ordered)):
            b = ordered[j]
            if b["sample"] - a["sample"] > window:
                break
            if a["side"] != b["side"]:
                dropped.add(i if a["distance"] < b["distance"] else j)
    return [e for i, e in enumerate(ordered) if i not in dropped]


def group_series(windows, gap):
    """Series number (1-based) for each window; windows `gap` samples apart or less share one."""
    numbers, current = [], 0
    for n, (start, _) in enumerate(windows):
        if n == 0 or start - windows[n - 1][1] > gap:
            current += 1
        numbers.append(current)
    return numbers


def side_events(side, series, strip, present, window):
    """Goal events for one box inside one window (all inputs already sliced)."""
    absent = b"\xff" * len(GLYPH_ROWS) * len(GLYPH_COLS)
    signatures = [glyph_signature(fp) if ok else absent for fp, ok in zip(series, present)]
    found = accepted_changes(signatures, strip, BOX_TOL, STRIP_TOL,
                             round(MIN_HOLD_SECONDS * FPS), round(STRIP_WINDOW_SECONDS * FPS),
                             accept=lambda rep: rep != absent)
    return [{"sample": window[0] + c.index, "side": side, "distance": round(c.distance, 1),
             "confidence": conf} for c, conf in found
            if flash_seen(present, c, FLASH_MARGIN_SAMPLES)]


def detect_goals(blue, orange, strip):
    """(windows, tagged goal events) from the three per-sample fingerprint series."""
    refs = {name: reference_fingerprint(box, REFERENCE_TOL, round(MIN_HOLD_SECONDS * FPS / 2),
                                        PRESENT_TOL)
            for name, box in (("blue", blue), ("orange", orange))}
    present = {"blue": presence_flags(blue, refs["blue"], PRESENT_TOL),
               "orange": presence_flags(orange, refs["orange"], PRESENT_TOL)}
    both = [a and b for a, b in zip(present["blue"], present["orange"])]
    windows = merge_windows(both, WINDOW_MERGE_SECONDS * FPS, WINDOW_MIN_SECONDS * FPS)
    events = []
    for win in windows:
        lo, hi = win
        for side, box in (("blue", blue), ("orange", orange)):
            events += side_events(side, box[lo:hi], strip[lo:hi], present[side][lo:hi], win)
    events = drop_simultaneous(events, round(SIMULTANEOUS_SECONDS * FPS))
    return windows, count_scores(events, windows)


# ---------------------------------------------------------------- ffmpeg layer

# (width, height, x, y) of each HUD region in the 1920x1080 frame
CROPS = {"blue": (80, 90, 785, 0), "orange": (80, 90, 1060, 0), "strip": (260, 40, 500, 20)}
FP_SIDE = 16                       # each region is area-averaged down to 16x16 gray
FRAME_ROW = FP_SIDE * len(CROPS)   # the three fingerprints are hstacked: 48 px per row
FRAME_BYTES = FRAME_ROW * FP_SIDE
CHUNK_SECONDS = 300                # decode this much video per ffmpeg process
DEFAULT_WORKERS = 6                # libopenh264 is single-threaded: parallelise by time
SHEET_OFFSETS = (-8, -4, -1, 2, 5, 9)
SHEET_TILE_WIDTH = 320


def hud_filter():
    """One filter graph: sample at FPS, crop and shrink each region, hstack them."""
    parts = [f"[0:v]fps={FPS},split={len(CROPS)}" + "".join(f"[s{i}]" for i in range(len(CROPS)))]
    for i, (w, h, x, y) in enumerate(CROPS.values()):
        parts.append(f"[s{i}]crop={w}:{h}:{x}:{y},scale={FP_SIDE}:{FP_SIDE}:flags=area,"
                     f"format=gray[c{i}]")
    parts.append("".join(f"[c{i}]" for i in range(len(CROPS))) + f"hstack=inputs={len(CROPS)}")
    return ";".join(parts)


def hud_command(video, start, length):
    return ["ffmpeg", "-v", "error", "-nostdin", "-ss", str(start), "-t", str(length),
            "-i", video, "-an", "-sn", "-filter_complex", hud_filter(),
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]


def chunk_plan(duration, chunk=CHUNK_SECONDS):
    """[(start, length)] whole-second chunks covering the video."""
    total = int(duration)
    return [(s, min(chunk, total - s)) for s in range(0, total, chunk)]


def split_frame(frame):
    """Cut one hstacked 48x16 gray frame into (blue, orange, strip) 256-byte fingerprints."""
    rows = [frame[r * FRAME_ROW:(r + 1) * FRAME_ROW] for r in range(FP_SIDE)]
    return tuple(b"".join(row[k * FP_SIDE:(k + 1) * FP_SIDE] for row in rows)
                 for k in range(len(CROPS)))


def decode_chunk(video, start, length):
    """Fingerprint frames for one time chunk, as a list of 768-byte frames."""
    proc = subprocess.run(hud_command(video, start, length), stdin=subprocess.DEVNULL,
                          capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise ToolError(f"ffmpeg failed at {start}s: " + " | ".join(tail))
    data = proc.stdout
    frames = [data[i:i + FRAME_BYTES]
              for i in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES)][:length * FPS]
    while frames and len(frames) < length * FPS:  # keep chunks sample-aligned
        frames.append(frames[-1])
    return frames


def read_hud(video, duration, workers):
    """Decode the whole video in parallel chunks; returns one frame per 1/FPS second."""
    plan = chunk_plan(duration)
    log(f"hud pass: {len(plan)} chunks of {CHUNK_SECONDS}s on {workers} workers")
    frames = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, chunk in enumerate(pool.map(lambda p: decode_chunk(video, *p), plan), 1):
            frames.extend(chunk)
            log(f"hud pass: {n}/{len(plan)} chunks")
    return frames


# ------------------------------------------------------------- goal sheets

def sheet_times(t, duration):
    """[(offset, time)] for the six contact strip tiles, clamped inside the video."""
    return [(o, min(max(t + o, 0.0), max(duration - 1.0, 0.0))) for o in SHEET_OFFSETS]


def build_goal_sheets(video, out_dir, goals, duration):
    """One 6 tile strip per goal under goal-sheets/, timestamps burned in."""
    sheet_dir = os.path.join(out_dir, "goal-sheets")
    work = os.path.join(sheet_dir, "_tiles")
    os.makedirs(work, exist_ok=True)
    plan = {g["n"]: sheet_times(g["t"], duration) for g in goals}
    tile = lambda n, k: os.path.join(work, f"g{n:03d}-{k}.jpg")  # noqa: E731
    jobs = [(lambda n=n, k=k, tt=tt: extract_frame(video, tt, tile(n, k), SHEET_TILE_WIDTH))
            for n, times in plan.items() for k, (_, tt) in enumerate(times)]
    log(f"extracting {len(jobs)} goal tiles")
    run_parallel(jobs)
    strips = []
    for n, times in plan.items():
        labels = [f"{format_hms(tt)} {o:+d}s" for o, tt in times]
        dest = os.path.join(sheet_dir, f"goal-{n:03d}.jpg")
        paths = [tile(n, k) for k in range(len(times))]
        strips.append(lambda p=paths, l=labels, d=dest: make_sheet_row(p, l, d))
    run_parallel(strips)
    shutil.rmtree(work)


# ------------------------------------------------------------------------ main

def goal_record(n, event):
    t = max(0.0, event["sample"] / FPS - EVENT_LEAD_SECONDS)
    return {"n": n, "t": round(t, 2), "hh:mm:ss": format_hms(t), "side": event["side"],
            "confidence": event["confidence"], "match": event["match"],
            "blue": event["blue"], "orange": event["orange"],
            "sheet": f"goal-sheets/goal-{n:03d}.jpg"}


def window_records(windows, goals):
    series = group_series(windows, SERIES_GAP_SECONDS * FPS)
    out = []
    for i, ((start, end), number) in enumerate(zip(windows, series), 1):
        mine = [g for g in goals if g["match"] == i]
        final = (mine[-1]["blue"], mine[-1]["orange"]) if mine else (0, 0)
        out.append({"match": i, "series": number, "start": start / FPS, "end": end / FPS,
                    "start_hms": format_hms(start / FPS), "end_hms": format_hms(end / FPS),
                    "final_blue": final[0], "final_orange": final[1]})
    return out


def build_report(video, windows, events):
    goals = [goal_record(n, e) for n, e in enumerate(events, 1)]
    return {"video": os.path.basename(video), "sample_fps": FPS,
            "match_windows": window_records(windows, goals), "goals": goals}


def load_or_decode(video, duration, out_dir, args):
    """Frames from the saved fingerprint file (--reuse) or a fresh ffmpeg pass."""
    cache = os.path.join(out_dir, "hud-fingerprints.bin")
    if args.reuse and os.path.isfile(cache):
        with open(cache, "rb") as handle:
            data = handle.read()
        log(f"reusing {cache}")
        return [data[i:i + FRAME_BYTES] for i in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES)]
    frames = read_hud(video, duration, args.workers)
    with open(cache, "wb") as handle:
        handle.write(b"".join(frames))
    return frames


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", help="path to the stream recording")
    parser.add_argument("--out", help="output dir (default: <video dir>/analysis)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="parallel ffmpeg decoders")
    parser.add_argument("--reuse", action="store_true",
                        help="reuse hud-fingerprints.bin from an earlier run")
    return parser.parse_args(argv)


def run(args):
    video = os.path.abspath(args.video)
    if not os.path.isfile(video):
        raise ToolError(f"input video not found: {video}")
    require_tools()
    out_dir = args.out or os.path.join(os.path.dirname(video), "analysis")
    os.makedirs(out_dir, exist_ok=True)
    duration = probe_duration(video)
    frames = load_or_decode(video, duration, out_dir, args)
    boxes = [split_frame(f) for f in frames]
    windows, events = detect_goals([b[0] for b in boxes], [b[1] for b in boxes],
                                   [b[2] for b in boxes])
    report = build_report(video, windows, events)
    log(f"found {len(report['goals'])} goals in {len(windows)} game windows")
    build_goal_sheets(video, out_dir, report["goals"], duration)
    with open(os.path.join(out_dir, "goals.json"), "w") as handle:
        json.dump(report, handle, indent=2)
    log(f"done -> {out_dir}")


def main(argv=None):
    started = time.time()
    try:
        run(parse_args(argv))
    except ToolError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    log(f"runtime {time.time() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
