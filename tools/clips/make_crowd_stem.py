#!/usr/bin/env python3
"""Build the crowd stem of a TikTok spec: one WAV, as long as the whole video, for crowd_stem.

Needs numpy, which only lives in the user site here, so run it WITHOUT python's -I:
    python3 make_crowd_stem.py PLAN.json
Plus ffmpeg/ffprobe on PATH (audio is decoded and encoded through ffmpeg pipes as 48 kHz stereo
float32). See README.md for the plan format.

Per clip of the render spec (the hook first, when it has one; cards stay digital silence) the
stem holds a looped crowd bed that rises with the commentary, a swell and a one-shot crowd sound
at each moment (a goal or save), all run through one synthetic room. The same seed gives the
same file. The hook is referred to as "hook" where a clip is referred to by index.
"""
import argparse
import json
import math
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I is not used, but be safe

import render_tiktok as rt  # noqa: E402
from find_candidates import ToolError, log, require_tools  # noqa: E402
from render_short import SpecError  # noqa: E402

RATE = 48000
EDGE_FADE = 0.04                # fade at both edges of every clip (the reverb tail is cut here)
CUT_FADE = 0.08                 # fade of a moment's sound where the clip end cuts it
SOUND_END_FADE = 0.02           # fade of a moment's sound where its file runs out
LOOP_CROSSFADE = 1.0            # equal-power crossfade at the seam of the looped bed, seconds
MIN_SOUND_SECONDS = 0.1
HOLD = 0.3                      # seconds the swell and the sound stay full after the moment
RESAMPLE_SPREAD = 0.03          # each moment is resampled by a factor within plus or minus this
# Excitement of the commentary: band RMS in dB, smoothed, relative to the clip's median.
EXCITE_BAND = (300.0, 3000.0)
EXCITE_ORDER = 4                # steepness of the band edges (second order is 2)
EXCITE_HOP = 0.01
EXCITE_ATTACK, EXCITE_RELEASE = 0.3, 1.2
EXCITE_FULL_DB = 12.0           # this far above the median gives the full follow_db
EXCITE_FLOOR_DB = -120.0
ROOM_ORDER = 2                  # highpass and lowpass of the room are second order
IR_DECAY = math.log(1000.0)     # natural-log amplitude decay of 60 dB

PLAN_KEYS = {"spec", "output", "sounds_dir", "seed", "bed", "room", "moments", "no_bed_clips"}
BED_KEYS = {"name", "level_db", "follow_db"}
ROOM_KEYS = {"wet", "rt60", "highpass_hz", "lowpass_hz"}
MOMENT_KEYS = {"clip", "at", "sound", "sound_start", "level_db", "rise", "tail", "bed_swell_db"}
LEVEL_RANGE = (-80.0, 0.0)      # dB of RMS after the file's own RMS is normalised to 0 dBFS
FOLLOW_RANGE = (0.0, 24.0)
WET_RANGE, RT60_RANGE = (0.0, 1.0), (0.1, 8.0)
HIGHPASS_RANGE, LOWPASS_RANGE = (20.0, 1000.0), (1000.0, 20000.0)
ROOM_DEFAULT = {"wet": 0.25, "rt60": 1.4, "highpass_hz": 150.0, "lowpass_hz": 6500.0}
RISE_RANGE, TAIL_RANGE, SWELL_RANGE = (0.1, 10.0), (0.1, 15.0), (0.0, 24.0)
RISE_DEFAULT, TAIL_DEFAULT = 1.2, 3.0
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FFMPEG_QUIET = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


# ---------------------------------------------------------------- plan validation

def _integer(value, name, low, high):
    number = rt._ranged(value, name, low, high)
    if number != int(number):
        raise SpecError(f"{name} must be a whole number")
    return int(number)


def _resolve_spec_path(path):
    """A relative spec path is tried from the current folder, then from the repo root."""
    if not isinstance(path, str) or not path:
        raise SpecError("spec must be a path to a render spec")
    candidates = [path] if os.path.isabs(path) else [path, os.path.join(REPO_ROOT, path)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise SpecError(f"spec not found: {path}")


def _bed(raw, sounds_dir):
    rt._check_keys(raw, BED_KEYS, "bed")
    for key in ("name", "level_db"):
        if key not in raw:
            raise SpecError(f"bed: missing {key}")
    return {"name": raw["name"], "path": rt._sound_path(raw["name"], "bed", sounds_dir),
            "level_db": rt._ranged(raw["level_db"], "bed level_db", *LEVEL_RANGE),
            "follow_db": rt._ranged(raw.get("follow_db", 0), "bed follow_db", *FOLLOW_RANGE)}


def _room(raw):
    if raw is None:
        return None
    rt._check_keys(raw, ROOM_KEYS, "room")
    got = {key: rt._ranged(raw.get(key, ROOM_DEFAULT[key]), f"room {key}", *limits)
           for key, limits in (("wet", WET_RANGE), ("rt60", RT60_RANGE),
                               ("highpass_hz", HIGHPASS_RANGE), ("lowpass_hz", LOWPASS_RANGE))}
    if got["highpass_hz"] >= got["lowpass_hz"]:
        raise SpecError("room: highpass_hz must be below lowpass_hz")
    return got


def _is_index(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _no_hook(where):
    return SpecError(f"{where}: the spec has no hook")


def _no_bed_clips(raw, spec):
    """Clips whose crowd bed is left out (their moments still play): indexes, or "hook"."""
    if raw is None:
        return []
    clips = spec["clips"]
    if not isinstance(raw, list) or not all(_is_index(i) or i == rt.HOOK for i in raw):
        raise SpecError("no_bed_clips must be a list of ints (clip indexes), or 'hook' for "
                        "the hook")
    seen = []
    for ref in raw:
        if ref == rt.HOOK and not spec.get("hook"):
            raise _no_hook("no_bed_clips")
        if ref != rt.HOOK and not 0 <= ref < len(clips):
            raise SpecError(f"no_bed_clips: {ref} is not a clip index "
                            f"(0 to {len(clips) - 1})")
        if ref in seen:
            raise SpecError(f"no_bed_clips: {_clip_name(ref)} is listed twice")
        seen.append(ref)
    return seen


def _clip_name(ref):
    return "the hook" if ref == rt.HOOK else f"clip {ref}"


def _moment_clip(raw, where, spec):
    """The clip a moment belongs to: an index into the clips, or "hook"."""
    if isinstance(raw, str) and raw == rt.HOOK:
        if not spec.get("hook"):
            raise _no_hook(where)
        return rt.HOOK
    return _integer(raw, f"{where} clip (an index, or 'hook')", 0, len(spec["clips"]) - 1)


def _moment(raw, n, spec, sounds_dir):
    where = f"moment {n + 1}"
    rt._check_keys(raw, MOMENT_KEYS, where)
    for key in ("clip", "at", "sound", "level_db"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key}")
    ref = _moment_clip(raw["clip"], where, spec)
    at = rt._number(raw["at"], f"{where} at")
    length = rt.clip_for(spec, ref)["length"]
    if not 0 <= at < length:
        raise SpecError(f"{where}: at {at:g} is not inside {_clip_name(ref)} ({length:.2f} s)")
    start = raw.get("sound_start")
    return {"clip": ref, "at": at, "sound": raw["sound"],
            "path": rt._sound_path(raw["sound"], where, sounds_dir),
            "sound_start": None if start is None else
            rt._non_negative(start, f"{where} sound_start"),
            "level_db": rt._ranged(raw["level_db"], f"{where} level_db", *LEVEL_RANGE),
            "rise": rt._ranged(raw.get("rise", RISE_DEFAULT), f"{where} rise", *RISE_RANGE),
            "tail": rt._ranged(raw.get("tail", TAIL_DEFAULT), f"{where} tail", *TAIL_RANGE),
            "bed_swell_db": rt._ranged(raw.get("bed_swell_db", 0), f"{where} bed_swell_db",
                                       *SWELL_RANGE)}


def validate_plan(raw, load_spec=rt.load_spec):
    """Check a plan dict and return a normalised copy with the render spec and its timeline."""
    rt._check_keys(raw, PLAN_KEYS, "plan")
    for key in ("spec", "output", "sounds_dir", "bed"):
        if key not in raw:
            raise SpecError(f"plan: missing required field {key}")
    output = raw["output"]
    if not isinstance(output, str) or not output.lower().endswith(".wav"):
        raise SpecError("output must be a path ending in .wav")
    sounds = raw["sounds_dir"]
    if not isinstance(sounds, str) or not os.path.isdir(sounds):
        raise SpecError(f"sounds_dir must be an existing folder (got {sounds!r})")
    spec = load_spec(_resolve_spec_path(raw["spec"]))
    moments_raw = raw.get("moments", [])
    if not isinstance(moments_raw, list):
        raise SpecError("moments must be a list")
    plan = {"spec": spec, "timeline": rt.build_timeline(spec), "output": output,
            "seed": _integer(raw.get("seed", 0), "seed", 0, 2 ** 32 - 1),
            "bed": _bed(raw["bed"], sounds), "room": _room(raw.get("room")),
            "no_bed_clips": _no_bed_clips(raw.get("no_bed_clips"), spec),
            "moments": [_moment(m, n, spec, sounds) for n, m in enumerate(moments_raw)]}
    _refuse_overwriting_an_input(plan)
    return plan


def _refuse_overwriting_an_input(plan):
    """The stem is written over `output`, so it must not be a file the build reads."""
    target = os.path.realpath(plan["output"])
    inputs = [("bed sound", plan["bed"]["path"])]
    inputs += [(f"sound of moment {n + 1}", m["path"]) for n, m in enumerate(plan["moments"])]
    inputs += [(f"source of {_clip_name(ref)}", c["source"])
               for ref, c in rt.scene_clips(plan["spec"])]
    for what, path in inputs:
        if os.path.realpath(path) == target:
            raise SpecError(f"output {plan['output']} is the same file as the {what}; "
                            "the stem would overwrite it")


def load_plan(path):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise SpecError(f"cannot read plan {path}: {exc.strerror}")
    except ValueError as exc:
        raise SpecError(f"plan {path} is not valid JSON: {exc}")
    return validate_plan(raw)


# ---------------------------------------------------------------- ffmpeg pipes

def decode_audio(path, start=0.0, duration=None):
    """The audio of a file (or a stretch of it) as float32 samples, shape (n, 2), 48 kHz."""
    cmd = FFMPEG_QUIET + ["-nostdin"] + (["-ss", f"{start:.3f}"] if start else [])
    cmd += ["-i", path] + (["-t", f"{duration:.3f}"] if duration is not None else [])
    # the first audio stream, which is what the render takes as [0:a]
    cmd += ["-map", "0:a:0", "-vn", "-f", "f32le", "-ac", "2", "-ar", str(RATE), "pipe:1"]
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise ToolError(f"ffmpeg could not decode {path}: " + " | ".join(tail))
    flat = np.frombuffer(proc.stdout, dtype="<f4")
    return flat[:len(flat) // 2 * 2].reshape(-1, 2).astype(np.float32)


def write_wav(path, samples):
    """Write float32 stereo samples as a WAV through ffmpeg, atomically (temp file, rename)."""
    partial = path + ".part"
    cmd = FFMPEG_QUIET + ["-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0",
                          "-c:a", "pcm_f32le", "-f", "wav", partial]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        proc = subprocess.run(cmd, input=np.ascontiguousarray(samples, "<f4").tobytes(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            tail = proc.stderr.decode(errors="replace").strip().splitlines()[-3:]
            raise ToolError("ffmpeg could not write the stem: " + " | ".join(tail))
        os.replace(partial, path)
    finally:
        if os.path.exists(partial):
            os.remove(partial)


# ---------------------------------------------------------------- signal helpers

def lin(db):
    return 10.0 ** (np.asarray(db, dtype=np.float64) / 20.0)


def to_db(amplitude):
    return 20.0 * math.log10(max(float(amplitude), 1e-12))


def rms(samples):
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) if len(samples) else 0.0


def normalise_rms(samples, where):
    """Scale a file so its own RMS is 0 dBFS, which makes levels comparable across files."""
    level = rms(samples)
    if level <= 0.0 or len(samples) < RATE * MIN_SOUND_SECONDS:
        raise ToolError(f"{where} is silent or shorter than {MIN_SOUND_SECONDS:g} s")
    return samples.astype(np.float64) / level


def raised_cosine(u):
    """0 to 1 over u in 0..1 (clipped), with zero slope at both ends."""
    return 0.5 - 0.5 * np.cos(math.pi * np.clip(u, 0.0, 1.0))


def fade_edges(samples, seconds):
    """Raised cosine fade in and out over `seconds` at the two ends (shorter for short input)."""
    count = min(int(round(seconds * RATE)), len(samples) // 2)
    if count <= 0:
        return samples
    ramp = raised_cosine(np.arange(count) / count)[:, None]
    out = samples.copy()
    out[:count] *= ramp
    out[len(out) - count:] *= ramp[::-1]
    return out


def band_response(freqs, low, high, order):
    """Magnitude of a Butterworth highpass at `low` times a lowpass at `high`."""
    with np.errstate(divide="ignore"):
        highpass = 1.0 / np.sqrt(1.0 + np.power(low / np.maximum(freqs, 1e-9), 2 * order))
    lowpass = 1.0 / np.sqrt(1.0 + np.power(freqs / high, 2 * order))
    return highpass * lowpass


# ---------------------------------------------------------------- bed

def make_loop(samples):
    """One period of a seamless loop: the end of the file is crossfaded (equal power) into its start."""
    x = normalise_rms(samples, "the bed sound")
    fade = min(int(LOOP_CROSSFADE * RATE), len(x) // 4)
    period = x[:len(x) - fade].copy()
    t = (np.arange(fade) / fade)[:, None] * (math.pi / 2)
    period[:fade] = x[:fade] * np.sin(t) + x[len(x) - fade:] * np.cos(t)
    return period


def loop_segment(period, offset, count):
    return period[(offset + np.arange(count)) % len(period)]


def bed_offsets(seed, period_length, clip_count):
    """A different start inside the loop for every clip, spread out, from the seed."""
    first = int(np.random.default_rng([seed, 1]).integers(period_length))
    step = period_length // max(1, clip_count)
    return [(first + k * step) % period_length for k in range(clip_count)]


# ---------------------------------------------------------------- following the commentary

def _smooth(values):
    """One-pole smoothing of a dB track: quick attack, slow release."""
    attack = 1.0 - math.exp(-EXCITE_HOP / EXCITE_ATTACK)
    release = 1.0 - math.exp(-EXCITE_HOP / EXCITE_RELEASE)
    out = np.empty_like(values)
    level = values[0]
    for i, value in enumerate(values):
        level += (value - level) * (attack if value > level else release)
        out[i] = level
    return out


def excitement_gain_db(commentary, follow_db):
    """Gain in dB per sample: 0 at the clip's median loudness of 300 to 3000 Hz, follow_db at +12 dB."""
    count = len(commentary)
    hop = int(EXCITE_HOP * RATE)
    frames = count // hop
    if frames < 2 or follow_db == 0:
        return np.zeros(count)
    mono = commentary.astype(np.float64).mean(axis=1)
    spectrum = np.fft.rfft(mono)
    spectrum *= band_response(np.fft.rfftfreq(count, 1.0 / RATE), *EXCITE_BAND, EXCITE_ORDER)
    band = np.fft.irfft(spectrum, count)[:frames * hop].reshape(frames, hop)
    db = 20.0 * np.log10(np.maximum(np.sqrt(np.mean(band ** 2, axis=1)), 1e-6))
    smooth = _smooth(np.maximum(db, EXCITE_FLOOR_DB))
    gain = follow_db * np.clip((smooth - np.median(smooth)) / EXCITE_FULL_DB, 0.0, 1.0)
    return np.interp(np.arange(count), (np.arange(frames) + 0.5) * hop, gain)


def fit_length(samples, count):
    """Cut or zero-pad to exactly `count` samples."""
    if len(samples) >= count:
        return samples[:count]
    return np.concatenate([samples, np.zeros((count - len(samples), 2), samples.dtype)])


# ---------------------------------------------------------------- moments

def swell_db(count, moment):
    """Extra bed gain in dB: raised cosine up to `at`, hold HOLD s, raised cosine down over tail."""
    t = np.arange(count) / RATE
    rising = raised_cosine((t - (moment["at"] - moment["rise"])) / moment["rise"])
    falling = 1.0 - raised_cosine((t - (moment["at"] + HOLD)) / moment["tail"])
    return moment["bed_swell_db"] * rising * falling


def sound_envelope(count, moment):
    """Raised cosine in over `rise` to `at`, full for HOLD s, equal-power out over `tail`."""
    t = np.arange(count) / RATE
    rising = raised_cosine((t - (moment["at"] - moment["rise"])) / moment["rise"])
    falling = np.cos(0.5 * math.pi * np.clip((t - (moment["at"] + HOLD)) / moment["tail"], 0, 1))
    cut = raised_cosine((count - 1 - np.arange(count)) / (CUT_FADE * RATE))
    return rising * falling * cut


def resample(samples, factor):
    """Play the sound `factor` times faster (pitch and speed together), by linear interpolation."""
    positions = np.arange(int((len(samples) - 1) / factor) + 1) * factor
    low = np.minimum(positions.astype(np.int64), len(samples) - 1)
    high = np.minimum(low + 1, len(samples) - 1)
    frac = (positions - low)[:, None]
    return samples[low] * (1.0 - frac) + samples[high] * frac


def _place(layer, sound, source_at, dest_at):
    """Copy `sound` into `layer` so that sound[source_at] lands on layer[dest_at], clipped.

    Returns the index in `layer` just past the last copied sample (None when nothing was copied).
    """
    skip = max(0, -dest_at, -source_at)
    dest, source = dest_at + skip, source_at + skip
    count = min(len(layer) - dest, len(sound) - source)
    if count <= 0:
        return None
    layer[dest:dest + count] = sound[source:source + count]
    return dest + count


def _sound_start(moment, seconds, factor, rng):
    """Where in the file the moment's sound begins: the plan's sound_start, or a seeded pick that
    leaves room for the whole moment (rise + hold + tail, played `factor` times faster)."""
    if moment["sound_start"] is not None:
        return moment["sound_start"]
    span = moment["rise"] + (HOLD + moment["tail"]) * factor
    return float(rng.uniform(0.0, max(0.0, seconds - span)))


def moment_layer(sound, count, moment, rng):
    """The one-shot sound of a moment as a (count, 2) layer, resampled by a seeded random factor.

    `sound` is the file normalised to 0 dBFS RMS. The factor is drawn first, then (when the plan
    gives no sound_start) a start position. The file position sound_start + rise is put exactly
    on `at` whatever the factor, so a pitch change cannot move the moment. A file that runs out
    before the moment ends is faded over SOUND_END_FADE at its last sample and logged.
    """
    factor = 1.0 + rng.uniform(-RESAMPLE_SPREAD, RESAMPLE_SPREAD)
    seconds = len(sound) / RATE
    start = _sound_start(moment, seconds, factor, rng)
    left = seconds - (start + moment["rise"])          # file seconds from the moment on
    name = moment["sound"]
    if left <= 0:
        raise SpecError(f"sound '{name}': sound_start {start:g} s plus rise {moment['rise']:g} s "
                        f"is past the end of the {seconds:.2f} s file")
    if left < MIN_SOUND_SECONDS:
        raise SpecError(f"sound '{name}': only {left:.2f} s of the {seconds:.2f} s file is left "
                        f"after sound_start {start:g} s plus rise, so nothing plays at the moment")
    layer = np.zeros((count, 2))
    dest_at = int(round(moment["at"] * RATE))
    anchor = int(round((start + moment["rise"]) / factor * RATE))   # resampled index of the moment
    rise_n = int(round(moment["rise"] * RATE))
    end = _place(layer, resample(sound, factor), anchor - rise_n, dest_at - rise_n)
    envelope_end = int(round((moment["at"] + HOLD + moment["tail"]) * RATE))
    if end is not None and end < min(count, envelope_end):
        fade = min(int(round(SOUND_END_FADE * RATE)), end)
        layer[end - fade:end] *= raised_cosine(np.arange(fade - 1, -1, -1) / fade)[:, None]
        log(f"warning: sound '{name}' ends {(envelope_end - end) / RATE:.2f} s before its moment "
            f"at {moment['at']:g} s does (rise + hold + tail need more of the file than "
            f"sound_start {start:g} s leaves); faded out over {SOUND_END_FADE * 1000:.0f} ms")
    return layer * sound_envelope(count, moment)[:, None] * lin(moment["level_db"])


# ---------------------------------------------------------------- room

def impulse_response(rt60, rng):
    """Decorrelated left and right noise decaying 60 dB over rt60 seconds, unit energy each."""
    length = int(rt60 * RATE)
    decay = np.exp(-IR_DECAY * np.arange(length) / (rt60 * RATE))[:, None]
    ir = rng.standard_normal((length, 2)) * decay
    return ir / np.sqrt(np.sum(ir ** 2, axis=0))


def apply_room(samples, ir, room):
    """Blend the dry crowd with its convolution (FFT) and band-limit the sum; cut at the input length."""
    count = len(samples)
    size = 1 << int(count + len(ir) - 1).bit_length()
    freqs = np.fft.rfftfreq(size, 1.0 / RATE)
    wet = room["wet"]
    response = (1.0 - wet) + wet * np.fft.rfft(ir, size, axis=0)
    response *= band_response(freqs, room["highpass_hz"], room["lowpass_hz"], ROOM_ORDER)[:, None]
    return np.fft.irfft(np.fft.rfft(samples, size, axis=0) * response, size, axis=0)[:count]


# ---------------------------------------------------------------- one clip and the whole stem

class StemBuilder:
    """Holds the decoded sounds and the seeded pieces shared by every clip."""

    def __init__(self, plan, decode):
        self.plan, self.decode, self.sounds = plan, decode, {}
        self.clips = [s for s in plan["timeline"]["segments"] if s["kind"] == "clip"]
        self.period = make_loop(self._file(plan["bed"]["path"]))
        self.offsets = bed_offsets(plan["seed"], len(self.period), len(self.clips))
        # a clip's bed starts where its place in play order says (the hook, if any, is first)
        self.place = {seg["clip"]: n for n, seg in enumerate(self.clips)}
        self.no_bed = set(plan["no_bed_clips"])
        room = plan["room"]
        self.ir = (impulse_response(room["rt60"], np.random.default_rng([plan["seed"], 3]))
                   if room else None)

    def _file(self, path):
        if path not in self.sounds:
            self.sounds[path] = self.decode(path, 0.0, None)
        return self.sounds[path]

    def _moment_sound(self, moment):
        return normalise_rms(self._file(moment["path"]), f"sound '{moment['sound']}'")

    def bed(self, segment):
        """The bed of one clip: looped, levelled, following the commentary, swelling at moments."""
        plan, index = self.plan, segment["clip"]
        clip = rt.clip_for(plan["spec"], index)
        count = int(round(segment["duration"] * RATE))
        if index in self.no_bed:
            return np.zeros((count, 2))
        bed = loop_segment(self.period, self.offsets[self.place[index]], count)
        bed = fade_edges(bed * lin(plan["bed"]["level_db"]), EDGE_FADE)
        gain = np.zeros(count)
        if plan["bed"]["follow_db"] > 0:
            commentary = self.decode(clip["source"], clip["start_s"], clip["length"])
            gain = excitement_gain_db(fit_length(commentary, count), plan["bed"]["follow_db"])
        swells = [swell_db(count, m) for m in self.moments(index)]
        if swells:
            gain = gain + np.max(swells, axis=0)
        return bed * lin(gain)[:, None]

    def moments(self, index):
        return [m for m in self.plan["moments"] if m["clip"] == index]

    def layers(self, segment):
        index, layers = segment["clip"], []
        count = int(round(segment["duration"] * RATE))
        for number, moment in enumerate(self.plan["moments"]):
            if moment["clip"] == index:
                rng = np.random.default_rng([self.plan["seed"], 2, number])
                layers.append(moment_layer(self._moment_sound(moment), count, moment, rng))
        return layers

    def clip(self, segment):
        """The finished crowd of one clip and its summary line."""
        bed, layers = self.bed(segment), self.layers(segment)
        crowd = bed + sum(layers) if layers else bed
        if self.ir is not None:
            crowd = apply_room(crowd, self.ir, self.plan["room"])
        peak = max((float(np.max(np.abs(x))) for x in layers), default=0.0)
        note = f"peak moment {to_db(peak):.1f} dBFS" if layers else "no moments"
        name = rt.clip_label(segment["clip"])
        if segment["clip"] in self.no_bed:
            line = f"{name}: no bed (left out), {note}"
        else:
            line = f"{name}: bed RMS {to_db(rms(bed)):.1f} dBFS, {note}"
        return fade_edges(crowd, EDGE_FADE), line


def build_stem(plan, decode=decode_audio):
    """The whole stem as float32 (n, 2), digital silence outside the clips, and summary lines."""
    total = int(round(plan["timeline"]["total"] * RATE))
    stem = np.zeros((total, 2), dtype=np.float32)
    builder = StemBuilder(plan, decode)
    lines = []
    for segment in builder.clips:
        crowd, line = builder.clip(segment)
        lines.append(line)
        first = int(round(segment["start"] * RATE))
        part = crowd[:max(0, total - first)]
        stem[first:first + len(part)] = part
    peak = float(np.max(np.abs(stem))) if total else 0.0
    if peak > 1.0:
        log(f"warning: the stem peaked at {to_db(peak):+.1f} dBFS; scaled the whole stem down "
            f"by {to_db(peak):.1f} dB")
        stem = (stem / peak).astype(np.float32)
    return stem, lines


# ---------------------------------------------------------------- command line

def parse_args(argv):
    parser = argparse.ArgumentParser(description="Build the crowd stem WAV of a TikTok spec.")
    parser.add_argument("plan", help="path to the plan JSON file")
    return parser.parse_args(argv)


def main(argv=None, decode=decode_audio, write=write_wav):
    args = parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if decode is decode_audio:
            require_tools()
        stem, lines = build_stem(plan, decode)
        print("\n".join(lines))
        write(plan["output"], stem)
    except (ToolError, OSError) as exc:
        log(f"error: {exc}")
        return 1
    print(f"{plan['output']}  {len(stem) / RATE:.2f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
