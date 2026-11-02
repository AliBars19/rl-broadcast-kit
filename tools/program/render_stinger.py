#!/usr/bin/env python3
"""Render the OBS stinger (program/stinger/stinger.html) to a transparent VP9 WebM.

Standard library plus ffmpeg, ffprobe and google-chrome on PATH. Usage:
    python3 -I tools/program/render_stinger.py [--out-dir DIR] [--keep-temp]
Writes stinger.webm (yuva420p, 60 fps) and stinger-preview.mp4 (over a checkerboard),
then decodes the WebM again and checks that the alpha channel survived.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir

import chrome_frames as cf  # noqa: E402
from chrome_frames import ToolError, log, run_tool  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STINGER_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "program", "stinger")
PAGE = os.path.join(STINGER_DIR, "stinger.html")

FPS = cf.FPS
DURATION = 1.3                   # seconds, matches the timeline in stinger.html
FRAMES = cf.frame_count(DURATION, FPS)
COVER_START_MS = 550             # every pixel is covered from here ...
COVER_END_MS = 780               # ... until the panels start to clear
TRANSITION_MS = 600              # OBS "Transition Point": inside the covered window
COVER_FRAME = int(round(TRANSITION_MS / 1000 * FPS))
ALPHA_CLEAR_MAX = 4              # corner alpha at or below this counts as transparent
ALPHA_OPAQUE_MIN = 250
FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
VP9_DECODER = ["-c:v", "libvpx-vp9"]   # the native vp9 decoder drops the alpha plane
CHECKER = ("nullsrc=s=1920x1080:r=60:d=1.3,"
           "geq=lum='if(mod(floor(X/60)+floor(Y/60)\\,2)\\,92\\,132)':cb=128:cr=128")


def encode_command(frames_pattern, out_path):
    return [*FFMPEG, "-framerate", str(FPS), "-start_number", "0", "-i", frames_pattern,
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
            "-b:v", "0", "-crf", "22", "-row-mt", "1", "-deadline", "good", "-cpu-used", "1",
            "-r", str(FPS), out_path]


def preview_command(webm_path, out_path):
    """Human preview: the WebM decoded with alpha, composited over a grey checkerboard."""
    return [*FFMPEG, "-f", "lavfi", "-i", CHECKER, *VP9_DECODER, "-i", webm_path,
            "-filter_complex", "[0:v][1:v]overlay=format=auto:shortest=1,format=yuv420p[v]",
            "-map", "[v]", "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19",
            "-b:v", "0", "-profile:v", "high", "-r", str(FPS), "-movflags", "+faststart", out_path]


def pixel_command(webm_path, frame, x, y):
    """Raw RGBA of one pixel of one decoded frame, written to stdout."""
    graph = f"format=rgba,select=eq(n\\,{frame}),crop=1:1:{x}:{y}"
    return [*FFMPEG, *VP9_DECODER, "-i", webm_path, "-vf", graph, "-frames:v", "1",
            "-pix_fmt", "rgba", "-f", "rawvideo", "-"]


def parse_rgba(data):
    if len(data) != 4:
        raise ToolError(f"expected 4 bytes of RGBA, got {len(data)}")
    return tuple(data)


def read_pixel(webm_path, frame, x, y):
    proc = subprocess.run(pixel_command(webm_path, frame, x, y), stdin=subprocess.DEVNULL,
                          capture_output=True)
    if proc.returncode != 0:
        raise ToolError("ffmpeg could not decode the stinger: " + proc.stderr.decode()[-300:])
    return parse_rgba(proc.stdout)


def check_alpha(first, last, cover):
    """Corners of the first and last frame transparent, the cover frame opaque."""
    if first[3] > ALPHA_CLEAR_MAX:
        raise ToolError(f"alpha lost: first frame corner is not transparent (alpha {first[3]})")
    if last[3] > ALPHA_CLEAR_MAX:
        raise ToolError(f"alpha lost: last frame corner is not transparent (alpha {last[3]})")
    if cover[3] < ALPHA_OPAQUE_MIN:
        raise ToolError(f"alpha lost: cover frame is see-through (alpha {cover[3]})")


def verify_alpha(webm_path):
    first = read_pixel(webm_path, 0, 0, 0)
    last = read_pixel(webm_path, FRAMES - 1, 0, 0)
    cover = read_pixel(webm_path, COVER_FRAME, 100, 100)
    check_alpha(first, last, cover)
    return first, last, cover


def render(out_dir, keep_temp=False):
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="render_stinger_")
    try:
        log(f"rendering {FRAMES} frames at {FPS} fps with {cf.CHROME_WORKERS} Chrome workers")
        cf.render_sequence(PAGE, FRAMES, [(0, DURATION)], tmp)
        webm = os.path.join(out_dir, "stinger.webm")
        run_tool(encode_command(os.path.join(tmp, "%05d.png"), webm))
        run_tool(preview_command(webm, os.path.join(out_dir, "stinger-preview.mp4")))
        return webm, verify_alpha(webm)
    finally:
        if keep_temp:
            log(f"kept temp dir {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Render the transparent OBS stinger.")
    parser.add_argument("--out-dir", default=STINGER_DIR, help="where stinger.webm is written")
    parser.add_argument("--keep-temp", action="store_true", help="keep the PNG frames")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        for tool in ("ffmpeg", "ffprobe", "google-chrome"):
            if shutil.which(tool) is None:
                raise ToolError(f"{tool} not found on PATH")
        webm, (first, last, cover) = render(args.out_dir, args.keep_temp)
    except ToolError as exc:
        log(f"error: {exc}")
        return 1
    print(f"{webm}  {os.path.getsize(webm) / 1e3:.0f} kB  {DURATION:.2f} s  {FRAMES} frames")
    print(f"alpha ok: first corner {first}, last corner {last}, cover frame {cover}")
    print(f"OBS transition point: {TRANSITION_MS} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
