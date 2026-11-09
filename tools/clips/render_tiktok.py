#!/usr/bin/env python3
"""Render a vertical (1080x1920, 60 fps) TikTok from clips of a 1920x1080 stream recording.

Style: a black caption card between the clips, one card sound, extra effects on top.
Standard library plus ffmpeg/ffprobe on PATH. Usage:
    python3 -I render_tiktok.py SPEC.json [--dry-run] [--keep-temp]
See README.md for the spec format.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops the script dir

from find_candidates import ToolError, format_hms, log, require_tools, run_tool  # noqa: E402
from render_short import SpecError, parse_timestamp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SFX_DIR = os.path.join(HERE, "sfx")
CARD_HIT = "card_hit.wav"
CARD_HIT_GAIN_DB = 0.0          # constant gain of the card sound, on every card

FPS = 60
WIDTH, HEIGHT = 1080, 1920
SRC_W, SRC_H = 1920, 1080
# Crops of the 1920x1080 source. The strip keeps the scoreboard, the game block drops the
# facecam (bottom left) and the spectator bar (bottom).
STRIP_CROP = (1380, 126, 270, 0)      # w, h, x, y
STRIP_SIZE = (1080, 99)
GAME_CROP = (948, 839, 486, 126)      # w, h, x, y
GAME_SIZE = (1080, 956)
# Layouts: where things sit in a 1920x1080 recording of a stream that uses the overlay pages of
# this kit. "kit" (the default) is the kit's own gameplay scene: the scoreboard bars along the top
# and the webcam frame (webcam-frame.html) as a box at about x 30..610, y 708..1035, no other bar,
# so the game crop can be tall:
#   "kit"          strip over the game, the facecam hidden;
#   "kit-reaction" strip, game, then the facecam underneath.
# "bottom-cam" is an example of a second geometry, for an overlay that has the facecam in the bottom
# left corner under a spectator bar at the bottom of the frame. Use it as a model for your own
# (see "Adding your own layout" in README.md). "broadcast" is described further down.
LAYOUTS = ("bottom-cam", "kit", "kit-reaction")
DEFAULT_LAYOUT = "kit"
KIT_GAME_CROP = (948, 954, 540, 126)  # w, h, x, y; keeps 72 px of the facecam box at the left
KIT_GAME_SIZE = (1080, 1087)
# The leftover facecam is covered before the crop with the mirror of the pitch beside it:
# the source region KIT_PATCH_SRC (w, h, x, y) is flipped, blurred and overlaid at KIT_PATCH_AT.
# The brackets end at x 611 but their compression halo reaches x 614, so the patch is 78 px
# wide (x 540..618) instead of the 72 px that only just covers the box. Every number is even:
# crop and overlay round odd offsets and sizes of yuv420p down, which left one bracket row out.
KIT_PATCH_SRC = (78, 336, 618, 704)
KIT_PATCH_AT = (540, 704)
KIT_PATCH_BLUR = 2
# The facecam box is x 30..610, y 708..1035, but its corner brackets and a 2 px dark frame sit
# on its edge, and the picture inside is x 34..605, y 713..1032. The crop stays inside the
# picture (so no bracket or frame shows) with an aspect within 1% of CAM_SIZE. It also starts
# 3 rows in: the top rows of the picture carry a blue chroma bleed from the game above.
CAM_CROP = (560, 316, 40, 716)        # w, h, x, y
CAM_SIZE = (1080, 609)
CAM_SHARPEN = "unsharp=5:5:0.5"
STACK_HEIGHT = {"bottom-cam": STRIP_SIZE[1] + GAME_SIZE[1],
                "kit": STRIP_SIZE[1] + KIT_GAME_SIZE[1],
                "kit-reaction": STRIP_SIZE[1] + KIT_GAME_SIZE[1] + CAM_SIZE[1]}
PAD_Y = {name: (HEIGHT - height) // 2 for name, height in STACK_HEIGHT.items()}  # centred
# Facecam windows (clip field "cam", bottom-cam and kit only), laid over the composed picture with
# a hard cut:
#   "full"  replaces the game block (same rectangle, so the scoreboard strip stays). A crop of the
#           facecam picture is scaled to the size of the game block; x is the source x of its left
#           edge (default centres it on the picture) and must keep the crop inside the picture.
#           The caption would sit across the face, so it is hidden.
#   "inset" the facecam crop scaled to a box in the bottom left corner of the game block,
#           CAM_INSET_MARGIN px from the left of the frame and from the bottom of the block (the
#           y is rounded down to an even number, so the real gap may be 25 px). The caption stays.
# The numbers per layout are in CAM_GEOMETRY, measured on real frames.
CAM_MODES = ("full", "inset")
MIN_CAM_SECONDS = 0.5                 # shortest window
CAM_INSET_MARGIN = 24


class CamGeometry(NamedTuple):
    full_crop: tuple      # w, h, default x, y of the source crop of a full window
    full_x_range: tuple   # the full crop must lie inside these source x values
    full_size: tuple      # what it is scaled to: the game block of the layout
    inset_crop: tuple     # w, h, x, y of the source crop of an inset window
    inset_size: tuple
    game_y: int           # top of the game block in the 1080x1920 picture
    game_height: int

    @property
    def inset_at(self):
        """x, y of the inset: the margin from the left and from the bottom of the game block,
        with an even y (the bottom edge of the block can be odd)."""
        bottom = self.game_y + self.game_height
        return CAM_INSET_MARGIN, (bottom - CAM_INSET_MARGIN - self.inset_size[1]) // 2 * 2


# kit: the picture of the facecam box is source x 34..605, y 713..1032. Full is a 314x316 crop
# (default x 162 centres it), inset is CAM_CROP.
CAM_FULL_CROP = (314, 316, 162, 716)  # w, h, default x, y (y as CAM_CROP)
CAM_FULL_X_RANGE = (36, 604)
CAM_INSET_SIZE = (600, 338)
CAM_GAME_Y = PAD_Y["kit"] + STRIP_SIZE[1]    # top of the game block in the 1080x1920 picture
# bottom-cam: the facecam picture is source x 36..476, y 788..1030 (440x242). Full is a 274x242 crop
# (default x 118 centres it, x up to 202), inset is the whole picture.
BOTTOM_CAM_PICTURE = (440, 242, 36, 788)     # w, h, x, y
BOTTOM_CAM_FULL_CROP = (274, 242, 118, 788)
BOTTOM_CAM_X_RANGE = (36, 476)
BOTTOM_CAM_INSET_SIZE = (600, 330)
BOTTOM_CAM_GAME_Y = PAD_Y["bottom-cam"] + STRIP_SIZE[1]
CAM_GEOMETRY = {
    "bottom-cam": CamGeometry(BOTTOM_CAM_FULL_CROP, BOTTOM_CAM_X_RANGE, GAME_SIZE,
                           BOTTOM_CAM_PICTURE, BOTTOM_CAM_INSET_SIZE, BOTTOM_CAM_GAME_Y,
                           GAME_SIZE[1]),
    "kit": CamGeometry(CAM_FULL_CROP, CAM_FULL_X_RANGE, KIT_GAME_SIZE, CAM_CROP,
                       CAM_INSET_SIZE, CAM_GAME_Y, KIT_GAME_SIZE[1]),
}
CAM_LAYOUTS = tuple(CAM_GEOMETRY)
CAM_INSET_AT = CAM_GEOMETRY["kit"].inset_at

# Broadcast footage (for example a hook from an official broadcast): a plain 16:9 picture with no
# facecam and no stream overlay, so nothing is cut out of it. Layout "broadcast" is not a stacked
# layout (no strip, no pad): the whole frame, centre cropped by the clip's zoom and scaled to the
# frame width, sits where the kit game block is (same vertical centre, so the caption at
# CAPTION_Y and the TikTok bottom zone behave as on every other layout). The rest of the frame
# is the same clip, scaled to cover, blurred and darkened. The background is built at a sixteenth
# of the pixels and scaled up after the blur, which makes the blur cheap.
BROADCAST = "broadcast"
TOP = "top"
SPEC_LAYOUTS = LAYOUTS + (BROADCAST, TOP)   # what a spec may name; LAYOUTS are the stacked ones
BROADCAST_ZOOM_RANGE = (1.0, 1.6)
BROADCAST_DEFAULT_ZOOM = 1.25
BROADCAST_MIN_SOURCE = (1280, 720)          # 1914x1080 and 1280x720 broadcasts both occur
BROADCAST_ASPECT_TOLERANCE = 0.01           # of width / height against 16:9
BROADCAST_SIZE = (WIDTH, 608)               # 1080 * 9 / 16 = 607.5, rounded up to an even number
BROADCAST_AT = (0, round((CAM_GAME_Y + KIT_GAME_SIZE[1] / 2 - BROADCAST_SIZE[1] / 2) / 2) * 2)
BROADCAST_BG_SIZE = (WIDTH // 4, HEIGHT // 4)
BROADCAST_BG_BLUR = 8                       # gblur sigma at BROADCAST_BG_SIZE
BROADCAST_BG_BRIGHTNESS = -0.25
BROADCAST_BG_SATURATION = 0.8

# Layout "top": horizontal footage sits in the TOP THIRD of the screen. The whole 16:9 game picture
# is shown at full width, 1080x608, with its top edge at the spec's `top_y` (so the scoreline at
# the top of the frame stays visible), over the same blurred, darkened cover of the picture that
# layout "broadcast" uses. The caption sits above the picture (baseline TOP_CAPTION_GAP above
# top_y), never over it. A clip's `source_kind` says where the picture comes from:
#   "broadcast"  the whole source frame (any 16:9 from 1280x720 up); no facecam, so no cam windows;
#   "kit"        a 1920x1080 recording of the kit's gameplay scene: the whole frame, with the
#                facecam box covered by a blurred mirror of the pitch beside it;
#   "bottom-cam" a 1920x1080 recording with the bottom-cam geometry: the frame without the
#                spectator bar (the 16:9 region of the game block's height), facecam covered the
#                same way.
# A kit or bottom-cam clip may set "cover": false to leave its facecam box visible in the game
# picture (no patch at all; default true). Only layout top with those sources accepts it.
# `zoom` (1.0 to 1.6, default 1.0) crops the picture anchored at the TOP centre, so the scoreline is
# kept. Cam windows of mode "below" show the stream's facecam in a block under the game, from the
# window's start to its end, with a short fade in; the game keeps playing above it.
# top_y bounds: the lowest value keeps the tallest caption glyphs (the baseline is 40 px above
# top_y, a 64 px font) under the 150 px TikTok top zone; the highest keeps the cam block at least
# 450 px high above the bottom zone (the last BOTTOM_UI_ZONE px of the frame stay clear).
TOP_Y_DEFAULT = 300
TOP_Y_RANGE = (280, 400)
TOP_GAME_SIZE = (WIDTH, 608)
TOP_CAPTION_GAP = 40
TOP_ZOOM_RANGE = (1.0, 1.6)
TOP_DEFAULT_ZOOM = 1.0
BOTTOM_UI_ZONE = 370
SOURCE_KINDS = (BROADCAST, "kit", "bottom-cam")
DEFAULT_SOURCE_KIND = BROADCAST
BELOW = "below"
CAM_MODES_TOP = (BELOW,)
BELOW_GAP = 40                              # between the game block and the cam block
BELOW_MAX_HEIGHT = 608
BELOW_FADE_IN = 0.15
TOP_COVER_BLUR = 14


class TopSource(NamedTuple):
    base_crop: tuple      # w, h, x, y of the game region of the 1920x1080 frame, None: all of it
    cover: tuple          # w, h, x, y of the facecam box (with its brackets), covered in the picture
    donor_x: int          # the cover is the mirror of the same-size region starting here


# Both are worked out from the numbers above: the kit box is x 30..610 with brackets and halo to
# x 614 (KIT_PATCH_SRC starts right after it), the bottom-cam game block ends at GAME_CROP's
# bottom (made even), its 16:9 width is centred.
_BC_H = (GAME_CROP[1] + GAME_CROP[3]) // 2 * 2
_BC_W = round(_BC_H * 16 / 9 / 2) * 2
TOP_SOURCES = {
    "kit": TopSource(None, (KIT_PATCH_SRC[2] - 28, KIT_PATCH_SRC[1], 28, KIT_PATCH_SRC[3]),
                     KIT_PATCH_SRC[2]),
    "bottom-cam": TopSource((_BC_W, _BC_H, (SRC_W - _BC_W) // 2 // 2 * 2, 0),
                            (456, 258, 30, 782), 492)}

# Any bold TrueType font works. Override with the TIKTOK_FONT environment variable.
FONT_PATH = os.environ.get("TIKTOK_FONT") or "/usr/share/fonts/google-noto/NotoSans-Bold.ttf"
FONT_SIZE = 64
BORDER_W = 4
CAPTION_Y = 640

MIN_TOTAL_SECONDS = 15.0
MAX_TOTAL_SECONDS = 180.0
DURATION_TOLERANCE = 0.25
MIN_CLIP_SECONDS, MAX_CLIP_SECONDS = 1.0, 75.0   # 75 s allows one long play as a video
MAX_CLIPS = 20                  # the hook is not counted: it is one more clip, in front
HOOK = "hook"                   # the reference of the hook where clips are referred to by index
CARD_MIN, CARD_MAX, CARD_DEFAULT = 0.4, 1.0, 0.75
CLIP_FADE_IN, CLIP_FADE_OUT = 0.05, 0.06
MAX_PRE_ROLL = 1.0              # an sfx may begin up to this long before its clip
SFX_GAIN_RANGE = (-30.0, 6.0)
MUSIC_GAIN_RANGE = (-60.0, 0.0)
MUSIC_DEFAULT_GAIN = -16.0
MUSIC_FADE_OUT = 1.0
CAPTION_MAX = 24
THREADS = "4"
DASHES = ("\u2014", "\u2013")

TOP_KEYS = {"caption", "output", "card_seconds", "opening_card", "music", "clips", "layout",
            "sfx_dir", "card_sound", "crowd_bed", "crowd_stem", "clip_comp", "hook", "top_y"}
CLIP_KEYS = {"source", "start", "end", "sfx", "cam", "layout", "zoom", "source_kind", "cover"}
CAM_KEYS = {"start", "end", "mode", "x"}
SFX_KEYS = {"at", "name", "gain_db", "start", "duration", "fade_in", "fade_out",
            "stop_at_clip_end"}
SOUND_KEYS = {"name", "gain_db"}          # card_sound and crowd_bed
STEM_KEYS = {"path", "gain_db"}
MUSIC_KEYS = {"path", "gain_db", "start"}
SOUND_NAME = re.compile(r"[A-Za-z0-9_-]+")
# Crowd bed: one looped file under each clip (never under a card), each clip starting at its own
# point so two clips never open on the same second of the crowd.
BED_DEFAULT_GAIN = -12.0
BED_GAIN_RANGE = (-60.0, 6.0)
BED_FADE_IN = 0.15
BED_OFFSET_STEP = 7.3           # seconds into the bed that clip n starts at is n * step, wrapped
# Crowd stem: one WAV, built by make_crowd_stem.py to the length of the whole video, mixed from
# time 0 in place of the bed and the pasted sounds. Same gain range as the bed.
STEM_DEFAULT_GAIN = 0.0
STEM_LENGTH_TOLERANCE = 0.1     # seconds the stem may differ from the planned total
SFX_STOP_FADE = 0.08            # fade at the cap when stop_at_clip_end cuts a sound
# Light compressor on the stream audio of each clip, to bring the commentary forward.
CLIP_COMP_THRESHOLD_DB = -24
CLIP_COMP_RATIO = 3
CLIP_COMP_ATTACK_MS = 15
CLIP_COMP_RELEASE_MS = 250
CLIP_COMP_MAKEUP = 2            # linear factor
CLIP_COMP = (f"acompressor=threshold={CLIP_COMP_THRESHOLD_DB}dB:ratio={CLIP_COMP_RATIO}"
             f":attack={CLIP_COMP_ATTACK_MS}:release={CLIP_COMP_RELEASE_MS}"
             f":makeup={CLIP_COMP_MAKEUP}")
STEREO_48K = "aresample=48000,aformat=channel_layouts=stereo"   # every mix input starts with this

FFMPEG = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
          "-filter_threads", THREADS, "-filter_complex_threads", THREADS]
ENCODE = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "12M",
          "-maxrate", "16M", "-bufsize", "24M", "-bf", "0", "-g", str(FPS),
          "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS)]
PCM = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
# Linear loudnorm falls back to dynamic when the measured LRA is over the LRA target, and the
# card sound raises the LRA. 50 is loudnorm's maximum, so the linear second pass always holds.
LOUDNORM_LRA = 50
LOUDNORM = f"loudnorm=I=-14:TP=-1.5:LRA={LOUDNORM_LRA}"
CARD_SOUND_FADE = 0.005         # fade at the end of the card sound when it is cut to the card
SOUND_END_TOLERANCE = 0.001
MEASURED_KEYS = (("input_i", "measured_I"), ("input_lra", "measured_LRA"),
                 ("input_tp", "measured_TP"), ("input_thresh", "measured_thresh"),
                 ("target_offset", "offset"))
# The card sound peaks far above the stream audio. Without this limit loudnorm cannot reach
# -14 LUFS under its -1.5 dBTP ceiling (it measured -16.6 LUFS); limiting the crest first does.
LIMITER = "alimiter=limit=0.3:level=0"
DUCK = "sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400"


# ---------------------------------------------------------------- numbers and timestamps

def frame_snap(seconds: float) -> float:
    """Round a length to a whole number of output frames."""
    return round(seconds * FPS) / FPS


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{name} must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise SpecError(f"{name} must be a finite number")
    if not math.isfinite(number):
        raise SpecError(f"{name} must be a finite number")
    return number


def _timestamp(value, name):
    """parse_timestamp, after refusing NaN, Infinity and numbers too big for a float."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        _number(value, name)
    return parse_timestamp(value)


def _ranged(value, name, low, high):
    number = _number(value, name)
    if not low <= number <= high:
        raise SpecError(f"{name} {number:g} is outside {low:g} to {high:g}")
    return number


def _check_keys(raw, allowed, where):
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be an object")
    unknown = set(raw) - allowed
    if unknown:
        raise SpecError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")


# ---------------------------------------------------------------- spec validation

def _caption(value):
    if not isinstance(value, str) or not value.strip():
        raise SpecError("caption must be a non-empty string")
    if len(value) > CAPTION_MAX:
        raise SpecError(f"caption is {len(value)} characters, the limit is {CAPTION_MAX}")
    if any(d in value for d in DASHES):
        raise SpecError("caption contains an em or en dash: use a hyphen, colon or comma")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SpecError("caption contains a control character")
    return value


def _sound_path(name, where, directory):
    """The WAV for a bare sound name in a folder, or a SpecError."""
    if not isinstance(name, str) or not SOUND_NAME.fullmatch(name):
        raise SpecError(f"{where}: name must be a bare sound name such as 'impact'")
    path = os.path.join(directory, name + ".wav")
    if not os.path.isfile(path):
        raise SpecError(f"{where}: no sound '{name}' (no {name}.wav in {directory})")
    return path


def _non_negative(value, name):
    number = _number(value, name)
    if number < 0:
        raise SpecError(f"{name} must not be negative")
    return number


def _sfx_shaping(raw, where):
    """start, duration, fade_in, fade_out and stop_at_clip_end of an sfx item."""
    duration = raw.get("duration")
    if duration is not None:
        duration = _number(duration, f"{where} duration")
        if duration <= 0:
            raise SpecError(f"{where}: duration must be above zero")
    out = {"start": _non_negative(raw.get("start", 0), f"{where} start"), "duration": duration,
           "fade_in": _non_negative(raw.get("fade_in", 0), f"{where} fade_in"),
           "fade_out": _non_negative(raw.get("fade_out", 0), f"{where} fade_out")}
    stop = raw.get("stop_at_clip_end", False)
    if not isinstance(stop, bool):
        raise SpecError(f"{where}: stop_at_clip_end must be true or false")
    out["stop_at_clip_end"] = stop
    if duration is not None and out["fade_in"] + out["fade_out"] > duration + SOUND_END_TOLERANCE:
        raise SpecError(f"{where}: the fades ({out['fade_in'] + out['fade_out']:g} s) are "
                        f"longer than the duration ({duration:g} s)")
    return out


def _sfx(raw, where, sfx_dir):
    _check_keys(raw, SFX_KEYS, where)
    for key in ("at", "name"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key}")
    at = _number(raw["at"], f"{where} at")
    path = _sound_path(raw["name"], where, sfx_dir)
    gain = _ranged(raw.get("gain_db", 0), f"{where} gain_db", *SFX_GAIN_RANGE)
    return {"at": at, "name": raw["name"], "gain_db": gain, "path": path,
            **_sfx_shaping(raw, where)}


def _named_sound(raw, where, directory, default_gain, gain_range):
    """card_sound and crowd_bed: {"name": ..., "gain_db": ...} looked up in the sound folder."""
    _check_keys(raw, SOUND_KEYS, where)
    if "name" not in raw:
        raise SpecError(f"{where}: missing name")
    path = _sound_path(raw["name"], where, directory)
    gain = _ranged(raw.get("gain_db", default_gain), f"{where} gain_db", *gain_range)
    return {"name": raw["name"], "gain_db": gain, "path": path}


def _card_sound(raw, directory):
    if raw is None:
        return {"name": CARD_HIT[:-4], "gain_db": CARD_HIT_GAIN_DB,
                "path": os.path.join(SFX_DIR, CARD_HIT)}
    return _named_sound(raw, "card_sound", directory, CARD_HIT_GAIN_DB, SFX_GAIN_RANGE)


def _crowd_bed(raw, directory):
    if raw is None:
        return None
    return _named_sound(raw, "crowd_bed", directory, BED_DEFAULT_GAIN, BED_GAIN_RANGE)


def _crowd_stem(raw):
    if raw is None:
        return None
    _check_keys(raw, STEM_KEYS, "crowd_stem")
    if not isinstance(raw.get("path"), str) or not raw["path"]:
        raise SpecError("crowd_stem: path is required")
    gain = _ranged(raw.get("gain_db", STEM_DEFAULT_GAIN), "crowd_stem gain_db", *BED_GAIN_RANGE)
    return {"path": raw["path"], "gain_db": gain}


def _sfx_dir(value, default):
    if value is None:
        return default
    if not isinstance(value, str) or not value or not os.path.isdir(value):
        raise SpecError(f"sfx_dir must be an existing folder (got {value!r})")
    return value


def below_block(top_y):
    """x, y, w, h of the cam block under the game block: BELOW_GAP below it, full width, as high
    as fits above the bottom UI zone (at most BELOW_MAX_HEIGHT), even."""
    y = top_y + TOP_GAME_SIZE[1] + BELOW_GAP
    return 0, y, WIDTH, min(BELOW_MAX_HEIGHT, HEIGHT - BOTTOM_UI_ZONE - y) // 2 * 2


def below_crop(kind, block_h):
    """w, h, default x, y of the facecam crop that fills the cam block when scaled to cover it:
    the facecam picture of the overlay (CAM_GEOMETRY), trimmed to the block's aspect on its
    longer side and centred on the other, every number even."""
    w, h, x, y = CAM_GEOMETRY[kind].inset_crop
    aspect = WIDTH / block_h
    cw, ch = (int(h * aspect) // 2 * 2, h) if w / h > aspect else (w, int(w / aspect) // 2 * 2)
    return cw, ch, x + (w - cw) // 2 // 2 * 2, y + (h - ch) // 2 // 2 * 2


def _below_x(raw, where, kind, top_y):
    """Source x of a below window's crop: even, whole, with the crop inside the facecam picture."""
    cw, _, default, _ = below_crop(kind, below_block(top_y)[3])
    if "x" not in raw:
        return default
    x = _number(raw["x"], f"{where} x")
    if x != int(x) or int(x) % 2:
        raise SpecError(f"{where}: x must be an even whole number (got {x:g})")
    low, high = CAM_GEOMETRY[kind].full_x_range[0], CAM_GEOMETRY[kind].full_x_range[1] - cw
    return int(_ranged(x, f"{where} x", low, high))


def _top_y(value):
    """The y of the top edge of the game block of layout top: even, whole, inside TOP_Y_RANGE."""
    number = _number(value, "top_y")
    low, high = TOP_Y_RANGE
    if number != int(number) or int(number) % 2:
        raise SpecError(f"top_y must be an even whole number of pixels (got {number:g})")
    if not low <= number <= high:
        raise SpecError(f"top_y {number:g} is outside {low} to {high}: lower would put the "
                        "caption under the TikTok top bar, higher would push the facecam block "
                        "into the bottom UI zone")
    return int(number)


def _cam_x(raw, where, layout):
    """Source x of a full window's crop: even, whole, with the crop inside the layout's picture."""
    geometry = CAM_GEOMETRY[layout]
    width, _, default, _ = geometry.full_crop
    if "x" not in raw:
        return default
    x = _number(raw["x"], f"{where} x")
    low, high = geometry.full_x_range[0], geometry.full_x_range[1] - width
    if x != int(x) or int(x) % 2:
        raise SpecError(f"{where}: x must be an even whole number (got {x:g})")
    return int(_ranged(x, f"{where} x", low, high))


def _cam_window(raw, where, length, layout, kind=None, top_y=TOP_Y_DEFAULT):
    _check_keys(raw, CAM_KEYS, where)
    for key in ("start", "end", "mode"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key}")
    start = _non_negative(raw["start"], f"{where} start")
    end = _number(raw["end"], f"{where} end")
    if end > length + SOUND_END_TOLERANCE:
        raise SpecError(f"{where}: end {end:g} is past the {length:.2f} s clip")
    if end - start < MIN_CAM_SECONDS - SOUND_END_TOLERANCE:
        raise SpecError(f"{where}: must be at least {MIN_CAM_SECONDS:g} s long")
    mode = raw["mode"]
    modes = CAM_MODES_TOP if layout == TOP else CAM_MODES
    if not isinstance(mode, str) or mode not in modes:
        raise SpecError(f"{where}: mode must be one of {', '.join(modes)} (got {mode!r})")
    if mode == "inset" and "x" in raw:
        raise SpecError(f"{where}: x is only for full windows")
    x = None
    if mode == "full":
        x = _cam_x(raw, where, layout)
    elif mode == BELOW:
        x = _below_x(raw, where, kind, top_y)
    return {"start": start, "end": end, "mode": mode, "x": x}


def _touches_below(window, previous):
    """True when a below window starts where the previous below window ends: one cam block."""
    return (window["mode"] == BELOW and previous["mode"] == BELOW
            and abs(window["start"] - previous["end"]) < SOUND_END_TOLERANCE)


def _cam_windows(raw, where, length, layout, kind=None, top_y=TOP_Y_DEFAULT):
    """The cam list of a clip: windows in order, none overlapping. Touching below windows are one
    window (one cam block, one fade in); touching windows of other modes are kept apart."""
    if not isinstance(raw, list):
        raise SpecError(f"{where}: cam must be a list")
    windows = []
    for n, item in enumerate(raw, 1):
        window = _cam_window(item, f"{where} cam window {n}", length, layout, kind, top_y)
        if windows and window["start"] < windows[-1]["start"]:
            raise SpecError(f"{where} cam window {n}: windows must be in order")
        if windows and window["start"] < windows[-1]["end"]:
            raise SpecError(f"{where} cam window {n}: overlaps the window before it")
        if windows and _touches_below(window, windows[-1]):
            if window["x"] != windows[-1]["x"]:
                raise SpecError(f"{where} cam window {n}: touches the window before it, so the "
                                "two are one cam block and must share x (got "
                                f"{windows[-1]['x']} and {window['x']})")
            windows[-1] = {**windows[-1], "end": window["end"]}
            continue
        windows.append(window)
    return windows


def scene_clips(spec):
    """(reference, clip) in play order: the hook (if any) first, then the clips by index."""
    hook = spec.get("hook")
    return ([(HOOK, hook)] if hook else []) + list(enumerate(spec["clips"]))


def clip_for(spec, ref):
    """The clip a reference stands for: HOOK, or an index into the clips."""
    return spec["hook"] if ref == HOOK else spec["clips"][ref]


def clip_label(ref):
    """How messages and the timeline name a clip: 'hook', or 'clip 3' for index 2."""
    return HOOK if ref == HOOK else f"clip {ref + 1}"


def clip_layout(clip, default):
    """The layout a clip is drawn with: its own field, else the top level one."""
    return clip.get("layout") or default


def _source_kind(raw, layout, where):
    """The source_kind of a clip of layout top (default broadcast); None for any other layout."""
    if "source_kind" in raw and layout != TOP:
        raise SpecError(f"{where}: source_kind is only for layout {TOP} (got {layout})")
    if layout != TOP:
        return None
    kind = raw.get("source_kind", DEFAULT_SOURCE_KIND)
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise SpecError(f"{where}: source_kind must be one of {', '.join(SOURCE_KINDS)} "
                        f"(got {kind!r})")
    return kind


def _zoom(raw, layout, where):
    """The zoom of a broadcast clip (default 1.25) or of a top clip (default 1.0, anchored at the
    top centre); None for a clip of another layout."""
    if layout == TOP:
        return _ranged(raw.get("zoom", TOP_DEFAULT_ZOOM), f"{where} zoom", *TOP_ZOOM_RANGE)
    if layout != BROADCAST:
        if "zoom" in raw:
            raise SpecError(f"{where}: zoom is only for layout {BROADCAST} or {TOP} "
                            f"(got {layout})")
        return None
    return _ranged(raw.get("zoom", BROADCAST_DEFAULT_ZOOM), f"{where} zoom",
                   *BROADCAST_ZOOM_RANGE)


def _cover(raw, layout, kind, where):
    """Whether the facecam box of a top clip is covered by the mirrored pitch patch (default
    True). Only layout top with a stream source has a facecam to cover."""
    if "cover" not in raw:
        return True
    value = raw["cover"]
    if not isinstance(value, bool):
        raise SpecError(f"{where}: cover must be true or false (got {value!r})")
    if layout != TOP:
        raise SpecError(f"{where}: cover is only for layout {TOP} (got {layout})")
    if kind == DEFAULT_SOURCE_KIND:
        raise SpecError(f"{where}: cover is not for a {DEFAULT_SOURCE_KIND} source "
                        "(it has no facecam to cover)")
    return value


def _check_cam_allowed(raw, layout, where, kind=None):
    if "cam" not in raw:
        return
    if layout == BROADCAST:
        raise SpecError(f"{where}: cam windows are not allowed on a {BROADCAST} clip "
                        "(the source has no facecam)")
    if layout == TOP:
        if kind == DEFAULT_SOURCE_KIND:
            raise SpecError(f"{where}: cam windows are not allowed on a {DEFAULT_SOURCE_KIND} "
                            f"source in layout {TOP} (the source has no facecam)")
        return
    if layout not in CAM_LAYOUTS:
        raise SpecError(f"{where}: cam windows need layout {' or '.join(CAM_LAYOUTS)} "
                        f"(got {layout})")


def _clip(raw, index, sfx_dir, default_layout=DEFAULT_LAYOUT, where=None,
          top_y=TOP_Y_DEFAULT):
    where = where or clip_label(index)
    _check_keys(raw, CLIP_KEYS, where)
    for key in ("source", "start", "end"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key}")
    if not isinstance(raw["source"], str) or not raw["source"]:
        raise SpecError(f"{where}: source must be a path")
    start = _timestamp(raw["start"], f"{where} start")
    end = _timestamp(raw["end"], f"{where} end")
    if end <= start:
        raise SpecError(f"{where}: end must be after start")
    length = frame_snap(end - start)
    if length < MIN_CLIP_SECONDS:
        raise SpecError(f"{where}: must be at least {MIN_CLIP_SECONDS:g} s long")
    if length > MAX_CLIP_SECONDS:
        raise SpecError(f"{where}: at most {MAX_CLIP_SECONDS:g} s long (got {length:.1f})")
    sfx_raw = raw.get("sfx", [])
    if not isinstance(sfx_raw, list):
        raise SpecError(f"{where}: sfx must be a list")
    sfx = [_sfx(item, f"{where} sfx {n + 1}", sfx_dir) for n, item in enumerate(sfx_raw)]
    own = _layout(raw["layout"], f"{where}: layout") if "layout" in raw else None
    layout = own or default_layout
    kind = _source_kind(raw, layout, where)
    cover = _cover(raw, layout, kind, where)
    _check_cam_allowed(raw, layout, where, kind)
    zoom = _zoom(raw, layout, where)
    cam = (_cam_windows(raw.get("cam", []), where, length, layout, kind, top_y)
           if layout != BROADCAST else [])
    clip = {"source": raw["source"], "start_s": start, "end_s": end, "length": length,
            "sfx": sfx, "cam": cam, "layout": own}
    if kind is not None:
        clip["source_kind"] = kind
    if not cover:
        clip["cover"] = False
    return {**clip, "zoom": zoom} if zoom is not None else clip


def _check_clip_sfx(label, clip, offset):
    """Every sfx of one clip, which starts `offset` seconds into the video."""
    for sfx in clip["sfx"]:
        where = f"{label} sfx '{sfx['name']}'"
        if sfx["at"] < -MAX_PRE_ROLL:
            raise SpecError(f"{where}: at {sfx['at']:g} is earlier than -{MAX_PRE_ROLL:.1f}")
        if sfx["at"] >= clip["length"]:
            raise SpecError(f"{where}: at {sfx['at']:g} is not inside the "
                            f"{clip['length']:.2f} s clip")
        if offset + sfx["at"] < 0:
            raise SpecError(f"{where}: at {sfx['at']:g} would begin before the start "
                            "of the video")


def _check_sfx_times(clips, lead, hook=None, card=0.0):
    """Every sfx must sit inside its clip, or begin at most 1 s ahead but not before time 0.

    `lead` is where the first clip starts; with a hook the hook starts at 0 and the first clip
    after the hook and one card.
    """
    offset = lead
    if hook:
        _check_clip_sfx(clip_label(HOOK), hook, 0.0)
        offset = hook["length"] + card
    for i, clip in enumerate(clips):
        _check_clip_sfx(clip_label(i), clip, offset)
        offset += clip["length"]


def _layout(value, where="layout"):
    if not isinstance(value, str) or value not in SPEC_LAYOUTS:
        raise SpecError(f"{where} must be one of {', '.join(SPEC_LAYOUTS)} (got {value!r})")
    return value


def _music(raw):
    if raw is None:
        return None
    _check_keys(raw, MUSIC_KEYS, "music")
    if not isinstance(raw.get("path"), str) or not raw["path"]:
        raise SpecError("music: path is required")
    gain = _ranged(raw.get("gain_db", MUSIC_DEFAULT_GAIN), "music gain_db", *MUSIC_GAIN_RANGE)
    start = _number(raw.get("start", 0), "music start")
    if start < 0:
        raise SpecError("music start must not be negative")
    return {"path": raw["path"], "gain_db": gain, "start": start}


def _hook(raw, sfx_dir, default_layout, top_y=TOP_Y_DEFAULT):
    """The optional hook: a clip object with the clip rules, or None."""
    if raw is None:
        return None
    return _clip(raw, 0, sfx_dir, default_layout, where=HOOK, top_y=top_y)


def _check_output_clear(output, clips, music):
    """The output must not be (a link to) a file the render reads."""
    inputs = [c["source"] for c in clips] + ([music["path"]] if music else [])
    target = os.path.realpath(output)
    for path in inputs:
        if os.path.realpath(path) == target:
            raise SpecError(f"output {output} would overwrite the input {path}")


def validate_spec(raw, sfx_dir=SFX_DIR):
    """Check a spec dict and return a normalised copy; raise SpecError with a clear message."""
    _check_keys(raw, TOP_KEYS, "spec")
    for key in ("caption", "output", "clips"):
        if key not in raw:
            raise SpecError(f"missing required field: {key}")
    output = raw["output"]
    if not isinstance(output, str) or not output.lower().endswith(".mp4"):
        raise SpecError("output must be a path ending in .mp4")
    card = _ranged(raw.get("card_seconds", CARD_DEFAULT), "card_seconds", CARD_MIN, CARD_MAX)
    opening = raw.get("opening_card", True)
    if not isinstance(opening, bool):
        raise SpecError("opening_card must be true or false")
    clips_raw = raw["clips"]
    if not isinstance(clips_raw, list) or not 1 <= len(clips_raw) <= MAX_CLIPS:
        raise SpecError(f"clips must be a list of 1 to {MAX_CLIPS} clips")
    sounds = _sfx_dir(raw.get("sfx_dir"), sfx_dir)
    layout = _layout(raw.get("layout", DEFAULT_LAYOUT))
    top_y = _top_y(raw.get("top_y", TOP_Y_DEFAULT))
    clips = [_clip(c, i, sounds, layout, top_y=top_y) for i, c in enumerate(clips_raw)]
    hook = _hook(raw.get("hook"), sounds, layout, top_y)
    card = frame_snap(card)
    _check_sfx_times(clips, card if opening else 0.0, hook, card)
    music = _music(raw.get("music"))
    _check_output_clear(output, clips + ([hook] if hook else []), music)
    if "card_sound" in raw and raw["card_sound"] is None:
        raise SpecError("card_sound must be an object with a name")
    if raw.get("crowd_stem") is not None and raw.get("crowd_bed") is not None:
        raise SpecError("crowd_stem and crowd_bed cannot be used together: the stem already "
                        "holds the bed")
    comp = raw.get("clip_comp", False)
    if not isinstance(comp, bool):
        raise SpecError("clip_comp must be true or false")
    return {"caption": _caption(raw["caption"]), "output": output,
            "layout": layout, "top_y": top_y, "card_seconds": card,
            "opening_card": opening, "music": music, "clips": clips, "hook": hook,
            "clip_comp": comp,
            "card_sound": _card_sound(raw.get("card_sound"), sounds),
            "crowd_bed": _crowd_bed(raw.get("crowd_bed"), sounds),
            "crowd_stem": _crowd_stem(raw.get("crowd_stem"))}


# ---------------------------------------------------------------- timeline and budget

def _sfx_event(sfx, t, clip_end, index, durations):
    """Timeline entry of a spec sfx, with its trimmed length and effective fade out.

    `trim` is the explicit cut (duration or the clip end) and goes into atrim; `length` is what
    is really played (also limited by the file when its length is known).
    """
    natural = None
    if durations and sfx["path"] in durations:
        natural = max(0.0, durations[sfx["path"]] - sfx["start"])
    limits = [x for x in (sfx["duration"], natural) if x is not None]
    base = min(limits) if limits else None
    cap = clip_end - t if sfx["stop_at_clip_end"] else None
    fade_out = sfx["fade_out"]
    trim, length = sfx["duration"], base
    if cap is not None:
        trim = cap if trim is None else min(trim, cap)
        if base is None or cap < base:
            length, fade_out = cap, max(fade_out, SFX_STOP_FADE)
    return {"t": t, "name": sfx["name"], "gain_db": sfx["gain_db"], "path": sfx["path"],
            "kind": "sfx", "clip": index, "start": sfx["start"], "trim": trim,
            "length": length, "fade_in": sfx["fade_in"], "fade_out": fade_out}


def _bed_offset(index, bed_length):
    offset = index * BED_OFFSET_STEP
    return offset % bed_length if bed_length else offset


def _clip_scene(spec, ref, clip, t, position, durations):
    """Segment, bed (None without a crowd bed), events and cam windows of a clip placed at t.

    `position` counts the clips in play order (the hook, when there is one, is 0) and picks the
    point in the bed file the clip starts at.
    """
    top_layout = spec.get("layout", DEFAULT_LAYOUT)
    segment = {"kind": "clip", "start": t, "duration": clip["length"], "clip": ref}
    own = clip_layout(clip, top_layout)
    if own != top_layout:
        segment["layout"] = own
    bed, beds = spec["crowd_bed"], None
    if bed:
        offset = _bed_offset(position, (durations or {}).get(bed["path"]))
        beds = {"t": t, "length": clip["length"], "clip": ref, "offset": offset,
                "gain_db": bed["gain_db"], "path": bed["path"], "name": bed["name"]}
    events = [_sfx_event(sfx, t + sfx["at"], t + clip["length"], ref, durations)
              for sfx in clip["sfx"]]
    cams = [{"t": t + w["start"], "length": w["end"] - w["start"], "clip": ref,
             "mode": w["mode"], "x": w["x"]} for w in clip.get("cam", ())]
    return segment, beds, events, cams


def build_timeline(spec, durations=None):
    """Absolute times of every card, clip, sound and crowd bed, in seconds from the video start.

    Order: the hook (if the spec has one), a card, then the clips with a card between each pair.
    Without a hook the opening card is there only when `opening_card` is true. With a hook the
    card after it is always there, whatever `opening_card` says: a hook must never run straight
    into the first clip. A segment, event, bed or cam window names its clip in `clip`: the
    index, or "hook".

    `durations` maps a sound file to its length in seconds (probed by check_sources); without it
    the lengths of untrimmed sounds are unknown and bed offsets are not wrapped.
    """
    segments, events, beds, cams = [], [], [], []
    card = spec["card_sound"]
    t = 0.0

    def add_card():
        nonlocal t
        segments.append({"kind": "card", "start": t, "duration": spec["card_seconds"]})
        events.append({"t": t, "name": card["name"], "gain_db": card["gain_db"],
                       "path": card["path"], "kind": "card", "max_len": spec["card_seconds"]})
        t += spec["card_seconds"]

    if not spec.get("hook") and spec["opening_card"]:
        add_card()
    for position, (ref, clip) in enumerate(scene_clips(spec)):
        if position:
            add_card()
        segment, bed, clip_events, clip_cams = _clip_scene(spec, ref, clip, t, position,
                                                           durations)
        segments.append(segment)
        beds += [bed] if bed else []
        events += clip_events
        cams += clip_cams
        t += clip["length"]
    events.sort(key=lambda e: e["t"])
    return {"segments": segments, "events": events, "beds": beds, "cams": cams, "total": t,
            "stem": spec.get("crowd_stem")}


def _stamp(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def format_timeline(timeline):
    lines = []
    for seg in timeline["segments"]:
        label = "card" if seg["kind"] == "card" else clip_label(seg["clip"])
        if "layout" in seg:
            label += f" ({seg['layout']})"
        lines.append(f"{_stamp(seg['start'])}  {label}  {seg['duration']:.3f} s")
    for event in timeline["events"]:
        lines.append(f"{_stamp(event['t'])}  sound {event['name']}  {event['gain_db']:+.1f} dB")
    for bed in timeline.get("beds", []):
        lines.append(f"{_stamp(bed['t'])}  crowd bed {bed['name']} from {bed['offset']:.1f} s  "
                     f"{bed['length']:.3f} s  {bed['gain_db']:+.1f} dB")
    for cam in timeline.get("cams", []):
        x = f" x {cam['x']}" if cam["x"] is not None else ""
        lines.append(f"{_stamp(cam['t'])}  cam {cam['mode']}{x}  {cam['length']:.3f} s")
    stem = timeline.get("stem")
    if stem:
        lines.append(f"{_stamp(0)}  crowd stem {os.path.basename(stem['path'])}  "
                     f"{stem['gain_db']:+.1f} dB")
    lines.sort(key=lambda line: line[:9])
    return lines


def check_budget(total):
    if total < MIN_TOTAL_SECONDS:
        raise SpecError(f"planned length {total:.1f} s is under the {MIN_TOTAL_SECONDS:g} s "
                        "minimum: add clips or lengthen them")
    if total > MAX_TOTAL_SECONDS:
        raise SpecError(f"planned length {total:.1f} s is over the {MAX_TOTAL_SECONDS:g} s "
                        "maximum: shorten or drop clips")


def verify_duration(expected, real):
    if abs(real - expected) > DURATION_TOLERANCE:
        raise ToolError(f"rendered length {real:.2f} s differs from the planned "
                        f"{expected:.2f} s by more than {DURATION_TOLERANCE} s")


# ---------------------------------------------------------------- sources

def probe_source(path):
    """Duration and video size of a file, via ffprobe."""
    out = run_tool(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height:format=duration",
                    "-of", "json", path])
    try:
        data = json.loads(out)
        stream = data["streams"][0]
        return {"duration": float(data["format"]["duration"]),
                "width": int(stream["width"]), "height": int(stream["height"])}
    except (ValueError, KeyError, IndexError) as exc:
        raise ToolError(f"could not read video info of {path}: {exc}")


def probe_wav_duration(path):
    out = run_tool(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", path])
    try:
        return float(out.strip())
    except ValueError:
        raise ToolError(f"could not read the length of {path}")


def sound_durations(spec, wav_duration):
    """Length in seconds of every sfx file and the crowd bed, each probed once."""
    paths = [sfx["path"] for _, clip in scene_clips(spec) for sfx in clip["sfx"]]
    if spec["crowd_bed"]:
        paths.append(spec["crowd_bed"]["path"])
    if spec.get("crowd_stem"):
        paths.append(spec["crowd_stem"]["path"])
    return {path: wav_duration(path) for path in dict.fromkeys(paths)}


def _check_sounds(spec, durations):
    """Every sfx starts inside its file, fits its fades, and ends before the video does."""
    timeline = build_timeline(spec, durations)
    total = timeline["total"]
    for event in (e for e in timeline["events"] if e["kind"] == "sfx"):
        where = f"{clip_label(event['clip'])} sound '{event['name']}'"
        length = event["length"]
        if length is not None and length <= 0:
            raise SpecError(f"{where}: start {event['start']:g} s is past the end of the "
                            f"{durations[event['path']]:.2f} s file")
        fades = event["fade_in"] + event["fade_out"]
        if length is not None and fades > length + SOUND_END_TOLERANCE:
            raise SpecError(f"{where}: the fades ({fades:g} s) are longer than the "
                            f"{length:.2f} s that plays")
        end = event["t"] + (length or 0.0)
        if end > total + SOUND_END_TOLERANCE:
            raise SpecError(f"{where} runs to {end:.2f} s but the video ends at "
                            f"{total:.2f} s: move it earlier, trim it or use a shorter sound")


def _check_stem(spec, durations):
    """The stem must be as long as the planned video, or its crowd drifts off the clips."""
    stem = spec.get("crowd_stem")
    if not stem:
        return
    total = build_timeline(spec, durations)["total"]
    length = durations[stem["path"]]
    if abs(length - total) > STEM_LENGTH_TOLERANCE:
        raise SpecError(f"crowd stem {stem['path']} is {length:.2f} s but the video is planned "
                        f"at {total:.2f} s: rebuild the stem with make_crowd_stem.py")


def broadcast_source_ok(width, height):
    """Whether a broadcast clip can use a source of this size: at least BROADCAST_MIN_SOURCE and
    16:9 within BROADCAST_ASPECT_TOLERANCE. The height test runs first, so the division never
    sees a zero height."""
    min_w, min_h = BROADCAST_MIN_SOURCE
    return (width >= min_w and height >= min_h
            and abs(width / height - 16 / 9) <= 16 / 9 * BROADCAST_ASPECT_TOLERANCE)


def _check_source_size(path, size, layout, kind=None):
    """Stream clips are cropped at fixed pixels, so they need exactly 1920x1080; a broadcast
    clip takes any 16:9 video from 1280x720 up (1914x1080 broadcasts, 1440p, 4K). A top clip
    follows its source_kind."""
    width, height = size
    if layout == TOP:
        layout = BROADCAST if kind in (None, DEFAULT_SOURCE_KIND) else kind
    if layout != BROADCAST:
        if size != (SRC_W, SRC_H):
            raise SpecError(f"{path} is {width}x{height}, the crops need {SRC_W}x{SRC_H}")
        return
    if not broadcast_source_ok(width, height):
        min_w, min_h = BROADCAST_MIN_SOURCE
        raise SpecError(f"{path} is {width}x{height}, a {BROADCAST} clip needs a 16:9 video "
                        f"of at least {min_w}x{min_h}")


def check_sources(spec, probe=probe_source, exists=os.path.isfile, wav_duration=probe_wav_duration):
    """Every source exists, is 1920x1080 and holds its clips; sounds, music and font exist.

    Returns the probed length of every sfx file and the crowd bed (for build_timeline).
    """
    info = {}
    for ref, clip in scene_clips(spec):
        path = clip["source"]
        if path not in info:
            if not exists(path):
                raise SpecError(f"source video not found: {path}")
            info[path] = probe(path)
        size = (info[path]["width"], info[path]["height"])
        _check_source_size(path, size, clip_layout(clip, spec["layout"]), clip.get("source_kind"))
        if clip["end_s"] > info[path]["duration"]:
            raise SpecError(f"{clip_label(ref)} ends at {format_hms(clip['end_s'])}, past the "
                            f"end of {path} ({format_hms(info[path]['duration'])})")
    music = spec["music"]
    if music and not exists(music["path"]):
        raise SpecError(f"music file not found: {music['path']}")
    card_path = spec["card_sound"]["path"]
    if not exists(card_path):
        raise SpecError(f"card sound not found: {card_path} (the built-in one comes from "
                        "sfx/make_sfx.sh)")
    stem = spec.get("crowd_stem")
    if stem and not exists(stem["path"]):
        raise SpecError(f"crowd stem not found: {stem['path']} (build it with "
                        "make_crowd_stem.py)")
    if not exists(FONT_PATH):
        raise SpecError(f"font not found: {FONT_PATH}")
    durations = sound_durations(spec, wav_duration)
    _check_sounds(spec, durations)
    _check_stem(spec, durations)
    return durations


# ---------------------------------------------------------------- ffmpeg command builders

def escape_filter_path(path):
    """Escape a path for a filter option value, through both ffmpeg parsing levels.

    Level 1 reads the option value (backslash, colon and quote are special); level 2 reads the
    filtergraph (backslash, quote, comma, semicolon and brackets are special).
    """
    option_level = re.sub(r"([\\:'])", r"\\\1", path)
    return re.sub(r"([\\',;\[\]])", r"\\\1", option_level)


def write_caption_file(tmp, caption):
    """Spec text only ever reaches ffmpeg through this file, never through a filter string."""
    path = os.path.join(tmp, "caption.txt")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(caption)
    return path


def _frame_edge(seconds):
    """A window edge on the 1/FPS grid, half a frame early, as an expression literal.

    Frame n has t = n / FPS (clip relative, also after an input seek), which a float can leave a
    hair under the literal; half a frame of room makes the comparison immune to that.
    """
    return f"{frame_snap(seconds) - 0.5 / FPS:.4f}"


def _in_windows(windows):
    """Expression that is 1 on the frames of the windows: half open, so touching windows share
    one literal and exactly one of them is on at the boundary frame (`between` has both ends)."""
    return "+".join(f"gte(t,{_frame_edge(w['start'])})*lt(t,{_frame_edge(w['end'])})"
                    for w in windows)


def _drawtext(caption_file, hidden=(), y=CAPTION_Y):
    """The caption at y (an expression); `hidden` are the windows (full facecam) it is switched
    off in."""
    enable = f":enable='not({_in_windows(hidden)})'" if hidden else ""
    return (f"drawtext=fontfile={escape_filter_path(FONT_PATH)}"
            f":textfile={escape_filter_path(caption_file)}:expansion=none"
            f":fontsize={FONT_SIZE}:fontcolor=white:borderw={BORDER_W}:bordercolor=black"
            f":x=(w-text_w)/2:y={y}{enable}")


# The scales leave a sample aspect ratio such as 253:252 (2560:2559 for broadcast) on the picture,
# and the first scene sets the one of the whole concatenated video; the picture is made square.
SQUARE_PIXELS = "setsar=1"

WIDE = ",format=yuv444p"   # kit blocks are stacked in 4:4:4, see _picture_graph


def _strip_chain(tail=""):
    sw, sh, sx, sy = STRIP_CROP
    return (f"[a]crop={sw}:{sh}:{sx}:{sy},scale={STRIP_SIZE[0]}:{STRIP_SIZE[1]}:flags=lanczos"
            f"{tail}[s];")


def _kit_game_chains():
    """Game block with the leftover facecam covered by a mirrored, blurred pitch patch."""
    pw, ph, px, py = KIT_PATCH_SRC
    gw, gh, gx, gy = KIT_GAME_CROP
    return (f"[p]crop={pw}:{ph}:{px}:{py},hflip,boxblur={KIT_PATCH_BLUR}[patch];"
            f"[b][patch]overlay={KIT_PATCH_AT[0]}:{KIT_PATCH_AT[1]},crop={gw}:{gh}:{gx}:{gy},"
            f"scale={KIT_GAME_SIZE[0]}:{KIT_GAME_SIZE[1]}:flags=lanczos{WIDE}[g];")


def _cam_chain():
    cw, ch, cx, cy = CAM_CROP
    return (f"[c]crop={cw}:{ch}:{cx}:{cy},scale={CAM_SIZE[0]}:{CAM_SIZE[1]}:flags=lanczos,"
            f"{CAM_SHARPEN}{WIDE}[cam];")


def _window_chain(k, window, layout):
    """Crop, scale and sharpen of facecam window k, from its own copy of the source [wk]."""
    geometry = CAM_GEOMETRY[layout]
    if window["mode"] == "full":
        cw, ch, _, cy = geometry.full_crop
        crop, size = (cw, ch, window["x"], cy), geometry.full_size
    else:
        crop, size = geometry.inset_crop, geometry.inset_size
    return (f"[w{k}]crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]},"
            f"scale={size[0]}:{size[1]}:flags=lanczos,{CAM_SHARPEN}{WIDE}[cam{k}];")


def _window_chains(cam, layout):
    return "".join(_window_chain(k, w, layout) for k, w in enumerate(cam))


def _window_overlays(cam, layout):
    """The composed picture [st] with every window laid over it, cut in and out at hard edges.

    The overlays keep format=yuv444 (the default would subsample the picture to 4:2:0 here).
    The last overlay has no output label, so the caption filter continues its chain.
    """
    if not cam:
        return ""
    geometry = CAM_GEOMETRY[layout]
    parts, previous = [], "st"
    for k, window in enumerate(cam):
        x, y = (0, geometry.game_y) if window["mode"] == "full" else geometry.inset_at
        label = "" if k == len(cam) - 1 else f"[o{k}]"
        parts.append(f"[{previous}][cam{k}]overlay={x}:{y}:format=yuv444:"
                     f"enable='{_in_windows([window])}'{label}")
        previous = f"o{k}"
    return "[st];" + ";".join(parts)


def _bottom_cam_graph(pad, cam):
    """The bottom-cam picture. Without windows it is stacked as yuv420p, as it always was; with
    windows both blocks are 4:4:4 like kit's, because the strip is 99 px tall (odd) and its
    chroma would bleed into the game block under it, then the overlays keep 4:4:4 too."""
    gw, gh, gx, gy = GAME_CROP
    wide = WIDE if cam else ""
    outs = "".join(f"[w{k}]" for k in range(len(cam)))
    return (f"[0:v]fps={FPS},split={2 + len(cam)}[a][b]{outs};" + _strip_chain(wide) +
            f"[b]crop={gw}:{gh}:{gx}:{gy},scale={GAME_SIZE[0]}:{GAME_SIZE[1]}:flags=lanczos"
            f"{wide}[g];" + _window_chains(cam, "bottom-cam") +
            f"[s][g]vstack=inputs=2,{pad}" + _window_overlays(cam, "bottom-cam"))


def _cover_background():
    """The picture [k] scaled to cover the frame at a sixteenth of the pixels, blurred, darkened
    and scaled up to the frame: [bg]. Shared by the broadcast and top layouts."""
    bw, bh = BROADCAST_BG_SIZE
    return (f"[k]scale={bw}:{bh}:force_original_aspect_ratio=increase:flags=bilinear,"
            f"crop={bw}:{bh},format=yuv420p,gblur=sigma={BROADCAST_BG_BLUR},"
            f"eq=brightness={BROADCAST_BG_BRIGHTNESS:g}"
            f":saturation={BROADCAST_BG_SATURATION:g},"
            f"scale={WIDTH}:{HEIGHT}:flags=bicubic[bg];")


def _broadcast_graph(zoom):
    """The whole 16:9 frame, centre cropped by `zoom`, over a blurred, darkened copy of itself.

    fps starts at time 0 (start_time=0): a 30 or 59.94 fps source whose first frame is a hair
    after the seek point is padded back to the grid, so the scene has exactly length * FPS
    frames, as the other layouts do. The crop is given as expressions of the input size, so any
    16:9 source works; it is kept even (yuv420p) and centred (the crop default).
    """
    fw, fh = BROADCAST_SIZE
    z = f"{zoom:g}"
    background = _cover_background()
    picture = (f"[m]crop=trunc(iw/{z}/2)*2:trunc(ih/{z}/2)*2,"
               f"scale={fw}:{fh}:flags=lanczos[fg];")
    return (f"[0:v]fps={FPS}:start_time=0,split=2[m][k];" + background + picture +
            f"[bg][fg]overlay={BROADCAST_AT[0]}:{BROADCAST_AT[1]}")


def _top_base(source, patched=True):
    """Chains from the split source [b] (and the donor [p]) to the game picture, split into the
    picture [m] and the background source [k]: the facecam box covered by a blurred mirror of the
    pitch beside it (only when `patched`), then the game region of the overlay. A broadcast has
    neither."""
    if source is None:
        return "[b]split=2[m][k];"
    crop = "crop={}:{}:{}:{},".format(*source.base_crop) if source.base_crop else ""
    if not patched:
        return f"[b]{crop}split=2[m][k];"
    cw, ch, cx, cy = source.cover
    return (f"[p]crop={cw}:{ch}:{source.donor_x}:{cy},hflip,boxblur={TOP_COVER_BLUR}[patch];"
            f"[b][patch]overlay={cx}:{cy},{crop}split=2[m][k];")


def _below_chain(k, window, kind, block_h):
    """Facecam crop of window k scaled to the cam block, sharpened, fading in over BELOW_FADE_IN."""
    cw, ch, _, cy = below_crop(kind, block_h)
    return (f"[w{k}]crop={cw}:{ch}:{window['x']}:{cy},scale={WIDTH}:{block_h}:flags=lanczos,"
            f"{CAM_SHARPEN},format=yuva420p,"
            f"fade=t=in:st={frame_snap(window['start']):.4f}:d={BELOW_FADE_IN:g}:alpha=1[cam{k}];")


def _top_graph(kind, zoom, top_y, cam, cover=True):
    """The game picture at full width with its top edge at top_y over the blurred cover of
    itself, and the facecam block under it during the cam windows (never replacing the game).

    The picture is cropped by `zoom` anchored at the top centre (the scoreline stays). Every cam
    crop is cut from its own copy of the source [wk], taken before the facecam box is covered.
    With `cover` false the facecam box is left as it is in the game picture.
    """
    if cam and kind == DEFAULT_SOURCE_KIND:
        raise ToolError(f"cam windows are not allowed on a {DEFAULT_SOURCE_KIND} source")
    source = TOP_SOURCES.get(kind)
    patched = source is not None and cover
    outs = "[b]" + ("[p]" if patched else "") + "".join(f"[w{k}]" for k in range(len(cam)))
    count = outs.count("[")
    head = f"[0:v]fps={FPS}:start_time=0" + (f",split={count}{outs};" if count > 1 else "[b];")
    fw, fh = TOP_GAME_SIZE
    z = f"{zoom:g}"
    picture = (f"[m]crop=trunc(iw/{z}/2)*2:trunc(ih/{z}/2)*2:trunc((iw-ow)/4)*2:0,"
               f"scale={fw}:{fh}:flags=lanczos[fg];")
    graph = head + _top_base(source, patched) + _cover_background() + picture
    if not cam:
        return graph + f"[bg][fg]overlay=0:{top_y}"
    _, block_y, _, block_h = below_block(top_y)
    graph += "".join(_below_chain(k, w, kind, block_h) for k, w in enumerate(cam))
    graph += f"[bg][fg]overlay=0:{top_y}[st];"
    parts, previous = [], "st"
    for k, window in enumerate(cam):
        label = "" if k == len(cam) - 1 else f"[o{k}]"
        parts.append(f"[{previous}][cam{k}]overlay=0:{block_y}:format=auto:"
                     f"enable='{_in_windows([window])}'{label}")
        previous = f"o{k}"
    return graph + ";".join(parts)


def _picture_graph(layout, cam=(), zoom=BROADCAST_DEFAULT_ZOOM, top_y=TOP_Y_DEFAULT,
                   source_kind=DEFAULT_SOURCE_KIND, cover=True):
    """Filter chains up to and including the stack and pad (the caption is added after).

    The kit blocks have odd heights (99, 1087), so stacked as yuv420p the chroma of one block
    bleeds into the next (a blue line on top of the facecam). They are stacked as yuv444p
    instead; the final format=yuv420p subsamples the finished picture once. Bottom-cam does the
    same when it has facecam windows (its strip is 99 px too).
    """
    if layout == BROADCAST:
        if cam:
            raise ToolError(f"cam windows are not allowed on a {BROADCAST} clip")
        return _broadcast_graph(zoom)
    if layout == TOP:
        return _top_graph(source_kind, zoom, top_y, cam, cover)
    pad = f"pad={WIDTH}:{HEIGHT}:0:{PAD_Y[layout]}:black"
    if cam and layout not in CAM_LAYOUTS:
        raise ToolError(f"cam windows need layout {' or '.join(CAM_LAYOUTS)} (got {layout})")
    if layout == "bottom-cam":
        return _bottom_cam_graph(pad, cam)
    if layout == "kit":
        outs = "".join(f"[w{k}]" for k in range(len(cam)))
        return (f"[0:v]fps={FPS},split={3 + len(cam)}[a][b][p]{outs};" + _kit_game_chains()
                + _strip_chain(WIDE) + _window_chains(cam, "kit")
                + f"[s][g]vstack=inputs=2,{pad}" + _window_overlays(cam, "kit"))
    return (f"[0:v]fps={FPS},split=4[a][b][p][c];" + _kit_game_chains() + _strip_chain(WIDE) +
            _cam_chain() + f"[s][g][cam]vstack=inputs=3,{pad}")


def _picture_chain(caption_file, layout=DEFAULT_LAYOUT, cam=(), zoom=BROADCAST_DEFAULT_ZOOM,
                   top_y=TOP_Y_DEFAULT, source_kind=DEFAULT_SOURCE_KIND, cover=True):
    hidden = [w for w in cam if w["mode"] == "full"]
    # Layout top draws the caption above the game block: its baseline TOP_CAPTION_GAP above top_y.
    caption_y = f"{top_y - TOP_CAPTION_GAP}-ascent" if layout == TOP else CAPTION_Y
    return (f"{_picture_graph(layout, cam, zoom, top_y, source_kind, cover)},{SQUARE_PIXELS},"
            f"{_drawtext(caption_file, hidden, caption_y)},format=yuv420p[v]")


def clip_command(clip, caption_file, out_path, layout=DEFAULT_LAYOUT, clip_comp=False,
                 top_y=TOP_Y_DEFAULT):
    length = clip["length"]
    layout = clip_layout(clip, layout)
    zoom = clip.get("zoom", TOP_DEFAULT_ZOOM if layout == TOP else BROADCAST_DEFAULT_ZOOM)
    comp = f"{CLIP_COMP}," if clip_comp else ""
    audio = (f"[0:a]{STEREO_48K},{comp}"
             f"afade=t=in:d={CLIP_FADE_IN},"
             f"afade=t=out:st={length - CLIP_FADE_OUT:.3f}:d={CLIP_FADE_OUT},"
             f"apad=whole_dur={length:.3f}[a]")
    return (FFMPEG + ["-threads", THREADS, "-ss", f"{clip['start_s']:.3f}", "-i", clip["source"],
                      "-t", f"{length:.3f}",
                      "-filter_complex",
                      _picture_chain(caption_file, layout, clip.get("cam", ()), zoom, top_y,
                                     clip.get("source_kind", DEFAULT_SOURCE_KIND),
                                     clip.get("cover", True))
                      + ";" + audio,
                      "-map", "[v]", "-map", "[a]"]
            + ENCODE + PCM + ["-threads", THREADS, out_path])


def card_command(duration, caption_file, out_path):
    return (FFMPEG + ["-f", "lavfi", "-i", f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}",
                      "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                      "-t", f"{duration:.3f}",
                      "-filter_complex", f"[0:v]{_drawtext(caption_file)},format=yuv420p[v]",
                      "-map", "[v]", "-map", "1:a"]
            + ENCODE + PCM + ["-threads", THREADS, out_path])


def concat_list(paths):
    return "".join("file '" + p.replace("'", "'\\''") + "'\n" for p in paths)


def concat_command(list_path, out_path):
    return FFMPEG + ["-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path]


def _music_chain(index, music, total):
    """Music trimmed to the video, ducked by the clip audio, fading out over the last second."""
    return [f"[{index}:a]{STEREO_48K},"
            f"atrim=start={music['start']:.3f},asetpts=PTS-STARTPTS,atrim=end={total:.3f},"
            f"volume={music['gain_db']:.1f}dB[mraw]",
            f"[mraw][key]{DUCK}[mduck]",
            f"[mduck]afade=t=out:st={total - MUSIC_FADE_OUT:.3f}:d={MUSIC_FADE_OUT:g}[mus]"]


def _card_trim(event):
    """The card sound is cut to its card (with a short fade) so it never runs into the clip."""
    limit = event.get("max_len")
    if limit is None:
        return ""
    return (f"atrim=end={limit:.3f},afade=t=out:st={limit - CARD_SOUND_FADE:.3f}"
            f":d={CARD_SOUND_FADE},")


def _sfx_shape(event):
    """atrim, then the fades, of an sfx: all of it happens before the delay."""
    trim = [f"start={event['start']:.3f}"] if event.get("start", 0) > 0 else []
    if event.get("trim") is not None:
        trim.append(f"duration={event['trim']:.3f}")
    parts = [f"atrim={':'.join(trim)},asetpts=PTS-STARTPTS"] if trim else []
    if event.get("fade_in", 0) > 0:
        parts.append(f"afade=t=in:d={event['fade_in']:g}")
    fade_out = event.get("fade_out", 0)
    if fade_out > 0:
        if event.get("length") is None:
            raise ToolError(f"cannot fade out '{event['name']}': its length is not known "
                            "(probe the sound files first)")
        parts.append(f"afade=t=out:st={max(0.0, event['length'] - fade_out):.3f}:d={fade_out:g}")
    return "".join(part + "," for part in parts)


def _event_chain(n, event):
    shaping = _card_trim(event) if event["kind"] == "card" else _sfx_shape(event)
    return (f"[{n}:a]{STEREO_48K},{shaping}volume={event['gain_db']:.1f}dB,"
            f"adelay={round(event['t'] * 1000)}:all=1[e{n}]")


def _bed_chains(index, beds):
    """One looped bed input, split per clip: trimmed from its own start point, faded, delayed."""
    names = [f"bd{k}" for k in range(len(beds))]
    chains = [f"[{index}:a]{STEREO_48K},asplit={len(beds)}" + "".join(f"[{n}]" for n in names)]
    for name, bed in zip(names, beds):
        out = bed["length"]
        chains.append(f"[{name}]atrim=start={bed['offset']:.3f}:duration={out:.3f},"
                      f"asetpts=PTS-STARTPTS,afade=t=in:d={BED_FADE_IN:g},"
                      f"afade=t=out:st={out - CLIP_FADE_OUT:.3f}:d={CLIP_FADE_OUT},"
                      f"volume={bed['gain_db']:.1f}dB,adelay={round(bed['t'] * 1000)}:all=1"
                      f"[cb{name[2:]}]")
    return chains, [f"[cb{name[2:]}]" for name in names]


def loudnorm_filter(measured=None):
    """Single-pass loudnorm, or the linear second pass when the first pass measured it."""
    if not measured:
        return LOUDNORM
    extra = ":".join(f"{name}={measured[key]:.2f}" for key, name in MEASURED_KEYS)
    return f"{LOUDNORM}:{extra}:linear=true:print_format=json"


def _mix_graph(concat_path, timeline, music, measured):
    """Input arguments and filter graph: concat audio plus every card sound and effect."""
    events, beds, total = timeline["events"], timeline.get("beds", []), timeline["total"]
    inputs = ["-i", concat_path]
    chains = []
    for n, event in enumerate(events, 1):
        inputs += ["-i", event["path"]]
        chains.append(_event_chain(n, event))
    labels = [f"[e{n}]" for n in range(1, len(events) + 1)]
    next_input = len(events) + 1
    if beds:
        inputs += ["-stream_loop", "-1", "-i", beds[0]["path"]]
        bed_chains, bed_labels = _bed_chains(next_input, beds)
        chains += bed_chains
        labels += bed_labels
        next_input += 1
    stem = timeline.get("stem")
    if stem:
        inputs += ["-i", stem["path"]]
        chains.append(f"[{next_input}:a]{STEREO_48K},volume={stem['gain_db']:.1f}dB[cs]")
        labels.append("[cs]")
        next_input += 1
    if music:
        inputs += ["-stream_loop", "-1", "-i", music["path"]]
        chains.append("[0:a]asplit=2[base][key]")
        chains += _music_chain(next_input, music, total)
        mix_in = ["[base]", "[mus]"] + labels
    else:
        mix_in = ["[0:a]"] + labels
    chains.append("".join(mix_in) + f"amix=inputs={len(mix_in)}:normalize=0:duration=first"
                  f":dropout_transition=0,{LIMITER},{loudnorm_filter(measured)}"
                  ",aresample=48000[aout]")
    return inputs, ";".join(chains)


def mix_command(concat_path, timeline, music, out_path, measured=None):
    """The audio pass: mix everything over the concat audio, loudnorm, encode AAC."""
    inputs, graph = _mix_graph(concat_path, timeline, music, measured)
    base = FFMPEG if not measured else FFMPEG[:3] + ["-loglevel", "info"] + FFMPEG[5:]
    return (base + inputs + ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]",
                               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                               "-movflags", "+faststart",
                               "-t", f"{timeline['total']:.3f}", out_path])


def measure_command(concat_path, timeline, music):
    """First loudnorm pass: same mix, measured and discarded (the JSON lands on stderr)."""
    inputs, graph = _mix_graph(concat_path, timeline, music, None)
    graph = graph.replace(LOUDNORM, LOUDNORM + ":print_format=json")
    base = FFMPEG[:3] + ["-loglevel", "info"] + FFMPEG[5:]
    return base + inputs + ["-filter_complex", graph, "-map", "[aout]",
                            "-t", f"{timeline['total']:.3f}", "-f", "null", "-"]


def parse_loudnorm(stderr):
    """Read the JSON block loudnorm prints; every number must be finite."""
    start, end = stderr.rfind("{"), stderr.rfind("}")
    try:
        raw = json.loads(stderr[start:end + 1])
        values = {key: float(raw[key]) for key, _ in MEASURED_KEYS}
    except (ValueError, KeyError):
        raise ToolError("could not read the loudnorm measurement from ffmpeg")
    if any(v != v or abs(v) == float("inf") for v in values.values()):
        raise ToolError("the mixed audio is silent or unmeasurable (loudnorm gave inf/nan)")
    return values


def parse_normalization_type(stderr):
    """'linear' or 'dynamic' from the loudnorm report, 'unknown' if it is not there."""
    try:
        return str(json.loads(stderr[stderr.rfind("{"):stderr.rfind("}") + 1])
                   ["normalization_type"])
    except (ValueError, KeyError):
        return "unknown"


def run_stderr(cmd):
    """Run a command and return its stderr; raise ToolError on a non-zero exit."""
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-5:]
        raise ToolError(f"{cmd[0]} failed ({proc.returncode}): " + " | ".join(tail))
    return proc.stderr


# ---------------------------------------------------------------- rendering

def _scene_commands(spec, timeline, tmp):
    caption_file = write_caption_file(tmp, spec["caption"])
    for n, seg in enumerate(timeline["segments"]):
        path = os.path.join(tmp, f"scene{n:02d}.mov")
        if seg["kind"] == "card":
            yield path, card_command(seg["duration"], caption_file, path)
        else:
            clip = clip_for(spec, seg["clip"])
            yield path, clip_command(clip, caption_file, path, spec["layout"], spec["clip_comp"],
                                     spec.get("top_y", TOP_Y_DEFAULT))


def render(spec, keep_temp=False, runner=run_tool, probe_output=None, measurer=run_stderr,
           durations=None):
    """Encode each card and clip, stream-copy concat, mix all audio (two loudnorm passes)."""
    probe_output = probe_output or (lambda p: probe_source(p)["duration"])
    out = os.path.abspath(spec["output"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    timeline = build_timeline(spec, durations)
    tmp = tempfile.mkdtemp(prefix="render_tiktok_")
    partial = out + ".part.mp4"
    try:
        scenes = []
        for n, (path, cmd) in enumerate(_scene_commands(spec, timeline, tmp), 1):
            log(f"scene {n} of {len(timeline['segments'])}")
            runner(cmd)
            scenes.append(path)
        list_path = os.path.join(tmp, "scenes.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            handle.write(concat_list(scenes))
        joined = os.path.join(tmp, "joined.mov")
        runner(concat_command(list_path, joined))
        log("measuring loudness, then mixing audio")
        measured = parse_loudnorm(measurer(measure_command(joined, timeline, spec["music"])))
        report = measurer(mix_command(joined, timeline, spec["music"], partial, measured))
        norm_type = parse_normalization_type(report)
        log(f"loudnorm second pass: {norm_type}")
        if norm_type != "linear":
            log(f"warning: loudnorm was {norm_type}, not linear: the loudness may miss "
                "the -14 LUFS target")
        real = probe_output(partial)
        try:
            verify_duration(timeline["total"], real)
        except ToolError:
            os.remove(partial)
            raise
        os.replace(partial, out)
        return out, real, norm_type
    finally:
        if os.path.exists(partial):
            os.remove(partial)
        if keep_temp:
            log(f"kept temp dir {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- command line

def load_spec(path):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise SpecError(f"cannot read spec {path}: {exc.strerror}")
    except ValueError as exc:
        raise SpecError(f"spec {path} is not valid JSON: {exc}")
    return validate_spec(raw)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Render a vertical TikTok from a JSON spec.")
    parser.add_argument("spec", help="path to the spec JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the spec and print the timeline, render nothing")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the temp dir with the intermediates")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        spec = load_spec(args.spec)
        timeline = build_timeline(spec)
        log(f"planned length {timeline['total']:.2f} s "
            f"(allowed {MIN_TOTAL_SECONDS:g} to {MAX_TOTAL_SECONDS:g} s)")
        check_budget(timeline["total"])
        require_tools()
        durations = check_sources(spec)
        print("\n".join(format_timeline(build_timeline(spec, durations))))
        if args.dry_run:
            return 0
        out, real, norm_type = render(spec, args.keep_temp, durations=durations)
    except (ToolError, OSError) as exc:
        log(f"error: {exc}")
        return 1
    print(f"{out}  {real:.2f} s  {os.path.getsize(out) / 1e6:.1f} MB  loudnorm {norm_type}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
