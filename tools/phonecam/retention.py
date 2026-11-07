#!/usr/bin/env python3
"""retention.py: move a finished stream's camera recording to the Trash (never a permanent delete).

Usage: python3 retention.py CAM_DIR_OR_DATE [--dry-run]
Accepts a date (2026-11-07), a dated folder, or its cam folder. Only a folder that passes every one
of these checks is ever touched: it is named `cam`, it is not a symlink, its parent is named
YYYY-MM-DD and sits directly in the out root ($PHONECAM_OUT_ROOT, default ~/Videos/phonecam), and it
holds nothing but files phonecam writes (chunks, cam-joined.mp4, sync.json, a broken/ folder).
Uses `gio trash`, so it can be restored from the Trash.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunks  # noqa: E402

DATE_LEN = 10
# Files phonecam itself writes into a cam folder (besides chunk files, see _is_phonecam_file).
OWN_FILES = {"cam-joined.mp4", ".cam-joined.mp4.part", "sync.json", "sync.tmp", ".offload.lock"}
OWN_PARTIAL_RE = re.compile(r"^\.cam-.*\.mp4\.part.*$")


class RetentionError(Exception):
    """The target is not a camera recording folder, or the trash command failed."""


def resolve(target: str, root: Path) -> Path:
    path = Path(target).expanduser()
    if len(target) == DATE_LEN and target[4] == "-" and target[7] == "-":
        path = chunks.cam_dir(root, target)
    elif chunks.DATE_FOLDER_RE.match(path.name):
        path = path / "cam"
    return path


def _is_phonecam_file(path: Path) -> bool:
    return (path.name in OWN_FILES or OWN_PARTIAL_RE.match(path.name) is not None
            or chunks.parse_chunk_name(path.name) is not None)


def _foreign_entry(cam: Path):
    """The first entry in the cam folder that phonecam did not write, or None."""
    for entry in sorted(cam.iterdir()):
        if entry.is_symlink():
            return entry
        if entry.is_dir() and entry.name == chunks.BROKEN_DIR:
            inner = next((e for e in sorted(entry.iterdir()) if e.is_symlink() or e.is_dir()
                          or not _is_phonecam_file(e)), None)
            if inner is not None:
                return inner
        elif not entry.is_file() or not _is_phonecam_file(entry):
            return entry
    return None


def check_target(path: Path, root: Path | None = None) -> None:
    root = root or chunks.out_root()
    if path.is_symlink():
        raise RetentionError("%s is a symlink" % path)
    if path.name != "cam" or not chunks.DATE_FOLDER_RE.match(path.parent.name):
        raise RetentionError("%s is not a <out root>/YYYY-MM-DD/cam folder" % path)
    if path.parent.parent.resolve() != Path(root).expanduser().resolve():
        raise RetentionError("%s is not inside the out root %s" % (path, root))
    if not path.is_dir():
        raise RetentionError("%s does not exist" % path)
    foreign = _foreign_entry(path)
    if foreign is not None:
        raise RetentionError("%s holds %s, which phonecam did not write; refusing to trash it" % (path, foreign.name))


def trash(target: str, root: Path | None = None, dry_run: bool = False, run=subprocess.run, log=print) -> Path:
    root = root or chunks.out_root()
    path = resolve(target, root)
    check_target(path, root)
    files = [p for p in path.iterdir() if p.is_file()]
    size_gb = sum(p.stat().st_size for p in files) / 1e9
    if dry_run:
        log("would move %s (%d files, %.1f GB) to the Trash" % (path, len(files), size_gb))
        return path
    try:
        done = run(["gio", "trash", str(path)], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetentionError("cannot run gio trash: %s" % exc)
    if done.returncode != 0:
        raise RetentionError("gio trash failed: %s" % (done.stderr or "").strip()[:300])
    log("moved %s (%d files, %.1f GB) to the Trash; restore it from there if the clips are not done" % (path, len(files), size_gb))
    return path


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("target", help="date YYYY-MM-DD, stream folder or cam folder")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        trash(args.target, dry_run=args.dry_run)
    except RetentionError as exc:
        print("trash: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
