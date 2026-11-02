#!/usr/bin/env python3
"""Add the animated end title (program/intro/endtitle.html) to a finished intro video.

Standard library plus ffmpeg, ffprobe and google-chrome on PATH. Usage:
    python3 -I tools/program/add_end_title.py INPUT.mp4 OUTPUT.mp4 --hit 14.058
    python3 -I tools/program/add_end_title.py INPUT.mp4 OUTPUT.mp4 --hit auto --search 13.8 14.3
The cut, the length and the audio are untouched (audio is stream-copied). The title slams in on the
audio hit and holds to the last frame. See README.md.
"""
import argparse
import html
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path
from typing import NamedTuple, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tools/brand.py

import brand as brand_config  # noqa: E402
import build_intro as bi  # noqa: E402
import chrome_frames as cf  # noqa: E402
from chrome_frames import ToolError, log, probe_duration, run_tool  # noqa: E402
from render_short import fill_template  # noqa: E402

PAGE = os.path.join(bi.INTRO_DIR, "endtitle.html")
MOTION_END = 1.6              # seconds after the hit when the last element stops moving
MIN_HOLD = 0.5                # the title must hold at least this long after it settles
WORKERS = 4                   # low memory use: at most four Chrome processes
CQ = 19
MAXRATE = "30M"
DURATION_SLACK_FRAMES = 1     # output length may differ from the input by one frame
TV_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-color_range", "tv"]
SET_BT709_TV = "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"

# Hit detection: windowed RMS, the onset is the first window that jumps well above the lead-in.
PCM_RATE = 48000
ONSET_WINDOW_S = 0.002
BASELINE_S = 0.1
ONSET_RISE_DB = 10.0
SILENCE_DB = -120.0
DEFAULT_SEARCH = (13.8, 14.3)


class VideoInfo(NamedTuple):
    frames: int
    fps: float
    duration: float


class OverlayPlan(NamedTuple):
    first_frame: int      # video frame the overlay starts on
    count: int            # overlay frames, through the last video frame
    offset: float         # seconds between the hit and that first frame (page time shift)


# ---------------------------------------------------------------- pure helpers

def check_paths(source: str, output: str) -> None:
    """Refuse to overwrite the input: same path, same file, or a symlink to it."""
    if os.path.realpath(source) == os.path.realpath(output):
        raise ToolError("output path equals the input path; refusing to overwrite the source")
    if os.path.exists(output) and os.path.samefile(source, output):
        raise ToolError("output is the same file as the input; refusing to overwrite the source")
    if not output.lower().endswith(".mp4"):
        raise ToolError(f"output must end in .mp4: {output}")


def plan_overlay(hit: float, fps: float, total_frames: int) -> OverlayPlan:
    """First video frame at or after the hit, how many overlay frames follow, and the time gap."""
    if not math.isfinite(hit) or hit <= 0:
        raise ToolError(f"hit time must be a positive number of seconds, got {hit}")
    first = int(math.ceil(hit * fps - cf.EPS))
    count = total_frames - first
    if count <= 0 or (total_frames - 1) / fps < hit + MOTION_END + MIN_HOLD:
        raise ToolError(f"hit at {hit:.3f} s leaves too little video for the title "
                        f"({MOTION_END + MIN_HOLD:.1f} s needed, video ends at "
                        f"{total_frames / fps:.3f} s)")
    return OverlayPlan(first, count, first / fps - hit)


def active_ranges(plan: OverlayPlan, fps: float) -> list[tuple[float, float]]:
    """Moving stretch in overlay time (the page timeline ends MOTION_END after the hit)."""
    return [(0.0, max(0.0, MOTION_END - plan.offset) + 1 / fps)]


def window_db(samples: Sequence[float], window: int) -> list[float]:
    """RMS of consecutive windows, in dB relative to full scale."""
    out = []
    for i in range(0, len(samples) - window + 1, window):
        rms = math.sqrt(sum(x * x for x in samples[i:i + window]) / window)
        out.append(20 * math.log10(rms) if rms > 0 else SILENCE_DB)
    return out


def find_onset(samples: Sequence[float], rate: int = PCM_RATE, start: float = 0.0) -> float:
    """Time (seconds, `start` + position) of the first window ONSET_RISE_DB above the lead-in.

    The lead-in is the median of the previous BASELINE_S of windows, so a steady bed of music does
    not count as a hit. Raises ToolError when nothing jumps that far.
    """
    window = max(1, int(rate * ONSET_WINDOW_S))
    levels = window_db(samples, window)
    back = max(1, int(BASELINE_S / ONSET_WINDOW_S))
    for i in range(back, len(levels)):
        if levels[i] - statistics.median(levels[i - back:i]) >= ONSET_RISE_DB:
            return start + i * window / rate
    raise ToolError("no audio hit found in the search window (no jump of "
                    f"{ONSET_RISE_DB:g} dB); pass --hit SECONDS explicitly")


def parse_hit(value: str) -> Optional[float]:
    """`auto` gives None (measure it), otherwise seconds as a float."""
    if value.strip().lower() == "auto":
        return None
    try:
        return float(value)
    except ValueError:
        raise ToolError(f"--hit must be a number of seconds or 'auto', got {value!r}")


# ---------------------------------------------------------------- ffmpeg

def overlay_filter(plan: OverlayPlan, fps: float) -> str:
    """Composite the PNG frames on the video, converting RGB to YUV with BT.709 limited range.

    Without the explicit `scale=out_color_matrix=bt709:out_range=tv` ffmpeg converts the RGBA
    overlay with the BT.601 matrix and the title colours shift.
    """
    start = plan.first_frame / fps
    return ";".join([
        f"[0:v]{SET_BT709_TV}[base]",
        f"[1:v]format=rgba,setpts=PTS-STARTPTS+{start:.6f}/TB,"
        "scale=out_color_matrix=bt709:out_range=tv:in_range=pc:flags=accurate_rnd+full_chroma_int+bitexact,"
        "format=yuva420p[title]",
        f"[base][title]overlay=0:0:format=yuv420:eof_action=repeat,"
        f"format=yuv420p,{SET_BT709_TV}[v]",
    ])


def encode_command(source: str, frames_dir: str, plan: OverlayPlan, info: VideoInfo, out_path: str) -> list[str]:
    return [*bi.FFMPEG, "-i", source, "-framerate", f"{info.fps:g}", "-start_number", "0",
            "-i", os.path.join(frames_dir, "%05d.png"),
            "-filter_complex", overlay_filter(plan, info.fps),
            "-map", "[v]", "-map", "0:a",
            "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(CQ), "-b:v", "0",
            "-maxrate", MAXRATE, "-bufsize", "60M", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-r", f"{info.fps:g}", "-fps_mode", "cfr", "-g", f"{round(info.fps)}", "-flags", "+cgop",
            *TV_TAGS, "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", out_path]


def pcm_command(source: str, lo: float, hi: float) -> list[str]:
    return [*bi.FFMPEG, "-ss", f"{lo:.3f}", "-t", f"{hi - lo:.3f}", "-i", source, "-vn", "-ac", "1",
            "-ar", str(PCM_RATE), "-f", "f32le", "-"]


def probe_video(path: str) -> VideoInfo:
    out = run_tool(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                    "stream=nb_read_frames,r_frame_rate", "-of", "default=nw=1", path])
    values = dict(line.split("=", 1) for line in out.split() if "=" in line)
    try:
        num, den = values["r_frame_rate"].split("/")
        return VideoInfo(int(values["nb_read_frames"]), int(num) / int(den), probe_duration(path))
    except (KeyError, ValueError, ZeroDivisionError):
        raise ToolError(f"could not read the frame count and rate of {path}")


def detect_hit(source: str, lo: float, hi: float) -> float:
    """Measure the audio hit inside [lo, hi] seconds of `source`."""
    if not 0 <= lo < hi:
        raise ToolError(f"bad search window {lo}..{hi}")
    proc = subprocess.run(pcm_command(source, lo, hi), stdin=subprocess.DEVNULL, capture_output=True)
    if proc.returncode != 0:
        raise ToolError("ffmpeg could not read the audio: " + proc.stderr.decode(errors="replace")[-200:])
    samples = array("f")
    samples.frombytes(proc.stdout[: len(proc.stdout) // 4 * 4])
    return find_onset(samples, PCM_RATE, lo)


# ---------------------------------------------------------------- run

def wordmark_html(name: str) -> str:
    """Event name with the last word in amber ("YOUR EVENT" + "26")."""
    head, _, last = name.strip().rpartition(" ")
    if not head:
        return html.escape(name.strip())
    return f'{html.escape(head)} <span class="amber">{html.escape(last)}</span>'


def event_line_html(text: str) -> str:
    """"A | B" becomes A, an amber bar, B."""
    parts = [p.strip() for p in text.split("|") if p.strip()]
    return '<i class="sep"></i>'.join(f"<span>{html.escape(p)}</span>" for p in parts)


def title_values(plan: OverlayPlan, brand: dict) -> dict:
    logo = brand.get("sponsor_logo")
    return {"base": Path(bi.INTRO_DIR).as_uri() + "/", "offset": f"{plan.offset:.6f}",
            "wordmark": wordmark_html(brand["event_name"]),
            "event_line": event_line_html(brand["event_sub"]),
            "sponsor_logo": Path(logo).as_uri() if logo else "",
            "presented_attr": "" if logo else "hidden"}


def render_title(plan: OverlayPlan, info: VideoInfo, frames_dir: str, tmp: str,
                 brand: Optional[dict] = None) -> int:
    with open(PAGE, encoding="utf-8") as handle:
        html_text = fill_template(handle.read(), title_values(plan, brand or brand_config.load_brand()))
    page = os.path.join(tmp, "endtitle.html")
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(html_text)
    return cf.render_sequence(page, plan.count, active_ranges(plan, info.fps), frames_dir, info.fps, WORKERS)


def check_result(source_info: VideoInfo, out_path: str) -> VideoInfo:
    result = probe_video(out_path)
    if abs(result.duration - source_info.duration) > DURATION_SLACK_FRAMES / source_info.fps:
        raise ToolError(f"output length {result.duration:.3f} s differs from the input "
                        f"{source_info.duration:.3f} s by more than one frame")
    if result.frames != source_info.frames:
        raise ToolError(f"output has {result.frames} frames, the input has {source_info.frames}")
    return result


def add_end_title(source: str, output: str, hit: Optional[float], search: tuple[float, float],
                  frames_dir: Optional[str] = None, keep_temp: bool = False) -> tuple[float, VideoInfo]:
    """Render and encode. Returns the hit time used and the output's probe."""
    check_paths(source, output)
    for tool in ("ffmpeg", "ffprobe", "google-chrome"):
        if shutil.which(tool) is None:
            raise ToolError(f"{tool} not found on PATH")
    if not os.path.isfile(source):
        raise ToolError(f"input not found: {source}")
    if not bi.has_audio(source):
        raise ToolError(f"{source} has no audio stream")
    if frames_dir and os.path.isdir(frames_dir) and os.listdir(frames_dir):
        raise ToolError(f"--frames-dir {frames_dir} is not empty; stale frames would be mixed in")
    info = probe_video(source)
    if hit is None:
        hit = detect_hit(source, *search)
        log(f"measured the audio hit at {hit:.3f} s")
    plan = plan_overlay(hit, info.fps, info.frames)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="end_title_")
    frames = frames_dir or os.path.join(tmp, "frames")
    partial = output + ".part.mp4"
    try:
        done = render_title(plan, info, frames, tmp)
        log(f"title: {done} of {plan.count} frames rendered (video frame {plan.first_frame} on)")
        run_tool(encode_command(source, frames, plan, info, partial))
        result = check_result(info, partial)
        os.replace(partial, output)
        return hit, result
    finally:
        if os.path.exists(partial):
            os.remove(partial)
        if keep_temp:
            log(f"kept temp dir {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            if frames_dir:
                shutil.rmtree(frames_dir, ignore_errors=True)


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Composite the animated end title onto a finished intro.")
    parser.add_argument("input", help="finished intro video (never modified)")
    parser.add_argument("output", help="new file to write (must differ from the input, end in .mp4)")
    parser.add_argument("--hit", required=True, type=parse_hit, metavar="SECONDS|auto",
                        help="time of the audio hit the title lands on, or 'auto' to measure it")
    parser.add_argument("--search", nargs=2, type=float, default=DEFAULT_SEARCH, metavar=("LO", "HI"),
                        help="seconds window searched when --hit is auto (default 13.8 14.3)")
    parser.add_argument("--frames-dir", help="where to write the title PNG frames (removed afterwards)")
    parser.add_argument("--keep-temp", action="store_true", help="keep the temp dir and frames")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        hit, result = add_end_title(args.input, args.output, args.hit, tuple(args.search),
                                    args.frames_dir, args.keep_temp)
    except ToolError as exc:
        log(f"error: {exc}")
        return 1
    print(f"{args.output}  hit {hit:.3f} s  {result.duration:.3f} s  {result.frames} frames  "
          f"{os.path.getsize(args.output) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
