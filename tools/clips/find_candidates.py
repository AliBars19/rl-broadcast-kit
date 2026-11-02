#!/usr/bin/env python3
"""Find candidate highlight moments in a long Rocket League stream recording.

Standard library only. Usage: python3 -I find_candidates.py VIDEO [--out DIR]
See README.md for the output layout.
"""
import argparse
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from operator import mul

SILENCE_DB = -60.0       # RMS below this counts as near-silent
SILENCE_MIN_SECONDS = 20  # a quiet stretch must last this long to be dropped
SKIP_FIRST_SECONDS = 180  # ignore the intro
MIN_SEPARATION = 10       # seconds between two picked candidates
TOP_N = 80
SMOOTH_RADIUS = 1         # seconds either side for score smoothing
RISE_WINDOW = 30          # seconds of lead-in used for the "sudden jump" term
WEIGHT_RMS, WEIGHT_HIGH, WEIGHT_RISE = 0.4, 0.4, 0.2

SAMPLE_RATE = 16000       # analysis rate; enough headroom above the 2 kHz split
HIGH_BAND_HZ = 2000
FLOOR_DB = -120.0
FRAME_OFFSETS = (-6, -3, 0, 3, 6)
CAND_THUMB_WIDTH = 384
OVERVIEW_WIDTH = 320
OVERVIEW_STEP = 30
OVERVIEW_COLS, OVERVIEW_ROWS = 6, 5
FFMPEG_WORKERS = 4


def format_hms(seconds):
    """Format seconds as zero-padded hh:mm:ss (fractions truncate)."""
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def silent_mask(rms_db, threshold_db=SILENCE_DB, min_len=SILENCE_MIN_SECONDS):
    """True for every second inside a quiet run of at least min_len seconds."""
    mask = [False] * len(rms_db)
    start = None
    for i, value in enumerate(list(rms_db) + [math.inf]):
        if value < threshold_db:
            start = i if start is None else start
            continue
        if start is not None and i - start >= min_len:
            mask[start:i] = [True] * (i - start)
        start = None
    return mask


def eligible_mask(silent, skip_first=SKIP_FIRST_SECONDS):
    """Seconds that may be picked: past the intro and outside silent stretches."""
    return [i >= skip_first and not s for i, s in enumerate(silent)]


def _smooth(values, radius):
    """Centered moving average with edge clamping."""
    out = []
    for i in range(len(values)):
        window = values[max(0, i - radius): i + radius + 1]
        out.append(sum(window) / len(window))
    return out


def _rise(values, window):
    """Each value minus the mean of the preceding `window` values."""
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    out = []
    for i, v in enumerate(values):
        lo = max(0, i - window)
        out.append(v - (prefix[i] - prefix[lo]) / (i - lo) if i > lo else 0.0)
    return out


def _zscores(values, eligible):
    """Z-score using mean/stdev of eligible seconds only (flat signal gives 0)."""
    sample = [v for v, ok in zip(values, eligible) if ok] or list(values) or [0.0]
    mean = statistics.fmean(sample)
    spread = statistics.pstdev(sample) or 1.0
    return [(v - mean) / spread for v in values]


def combined_scores(rms_db, high_db, eligible):
    """Blend overall loudness, high-band energy and sudden rise into one score."""
    rms = _smooth(rms_db, SMOOTH_RADIUS)
    high = _smooth(high_db, SMOOTH_RADIUS)
    z_rms = _zscores(rms, eligible)
    z_high = _zscores(high, eligible)
    z_rise = _zscores(_rise(rms, RISE_WINDOW), eligible)
    return [WEIGHT_RMS * a + WEIGHT_HIGH * b + WEIGHT_RISE * c
            for a, b, c in zip(z_rms, z_high, z_rise)]


def pick_peaks(scores, eligible, min_sep=MIN_SEPARATION, limit=TOP_N):
    """Greedy top-N picking: best score first, skipping ones too close to a pick."""
    order = sorted((i for i, ok in enumerate(eligible) if ok),
                   key=lambda i: scores[i], reverse=True)
    picked = []
    for i in order:
        if len(picked) >= limit:
            break
        if all(abs(i - j) >= min_sep for j in picked):
            picked.append(i)
    return picked


# ---------------------------------------------------------------- ffmpeg layer

class ToolError(Exception):
    """A user-facing failure (bad input, ffmpeg problem)."""


def require_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise ToolError(f"{tool} not found on PATH")


def run_tool(cmd):
    """Run a command, returning stdout; raise ToolError with stderr on failure."""
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-5:]
        raise ToolError(f"{cmd[0]} failed ({proc.returncode}): " + " | ".join(tail))
    return proc.stdout


def probe_duration(video):
    out = run_tool(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", video])
    try:
        return float(out.strip())
    except ValueError:
        raise ToolError(f"could not read duration of {video}")


def to_db(amplitude):
    """16-bit sample amplitude to dBFS, floored for digital silence."""
    return max(FLOOR_DB, 20 * math.log10(amplitude / 32768)) if amplitude > 0 else FLOOR_DB


def rms_of(samples):
    return math.sqrt(sum(map(mul, samples, samples)) / len(samples))


def measure_second(interleaved):
    """(rms_db, peak_db, high_rms_db) from interleaved [full, high-passed] samples."""
    full, high = interleaved[0::2], interleaved[1::2]
    peak = max(max(full), -min(full))
    return to_db(rms_of(full)), to_db(peak), to_db(rms_of(high))


def loudness_command(video):
    mono = "aformat=sample_fmts=s16:channel_layouts=mono"
    graph = (f"[0:a]pan=mono|c0=0.5*c0+0.5*c1,aresample={SAMPLE_RATE},{mono},asplit[a][b];"
             f"[b]highpass=f={HIGH_BAND_HZ},{mono}[h];[a][h]amerge=inputs=2")
    return ["ffmpeg", "-v", "error", "-nostdin", "-i", video, "-vn",
            "-filter_complex", graph, "-f", "s16le", "-ac", "2",
            "-ar", str(SAMPLE_RATE), "pipe:1"]


def analyse_loudness(video, seconds):
    """Stream decoded audio from ffmpeg and measure it one second at a time."""
    chunk_bytes = SAMPLE_RATE * 2 * 2
    rows = []
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(loudness_command(video), stdout=subprocess.PIPE,
                                stderr=err, stdin=subprocess.DEVNULL)
        while True:  # read to EOF so ffmpeg exits cleanly; drop the partial last second
            raw = proc.stdout.read(chunk_bytes)
            if len(raw) < chunk_bytes:
                break
            rows.append(measure_second(array("h", raw)))
            if len(rows) % 600 == 0:
                log(f"loudness: {len(rows)}/{seconds} s")
        proc.stdout.close()
        proc.wait()
        err.seek(0)
        message = err.read().decode(errors="replace").strip()
    if proc.returncode != 0 or not rows:
        raise ToolError("audio decode failed: " + (message or "no audio samples"))
    return rows[:seconds]


def log(message):
    print(message, file=sys.stderr, flush=True)


def write_loudness_csv(path, rows):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "rms_db", "peak_db", "high_rms_db"])
        for t, (rms, peak, high) in enumerate(rows):
            writer.writerow([t, f"{rms:.2f}", f"{peak:.2f}", f"{high:.2f}"])


# ------------------------------------------------- thumbnails and contact sheets

def label_filter(text, size):
    """drawtext filter that burns `text` into the top-left corner."""
    safe = text.replace(":", "\\:")
    return (f"drawtext=text='{safe}':fontcolor=white:fontsize={size}:"
            "box=1:boxcolor=black@0.65:boxborderw=4:x=6:y=6")


def extract_frame(video, t, dest, width, label=None):
    """Grab one scaled JPG at time t (fast input seek), optionally with a label."""
    vf = f"scale={width}:-2"
    if label:
        vf += "," + label_filter(label, 18)
    run_tool(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", video,
              "-frames:v", "1", "-vf", vf, "-q:v", "3", "-update", "1", "-y", dest])


def run_parallel(jobs):
    """Run zero-argument callables on a small pool; re-raise the first error."""
    with ThreadPoolExecutor(max_workers=FFMPEG_WORKERS) as pool:
        for future in [pool.submit(job) for job in jobs]:
            future.result()


def offset_name(offset):
    return "t00" if offset == 0 else f"{'m' if offset < 0 else 'p'}{abs(offset):02d}"


def clamp_time(t, duration):
    return min(max(t, 0.0), max(duration - 1.0, 0.0))


def candidate_frames(rank, t, duration):
    """[(relative_path, actual_time)] for the five thumbnails of one candidate."""
    return [(f"frames/cand-{rank:03d}-{offset_name(o)}.jpg", clamp_time(t + o, duration))
            for o in FRAME_OFFSETS]


def make_sheet_row(frame_paths, labels, dest):
    """Hstack frames into one 5x1 contact sheet with a timestamp on each tile."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    for path in frame_paths:
        cmd += ["-i", path]
    parts = [f"[{i}:v]{label_filter(text, 20)}[v{i}]" for i, text in enumerate(labels)]
    chain = "".join(f"[v{i}]" for i in range(len(labels)))
    graph = ";".join(parts) + f";{chain}hstack=inputs={len(labels)}"
    run_tool(cmd + ["-filter_complex", graph, "-frames:v", "1", "-q:v", "3",
                    "-update", "1", "-y", dest])


def build_candidate_assets(video, out_dir, picks, duration):
    """Extract all thumbnails, then build one sheet per candidate."""
    os.makedirs(os.path.join(out_dir, "frames"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "sheets"), exist_ok=True)
    plan = {rank: candidate_frames(rank, t, duration) for rank, t in picks}
    jobs = [(lambda d=os.path.join(out_dir, rel), tt=tt: extract_frame(video, tt, d, CAND_THUMB_WIDTH))
            for frames in plan.values() for rel, tt in frames]
    log(f"extracting {len(jobs)} candidate thumbnails")
    run_parallel(jobs)
    sheet_jobs = []
    for rank, frames in plan.items():
        paths = [os.path.join(out_dir, rel) for rel, _ in frames]
        labels = [format_hms(tt) for _, tt in frames]
        dest = os.path.join(out_dir, "sheets", f"cand-{rank:03d}.jpg")
        sheet_jobs.append(lambda p=paths, l=labels, d=dest: make_sheet_row(p, l, d))
    run_parallel(sheet_jobs)
    return {rank: [rel for rel, _ in frames] for rank, frames in plan.items()}


def overview_times(duration):
    return [float(t) for t in range(0, int(duration), OVERVIEW_STEP)]


def build_overview(video, out_dir, duration):
    """Contact sheets of the whole stream: one tile every OVERVIEW_STEP seconds."""
    dest_dir = os.path.join(out_dir, "overview")
    work = os.path.join(dest_dir, "_tiles")
    per_sheet = OVERVIEW_COLS * OVERVIEW_ROWS
    times = overview_times(duration)
    sheets = [times[i:i + per_sheet] for i in range(0, len(times), per_sheet)]
    jobs = []
    for n, chunk in enumerate(sheets, 1):
        os.makedirs(os.path.join(work, f"sheet-{n:02d}"), exist_ok=True)
        for k, t in enumerate(chunk, 1):
            dest = os.path.join(work, f"sheet-{n:02d}", f"f-{k:03d}.jpg")
            jobs.append(lambda t=t, d=dest: extract_frame(
                video, t, d, OVERVIEW_WIDTH, label=format_hms(t)))
    log(f"extracting {len(jobs)} overview tiles for {len(sheets)} sheets")
    run_parallel(jobs)
    for n in range(1, len(sheets) + 1):
        tile_sheet(os.path.join(work, f"sheet-{n:02d}"),
                   os.path.join(dest_dir, f"overview-{n:02d}.jpg"))
    shutil.rmtree(work)
    return len(sheets)


def tile_sheet(tiles_dir, dest):
    run_tool(["ffmpeg", "-v", "error", "-nostdin", "-framerate", "1",
              "-i", os.path.join(tiles_dir, "f-%03d.jpg"),
              "-vf", f"tile={OVERVIEW_COLS}x{OVERVIEW_ROWS}",
              "-frames:v", "1", "-q:v", "3", "-update", "1", "-y", dest])


# ------------------------------------------------------------------------ main

def build_candidates(rows, picks, scores, frame_map):
    out = []
    for rank, t in picks:
        rms, peak, high = rows[t]
        out.append({"rank": rank, "t": t, "hh:mm:ss": format_hms(t),
                    "score": round(scores[t], 3), "rms_db": round(rms, 2),
                    "peak_db": round(peak, 2), "high_rms_db": round(high, 2),
                    "frames": frame_map[rank]})
    return out


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", help="path to the stream recording")
    parser.add_argument("--out", help="output dir (default: <video dir>/analysis)")
    parser.add_argument("--top", type=int, default=TOP_N, help="number of candidates")
    return parser.parse_args(argv)


def run(args):
    video = os.path.abspath(args.video)
    if not os.path.isfile(video):
        raise ToolError(f"input video not found: {video}")
    require_tools()
    out_dir = args.out or os.path.join(os.path.dirname(video), "analysis")
    os.makedirs(out_dir, exist_ok=True)
    duration = probe_duration(video)
    rows = analyse_loudness(video, int(duration))
    write_loudness_csv(os.path.join(out_dir, "loudness.csv"), rows)
    silent = silent_mask([r[0] for r in rows])
    eligible = eligible_mask(silent)
    scores = combined_scores([r[0] for r in rows], [r[2] for r in rows], eligible)
    picks = list(enumerate(pick_peaks(scores, eligible, MIN_SEPARATION, args.top), 1))
    frame_map = build_candidate_assets(video, out_dir, picks, duration)
    with open(os.path.join(out_dir, "candidates.json"), "w") as handle:
        json.dump(build_candidates(rows, picks, scores, frame_map), handle, indent=2)
    sheets = build_overview(video, out_dir, duration)
    log(f"done: {len(picks)} candidates, {sheets} overview sheets -> {out_dir}")


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
