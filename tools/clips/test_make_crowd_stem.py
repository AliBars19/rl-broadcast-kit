"""Tests for make_crowd_stem (run with: cd tools/clips && python3 -m unittest test_make_crowd_stem).

numpy is only in the user site, so under `python3 -I` the import below fails and the whole file
skips itself. Audio decoding is injected: no video is needed, the sounds and the "commentary"
are synthetic arrays. The real ffmpeg pipes get one round trip test.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    import numpy as np
    import make_crowd_stem as mcs
except ImportError as exc:  # pragma: no cover - depends on the interpreter flags
    raise unittest.SkipTest(f"numpy is needed ({exc}); run without -I")

RATE = mcs.RATE
FFMPEG_OK = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
# Two clips: 6 s and 5 s, 0.4 s cards: card 0-0.4, clip 1 0.4-6.4, card 6.4-6.8, clip 2 6.8-11.8
CLIP1_START, CLIP2_START, TOTAL = 0.4, 6.8, 11.8
SPEC = {"caption": "test.", "output": "/out/test.mp4", "card_seconds": 0.4,
        "clips": [{"source": "/v/stream.mp4", "start": 10, "end": 16},
                  {"source": "/v/stream.mp4", "start": 30, "end": 35}]}
PLAN = {"seed": 7,
        "bed": {"name": "bed", "level_db": -30, "follow_db": 6},
        "moments": [{"clip": 0, "at": 2.0, "sound": "roar", "sound_start": 0.5,
                     "level_db": -20, "rise": 1.0, "tail": 2.0, "bed_swell_db": 10}]}


def noise(seconds, seed, amp=0.1):
    return (np.random.default_rng(seed).standard_normal((int(seconds * RATE), 2)) * amp
            ).astype(np.float32)


def tone(seconds, freq, amp):
    t = np.arange(int(seconds * RATE)) / RATE
    return np.repeat((amp * np.sin(2 * np.pi * freq * t))[:, None], 2, axis=1).astype(np.float32)


def commentary(seconds):
    """Quiet 1 kHz talking, then the last 30 % of the clip shouting 30 times louder."""
    out = tone(seconds, 1000, 0.01)
    loud = int(len(out) * 0.7)
    out[loud:] *= 30
    return out


LOUD_SOURCE_SECONDS = ((14.2, 16.0), (33.5, 35.0))    # where the "stream" shouts, source time


def commentary_at(start, duration):
    """Quiet 1 kHz talking from source second `start`, shouting in LOUD_SOURCE_SECONDS.

    A decode of the wrong stretch of the source therefore gives the wrong (quiet) commentary.
    """
    out = tone(duration, 1000, 0.01)
    seconds = start + np.arange(len(out)) / RATE
    for low, high in LOUD_SOURCE_SECONDS:
        out[(seconds >= low) & (seconds < high)] *= 30
    return out


def fake_decode(path, start=0.0, duration=None):
    name = os.path.basename(path)
    if name == "bed.wav":
        return noise(6.0, 1)
    if name == "roar.wav":
        return noise(5.0, 2, 0.2)
    if name == "click.wav":
        out = np.zeros((int(4 * RATE), 2), np.float32)
        out[int(2.5 * RATE)] = 1.0
        return out
    if name == "tone.wav":
        return tone(4.0, 1000, 0.2)
    if name.endswith(".mp4"):
        return commentary_at(start, duration)
    raise AssertionError(f"unexpected decode of {path}")


def window_db(stem, start, end):
    chunk = stem[int(start * RATE):int(end * RATE)].astype(np.float64)
    return mcs.to_db(mcs.rms(chunk))


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.sounds = os.path.join(self.dir, "sfx")
        os.makedirs(self.sounds)
        for name in ("bed", "roar", "click", "tone"):
            open(os.path.join(self.sounds, name + ".wav"), "wb").close()
        self.spec_path = os.path.join(self.dir, "spec.json")
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            json.dump(SPEC, handle)

    def raw(self, **changes):
        out = copy.deepcopy(PLAN)
        out.update({"spec": self.spec_path, "output": os.path.join(self.dir, "stem.wav"),
                    "sounds_dir": self.sounds})
        out.update(changes)
        return out

    def plan(self, **changes):
        return mcs.validate_plan(self.raw(**changes))

    def build(self, decode=fake_decode, **changes):
        with contextlib.redirect_stderr(io.StringIO()):
            return mcs.build_stem(self.plan(**changes), decode)

    def assertInvalid(self, needle, **changes):
        with self.assertRaises(mcs.SpecError) as ctx:
            self.plan(**changes)
        self.assertIn(needle, str(ctx.exception))


def with_bed(**fields):
    return {**PLAN["bed"], **fields}


def with_moment(**fields):
    return [{**PLAN["moments"][0], **fields}]


ROOM = {"wet": 0.3, "rt60": 1.2, "highpass_hz": 150, "lowpass_hz": 6500}


class ShapeTests(Fixture):
    def test_length_is_the_planned_total_in_samples(self):
        stem, _ = self.build()
        self.assertEqual(stem.shape, (round(TOTAL * RATE), 2))
        self.assertEqual(stem.dtype, np.float32)

    def test_cards_are_digital_silence_even_with_a_room(self):
        for room in (None, ROOM):
            stem, _ = self.build(room=room, moments=PLAN["moments"] + [
                {"clip": 0, "at": 5.5, "sound": "roar", "level_db": -20, "tail": 3}])
            for start, end in ((0, 0.4), (6.4, 6.8)):
                self.assertTrue(np.all(stem[round(start * RATE):round(end * RATE)] == 0),
                                (room, start))
            self.assertTrue(np.any(stem[round(1 * RATE):round(2 * RATE)] != 0))
            self.assertTrue(np.any(stem[round(8 * RATE):round(9 * RATE)] != 0))

    def test_the_clip_edges_are_faded(self):
        stem, _ = self.build(room=ROOM)
        first = round(CLIP1_START * RATE)
        self.assertLess(abs(float(stem[first, 0])), 1e-3)
        self.assertLess(abs(float(stem[round(6.4 * RATE) - 1, 0])), 1e-3)

    def test_bed_sits_at_its_level_in_rms(self):
        stem, _ = self.build(moments=[], bed=with_bed(follow_db=0))
        self.assertAlmostEqual(window_db(stem, 2.0, 5.0), -30.0, delta=0.5)
        self.assertAlmostEqual(window_db(stem, 8.0, 11.0), -30.0, delta=0.5)

    def test_every_clip_starts_at_a_different_point_of_the_bed(self):
        stem, _ = self.build(moments=[], bed=with_bed(follow_db=0))
        a = stem[round(1 * RATE):round(1.2 * RATE)]
        b = stem[round(7.4 * RATE):round(7.6 * RATE)]
        self.assertFalse(np.array_equal(a, b))
        offsets = mcs.bed_offsets(7, 100000, 4)
        self.assertEqual(len(set(offsets)), 4)

    def test_summary_line_per_clip(self):
        _, lines = self.build()
        self.assertEqual(len(lines), 2)
        self.assertIn("clip 1", lines[0])
        self.assertIn("bed RMS", lines[0])
        self.assertIn("peak moment", lines[0])
        self.assertIn("no moments", lines[1])


class MomentTests(Fixture):
    def envelope(self, stem, start, end, step=0.1):
        times = np.arange(start, end - step, step)
        return times, np.array([window_db(stem, t, t + step) for t in times])

    def test_swell_peaks_within_half_a_second_of_the_moment_and_rises_before_it(self):
        stem, _ = self.build(bed=with_bed(follow_db=0))
        moment = CLIP1_START + 2.0
        times, env = self.envelope(stem, CLIP1_START + 0.1, CLIP1_START + 5.9)
        self.assertLess(abs(times[int(np.argmax(env))] + 0.05 - moment), 0.5)
        early = window_db(stem, CLIP1_START + 0.2, CLIP1_START + 0.5)
        middle = window_db(stem, moment - 0.5, moment - 0.3)
        peak = window_db(stem, moment, moment + 0.3)
        self.assertLess(early, middle - 3)
        self.assertLess(middle, peak - 3)
        late = window_db(stem, CLIP1_START + 5.0, CLIP1_START + 5.8)
        self.assertLess(late, peak - 10)       # and it has died away again after the tail

    def test_sound_lands_at_the_moment_minus_rise(self):
        # the click sits 2.5 s into the file; sound_start 2.0 puts it 0.5 s after at - rise
        moment = {"sound": "click", "sound_start": 2.0, "rise": 1.0, "at": 3.0,
                  "level_db": 0.0, "tail": 1.0, "bed_swell_db": 0}
        click = mcs.normalise_rms(fake_decode("click.wav"), "click")
        layer = mcs.moment_layer(click, int(6 * RATE), moment, np.random.default_rng(1))
        where = int(np.argmax(np.abs(layer[:, 0]))) / RATE
        self.assertAlmostEqual(where, 2.0 + 0.5, delta=0.03)   # at - rise + 0.5, within the 3 %

    def test_the_file_position_sound_start_plus_rise_lands_on_at_whatever_the_pitch(self):
        # a click at 10 s of the file; sound_start 2 + rise 8 is exactly that file position, so
        # it must sound at `at` = 9 s with no drift from the resampling factor (up to 0.24 s here)
        sound = np.zeros((12 * RATE, 2))
        sound[10 * RATE] = 1.0
        moment = {"sound": "click", "sound_start": 2.0, "rise": 8.0, "at": 9.0,
                  "level_db": 0.0, "tail": 1.0, "bed_swell_db": 0}
        seen = set()
        for seed in range(20):
            layer = mcs.moment_layer(sound, 14 * RATE, moment, np.random.default_rng(seed))
            where = int(np.argmax(np.abs(layer[:, 0]))) / RATE
            self.assertAlmostEqual(where, 9.0, delta=0.001, msg=f"seed {seed}")
            seen.add(round(float(np.max(np.abs(layer[:, 0]))), 3))
        self.assertGreaterEqual(min(seen), 0.4)    # played, not dropped (linear interpolation may split it)

    def test_a_moment_may_begin_before_the_clip_does(self):
        flat = with_bed(follow_db=0)
        early = with_moment(at=0.5, rise=1.2, bed_swell_db=0)
        stem, _ = self.build(bed=flat, moments=early)
        bare, _ = self.build(bed=flat, moments=[])
        self.assertEqual(stem.shape[0], round(TOTAL * RATE))
        # the rise began 0.7 s before the clip: 0.2 s in, the crowd sound is already well up
        window = (CLIP1_START + 0.2, CLIP1_START + 0.9)
        self.assertGreater(window_db(stem, *window), window_db(bare, *window) + 6)

    def test_the_sound_is_cut_at_the_clip_end_and_the_next_card_stays_silent(self):
        moment = {"sound": "ones", "sound_start": 0.0, "rise": 1.0, "at": 5.0,
                  "level_db": 0.0, "tail": 4.0, "bed_swell_db": 0}
        count = int(6.0 * RATE)                       # the clip ends 1 s after the moment
        sound = np.ones((8 * RATE, 2))
        layer = mcs.moment_layer(sound, count, moment, np.random.default_rng(1))
        self.assertLess(abs(float(layer[-1, 0])), 1e-3)              # fully faded at the cut
        half = count - 1 - int(mcs.CUT_FADE * RATE / 2)
        flat = float(mcs.sound_envelope(count, {**moment, "at": 5.0})[half - 4800])
        self.assertGreater(flat, 0.9)                                # full before the fade
        self.assertAlmostEqual(float(layer[half, 0]) / flat, 0.5, delta=0.1)   # mid fade
        stem, _ = self.build(room=ROOM, moments=with_moment(at=5.0, tail=4.0))
        self.assertTrue(np.all(stem[round(6.4 * RATE):round(6.8 * RATE)] == 0))
        self.assertGreater(window_db(stem, CLIP1_START + 4.9, CLIP1_START + 5.3), -35)

    def test_a_sound_that_runs_out_of_file_fades_over_20_ms_instead_of_clicking(self):
        # 1.0 s of constant file after the anchor; the moment wants 3.3 s (rise + hold + tail)
        moment = {"sound": "ones", "sound_start": 2.5, "rise": 1.0, "at": 3.0,
                  "level_db": 0.0, "tail": 2.0, "bed_swell_db": 0}
        sound = np.ones((4 * RATE, 2))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            layer = mcs.moment_layer(sound, 8 * RATE, moment, np.random.default_rng(1))
        last = int(np.flatnonzero(layer[:, 0])[-1])
        self.assertGreater(last, int(3.4 * RATE))                    # it did play to the end
        self.assertLess(abs(float(layer[last, 0])), 1e-3)            # and ends on zero
        self.assertAlmostEqual(float(layer[last - 480, 0]), 0.5 * 0.99, delta=0.1)
        self.assertGreater(float(layer[last - 960 - 480, 0]), 0.9)   # flat before the 20 ms
        self.assertIn("warning", err.getvalue())
        self.assertIn("ones", err.getvalue())

    def test_no_warning_when_the_file_is_long_enough_for_the_whole_moment(self):
        moment = {"sound": "ones", "sound_start": 0.5, "rise": 1.0, "at": 3.0,
                  "level_db": 0.0, "tail": 2.0, "bed_swell_db": 0}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mcs.moment_layer(np.ones((8 * RATE, 2)), 8 * RATE, moment, np.random.default_rng(1))
            # the clip ending first is not the file running out
            mcs.moment_layer(np.ones((8 * RATE, 2)), 4 * RATE, moment, np.random.default_rng(1))
        self.assertEqual(err.getvalue(), "")

    def test_a_sound_start_so_late_that_nothing_plays_is_an_error_not_silence(self):
        # roar is 5 s; rise 1.0 puts the moment's file position at sound_start + 1
        for start, needle in ((9.0, "past the end"), (4.5, "past the end"),
                              (3.95, "nothing plays")):
            with self.assertRaises(mcs.SpecError, msg=start) as ctx:
                self.build(moments=with_moment(sound_start=start))
            self.assertIn(needle, str(ctx.exception))
        self.build(moments=with_moment(sound_start=3.5))       # 0.5 s left: plays

    def test_several_moments_in_one_clip_and_in_two_clips(self):
        moments = [{"clip": 0, "at": 1.5, "sound": "roar", "level_db": -25},
                   {"clip": 0, "at": 4.0, "sound": "roar", "level_db": -25},
                   {"clip": 1, "at": 2.0, "sound": "tone", "level_db": -25}]
        stem, lines = self.build(moments=moments)
        self.assertEqual(stem.shape[0], round(TOTAL * RATE))
        self.assertIn("peak moment", lines[1])

    def test_default_sound_start_is_seeded_and_inside_the_file(self):
        moments = [{"clip": 0, "at": 2.0, "sound": "roar", "level_db": -20}]
        a, _ = self.build(moments=moments)
        b, _ = self.build(moments=moments)
        c, _ = self.build(moments=moments, seed=8)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, c))

    def test_the_default_sound_start_leaves_room_for_the_whole_moment_in_the_file(self):
        # a ramp that reads 1 + its own file position in seconds; at `at` the layer is at full
        # envelope, so the layer value there says which file position was put on the moment
        sound = np.repeat((1.0 + np.arange(5 * RATE) / RATE)[:, None], 2, axis=1)
        moment = {"sound": "ramp", "sound_start": None, "rise": 0.5, "at": 3.0,
                  "level_db": 0.0, "tail": 1.0, "bed_swell_db": 0}
        end = int((moment["at"] + mcs.HOLD + moment["tail"]) * RATE)
        starts = []
        for seed in range(60):
            layer = mcs.moment_layer(sound, 7 * RATE, moment, np.random.default_rng(seed))
            starts.append(float(layer[int(moment["at"] * RATE), 0]) - 1.0 - moment["rise"])
            self.assertGreaterEqual(starts[-1], -1e-3, f"seed {seed}")
            # the file lasts until the envelope ends: still sounding just before it
            self.assertNotEqual(float(layer[end - 100, 0]), 0.0, f"seed {seed} ran out of file")
        self.assertGreater(max(starts) - min(starts), 1.0)       # it really is random
        again = mcs.moment_layer(sound, 7 * RATE, moment, np.random.default_rng(3))
        same = mcs.moment_layer(sound, 7 * RATE, moment, np.random.default_rng(3))
        self.assertTrue(np.array_equal(again, same))

    def test_a_seeded_start_with_a_large_rise_and_a_slow_pitch_still_leaves_the_whole_moment(self):
        # rise 8 s, tail 2 s, factor 0.97 (slower): the moment needs start + rise + (HOLD + tail) *
        # factor of file, which must not pass the 12 s file
        moment = {"sound": "roar", "sound_start": None, "rise": 8.0, "at": 9.0,
                  "level_db": 0.0, "tail": 2.0, "bed_swell_db": 0}
        seconds, factor = 12.0, 0.97
        starts = []
        for seed in range(200):
            start = mcs._sound_start(moment, seconds, factor, np.random.default_rng(seed))
            starts.append(start)
            self.assertGreaterEqual(start, 0.0, f"seed {seed}")
            self.assertLessEqual(start + moment["rise"] + (mcs.HOLD + moment["tail"]) * factor,
                                 seconds + 1e-9, f"seed {seed} runs out of file")
        self.assertGreater(max(starts) - min(starts), 0.5)       # it really is random

    def test_no_two_moments_are_the_same(self):
        sound = mcs.normalise_rms(fake_decode("tone.wav"), "tone")
        moment = {"sound": "tone", "sound_start": 0.5, "rise": 0.5, "at": 1.5, "level_db": 0.0,
                  "tail": 1.0, "bed_swell_db": 0}
        layers = [mcs.moment_layer(sound, int(4 * RATE), moment,
                                   np.random.default_rng([7, 2, n])) for n in range(6)]
        for i in range(len(layers)):
            for j in range(i):
                self.assertFalse(np.array_equal(layers[i], layers[j]))
        # pitch and speed move together by at most 3 %: count zero crossings in the held part
        for layer in layers:
            held = layer[int(1.55 * RATE):int(1.75 * RATE), 0]
            crossings = np.sum(np.signbit(held[1:]) != np.signbit(held[:-1]))
            freq = crossings / 2 / 0.2
            self.assertGreater(freq, 1000 * 0.97 - 15)
            self.assertLess(freq, 1000 * 1.03 + 15)
        freqs = {round(float(np.sum(np.signbit(x[int(1.55 * RATE):int(1.75 * RATE), 0][1:]) !=
                                    np.signbit(x[int(1.55 * RATE):int(1.75 * RATE), 0][:-1]))))
                 for x in layers}
        self.assertGreater(len(freqs), 1)

    def test_resample_changes_length_by_the_factor(self):
        ramp = np.repeat(np.arange(1000, dtype=np.float64)[:, None], 2, axis=1)
        out = mcs.resample(ramp, 1.03)
        self.assertAlmostEqual(len(out), 1000 / 1.03, delta=1.5)
        self.assertAlmostEqual(float(out[100, 0]), 103.0, places=6)

    def test_the_resample_factor_is_within_three_percent(self):
        factors = [1.0 + np.random.default_rng([7, 2, n]).uniform(-mcs.RESAMPLE_SPREAD,
                                                                   mcs.RESAMPLE_SPREAD)
                   for n in range(200)]
        self.assertGreaterEqual(min(factors), 0.97)
        self.assertLessEqual(max(factors), 1.03)


class FollowTests(Fixture):
    def levels(self, follow_db):
        stem, _ = self.build(moments=[], bed=with_bed(follow_db=follow_db))
        quiet = window_db(stem, CLIP1_START + 1.0, CLIP1_START + 3.0)
        loud = window_db(stem, CLIP1_START + 4.9, CLIP1_START + 5.8)   # shouting starts at 4.2 s
        return quiet, loud

    def test_the_bed_rises_where_the_commentary_is_loud(self):
        quiet, loud = self.levels(6)
        self.assertGreater(loud - quiet, 5.0)
        self.assertLess(loud - quiet, 7.0)

    def test_each_clip_follows_its_own_stretch_of_the_source(self):
        calls = []

        def spy(path, start=0.0, duration=None):
            calls.append((os.path.basename(path), start, duration))
            return fake_decode(path, start, duration)

        stem, _ = self.build(decode=spy, moments=[])
        self.assertIn(("stream.mp4", 10.0, 6.0), calls)
        self.assertIn(("stream.mp4", 30.0, 5.0), calls)
        # clip 2 shouts from its 3.5 s on (source second 33.5), clip 1 from 4.2 s
        quiet = window_db(stem, CLIP2_START + 0.5, CLIP2_START + 2.0)
        loud = window_db(stem, CLIP2_START + 3.9, CLIP2_START + 4.8)
        self.assertGreater(loud - quiet, 5.0)
        self.assertLess(loud - quiet, 7.0)

    def test_without_follow_the_bed_stays_flat(self):
        quiet, loud = self.levels(0)
        self.assertLess(abs(loud - quiet), 0.7)

    def test_gain_mapping(self):
        gain = mcs.excitement_gain_db(commentary(6.0), 6.0)
        self.assertEqual(len(gain), 6 * RATE)
        self.assertAlmostEqual(float(gain[int(2 * RATE)]), 0.0, places=3)
        self.assertAlmostEqual(float(gain[int(5.5 * RATE)]), 6.0, places=2)
        self.assertLessEqual(float(gain.max()), 6.0 + 1e-9)
        self.assertGreaterEqual(float(gain.min()), 0.0)
        self.assertTrue(np.all(mcs.excitement_gain_db(commentary(6.0), 0.0) == 0))

    def test_silent_commentary_gives_no_change(self):
        gain = mcs.excitement_gain_db(np.zeros((3 * RATE, 2), np.float32), 6.0)
        self.assertTrue(np.all(gain == 0))

    def test_only_the_speech_band_counts(self):
        audio = tone(6.0, 1000, 0.01)
        rumble = tone(2.0, 60, 0.5)                     # a 60 Hz rumble, 34 dB louder,
        rumble *= np.hanning(len(rumble))[:, None]      # faded in and out: no click
        audio[int(4 * RATE):] += rumble
        gain = mcs.excitement_gain_db(audio, 6.0)
        self.assertLess(float(gain.max()), 0.5)      # 60 Hz is far outside 300 to 3000 Hz


class SeedAndSafetyTests(Fixture):
    def test_same_seed_same_output_other_seed_other_output(self):
        a, _ = self.build(room=ROOM)
        b, _ = self.build(room=ROOM)
        c, _ = self.build(room=ROOM, seed=8)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, c))

    def test_the_room_changes_the_sound_but_not_the_length(self):
        dry, _ = self.build()
        wet, _ = self.build(room=ROOM)
        self.assertEqual(dry.shape, wet.shape)
        self.assertFalse(np.array_equal(dry, wet))

    def test_wet_zero_keeps_the_level_of_a_dry_bed_within_the_filter_loss(self):
        # a second order lowpass at 20 kHz still takes about 1 dB off white noise up to 24 kHz
        stem, _ = self.build(moments=[], bed=with_bed(follow_db=0),
                             room={**ROOM, "wet": 0.0, "highpass_hz": 20, "lowpass_hz": 20000})
        self.assertAlmostEqual(window_db(stem, 2.0, 5.0), -31.0, delta=0.5)

    def test_impulse_response_decays_sixty_db_and_is_decorrelated(self):
        ir = mcs.impulse_response(1.0, np.random.default_rng(3))
        self.assertEqual(ir.shape, (RATE, 2))
        self.assertAlmostEqual(float(np.sum(ir[:, 0] ** 2)), 1.0, places=6)
        early = np.sqrt(np.mean(ir[:4800, 0] ** 2))
        late = np.sqrt(np.mean(ir[-4800:, 0] ** 2))
        # the windows are centred 0.9 s apart, so 0.9 of the 60 dB lie between them
        self.assertAlmostEqual(20 * np.log10(early / late), 54.0, delta=3.0)
        corr = np.corrcoef(ir[:, 0], ir[:, 1])[0, 1]
        self.assertLess(abs(corr), 0.1)

    def test_a_stem_over_zero_dbfs_is_scaled_down_with_a_warning(self):
        moments = with_moment(level_db=0.0, bed_swell_db=0)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            stem, _ = mcs.build_stem(self.plan(moments=moments), fake_decode)
        self.assertAlmostEqual(float(np.max(np.abs(stem))), 1.0, places=5)
        self.assertRegex(err.getvalue(), r"warning: .*scaled the whole stem down by \d+\.\d dB")

    def test_a_quiet_stem_is_left_alone_and_warns_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            stem, _ = mcs.build_stem(self.plan(), fake_decode)
        self.assertLess(float(np.max(np.abs(stem))), 1.0)
        self.assertEqual(err.getvalue(), "")

    def test_bed_loop_has_no_jump_at_the_seam(self):
        sine = tone(4.0, 440, 0.3)
        period = mcs.make_loop(sine)
        wrapped = np.concatenate([period[-5:], period[:5]])[:, 0]
        steps = np.abs(np.diff(wrapped))
        inside = np.abs(np.diff(period[1000:2000, 0] / mcs.rms(period))).max() * mcs.rms(period)
        self.assertLess(float(steps.max()), 3 * float(inside) + 1e-6)

    def test_a_silent_sound_is_refused(self):
        def decode(path, start=0.0, duration=None):
            return np.zeros((RATE, 2), np.float32) if path.endswith("roar.wav") \
                else fake_decode(path, start, duration)
        with self.assertRaises(mcs.ToolError):
            self.build(decode=decode)


class PlanValidationTests(Fixture):
    def test_valid_plan_is_normalised(self):
        plan = self.plan(room=ROOM)
        self.assertEqual(plan["seed"], 7)
        self.assertEqual(plan["bed"]["path"], os.path.join(self.sounds, "bed.wav"))
        self.assertEqual(plan["moments"][0]["sound_start"], 0.5)
        self.assertAlmostEqual(plan["timeline"]["total"], TOTAL)
        self.assertEqual(plan["room"]["rt60"], 1.2)

    def test_defaults(self):
        raw = self.raw()
        del raw["seed"]
        plan = mcs.validate_plan(raw)
        self.assertEqual(plan["seed"], 0)
        self.assertIsNone(plan["room"])
        self.assertEqual(plan["bed"]["follow_db"], 6.0)
        raw["bed"] = {"name": "bed", "level_db": -30}
        raw["moments"] = [{"clip": 0, "at": 1, "sound": "roar", "level_db": -20}]
        moment = mcs.validate_plan(raw)["moments"][0]
        self.assertEqual((moment["rise"], moment["tail"], moment["bed_swell_db"]),
                         (1.2, 3.0, 0.0))
        self.assertIsNone(moment["sound_start"])

    def test_input_is_not_mutated(self):
        raw = self.raw(room=dict(ROOM))
        before = copy.deepcopy(raw)
        mcs.validate_plan(raw)
        self.assertEqual(raw, before)

    def test_unknown_and_missing_fields(self):
        self.assertInvalid("bogus", bogus=1)
        for key in ("spec", "output", "sounds_dir", "bed"):
            raw = self.raw()
            del raw[key]
            with self.assertRaises(mcs.SpecError) as ctx:
                mcs.validate_plan(raw)
            self.assertIn(key, str(ctx.exception))
        self.assertInvalid("typo", bed=with_bed(typo=1))
        self.assertInvalid("typo", room={**ROOM, "typo": 1})
        self.assertInvalid("typo", moments=with_moment(typo=1))
        self.assertInvalid("missing level_db", bed={"name": "bed"})
        self.assertInvalid("missing name", bed={"level_db": -30})
        for key in ("clip", "at", "sound", "level_db"):
            moment = PLAN["moments"][0].copy()
            del moment[key]
            self.assertInvalid(key, moments=[moment])

    def test_not_an_object(self):
        with self.assertRaises(mcs.SpecError):
            mcs.validate_plan([])
        self.assertInvalid("object", bed="bed")
        self.assertInvalid("object", room="big")
        self.assertInvalid("must be a list", moments={})
        self.assertInvalid("object", moments=["roar"])

    def test_paths(self):
        self.assertInvalid(".wav", output="/o/stem.mp3")
        self.assertInvalid("sounds_dir", sounds_dir=os.path.join(self.dir, "nope"))
        self.assertInvalid("spec not found", spec=os.path.join(self.dir, "nope.json"))
        self.assertInvalid("no sound 'nope'", bed=with_bed(name="nope"))
        self.assertInvalid("no sound 'nope'", moments=with_moment(sound="nope"))
        self.assertInvalid("bare sound name", moments=with_moment(sound="../bed"))

    def test_the_output_may_not_be_a_file_the_build_reads(self):
        for name in ("bed.wav", "roar.wav"):
            self.assertInvalid("output", output=os.path.join(self.sounds, name))
        # a different spelling and a symlink of the same file are the same file
        self.assertInvalid("output", output=os.path.join(self.sounds, "..", "sfx", "bed.wav"))
        link = os.path.join(self.dir, "link.wav")
        os.symlink(os.path.join(self.sounds, "bed.wav"), link)
        self.assertInvalid("output", output=link)
        source = os.path.join(self.dir, "stream.wav")
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            json.dump({**SPEC, "clips": [{**SPEC["clips"][0], "source": source},
                                         SPEC["clips"][1]]}, handle)
        self.assertInvalid("output", output=source)
        self.plan(output=os.path.join(self.dir, "other.wav"))      # an unrelated file is fine

    def test_relative_spec_path_is_tried_from_the_repo_root(self):
        found = mcs._resolve_spec_path("tools/clips/tiktoks/example.json")
        self.assertTrue(os.path.isfile(found))

    def test_spec_errors_pass_through(self):
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            json.dump({**SPEC, "layout": "x"}, handle)
        self.assertInvalid("layout")
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            handle.write("{nope")
        self.assertInvalid("not valid JSON")

    def test_numbers(self):
        self.assertInvalid("seed", seed=-1)
        self.assertInvalid("seed", seed=1.5)
        self.assertInvalid("seed", seed=True)
        self.assertInvalid("seed", seed="7")
        self.assertInvalid("level_db", bed=with_bed(level_db=3))
        self.assertInvalid("level_db", bed=with_bed(level_db=float("nan")))
        self.assertInvalid("follow_db", bed=with_bed(follow_db=-1))
        self.assertInvalid("room wet", room={**ROOM, "wet": 1.5})
        self.assertInvalid("room rt60", room={**ROOM, "rt60": 0})
        self.assertInvalid("room highpass_hz", room={**ROOM, "highpass_hz": 5})
        self.assertInvalid("room lowpass_hz", room={**ROOM, "lowpass_hz": 99999})
        self.assertInvalid("rise", moments=with_moment(rise=0))
        self.assertInvalid("tail", moments=with_moment(tail=100))
        self.assertInvalid("bed_swell_db", moments=with_moment(bed_swell_db=-3))
        self.assertInvalid("sound_start", moments=with_moment(sound_start=-1))
        self.assertInvalid("level_db", moments=with_moment(level_db=1))

    def test_clip_index_and_moment_time(self):
        self.assertInvalid("moment 1 clip", moments=with_moment(clip=2))
        self.assertInvalid("moment 1 clip", moments=with_moment(clip=-1))
        self.assertInvalid("whole number", moments=with_moment(clip=0.5))
        self.assertInvalid("not inside clip 0", moments=with_moment(at=6.0))
        self.assertInvalid("not inside clip 0", moments=with_moment(at=-0.5))
        self.assertInvalid("not inside clip 1", moments=with_moment(clip=1, at=5.5))
        self.plan(moments=with_moment(clip=1, at=4.9))
        self.plan(moments=with_moment(at=0))


class NoBedClipsTests(Fixture):
    """The optional plan field no_bed_clips: a clip listed there has no crowd bed, its moments play."""

    def test_absent_field_is_the_same_as_an_empty_list(self):
        plain, _ = self.build(room=ROOM)
        empty, _ = self.build(room=ROOM, no_bed_clips=[])
        self.assertTrue(np.array_equal(plain, empty))
        self.assertEqual(self.plan()["no_bed_clips"], [])

    def test_skipped_clip_has_no_bed_but_its_moment_still_plays(self):
        bed = with_bed(follow_db=0)
        with_it, lines = self.build(bed=bed)
        without, skipped_lines = self.build(bed=bed, no_bed_clips=[0])
        # clip 0 starts at 0.4 s; its bed alone runs from 0.9 s to 1.3 s (the roar rises from 1.4 s)
        self.assertGreater(window_db(with_it, 0.9, 1.3), -40.0)
        self.assertLess(window_db(without, 0.9, 1.3), -150.0)
        self.assertGreater(window_db(without, 2.2, 2.6), -35.0)
        self.assertIn("no bed", skipped_lines[0])
        self.assertNotIn("no bed", lines[0])

    def test_samples_outside_the_skipped_clip_are_identical(self):
        for room in (None, ROOM):
            with self.subTest(room=room):
                plain, _ = self.build(room=room)
                skipped, _ = self.build(room=room, no_bed_clips=[0])
                first, end = round(CLIP1_START * RATE), round(6.4 * RATE)
                self.assertTrue(np.array_equal(plain[:first], skipped[:first]))
                self.assertTrue(np.array_equal(plain[end:], skipped[end:]))
                self.assertFalse(np.array_equal(plain[first:end], skipped[first:end]))

    def test_the_last_clip_is_unchanged_by_skipping_an_earlier_one(self):
        # clip 1 (the second clip) draws its bed offset from the same seed in both builds
        plain, _ = self.build(room=ROOM)
        skipped, _ = self.build(room=ROOM, no_bed_clips=[0])
        self.assertTrue(np.array_equal(plain[round(CLIP2_START * RATE):],
                                       skipped[round(CLIP2_START * RATE):]))

    def test_no_bed_clips_validation(self):
        self.assertInvalid("must be a list of ints", no_bed_clips={0: 1})
        self.assertInvalid("must be a list of ints", no_bed_clips=0)
        self.assertInvalid("must be a list of ints", no_bed_clips=[0.0])
        self.assertInvalid("must be a list of ints", no_bed_clips=["0"])
        self.assertInvalid("must be a list of ints", no_bed_clips=[True])
        self.assertInvalid("not a clip index", no_bed_clips=[2])
        self.assertInvalid("not a clip index", no_bed_clips=[-1])
        self.assertInvalid("listed twice", no_bed_clips=[1, 1])
        self.assertEqual(self.plan(no_bed_clips=[1, 0])["no_bed_clips"], [1, 0])


# SPEC with a 4 s hook: hook 0-4, card 4-4.4, clip 1 4.4-10.4, card 10.4-10.8, clip 2 10.8-15.8.
# The hook is cut from source second 33, where the synthetic commentary starts shouting at 33.5.
HOOK_SPEC = {**SPEC, "hook": {"source": "/v/stream.mp4", "start": 33, "end": 37}}
HOOK_TOTAL, HOOK_CLIP1_START, HOOK_CLIP2_START = 15.8, 4.4, 10.8
HOOK_MOMENT = {"clip": "hook", "at": 2.0, "sound": "roar", "sound_start": 0.5, "level_db": -20,
               "rise": 1.0, "tail": 1.5, "bed_swell_db": 10}


class HookFixture(Fixture):
    def setUp(self):
        super().setUp()
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            json.dump(HOOK_SPEC, handle)


class HookPlanTests(HookFixture):
    """The hook is referred to as "hook" where a clip is referred to by index."""

    def test_moment_and_no_bed_clips_accept_hook(self):
        plan = self.plan(moments=[HOOK_MOMENT], no_bed_clips=["hook", 1])
        self.assertEqual(plan["moments"][0]["clip"], "hook")
        self.assertEqual(plan["no_bed_clips"], ["hook", 1])
        self.assertEqual(plan["timeline"]["segments"][0]["clip"], "hook")

    def test_integer_indexes_still_count_the_clips_only(self):
        self.assertEqual(self.plan(moments=with_moment(clip=1))["moments"][0]["clip"], 1)
        self.assertInvalid("clip", moments=with_moment(clip=2))
        self.assertInvalid("not a clip index", no_bed_clips=[2])

    def test_moment_must_be_inside_the_hook(self):
        self.assertInvalid("not inside the hook (4.00 s)",
                           moments=[{**HOOK_MOMENT, "at": 4.0}])
        self.assertInvalid("not inside the hook", moments=[{**HOOK_MOMENT, "at": -0.1}])
        self.plan(moments=[{**HOOK_MOMENT, "at": 3.9}])

    def test_other_names_are_not_the_hook(self):
        for bad in ("Hook", "clip", "", "0", None, True, 1.5, ["hook"]):
            self.assertInvalid("clip", moments=[{**HOOK_MOMENT, "clip": bad}])
        for bad in (["Hook"], ["0"], [None], [["hook"]]):
            self.assertInvalid("must be a list of ints", no_bed_clips=bad)

    def test_hook_listed_twice(self):
        self.assertInvalid("listed twice", no_bed_clips=["hook", "hook"])

    def test_output_may_not_overwrite_the_hook_source(self):
        source = os.path.join(self.dir, "stream.wav")
        with open(self.spec_path, "w", encoding="utf-8") as handle:
            json.dump({**HOOK_SPEC, "hook": {**HOOK_SPEC["hook"], "source": source}}, handle)
        self.assertInvalid("source of the hook", output=source)


class HookWithoutHookInSpecTests(Fixture):
    def test_hook_is_refused_when_the_spec_has_none(self):
        self.assertInvalid("the spec has no hook", moments=[HOOK_MOMENT])
        self.assertInvalid("the spec has no hook", no_bed_clips=["hook"])


class HookStemTests(HookFixture):
    def test_length_cards_and_summary_lines(self):
        stem, lines = self.build(room=ROOM, moments=[HOOK_MOMENT] + PLAN["moments"])
        self.assertEqual(stem.shape, (round(HOOK_TOTAL * RATE), 2))
        for start, end in ((4.0, 4.4), (10.4, 10.8)):
            self.assertTrue(np.all(stem[round(start * RATE):round(end * RATE)] == 0), start)
        self.assertTrue(np.any(stem[round(1 * RATE):round(3.5 * RATE)] != 0))
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("hook:"), lines[0])
        self.assertTrue(lines[1].startswith("clip 1:"), lines[1])
        self.assertTrue(lines[2].startswith("clip 2:"), lines[2])

    def test_hook_moment_swells_the_hook_not_a_clip(self):
        bed = with_bed(follow_db=0)
        quiet, _ = self.build(bed=bed, moments=[])
        loud, _ = self.build(bed=bed, moments=[HOOK_MOMENT])
        self.assertGreater(window_db(loud, 2.0, 2.3), window_db(quiet, 2.0, 2.3) + 6.0)
        first, end = round(HOOK_CLIP1_START * RATE), len(quiet)
        self.assertTrue(np.array_equal(loud[first:end], quiet[first:end]))

    def test_clip_moments_are_shifted_by_the_hook_and_its_card(self):
        bed = with_bed(follow_db=0)
        quiet, _ = self.build(bed=bed, moments=[])
        loud, _ = self.build(bed=bed, moments=with_moment(clip=0, at=2.0))
        at = HOOK_CLIP1_START + 2.0
        self.assertGreater(window_db(loud, at, at + 0.3), window_db(quiet, at, at + 0.3) + 6.0)
        self.assertTrue(np.array_equal(loud[:round(HOOK_CLIP1_START * RATE)],
                                       quiet[:round(HOOK_CLIP1_START * RATE)]))

    def test_hook_without_bed_keeps_its_moments(self):
        bed = with_bed(follow_db=0)
        stem, lines = self.build(bed=bed, moments=[HOOK_MOMENT], no_bed_clips=["hook"])
        self.assertLess(window_db(stem, 0.1, 0.8), -150.0)
        self.assertGreater(window_db(stem, 2.0, 2.3), -40.0)
        self.assertGreater(window_db(stem, 5.0, 6.0), -40.0)       # clip 1 keeps its bed
        self.assertIn("no bed", lines[0])
        self.assertNotIn("no bed", lines[1])

    def test_hook_bed_follows_the_hook_commentary(self):
        calls = []

        def decode(path, start=0.0, duration=None):
            calls.append((os.path.basename(path), start, duration))
            return fake_decode(path, start, duration)

        stem, _ = self.build(decode=decode, moments=[], bed=with_bed(follow_db=8))
        self.assertIn(("stream.mp4", 33.0, 4.0), calls)
        self.assertGreater(window_db(stem, 1.0, 1.9), window_db(stem, 0.1, 0.45) + 3.0)

    def test_hook_and_clips_start_at_different_points_of_the_bed(self):
        plan = self.plan()
        builder = mcs.StemBuilder(plan, fake_decode)
        self.assertEqual(len(builder.offsets), 3)
        self.assertEqual(len(set(builder.offsets)), 3)
        stem, _ = self.build(moments=[], bed=with_bed(follow_db=0))
        a = stem[round(1 * RATE):round(1.2 * RATE)]
        b = stem[round(5.4 * RATE):round(5.6 * RATE)]
        self.assertFalse(np.array_equal(a, b))


class CommandLineTests(Fixture):
    def write_plan(self, **changes):
        path = os.path.join(self.dir, "plan.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.raw(**changes), handle)
        return path

    def run_main(self, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mcs.main(argv, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_bad_plan_exits_non_zero_with_a_message(self):
        code, _, err = self.run_main([self.write_plan(bogus=1)], decode=fake_decode)
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertIn("bogus", err)

    def test_missing_plan_file(self):
        code, _, err = self.run_main([os.path.join(self.dir, "none.json")], decode=fake_decode)
        self.assertEqual(code, 1)
        self.assertIn("cannot read plan", err)

    def test_decode_failure_is_reported_not_raised(self):
        def decode(path, start=0.0, duration=None):
            raise mcs.ToolError("ffmpeg could not decode " + path)
        code, _, err = self.run_main([self.write_plan()], decode=decode)
        self.assertEqual(code, 1)
        self.assertIn("could not decode", err)

    def test_success_writes_the_stem_and_prints_one_line_per_clip(self):
        written = {}
        code, out, _ = self.run_main(
            [self.write_plan()], decode=fake_decode,
            write=lambda path, stem: written.update(path=path, shape=stem.shape))
        self.assertEqual(code, 0)
        self.assertEqual(written["path"], os.path.join(self.dir, "stem.wav"))
        self.assertEqual(written["shape"], (round(TOTAL * RATE), 2))
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("clip ")), 2)

    @unittest.skipUnless(FFMPEG_OK, "ffmpeg and ffprobe are needed")
    def test_real_ffmpeg_pipes_round_trip_and_the_file_has_the_planned_length(self):
        code, _, err = self.run_main([self.write_plan(room=ROOM)], decode=fake_decode)
        self.assertEqual(code, 0, err)
        out = os.path.join(self.dir, "stem.wav")
        self.assertEqual(os.listdir(self.dir).count("stem.wav.part"), 0)
        length = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
             "default=nw=1:nk=1", out], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(length, TOTAL, delta=0.002)
        back = mcs.decode_audio(out)
        stem, _ = self.build(room=ROOM)
        self.assertEqual(back.shape, stem.shape)
        self.assertTrue(np.allclose(back, stem, atol=1e-6))

    def test_failed_write_is_a_tool_error_and_leaves_no_partial_file(self):
        target = os.path.join(self.dir, "sub", "stem.wav")

        def failing(cmd, **kwargs):
            open(cmd[-1], "wb").close()     # ffmpeg had started the temp file
            return subprocess.CompletedProcess(cmd, 1, b"", b"disk on fire")

        with mock.patch.object(mcs.subprocess, "run", failing):
            with self.assertRaises(mcs.ToolError) as ctx:
                mcs.write_wav(target, np.zeros((10, 2), np.float32))
        self.assertIn("disk on fire", str(ctx.exception))
        self.assertEqual(os.listdir(os.path.join(self.dir, "sub")), [])

    def test_decode_audio_maps_the_first_audio_stream_like_the_render(self):
        seen = []

        def spy(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        with mock.patch.object(mcs.subprocess, "run", spy):
            mcs.decode_audio("/v/stream.mp4", 10.0, 6.0)
        cmd = seen[0]
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:a:0")
        self.assertGreater(cmd.index("-map"), cmd.index("-i"))     # an output option

    @unittest.skipUnless(FFMPEG_OK, "ffmpeg and ffprobe are needed")
    def test_decode_audio_reads_stream_zero_even_when_another_is_flagged_default(self):
        path = os.path.join(self.dir, "two.mka")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
                        "-f", "lavfi", "-i", "sine=f=440:d=0.3:sample_rate=48000,volume=2",
                        "-f", "lavfi", "-i", "sine=f=440:d=0.3:sample_rate=48000,volume=8,"
                        "pan=stereo|c0=c0|c1=c0", "-map", "0", "-map", "1",
                        "-disposition:a:0", "0", "-disposition:a:1", "default",   # ffmpeg's pick
                        "-c:a", "pcm_s16le", path], check=True)
        peak = float(np.max(np.abs(mcs.decode_audio(path))))
        self.assertTrue(0.1 < peak < 0.5, peak)    # stream 0 (quiet), not the loud default one

    @unittest.skipUnless(FFMPEG_OK, "ffmpeg and ffprobe are needed")
    def test_decode_audio_error_is_a_tool_error(self):
        with self.assertRaises(mcs.ToolError):
            mcs.decode_audio(os.path.join(self.dir, "missing.wav"))


class ExamplePlanTests(Fixture):
    """tiktoks/example-crowd-plan.json is the plan the README tells people to copy."""

    PATH = os.path.join(HERE, "tiktoks", "example-crowd-plan.json")

    def test_the_example_plan_is_valid_once_its_sounds_exist(self):
        with open(self.PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
        for name in ("crowd_bed", "crowd_roar"):
            open(os.path.join(self.sounds, name + ".wav"), "wb").close()
        raw["sounds_dir"] = self.sounds
        raw["output"] = os.path.join(self.dir, "stem.wav")
        plan = mcs.validate_plan(raw)
        self.assertEqual([m["clip"] for m in plan["moments"]], ["hook", 0])

    def test_the_example_plan_uses_placeholder_paths_only(self):
        with open(self.PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
        for key in ("output", "sounds_dir"):
            self.assertTrue(raw[key].startswith("/path/to/"), raw[key])


if __name__ == "__main__":
    unittest.main()
