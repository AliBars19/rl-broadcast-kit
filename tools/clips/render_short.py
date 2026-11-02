#!/usr/bin/env python3
"""Render a vertical (1080x1920) YouTube Short from clip timestamps of a long recording.

Standard library plus ffmpeg/ffprobe/google-chrome on PATH. Usage:
    python3 -I render_short.py SPEC.json [--dry-run] [--keep-temp]
See README.md for the spec format and the look.
"""
import argparse
import html
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tools/brand.py

import brand as brand_config  # noqa: E402
from find_candidates import (ToolError, format_hms, log, probe_duration,  # noqa: E402
                             run_tool)

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPHICS_DIR = os.path.join(HERE, "graphics")

FPS = 30
WIDTH, HEIGHT = 1080, 1920
VIDEO_H = 608                  # 16:9 frame at 1080 wide
VIDEO_Y = (HEIGHT - VIDEO_H) // 2  # classic layout: 656, frame centred (656..1264)
# Reaction layout, every block's y range (the graphics/*.html files use the same numbers):
#   top block (tag, title, subtitle)   0..380   = 380 tall
#   game frame (1080 x 608)          380..988   = 608 tall
#   reaction panel (facecam, 1080x600) 988..1588 = 600 tall
#   bottom block (rank, player, team, caption, handle strip) 1588..1920 = 332 tall
#   380 + 608 + 600 + 332 = 1920
REACT_TOP_H = 380
GAME_Y = REACT_TOP_H
CAM_Y = GAME_Y + VIDEO_H
CAM_H = 600
BOTTOM_Y = CAM_Y + CAM_H
REACT_BOTTOM_H = HEIGHT - BOTTOM_Y
# Facecam in the 1920x1080 source: the camera image is x 34..478, y 784..1032 (444x248).
# CAM_CROP is inset 2-4 px to drop the dark border; 440x244 is 1.80:1, so the 1080x600
# panel is a 2.45x upscale with no visible distortion.
CAM_CROP = (36, 786, 440, 244)    # x, y, w, h
# Patch that hides the camera (plus its amber corner brackets) inside the game frame.
CAM_PATCH = (28, 780, 458, 262)   # x, y, w, h
TITLE_SECONDS = 1.5
RANK_SECONDS = 1.1
END_SECONDS = 3.0
MAX_TOTAL_SECONDS = 58.0       # planned length above this is refused before rendering
MAX_REAL_SECONDS = 59.5        # rendered length at or above this fails the run
MIN_CLIP_SECONDS = 1.0
MAX_CLIP_SECONDS = 30.0
MAX_CLIPS = 5
AUDIO_FADE = 0.15
VIDEO_FADE = 0.2
CHROME_TIMEOUT = 40
CHROME_WORKERS = 3
CHAR_WIDTH = 0.62              # Chakra Petch 700 uppercase, em per character (for fitting)
MONO_WIDTH = 0.6               # IBM Plex Mono, em per character
NAVY = "0x07090d"

TEXT_LIMITS = {"title": 28, "subtitle": 44, "handle": 24, "channel": 24, "end_title": 28, "end_line": 36}
CLIP_TEXT_LIMITS = {"player": 20, "team": 28, "caption": 30, "detail": 40}
TOP_KEYS = {"title", "subtitle", "handle", "channel", "source", "output", "clips", "end_title", "end_line"}
CLIP_KEYS = {"rank", "start", "end", "reaction"} | set(CLIP_TEXT_LIMITS)
DASHES = ("—", "–")

ENCODE = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-b:v", "8M",
          "-maxrate", "10M", "-bufsize", "16M", "-bf", "0", "-g", "30",
          "-profile:v", "main", "-pix_fmt", "yuv420p", "-r", str(FPS)]
PCM = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


class SpecError(ToolError):
    """The spec file is invalid or the planned video breaks a limit."""


# ---------------------------------------------------------------- timestamps and budget

_TIMESTAMP = re.compile(r"^(?:(?:(\d+):)?(\d+):)?(\d+(?:\.\d+)?)$")


def parse_timestamp(value):
    """Seconds from 'HH:MM:SS.f', 'MM:SS', 'SS.f' or a plain non-negative number."""
    if isinstance(value, bool) or value is None:
        raise SpecError(f"bad timestamp: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise SpecError(f"negative timestamp: {value!r}")
        return float(value)
    match = _TIMESTAMP.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise SpecError(f"bad timestamp {value!r}: use HH:MM:SS, MM:SS or seconds")
    hours, minutes, seconds = match.groups()
    secs = float(seconds)
    if (hours or minutes) and secs >= 60:
        raise SpecError(f"bad timestamp {value!r}: seconds must be below 60")
    if hours and int(minutes) >= 60:
        raise SpecError(f"bad timestamp {value!r}: minutes must be below 60")
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + secs


def frame_snap(seconds):
    """Round a length to a whole number of output frames."""
    return round(seconds * FPS) / FPS


def total_duration(spec):
    total = TITLE_SECONDS + END_SECONDS
    for clip in spec["clips"]:
        total += clip["length"] + (RANK_SECONDS if clip["rank"] is not None else 0)
    return total


def check_budget(total):
    if total > MAX_TOTAL_SECONDS:
        raise SpecError(f"planned length {total:.1f} s exceeds the {MAX_TOTAL_SECONDS:.1f} s "
                        "limit (Shorts must stay under 60 s): shorten or drop clips")


def verify_duration(real):
    if real >= MAX_REAL_SECONDS:
        raise ToolError(f"rendered length {real:.2f} s is not under {MAX_REAL_SECONDS} s")


# ---------------------------------------------------------------- spec validation

def _text(value, name, limit, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise SpecError(f"{name} must be a non-empty string" if required
                        else f"{name} must be a string")
    if len(value) > limit:
        raise SpecError(f"{name} is {len(value)} characters, the limit is {limit}")
    if any(d in value for d in DASHES):
        raise SpecError(f"{name} contains an em or en dash: use a hyphen, colon or comma")
    return value.strip()


def _clip(raw, index):
    where = f"clip {index + 1}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be an object")
    unknown = set(raw) - CLIP_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    for key in ("start", "end"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key}")
    start, end = parse_timestamp(raw["start"]), parse_timestamp(raw["end"])
    length = frame_snap(end - start)
    if end <= start:
        raise SpecError(f"{where}: end must be after start")
    if length < MIN_CLIP_SECONDS:
        raise SpecError(f"{where}: must be at least {MIN_CLIP_SECONDS:g} s long")
    if length > MAX_CLIP_SECONDS:
        raise SpecError(f"{where}: at most {MAX_CLIP_SECONDS:g} s long (got {length:.1f})")
    reaction = raw.get("reaction", True)
    if not isinstance(reaction, bool):
        raise SpecError(f"{where}: reaction must be true or false")
    out = {"start_s": start, "end_s": end, "length": length, "rank": raw.get("rank"),
           "reaction": reaction}
    for key, limit in CLIP_TEXT_LIMITS.items():
        out[key] = _text(raw.get(key), f"{where} {key}", limit)
    return out


def _check_ranks(clips):
    ranks = [c["rank"] for c in clips]
    given = [r for r in ranks if r is not None]
    if not given:
        return
    if len(given) != len(ranks):
        raise SpecError("rank: give every clip a rank or none of them")
    if any(isinstance(r, bool) or not isinstance(r, int) or not 1 <= r <= 9 for r in given):
        raise SpecError("rank must be a whole number from 1 to 9")
    if len(set(given)) != len(given):
        raise SpecError("rank values must be unique")


def validate_spec(raw, brand=None):
    """Check a spec dict and return a normalised copy; raise SpecError with a clear message.

    `brand` supplies the defaults for `handle` and `channel` (see tools/brand.py); without it the
    generic placeholders are used.
    """
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    brand = brand or brand_config.defaults()
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise SpecError(f"unknown field(s) {', '.join(sorted(unknown))}")
    for key in ("title", "source", "output", "clips"):
        if key not in raw:
            raise SpecError(f"missing required field: {key}")
    handle = _text(raw.get("handle", brand["handle"]), "handle", TEXT_LIMITS["handle"], True)
    if not handle.startswith("@"):
        raise SpecError("handle must start with @")
    if not isinstance(raw["output"], str) or not raw["output"].lower().endswith(".mp4"):
        raise SpecError("output must be a path ending in .mp4")
    if not isinstance(raw["source"], str) or not raw["source"]:
        raise SpecError("source must be a path string")
    clips_raw = raw["clips"]
    if not isinstance(clips_raw, list) or not 1 <= len(clips_raw) <= MAX_CLIPS:
        raise SpecError(f"clips must be a list of 1 to {MAX_CLIPS} clips")
    clips = [_clip(c, i) for i, c in enumerate(clips_raw)]
    _check_ranks(clips)
    return {
        "title": _text(raw["title"], "title", TEXT_LIMITS["title"], True),
        "subtitle": _text(raw.get("subtitle"), "subtitle", TEXT_LIMITS["subtitle"]),
        "handle": handle,
        "channel": _text(raw.get("channel", brand["channel"]), "channel", TEXT_LIMITS["channel"], True),
        "source": raw["source"], "output": raw["output"],
        "end_title": _text(raw.get("end_title") or f"FOLLOW {handle.upper()}",
                           "end_title", TEXT_LIMITS["end_title"]),
        "end_line": _text(raw.get("end_line", "NEW CLIPS EVERY MATCHDAY"),
                          "end_line", TEXT_LIMITS["end_line"]),
        "clips": clips,
    }


def load_spec(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return validate_spec(json.load(handle), brand_config.load_brand())
    except OSError as exc:
        raise SpecError(f"cannot read spec {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec {path} is not valid JSON: {exc}")


def scene_plan(spec):
    """Ordered scenes with their durations: title, [rank card, clip]..., end."""
    plan = [{"kind": "title", "duration": TITLE_SECONDS}]
    for i, clip in enumerate(spec["clips"]):
        if clip["rank"] is not None:
            plan.append({"kind": "rank", "duration": RANK_SECONDS, "index": i})
        plan.append({"kind": "clip", "duration": clip["length"], "index": i})
    plan.append({"kind": "end", "duration": END_SECONDS})
    return plan


# ---------------------------------------------------------------- graphics templates

def fit_size(text, base, max_width, floor=40, width=CHAR_WIDTH):
    """Largest font size up to `base` at which `text` fits inside `max_width` pixels."""
    if not text or len(text) * base * width <= max_width:
        return base
    return max(floor, int(max_width / (len(text) * width)))


def progress_html(ranks, current):
    """One box per rank in the order shown; empty when the short has no ranks."""
    if any(r is None for r in ranks):
        return ""
    cells = []
    for i, rank in enumerate(ranks):
        state = "done" if i < current else "current" if i == current else "todo"
        cells.append(f'<div class="step {state}">{rank}</div>')
    return "".join(cells)


def fill_template(template, values):
    """Replace {{name}} with escaped text and {{raw:name}} with trusted markup."""
    def sub(match):
        raw, name = match.group(1), match.group(2)
        if name not in values:
            raise ToolError(f"graphics template placeholder {name!r} has no value")
        return str(values[name]) if raw else html.escape(str(values[name]), quote=True)
    return re.sub(r"\{\{(raw:)?(\w+)\}\}", sub, template)


def stack_positions(top, title_size, subtitle_size):
    """Rule and subtitle y positions below a title that starts at `top`."""
    rule_y = int(top + title_size * 1.1 + 16)
    return {"rule_y": rule_y, "sub_y": rule_y + 34, "subtitle_size": subtitle_size}


def _headline(spec, reaction):
    """Title block values; the reaction layout starts higher and uses smaller type."""
    base, sub_base, top = (120, 32, 108) if reaction else (132, 34, 262)
    size = fit_size(spec["title"].upper(), base, 960)
    sub_size = fit_size(spec["subtitle"].upper(), sub_base, 960, 20, MONO_WIDTH)
    return {"title": spec["title"].upper(), "title_size": size,
            "subtitle": spec["subtitle"].upper(), "handle": spec["handle"],
            "channel": spec["channel"].upper(),
            **stack_positions(top, size, sub_size)}


def title_values(spec):
    size = fit_size(spec["title"].upper(), 150, 960)
    sub_size = fit_size(spec["subtitle"].upper(), 38, 960, 20, MONO_WIDTH)
    return {"title": spec["title"].upper(), "title_size": size,
            "subtitle": spec["subtitle"].upper(), "handle": spec["handle"],
            "channel": spec["channel"].upper(),
            **stack_positions(660, size, sub_size)}


def end_values(spec):
    size = fit_size(spec["end_title"].upper(), 110, 960)
    line_size = fit_size(spec["end_line"].upper(), 40, 960, 20, MONO_WIDTH)
    return {"title": spec["end_title"].upper(), "title_size": size,
            "subtitle": spec["end_line"].upper(), "handle": spec["handle"],
            "channel": spec["channel"].upper(),
            "handle_size": fit_size(spec["handle"], 120, 960),
            **stack_positions(700, size, line_size)}


def clip_values(spec, index):
    """Text and sizes for the overlay of clip `index` (also used by the rank card)."""
    clip = spec["clips"][index]
    ranks = [c["rank"] for c in spec["clips"]]
    ranked, react = clip["rank"] is not None, clip["reaction"]
    # (text column width, player, team, detail, caption font sizes)
    if react:
        text_w, sizes = (780, (76, 30, 26, 42)) if ranked else (960, (84, 32, 26, 42))
    else:
        text_w, sizes = (690, (104, 36, 30, 60)) if ranked else (960, (104, 36, 30, 60))
    player, team = clip["player"].upper(), clip["team"].upper()
    caption = clip["caption"].upper()
    return {**_headline(spec, react),
            "rank": "" if clip["rank"] is None else str(clip["rank"]),
            "player": player, "player_size": fit_size(player, sizes[0], text_w),
            "team": team, "team_size": fit_size(team, sizes[1], text_w, 18, MONO_WIDTH),
            "detail": clip["detail"],
            "detail_size": fit_size(clip["detail"], sizes[2], text_w, 18, MONO_WIDTH),
            "caption": caption, "caption_size": fit_size(caption, sizes[3], 904),
            "progress": progress_html(ranks, index)}


def rank_card_values(spec, index):
    vals = clip_values(spec, index)
    vals["player_size"] = fit_size(vals["player"], 124, 960)
    vals["team_size"] = fit_size(vals["team"], 42, 960, 20, MONO_WIDTH)
    vals["caption_size"] = fit_size(vals["caption"], 76, 960)
    return vals


# ---------------------------------------------------------------- command builders

def chrome_command(html_path, png_path, profile_dir):
    return ["google-chrome", "--headless=new", f"--screenshot={png_path}",
            f"--window-size={WIDTH},{HEIGHT}", "--default-background-color=00000000",
            f"--user-data-dir={profile_dir}", "--hide-scrollbars",
            "--virtual-time-budget=2000", "--disable-gpu", "--no-first-run",
            "--disable-extensions", "--disable-background-networking",
            "--allow-file-access-from-files", Path(html_path).as_uri()]


def _background_chain():
    """Cover-fit copy of the frame, blurred (at 1/5 size for speed) and darkened to navy."""
    return ("[bgsrc]scale=216:384:force_original_aspect_ratio=increase,crop=216:384,"
            "gblur=sigma=5,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color={NAVY}@0.62:t=fill,"
            f"scale={WIDTH}:{HEIGHT}:flags=bilinear[bg]")


def _video_chains(clip):
    """Video part of the graph up to the composed [base] stream."""
    if not clip["reaction"]:
        return [f"[0:v]fps={FPS},split=2[bgsrc][fgsrc]", _background_chain(),
                f"[fgsrc]scale={WIDTH}:{VIDEO_H}:flags=lanczos[vid]",
                f"[bg][vid]overlay=0:{VIDEO_Y}[base]"]
    cx, cy, cw, ch = CAM_CROP
    px, py, pw, ph = CAM_PATCH
    return [f"[0:v]fps={FPS},split=4[bgsrc][gamesrc][patchsrc][camsrc]", _background_chain(),
            f"[patchsrc]crop={pw}:{ph}:{px}:{py},gblur=sigma=40,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color={NAVY}@0.45:t=fill[patch]",
            f"[gamesrc][patch]overlay={px}:{py},scale={WIDTH}:{VIDEO_H}:flags=lanczos[vid]",
            f"[camsrc]crop={cw}:{ch}:{cx}:{cy},scale={WIDTH}:{CAM_H}:flags=lanczos,"
            "unsharp=5:5:0.6:5:5:0[cam]",
            f"[bg][vid]overlay=0:{GAME_Y}[b1]", f"[b1][cam]overlay=0:{CAM_Y}[base]"]


def clip_filter(clip):
    """Blurred cover background, game frame, optional reaction panel, overlay PNG, fades."""
    out_st = f"{clip['length'] - AUDIO_FADE:.3f}"
    return ";".join([
        *_video_chains(clip),
        f"[base][1:v]overlay=0:0:format=auto:shortest=1,"
        f"fade=t=in:st=0:d={VIDEO_FADE}:color={NAVY},format=yuv420p[v]",
        "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"afade=t=in:st=0:d={AUDIO_FADE},afade=t=out:st={out_st}:d={AUDIO_FADE}[a]",
    ])


def clip_command(spec, clip, overlay_png, out_path):
    length = f"{clip['length']:.3f}"
    return [*FFMPEG, "-ss", f"{clip['start_s']:.3f}", "-t", length, "-i", spec["source"],
            "-loop", "1", "-framerate", str(FPS), "-t", length, "-i", overlay_png,
            "-filter_complex", clip_filter(clip), "-map", "[v]", "-map", "[a]",
            *ENCODE, *PCM, "-t", length, out_path]


CARD_MOTION = {  # overlay x:y expressions; commas escaped for the filtergraph parser
    "up": ("0", "70*pow(1-min(t/0.45\\,1)\\,3)"),
    "right": ("500*pow(1-min(t/0.4\\,1)\\,3)", "0"),
}
WHOOSH = ("highpass=f=300,lowpass=f=5000,afade=t=in:st=0:d=0.3,afade=t=out:st=0.45:d=0.55,"
          "aformat=channel_layouts=stereo")


def card_command(fg_png, out_path, duration, motion, audio):
    """Navy card with a transparent text layer that fades and slides in."""
    secs = f"{duration:.3f}"
    x_expr, y_expr = CARD_MOTION[motion]
    if audio == "whoosh":
        src = f"anoisesrc=d={secs}:c=pink:r=48000:a=0.3"
        a_graph = f"[2:a]{WHOOSH}[a]"
    else:
        src = "anullsrc=r=48000:cl=stereo"
        a_graph = "[2:a]anull[a]"
    graph = ";".join([
        "[1:v]format=rgba,fade=t=in:st=0:d=0.3:alpha=1[fg]",
        f"[0:v][fg]overlay=x={x_expr}:y={y_expr}:format=auto:shortest=1,format=yuv420p[v]",
        a_graph])
    return [*FFMPEG, "-f", "lavfi", "-t", secs, "-i", f"color=c={NAVY}:s={WIDTH}x{HEIGHT}:r={FPS}",
            "-loop", "1", "-framerate", str(FPS), "-t", secs, "-i", fg_png,
            "-f", "lavfi", "-t", secs, "-i", src,
            "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
            *ENCODE, *PCM, "-t", secs, out_path]


def concat_list(paths):
    return "".join("file '" + p.replace("'", "'\\''") + "'\n" for p in paths)


def concat_command(list_path, out_path):
    """Video is stream-copied; audio is encoded once so there are no AAC seams."""
    return [*FFMPEG, "-f", "concat", "-safe", "0", "-i", list_path,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", out_path]


# ---------------------------------------------------------------- rendering

def png_size(path):
    with open(path, "rb") as handle:
        head = handle.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ToolError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def screenshot(html_path, png_path, profile_dir):
    """Run headless Chrome on a static page; kill the whole process group on timeout."""
    cmd = chrome_command(html_path, png_path, profile_dir)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=CHROME_TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise ToolError(f"chrome timed out after {CHROME_TIMEOUT} s on {html_path}")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # stray helper processes
        except ProcessLookupError:
            pass
    if not os.path.exists(png_path) or png_size(png_path) != (WIDTH, HEIGHT):
        raise ToolError(f"chrome did not produce a {WIDTH}x{HEIGHT} PNG for {html_path}")


def read_graphic(name):
    with open(os.path.join(GRAPHICS_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def page_html(page, values):
    """Fill graphics/<page>.html; clip pages pull their lower block from a part file."""
    values = {**values, "base": Path(GRAPHICS_DIR).as_uri() + "/"}
    if page.startswith("clip"):
        prefix = "react" if page == "clip-react" else "clip"
        part = f"{prefix}-{'ranked' if values['rank'] else 'plain'}.part.html"
        values["bottom"] = fill_template(read_graphic(part), values)
    return fill_template(read_graphic(page + ".html"), values)


def graphic_jobs(spec, tmp):
    """List of (page, values, png path) for every graphic the short needs."""
    jobs = [("title", title_values(spec), os.path.join(tmp, "title.png")),
            ("end", end_values(spec), os.path.join(tmp, "end.png"))]
    for i, clip in enumerate(spec["clips"]):
        page = "clip-react" if clip["reaction"] else "clip"
        jobs.append((page, clip_values(spec, i), os.path.join(tmp, f"clip{i}.png")))
        if clip["rank"] is not None:
            jobs.append(("rank", rank_card_values(spec, i), os.path.join(tmp, f"rank{i}.png")))
    return jobs


def build_graphics(spec, tmp):
    jobs = graphic_jobs(spec, tmp)

    def one(n_job):
        n, (page, values, png) = n_job
        page_path = os.path.join(tmp, f"{page}-{n}.html")
        with open(page_path, "w", encoding="utf-8") as handle:
            handle.write(page_html(page, values))
        screenshot(page_path, png, os.path.join(tmp, f"profile-{n}"))
    with ThreadPoolExecutor(max_workers=CHROME_WORKERS) as pool:
        list(pool.map(one, enumerate(jobs)))
    return {os.path.basename(png): png for _, _, png in jobs}


def scene_command(spec, scene, tmp, out_path):
    kind = scene["kind"]
    if kind == "clip":
        i = scene["index"]
        return clip_command(spec, spec["clips"][i], os.path.join(tmp, f"clip{i}.png"), out_path)
    if kind == "rank":
        png = os.path.join(tmp, f"rank{scene['index']}.png")
        return card_command(png, out_path, scene["duration"], "right", "whoosh")
    png = os.path.join(tmp, f"{kind}.png")
    return card_command(png, out_path, scene["duration"], "up", "silence")


def preflight(spec):
    for tool in ("ffmpeg", "ffprobe", "google-chrome"):
        if shutil.which(tool) is None:
            raise ToolError(f"{tool} not found on PATH")
    if not os.path.isfile(spec["source"]):
        raise SpecError(f"source video not found: {spec['source']}")
    duration = probe_duration(spec["source"])
    for i, clip in enumerate(spec["clips"], 1):
        if clip["end_s"] > duration:
            raise SpecError(f"clip {i} ends at {format_hms(clip['end_s'])}, past the end "
                            f"of the source ({format_hms(duration)})")


def render(spec, keep_temp=False):
    """Build every scene as its own intermediate, concat them, verify the length."""
    out = os.path.abspath(spec["output"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="render_short_")
    try:
        log(f"graphics: {len(graphic_jobs(spec, tmp))} pages via headless chrome")
        build_graphics(spec, tmp)
        scenes = []
        for n, scene in enumerate(scene_plan(spec)):
            path = os.path.join(tmp, f"scene{n:02d}.mov")
            log(f"scene {n + 1}: {scene['kind']} ({scene['duration']:.1f} s)")
            run_tool(scene_command(spec, scene, tmp, path))
            scenes.append(path)
        list_path = os.path.join(tmp, "scenes.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            handle.write(concat_list(scenes))
        partial = out + ".part.mp4"
        run_tool(concat_command(list_path, partial))
        real = probe_duration(partial)
        try:
            verify_duration(real)
        except ToolError:
            os.remove(partial)
            raise
        os.replace(partial, out)
        return out, real
    finally:
        if keep_temp:
            log(f"kept temp dir {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Render a vertical YouTube Short from a JSON spec.")
    parser.add_argument("spec", help="path to the spec JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the spec and print the length budget, render nothing")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the temp dir with the HTML, PNGs and scene files")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        spec = load_spec(args.spec)
        total = total_duration(spec)
        log(f"planned length {total:.1f} s of {MAX_TOTAL_SECONDS:.1f} s allowed")
        check_budget(total)
        if args.dry_run:
            return 0
        preflight(spec)
        out, real = render(spec, args.keep_temp)
    except ToolError as exc:
        log(f"error: {exc}")
        return 1
    size = os.path.getsize(out) / 1e6
    print(f"{out}  {real:.2f} s  {size:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
