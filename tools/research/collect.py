#!/usr/bin/env python3
"""Fetch the first 60 TikTok entries per validated account into raw/<handle>.json.

Usage: python3 -I collect.py [--dir FOLDER] [--yt-dlp COMMAND] [--limit N] [--force]
Resumable: accounts with a valid raw file are skipped unless --force is given.
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

RAW_SUBDIR = "raw"
FAILURES_NAME = "_failures.json"
PLAYLIST_END = 60
TIMEOUT_S = 180
RETRY_WAIT_S = 30
GAP_S = 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


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
                seen[handle.lower()] = {"handle": handle}
    return list(seen.values())


def resolve_yt_dlp(command: str) -> str:
    """Return the full path of a yt-dlp command name (on PATH) or path, else raise."""
    found = shutil.which(command)
    if found is None:
        raise FileNotFoundError(f"yt-dlp not found: {command}")
    return found


def raw_path(raw_dir: str, handle: str) -> str:
    return os.path.join(raw_dir, handle + ".json")


def raw_is_valid(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        log(f"  existing raw file unusable, refetching: {exc}")
        return False
    return isinstance(data, dict) and isinstance(data.get("entries"), list)


def last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def fetch_once(handle: str, yt_dlp: str) -> str:
    """Run yt-dlp once and return its JSON text; raise RuntimeError with the reason on failure."""
    url = f"https://www.tiktok.com/@{handle}"
    try:
        cmd = [yt_dlp, "--flat-playlist", "--playlist-end", str(PLAYLIST_END), "-J", url]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {TIMEOUT_S} s") from None
    except OSError as exc:
        raise RuntimeError(f"could not run yt-dlp: {exc}") from None
    if proc.returncode != 0:
        raise RuntimeError(last_line(proc.stderr) or f"exit code {proc.returncode}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"invalid JSON from yt-dlp: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise RuntimeError("yt-dlp output has no entries list")
    return proc.stdout


def fetch_with_retry(handle: str, yt_dlp: str) -> str:
    """One attempt, then one retry after RETRY_WAIT_S. A second failure propagates."""
    try:
        return fetch_once(handle, yt_dlp)
    except RuntimeError as first:
        log(f"  attempt 1 failed: {first}; retrying in {RETRY_WAIT_S} s")
    time.sleep(RETRY_WAIT_S)
    return fetch_once(handle, yt_dlp)


def write_atomic(path: str, text: str) -> None:
    """Write via a temp file so a half-written raw file never looks valid."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def load_failures(failures_path: str) -> list[dict[str, str]]:
    try:
        with open(failures_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log(f"WARNING: ignoring unreadable {failures_path}: {exc}")
        return []
    return data if isinstance(data, list) else []


def record_failure(failures_path: str, failures: list[dict[str, str]], handle: str,
                   error: Optional[str]) -> list[dict[str, str]]:
    """Return a new failure list without this handle, plus an entry if error is given."""
    kept = [f for f in failures if str(f.get("handle", "")).lower() != handle.lower()]
    if error is not None:
        kept.append({"handle": handle, "error": error})
    write_atomic(failures_path, json.dumps(kept, indent=2))
    return kept


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", help="folder with accounts-*.json and raw/ (default: current folder)")
    parser.add_argument("--yt-dlp", default="yt-dlp", dest="yt_dlp",
                        help="yt-dlp command or path (default: yt-dlp on PATH)")
    parser.add_argument("--limit", type=int, help="only the first N handles")
    parser.add_argument("--force", action="store_true", help="refetch existing raw files")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        yt_dlp = resolve_yt_dlp(args.yt_dlp)
    except FileNotFoundError as exc:
        log(f"ERROR: {exc}")
        return 2
    folder = os.path.abspath(args.dir or os.getcwd())
    raw_dir = os.path.join(folder, RAW_SUBDIR)
    failures_path = os.path.join(raw_dir, FAILURES_NAME)
    os.makedirs(raw_dir, exist_ok=True)
    accounts = load_accounts(folder)
    if args.limit is not None:
        accounts = accounts[:args.limit]
    total = len(accounts)
    failures = load_failures(failures_path)
    fetched_any = False
    ok_count = 0
    for index, acc in enumerate(accounts, 1):
        handle = acc["handle"]
        prefix = f"[{index}/{total}] {handle}:"
        if not args.force and raw_is_valid(raw_path(raw_dir, handle)):
            print(f"{prefix} skipped (raw file present)", flush=True)
            continue
        if fetched_any:
            time.sleep(GAP_S)
        fetched_any = True
        try:
            text = fetch_with_retry(handle, yt_dlp)
        except RuntimeError as exc:
            print(f"{prefix} FAILED: {exc}", flush=True)
            failures = record_failure(failures_path, failures, handle, str(exc))
            continue
        try:
            write_atomic(raw_path(raw_dir, handle), text)
        except OSError as exc:
            print(f"{prefix} FAILED to write raw file: {exc}", flush=True)
            failures = record_failure(failures_path, failures, handle, f"write failed: {exc}")
            continue
        failures = record_failure(failures_path, failures, handle, None)
        ok_count += 1
        print(f"{prefix} ok, {len(json.loads(text)['entries'])} entries", flush=True)
    print(f"Done: {ok_count} fetched, {len(failures)} failure(s) recorded in {failures_path}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
