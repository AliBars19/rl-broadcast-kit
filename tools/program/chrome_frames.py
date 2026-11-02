"""Render a time-driven HTML animation to a numbered PNG sequence with headless Chrome.

Every page exposes its animation through the query string: `page.html?t=0.6` shows the
state at 0.6 s (see program/intro/motion.js). One Chrome per frame, a bounded number
in parallel, each with a fresh profile and killed as a process group on timeout.
Standard library only; pairs with ffmpeg in render_stinger.py and build_intro.py.
"""
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clips"))

from find_candidates import ToolError, log, probe_duration, run_tool  # noqa: E402,F401

FPS = 60
WIDTH, HEIGHT = 1920, 1080
CHROME_TIMEOUT = 40
CHROME_WORKERS = 6          # bounded: keep memory low: each Chrome is about 300 MB
EPS = 1e-6


def frame_count(duration, fps=FPS):
    """Whole frames in `duration` seconds."""
    return int(round(duration * fps))


def frame_time(index, fps=FPS):
    return index / fps


def plan_frames(count, fps, active):
    """For each frame, the index of the frame that must be rendered to show it.

    `active` lists the (start, end) second ranges in which something moves. Inside a
    range every frame is rendered, plus the first frame at or after its end; frames in
    between are static and reuse the last rendered frame. Frame 0 is always rendered.
    """
    rendered = {0} if count else set()
    for lo, hi in active:
        if hi < lo:
            raise ToolError(f"active range {lo}..{hi} ends before it starts")
        first = max(0, int(math.ceil(lo * fps - EPS)))
        last = int(math.ceil(hi * fps - EPS))
        rendered.update(range(first, min(last, count - 1) + 1))
    plan, current = [], 0
    for i in range(count):
        if i in rendered:
            current = i
        plan.append(current)
    return plan


def page_url(page_path, t):
    return Path(page_path).resolve().as_uri() + f"?t={t:.4f}"


def chrome_command(url, png_path, profile_dir):
    return ["google-chrome", "--headless=new", f"--screenshot={png_path}",
            f"--window-size={WIDTH},{HEIGHT}", "--default-background-color=00000000",
            f"--user-data-dir={profile_dir}", "--hide-scrollbars",
            "--virtual-time-budget=2000", "--disable-gpu", "--no-first-run",
            "--force-device-scale-factor=1", "--disable-extensions",
            "--disable-background-networking", "--allow-file-access-from-files", url]


def png_size(path):
    with open(path, "rb") as handle:
        head = handle.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ToolError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def screenshot(url, png_path, profile_dir):
    """Run headless Chrome on one URL; kill the whole process group on timeout."""
    proc = subprocess.Popen(chrome_command(url, png_path, profile_dir), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        proc.wait(timeout=CHROME_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise ToolError(f"chrome timed out after {CHROME_TIMEOUT} s on {url}")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # stray helper processes
        except ProcessLookupError:
            pass
    if not os.path.exists(png_path) or png_size(png_path) != (WIDTH, HEIGHT):
        raise ToolError(f"chrome did not produce a {WIDTH}x{HEIGHT} PNG for {url}")


def link_static(out_dir, plan):
    """Fill the frames that reuse another frame (hard link, copy as a fallback)."""
    for i, source in enumerate(plan):
        if source == i:
            continue
        src, dst = os.path.join(out_dir, f"{source:05d}.png"), os.path.join(out_dir, f"{i:05d}.png")
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)


def render_sequence(page_path, count, active, out_dir, fps=FPS, workers=CHROME_WORKERS):
    """Write out_dir/00000.png .. for `count` frames of the page. Returns frames rendered."""
    os.makedirs(out_dir, exist_ok=True)
    plan = plan_frames(count, fps, active)
    todo = [i for i, source in enumerate(plan) if source == i]
    profiles = tempfile.mkdtemp(prefix="chrome_profiles_")

    def one(i):
        profile = os.path.join(profiles, str(i))
        try:
            screenshot(page_url(page_path, frame_time(i, fps)),
                       os.path.join(out_dir, f"{i:05d}.png"), profile)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, todo))
    finally:
        shutil.rmtree(profiles, ignore_errors=True)
    link_static(out_dir, plan)
    return len(todo)
