"""Spec validation, graphic table and timeline math for build_intro.py (pure, no I/O)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

sys.path.insert(0, os.path.dirname(HERE))  # tools/brand.py

import brand as brand_config  # noqa: E402
import intro_sfx  # noqa: E402
from render_short import SpecError, parse_timestamp  # noqa: E402,F401  (clips/ is on the path via chrome_frames)

FPS = 60
MIN_TOTAL, MAX_TOTAL = 15.0, 30.0
MIN_SHOTS, MAX_SHOTS = 1, 12
MIN_SHOT_SECONDS, MAX_SHOT_SECONDS = 0.4, 12.0
MIN_SPEED, MAX_SPEED = 0.25, 2.0
WHOOSH_LEAD = 0.06           # a whoosh starts this long before its cut
DASHES = ("\u2014", "\u2013")
TEXT_LIMITS = {"title": 22, "subtitle": 28, "opponent": 28, "home_team": 28, "channel": 24}
MATCHDAY_MAX_CHARS = 8
PROGRAM_VIDEOS = os.path.join("~", "Videos", "rl-broadcast-kit")

TOP_KEYS = {"output", "title", "subtitle", "matchday", "opponent", "home_team", "channel", "sponsor_logo",
            "shots", "music"}
REQUIRED_KEYS = ("title", "subtitle", "matchday", "opponent", "shots")
SHOT_KEYS = {"source", "start", "end", "speed", "graphic"}

# Overlay graphics. `duration` is the animation length in seconds (a shot must last at least that
# long). `active` lists the ranges in which something moves, so every other frame is a copy of the
# previous one (see chrome_frames.plan_frames). `sfx` is (offset in graphic, effect kind).
# Keep `active` in step with the timeline comment at the top of each html file.
GRAPHICS = {
    "presents": {"page": "presents.html", "duration": 2.4, "active": [(0.0, 0.9), (1.85, 2.4)],
                 "sfx": [(0.1, "whoosh")]},
    "wordmark": {"page": "wordmark.html", "duration": 3.0, "active": [(0.0, 1.1), (2.5, 3.0)],
                 "sfx": [(0.45, "boom")]},
    "sponsor": {"page": "sponsor.html", "duration": 2.4, "active": [(0.0, 0.85), (1.9, 2.4)],
                "sfx": [(0.1, "whoosh")]},
    "matchup": {"page": "matchup.html", "duration": 3.0, "active": [(0.0, 0.8), (2.4, 3.0)],
                "sfx": [(0.05, "whoosh"), (0.55, "hit")]},
    "lockup": {"page": "lockup.html", "duration": 3.4, "active": [(0.0, 1.65)],
               "sfx": [(0.3, "boom")]},
}
GRAPHIC_IDS = tuple(GRAPHICS)


# ---------------------------------------------------------------- field helpers

def _text(value, name, limit, required=True):
    if not isinstance(value, str) or (required and not value.strip()):
        raise SpecError(f"{name} must be a non-empty string")
    if len(value.strip()) > limit:
        raise SpecError(f"{name} is {len(value.strip())} characters, the limit is {limit}")
    if any(d in value for d in DASHES):
        raise SpecError(f"{name} contains an em or en dash: use a hyphen, colon or comma")
    return value.strip()


def _matchday(value):
    if isinstance(value, bool):
        raise SpecError("matchday must be a whole number from 1 to 99 or a short word")
    if isinstance(value, int):
        if not 1 <= value <= 99:
            raise SpecError("matchday must be a whole number from 1 to 99 or a short word")
        return str(value)
    return _text(value, "matchday", MATCHDAY_MAX_CHARS)


def _path(value, name, base_dir):
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{name} must be a path string")
    path = os.path.expanduser(value.strip())
    if not os.path.isabs(path):
        path = os.path.join(base_dir or os.getcwd(), path)
    return os.path.normpath(path)


def _output(value, base_dir):
    folder = os.path.expanduser(PROGRAM_VIDEOS)
    if value is None:
        return os.path.join(folder, "intro.mp4")
    if not isinstance(value, str) or not value.lower().endswith(".mp4"):
        raise SpecError("output must be a path ending in .mp4")
    if os.path.dirname(value) == "":
        return os.path.join(folder, value)
    return _path(value, "output", base_dir)


# ---------------------------------------------------------------- shots

def shot_frames(source_seconds, speed):
    """Output frames of a shot cut `source_seconds` long and played at `speed`."""
    return int(round(source_seconds / speed * FPS))


def _speed(raw, where):
    value = raw.get("speed", 1)
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not MIN_SPEED <= value <= MAX_SPEED:
        raise SpecError(f"{where}: speed must be a number from {MIN_SPEED:g} to {MAX_SPEED:g}")
    return float(value)


def _shot(raw, index, base_dir):
    where = f"shot {index + 1}"
    if not isinstance(raw, dict):
        raise SpecError(f"{where} must be an object")
    unknown = set(raw) - SHOT_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    for key in ("source", "start", "end"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key}")
    start, end = parse_timestamp(raw["start"]), parse_timestamp(raw["end"])
    if end <= start:
        raise SpecError(f"{where}: end must be after start")
    speed = _speed(raw, where)
    graphic = raw.get("graphic")
    if graphic is not None and graphic not in GRAPHICS:
        raise SpecError(f"{where}: unknown graphic {graphic!r}, use one of "
                        f"{', '.join(GRAPHIC_IDS)} or null")
    frames = shot_frames(end - start, speed)
    seconds = frames / FPS
    if not MIN_SHOT_SECONDS <= seconds <= MAX_SHOT_SECONDS:
        raise SpecError(f"{where}: plays for {seconds:.2f} s, it must be {MIN_SHOT_SECONDS:g} to "
                        f"{MAX_SHOT_SECONDS:g} s after speed")
    if graphic and seconds + 1e-9 < GRAPHICS[graphic]["duration"]:
        raise SpecError(f"{where}: graphic '{graphic}' needs {GRAPHICS[graphic]['duration']:.1f} s "
                        f"but the shot plays for {seconds:.2f} s")
    return {"source": _path(raw["source"], f"{where} source", base_dir), "start_s": start,
            "end_s": end, "speed": speed, "graphic": graphic, "frames": frames}


def _check_graphics(shots):
    used = [s["graphic"] for s in shots if s["graphic"]]
    for gid in set(used):
        if used.count(gid) > 1:
            raise SpecError(f"graphic '{gid}' is used on more than one shot: each graphic goes on one shot once")
    if "lockup" in used and shots[-1]["graphic"] != "lockup":
        raise SpecError("graphic 'lockup' must be on the last shot (the intro fades to black after it)")


# ---------------------------------------------------------------- budget and timeline

def check_budget(total):
    if not MIN_TOTAL <= round(total, 6) <= MAX_TOTAL:
        raise SpecError(f"intro is {total:.1f} s long, it must be between {MIN_TOTAL:g} and "
                        f"{MAX_TOTAL:g} s: lengthen or shorten shots")


def timeline(spec):
    """Per shot: first output frame and frame count."""
    line, cursor = [], 0
    for shot in spec["shots"]:
        line.append({"start_frame": cursor, "frames": shot["frames"]})
        cursor += shot["frames"]
    return line


def total_frames(spec):
    return sum(shot["frames"] for shot in spec["shots"])


def total_duration(spec):
    return total_frames(spec) / FPS


def sfx_events(spec):
    """Sorted (time, kind) sound effects: whoosh on cuts, graphic hits, riser into the lockup."""
    events = []
    for index, (shot, slot) in enumerate(zip(spec["shots"], timeline(spec))):
        start = slot["start_frame"] / FPS
        if index > 0:
            events.append((max(0.0, start - WHOOSH_LEAD), "whoosh"))
        if shot["graphic"]:
            events.extend((start + off, kind) for off, kind in GRAPHICS[shot["graphic"]]["sfx"])
        if shot["graphic"] == "lockup":
            events.append((max(0.0, start - intro_sfx.RISER_SECONDS), "riser"))
    return sorted(events)


# ---------------------------------------------------------------- spec

def validate_spec(raw, base_dir=None, brand=None):
    """Check a spec dict and return a normalised copy; raise SpecError with a clear message.

    `brand` (see tools/brand.py) supplies the defaults for channel, home_team and sponsor_logo;
    without it the generic placeholders are used and there is no sponsor.
    """
    brand = brand or brand_config.defaults()
    if not isinstance(raw, dict):
        raise SpecError("spec must be a JSON object")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise SpecError(f"unknown field(s) {', '.join(sorted(unknown))}")
    for key in REQUIRED_KEYS:
        if key not in raw:
            raise SpecError(f"missing required field: {key}")
    shots_raw = raw["shots"]
    if not isinstance(shots_raw, list) or not MIN_SHOTS <= len(shots_raw) <= MAX_SHOTS:
        raise SpecError(f"shots must be a list of {MIN_SHOTS} to {MAX_SHOTS} shots")
    shots = [_shot(s, i, base_dir) for i, s in enumerate(shots_raw)]
    _check_graphics(shots)
    music = raw.get("music")
    if music is not None:
        music = _path(music, "music", base_dir)
    logo = raw.get("sponsor_logo", brand.get("sponsor_logo"))
    logo = _path(logo, "sponsor_logo", base_dir) if logo else None
    if logo is None and any(s["graphic"] == "sponsor" for s in shots):
        raise SpecError("graphic 'sponsor' needs a sponsor_logo (in the spec, or sponsor in config.js)")
    spec = {
        "output": _output(raw.get("output"), base_dir),
        "title": _text(raw["title"], "title", TEXT_LIMITS["title"]),
        "subtitle": _text(raw["subtitle"], "subtitle", TEXT_LIMITS["subtitle"]),
        "matchday": _matchday(raw["matchday"]),
        "opponent": _text(raw["opponent"], "opponent", TEXT_LIMITS["opponent"]),
        "home_team": _text(raw.get("home_team", brand["home_team"]), "home_team", TEXT_LIMITS["home_team"]),
        "channel": _text(raw.get("channel", brand["channel"]), "channel", TEXT_LIMITS["channel"]),
        "sponsor_logo": logo,
        "music": music, "shots": shots,
    }
    check_budget(total_duration(spec))
    return spec
