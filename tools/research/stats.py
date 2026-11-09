#!/usr/bin/env python3
"""Per-account and per-segment TikTok posting stats from raw/*.json (stdlib only).

Usage: python3 -I stats.py [--dir FOLDER] [--today YYYY-MM-DD]
Reads accounts-*.json and raw/ from FOLDER (default: current folder) and writes
stats-accounts.csv, stats-videos.csv and stats-summary.json there.
"""
import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import sys
from collections import Counter
from typing import Any, Optional

TITLE_MAX = 120
BANDS = [("under 3", 0, 3), ("3-7", 3, 7), ("7-14", 7, 14),
         ("14-21", 14, 21), ("over 21", 21, math.inf)]
BUCKETS = [("under 15 s", 0, 15), ("15-30 s", 15, 30), ("30-45 s", 30, 45),
           ("45-60 s", 45, 60), ("60-90 s", 60, 90), ("over 90 s", 90, math.inf)]
ACCOUNT_FIELDS = [
    "handle", "segment", "status", "videos_fetched", "posts_30d", "posts_90d",
    "posts_per_week_30d", "posts_per_week_90d", "active_day_share_30d",
    "max_posts_one_day_90d", "median_duration", "p25_duration", "p75_duration",
    "median_views", "median_views_90d", "median_likes", "days_since_latest", "missing",
]
VIDEO_FIELDS = ["handle", "segment", "id", "date", "duration", "views", "likes",
                "comments", "reposts", "title"]

Video = dict[str, Any]
Row = dict[str, Any]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def num(value: Any) -> Optional[float]:
    """Return value as float when it is a real number (bools excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def r3(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 3)


def percentile(values: list[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (pct in 0..100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(rank), math.ceil(rank)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def median(values: list[float]) -> Optional[float]:
    return percentile(values, 50)


def distribution(values: list[float]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(values)}
    for name, pct in [("min", 0), ("p25", 25), ("median", 50),
                      ("p75", 75), ("p90", 90), ("max", 100)]:
        out[name] = r3(percentile(values, pct))
    return out


def share_at_least(values: list[float], threshold: float) -> dict[str, Any]:
    count = sum(1 for v in values if v >= threshold)
    return {"count": count, "share": r3(count / len(values)) if values else None}


def load_accounts(folder: str) -> list[dict[str, str]]:
    """Validated, de-duplicated (lower-cased handle, first seen wins) accounts in file order."""
    seen: dict[str, dict[str, str]] = {}
    for path in sorted(glob.glob(os.path.join(folder, "accounts-*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                items = json.load(fh)
            if not isinstance(items, list):
                raise ValueError("top level is not a list")
        except (OSError, ValueError) as exc:
            log(f"WARNING: skipping {path}: {exc}")
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("handle"), str):
                log(f"WARNING: skipping malformed entry in {path}: {item!r}")
                continue
            if item.get("validated") is False:
                continue
            handle = item["handle"].strip().lstrip("@")
            if handle and handle.lower() not in seen:
                seen[handle.lower()] = {"handle": handle,
                                        "segment": str(item.get("segment") or "unassigned")}
    return list(seen.values())


def load_raw(folder: str, handle: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """Return (entries, None) on success or (None, reason) on failure."""
    path = os.path.join(folder, "raw", handle + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, "no raw file"
    except (OSError, ValueError) as exc:
        return None, f"unreadable raw file: {exc}"
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None, "raw file has no entries list"
    return [e for e in entries if isinstance(e, dict)], None


def to_video(entry: dict, today: dt.date) -> Video:
    ts = num(entry.get("timestamp"))
    date: Optional[dt.date] = None
    if ts is not None:
        try:
            date = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError) as exc:
            log(f"WARNING: bad timestamp {ts!r} on {entry.get('id')}: {exc}")
    title = str(entry.get("title") or "").replace("\r", " ").replace("\n", " ")
    return {
        "id": str(entry.get("id") or ""),
        "title": title[:TITLE_MAX],
        "date": date.isoformat() if date else "",
        "age": (today - date).days if date else None,
        "duration": num(entry.get("duration")),
        "views": num(entry.get("view_count")),
        "likes": num(entry.get("like_count")),
        "comments": num(entry.get("comment_count")),
        "reposts": num(entry.get("repost_count")),
    }


def in_window(video: Video, days: int) -> bool:
    return video["age"] is not None and 0 <= video["age"] < days


def present(videos: list[Video], key: str) -> list[float]:
    return [v[key] for v in videos if v[key] is not None]


def account_row(handle: str, segment: str, entries: list[dict],
                videos: list[Video]) -> Row:
    w30 = [v for v in videos if in_window(v, 30)]
    w90 = [v for v in videos if in_window(v, 90)]
    ages = [v["age"] for v in videos if v["age"] is not None]
    per_day = Counter(v["age"] for v in w90)
    durations = present(videos, "duration")
    missing = sum(1 for e in entries
                  if num(e.get("timestamp")) is None or num(e.get("duration")) is None)
    return {
        "handle": handle, "segment": segment, "status": "ok",
        "videos_fetched": len(videos),
        "posts_30d": len(w30), "posts_90d": len(w90),
        "posts_per_week_30d": r3(len(w30) / (30 / 7)),
        "posts_per_week_90d": r3(len(w90) / (90 / 7)),
        "active_day_share_30d": r3(len({v["age"] for v in w30}) / 30),
        "max_posts_one_day_90d": max(per_day.values(), default=0),
        "median_duration": r3(median(durations)),
        "p25_duration": r3(percentile(durations, 25)),
        "p75_duration": r3(percentile(durations, 75)),
        "median_views": r3(median(present(videos, "views"))),
        "median_views_90d": r3(median(present(w90, "views"))),
        "median_likes": r3(median(present(videos, "likes"))),
        "days_since_latest": min(ages) if ages else None,
        "missing": missing,
    }


def bucket_table(videos: list[Video]) -> list[dict[str, Any]]:
    """videos must already be filtered to the period and to rows with duration and views."""
    out = []
    for label, lo, hi in BUCKETS:
        sel = [v["views"] for v in videos if lo <= v["duration"] < hi]
        out.append({"bucket": label, "videos": len(sel), "median_views": r3(median(sel))})
    return out


def band_table(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for label, lo, hi in BANDS:
        sel = [r["median_views_90d"] for r in rows
               if lo <= r["posts_per_week_30d"] < hi and r["median_views_90d"] is not None]
        in_band = [r for r in rows if lo <= r["posts_per_week_30d"] < hi]
        out.append({"band": label, "accounts": len(in_band),
                    "median_of_median_views_90d": r3(median(sel))})
    return out


def scope_summary(rows: list[Row], videos: list[Video]) -> dict[str, Any]:
    active = [r for r in rows if r["posts_30d"] > 0]
    ppw = [r["posts_per_week_30d"] for r in active]
    recent = [v for v in videos if in_window(v, 365)
              and v["duration"] is not None and v["views"] is not None]
    return {
        "accounts": len(rows),
        "active_last_30d": len(active),
        "posts_per_week_30d_active": distribution(ppw),
        "active_at_least_per_week": {str(t): share_at_least(ppw, t) for t in (7, 14, 21)},
        "median_duration_per_account": distribution(
            [r["median_duration"] for r in rows if r["median_duration"] is not None]),
        "duration_buckets_last_365d": bucket_table(recent),
        "posts_per_week_bands_30d": band_table(rows),
    }


def build_summary(rows: list[Row], videos: list[Video], today: dt.date,
                  without_data: list[str]) -> dict[str, Any]:
    segments = sorted({r["segment"] for r in rows})
    return {
        "today": today.isoformat(),
        "accounts_without_data": without_data,
        "overall": scope_summary(rows, videos),
        "segments": {
            seg: scope_summary([r for r in rows if r["segment"] == seg],
                               [v for v in videos if v["segment"] == seg])
            for seg in segments
        },
    }


def fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def format_scope(name: str, scope: dict[str, Any]) -> list[str]:
    d = scope["posts_per_week_30d_active"]
    shares = scope["active_at_least_per_week"]
    dur = scope["median_duration_per_account"]
    lines = [
        f"{name}: {scope['accounts']} accounts, {scope['active_last_30d']} posted in last 30 days",
        f"  posts/week (30d, active): median {fmt(d['median'])}, p75 {fmt(d['p75'])}, "
        f"p90 {fmt(d['p90'])}, max {fmt(d['max'])}",
        "  active share >=7/wk {}, >=14/wk {}, >=21/wk {}".format(
            *(fmt(shares[t]["share"]) for t in ("7", "14", "21"))),
        f"  median duration per account: median {fmt(dur['median'])} s",
    ]
    for b in scope["duration_buckets_last_365d"]:
        lines.append(f"  {b['bucket']:<11} {b['videos']:>4} videos, "
                     f"median views {fmt(b['median_views'])}")
    return lines


def print_summary(summary: dict[str, Any]) -> None:
    print(f"Stats as of {summary['today']} (missing data: "
          f"{', '.join(summary['accounts_without_data']) or 'none'})")
    for line in format_scope("OVERALL", summary["overall"]):
        print(line)
    for seg, scope in summary["segments"].items():
        for line in format_scope(f"SEGMENT {seg}", scope):
            print(line)


def write_csv(path: str, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def collect_rows(folder: str, accounts: list[dict[str, str]],
                 today: dt.date) -> tuple[list[Row], list[Video], list[str]]:
    rows: list[Row] = []
    videos: list[Video] = []
    without: list[str] = []
    for acc in accounts:
        entries, err = load_raw(folder, acc["handle"])
        if entries is None:
            log(f"WARNING: {acc['handle']}: {err}")
            without.append(acc["handle"])
            continue
        vids = [to_video(e, today) for e in entries]
        rows.append(account_row(acc["handle"], acc["segment"], entries, vids))
        videos.extend({"handle": acc["handle"], "segment": acc["segment"], **v} for v in vids)
    return rows, videos, without


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", help="folder with accounts-*.json and raw/ (default: current folder)")
    parser.add_argument("--today", help="reference date YYYY-MM-DD (default: today)")
    args = parser.parse_args(argv)
    folder = os.path.abspath(args.dir or os.getcwd())
    try:
        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    except ValueError as exc:
        log(f"ERROR: bad --today: {exc}")
        return 2
    try:
        accounts = load_accounts(folder)
        rows, videos, without = collect_rows(folder, accounts, today)
        write_csv(os.path.join(folder, "stats-accounts.csv"), ACCOUNT_FIELDS, rows)
        write_csv(os.path.join(folder, "stats-videos.csv"), VIDEO_FIELDS, videos)
        summary = build_summary(rows, videos, today, without)
        with open(os.path.join(folder, "stats-summary.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    except OSError as exc:
        log(f"ERROR: could not write output: {exc}")
        return 1
    print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
