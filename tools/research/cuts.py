#!/usr/bin/env python3
"""Cut structure of Rocket League compilation TikToks.

Usage: python3 -I cuts.py [--dir FOLDER] [--yt-dlp COMMAND] [--limit N]
Reads stats-videos.csv from FOLDER (default: current folder) and writes cuts-* files there.
"""
import argparse
import collections
import csv
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

CSV_NAME, SAMPLE_NAME, RESULTS_JSON_NAME, RESULTS_CSV_NAME, DL_SUBDIR = (
    "stats-videos.csv", "cuts-sample.json", "cuts-results.json", "cuts-results.csv", "cuts-dl")
TITLE_RE = re.compile(r"best|moments|highlights|compilation|casters|saves|goals of|"
                      r"top \d|pt\.? ?\d|part ?\d|montage|craziest", re.IGNORECASE)
MIN_DUR, MAX_DUR, PER_HANDLE, SAMPLE_SIZE, MAX_BYTES = 40.0, 180.0, 4, 100, 60 * 1024 * 1024
DL_TIMEOUT, FF_TIMEOUT, DL_SLEEP = 180, 300, 3
SCENE_RE = re.compile(r"pts_time:([0-9.]+)")
BLACK_RE = re.compile(r"black_duration:([0-9.]+)")
MERGE_GAP, CLIP_MIN, BLACK_MIN, BLACK_MAX = 0.4, 3.0, 0.3, 1.5
RESULT_FIELDS = ["handle", "id", "duration", "views", "cuts", "segments", "clip_segments",
                 "median_segment_s", "black_cards"]

log = logging.getLogger("cuts")
Record = Dict[str, Any]


def num(value: Optional[str]) -> float:
    return float(value) if value else 0.0


def select_sample(path: str) -> List[Record]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = {r["id"]: r for r in csv.DictReader(fh)}.values()
    cands = [r for r in rows
             if MIN_DUR <= num(r["duration"]) <= MAX_DUR
             and TITLE_RE.search(r["title"])]
    cands.sort(key=lambda r: -num(r["views"]))
    per_handle: Dict[str, int] = collections.Counter()
    kept: List[Record] = []
    for r in cands:
        if per_handle[r["handle"]] < PER_HANDLE:
            per_handle[r["handle"]] += 1
            kept.append({"handle": r["handle"], "id": r["id"],
                         "url": f"https://www.tiktok.com/@{r['handle']}/video/{r['id']}",
                         "duration": num(r["duration"]),
                         "views": num(r["views"])})
    return kept[:SAMPLE_SIZE]


def download(video: Record, dest: str, yt_dlp: str) -> Optional[str]:
    if os.path.exists(dest):
        return None
    cmd = [yt_dlp, "-f", "worst[height>=360]/worst", "-o", dest, video["url"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace", timeout=DL_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("download failed for %s: %s", video["id"], exc)
        return f"download failed: {exc}"
    finally:
        time.sleep(DL_SLEEP)
    if proc.returncode != 0:
        msg = f"yt-dlp exit {proc.returncode}: {(proc.stderr.strip().splitlines() or [''])[-1]}"
        log.warning("%s for %s", msg, video["id"])
        return msg
    return None if os.path.exists(dest) else "file missing after download"


def run_ffmpeg(path: str, filter_args: List[str]) -> str:
    cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-i", path,
           *filter_args, "-an", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace", timeout=FF_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {proc.returncode}")
    return proc.stderr


def scene_times(path: str) -> List[float]:
    out = run_ffmpeg(path, ["-vf", "select='gt(scene,0.35)',showinfo"])
    return [float(t) for t in SCENE_RE.findall(out)]


def count_black(path: str) -> int:
    out = run_ffmpeg(path, ["-vf", "blackdetect=d=0.3:pix_th=0.10"])
    return sum(1 for d in map(float, BLACK_RE.findall(out)) if BLACK_MIN <= d <= BLACK_MAX)


def segment_stats(times: List[float], duration: float) -> Dict[str, Any]:
    cuts: List[float] = []
    for t in sorted(times):
        if 0 < t < duration and (not cuts or t - cuts[-1] >= MERGE_GAP):
            cuts.append(t)
    bounds = [0.0] + cuts + [duration]
    lengths = [b - a for a, b in zip(bounds, bounds[1:]) if b > a]
    return {"cuts": len(cuts), "segments": len(lengths),
            "clip_segments": sum(1 for x in lengths if x >= CLIP_MIN),
            "median_segment_s": round(statistics.median(lengths), 2) if lengths else 0.0}


def process(video: Record, dl_dir: str, yt_dlp: str) -> Record:
    dest = os.path.join(dl_dir, video["id"] + ".mp4")
    record: Record = dict(video, error=download(video, dest, yt_dlp))
    if record["error"] is None and os.path.getsize(dest) > MAX_BYTES:
        record["error"] = "skipped: file over 60 MB"
    if record["error"] is None:
        try:
            record.update(segment_stats(scene_times(dest), video["duration"]))
            record["black_cards"] = count_black(dest)
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as exc:
            record["error"] = f"analysis failed: {exc}"
            log.warning("%s for %s", record["error"], video["id"])
    return record


def is_done(record: Optional[Record]) -> bool:
    return record is not None and not record.get("error")


def write_csv(path: str, results: Dict[str, Record], sample: List[Record]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for video in sample:
            if is_done(results.get(video["id"])):
                writer.writerow(results[video["id"]])


def quartiles(values: List[float]) -> str:
    if not values:
        return "n/a"
    q = [values[0]] * 3 if len(values) == 1 else statistics.quantiles(values, n=4, method="inclusive")
    return f"p25={q[0]:.2f} median={statistics.median(values):.2f} p75={q[2]:.2f}"


def print_distributions(label: str, ok: List[Record]) -> None:
    for key in ("clip_segments", "median_segment_s", "duration"):
        print(f"  {key} {label}: {quartiles([r[key] for r in ok])}")


def print_summary(sample: List[Record], results: Dict[str, Record]) -> None:
    done = [results[v["id"]] for v in sample if v["id"] in results]
    ok = [r for r in done if is_done(r)]
    failed = [r for r in done if not is_done(r)]
    print(f"videos analysed: {len(ok)} of {len(sample)} sampled")
    print(f"failures: {len(failed)}")
    for reason, count in collections.Counter(r["error"] for r in failed).items():
        print(f"  {count} x {reason}")
    print("distribution (all analysed):")
    print_distributions("all", ok)
    heavy = [r for r in ok if r["black_cards"] >= 3]
    share = len(heavy) / len(ok) * 100 if ok else 0.0
    print(f"share with 3+ black cards: {len(heavy)}/{len(ok)} = {share:.1f}%")
    print("distribution (3+ black-card videos):")
    print_distributions("black>=3", heavy)
    print("median views by clip_segments group:")
    for label, pick in (("under 5 clips", lambda n: n < 5), ("5 to 8 clips", lambda n: 5 <= n <= 8),
                        ("over 8 clips", lambda n: n > 8)):
        views = [r["views"] for r in ok if pick(r["clip_segments"])]
        med = f"{statistics.median(views):.0f}" if views else "n/a"
        print(f"  {label}: n={len(views)} median_views={med}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", help="folder with stats-videos.csv (default: current folder)")
    parser.add_argument("--yt-dlp", default="yt-dlp", dest="yt_dlp",
                        help="yt-dlp command or path (default: yt-dlp on PATH)")
    parser.add_argument("--limit", type=int, default=0,
                        help="process only the first N unfinished videos")
    args = parser.parse_args()
    yt_dlp = shutil.which(args.yt_dlp)
    if yt_dlp is None:
        log.error("yt-dlp not found: %s", args.yt_dlp)
        return 2
    folder = os.path.abspath(args.dir or os.getcwd())
    sample_path = os.path.join(folder, SAMPLE_NAME)
    results_json = os.path.join(folder, RESULTS_JSON_NAME)
    results_csv = os.path.join(folder, RESULTS_CSV_NAME)
    dl_dir = os.path.join(folder, DL_SUBDIR)
    sample = select_sample(os.path.join(folder, CSV_NAME))
    with open(sample_path, "w", encoding="utf-8") as fh:
        json.dump(sample, fh, indent=2)
    log.info("sample selected: %d videos", len(sample))
    os.makedirs(dl_dir, exist_ok=True)
    results: Dict[str, Record] = {}
    if os.path.exists(results_json):
        with open(results_json, encoding="utf-8") as fh:
            results = json.load(fh)
    todo = [v for v in sample if not is_done(results.get(v["id"]))]
    todo = todo[:args.limit] if args.limit else todo
    for i, video in enumerate(todo, 1):
        log.info("[%d/%d] %s", i, len(todo), video["url"])
        results[video["id"]] = process(video, dl_dir, yt_dlp)
        with open(results_json + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        os.replace(results_json + ".tmp", results_json)

    write_csv(results_csv, results, sample)
    print_summary(sample, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
