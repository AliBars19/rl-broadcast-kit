#!/usr/bin/env python3
"""Find the top-right stat popups (goal, shot, save, demo ...) of a Rocket League recording.

Standard library only. Usage: python3 -I find_feed_events.py VIDEO [--out DIR]
See README.md for the output layout.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir

from find_candidates import (ToolError, format_hms, label_filter, log,  # noqa: E402
                             require_tools, run_tool)

FPS = 4                      # popup samples per second
CROP = (360, 150, 1560, 0)   # (w, h, x, y) of the popup area in the 1920x1080 frame
SCALE = 2                    # the crop is area-averaged down by this factor
W2, H2 = CROP[0] // SCALE, CROP[1] // SCALE
FRAME_BYTES = W2 * H2 * 3
CHUNK_SECONDS = 150          # decode this much video per ffmpeg process
DEFAULT_WORKERS = 12         # libopenh264 is single-threaded: parallelise by time

# Geometry in crop-space half-resolution pixels. Rows are 55.5 full-res px apart;
# the player panel hides everything below y=150, so row 2 is only partly visible.
ROWS = 3
ROW_PITCH = 27.75
CHIP_TOP = 13                # top edge of the chip in row 0
CHIP_ROWS = (2, 3, 14, 15)   # sampled lines (above and below the name text)
COL_STEP = 2
MERGE_GAP = 4 * COL_STEP     # name text opens gaps this wide inside one chip
MIN_CHIP = 24                # shorter colour runs are noise
SINGLE_MIN_END, SINGLE_MAX_END = 135, 157   # chip right end of a popup with a right-hand icon
DEMO_GAP = (10, 45)          # gap between the two chips of a demolition style popup
ICON_X, ICON_SIZE, ICON_H, ICON_TOP = 145, 30, 27, 9  # icon box at the right end (half-res)
DEMO_ICON_BACK = 34          # demolition icon box starts this far left of the right chip
DEMO_MIN_WHITE = 100         # the bomb is a big white disk: white pixels needed in its box
ICON_MIN_WARM, CLIPPED_MIN_WARM = 20, 8
FP_GRID = 12                 # icon fingerprint: 12x12 cells x (warm, white) channels
FP_BYTES = FP_GRID * FP_GRID * 2

MATCH_FP_TOL = 0.4           # icons closer than this (cosine) in a row are the same popup
MISMATCH_COST, MISSING_COST, SHIFT_COST = 0.2, 1.5, 0.3
MAX_MISS = 4                 # samples a popup may drop out of detection (1 s)
MIN_SEEN = 4                 # a popup is seen in at least this many samples (1 s)
SKIP_ANIMATION = 2           # first samples are the pop-in animation: not used for the icon
CLUSTER_TOL = 0.22
SMALL_CLUSTER = 2            # clusters this small are checked against bigger ones
ABSORB_TOL = 0.30            # and joined to the nearest bigger one when that close
STRIP_OFFSETS = range(-1, 7)
EXAMPLES = 12
GOAL_WINDOW = (-1.0, 3.5)    # seconds after a goal in which a same side popup counts as a hint

Row = namedtuple("Row", "k colours kind fp clipped x0")
Event = namedtuple("Event", "t duration colours kind fp t_rep k_rep x_rep")


# ---------------------------------------------------------------- per frame

def pixel(frame, x, y):
    o = (y * W2 + x) * 3
    return frame[o], frame[o + 1], frame[o + 2]


def classify_chip(r, g, b):
    """'blue' or 'orange' for the pastel body colour of a name chip, else None."""
    if b > 185 and b - r > 80 and b - g > 50:
        return "blue"
    if r > 200 and r - b > 70 and r - g > 35 and g < 200:
        return "orange"
    return None


def row_top(k):
    return round(CHIP_TOP + ROW_PITCH * k)


def icon_top(k):
    return round(ICON_TOP + ROW_PITCH * k)


def body_rows(k):
    return [y for y in (row_top(k) + o for o in CHIP_ROWS) if y < H2]


def column_flags(frame, k):
    """Chip colour (or None) of every COL_STEP-th column of row k."""
    rows = body_rows(k)
    need = 2 if len(rows) >= 4 else 1
    flags = []
    for x in range(0, W2, COL_STEP):
        counts = {"blue": 0, "orange": 0}
        for y in rows:
            colour = classify_chip(*pixel(frame, x, y))
            if colour:
                counts[colour] += 1
        best = max(counts, key=counts.get)
        flags.append(best if counts[best] >= need else None)
    return flags


def flag_runs(flags):
    """[(colour, x0, x1)] of same colour columns; short gaps bridged, short runs dropped."""
    runs, cur = [], None
    for i, colour in enumerate(flags + [None] * (MERGE_GAP // COL_STEP + 1)):
        if cur and colour == cur[0]:
            cur[2] = i
        elif cur and colour is None and i - cur[2] <= MERGE_GAP // COL_STEP:
            continue
        else:
            if cur and (cur[2] + 1 - cur[1]) * COL_STEP >= MIN_CHIP:
                runs.append((cur[0], cur[1] * COL_STEP, (cur[2] + 1) * COL_STEP))
            cur = [colour, i, i] if colour else None
    return runs


def icon_fp(frame, x0, y0):
    """12x12 grid, two channels: share of warm (gold) and of white pixels per cell.

    Independent of the background behind the transparent icon.
    """
    warm = [0] * (FP_GRID * FP_GRID)
    white = [0] * (FP_GRID * FP_GRID)
    for y in range(max(y0, 0), min(y0 + ICON_H, H2)):
        base = (y - y0) * FP_GRID // ICON_H * FP_GRID
        for x in range(max(x0, 0), min(x0 + ICON_SIZE, W2)):
            r, g, b = pixel(frame, x, y)
            cell = base + (x - x0) * FP_GRID // ICON_SIZE
            if r > 215 and g > 195 and r - b > 45:
                warm[cell] += 1
            elif min(r, g, b) > 215:
                white[cell] += 1
    cell_px = ICON_SIZE * ICON_H / FP_GRID ** 2
    return bytes(min(255, int(v * 255 / cell_px)) for v in warm + white)


def white_pixels(fp):
    """Approximate number of white pixels an icon fingerprint stands for."""
    return sum(fp[FP_GRID * FP_GRID:]) / 255 * ICON_SIZE * ICON_H / FP_GRID ** 2


def warm_count(frame, x0, y0):
    total = 0
    for y in range(max(y0, 0), min(y0 + ICON_H, H2)):
        for x in range(max(x0, 0), min(x0 + ICON_SIZE, W2)):
            r, g, b = pixel(frame, x, y)
            total += r > 215 and g > 195 and r - b > 45
    return total


def read_row(frame, k):
    """The popup in row k as a Row, or None."""
    runs = chip_runs(frame, k)
    if not runs:
        return None
    clipped = icon_top(k) + ICON_H > H2
    last = runs[-1]
    if SINGLE_MIN_END <= last[2] <= SINGLE_MAX_END:
        need = CLIPPED_MIN_WARM if clipped else ICON_MIN_WARM
        if warm_count(frame, ICON_X, icon_top(k)) < need:
            return None
        fp = None if clipped else icon_fp(frame, ICON_X, icon_top(k))
        return Row(k, (last[0],), "single", fp, clipped, ICON_X)
    if last[2] > SINGLE_MAX_END and len(runs) >= 2:
        left = runs[-2]
        gap = last[1] - left[2]
        if DEMO_GAP[0] <= gap <= DEMO_GAP[1]:
            x0 = max(0, last[1] - DEMO_ICON_BACK)
            fp = None if clipped else icon_fp(frame, x0, icon_top(k))
            if fp is None or white_pixels(fp) >= DEMO_MIN_WHITE:
                return Row(k, (left[0], last[0]), "demo", fp, clipped, x0)
    return None


def chip_runs(frame, k):
    return flag_runs(column_flags(frame, k))


def observe(frame):
    """All popups visible in one frame, top to bottom."""
    return [row for row in (read_row(frame, k) for k in range(ROWS)) if row]


# ------------------------------------------------------------------ tracking

def fp_distance(a, b):
    """Cosine distance (0 same shape .. 1 nothing in common) between two fingerprints.

    Brightness and amount of icon pixels matter much less than where they are, and
    the mean absolute difference of these sparse grids barely separates different icons.
    """
    na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0 if na == nb else 1.0
    return 1.0 - sum(x * y for x, y in zip(a, b)) / (na * nb)


class Track:
    """One popup followed over time."""
    __slots__ = ("first", "last", "seen", "missed", "colours", "kind", "last_fp", "samples")

    def __init__(self, index, row):
        self.first = self.last = index
        self.seen, self.missed = 0, 0
        self.colours, self.kind, self.last_fp = row.colours, row.kind, None
        self.samples = []
        self.update(index, row)

    def update(self, index, row):
        self.last, self.missed = index, 0
        self.seen += 1
        if row.fp is not None:
            self.last_fp = row.fp
        self.samples.append((index, row))


def mismatch(track, row):
    if row is None:
        return 0.0
    if track.colours != row.colours or track.kind != row.kind:
        return 3.0
    if track.last_fp is not None and row.fp is not None \
            and fp_distance(track.last_fp, row.fp) > MATCH_FP_TOL:
        return MISMATCH_COST
    return 0.0


def alignment_cost(active, rows, shift):
    """Cost of explaining the visible rows by active[shift:], the front `shift` having expired."""
    kept = active[shift:]
    cost = sum(mismatch(t, r) for t, r in zip(kept, rows))
    return cost + MISSING_COST * max(0, len(kept) - len(rows)) + SHIFT_COST * shift


def by_row(rows):
    """Visible rows as a list indexed by row number (None for a row nobody was seen in)."""
    out = [None] * (max((r.k for r in rows), default=-1) + 1)
    for r in rows:
        out[r.k] = r
    return out


def advance(active, rows, index):
    """Match one sample's rows to the ordered active tracks.

    Popups expire from the top and new ones join at the bottom, so the rows are
    the surviving tracks in order followed by new ones; the stack shifting up
    shows as expired tracks at the front.
    Returns (still active, closed).
    """
    slots = by_row(rows)
    shift = 0
    if slots:
        shift = min(range(len(active) + 1), key=lambda s: (alignment_cost(active, slots, s), s))
    closed, kept = active[:shift], active[shift:]
    for i, track in enumerate(kept):
        if i < len(slots) and slots[i] is not None:
            track.update(index, slots[i])
        else:
            track.missed += 1
    for slot in slots[len(kept):]:
        if slot is not None:
            kept.append(Track(index, slot))
    closed += [t for t in kept if t.missed > MAX_MISS]
    return [t for t in kept if t.missed <= MAX_MISS], closed


def median_fp(fps):
    return bytes(sorted(column)[len(column) // 2] for column in zip(*fps))


def make_event(track, times):
    """Event for a finished track, or None when it was seen too briefly."""
    if track.seen < MIN_SEEN:
        return None
    usable = [(i, r) for i, r in track.samples if r.fp is not None]
    if len(usable) > 2 * SKIP_ANIMATION:
        usable = usable[SKIP_ANIMATION:]
    if not usable:
        return None
    mid_i, mid_row = usable[len(usable) // 2]
    t = times[track.first]
    return Event(t, times[track.last] - t + 1.0 / FPS, track.colours, track.kind,
                 median_fp([r.fp for _, r in usable]), times[mid_i], mid_row.k, mid_row.x0)


def track_events(samples):
    """Events from a window's [(t, rows)] samples, ordered by start time."""
    times = [t for t, _ in samples]
    active, tracks = [], []
    for index, (_, rows) in enumerate(samples):
        active, closed = advance(active, rows, index)
        tracks += closed
    tracks += active
    events = [make_event(t, times) for t in tracks]
    return sorted((e for e in events if e), key=lambda e: e.t)


# ---------------------------------------------------------------- clustering

def nearest(centres, fp):
    best = min(range(len(centres)), key=lambda i: fp_distance(centres[i], fp))
    return best, fp_distance(centres[best], fp)


def mean_fp(members):
    return bytes(sum(c) // len(c) for c in zip(*members))


def merge_close(centres, labels, threshold):
    """Relabel so clusters whose centres are within threshold of each other share one label."""
    parent = list(range(len(centres)))

    def root(c):
        while parent[c] != c:
            c = parent[c]
        return c

    for a in range(len(centres)):
        for b in range(a + 1, len(centres)):
            if fp_distance(centres[a], centres[b]) <= threshold:
                parent[root(b)] = root(a)
    return [root(lab) for lab in labels]


def absorb_small(fps, labels, tol):
    """Move events of tiny clusters (blurred or half covered icons) to the nearest big cluster."""
    members = {}
    for fp, lab in zip(fps, labels):
        members.setdefault(lab, []).append(fp)
    big = {lab: mean_fp(m) for lab, m in members.items() if len(m) > SMALL_CLUSTER}
    moved = {}
    for lab, m in members.items():
        if len(m) > SMALL_CLUSTER or not big:
            continue
        near = min(big, key=lambda b: fp_distance(big[b], mean_fp(m)))
        if fp_distance(big[near], mean_fp(m)) <= tol:
            moved[lab] = near
    return [moved.get(lab, lab) for lab in labels]


def cluster_fps(fps, threshold):
    """Cluster ids (1 = biggest): greedy nearest-centre assignment, two refinement passes, then
    merging of clusters whose centres ended up close."""
    if not fps:
        return []
    centres, labels = [], []
    for fp in fps:
        best, dist = nearest(centres, fp) if centres else (0, threshold + 1)
        if dist <= threshold:
            labels.append(best)
        else:
            centres.append(fp)
            labels.append(len(centres) - 1)
    for _ in range(2):
        groups = {}
        for fp, lab in zip(fps, labels):
            groups.setdefault(lab, []).append(fp)
        centres = [mean_fp(groups[c]) if c in groups else centres[c] for c in range(len(centres))]
        labels = [nearest(centres, fp)[0] for fp in fps]
    labels = absorb_small(fps, merge_close(centres, labels, threshold), ABSORB_TOL)
    order = sorted(set(labels), key=lambda c: (-labels.count(c), labels.index(c)))
    rank = {c: n for n, c in enumerate(order, 1)}
    return [rank[lab] for lab in labels]


# ------------------------------------------------------------- ffmpeg layer

def chunk_plan(windows, chunk=CHUNK_SECONDS):
    """[(start, samples)] chunks of at most `chunk` seconds covering every (start, end) window."""
    plan = []
    for start, end in windows:
        t = start
        while t < end - 1e-6:
            length = min(chunk, end - t)
            plan.append((t, round(length * FPS)))
            t += length
    return plan


def crop_command(video, start, samples):
    w, h, x, y = CROP
    vf = f"fps={FPS},crop={w}:{h}:{x}:{y},scale={W2}:{H2}:flags=area"
    return ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t",
            f"{samples / FPS:.3f}", "-i", video, "-an", "-sn", "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def decode_chunk(video, start, samples):
    """Raw RGB crops for one chunk, exactly `samples` frames."""
    proc = subprocess.run(crop_command(video, start, samples), stdin=subprocess.DEVNULL,
                          capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise ToolError(f"ffmpeg failed at {start}s: " + " | ".join(tail))
    data = proc.stdout[:samples * FRAME_BYTES]
    if len(data) < FRAME_BYTES:
        raise ToolError(f"ffmpeg returned no frames at {start}s")
    missing = samples * FRAME_BYTES - len(data)
    return data + data[-FRAME_BYTES:] * (missing // FRAME_BYTES)


def decode_all(video, plan, path, workers):
    """Decode every chunk in parallel and store the crops back to back in `path`."""
    log(f"decode: {len(plan)} chunks on {workers} workers")
    with open(path, "wb") as out, ThreadPoolExecutor(max_workers=workers) as pool:
        for n, data in enumerate(pool.map(lambda p: decode_chunk(video, *p), plan), 1):
            out.write(data)
            log(f"decode: {n}/{len(plan)} chunks")


def observe_chunk(args):
    """Observations for one chunk of the crop cache (runs in a worker process)."""
    path, offset, samples = args
    out = []
    with open(path, "rb") as handle:
        handle.seek(offset)
        for _ in range(samples):
            out.append(observe(handle.read(FRAME_BYTES)))
    return out


def observe_all(path, plan, workers):
    jobs, offset = [], 0
    for _, samples in plan:
        jobs.append((path, offset, samples))
        offset += samples * FRAME_BYTES
    log(f"detect: {len(jobs)} chunks on {workers} processes")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(observe_chunk, jobs))


# ----------------------------------------------------------- caches + report

def row_to_json(row):
    return [row.k, list(row.colours), row.kind, row.fp.hex() if row.fp else "",
            row.clipped, row.x0]


def row_from_json(item):
    k, colours, kind, fp, clipped, x0 = item
    return Row(k, tuple(colours), kind, bytes.fromhex(fp) if fp else None, clipped, x0)


def save_observations(path, plan, chunks):
    data = {"fps": FPS, "plan": plan,
            "chunks": [[[row_to_json(r) for r in rows] for rows in chunk] for chunk in chunks]}
    with open(path, "w") as handle:
        json.dump(data, handle, separators=(",", ":"))


def load_observations(path, plan):
    """Cached observations when they were made for this exact plan, else None."""
    if not os.path.isfile(path):
        return None
    with open(path) as handle:
        data = json.load(handle)
    if data.get("fps") != FPS or [list(p) for p in plan] != data.get("plan"):
        return None
    return [[[row_from_json(r) for r in rows] for rows in chunk] for chunk in data["chunks"]]


def window_samples(windows, plan, chunks):
    """[(t, rows)] sample lists, one per window (chunks are consecutive inside a window)."""
    per_window, c = [], 0
    for _, end in windows:
        samples = []
        while c < len(plan) and plan[c][0] < end - 1e-6:
            t0 = plan[c][0]
            samples += [(t0 + i / FPS, rows) for i, rows in enumerate(chunks[c])]
            c += 1
        per_window.append(samples)
    return per_window


def match_for(windows, t):
    for w in windows:
        if w["start"] <= t < w["end"]:
            return w["match"]
    return None


def near_goal(goals, t, side):
    lo, hi = GOAL_WINDOW
    return any(g["side"] == side and lo <= t - g["t"] <= hi for g in goals)


def pick_examples(items, n):
    """n items spread evenly over the list (all of them when it is short)."""
    if len(items) <= n:
        return list(items)
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def icon_box(event):
    x = CROP[2] + event.x_rep * SCALE
    y = CROP[3] + icon_top(event.k_rep) * SCALE
    return [x, y, ICON_SIZE * SCALE, ICON_H * SCALE]


def event_records(events, labels, windows, goals):
    out = []
    for n, (e, label) in enumerate(zip(events, labels), 1):
        out.append({"n": n, "t": round(e.t, 2), "hh:mm:ss": format_hms(e.t),
                    "duration": round(e.duration, 2), "side": e.colours[0],
                    "chips": list(e.colours), "kind": e.kind, "cluster": label,
                    "match": match_for(windows, e.t), "icon_t": round(e.t_rep, 2),
                    "icon_box": icon_box(e), "near_goal": near_goal(goals, e.t, e.colours[0]),
                    "strip": f"feed-events/ev-{n:03d}.jpg"})
    return out


def cluster_records(records):
    out = []
    for cid in sorted({r["cluster"] for r in records}):
        mine = [r for r in records if r["cluster"] == cid]
        out.append({"id": cid, "count": len(mine), "kinds": sorted({r["kind"] for r in mine}),
                    "blue": sum(r["side"] == "blue" for r in mine),
                    "orange": sum(r["side"] == "orange" for r in mine),
                    "near_goal": sum(r["near_goal"] for r in mine),
                    "median_duration": sorted(r["duration"] for r in mine)[len(mine) // 2],
                    "examples": [r["n"] for r in pick_examples(mine, EXAMPLES)],
                    "montage": f"feed-icons/cluster-{cid:02d}.png"})
    return out


# ------------------------------------------------------------ human sheets

def run_jobs(jobs, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(job) for job in jobs]:
            future.result()


def icon_tile(video, record, dest):
    x, y, w, h = record["icon_box"]
    vf = (f"crop={w}:{h}:{x}:{y},scale={w * 4}:{h * 4}:flags=neighbor,"
          + label_filter(f"{record['hh:mm:ss']} #{record['n']}", 16))
    run_tool(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{record['icon_t']:.3f}", "-i", video,
              "-frames:v", "1", "-vf", vf, "-update", "1", "-y", dest])


def montage(tiles, dest, size=(ICON_SIZE * SCALE * 4, ICON_H * SCALE * 4), columns=6):
    """xstack the tile images into a grid (a lone tile is just copied)."""
    if len(tiles) == 1:
        shutil.copyfile(tiles[0], dest)
        return
    layout = "|".join(f"{i % columns * size[0]}_{i // columns * size[1]}"
                      for i in range(len(tiles)))
    graph = "".join(f"[{i}:v]" for i in range(len(tiles))) + \
        f"xstack=inputs={len(tiles)}:layout={layout}:fill=black"
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    for tile in tiles:
        cmd += ["-i", tile]
    run_tool(cmd + ["-filter_complex", graph, "-frames:v", "1", "-update", "1", "-y", dest])


def build_icon_montages(video, out_dir, records, clusters, workers):
    folder = os.path.join(out_dir, "feed-icons")
    work = os.path.join(folder, "_tiles")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(work)
    by_n = {r["n"]: r for r in records}
    jobs, plan = [], []
    for cl in clusters:
        tiles = [os.path.join(work, f"c{cl['id']:02d}-{n:03d}.png") for n in cl["examples"]]
        jobs += [lambda n=n, t=t: icon_tile(video, by_n[n], t) for n, t in zip(cl["examples"], tiles)]
        plan.append((tiles, os.path.join(out_dir, cl["montage"])))
    log(f"extracting {len(jobs)} icon tiles")
    run_jobs(jobs, workers)
    run_jobs([lambda t=t, d=d: montage(t, d) for t, d in plan], workers)
    shutil.rmtree(work)


def strip_command(video, record, tile_width=480):
    first = max(0.0, record["t"] + STRIP_OFFSETS[0])
    stamp = f"%{{pts:hms:{first:.3f}}}"
    vf = (f"fps=1,scale={tile_width}:-2," + label_filter(stamp, 20) + ",tile=4x2")
    return ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{first:.3f}", "-t",
            f"{len(STRIP_OFFSETS) + 0.5}", "-i", video, "-vf", vf, "-frames:v", "1",
            "-q:v", "3", "-update", "1", "-y"]


def build_event_strips(video, out_dir, records, workers):
    folder = os.path.join(out_dir, "feed-events")
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    log(f"building {len(records)} event strips")
    run_jobs([lambda r=r: run_tool(strip_command(video, r) + [os.path.join(out_dir, r["strip"])])
              for r in records], workers)


# ---------------------------------------------------------------------- main

def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", help="path to the stream recording")
    parser.add_argument("--out", help="output dir (default: <video dir>/analysis)")
    parser.add_argument("--goals", help="goals.json with match_windows (default: <out>/goals.json)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help="parallel ffmpeg decoders / detection processes")
    parser.add_argument("--reuse", action="store_true",
                        help="reuse feed-crops.bin and feed-observations.json from an earlier run")
    parser.add_argument("--redetect", action="store_true",
                        help="with --reuse: keep the crops but run the detection again")
    parser.add_argument("--cluster-tol", type=float, default=CLUSTER_TOL,
                        help="cosine distance below which two icon fingerprints are one cluster")
    parser.add_argument("--no-sheets", action="store_true", help="skip montages and strips")
    parser.add_argument("--no-strips", action="store_true", help="skip only the per-event strips")
    return parser.parse_args(argv)


def load_chunks(video, plan, out_dir, args):
    """Observations per chunk: from cache, from cached crops, or from a fresh ffmpeg pass."""
    crops = os.path.join(out_dir, "feed-crops.bin")
    obs = os.path.join(out_dir, "feed-observations.json")
    expected = sum(n for _, n in plan) * FRAME_BYTES
    if args.reuse and not args.redetect:
        cached = load_observations(obs, plan)
        if cached is not None:
            log(f"reusing {obs}")
            return cached
    if not (args.reuse and os.path.isfile(crops) and os.path.getsize(crops) == expected):
        decode_all(video, plan, crops, args.workers)
    chunks = observe_all(crops, plan, args.workers)
    save_observations(obs, plan, chunks)
    return chunks


def run(args):
    video = os.path.abspath(args.video)
    if not os.path.isfile(video):
        raise ToolError(f"input video not found: {video}")
    require_tools()
    out_dir = args.out or os.path.join(os.path.dirname(video), "analysis")
    os.makedirs(out_dir, exist_ok=True)
    goals_path = args.goals or os.path.join(out_dir, "goals.json")
    if not os.path.isfile(goals_path):
        raise ToolError(f"{goals_path} not found: run find_goals.py first")
    with open(goals_path) as handle:
        goal_data = json.load(handle)
    windows = goal_data["match_windows"]
    plan = chunk_plan([(w["start"], w["end"]) for w in windows])
    chunks = load_chunks(video, plan, out_dir, args)
    events = []
    for samples in window_samples([(w["start"], w["end"]) for w in windows], plan, chunks):
        events += track_events(samples)
    events.sort(key=lambda e: e.t)
    labels = [0] * len(events)
    for kind in ("single", "demo"):
        idx = [i for i, e in enumerate(events) if e.kind == kind]
        base = max(labels, default=0)
        for i, lab in zip(idx, cluster_fps([events[i].fp for i in idx], args.cluster_tol)):
            labels[i] = base + lab
    records = event_records(events, labels, windows, goal_data["goals"])
    clusters = cluster_records(records)
    log(f"found {len(records)} popups in {len(clusters)} icon clusters")
    report = {"video": os.path.basename(video), "sample_fps": FPS, "match_windows": len(windows),
              "clusters": clusters, "events": records}
    with open(os.path.join(out_dir, "feed-events.json"), "w") as handle:
        json.dump(report, handle, indent=2)
    if not args.no_sheets:
        build_icon_montages(video, out_dir, records, clusters, args.workers)
        if not args.no_strips:
            build_event_strips(video, out_dir, records, args.workers)
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
