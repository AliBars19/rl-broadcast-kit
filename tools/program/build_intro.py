#!/usr/bin/env python3
"""Build the program intro (1920x1080, 60 fps) from replay shots and a JSON spec.

Standard library plus ffmpeg, ffprobe and google-chrome on PATH. Usage:
    python3 -I tools/program/build_intro.py SPEC.json [--dry-run] [--keep-temp]
See README.md for the spec format. Each shot becomes its own intermediate (cut, speed, scale,
graphic overlay, flash cut), the sound effects are synthesised in one WAV, and a last pass joins
the video without re-encoding and mixes the audio.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # tools/brand.py

import brand as brand_config  # noqa: E402
import chrome_frames as cf  # noqa: E402
import intro_sfx  # noqa: E402
import intro_spec as sp  # noqa: E402
from chrome_frames import ToolError, log, probe_duration, run_tool  # noqa: E402
from render_short import MONO_WIDTH, fill_template, fit_size  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRAM_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "program")
INTRO_DIR = os.path.join(PROGRAM_DIR, "intro")

FPS = sp.FPS
WIDTH, HEIGHT = cf.WIDTH, cf.HEIGHT
FLASH_FRAMES = 7                  # 6 to 10 frames of white or blue after each hard cut
FADE_OUT_SECONDS = 0.5            # fade to black at the very end
AUDIO_EDGE = 0.01                 # click guard on every shot edge
GAME_VOLUME = 0.6
GAME_VOLUME_WITH_MUSIC = 0.3
MUSIC_VOLUME = 0.8
SLOW_LOWPASS_HZ = 1800            # slow-motion game audio is darkened
DURATION_TOLERANCE = 0.1
FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
ENCODE = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "22", "-b:v", "0",
          "-maxrate", "20M", "-bufsize", "40M", "-profile:v", "high", "-pix_fmt", "yuv420p",
          "-r", str(FPS), "-g", str(FPS)]
PCM = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]


# ---------------------------------------------------------------- ffmpeg command builders

def atempo_chain(speed):
    """atempo filters for `speed` (each stage must stay within 0.5..2)."""
    factors, rest = [], speed
    while rest < 0.5:
        factors.append(0.5)
        rest /= 0.5
    if abs(rest - 1.0) > 1e-9:
        factors.append(rest)
    return ",".join(f"atempo={f:g}" for f in factors)


def flash_colour(index):
    """White on odd cuts, bright blue on even ones."""
    return "white" if index % 2 else "0x4da3ff"


def _video_graph(spec, index, overlay_input, flash_input):
    shot, seconds = spec["shots"][index], spec["shots"][index]["frames"] / FPS
    graph = [f"[0:v]setpts=(PTS-STARTPTS)/{shot['speed']:g},fps={FPS},"
             f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:flags=lanczos,"
             f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p[v0]"]
    last = "v0"
    if overlay_input is not None:
        graph.append(f"[{last}][{overlay_input}:v]overlay=0:0:format=auto[v1]")
        last = "v1"
    if flash_input is not None:
        graph.append(f"[{flash_input}:v]format=rgba,fade=t=out:st=0:d={FLASH_FRAMES / FPS:.3f}:alpha=1[fl]")
        graph.append(f"[{last}][fl]overlay=0:0:format=auto:eof_action=pass[v2]")
        last = "v2"
    tail = "format=yuv420p"
    if index == len(spec["shots"]) - 1:
        tail = (f"fade=t=out:st={seconds - FADE_OUT_SECONDS:.3f}:d={FADE_OUT_SECONDS:.3f}:color=black,"
                + tail)
    graph.append(f"[{last}]{tail}[v]")
    return graph


def _audio_graph(shot):
    seconds = shot["frames"] / FPS
    steps = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
    tempo = atempo_chain(shot["speed"])
    if tempo:
        steps.append(tempo)
    if shot["speed"] < 1:
        steps.append(f"lowpass=f={SLOW_LOWPASS_HZ}")
    steps += [f"afade=t=in:st=0:d={AUDIO_EDGE}", f"apad=whole_dur={seconds:.3f}",
              f"atrim=end={seconds:.3f}", "asetpts=PTS-STARTPTS",
              f"afade=t=out:st={seconds - AUDIO_EDGE:.3f}:d={AUDIO_EDGE}"]
    return "[0:a]" + ",".join(steps) + "[a]"


def segment_command(spec, index, overlay_dir, out_path):
    """One shot: seek, cut, speed, 1080p60, graphic overlay, flash cut, audio rebuilt."""
    shot = spec["shots"][index]
    cmd = [*FFMPEG, "-ss", f"{shot['start_s']:.3f}", "-t", f"{shot['end_s'] - shot['start_s']:.3f}",
           "-i", shot["source"]]
    next_input, overlay_input, flash_input = 1, None, None
    if overlay_dir is not None:
        cmd += ["-framerate", str(FPS), "-start_number", "0", "-i",
                os.path.join(overlay_dir, "%05d.png")]
        overlay_input, next_input = next_input, next_input + 1
    if index > 0:
        cmd += ["-f", "lavfi", "-i", f"color=c={flash_colour(index)}:s={WIDTH}x{HEIGHT}:r={FPS}"
                f":d={FLASH_FRAMES / FPS:.3f}"]
        flash_input = next_input
    graph = _video_graph(spec, index, overlay_input, flash_input) + [_audio_graph(shot)]
    return [*cmd, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
            *ENCODE, *PCM, "-frames:v", str(shot["frames"]), out_path]


def concat_list(paths):
    return "".join("file '" + p.replace("'", "'\\''") + "'\n" for p in paths)


def _mix_graph(spec, total):
    secs = f"{total:.3f}"
    with_music = spec["music"] is not None
    game = GAME_VOLUME_WITH_MUSIC if with_music else GAME_VOLUME
    graph = [f"[0:a]volume={game}[g]", "[1:a]anull[s]"]
    inputs = "[g][s]"
    if with_music:
        graph.append("[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"atrim=end={secs},apad=whole_dur={secs},afade=t=in:st=0:d=0.5,"
                     f"volume={MUSIC_VOLUME}[m]")
        inputs += "[m]"
    count = 3 if with_music else 2
    fade_at = total - FADE_OUT_SECONDS
    graph.append(f"{inputs}amix=inputs={count}:normalize=0:duration=first,alimiter=limit=0.95,"
                 f"afade=t=out:st={fade_at:.3f}:d={FADE_OUT_SECONDS:.3f}[a]")
    return ";".join(graph)


def final_command(spec, list_path, sfx_path, out_path):
    """Join the shots (video copied), mix game audio, SFX and optional music into AAC."""
    total = sp.total_duration(spec)
    cmd = [*FFMPEG, "-f", "concat", "-safe", "0", "-i", list_path, "-i", sfx_path]
    if spec["music"] is not None:
        cmd += ["-i", spec["music"]]
    return [*cmd, "-filter_complex", _mix_graph(spec, total), "-map", "0:v", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-t", f"{total:.3f}", "-movflags", "+faststart", out_path]


# ---------------------------------------------------------------- graphics

def graphic_values(spec):
    """Placeholder values for every graphic page (text is escaped by fill_template)."""
    return {
        "base": Path(INTRO_DIR).as_uri() + "/",
        "title": spec["title"], "title_size": fit_size(spec["title"], 190, 1700, 60),
        "subtitle": spec["subtitle"],
        "subtitle_size": fit_size(spec["subtitle"], 56, 1000, 26, MONO_WIDTH),
        "matchday": spec["matchday"], "opponent": spec["opponent"],
        "opponent_size": fit_size(spec["opponent"], 96, 740, 30),
        "home": spec["home_team"], "home_size": fit_size(spec["home_team"], 96, 740, 30),
        "channel": spec["channel"].upper(),
        "sponsor_logo": Path(spec["sponsor_logo"]).as_uri() if spec["sponsor_logo"] else "",
    }


def graphic_page(spec, graphic_id):
    template_path = os.path.join(INTRO_DIR, sp.GRAPHICS[graphic_id]["page"])
    with open(template_path, encoding="utf-8") as handle:
        return fill_template(handle.read(), graphic_values(spec))


def render_graphics(spec, tmp):
    """Render each used graphic to a PNG sequence sized to its shot. Returns {id: dir}."""
    dirs = {}
    for shot in spec["shots"]:
        gid = shot["graphic"]
        if not gid:
            continue
        page = os.path.join(tmp, f"{gid}.html")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write(graphic_page(spec, gid))
        dirs[gid] = os.path.join(tmp, f"frames-{gid}")
        done = cf.render_sequence(page, shot["frames"], sp.GRAPHICS[gid]["active"], dirs[gid])
        log(f"graphic {gid}: {done} of {shot['frames']} frames rendered")
    return dirs


# ---------------------------------------------------------------- run

def has_audio(path):
    out = run_tool(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                    "stream=index", "-of", "csv=p=0", path])
    return bool(out.strip())


def preflight(spec):
    for tool in ("ffmpeg", "ffprobe", "google-chrome"):
        if shutil.which(tool) is None:
            raise ToolError(f"{tool} not found on PATH")
    for key in ("sponsor_logo", "music"):
        if spec[key] is not None and not os.path.isfile(spec[key]):
            raise sp.SpecError(f"{key} file not found: {spec[key]}")
    lengths = {}
    for i, shot in enumerate(spec["shots"], 1):
        src = shot["source"]
        if not os.path.isfile(src):
            raise sp.SpecError(f"shot {i}: source not found: {src}")
        if src not in lengths:
            if not has_audio(src):
                raise sp.SpecError(f"shot {i}: {src} has no audio stream")
            lengths[src] = probe_duration(src)
        if shot["end_s"] > lengths[src] + 0.05:
            raise sp.SpecError(f"shot {i}: ends at {shot['end_s']:.1f} s, past the end of {src} "
                               f"({lengths[src]:.1f} s)")


def build_segments(spec, tmp, dirs):
    paths = []
    for i, shot in enumerate(spec["shots"]):
        path = os.path.join(tmp, f"shot{i:02d}.mov")
        log(f"shot {i + 1}/{len(spec['shots'])}: {shot['frames'] / FPS:.2f} s"
            f"{' + ' + shot['graphic'] if shot['graphic'] else ''}")
        run_tool(segment_command(spec, i, dirs.get(shot["graphic"]), path))
        paths.append(path)
    return paths


def render(spec, keep_temp=False):
    out = spec["output"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="build_intro_")
    try:
        dirs = render_graphics(spec, tmp)
        segments = build_segments(spec, tmp, dirs)
        sfx_path = os.path.join(tmp, "sfx.wav")
        total = sp.total_duration(spec)
        run_tool(intro_sfx.sfx_command(sp.sfx_events(spec), total, sfx_path))
        list_path = os.path.join(tmp, "shots.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            handle.write(concat_list(segments))
        partial = out + ".part.mp4"
        run_tool(final_command(spec, list_path, sfx_path, partial))
        real = probe_duration(partial)
        if abs(real - total) > DURATION_TOLERANCE:
            os.remove(partial)
            raise ToolError(f"rendered length {real:.2f} s differs from the planned {total:.2f} s")
        os.replace(partial, out)
        return out, real
    finally:
        if keep_temp:
            log(f"kept temp dir {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def load_spec(path):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise sp.SpecError(f"cannot read spec {path}: {exc}")
    except json.JSONDecodeError as exc:
        raise sp.SpecError(f"spec {path} is not valid JSON: {exc}")
    return sp.validate_spec(raw, os.path.dirname(os.path.abspath(path)), brand_config.load_brand())


def describe(spec):
    for i, (shot, slot) in enumerate(zip(spec["shots"], sp.timeline(spec)), 1):
        log(f"  {i}. {slot['start_frame'] / FPS:6.2f} s  {shot['frames'] / FPS:5.2f} s  "
            f"x{shot['speed']:g}  {shot['graphic'] or '-':9s} {os.path.basename(shot['source'])}")
    log(f"planned length {sp.total_duration(spec):.2f} s "
        f"({sp.MIN_TOTAL:g} to {sp.MAX_TOTAL:g} s allowed), {len(sp.sfx_events(spec))} sound effects")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Build the program intro from a JSON spec.")
    parser.add_argument("spec", help="path to the spec JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the spec and print the timeline, render nothing")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the temp dir with the graphic frames and shot files")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        spec = load_spec(args.spec)
        describe(spec)
        if args.dry_run:
            return 0
        preflight(spec)
        out, real = render(spec, args.keep_temp)
    except ToolError as exc:
        log(f"error: {exc}")
        return 1
    print(f"{out}  {real:.2f} s  {os.path.getsize(out) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
