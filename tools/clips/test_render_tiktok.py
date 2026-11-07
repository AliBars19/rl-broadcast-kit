"""Unit tests for render_tiktok (run with python3 -I -m unittest -v test_render_tiktok).

Nothing here encodes video: command builders are checked by their exact argument lists and
ffprobe is replaced by a stub.
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from array import array
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import render_tiktok as rt  # noqa: E402

FONT = rt.FONT_PATH
TEXT_FILTER = ("drawtext=fontfile=" + FONT + ":textfile=/tmp/t/caption.txt:expansion=none"
               ":fontsize=64:fontcolor=white:borderw=4:bordercolor=black"
               ":x=(w-text_w)/2:y=640")
BASE = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-filter_threads", "4", "-filter_complex_threads", "4"]
ENCODE = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "12M",
          "-maxrate", "16M", "-bufsize", "24M", "-bf", "0", "-g", "60",
          "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "60"]
PCM = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
RS = "aresample=48000,aformat=channel_layouts=stereo,"   # every mix input is made 48 kHz stereo
CARD_TRIM = "[{n}:a]" + RS + "atrim=end=0.750,afade=t=out:st=0.745:d=0.005,"

BASE_SPEC = {
    "caption": "Defense.",
    "output": "/videos/out/defense.mp4",
    "card_seconds": 0.75,
    "opening_card": True,
    "music": None,
    "clips": [
        {"source": "/videos/stream.mp4", "start": "00:27:33", "end": "00:27:42",
         "sfx": [{"at": 6.2, "name": "impact", "gain_db": 0}]},
        {"source": "/videos/stream.mp4", "start": "01:23:26", "end": "01:23:35",
         "sfx": [{"at": 6, "name": "impact"}]},
    ],
}


def spec(**changes):
    out = copy.deepcopy(BASE_SPEC)
    out.update(changes)
    return out


class SfxDirCase(unittest.TestCase):
    """A temp sfx folder with a few placeholder WAV names."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.sfx_dir = self._dir.name
        for name in ("card_hit", "impact", "whoosh", "riser", "hit"):
            open(os.path.join(self.sfx_dir, name + ".wav"), "wb").close()

    def validate(self, raw):
        return rt.validate_spec(raw, sfx_dir=self.sfx_dir)

    def assertInvalid(self, bad, needle):
        with self.assertRaises(rt.SpecError) as ctx:
            self.validate(bad)
        self.assertIn(needle, str(ctx.exception))


class TimelineTests(SfxDirCase):
    def test_with_opening_card(self):
        tl = rt.build_timeline(self.validate(BASE_SPEC))
        kinds = [(s["kind"], s["start"], s["duration"]) for s in tl["segments"]]
        self.assertEqual(kinds, [("card", 0.0, 0.75), ("clip", 0.75, 9.0),
                                 ("card", 9.75, 0.75), ("clip", 10.5, 9.0)])
        self.assertAlmostEqual(tl["total"], 19.5)

    def test_without_opening_card(self):
        tl = rt.build_timeline(self.validate(spec(opening_card=False)))
        kinds = [(s["kind"], s["start"]) for s in tl["segments"]]
        self.assertEqual(kinds, [("clip", 0.0), ("card", 9.0), ("clip", 9.75)])
        self.assertAlmostEqual(tl["total"], 18.75)

    def test_no_card_after_last_clip(self):
        tl = rt.build_timeline(self.validate(BASE_SPEC))
        self.assertEqual(tl["segments"][-1]["kind"], "clip")

    def test_events_have_absolute_times(self):
        tl = rt.build_timeline(self.validate(BASE_SPEC))
        got = [(round(e["t"], 3), e["name"]) for e in tl["events"]]
        self.assertEqual(got, [(0.0, "card_hit"), (6.95, "impact"), (9.75, "card_hit"),
                               (16.5, "impact")])

    def test_events_without_opening_card(self):
        tl = rt.build_timeline(self.validate(spec(opening_card=False)))
        got = [(round(e["t"], 3), e["name"]) for e in tl["events"]]
        self.assertEqual(got, [(6.2, "impact"), (9.0, "card_hit"), (15.75, "impact")])

    def test_card_hit_gain_is_the_constant(self):
        tl = rt.build_timeline(self.validate(BASE_SPEC))
        hits = [e for e in tl["events"] if e["name"] == "card_hit"]
        self.assertTrue(hits and all(e["gain_db"] == rt.CARD_HIT_GAIN_DB for e in hits))

    def test_negative_sfx_begins_over_the_card(self):
        raw = spec()
        raw["clips"][1]["sfx"] = [{"at": -0.5, "name": "riser"}]
        tl = rt.build_timeline(self.validate(raw))
        riser = [e for e in tl["events"] if e["name"] == "riser"][0]
        self.assertAlmostEqual(riser["t"], 10.0)

    def test_clip_lengths_snap_to_frames(self):
        raw = spec()
        raw["clips"][0]["end"] = "00:27:42.008"
        tl = rt.build_timeline(self.validate(raw))
        self.assertAlmostEqual(tl["segments"][1]["duration"], 9.0)
        raw["clips"][0]["end"] = "00:27:42.02"
        tl = rt.build_timeline(self.validate(raw))
        self.assertAlmostEqual(tl["segments"][1]["duration"], 541 / 60)

    def test_format_timeline_lists_every_item(self):
        text = "\n".join(rt.format_timeline(rt.build_timeline(self.validate(BASE_SPEC))))
        self.assertEqual(text.count("card"), 2 + 2)  # two card segments, two card_hit events
        self.assertIn("impact", text)
        self.assertIn("00:06.950", text)


HOOK = {"source": "/videos/stream.mp4", "start": "00:10:00", "end": "00:10:05",
        "sfx": [{"at": 3.0, "name": "impact"}]}   # 5 s


def hook_spec(hook=None, **changes):
    return spec(hook=copy.deepcopy(HOOK if hook is None else hook), **changes)


class HookValidationTests(SfxDirCase):
    """The optional top level hook: a clip object checked like an entry of clips."""

    def test_normalised_like_a_clip(self):
        got = self.validate(hook_spec())["hook"]
        want = self.validate(spec(clips=[HOOK]))["clips"][0]
        self.assertEqual(got, want)
        self.assertEqual(got["length"], 5.0)

    def test_absent_and_null_are_no_hook(self):
        self.assertIsNone(self.validate(BASE_SPEC)["hook"])
        self.assertIsNone(self.validate(spec(hook=None))["hook"])

    def test_must_be_an_object(self):
        for bad in ([], "x", 3, [HOOK]):
            self.assertInvalid(spec(hook=bad), "hook must be an object")

    def test_unknown_and_missing_fields_name_the_hook(self):
        self.assertInvalid(hook_spec({**HOOK, "fade": 1}), "hook: unknown field(s) fade")
        for key in ("source", "start", "end"):
            bad = {k: v for k, v in HOOK.items() if k != key}
            self.assertInvalid(hook_spec(bad), f"hook: missing {key}")

    def test_length_rules_are_the_clip_rules(self):
        self.assertInvalid(hook_spec({**HOOK, "end": "00:10:00.5"}), "hook: must be at least")
        self.assertInvalid(hook_spec({**HOOK, "end": "00:09:00"}), "hook: end must be after")
        self.assertInvalid(hook_spec({**HOOK, "end": "00:12:00"}), "hook: at most")

    def test_cam_windows_are_checked_against_the_hook_length(self):
        ok = {**HOOK, "cam": [{"start": 2, "end": 4.5, "mode": "inset"}]}
        got = self.validate(hook_spec(ok, layout="kit"))["hook"]["cam"]
        self.assertEqual(got[0]["end"], 4.5)
        bad = {**HOOK, "cam": [{"start": 2, "end": 6, "mode": "inset"}]}
        self.assertInvalid(hook_spec(bad, layout="kit"),
                           "hook cam window 1: end 6 is past the 5.00 s")

    def test_cam_needs_a_cam_layout(self):
        bad = {**HOOK, "cam": [{"start": 1, "end": 3, "mode": "full"}]}
        self.assertInvalid(hook_spec(bad, layout="kit-reaction"), "hook: cam windows need")

    def test_own_layout_decides_the_cam_geometry(self):
        cam = [{"start": 1, "end": 3, "mode": "full", "x": 290}]    # kit reaches 290, bottom-cam 202
        kit = {**HOOK, "layout": "kit", "cam": cam}
        self.assertEqual(self.validate(hook_spec(kit, layout="bottom-cam"))["hook"]["cam"][0]["x"],
                         290)
        bottom_cam = {**HOOK, "layout": "bottom-cam", "cam": cam}
        self.assertInvalid(hook_spec(bottom_cam, layout="kit"), "hook cam window 1")

    def test_sfx_errors_name_the_hook(self):
        bad = {**HOOK, "sfx": [{"at": 3, "name": "nope"}]}
        self.assertInvalid(hook_spec(bad), "hook sfx 1")
        late = {**HOOK, "sfx": [{"at": 5, "name": "impact"}]}
        self.assertInvalid(hook_spec(late), "hook sfx 'impact': at 5 is not inside")

    def test_sfx_cannot_begin_before_the_video(self):
        early = {**HOOK, "sfx": [{"at": -0.5, "name": "riser"}]}
        self.assertInvalid(hook_spec(early), "hook sfx 'riser': at -0.5 would begin before")

    def test_clip_one_may_pre_roll_over_the_card_after_the_hook(self):
        raw = hook_spec()
        raw["clips"][0]["sfx"] = [{"at": -0.5, "name": "riser"}]
        tl = rt.build_timeline(self.validate(raw))
        riser = [e for e in tl["events"] if e["name"] == "riser"][0]
        self.assertAlmostEqual(riser["t"], 5.25)

    def test_output_may_not_overwrite_the_hook_source(self):
        self.assertInvalid(hook_spec({**HOOK, "source": "/videos/out/defense.mp4"}),
                           "would overwrite the input /videos/out/defense.mp4")

    def test_max_clips_counts_clips_only(self):
        clips = [copy.deepcopy(BASE_SPEC["clips"][0]) for _ in range(rt.MAX_CLIPS)]
        self.assertEqual(len(self.validate(hook_spec(clips=clips))["clips"]), rt.MAX_CLIPS)

    def test_input_is_not_mutated(self):
        raw = hook_spec()
        before = copy.deepcopy(raw)
        self.validate(raw)
        self.assertEqual(raw, before)


class HookTimelineTests(SfxDirCase):
    def timeline(self, **changes):
        return rt.build_timeline(self.validate(hook_spec(**changes)))

    def test_hook_then_card_then_clips_with_opening_card(self):
        tl = self.timeline()
        got = [(s["kind"], s["start"], s["duration"], s.get("clip")) for s in tl["segments"]]
        self.assertEqual(got, [("clip", 0.0, 5.0, "hook"), ("card", 5.0, 0.75, None),
                               ("clip", 5.75, 9.0, 0), ("card", 14.75, 0.75, None),
                               ("clip", 15.5, 9.0, 1)])
        self.assertAlmostEqual(tl["total"], 24.5)

    def test_a_hook_is_always_followed_by_a_card_even_without_opening_card(self):
        with_card = self.timeline()
        without = self.timeline(opening_card=False)
        self.assertEqual(without["segments"], with_card["segments"])
        self.assertEqual(without["events"], with_card["events"])
        self.assertAlmostEqual(without["total"], 24.5)

    def test_events_are_absolute_and_tagged(self):
        got = [(round(e["t"], 3), e["name"], e.get("clip")) for e in self.timeline()["events"]]
        self.assertEqual(got, [(3.0, "impact", "hook"), (5.0, "card_hit", None),
                               (11.95, "impact", 0), (14.75, "card_hit", None),
                               (21.5, "impact", 1)])

    def test_hook_counts_toward_the_total(self):
        plain = rt.build_timeline(self.validate(BASE_SPEC))["total"]
        # the card after the hook is the opening card, so only the hook's 5 s are added
        self.assertAlmostEqual(self.timeline()["total"], plain + 5.0)

    def test_cam_windows_in_the_hook(self):
        hook = {**HOOK, "cam": [{"start": 2, "end": 4, "mode": "full"}]}
        cams = rt.build_timeline(self.validate(hook_spec(hook, layout="kit")))["cams"]
        self.assertEqual([(c["clip"], c["mode"], c["x"], round(c["t"], 3), c["length"])
                          for c in cams], [("hook", "full", 162, 2.0, 2.0)])

    def test_beds_under_the_hook_and_every_clip_never_the_card(self):
        tl = self.timeline(crowd_bed={"name": "hit"})
        got = [(b["clip"], b["t"], b["length"], round(b["offset"], 1)) for b in tl["beds"]]
        self.assertEqual(got, [("hook", 0.0, 5.0, 0.0), (0, 5.75, 9.0, 7.3),
                               (1, 15.5, 9.0, 14.6)])

    def test_format_timeline_labels_the_hook(self):
        lines = rt.format_timeline(self.timeline())
        self.assertEqual(lines[0], "00:00.000  hook  5.000 s")
        self.assertIn("00:05.000  card  0.750 s", lines)
        self.assertIn("00:05.750  clip 1  9.000 s", lines)
        self.assertNotIn("clip 0", "\n".join(lines))

    def test_hook_layout_is_named_when_it_differs(self):
        hook = {**HOOK, "layout": "bottom-cam"}
        tl = rt.build_timeline(self.validate(hook_spec(hook, layout="kit")))
        self.assertIn("hook (bottom-cam)", rt.format_timeline(tl)[0])


class NoHookTimelineTests(SfxDirCase):
    """Without a hook the timeline is exactly what it was before the field existed."""

    RAW = {"caption": "Defense.", "output": "/o/d.mp4", "layout": "kit", "card_seconds": 0.75,
           "opening_card": True, "crowd_bed": {"name": "hit"},
           "clips": [{"source": "/v/s.mp4", "start": "00:27:33", "end": "00:27:42",
                      "sfx": [{"at": 6.2, "name": "impact"}],
                      "cam": [{"start": 2, "end": 4, "mode": "full"}]},
                     {"source": "/v/s.mp4", "start": "01:23:26", "end": "01:23:35",
                      "sfx": [{"at": -0.5, "name": "riser"}], "layout": "bottom-cam"}]}
    SFX_KEYS = ("t", "name", "gain_db", "kind", "clip", "max_len")

    def test_golden_timeline(self):
        tl = rt.build_timeline(self.validate(self.RAW))
        self.assertEqual(tl["segments"], [
            {"kind": "card", "start": 0.0, "duration": 0.75},
            {"kind": "clip", "start": 0.75, "duration": 9.0, "clip": 0},
            {"kind": "card", "start": 9.75, "duration": 0.75},
            {"kind": "clip", "start": 10.5, "duration": 9.0, "clip": 1, "layout": "bottom-cam"}])
        self.assertEqual([(e["t"], e["name"], e["kind"], e.get("clip")) for e in tl["events"]],
                         [(0.0, "card_hit", "card", None), (6.95, "impact", "sfx", 0),
                          (9.75, "card_hit", "card", None), (10.0, "riser", "sfx", 1)])
        self.assertEqual([(b["t"], b["length"], b["clip"], b["offset"]) for b in tl["beds"]],
                         [(0.75, 9.0, 0, 0.0), (10.5, 9.0, 1, 7.3)])
        self.assertEqual(tl["cams"], [{"t": 2.75, "length": 2.0, "clip": 0, "mode": "full",
                                       "x": 162}])
        self.assertEqual((tl["total"], tl["stem"]), (19.5, None))

    def test_null_hook_is_the_same_as_no_hook(self):
        plain = rt.build_timeline(self.validate(self.RAW))
        nulled = rt.build_timeline(self.validate({**self.RAW, "hook": None}))
        self.assertEqual(plain, nulled)

    def test_opening_card_false_still_has_no_card_first(self):
        tl = rt.build_timeline(self.validate({**self.RAW, "opening_card": False}))
        self.assertEqual([s["kind"] for s in tl["segments"]], ["clip", "card", "clip"])


class HookSourceAndRenderTests(SfxDirCase):
    @staticmethod
    def probe(duration=6000.0, width=1920, height=1080):
        return lambda path: {"duration": duration, "width": width, "height": height}

    def check(self, raw, probe=None, wav=lambda p: 1.0):
        return rt.check_sources(self.validate(raw), probe or self.probe(),
                                exists=lambda p: True, wav_duration=wav)

    def test_the_hook_source_is_probed_and_must_hold_the_hook(self):
        calls = []

        def probe(path):
            calls.append(path)
            return {"duration": 6000.0, "width": 1920, "height": 1080}

        self.check(hook_spec({**HOOK, "source": "/videos/other.mp4"}), probe)
        self.assertEqual(sorted(calls), ["/videos/other.mp4", "/videos/stream.mp4"])
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(hook_spec(), self.probe(duration=599.0))
        self.assertIn("hook ends at", str(ctx.exception))

    def test_hook_must_be_1920_by_1080(self):
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(hook_spec(), self.probe(width=1280, height=720))
        self.assertIn("1920x1080", str(ctx.exception))

    def test_sound_running_past_the_video_names_the_hook(self):
        raw = hook_spec(clips=[{**BASE_SPEC["clips"][0], "sfx": []}])
        raw["clips"][0]["end"] = "00:27:34"       # 1 s clip: video ends at 5 + 0.75 + 1 s
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(raw, wav=lambda p: 5.0)    # hook sfx at 3 s lasting 5 s: ends at 8 s
        self.assertIn("hook sound 'impact'", str(ctx.exception))

    def test_scene_commands_start_with_the_hook_clip(self):
        parsed = self.validate(hook_spec())
        tl = rt.build_timeline(parsed)
        with tempfile.TemporaryDirectory() as tmp:
            cmds = list(rt._scene_commands(parsed, tl, tmp))
        self.assertEqual(len(cmds), 5)
        first = cmds[0][1]
        self.assertEqual(first[first.index("-ss") + 1], "600.000")
        self.assertEqual(first[first.index("-t") + 1], "5.000")
        second = cmds[2][1]
        self.assertEqual(second[second.index("-ss") + 1], f"{27 * 60 + 33:.3f}")

    def test_hook_cam_window_reaches_the_filter(self):
        hook = {**HOOK, "cam": [{"start": 2, "end": 4, "mode": "inset"}]}
        parsed = self.validate(hook_spec(hook, layout="kit"))
        with tempfile.TemporaryDirectory() as tmp:
            cmds = list(rt._scene_commands(parsed, rt.build_timeline(parsed), tmp))
        self.assertIn("overlay=24:1190:format=yuv444", filter_of(cmds[0][1]))
        self.assertNotIn("overlay=24:1190", filter_of(cmds[2][1]))


class BudgetTests(unittest.TestCase):
    def test_bounds(self):
        rt.check_budget(15.0)
        rt.check_budget(180.0)

    def test_too_short(self):
        with self.assertRaises(rt.SpecError) as ctx:
            rt.check_budget(14.9)
        self.assertIn("15", str(ctx.exception))

    def test_too_long(self):
        with self.assertRaises(rt.SpecError) as ctx:
            rt.check_budget(180.1)
        self.assertIn("180", str(ctx.exception))

    def test_verify_duration_tolerance(self):
        rt.verify_duration(19.5, 19.74)
        rt.verify_duration(19.5, 19.26)
        with self.assertRaises(rt.ToolError):
            rt.verify_duration(19.5, 19.76)
        with self.assertRaises(rt.ToolError):
            rt.verify_duration(19.5, 19.2)


class ValidationTests(SfxDirCase):
    def test_defaults_and_normalised(self):
        raw = {"caption": "Zen.", "output": "/o/z.mp4",
               "clips": [{"source": "/s.mp4", "start": "10", "end": "20"}]}
        parsed = self.validate(raw)
        self.assertEqual(parsed["card_seconds"], 0.75)
        self.assertTrue(parsed["opening_card"])
        self.assertIsNone(parsed["music"])
        self.assertEqual(parsed["clips"][0]["sfx"], [])
        self.assertAlmostEqual(parsed["clips"][0]["length"], 10.0)

    def test_input_not_mutated(self):
        before = copy.deepcopy(BASE_SPEC)
        self.validate(BASE_SPEC)
        self.assertEqual(BASE_SPEC, before)

    def test_sfx_defaults_gain_zero_and_resolves_path(self):
        sfx = self.validate(BASE_SPEC)["clips"][1]["sfx"][0]
        self.assertEqual(sfx["gain_db"], 0.0)
        self.assertEqual(sfx["path"], os.path.join(self.sfx_dir, "impact.wav"))

    def test_unknown_top_field(self):
        self.assertInvalid(spec(bogus=1), "bogus")

    def test_unknown_clip_field(self):
        bad = spec()
        bad["clips"][0]["typo"] = 1
        self.assertInvalid(bad, "typo")

    def test_unknown_sfx_field(self):
        bad = spec()
        bad["clips"][0]["sfx"][0]["volume"] = 3
        self.assertInvalid(bad, "volume")

    def test_unknown_music_field(self):
        self.assertInvalid(spec(music={"path": "/m.mp3", "loud": 1}), "loud")

    def test_missing_required(self):
        for key in ("caption", "output", "clips"):
            bad = spec()
            del bad[key]
            self.assertInvalid(bad, key)
        bad = spec()
        del bad["clips"][0]["source"]
        self.assertInvalid(bad, "source")

    def test_caption_rules(self):
        self.assertInvalid(spec(caption=""), "caption")
        self.assertInvalid(spec(caption="X" * 25), "24")
        self.assertInvalid(spec(caption=5), "caption")
        self.assertInvalid(spec(caption="A \u2014 B"), "dash")
        self.assertInvalid(spec(caption="A \u2013 B"), "dash")
        self.assertInvalid(spec(caption="a\nb"), "control")
        self.assertEqual(self.validate(spec(caption="X" * 24))["caption"], "X" * 24)

    def test_output_must_be_mp4(self):
        self.assertInvalid(spec(output="/o/x.mov"), ".mp4")

    def test_card_seconds_range(self):
        self.assertInvalid(spec(card_seconds=0.39), "card_seconds")
        self.assertInvalid(spec(card_seconds=1.01), "card_seconds")
        self.assertInvalid(spec(card_seconds="0.7"), "card_seconds")
        self.assertInvalid(spec(card_seconds=True), "card_seconds")
        self.assertAlmostEqual(self.validate(spec(card_seconds=0.4))["card_seconds"], 0.4)
        self.assertAlmostEqual(self.validate(spec(card_seconds=1.0))["card_seconds"], 1.0)

    def test_opening_card_must_be_bool(self):
        self.assertInvalid(spec(opening_card="yes"), "opening_card")

    def test_clip_count(self):
        self.assertInvalid(spec(clips=[]), "1 to 20")
        many = [copy.deepcopy(BASE_SPEC["clips"][0]) for _ in range(21)]
        self.assertInvalid(spec(clips=many), "1 to 20")

    def test_clip_length_bounds(self):
        bad = spec()
        bad["clips"][0]["end"] = "00:27:33.5"
        self.assertInvalid(bad, "at least")
        bad["clips"][0]["end"] = "00:28:49"   # 76 s
        self.assertInvalid(bad, "at most 75")
        ok = spec()
        ok["clips"][0]["end"] = "00:28:48"    # exactly 75 s
        self.assertEqual(self.validate(ok)["clips"][0]["length"], 75.0)
        bad["clips"][0]["end"] = "00:27:30"
        self.assertInvalid(bad, "clip 1")

    def test_timestamp_forms(self):
        raw = spec()
        raw["clips"][0].update(start="27:33", end=1672)
        self.assertAlmostEqual(self.validate(raw)["clips"][0]["length"], 19.0)

    def test_sfx_at_must_fall_inside_clip(self):
        bad = spec()
        bad["clips"][0]["sfx"][0]["at"] = 9.0
        self.assertInvalid(bad, "inside")
        bad["clips"][0]["sfx"][0]["at"] = 12
        self.assertInvalid(bad, "inside")

    def test_sfx_negative_at_limit(self):
        ok = spec()
        ok["clips"][1]["sfx"][0]["at"] = -1.0
        self.assertAlmostEqual(self.validate(ok)["clips"][1]["sfx"][0]["at"], -1.0)
        bad = spec()
        bad["clips"][1]["sfx"][0]["at"] = -1.01
        self.assertInvalid(bad, "-1.0")

    def test_sfx_before_start_of_video_rejected(self):
        # opening card 0.75: -0.5 starts at 0.25 (ok), but -1.0 would start at -0.25
        ok = spec()
        ok["clips"][0]["sfx"][0]["at"] = -0.5
        self.validate(ok)
        bad = spec()
        bad["clips"][0]["sfx"][0]["at"] = -1.0
        self.assertInvalid(bad, "before the start of the video")
        noopen = spec(opening_card=False)
        noopen["clips"][0]["sfx"][0]["at"] = -0.1
        self.assertInvalid(noopen, "before the start of the video")

    def test_sfx_negative_in_later_clip_is_fine_without_opening_card(self):
        raw = spec(opening_card=False)
        raw["clips"][1]["sfx"][0]["at"] = -0.5
        self.validate(raw)

    def test_unknown_sfx_name(self):
        bad = spec()
        bad["clips"][0]["sfx"][0]["name"] = "nope"
        self.assertInvalid(bad, "nope")

    def test_sfx_name_cannot_be_a_path(self):
        for name in ("../impact", "sub/impact", "impact.wav", ""):
            bad = spec()
            bad["clips"][0]["sfx"][0]["name"] = name
            self.assertInvalid(bad, "name")

    def test_gain_range(self):
        for gain, ok in ((-30, True), (6, True), (-30.1, False), (6.1, False)):
            raw = spec()
            raw["clips"][0]["sfx"][0]["gain_db"] = gain
            if ok:
                self.validate(raw)
            else:
                self.assertInvalid(raw, "gain_db")

    def test_music_defaults_and_checks(self):
        parsed = self.validate(spec(music={"path": "/m/track.mp3"}))
        self.assertEqual(parsed["music"], {"path": "/m/track.mp3", "gain_db": -16.0,
                                           "start": 0.0})
        self.assertInvalid(spec(music={"gain_db": -10}), "path")
        self.assertInvalid(spec(music={"path": "/m.mp3", "gain_db": 5}), "gain_db")
        self.assertInvalid(spec(music={"path": "/m.mp3", "start": -1}), "start")
        self.assertInvalid(spec(music="song.mp3"), "music")

    def test_not_an_object(self):
        self.assertInvalid([], "object")


class SourceCheckTests(SfxDirCase):
    @staticmethod
    def probe(duration=6000.0, width=1920, height=1080):
        return lambda path: {"duration": duration, "width": width, "height": height}

    def check(self, raw, probe=None, exists=lambda p: True, wav=lambda p: 1.0):
        rt.check_sources(self.validate(raw), probe or self.probe(), exists=exists,
                         wav_duration=wav)

    def test_ok(self):
        self.check(BASE_SPEC)

    def test_clip_must_end_inside_source(self):
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(BASE_SPEC, self.probe(duration=1650.0))
        self.assertIn("clip 1", str(ctx.exception))

    def test_missing_source(self):
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(BASE_SPEC, exists=lambda p: False)
        self.assertIn("not found", str(ctx.exception))

    def test_wrong_resolution(self):
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(BASE_SPEC, self.probe(width=1280, height=720))
        self.assertIn("1920x1080", str(ctx.exception))

    def test_missing_music_file(self):
        raw = spec(music={"path": "/m/track.mp3"})
        with self.assertRaises(rt.SpecError):
            self.check(raw, exists=lambda p: p != "/m/track.mp3")

    def test_each_source_probed_once(self):
        calls = []

        def probe(path):
            calls.append(path)
            return {"duration": 6000.0, "width": 1920, "height": 1080}

        self.check(BASE_SPEC, probe)
        self.assertEqual(calls, ["/videos/stream.mp4"])


    def test_sfx_running_past_the_end_of_the_video_is_refused(self):
        # clip 2 spans 10.5 to 19.5 s; an sfx at 6 s lasting 3.6 s ends at 20.1 s
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(BASE_SPEC, wav=lambda p: 3.6)
        message = str(ctx.exception)
        self.assertIn("clip 2", message)
        self.assertIn("impact", message)
        self.assertIn("19.50", message)

    def test_sfx_ending_exactly_at_the_end_is_fine(self):
        self.check(BASE_SPEC, wav=lambda p: 3.0)

    def test_sfx_running_into_the_next_clip_is_fine(self):
        # clip 1 sfx at 6 s lasting 8 s runs over the card and into clip 2: not past the end
        raw = spec()
        raw["clips"][1]["sfx"] = []
        self.check(raw, wav=lambda p: 8.0)

    def test_each_sound_probed_once_per_file(self):
        calls = []
        self.check(BASE_SPEC, wav=lambda p: calls.append(p) or 1.0)
        self.assertEqual(calls, [os.path.join(self.sfx_dir, "impact.wav")])


class CommandTests(SfxDirCase):
    def setUp(self):
        super().setUp()
        self.spec = self.validate(BASE_SPEC)

    def test_clip_command_exact(self):
        cmd = rt.clip_command(self.spec["clips"][0], "/tmp/t/caption.txt", "/tmp/t/c00.mov",
                              layout="bottom-cam")
        video = ("[0:v]fps=60,split=2[a][b];"
                 "[a]crop=1380:126:270:0,scale=1080:99:flags=lanczos[s];"
                 "[b]crop=948:839:486:126,scale=1080:956:flags=lanczos[g];"
                 "[s][g]vstack=inputs=2,pad=1080:1920:0:432:black,setsar=1," + TEXT_FILTER +
                 ",format=yuv420p[v]")
        audio = ("[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                 "afade=t=in:d=0.05,afade=t=out:st=8.940:d=0.06,apad=whole_dur=9.000[a]")
        self.assertEqual(cmd, BASE + [
            "-threads", "4", "-ss", "1653.000", "-i", "/videos/stream.mp4",
            "-t", "9.000", "-filter_complex", video + ";" + audio,
            "-map", "[v]", "-map", "[a]"] + ENCODE + PCM + ["-threads", "4", "/tmp/t/c00.mov"])

    def test_card_command_exact(self):
        cmd = rt.card_command(0.75, "/tmp/t/caption.txt", "/tmp/t/k00.mov")
        self.assertEqual(cmd, BASE + [
            "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=60",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", "0.750", "-filter_complex",
            "[0:v]" + TEXT_FILTER + ",format=yuv420p[v]",
            "-map", "[v]", "-map", "1:a"] + ENCODE + PCM + ["-threads", "4", "/tmp/t/k00.mov"])

    def test_concat_list_and_command(self):
        self.assertEqual(rt.concat_list(["/a/x.mov", "/a/it's.mov"]),
                         "file '/a/x.mov'\nfile '/a/it'\\''s.mov'\n")
        self.assertEqual(rt.concat_command("/t/list.txt", "/t/all.mov"), BASE + [
            "-f", "concat", "-safe", "0", "-i", "/t/list.txt", "-c", "copy", "/t/all.mov"])

    def test_mix_command_without_music(self):
        tl = rt.build_timeline(self.spec)
        cmd = rt.mix_command("/t/all.mov", tl, None, "/o/out.part.mp4")
        card = os.path.join(rt.SFX_DIR, "card_hit.wav")
        imp = os.path.join(self.sfx_dir, "impact.wav")
        self.assertEqual(cmd[:len(BASE)], BASE)
        self.assertEqual(cmd[len(BASE):len(BASE) + 10],
                         ["-i", "/t/all.mov", "-i", card, "-i", imp, "-i", card, "-i", imp])
        fc = (CARD_TRIM.replace("{n}", "1") + "volume=0.0dB,adelay=0:all=1[e1];"
              "[2:a]" + RS + "volume=0.0dB,adelay=6950:all=1[e2];"
              + CARD_TRIM.replace("{n}", "3") + "volume=0.0dB,adelay=9750:all=1[e3];"
              "[4:a]" + RS + "volume=0.0dB,adelay=16500:all=1[e4];"
              "[0:a][e1][e2][e3][e4]amix=inputs=5:normalize=0:duration=first"
              ":dropout_transition=0,alimiter=limit=0.3:level=0,"
              "loudnorm=I=-14:TP=-1.5:LRA=50,aresample=48000[aout]")
        self.assertEqual(cmd[len(BASE) + 10:], [
            "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", "-t", "19.500", "/o/out.part.mp4"])

    def test_mix_command_with_music(self):
        music = {"path": "/m/track.mp3", "gain_db": -16.0, "start": 4.0}
        tl = rt.build_timeline(self.validate(spec(clips=BASE_SPEC["clips"][:1])))
        cmd = rt.mix_command("/t/all.mov", tl, music, "/o/out.part.mp4")
        card = os.path.join(rt.SFX_DIR, "card_hit.wav")
        imp = os.path.join(self.sfx_dir, "impact.wav")
        # total = 0.75 + 9 = 9.75
        fc = (CARD_TRIM.replace("{n}", "1") + "volume=0.0dB,adelay=0:all=1[e1];"
              "[2:a]" + RS + "volume=0.0dB,adelay=6950:all=1[e2];"
              "[0:a]asplit=2[base][key];"
              "[3:a]aresample=48000,aformat=channel_layouts=stereo,atrim=start=4.000,"
              "asetpts=PTS-STARTPTS,atrim=end=9.750,volume=-16.0dB[mraw];"
              "[mraw][key]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400"
              "[mduck];"
              "[mduck]afade=t=out:st=8.750:d=1[mus];"
              "[base][mus][e1][e2]amix=inputs=4:normalize=0:duration=first"
              ":dropout_transition=0,alimiter=limit=0.3:level=0,"
              "loudnorm=I=-14:TP=-1.5:LRA=50,aresample=48000[aout]")
        self.assertEqual(cmd[len(BASE):len(BASE) + 10], [
            "-i", "/t/all.mov", "-i", card, "-i", imp, "-stream_loop", "-1", "-i",
            "/m/track.mp3"])
        self.assertEqual(cmd[len(BASE) + 10:len(BASE) + 12], ["-filter_complex", fc])

    def test_sfx_gain_shows_in_mix(self):
        raw = spec()
        raw["clips"][0]["sfx"][0]["gain_db"] = -4.5
        cmd = rt.mix_command("/t/all.mov", rt.build_timeline(self.validate(raw)), None, "/o.mp4")
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("volume=-4.5dB,adelay=6950:all=1", fc)

    def test_spec_text_never_enters_a_filter_string(self):
        nasty = "Evil':%{x},[a];b=c"
        raw = spec(caption=nasty)
        parsed = self.validate(raw)
        tl = rt.build_timeline(parsed)
        cmds = [rt.clip_command(c, "/tmp/t/caption.txt", "/tmp/t/c.mov") for c in parsed["clips"]]
        cmds.append(rt.card_command(0.75, "/tmp/t/caption.txt", "/tmp/t/k.mov"))
        cmds.append(rt.mix_command("/t/all.mov", tl, None, "/o.mp4"))
        for cmd in cmds:
            fc = cmd[cmd.index("-filter_complex") + 1]
            self.assertNotIn(nasty, fc)
            for piece in ("Evil", "%{x}", "Defense"):
                self.assertNotIn(piece, fc)
            self.assertFalse(any("Evil" in arg for arg in cmd))

    def test_caption_file_holds_exact_text_without_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = rt.write_caption_file(tmp, "Defense.")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "Defense.")

    def test_filter_path_escaping(self):
        self.assertEqual(rt.escape_filter_path("/tmp/a b/c.txt"), "/tmp/a b/c.txt")
        # two parsing levels: the option value first, then the filtergraph
        self.assertEqual(rt.escape_filter_path("/t/a:b,c'd[e];f=g"),
                         "/t/a\\\\:b\\,c\\\\\\'d\\[e\\]\\;f=g")
        self.assertEqual(rt.escape_filter_path("/t/a\\b"), "/t/a\\\\\\\\b")

    def test_threads_capped_everywhere(self):
        cmds = [rt.clip_command(self.spec["clips"][0], "/c.txt", "/o.mov"),
                rt.card_command(0.75, "/c.txt", "/o.mov"),
                rt.mix_command("/a.mov", rt.build_timeline(self.spec), None, "/o.mp4")]
        for cmd in cmds:
            self.assertIn("-filter_threads", cmd)
            self.assertEqual(cmd[cmd.index("-filter_threads") + 1], "4")
            self.assertIn("-nostdin", cmd)


KIT_PATCH = "[p]crop=78:336:618:704,hflip,boxblur=2[patch];"
# The kit blocks are stacked in yuv444p: with odd block heights (99, 1087) the 4:2:0 chroma of
# one block bled two rows into the next (a blue line on top of the facecam in a real still).
KIT_GAME = ("[b][patch]overlay=540:704,crop=948:954:540:126,"
            "scale=1080:1087:flags=lanczos,format=yuv444p[g];")
STRIP = "[a]crop=1380:126:270:0,scale=1080:99:flags=lanczos,format=yuv444p[s];"
KIT_FILTER = ("[0:v]fps=60,split=3[a][b][p];" + KIT_PATCH + KIT_GAME + STRIP +
              "[s][g]vstack=inputs=2,pad=1080:1920:0:367:black,setsar=1," + TEXT_FILTER +
              ",format=yuv420p[v]")
KIT_REACTION_FILTER = ("[0:v]fps=60,split=4[a][b][p][c];" + KIT_PATCH + KIT_GAME + STRIP +
                       "[c]crop=560:316:40:716,scale=1080:609:flags=lanczos,"
                       "unsharp=5:5:0.5,format=yuv444p[cam];"
                       "[s][g][cam]vstack=inputs=3,pad=1080:1920:0:62:black,setsar=1," + TEXT_FILTER +
                       ",format=yuv420p[v]")


class LayoutValidationTests(SfxDirCase):
    def test_default_is_kit(self):
        self.assertEqual(self.validate(BASE_SPEC)["layout"], "kit")

    def test_known_layouts_are_accepted(self):
        for name in ("bottom-cam", "kit", "kit-reaction"):
            self.assertEqual(self.validate(spec(layout=name))["layout"], name)

    def test_unknown_layout_is_refused(self):
        self.assertInvalid(spec(layout="md4"), "layout")
        self.assertInvalid(spec(layout="KIT"), "layout")
        self.assertInvalid(spec(layout=3), "layout")
        self.assertInvalid(spec(layout=None), "layout")

    def test_error_lists_the_choices(self):
        self.assertInvalid(spec(layout="x"), "kit-reaction")


class LayoutGeometryTests(unittest.TestCase):
    def test_stack_heights_fit_and_pad_is_centred(self):
        heights = {"bottom-cam": 99 + 956, "kit": 99 + 1087, "kit-reaction": 99 + 1087 + 609}
        self.assertEqual(set(rt.LAYOUTS), set(heights))
        for name, height in heights.items():
            self.assertEqual(rt.STACK_HEIGHT[name], height, name)
            self.assertLessEqual(height, rt.HEIGHT, name)
            self.assertEqual(rt.PAD_Y[name], (rt.HEIGHT - height) // 2, name)
        self.assertEqual((rt.PAD_Y["bottom-cam"], rt.PAD_Y["kit"], rt.PAD_Y["kit-reaction"]),
                         (432, 367, 62))

    def test_scaled_sizes_keep_their_aspect(self):
        cw, ch = rt.KIT_GAME_CROP[:2]
        self.assertEqual(round(ch * rt.WIDTH / cw), rt.KIT_GAME_SIZE[1])
        fw, fh = rt.CAM_CROP[:2]
        self.assertAlmostEqual(fh * rt.WIDTH / fw, rt.CAM_SIZE[1], delta=0.01 * rt.CAM_SIZE[1])

    def test_cam_crop_stays_inside_the_picture_of_the_box(self):
        # measured on real frames: the box is x 30..610, y 708..1035 with amber brackets at its
        # corners and a dark frame 2 px thick; the picture itself is x 34..605, y 713..1032
        w, h, x, y = rt.CAM_CROP
        self.assertGreaterEqual(x, 34)
        self.assertLessEqual(x + w, 606)
        self.assertGreaterEqual(y, 716)  # rows 713..715 carry chroma bleed from the game above
        self.assertLessEqual(y + h, 1033)
        for value in rt.CAM_CROP:
            self.assertEqual(value % 2, 0, value)

    def test_patch_covers_the_facecam_left_over_in_the_game_crop(self):
        # the facecam box is x 30..610, y 708..1035; the game crop starts at x 540
        px, py = rt.KIT_PATCH_AT
        pw, ph = rt.KIT_PATCH_SRC[:2]
        self.assertEqual(px, rt.KIT_GAME_CROP[2])
        self.assertLessEqual(py, 708)
        self.assertGreaterEqual(py + ph, 1035)
        self.assertGreaterEqual(px + pw, 610)

    def test_patch_geometry_is_even_so_yuv420_crop_does_not_round_it(self):
        # crop and overlay round odd offsets and sizes of a yuv420p frame down to even ones,
        # which left one bracket row uncovered at the bottom of the patch (seen in a real still)
        for value in (*rt.KIT_PATCH_SRC, *rt.KIT_PATCH_AT, *rt.KIT_GAME_CROP):
            self.assertEqual(value % 2, 0, value)
        self.assertEqual(rt.KIT_PATCH_SRC[3], rt.KIT_PATCH_AT[1])

    def test_patch_reaches_past_the_chroma_halo_of_the_brackets(self):
        # the brackets end at x 611 but their compression halo reaches x 614 in the source,
        # so the patch must extend at least that far (found by looking at real stills)
        self.assertGreaterEqual(rt.KIT_PATCH_AT[0] + rt.KIT_PATCH_SRC[0], 615)

    def test_patch_source_is_the_mirror_neighbour(self):
        # the flipped source starts where the patch ends, so the seam at its right edge is the
        # same pixel column on both sides
        sx = rt.KIT_PATCH_SRC[2]
        self.assertEqual(sx, rt.KIT_PATCH_AT[0] + rt.KIT_PATCH_SRC[0])

    def test_crops_stay_inside_the_source(self):
        for w, h, x, y in (rt.KIT_GAME_CROP, rt.CAM_CROP, rt.KIT_PATCH_SRC, rt.STRIP_CROP):
            self.assertLessEqual(x + w, rt.SRC_W)
            self.assertLessEqual(y + h, rt.SRC_H)


class LayoutCommandTests(SfxDirCase):
    def filter_of(self, cmd):
        return cmd[cmd.index("-filter_complex") + 1]

    def audio(self):
        return (";[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                "afade=t=in:d=0.05,afade=t=out:st=8.940:d=0.06,apad=whole_dur=9.000[a]")

    def clip(self):
        return self.validate(BASE_SPEC)["clips"][0]

    def test_default_arguments_are_the_kit_ones(self):
        self.assertEqual(rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov"),
                         rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov",
                                         layout="kit"))

    def test_kit_filter_exact(self):
        cmd = rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov", layout="kit")
        self.assertEqual(self.filter_of(cmd), KIT_FILTER + self.audio())

    def test_kit_reaction_filter_exact(self):
        cmd = rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov",
                              layout="kit-reaction")
        self.assertEqual(self.filter_of(cmd), KIT_REACTION_FILTER + self.audio())

    def test_other_arguments_do_not_change_with_layout(self):
        bottom_cam = rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov")
        kit = rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov", layout="kit")
        drop = lambda cmd: [a for a in cmd if a != self.filter_of(cmd)]  # noqa: E731
        self.assertEqual(drop(bottom_cam), drop(kit))

    def test_caption_stays_at_the_same_height_on_every_layout(self):
        for name in rt.LAYOUTS:
            cmd = rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov", layout=name)
            self.assertIn(":y=640,format=yuv420p[v]", self.filter_of(cmd))

    def test_cards_do_not_change(self):
        # card_command has no layout: the black card is the same for every layout
        self.assertEqual(rt.card_command(0.75, "/c.txt", "/o.mov")[-1], "/o.mov")
        self.assertNotIn("layout", rt.card_command.__code__.co_varnames)

    def test_scene_commands_use_the_spec_layout(self):
        parsed = self.validate(spec(layout="kit-reaction"))
        timeline = rt.build_timeline(parsed)
        with tempfile.TemporaryDirectory() as tmp:
            cmds = [cmd for _, cmd in rt._scene_commands(parsed, timeline, tmp)]
        clip_cmds = [c for c in cmds if "-ss" in c]
        self.assertEqual(len(clip_cmds), 2)
        for cmd in clip_cmds:
            self.assertIn("overlay=540:704", self.filter_of(cmd))
            self.assertIn("vstack=inputs=3", self.filter_of(cmd))


LOUDNORM_JSON = """{
\t"input_i" : "-23.50",
\t"input_tp" : "-3.10",
\t"input_lra" : "7.20",
\t"input_thresh" : "-34.00",
\t"output_i" : "-14.5",
\t"normalization_type" : "linear",
\t"target_offset" : "0.40"
}
"""


class LoudnormTests(SfxDirCase):
    def test_parse_reads_last_json_block(self):
        got = rt.parse_loudnorm("[Parsed_loudnorm_0 @ 0x1] \n" + LOUDNORM_JSON)
        self.assertEqual(got["input_i"], -23.5)
        self.assertEqual(got["target_offset"], 0.4)

    def test_parse_rejects_missing_or_inf(self):
        with self.assertRaises(rt.ToolError):
            rt.parse_loudnorm("no json here")
        with self.assertRaises(rt.ToolError):
            rt.parse_loudnorm(LOUDNORM_JSON.replace('"-23.50"', '"-inf"'))

    def test_normalization_type(self):
        self.assertEqual(rt.parse_normalization_type("x\n" + LOUDNORM_JSON), "linear")
        dynamic = LOUDNORM_JSON.replace("linear", "dynamic")
        self.assertEqual(rt.parse_normalization_type(dynamic), "dynamic")
        self.assertEqual(rt.parse_normalization_type("no json"), "unknown")

    def test_target_lra_is_high_enough_for_linear_mode(self):
        self.assertEqual(rt.LOUDNORM_LRA, 50)
        self.assertIn("LRA=50", rt.LOUDNORM)

    def test_second_pass_filter(self):
        got = rt.parse_loudnorm(LOUDNORM_JSON)
        self.assertEqual(rt.loudnorm_filter(got),
                         "loudnorm=I=-14:TP=-1.5:LRA=50:measured_I=-23.50:measured_LRA=7.20"
                         ":measured_TP=-3.10:measured_thresh=-34.00:offset=0.40:linear=true"
                         ":print_format=json")
        self.assertEqual(rt.loudnorm_filter(None), "loudnorm=I=-14:TP=-1.5:LRA=50")

    def test_measure_command_discards_output_and_prints_json(self):
        tl = rt.build_timeline(self.validate(BASE_SPEC))
        cmd = rt.measure_command("/t/all.mov", tl, None)
        self.assertEqual(cmd[:6], ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info",
                                   "-nostdin"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertTrue(fc.endswith("loudnorm=I=-14:TP=-1.5:LRA=50:print_format=json"
                                    ",aresample=48000[aout]"))
        self.assertEqual(cmd[-7:], ["-map", "[aout]", "-t", "19.500", "-f", "null", "-"])
        self.assertNotIn("copy", cmd)

    def test_second_pass_mix_logs_at_info_so_the_report_is_readable(self):
        tl = rt.build_timeline(self.validate(BASE_SPEC))
        measured = rt.parse_loudnorm(LOUDNORM_JSON)
        cmd = rt.mix_command("/t/all.mov", tl, None, "/o.mp4", measured)
        self.assertEqual(cmd[3:5], ["-loglevel", "info"])
        plain = rt.mix_command("/t/all.mov", tl, None, "/o.mp4")
        self.assertEqual(plain[3:5], ["-loglevel", "error"])


class CardSoundTrimTests(SfxDirCase):
    def mix_graph(self, card_seconds):
        parsed = self.validate(spec(card_seconds=card_seconds))
        cmd = rt.mix_command("/t/all.mov", rt.build_timeline(parsed), None, "/o.mp4")
        return cmd[cmd.index("-filter_complex") + 1]

    def test_hit_is_cut_to_a_short_card(self):
        fc = self.mix_graph(0.4)
        self.assertIn("[1:a]" + RS + "atrim=end=0.400,afade=t=out:st=0.395:d=0.005,volume=0.0dB", fc)
        self.assertIn("atrim=end=0.400,afade=t=out:st=0.395:d=0.005,volume=0.0dB,"
                      "adelay=9400:all=1", fc)  # the second card starts at 0.4 + 9.0

    def test_long_card_just_has_silence_after_the_hit(self):
        fc = self.mix_graph(1.0)
        self.assertIn("[1:a]" + RS + "atrim=end=1.000,afade=t=out:st=0.995:d=0.005,volume=", fc)

    def test_effects_are_not_trimmed(self):
        fc = self.mix_graph(0.4)
        self.assertIn("[2:a]" + RS + "volume=0.0dB,adelay=6600:all=1[e2]", fc)


class NonFiniteTests(SfxDirCase):
    def test_nan_and_inf_numbers_are_spec_errors(self):
        nan, inf = float("nan"), float("inf")
        for bad in (spec(card_seconds=nan), spec(card_seconds=inf)):
            self.assertInvalid(bad, "finite")
        for value in (nan, inf, -inf):
            raw = spec()
            raw["clips"][0]["sfx"][0]["at"] = value
            self.assertInvalid(raw, "finite")
            raw = spec()
            raw["clips"][0]["sfx"][0]["gain_db"] = value
            self.assertInvalid(raw, "finite")
        self.assertInvalid(spec(music={"path": "/m.mp3", "gain_db": nan}), "finite")
        self.assertInvalid(spec(music={"path": "/m.mp3", "start": inf}), "finite")

    def test_nan_and_inf_timestamps_are_spec_errors(self):
        for field in ("start", "end"):
            for value in (float("nan"), float("inf")):
                raw = spec()
                raw["clips"][0][field] = value
                self.assertInvalid(raw, "finite")

    def test_huge_integers_do_not_overflow(self):
        self.assertInvalid(spec(card_seconds=10 ** 400), "finite")
        raw = spec()
        raw["clips"][0]["end"] = 10 ** 400
        self.assertInvalid(raw, "finite")

    def test_json_nan_literal_is_a_spec_error_not_a_traceback(self):
        text = ('{"caption": "A", "output": "/o/a.mp4", "card_seconds": NaN, '
                '"clips": [{"source": "/s.mp4", "start": 1, "end": 12}]}')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spec.json")
            with open(path, "w") as handle:
                handle.write(text)
            with self.assertRaises(rt.SpecError):
                rt.load_spec(path)


class OutputOverwriteTests(SfxDirCase):
    def test_output_equal_to_a_source_is_refused(self):
        self.assertInvalid(spec(output="/videos/stream.mp4"), "overwrite")

    def test_output_equal_to_the_music_is_refused(self):
        self.assertInvalid(spec(output="/m/track.mp4", music={"path": "/m/track.mp4"}),
                           "overwrite")

    def test_resolves_symlinks_and_dot_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "stream.mp4")
            open(real, "wb").close()
            link = os.path.join(tmp, "link.mp4")
            os.symlink(real, link)
            raw = spec(output=link)
            raw["clips"][0]["source"] = real
            self.assertInvalid(raw, "overwrite")
            raw = spec(output=os.path.join(tmp, "x", "..", "stream.mp4"))
            raw["clips"][0]["source"] = real
            self.assertInvalid(raw, "overwrite")

    def test_different_output_is_fine(self):
        self.validate(spec(output="/videos/other.mp4"))


class MainErrorTests(unittest.TestCase):
    def test_oserror_becomes_a_clean_message(self):
        err = io.StringIO()
        with mock.patch.object(rt, "load_spec", side_effect=PermissionError(13, "Permission denied")), \
                contextlib.redirect_stderr(err):
            code = rt.main(["whatever.json"])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())
        self.assertIn("Permission denied", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_oserror_during_render_is_clean_too(self):
        fake = {"caption": "A", "output": "/o/a.mp4", "card_seconds": 0.75, "opening_card": True,
                "music": None, "clip_comp": False, "crowd_bed": None,
                "card_sound": {"name": "card_hit", "gain_db": 0.0, "path": "/s/card_hit.wav"},
                "clips": [{"source": "/s.mp4", "start_s": 0.0, "end_s": 20.0,
                           "length": 20.0, "sfx": []}]}
        err = io.StringIO()
        with mock.patch.object(rt, "load_spec", return_value=fake), \
                mock.patch.object(rt, "require_tools"), \
                mock.patch.object(rt, "check_sources"), \
                mock.patch.object(rt, "render", side_effect=OSError(28, "No space left on device")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = rt.main(["whatever.json"])
        self.assertEqual(code, 1)
        self.assertIn("No space left on device", err.getvalue())


def measurer_for(measured, norm_type="linear"):
    """Fake stderr runner: the measure pass and the final mix pass both go through it."""
    def run(cmd):
        measured.append(cmd)
        if cmd[-1].endswith(".part.mp4"):
            with open(cmd[-1], "wb") as handle:
                handle.write(b"x")
            return LOUDNORM_JSON.replace("linear", norm_type)
        return "noise\n" + LOUDNORM_JSON.replace("linear", "dynamic")
    return run


class RenderFlowTests(SfxDirCase):
    """render() with a fake runner: order of work, cleanup and the duration check."""

    def run_render(self, real_duration, norm_type="linear"):
        parsed = self.validate(BASE_SPEC)
        with tempfile.TemporaryDirectory() as out_dir:
            parsed["output"] = os.path.join(out_dir, "sub", "v.mp4")
            commands, passes = [], []

            def runner(cmd):
                commands.append(cmd)
                with open(cmd[-1], "wb") as handle:
                    handle.write(b"x")
                return ""

            result = error = None
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    result = rt.render(parsed, runner=runner,
                                       probe_output=lambda p: real_duration,
                                       measurer=measurer_for(passes, norm_type))
            except rt.ToolError as exc:
                error = exc
            exists = os.path.exists(parsed["output"])
            leftovers = os.listdir(os.path.dirname(parsed["output"]))
            return commands, passes, result, error, exists, leftovers, err.getvalue()

    def test_good_render_runs_all_steps_in_order(self):
        commands, passes, result, error, exists, leftovers, log = self.run_render(19.5)
        self.assertIsNone(error)
        self.assertTrue(exists)
        self.assertEqual(leftovers, ["v.mp4"])
        # 2 cards + 2 clips + concat through the runner; measure and mix through the measurer
        self.assertEqual(len(commands), 5)
        self.assertEqual(commands[-1][commands[-1].index("-f") + 1], "concat")
        self.assertEqual(len(passes), 2)
        self.assertTrue(passes[-1][-1].endswith(".part.mp4"))
        fc = passes[-1][passes[-1].index("-filter_complex") + 1]
        self.assertIn("measured_I=-23.50:measured_LRA=7.20:measured_TP=-3.10"
                      ":measured_thresh=-34.00:offset=0.40:linear=true", fc)
        self.assertEqual(result[2], "linear")
        self.assertNotIn("warning", log)

    def test_fallback_to_dynamic_is_reported_loudly(self):
        _, _, result, error, exists, _, log = self.run_render(19.5, "dynamic")
        self.assertIsNone(error)
        self.assertEqual(result[2], "dynamic")
        self.assertIn("warning", log)
        self.assertIn("dynamic", log)

    def test_wrong_duration_deletes_output_and_fails(self):
        _, _, _, error, exists, leftovers, _ = self.run_render(20.0)
        self.assertIsNotNone(error)
        self.assertIn("0.25", str(error))
        self.assertFalse(exists)
        self.assertEqual(leftovers, [])


FFMPEG_OK = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd):
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True)
    if proc.returncode:
        raise AssertionError(proc.stderr.decode(errors="replace")[-800:])
    return proc.stdout


def _rms_db(samples, start, end, rate=48000):
    chunk = samples[int(start * rate):int(end * rate)]
    mean = sum(x * x for x in chunk) / len(chunk)
    return 10 * __import__("math").log10(max(mean, 1e-12))


@unittest.skipUnless(FFMPEG_OK, "ffmpeg and ffprobe are needed")
class RealMixGraphTests(unittest.TestCase):
    """Runs the real mix graph on 2 s of synthetic audio (no video worth speaking of)."""

    def test_mix_has_the_right_length_and_the_sfx_raises_the_level_at_its_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "base.mov")
            sfx = os.path.join(tmp, "sfx.wav")
            out = os.path.join(tmp, "out.mp4")
            _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-threads", "4",
                  "-f", "lavfi", "-i", "color=c=black:s=64x64:r=30:d=2",
                  "-f", "lavfi", "-i", "sine=f=220:d=2:sample_rate=48000,volume=0.05",
                  "-c:v", "mpeg4", "-c:a", "pcm_s16le", "-ac", "2", base])
            _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-threads", "4",
                  "-f", "lavfi", "-i", "sine=f=1000:d=0.5:sample_rate=48000,volume=0.5",
                  "-ac", "2", sfx])
            timeline = {"segments": [], "total": 2.0, "events": [
                {"t": 1.0, "name": "tone", "gain_db": 0.0, "path": sfx, "kind": "sfx"}]}
            _run(rt.mix_command(base, timeline, None, out))
            length = rt.probe_source(out)["duration"]
            self.assertAlmostEqual(length, 2.0, delta=0.1)
            pcm = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", out,
                        "-f", "f32le", "-ac", "1", "-ar", "48000", "-"])
            samples = array("f")
            samples.frombytes(pcm)
            before = _rms_db(samples, 0.30, 0.85)
            during = _rms_db(samples, 1.10, 1.40)
            after = _rms_db(samples, 1.70, 1.95)
            self.assertGreater(during - before, 8.0, (before, during, after))
            self.assertGreater(during - after, 8.0, (before, during, after))


@unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
class RealDrawtextPathTests(unittest.TestCase):
    """The escaped caption path must survive both ffmpeg parsing levels."""

    def test_awkward_directory_names_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("plain dir", "a:b,c", "it's [x];y=z", "back\\slash"):
                folder = os.path.join(tmp, name)
                os.makedirs(folder)
                path = rt.write_caption_file(folder, "Hi.")
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-threads", "4",
                       "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=0.1",
                       "-filter_complex", f"[0:v]{rt._drawtext(path)}[v]",
                       "-map", "[v]", "-frames:v", "1", "-f", "null", "-"]
                with self.subTest(name=name):
                    _run(cmd)


class SoundsDirCase(SfxDirCase):
    """SfxDirCase plus a second folder of (empty) sound files, for the sfx_dir field."""

    def setUp(self):
        super().setUp()
        self._other = tempfile.TemporaryDirectory()
        self.addCleanup(self._other.cleanup)
        self.other_dir = self._other.name
        for name in ("card_hit_alt", "crowd_bed", "crowd_save", "swell"):
            open(os.path.join(self.other_dir, name + ".wav"), "wb").close()


class SfxDirFieldTests(SoundsDirCase):
    def test_sfx_names_are_looked_up_in_the_spec_folder(self):
        raw = spec(sfx_dir=self.other_dir)
        raw["clips"][0]["sfx"] = [{"at": 3, "name": "swell"}]
        raw["clips"][1]["sfx"] = []
        parsed = self.validate(raw)
        self.assertEqual(parsed["clips"][0]["sfx"][0]["path"],
                         os.path.join(self.other_dir, "swell.wav"))

    def test_without_the_field_the_function_default_is_used(self):
        raw = spec()
        raw["clips"][0]["sfx"] = [{"at": 3, "name": "swell"}]
        self.assertInvalid(raw, "swell")  # "swell" only exists in the other folder

    def test_names_missing_from_the_spec_folder_are_refused(self):
        raw = spec(sfx_dir=self.other_dir)  # BASE_SPEC uses "impact", not in the other folder
        self.assertInvalid(raw, "impact")

    def test_must_be_an_existing_folder(self):
        self.assertInvalid(spec(sfx_dir="/no/such/folder"), "sfx_dir")
        self.assertInvalid(spec(sfx_dir=5), "sfx_dir")
        self.assertInvalid(spec(sfx_dir=""), "sfx_dir")
        self.assertInvalid(spec(sfx_dir=os.path.join(self.other_dir, "swell.wav")), "sfx_dir")

    def test_the_name_rules_still_apply(self):
        raw = spec(sfx_dir=self.other_dir)
        raw["clips"][0]["sfx"] = [{"at": 3, "name": "../swell"}]
        self.assertInvalid(raw, "name")


class CardSoundFieldTests(SoundsDirCase):
    def test_default_is_the_builtin_card_hit_at_zero(self):
        card = self.validate(BASE_SPEC)["card_sound"]
        self.assertEqual(card, {"name": "card_hit", "gain_db": 0.0,
                                "path": os.path.join(rt.SFX_DIR, "card_hit.wav")})

    def test_default_ignores_the_spec_folder(self):
        card = self.validate(spec(sfx_dir=self.other_dir, clips=[
            {"source": "/s.mp4", "start": 10, "end": 30}]))["card_sound"]
        self.assertEqual(card["path"], os.path.join(rt.SFX_DIR, "card_hit.wav"))

    def test_named_sound_comes_from_the_spec_folder(self):
        raw = spec(sfx_dir=self.other_dir, card_sound={"name": "card_hit_alt", "gain_db": -3},
                   clips=[{"source": "/s.mp4", "start": 10, "end": 30}])
        card = self.validate(raw)["card_sound"]
        self.assertEqual(card, {"name": "card_hit_alt", "gain_db": -3.0,
                                "path": os.path.join(self.other_dir, "card_hit_alt.wav")})

    def test_gain_defaults_to_zero(self):
        raw = spec(card_sound={"name": "hit"})
        self.assertEqual(self.validate(raw)["card_sound"]["gain_db"], 0.0)

    def test_errors(self):
        self.assertInvalid(spec(card_sound={"name": "nope"}), "nope")
        self.assertInvalid(spec(card_sound={}), "name")
        self.assertInvalid(spec(card_sound={"name": "hit", "extra": 1}), "extra")
        self.assertInvalid(spec(card_sound={"name": "hit", "gain_db": 7}), "gain_db")
        self.assertInvalid(spec(card_sound={"name": "hit", "gain_db": float("nan")}), "finite")
        self.assertInvalid(spec(card_sound={"name": "a/b"}), "name")
        self.assertInvalid(spec(card_sound="card_hit"), "card_sound")
        self.assertInvalid(spec(card_sound=None), "card_sound")

    def test_timeline_cards_use_the_chosen_sound(self):
        raw = spec(card_sound={"name": "hit", "gain_db": -3})
        tl = rt.build_timeline(self.validate(raw))
        cards = [e for e in tl["events"] if e["kind"] == "card"]
        self.assertEqual([(e["name"], e["gain_db"]) for e in cards], [("hit", -3.0)] * 2)
        self.assertEqual(cards[0]["path"], os.path.join(self.sfx_dir, "hit.wav"))

    def test_mix_cuts_the_chosen_sound_to_the_card_with_a_fade(self):
        raw = spec(card_sound={"name": "hit"}, card_seconds=0.5)
        cmd = rt.mix_command("/t/all.mov", rt.build_timeline(self.validate(raw)), None, "/o.mp4")
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[1:a]" + RS + "atrim=end=0.500,afade=t=out:st=0.495:d=0.005,volume=0.0dB",
                      fc)


class CrowdBedFieldTests(SoundsDirCase):
    def test_default_is_off(self):
        self.assertIsNone(self.validate(BASE_SPEC)["crowd_bed"])
        self.assertIsNone(self.validate(spec(crowd_bed=None))["crowd_bed"])

    def test_defaults_and_path(self):
        raw = spec(sfx_dir=self.other_dir, crowd_bed={"name": "crowd_bed"},
                   clips=[{"source": "/s.mp4", "start": 10, "end": 30}])
        self.assertEqual(self.validate(raw)["crowd_bed"],
                         {"name": "crowd_bed", "gain_db": -12.0,
                          "path": os.path.join(self.other_dir, "crowd_bed.wav")})

    def test_errors(self):
        self.assertInvalid(spec(crowd_bed={}), "name")
        self.assertInvalid(spec(crowd_bed={"name": "nope"}), "nope")
        self.assertInvalid(spec(crowd_bed={"name": "hit", "x": 1}), "x")
        self.assertInvalid(spec(crowd_bed={"name": "hit", "gain_db": 7}), "gain_db")
        self.assertInvalid(spec(crowd_bed={"name": "hit", "gain_db": -61}), "gain_db")
        self.assertInvalid(spec(crowd_bed="crowd_bed"), "crowd_bed")

    def bed_spec(self, **changes):
        return self.validate(spec(crowd_bed={"name": "hit", "gain_db": -12}, **changes))

    def test_one_bed_entry_per_clip_and_none_under_cards(self):
        tl = rt.build_timeline(self.bed_spec())
        clips = [s for s in tl["segments"] if s["kind"] == "clip"]
        self.assertEqual([(b["t"], b["length"]) for b in tl["beds"]],
                         [(c["start"], c["duration"]) for c in clips])
        for bed in tl["beds"]:
            for card in (s for s in tl["segments"] if s["kind"] == "card"):
                self.assertTrue(bed["t"] + bed["length"] <= card["start"] + 1e-9
                                or bed["t"] >= card["start"] + card["duration"] - 1e-9)

    def test_no_beds_without_the_field(self):
        self.assertEqual(rt.build_timeline(self.validate(BASE_SPEC))["beds"], [])

    def test_each_clip_starts_at_a_different_wrapped_point(self):
        many = [dict(BASE_SPEC["clips"][0], sfx=[]) for _ in range(4)]
        parsed = self.validate(spec(crowd_bed={"name": "hit"}, clips=many))
        path = parsed["crowd_bed"]["path"]
        tl = rt.build_timeline(parsed, {path: 23.75})
        offsets = [round(b["offset"], 3) for b in tl["beds"]]
        step = rt.BED_OFFSET_STEP
        self.assertEqual(offsets, [round(i * step % 23.75, 3) for i in range(4)])
        self.assertEqual(len(set(offsets)), 4)
        self.assertTrue(all(0 <= o < 23.75 for o in offsets))

    def test_without_the_length_the_offsets_are_not_wrapped(self):
        many = [dict(BASE_SPEC["clips"][0], sfx=[]) for _ in range(5)]
        tl = rt.build_timeline(self.validate(spec(crowd_bed={"name": "hit"}, clips=many)))
        self.assertAlmostEqual(tl["beds"][4]["offset"], 4 * rt.BED_OFFSET_STEP)

    def test_mix_graph_exact(self):
        raw = spec(crowd_bed={"name": "hit", "gain_db": -12}, clips=[
            dict(BASE_SPEC["clips"][0], sfx=[]), dict(BASE_SPEC["clips"][1], sfx=[])])
        parsed = self.validate(raw)
        tl = rt.build_timeline(parsed, {parsed["crowd_bed"]["path"]: 23.75})
        cmd = rt.mix_command("/t/all.mov", tl, None, "/o.mp4")
        card = os.path.join(rt.SFX_DIR, "card_hit.wav")
        bed = os.path.join(self.sfx_dir, "hit.wav")
        self.assertEqual(cmd[len(BASE):len(BASE) + 8],
                         ["-i", "/t/all.mov", "-i", card, "-i", card, "-stream_loop", "-1"])
        self.assertEqual(cmd[len(BASE) + 8:len(BASE) + 10], ["-i", bed])
        fc = cmd[cmd.index("-filter_complex") + 1]
        expected = (
            CARD_TRIM.replace("{n}", "1") + "volume=0.0dB,adelay=0:all=1[e1];"
            + CARD_TRIM.replace("{n}", "2") + "volume=0.0dB,adelay=9750:all=1[e2];"
            "[3:a]" + RS + "asplit=2[bd0][bd1];"
            "[bd0]atrim=start=0.000:duration=9.000,asetpts=PTS-STARTPTS,"
            "afade=t=in:d=0.15,afade=t=out:st=8.940:d=0.06,volume=-12.0dB,"
            "adelay=750:all=1[cb0];"
            "[bd1]atrim=start=7.300:duration=9.000,asetpts=PTS-STARTPTS,"
            "afade=t=in:d=0.15,afade=t=out:st=8.940:d=0.06,volume=-12.0dB,"
            "adelay=10500:all=1[cb1];"
            "[0:a][e1][e2][cb0][cb1]amix=inputs=5:normalize=0:duration=first"
            ":dropout_transition=0,alimiter=limit=0.3:level=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=50,aresample=48000[aout]")
        self.assertEqual(fc, expected)

    def test_bed_comes_before_music_in_the_inputs(self):
        parsed = self.validate(spec(crowd_bed={"name": "hit"}))
        tl = rt.build_timeline(parsed)
        music = {"path": "/m/track.mp3", "gain_db": -16.0, "start": 0.0}
        cmd = rt.mix_command("/t/all.mov", tl, music, "/o.mp4")
        loops = [i for i, a in enumerate(cmd) if a == "-stream_loop"]
        self.assertEqual(len(loops), 2)
        self.assertEqual(cmd[loops[0] + 3], os.path.join(self.sfx_dir, "hit.wav"))
        self.assertEqual(cmd[loops[1] + 3], "/m/track.mp3")
        fc = cmd[cmd.index("-filter_complex") + 1]
        # inputs: concat, 2 cards, 2 impacts, bed, music
        self.assertIn("[6:a]aresample=48000,aformat=channel_layouts=stereo,atrim=start=0.000",
                      fc)


class SfxShapingFieldTests(SoundsDirCase):
    def sfx_spec(self, **fields):
        raw = spec()
        raw["clips"][0]["sfx"] = [dict({"at": 3, "name": "impact"}, **fields)]
        return raw

    def test_defaults(self):
        item = self.validate(self.sfx_spec())["clips"][0]["sfx"][0]
        self.assertEqual((item["start"], item["duration"], item["fade_in"], item["fade_out"],
                          item["stop_at_clip_end"]), (0.0, None, 0.0, 0.0, False))

    def test_values_are_kept(self):
        item = self.validate(self.sfx_spec(start=1, duration=2.5, fade_in=0.1, fade_out=1.0,
                                           stop_at_clip_end=True))["clips"][0]["sfx"][0]
        self.assertEqual((item["start"], item["duration"], item["fade_in"], item["fade_out"],
                          item["stop_at_clip_end"]), (1.0, 2.5, 0.1, 1.0, True))

    def test_errors(self):
        for field, bad in (("start", -0.1), ("duration", 0), ("duration", -1), ("fade_in", -1),
                           ("fade_out", -0.5)):
            self.assertInvalid(self.sfx_spec(**{field: bad}), field)
        for field in ("start", "duration", "fade_in", "fade_out"):
            for bad in (float("nan"), float("inf"), "1", True):
                self.assertInvalid(self.sfx_spec(**{field: bad}), field)
        self.assertInvalid(self.sfx_spec(stop_at_clip_end="yes"), "stop_at_clip_end")
        self.assertInvalid(self.sfx_spec(stop_at_clip_end=1), "stop_at_clip_end")

    def test_fades_must_fit_in_an_explicit_duration(self):
        self.assertInvalid(self.sfx_spec(duration=1, fade_in=0.6, fade_out=0.6), "fade")
        self.validate(self.sfx_spec(duration=1, fade_in=0.5, fade_out=0.5))

    def timeline(self, durations=None, **fields):
        parsed = self.validate(self.sfx_spec(**fields))
        return rt.build_timeline(parsed, durations)

    def sfx_event(self, tl):
        return [e for e in tl["events"] if e["kind"] == "sfx"][0]

    def test_duration_sets_the_trimmed_length(self):
        event = self.sfx_event(self.timeline(duration=2.0))
        self.assertEqual((event["start"], event["trim"], event["length"]), (0.0, 2.0, 2.0))

    def test_file_length_limits_the_trim(self):
        path = os.path.join(self.sfx_dir, "impact.wav")
        event = self.sfx_event(self.timeline({path: 5.0}, start=1.0, duration=20.0))
        self.assertAlmostEqual(event["length"], 4.0)
        self.assertEqual(event["trim"], 20.0)

    def test_no_trim_fields_means_no_trim(self):
        path = os.path.join(self.sfx_dir, "impact.wav")
        event = self.sfx_event(self.timeline({path: 5.0}))
        self.assertIsNone(event["trim"])
        self.assertEqual(event["length"], 5.0)

    def test_stop_at_clip_end_ends_with_the_clip(self):
        # clip 1 spans 0.75 to 9.75 s; the sound begins at 3.75 s
        event = self.sfx_event(self.timeline(stop_at_clip_end=True))
        self.assertAlmostEqual(event["t"] + event["length"], 9.75)
        self.assertAlmostEqual(event["trim"], 6.0)
        self.assertAlmostEqual(event["fade_out"], rt.SFX_STOP_FADE)

    def test_stop_at_clip_end_keeps_a_longer_fade_out(self):
        event = self.sfx_event(self.timeline(stop_at_clip_end=True, fade_out=1.0))
        self.assertEqual(event["fade_out"], 1.0)

    def test_stop_at_clip_end_uses_the_shortest_limit(self):
        event = self.sfx_event(self.timeline(stop_at_clip_end=True, duration=2.0))
        self.assertEqual(event["length"], 2.0)
        self.assertEqual(event["fade_out"], 0.0)  # the sound ends by itself, no cap fade

    def test_stop_at_clip_end_with_a_short_file_gets_no_extra_fade(self):
        path = os.path.join(self.sfx_dir, "impact.wav")
        event = self.sfx_event(self.timeline({path: 1.0}, stop_at_clip_end=True))
        self.assertEqual(event["length"], 1.0)
        self.assertEqual(event["fade_out"], 0.0)

    def test_negative_at_still_stops_at_the_clip_end(self):
        raw = self.sfx_spec(stop_at_clip_end=True)
        raw["clips"][1]["sfx"] = [{"at": -0.5, "name": "impact", "stop_at_clip_end": True}]
        tl = rt.build_timeline(self.validate(raw))
        second = [e for e in tl["events"] if e["kind"] == "sfx"][1]
        self.assertAlmostEqual(second["t"] + second["length"], 19.5)

    def filter_for(self, tl):
        cmd = rt.mix_command("/t/all.mov", tl, None, "/o.mp4")
        return cmd[cmd.index("-filter_complex") + 1]

    def test_exact_filter_for_every_field(self):
        tl = self.timeline(gain_db=-4.5, start=1, duration=2, fade_in=0.1, fade_out=0.5)
        self.assertIn("[2:a]" + RS + "atrim=start=1.000:duration=2.000,asetpts=PTS-STARTPTS,"
                      "afade=t=in:d=0.1,afade=t=out:st=1.500:d=0.5,volume=-4.5dB,"
                      "adelay=3750:all=1[e2]", self.filter_for(tl))

    def test_exact_filter_for_stop_at_clip_end(self):
        tl = self.timeline(stop_at_clip_end=True)
        self.assertIn("[2:a]" + RS + "atrim=duration=6.000,asetpts=PTS-STARTPTS,"
                      "afade=t=out:st=5.920:d=0.08,volume=0.0dB,adelay=3750:all=1[e2]",
                      self.filter_for(tl))

    def test_start_only_trim(self):
        path = os.path.join(self.sfx_dir, "impact.wav")
        tl = self.timeline({path: 5.0}, start=2)
        self.assertIn("[2:a]" + RS + "atrim=start=2.000,asetpts=PTS-STARTPTS,volume=0.0dB",
                      self.filter_for(tl))

    def test_untouched_sfx_has_no_trim_or_fade(self):
        tl = self.timeline()
        self.assertIn("[2:a]" + RS + "volume=0.0dB,adelay=3750:all=1[e2]", self.filter_for(tl))

    def test_fade_out_without_a_known_length_is_an_error(self):
        with self.assertRaises(rt.ToolError):
            self.filter_for(self.timeline(fade_out=0.5))

    # the end-of-video check and the fades use the trimmed length

    def check(self, raw, wav):
        rt.check_sources(self.validate(raw), SourceCheckTests.probe(), exists=lambda p: True,
                         wav_duration=wav)

    def test_end_check_uses_the_trimmed_length(self):
        raw = spec()
        raw["clips"][0]["sfx"] = []
        raw["clips"][1]["sfx"] = [{"at": 6, "name": "impact"}]
        with self.assertRaises(rt.SpecError):
            self.check(raw, lambda p: 20.0)
        raw["clips"][1]["sfx"] = [{"at": 6, "name": "impact", "duration": 2.0}]
        self.check(raw, lambda p: 20.0)
        raw["clips"][1]["sfx"] = [{"at": 6, "name": "impact", "stop_at_clip_end": True}]
        self.check(raw, lambda p: 20.0)

    def test_end_check_uses_the_part_after_start(self):
        raw = spec()
        raw["clips"][0]["sfx"] = []
        raw["clips"][1]["sfx"] = [{"at": 6, "name": "impact", "start": 18.0}]
        self.check(raw, lambda p: 20.0)  # only 2 s left of the file

    def test_start_past_the_end_of_the_file_is_refused(self):
        raw = spec()
        raw["clips"][0]["sfx"] = [{"at": 3, "name": "impact", "start": 5.0}]
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(raw, lambda p: 4.0)
        self.assertIn("start", str(ctx.exception))
        self.assertIn("impact", str(ctx.exception))

    def test_fades_longer_than_the_sound_are_refused(self):
        raw = spec()
        raw["clips"][0]["sfx"] = [{"at": 3, "name": "impact", "fade_in": 0.6, "fade_out": 0.6}]
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(raw, lambda p: 1.0)
        self.assertIn("fade", str(ctx.exception))

    def test_bed_file_is_probed_and_wraps_the_offsets(self):
        raw = spec(crowd_bed={"name": "hit"})
        parsed = self.validate(raw)
        seen = []
        durations = rt.check_sources(parsed, SourceCheckTests.probe(), exists=lambda p: True,
                                     wav_duration=lambda p: seen.append(p) or 4.0 if p.endswith("hit.wav") else 1.0)
        self.assertIn(parsed["crowd_bed"]["path"], seen)
        self.assertEqual(durations[parsed["crowd_bed"]["path"]], 4.0)

    def test_check_returns_the_probed_lengths(self):
        durations = rt.check_sources(self.validate(BASE_SPEC), SourceCheckTests.probe(),
                                     exists=lambda p: True, wav_duration=lambda p: 1.5)
        self.assertEqual(durations, {os.path.join(self.sfx_dir, "impact.wav"): 1.5})


class ClipCompTests(SfxDirCase):
    def test_field(self):
        self.assertFalse(self.validate(BASE_SPEC)["clip_comp"])
        self.assertTrue(self.validate(spec(clip_comp=True))["clip_comp"])
        self.assertInvalid(spec(clip_comp="yes"), "clip_comp")
        self.assertInvalid(spec(clip_comp=1), "clip_comp")

    def test_named_constants(self):
        self.assertEqual(rt.CLIP_COMP, "acompressor=threshold=-24dB:ratio=3:attack=15"
                                       ":release=250:makeup=2")

    def audio_of(self, **kw):
        clip = self.validate(BASE_SPEC)["clips"][0]
        cmd = rt.clip_command(clip, "/tmp/t/caption.txt", "/o.mov", **kw)
        return cmd[cmd.index("-filter_complex") + 1].split(";[0:a]")[1]

    def test_compressor_sits_before_the_fades(self):
        self.assertEqual(self.audio_of(clip_comp=True),
                         "aresample=48000,aformat=channel_layouts=stereo,"
                         "acompressor=threshold=-24dB:ratio=3:attack=15:release=250:makeup=2,"
                         "afade=t=in:d=0.05,afade=t=out:st=8.940:d=0.06,"
                         "apad=whole_dur=9.000[a]")

    def test_off_by_default_leaves_the_old_chain(self):
        self.assertNotIn("acompressor", self.audio_of())

    def test_scene_commands_pass_it_to_clips_only(self):
        parsed = self.validate(spec(clip_comp=True))
        with tempfile.TemporaryDirectory() as tmp:
            cmds = [c for _, c in rt._scene_commands(parsed, rt.build_timeline(parsed), tmp)]
        for cmd in cmds:
            fc = cmd[cmd.index("-filter_complex") + 1]
            self.assertEqual("acompressor" in fc, "-ss" in cmd)

    def test_mix_does_not_compress(self):
        cmd = rt.mix_command("/t/all.mov", rt.build_timeline(self.validate(
            spec(clip_comp=True))), None, "/o.mp4")
        self.assertNotIn("acompressor", cmd[cmd.index("-filter_complex") + 1])


class TimelinePrintTests(SoundsDirCase):
    def test_beds_are_listed(self):
        parsed = self.validate(spec(crowd_bed={"name": "hit"}))
        text = "\n".join(rt.format_timeline(rt.build_timeline(parsed)))
        self.assertEqual(text.count("crowd bed"), 2)


class CrowdStemFieldTests(SoundsDirCase):
    STEM = {"path": "/stems/clutch-crowd.wav", "gain_db": -2}

    def test_default_is_off(self):
        self.assertIsNone(self.validate(BASE_SPEC)["crowd_stem"])
        self.assertIsNone(self.validate(spec(crowd_stem=None))["crowd_stem"])
        self.assertIsNone(rt.build_timeline(self.validate(BASE_SPEC))["stem"])

    def test_normalised_with_default_gain_zero(self):
        self.assertEqual(self.validate(spec(crowd_stem=self.STEM))["crowd_stem"],
                         {"path": "/stems/clutch-crowd.wav", "gain_db": -2.0})
        got = self.validate(spec(crowd_stem={"path": "/s.wav"}))["crowd_stem"]
        self.assertEqual(got, {"path": "/s.wav", "gain_db": 0.0})

    def test_errors(self):
        self.assertInvalid(spec(crowd_stem={}), "path")
        self.assertInvalid(spec(crowd_stem={"path": ""}), "path")
        self.assertInvalid(spec(crowd_stem={"path": 3}), "path")
        self.assertInvalid(spec(crowd_stem={"path": "/s.wav", "x": 1}), "x")
        self.assertInvalid(spec(crowd_stem={"path": "/s.wav", "gain_db": 7}), "gain_db")
        self.assertInvalid(spec(crowd_stem={"path": "/s.wav", "gain_db": -61}), "gain_db")
        self.assertInvalid(spec(crowd_stem={"path": "/s.wav", "gain_db": float("nan")}),
                           "gain_db")
        self.assertInvalid(spec(crowd_stem="/s.wav"), "crowd_stem")

    def test_not_with_a_crowd_bed(self):
        self.assertInvalid(spec(crowd_stem=self.STEM, crowd_bed={"name": "hit"}),
                           "crowd_stem and crowd_bed")

    def test_timeline_carries_the_stem(self):
        tl = rt.build_timeline(self.validate(spec(crowd_stem=self.STEM)))
        self.assertEqual(tl["stem"], {"path": "/stems/clutch-crowd.wav", "gain_db": -2.0})
        text = "\n".join(rt.format_timeline(tl))
        self.assertIn("00:00.000  crowd stem clutch-crowd.wav  -2.0 dB", text)

    def check(self, stem_length, exists=lambda p: True, calls=None):
        def wav(path):
            if calls is not None:
                calls.append(path)
            return stem_length if path == self.STEM["path"] else 1.0

        rt.check_sources(self.validate(spec(crowd_stem=self.STEM)),
                         lambda p: {"duration": 6000.0, "width": 1920, "height": 1080},
                         exists=exists, wav_duration=wav)

    def test_length_must_match_the_plan_within_a_tenth(self):
        # BASE_SPEC plans 19.5 s
        for good in (19.5, 19.59, 19.41):
            self.check(good)
        for bad in (19.61, 19.39, 5.0):
            with self.assertRaises(rt.SpecError) as ctx:
                self.check(bad)
            self.assertIn("rebuild", str(ctx.exception))
            self.assertIn("make_crowd_stem", str(ctx.exception))

    def test_missing_file(self):
        with self.assertRaises(rt.SpecError) as ctx:
            self.check(19.5, exists=lambda p: p != self.STEM["path"])
        self.assertIn("crowd stem not found", str(ctx.exception))

    def test_stem_is_probed_once(self):
        calls = []
        self.check(19.5, calls=calls)
        self.assertEqual(calls.count(self.STEM["path"]), 1)

    def mix(self, builder=rt.mix_command, music=None, **extra):
        tl = rt.build_timeline(self.validate(spec(crowd_stem=self.STEM)))
        args = ["/t/all.mov", tl, music]
        return builder(*args, "/o.mp4", **extra) if builder is rt.mix_command \
            else builder(*args)

    def test_stem_is_mixed_from_time_zero_through_the_limiter_and_loudnorm(self):
        cmd = self.mix()
        self.assertEqual(cmd[len(BASE) + 10:len(BASE) + 12], ["-i", "/stems/clutch-crowd.wav"])
        fc = cmd[cmd.index("-filter_complex") + 1]
        # inputs: concat, 2 cards, 2 impacts, then the stem as input 5
        self.assertIn("[5:a]" + RS + "volume=-2.0dB[cs];", fc)
        self.assertIn("[0:a][e1][e2][e3][e4][cs]amix=inputs=6:normalize=0:duration=first"
                      ":dropout_transition=0,alimiter=limit=0.3:level=0,"
                      "loudnorm=I=-14:TP=-1.5:LRA=50,aresample=48000[aout]", fc)
        self.assertNotIn("adelay=0:all=1[cs]", fc)
        self.assertNotIn("-stream_loop", cmd)

    def test_both_loudnorm_passes_include_the_stem(self):
        measure = self.mix(rt.measure_command)
        self.assertIn("/stems/clutch-crowd.wav", measure)
        fc = measure[measure.index("-filter_complex") + 1]
        self.assertIn("[cs]amix=inputs=6", fc)
        self.assertIn("loudnorm=I=-14:TP=-1.5:LRA=50:print_format=json", fc)
        measured = rt.parse_loudnorm(LOUDNORM_JSON)
        final = self.mix(measured=measured)
        self.assertIn("/stems/clutch-crowd.wav", final)
        self.assertIn("[cs]amix=inputs=6", final[final.index("-filter_complex") + 1])

    def test_stem_comes_before_music_in_the_inputs(self):
        music = {"path": "/m/track.mp3", "gain_db": -16.0, "start": 0.0}
        cmd = self.mix(music=music)
        self.assertLess(cmd.index("/stems/clutch-crowd.wav"), cmd.index("/m/track.mp3"))
        fc = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("[5:a]" + RS + "volume=-2.0dB[cs]", fc)
        self.assertIn("[6:a]" + RS + "atrim=start=0.000", fc)
        self.assertIn("[base][mus][e1][e2][e3][e4][cs]amix=inputs=7", fc)


CAM_FULL_CHAIN = ("[w{k}]crop=314:316:{x}:716,scale=1080:1087:flags=lanczos,"
                  "unsharp=5:5:0.5,format=yuv444p[cam{k}];")
CAM_INSET_CHAIN = ("[w{k}]crop=560:316:40:716,scale=600:338:flags=lanczos,"
                   "unsharp=5:5:0.5,format=yuv444p[cam{k}];")
CAM_STACK = "[s][g]vstack=inputs=2,pad=1080:1920:0:367:black"
CAPTION_OFF = TEXT_FILTER + ":enable='not({})'"


def cam_spec(*windows, layout="kit"):
    raw = spec(layout=layout)
    raw["clips"][0]["cam"] = list(windows)
    return raw


class CamFieldTests(SfxDirCase):
    """The cam field: facecam windows on a kit clip (9 s long in BASE_SPEC)."""

    FULL = {"start": 2, "end": 4, "mode": "full"}
    INSET = {"start": 5, "end": 7.5, "mode": "inset"}

    def cam(self, *windows):
        return self.validate(cam_spec(*windows))["clips"][0]["cam"]

    def test_default_is_no_windows(self):
        self.assertEqual(self.validate(spec(layout="kit"))["clips"][0]["cam"], [])
        self.assertEqual(self.validate(BASE_SPEC)["clips"][0]["cam"], [])

    def test_normalised_windows(self):
        self.assertEqual(self.cam(self.FULL, self.INSET),
                         [{"start": 2.0, "end": 4.0, "mode": "full", "x": 162},
                          {"start": 5.0, "end": 7.5, "mode": "inset", "x": None}])

    def test_explicit_x(self):
        got = self.cam({**self.FULL, "x": 200})
        self.assertEqual(got[0]["x"], 200)

    def test_x_limits_keep_the_crop_inside_source_x_36_to_604(self):
        self.assertEqual(self.cam({**self.FULL, "x": 36})[0]["x"], 36)
        self.assertEqual(self.cam({**self.FULL, "x": 290})[0]["x"], 290)
        for bad in (34, 292, 1000, -2):
            self.assertInvalid(cam_spec({**self.FULL, "x": bad}), "x")

    def test_x_must_be_an_even_whole_number(self):
        for bad in (161, 162.5, "162", True, None, float("nan")):
            self.assertInvalid(cam_spec({**self.FULL, "x": bad}), "x")

    def test_x_is_only_for_full(self):
        self.assertInvalid(cam_spec({**self.INSET, "x": 100}), "only for full")

    def test_not_with_kit_reaction(self):
        self.assertInvalid(cam_spec(self.FULL, layout="kit-reaction"), "kit-reaction")
        self.assertInvalid(cam_spec(self.INSET, layout="kit-reaction"), "cam windows need")

    def test_not_a_list_or_window(self):
        bad = spec(layout="kit")
        bad["clips"][0]["cam"] = {"start": 1}
        self.assertInvalid(bad, "cam must be a list")
        self.assertInvalid(cam_spec("full"), "cam window 1")

    def test_unknown_and_missing_keys(self):
        self.assertInvalid(cam_spec({**self.FULL, "fade": 1}), "fade")
        for key in ("start", "end", "mode"):
            window = dict(self.FULL)
            del window[key]
            self.assertInvalid(cam_spec(window), key)

    def test_mode_must_be_known(self):
        for bad in ("pip", "FULL", 1, None):
            self.assertInvalid(cam_spec({**self.FULL, "mode": bad}), "mode")

    def test_numbers_must_be_finite(self):
        for key in ("start", "end"):
            for bad in (float("nan"), float("inf"), "2", True, None):
                self.assertInvalid(cam_spec({**self.FULL, key: bad}), key)

    def test_time_limits(self):
        self.assertInvalid(cam_spec({**self.FULL, "start": -0.1}), "start")
        self.assertInvalid(cam_spec({**self.FULL, "end": 9.5}), "end")
        self.cam({"start": 0, "end": 9, "mode": "full"})   # the whole clip is fine

    def test_minimum_length(self):
        self.assertInvalid(cam_spec({"start": 1, "end": 1.4, "mode": "full"}), "0.5")
        self.cam({"start": 1, "end": 1.5, "mode": "full"})

    def test_windows_must_be_in_order_and_not_overlap(self):
        self.assertInvalid(cam_spec(self.INSET, self.FULL), "order")
        self.assertInvalid(cam_spec({"start": 2, "end": 5, "mode": "full"},
                                    {"start": 4, "end": 6, "mode": "inset"}), "overlap")
        self.cam({"start": 2, "end": 4, "mode": "full"},
                 {"start": 4, "end": 6, "mode": "inset"})   # touching is fine

    def test_error_names_the_clip(self):
        bad = spec(layout="kit")
        bad["clips"][1]["cam"] = [{"start": 1, "end": 20, "mode": "full"}]
        self.assertInvalid(bad, "clip 2 cam window 1")

    def test_geometry_constants(self):
        w, h, x, y = rt.CAM_FULL_CROP
        self.assertEqual(y, rt.CAM_CROP[3])
        self.assertAlmostEqual(h * rt.WIDTH / w, rt.KIT_GAME_SIZE[1], delta=1)
        self.assertEqual(x % 2, 0)
        lo, hi = rt.CAM_FULL_X_RANGE
        self.assertEqual((lo, hi), (36, 604))
        self.assertGreaterEqual(x, lo)
        self.assertLessEqual(x + w, hi)
        # the picture of the box is x 34..605: the default crop is centred on it
        self.assertAlmostEqual(x + w / 2, (34 + 605) / 2, delta=1)
        # the game block starts under the strip, and the inset sits inside it
        self.assertEqual(rt.CAM_GAME_Y, rt.PAD_Y["kit"] + rt.STRIP_SIZE[1])
        ix, iy = rt.CAM_INSET_AT
        self.assertEqual((ix, rt.CAM_INSET_SIZE), (24, (600, 338)))
        self.assertEqual((ix % 2, iy % 2), (0, 0))
        bottom = rt.CAM_GAME_Y + rt.KIT_GAME_SIZE[1]
        self.assertIn(bottom - (iy + 338), (24, 25))   # 24, or 25 when rounded down to even
        self.assertEqual(rt.CAM_GAME_Y % 2, 0)


class CamCommandTests(SfxDirCase):
    def filter_of(self, cmd):
        return cmd[cmd.index("-filter_complex") + 1]

    def command(self, *windows, layout="kit"):
        clip = self.validate(cam_spec(*windows, layout=layout))["clips"][0]
        return rt.clip_command(clip, "/tmp/t/caption.txt", "/o.mov", layout=layout)

    def audio(self):
        return (";[0:a]aresample=48000,aformat=channel_layouts=stereo,"
                "afade=t=in:d=0.05,afade=t=out:st=8.940:d=0.06,apad=whole_dur=9.000[a]")

    def test_no_cam_is_identical_to_before(self):
        clip = self.validate(spec(layout="kit"))["clips"][0]
        self.assertEqual(self.filter_of(rt.clip_command(clip, "/tmp/t/caption.txt", "/o.mov",
                                                        layout="kit")), KIT_FILTER + self.audio())

    def test_full_window_filter_exact(self):
        got = self.filter_of(self.command({"start": 2, "end": 4, "mode": "full"}))
        want = ("[0:v]fps=60,split=4[a][b][p][w0];" + KIT_PATCH + KIT_GAME + STRIP
                + CAM_FULL_CHAIN.format(k=0, x=162) + CAM_STACK + "[st];"
                "[st][cam0]overlay=0:466:format=yuv444:enable='gte(t,1.9917)*lt(t,3.9917)',setsar=1,"
                + CAPTION_OFF.format("gte(t,1.9917)*lt(t,3.9917)") + ",format=yuv420p[v]")
        self.assertEqual(got, want + self.audio())

    def test_inset_window_filter_exact_and_caption_stays(self):
        got = self.filter_of(self.command({"start": 5, "end": 7.5, "mode": "inset"}))
        want = ("[0:v]fps=60,split=4[a][b][p][w0];" + KIT_PATCH + KIT_GAME + STRIP
                + CAM_INSET_CHAIN.format(k=0) + CAM_STACK + "[st];"
                "[st][cam0]overlay=24:1190:format=yuv444:enable='gte(t,4.9917)*lt(t,7.4917)',setsar=1,"
                + TEXT_FILTER + ",format=yuv420p[v]")
        self.assertEqual(got, want + self.audio())

    def test_several_windows_chain_in_order_with_their_own_x(self):
        got = self.filter_of(self.command({"start": 1, "end": 2, "mode": "full", "x": 200},
                                          {"start": 3, "end": 4, "mode": "inset"},
                                          {"start": 5, "end": 6, "mode": "full"}))
        self.assertIn("split=6[a][b][p][w0][w1][w2];", got)
        self.assertIn(CAM_FULL_CHAIN.format(k=0, x=200) + CAM_INSET_CHAIN.format(k=1)
                      + CAM_FULL_CHAIN.format(k=2, x=162) + CAM_STACK + "[st];", got)
        self.assertIn("[st][cam0]overlay=0:466:format=yuv444:enable='gte(t,0.9917)*lt(t,1.9917)'"
                      "[o0];[o0][cam1]overlay=24:1190:format=yuv444:"
                      "enable='gte(t,2.9917)*lt(t,3.9917)'[o1];[o1][cam2]overlay=0:466:"
                      "format=yuv444:enable='gte(t,4.9917)*lt(t,5.9917)',", got)
        # the caption is hidden during both full windows and during no inset window
        self.assertIn(CAPTION_OFF.format("gte(t,0.9917)*lt(t,1.9917)+gte(t,4.9917)*lt(t,5.9917)"), got)

    def test_window_edges_are_snapped_to_the_frame_grid_and_half_open(self):
        # 1.01 s is 60.6 frames: it snaps to frame 61 (1.0167 s), and the edge sits half a frame
        # before it so that no frame time can land a hair under the literal
        got = self.filter_of(self.command({"start": 1.01, "end": 2.5, "mode": "full"}))
        self.assertIn("enable='gte(t,1.0083)*lt(t,2.4917)'", got)
        self.assertNotIn("between", got)

    def test_touching_windows_share_one_literal_so_exactly_one_is_on(self):
        got = self.filter_of(self.command({"start": 1, "end": 2, "mode": "full"},
                                          {"start": 2, "end": 3, "mode": "inset"}))
        self.assertIn("lt(t,1.9917)'", got)
        self.assertIn("gte(t,1.9917)*lt(t,2.9917)", got)

    def test_everything_but_the_filter_is_unchanged(self):
        plain = self.validate(spec(layout="kit"))["clips"][0]
        a = rt.clip_command(plain, "/tmp/t/caption.txt", "/o.mov", layout="kit")
        b = self.command({"start": 2, "end": 4, "mode": "full"})
        drop = lambda cmd: [x for x in cmd if x != self.filter_of(cmd)]  # noqa: E731
        self.assertEqual(drop(a), drop(b))

    def test_overlay_offsets_are_even_and_the_stack_stays_4_4_4(self):
        got = self.filter_of(self.command({"start": 1, "end": 2, "mode": "full"},
                                          {"start": 3, "end": 4, "mode": "inset"}))
        for x, y in __import__("re").findall(r"overlay=(\d+):(\d+):format=yuv444", got):
            self.assertEqual((int(x) % 2, int(y) % 2), (0, 0))
        self.assertEqual(got.count("format=yuv444:"), 2)    # every cam overlay keeps 4:4:4
        self.assertEqual(got.count("format=yuv444p"), 4)    # game, strip and both cam blocks
        self.assertTrue(got.index("overlay=0:466") < got.index("drawtext"))
        self.assertEqual(got.count("format=yuv420p"), 1)    # subsampled once, at the end

    def test_spec_text_still_never_reaches_the_filter(self):
        got = self.filter_of(self.command({"start": 2, "end": 4, "mode": "full"}))
        self.assertNotIn("Defense", got)

    def test_scene_commands_pass_the_windows(self):
        parsed = self.validate(cam_spec({"start": 2, "end": 4, "mode": "full"}))
        timeline = rt.build_timeline(parsed)
        with tempfile.TemporaryDirectory() as tmp:
            cmds = [cmd for _, cmd in rt._scene_commands(parsed, timeline, tmp)]
        clip_cmds = [c for c in cmds if "-ss" in c]
        self.assertIn("overlay=0:466", self.filter_of(clip_cmds[0]))
        self.assertNotIn("overlay=0:466", self.filter_of(clip_cmds[1]))

    def test_timeline_and_print_list_the_windows(self):
        parsed = self.validate(cam_spec({"start": 2, "end": 4, "mode": "full"},
                                        {"start": 5, "end": 7.5, "mode": "inset"}))
        tl = rt.build_timeline(parsed)
        self.assertEqual([(c["clip"], c["mode"], round(c["t"], 3), round(c["length"], 3))
                          for c in tl["cams"]], [(0, "full", 2.75, 2.0), (0, "inset", 5.75, 2.5)])
        lines = rt.format_timeline(tl)
        self.assertEqual([l for l in lines if "cam" in l],
                         ["00:02.750  cam full x 162  2.000 s",
                          "00:05.750  cam inset  2.500 s"])


class CamRealGraphTests(unittest.TestCase):
    """The real filter graph on 2 s of a synthetic 1920x1080 picture (software encoder)."""

    @unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
    def test_graph_runs_and_the_full_window_replaces_the_game_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mp4")
            # left third red, rest blue, with a green block where the facecam picture is
            _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                  "color=c=blue:s=1920x1080:r=30:d=2,drawbox=x=40:y=720:w=560:h=310:"
                  "color=green:t=fill", "-f", "lavfi", "-i", "sine=f=440:d=2:sample_rate=48000",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", src])
            clip = {"length": 2.0, "start_s": 0.0, "cam": [
                {"start": 1.0, "end": 2.0, "mode": "full", "x": 162}]}
            cap = os.path.join(tmp, "caption.txt")
            with open(cap, "w", encoding="utf-8") as handle:
                handle.write("hi")
            out = os.path.join(tmp, "o.mov")
            cmd = rt.clip_command({**clip, "source": src}, cap, out, layout="kit")
            cmd = [a if a != "h264_nvenc" else "libx264" for a in cmd]
            cmd = _drop_nvenc_options(cmd)
            _run(cmd)
            for t, expect_green in ((0.5, False), (1.5, True)):
                rgb = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-ss", str(t), "-i",
                            out, "-frames:v", "1", "-vf", "crop=2:2:540:900", "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-"])
                self.assertEqual(rgb[1] > 90 and rgb[2] < 60, expect_green, (t, list(rgb)))


class CamEdgeFrameTests(unittest.TestCase):
    """Which frames a window covers, on the real graph, with a clip that starts at 1.5 s."""

    FULL, INSET = (30, 60), (60, 90)        # clip frames of the two touching windows
    FRAMES = 120

    def classify(self, rgb, x, y):
        offset = (y * 270 + x) * 3
        return tuple(rgb[offset:offset + 3])

    @unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
    def test_a_window_covers_exactly_its_frames_with_a_seek_and_touching_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mp4")
            # 4 s at 60 fps: blue, a green facecam picture, a red patch at the left of it that
            # only the inset shows (the full window crops 162..476)
            _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                  "color=c=blue:s=1920x1080:r=60:d=4,drawbox=x=40:y=720:w=560:h=310:"
                  "color=green:t=fill,drawbox=x=40:y=800:w=60:h=100:color=red:t=fill",
                  "-f", "lavfi", "-i", "sine=f=440:d=4:sample_rate=48000",
                  "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", src])
            clip = {"source": src, "length": 2.0, "start_s": 1.5, "cam": [
                {"start": 0.5, "end": 1.0, "mode": "full", "x": 162},
                {"start": 1.0, "end": 1.5, "mode": "inset", "x": None}]}
            cap = os.path.join(tmp, "caption.txt")
            with open(cap, "w", encoding="utf-8") as handle:
                handle.write("MMMMMMMM")
            out = os.path.join(tmp, "o.mov")
            cmd = _drop_nvenc_options([a if a != "h264_nvenc" else "libx264"
                                       for a in rt.clip_command(clip, cap, out, layout="kit")])
            _run(cmd)
            # every frame, a quarter of the size, averaged (area) so a pixel is a block mean
            rgb = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", out, "-vf",
                        "scale=270:480:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        size = 270 * 480 * 3
        self.assertEqual(len(rgb), size * self.FRAMES)
        states = []
        for n in range(self.FRAMES):
            frame = rgb[n * size:(n + 1) * size]
            full = self.classify(frame, 225, 225)[1] > 90           # (900, 900): game or full cam
            inset = self.classify(frame, 12, 330)[0] > 150          # (50, 1320): red patch
            caption = max(min(self.classify(frame, x, y)) for y in range(162, 178)
                          for x in range(40, 230)) > 200
            states.append((full, inset, caption))
        want = [(self.FULL[0] <= n < self.FULL[1], self.INSET[0] <= n < self.INSET[1],
                 not self.FULL[0] <= n < self.FULL[1]) for n in range(self.FRAMES)]
        for n, (got, expect) in enumerate(zip(states, want)):
            self.assertEqual(got, expect, f"frame {n} (full, inset, caption)")


BOTTOM_CAM_STRIP = STRIP       # the strip chain is the same crop on every layout
BOTTOM_CAM_GAME_WIDE = ("[b]crop=948:839:486:126,scale=1080:956:flags=lanczos,"
                     "format=yuv444p[g];")
BOTTOM_CAM_FILTER = ("[0:v]fps=60,split=2[a][b];"
                  "[a]crop=1380:126:270:0,scale=1080:99:flags=lanczos[s];"
                  "[b]crop=948:839:486:126,scale=1080:956:flags=lanczos[g];"
                  "[s][g]vstack=inputs=2,pad=1080:1920:0:432:black,setsar=1," + TEXT_FILTER +
                  ",format=yuv420p[v]")
BOTTOM_CAM_FULL_CHAIN = ("[w{k}]crop=274:242:{x}:788,scale=1080:956:flags=lanczos,"
                      "unsharp=5:5:0.5,format=yuv444p[cam{k}];")
BOTTOM_CAM_INSET_CHAIN = ("[w{k}]crop=440:242:36:788,scale=600:330:flags=lanczos,"
                       "unsharp=5:5:0.5,format=yuv444p[cam{k}];")
BOTTOM_CAM_STACK = "[s][g]vstack=inputs=2,pad=1080:1920:0:432:black"
AUDIO_9S = (";[0:a]aresample=48000,aformat=channel_layouts=stereo,"
            "afade=t=in:d=0.05,afade=t=out:st=8.940:d=0.06,apad=whole_dur=9.000[a]")


def filter_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def layout_spec(top="bottom-cam", first=None, second=None, cam=None):
    """BASE_SPEC with a top level layout and optional per clip layouts and cam windows."""
    raw = spec(layout=top)
    for clip, layout in zip(raw["clips"], (first, second)):
        if layout is not None:
            clip["layout"] = layout
    if cam is not None:
        raw["clips"][0]["cam"] = list(cam)
    return raw


class ClipLayoutFieldTests(SfxDirCase):
    """The per clip layout field overrides the top level layout for that clip."""

    def layouts(self, raw):
        parsed = self.validate(raw)
        return [rt.clip_layout(c, parsed["layout"]) for c in parsed["clips"]]

    def test_a_clip_without_it_uses_the_top_level_layout(self):
        self.assertEqual(self.layouts(layout_spec("kit")), ["kit", "kit"])
        self.assertEqual(self.layouts(BASE_SPEC), ["kit", "kit"])
        self.assertIsNone(self.validate(BASE_SPEC)["clips"][0]["layout"])

    def test_it_overrides_the_top_level_for_its_clip_only(self):
        self.assertEqual(self.layouts(layout_spec("bottom-cam", second="kit")), ["bottom-cam", "kit"])
        self.assertEqual(self.layouts(layout_spec("kit", "bottom-cam", "kit-reaction")),
                         ["bottom-cam", "kit-reaction"])

    def test_it_takes_the_same_values_as_the_top_level_field(self):
        for name in rt.LAYOUTS:
            self.assertEqual(self.layouts(layout_spec("bottom-cam", first=name))[0], name)

    def test_unknown_clip_layout_is_refused_and_names_the_clip(self):
        for bad in ("md4", "KIT", 3, None, True):
            raw = layout_spec("bottom-cam")
            raw["clips"][1]["layout"] = bad
            self.assertInvalid(raw, "clip 2: layout")
        self.assertInvalid(layout_spec("bottom-cam", second="x"), "kit-reaction")

    def test_the_clip_field_is_a_known_key(self):
        raw = layout_spec("bottom-cam")
        raw["clips"][0]["layouts"] = "kit"
        self.assertInvalid(raw, "layouts")

    def test_a_clip_command_uses_the_layout_of_its_clip(self):
        parsed = self.validate(layout_spec("bottom-cam", "kit", "bottom-cam"))
        first, second = parsed["clips"]
        got = [filter_of(rt.clip_command(c, "/tmp/t/caption.txt", "/o.mov",
                                         layout=parsed["layout"])) for c in (first, second)]
        self.assertEqual(got[0], KIT_FILTER + AUDIO_9S)
        self.assertEqual(got[1], BOTTOM_CAM_FILTER + AUDIO_9S)

    def test_each_clip_picture_uses_its_own_pad_y_and_the_caption_stays_put(self):
        for top, first in (("bottom-cam", "kit"), ("kit", "bottom-cam"), ("kit", "kit-reaction")):
            parsed = self.validate(layout_spec(top, first))
            cmd = rt.clip_command(parsed["clips"][0], "/tmp/t/caption.txt", "/o.mov",
                                  layout=parsed["layout"])
            self.assertIn(f"pad=1080:1920:0:{rt.PAD_Y[first]}:black", filter_of(cmd))
            self.assertIn(":y=640,format=yuv420p[v]", filter_of(cmd))

    def test_a_spec_without_the_field_renders_the_same_commands_as_before(self):
        parsed = self.validate(BASE_SPEC)
        cmd = rt.clip_command(parsed["clips"][0], "/tmp/t/caption.txt", "/o.mov")
        self.assertEqual(filter_of(cmd), KIT_FILTER + AUDIO_9S)
        parsed = self.validate(layout_spec("kit-reaction"))
        cmd = rt.clip_command(parsed["clips"][1], "/tmp/t/caption.txt", "/o.mov",
                              layout="kit-reaction")
        self.assertEqual(filter_of(cmd), KIT_REACTION_FILTER + AUDIO_9S)

    def test_scene_commands_mix_the_layouts_of_one_video(self):
        parsed = self.validate(layout_spec("bottom-cam", second="kit"))
        timeline = rt.build_timeline(parsed)
        with tempfile.TemporaryDirectory() as tmp:
            cmds = [cmd for _, cmd in rt._scene_commands(parsed, timeline, tmp)]
        pads = [re.search(r"pad=1080:1920:0:(\d+):black", filter_of(c)).group(1)
                for c in cmds if "-ss" in c]
        self.assertEqual(pads, ["432", "367"])

    def test_the_timeline_names_the_layout_only_of_clips_that_differ(self):
        parsed = self.validate(layout_spec("bottom-cam", second="kit"))
        lines = rt.format_timeline(rt.build_timeline(parsed))
        self.assertEqual([l for l in lines if "clip" in l],
                         ["00:00.750  clip 1  9.000 s", "00:10.500  clip 2 (kit)  9.000 s"])
        same = rt.format_timeline(rt.build_timeline(self.validate(layout_spec("kit", "kit"))))
        self.assertEqual([l for l in same if "clip" in l],
                         ["00:00.750  clip 1  9.000 s", "00:10.500  clip 2  9.000 s"])


class CamLayoutValidationTests(SfxDirCase):
    """cam is checked against the layout the clip really uses."""

    FULL = {"start": 2, "end": 4, "mode": "full"}
    INSET = {"start": 5, "end": 7.5, "mode": "inset"}

    def cam(self, raw):
        return self.validate(raw)["clips"][0]["cam"]

    def test_bottom_cam_accepts_both_modes_and_defaults_x_to_the_centre(self):
        got = self.cam(layout_spec("bottom-cam", cam=[self.FULL, self.INSET]))
        self.assertEqual([(w["mode"], w["x"]) for w in got], [("full", 118), ("inset", None)])

    def test_a_clip_override_decides_not_the_top_level(self):
        # top level kit-reaction refuses cam, but the clip is kit
        self.assertEqual(self.cam(layout_spec("kit-reaction", "kit", cam=[self.FULL]))[0]["x"],
                         162)
        # top level kit accepts cam, but the clip is kit-reaction
        self.assertInvalid(layout_spec("kit", "kit-reaction", cam=[self.FULL]),
                           "clip 1: cam windows need layout bottom-cam or kit (got kit-reaction)")
        # the default for x follows the clip layout too
        self.assertEqual(self.cam(layout_spec("kit", "bottom-cam", cam=[self.FULL]))[0]["x"], 118)
        self.assertEqual(self.cam(layout_spec("bottom-cam", "kit", cam=[self.FULL]))[0]["x"], 162)

    def test_a_clip_without_cam_may_use_any_layout(self):
        self.validate(layout_spec("kit-reaction", second="kit-reaction"))

    def test_x_range_follows_the_layout(self):
        for x in (36, 118, 202):
            self.assertEqual(self.cam(layout_spec("bottom-cam", cam=[{**self.FULL, "x": x}]))[0]["x"], x)
        for bad in (34, 204, 290, 117, 118.5, -2, "118", True, None):
            self.assertInvalid(layout_spec("bottom-cam", cam=[{**self.FULL, "x": bad}]), "x")
        # 290 is the kit limit, 204 is above the bottom-cam one
        self.assertEqual(self.cam(layout_spec("kit", cam=[{**self.FULL, "x": 290}]))[0]["x"], 290)
        self.assertInvalid(layout_spec("kit", "bottom-cam", cam=[{**self.FULL, "x": 290}]), "x")
        self.assertInvalid(layout_spec("bottom-cam", cam=[{**self.FULL, "x": 204}]), "202")

    def test_x_is_still_only_for_full_on_bottom_cam(self):
        self.assertInvalid(layout_spec("bottom-cam", cam=[{**self.INSET, "x": 100}]), "only for full")


class BottomCamGeometryTests(unittest.TestCase):
    """The bottom-cam numbers were measured on real frames: the facecam picture is x 36..476,
    y 788..1030 in the source."""

    def geo(self):
        return rt.CAM_GEOMETRY["bottom-cam"]

    def test_the_table_holds_bottom_cam_and_kit_only(self):
        self.assertEqual(set(rt.CAM_GEOMETRY), {"bottom-cam", "kit"})

    def test_kit_entry_keeps_the_existing_constants(self):
        geo = rt.CAM_GEOMETRY["kit"]
        self.assertEqual((geo.full_crop, geo.full_x_range, geo.full_size),
                         (rt.CAM_FULL_CROP, rt.CAM_FULL_X_RANGE, rt.KIT_GAME_SIZE))
        self.assertEqual((geo.inset_crop, geo.inset_size, geo.inset_at, geo.game_y),
                         (rt.CAM_CROP, rt.CAM_INSET_SIZE, rt.CAM_INSET_AT, rt.CAM_GAME_Y))

    def test_full_crop_lies_inside_the_picture_and_centres_on_it(self):
        w, h, x, y = self.geo().full_crop
        lo, hi = self.geo().full_x_range
        self.assertEqual((lo, hi, w, h, y), (36, 476, 274, 242, 788))
        self.assertEqual(x, 118)
        self.assertAlmostEqual(x + w / 2, (36 + 476) / 2, delta=1)   # 119 would be odd
        self.assertEqual(hi - w, 202)
        self.assertLessEqual(y + h, 1030)
        for value in (w, h, x, y, lo):
            self.assertEqual(value % 2, 0, value)
        self.assertAlmostEqual(h * rt.WIDTH / w, rt.GAME_SIZE[1], delta=0.01 * rt.GAME_SIZE[1])
        self.assertEqual(self.geo().full_size, rt.GAME_SIZE)

    def test_inset_is_the_whole_picture_scaled_down(self):
        geo = self.geo()
        self.assertEqual(geo.inset_crop, (440, 242, 36, 788))
        self.assertEqual(geo.inset_size, (600, 330))
        self.assertAlmostEqual(440 / 242, 600 / 330, delta=0.01)

    def test_inset_sits_about_24_px_above_the_bottom_of_the_game_block(self):
        geo = self.geo()
        self.assertEqual(geo.game_y, rt.PAD_Y["bottom-cam"] + rt.STRIP_SIZE[1])
        bottom = geo.game_y + rt.GAME_SIZE[1]
        self.assertEqual(bottom, 1487)
        self.assertEqual(geo.inset_at, (24, 1132))
        self.assertEqual((geo.inset_at[0] % 2, geo.inset_at[1] % 2), (0, 0))
        self.assertIn(bottom - (geo.inset_at[1] + geo.inset_size[1]), (24, 25))

    def test_the_inset_fits_inside_the_game_block(self):
        geo = self.geo()
        self.assertGreaterEqual(geo.inset_at[1], geo.game_y)
        self.assertLessEqual(geo.inset_at[0] + geo.inset_size[0], rt.WIDTH)


class BottomCamCommandTests(SfxDirCase):
    """Bottom-cam windows: the picture is stacked in 4:4:4 as soon as a window is present."""

    def command(self, *windows, top="bottom-cam", clip_layout=None):
        raw = layout_spec(top, clip_layout, cam=windows)
        parsed = self.validate(raw)
        return rt.clip_command(parsed["clips"][0], "/tmp/t/caption.txt", "/o.mov",
                               layout=parsed["layout"])

    def test_no_cam_keeps_the_exact_current_command_and_stays_4_2_0(self):
        got = filter_of(self.command())
        self.assertEqual(got, BOTTOM_CAM_FILTER + AUDIO_9S)
        self.assertNotIn("yuv444", got)
        self.assertEqual(rt.clip_command(self.validate(BASE_SPEC)["clips"][0],
                                         "/tmp/t/caption.txt", "/o.mov", layout="bottom-cam"),
                         self.command())

    def test_full_window_filter_exact(self):
        got = filter_of(self.command({"start": 2, "end": 4, "mode": "full"}))
        want = ("[0:v]fps=60,split=3[a][b][w0];" + BOTTOM_CAM_STRIP + BOTTOM_CAM_GAME_WIDE
                + BOTTOM_CAM_FULL_CHAIN.format(k=0, x=118) + BOTTOM_CAM_STACK + "[st];"
                "[st][cam0]overlay=0:531:format=yuv444:enable='gte(t,1.9917)*lt(t,3.9917)',setsar=1,"
                + CAPTION_OFF.format("gte(t,1.9917)*lt(t,3.9917)") + ",format=yuv420p[v]")
        self.assertEqual(got, want + AUDIO_9S)

    def test_inset_window_filter_exact_and_caption_stays(self):
        got = filter_of(self.command({"start": 5, "end": 7.5, "mode": "inset"}))
        want = ("[0:v]fps=60,split=3[a][b][w0];" + BOTTOM_CAM_STRIP + BOTTOM_CAM_GAME_WIDE
                + BOTTOM_CAM_INSET_CHAIN.format(k=0) + BOTTOM_CAM_STACK + "[st];"
                "[st][cam0]overlay=24:1132:format=yuv444:enable='gte(t,4.9917)*lt(t,7.4917)',setsar=1,"
                + TEXT_FILTER + ",format=yuv420p[v]")
        self.assertEqual(got, want + AUDIO_9S)

    def test_several_windows_chain_with_their_own_x_and_half_open_edges(self):
        got = filter_of(self.command({"start": 1, "end": 2, "mode": "full", "x": 202},
                                     {"start": 2, "end": 3, "mode": "inset"},
                                     {"start": 5, "end": 6, "mode": "full"}))
        self.assertIn("split=5[a][b][w0][w1][w2];", got)
        self.assertIn(BOTTOM_CAM_FULL_CHAIN.format(k=0, x=202) + BOTTOM_CAM_INSET_CHAIN.format(k=1)
                      + BOTTOM_CAM_FULL_CHAIN.format(k=2, x=118) + BOTTOM_CAM_STACK + "[st];", got)
        self.assertIn("[st][cam0]overlay=0:531:format=yuv444:enable='gte(t,0.9917)*lt(t,1.9917)'"
                      "[o0];[o0][cam1]overlay=24:1132:format=yuv444:"
                      "enable='gte(t,1.9917)*lt(t,2.9917)'[o1];[o1][cam2]overlay=0:531:"
                      "format=yuv444:enable='gte(t,4.9917)*lt(t,5.9917)',", got)
        self.assertIn(CAPTION_OFF.format("gte(t,0.9917)*lt(t,1.9917)+gte(t,4.9917)*lt(t,5.9917)"),
                      got)
        self.assertNotIn("between", got)

    def test_4_4_4_stack_then_one_subsample_at_the_end(self):
        got = filter_of(self.command({"start": 1, "end": 2, "mode": "full"},
                                     {"start": 3, "end": 4, "mode": "inset"}))
        self.assertEqual(got.count("format=yuv444:"), 2)    # both overlays keep 4:4:4
        self.assertEqual(got.count("format=yuv444p"), 4)    # strip, game and both cam blocks
        self.assertEqual(got.count("format=yuv420p"), 1)
        self.assertLess(got.index("vstack"), got.index("format=yuv444:"))
        self.assertLess(got.rindex("overlay="), got.index("drawtext"))

    def test_overlay_offsets_are_even_where_the_geometry_allows(self):
        got = filter_of(self.command({"start": 1, "end": 2, "mode": "full"},
                                     {"start": 3, "end": 4, "mode": "inset"}))
        offsets = re.findall(r"overlay=(\d+):(\d+):format=yuv444", got)
        self.assertEqual(offsets, [("0", "531"), ("24", "1132")])
        # the full window must sit exactly on the game block, whose top (432 + 99) is odd: that
        # is safe because the overlay runs in 4:4:4, where nothing is rounded to chroma pairs
        for x, y in offsets:
            self.assertEqual(int(x) % 2, 0)

    def test_a_clip_override_uses_the_geometry_of_its_own_layout(self):
        kit = filter_of(self.command({"start": 2, "end": 4, "mode": "full"}, top="bottom-cam",
                                     clip_layout="kit"))
        self.assertIn("overlay=0:466:", kit)
        self.assertIn("crop=314:316:162:716", kit)
        bottom_cam = filter_of(self.command({"start": 2, "end": 4, "mode": "full"}, top="kit",
                                         clip_layout="bottom-cam"))
        self.assertIn("overlay=0:531:", bottom_cam)
        self.assertIn("crop=274:242:118:788", bottom_cam)

    def test_the_builder_refuses_cam_on_kit_reaction(self):
        with self.assertRaises(rt.ToolError):
            rt._picture_graph("kit-reaction", [{"start": 1, "end": 2, "mode": "full", "x": 162}])

    def test_everything_but_the_filter_is_unchanged(self):
        plain, win = self.command(), self.command({"start": 2, "end": 4, "mode": "full"})
        drop = lambda cmd: [x for x in cmd if x != filter_of(cmd)]  # noqa: E731
        self.assertEqual(drop(plain), drop(win))

    def test_timeline_lists_bottom_cam_windows(self):
        parsed = self.validate(layout_spec("bottom-cam", cam=[
            {"start": 2, "end": 4, "mode": "full"}, {"start": 5, "end": 7.5, "mode": "inset"}]))
        lines = rt.format_timeline(rt.build_timeline(parsed))
        self.assertEqual([l for l in lines if "cam" in l],
                         ["00:02.750  cam full x 118  2.000 s",
                          "00:05.750  cam inset  2.500 s"])


class BottomCamEdgeFrameTests(unittest.TestCase):
    """Bottom-cam windows on the real graph: the right frames, the caption, the output size."""

    FULL, INSET = (30, 60), (60, 90)
    FRAMES = 120

    @unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
    def test_full_and_inset_land_on_their_frames_and_only_full_hides_the_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mp4")
            # blue, a green facecam picture (x 36..476, y 788..1030) with a red patch at its
            # left edge: the full crop starts at x 118, so only the inset shows the red
            _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i",
                  "color=c=blue:s=1920x1080:r=60:d=4,drawbox=x=36:y=788:w=440:h=242:"
                  "color=green:t=fill,drawbox=x=36:y=820:w=60:h=100:color=red:t=fill",
                  "-f", "lavfi", "-i", "sine=f=440:d=4:sample_rate=48000",
                  "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", src])
            clip = {"source": src, "length": 2.0, "start_s": 1.5, "cam": [
                {"start": 0.5, "end": 1.0, "mode": "full", "x": 118},
                {"start": 1.0, "end": 1.5, "mode": "inset", "x": None}]}
            cap = os.path.join(tmp, "caption.txt")
            with open(cap, "w", encoding="utf-8") as handle:
                handle.write("MMMMMMMM")
            out = os.path.join(tmp, "o.mov")
            cmd = _drop_nvenc_options([a if a != "h264_nvenc" else "libx264"
                                       for a in rt.clip_command(clip, cap, out,
                                                                layout="bottom-cam")])
            _run(cmd)
            size = rt.probe_source(out)
            rgb = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", out, "-vf",
                        "scale=270:480:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        self.assertEqual((size["width"], size["height"]), (1080, 1920))
        frame_size = 270 * 480 * 3
        self.assertEqual(len(rgb), frame_size * self.FRAMES)

        def pixel(frame, x, y):
            offset = (y * 270 + x) * 3
            return tuple(frame[offset:offset + 3])

        for n in range(self.FRAMES):
            frame = rgb[n * frame_size:(n + 1) * frame_size]
            full = pixel(frame, 225, 225)[1] > 90            # (900, 900) in the game block
            inset = pixel(frame, 12, 310)[0] > 150           # (50, 1240): the red patch
            caption = max(min(pixel(frame, x, y)) for y in range(162, 178)
                          for x in range(40, 230)) > 200
            want = (self.FULL[0] <= n < self.FULL[1], self.INSET[0] <= n < self.INSET[1],
                    not self.FULL[0] <= n < self.FULL[1])
            self.assertEqual((full, inset, caption), want, f"frame {n} (full, inset, caption)")


def _drop_nvenc_options(cmd):
    """Swap the nvenc rate-control flags for ones libx264 accepts (tests run without a GPU)."""
    drop = {"-preset": 2, "-rc": 2, "-cq": 2, "-b:v": 2, "-maxrate": 2, "-bufsize": 2,
            "-bf": 2, "-profile:v": 2}
    out, skip = [], 0
    for a in cmd:
        if skip:
            skip -= 1
        elif a in drop:
            skip = drop[a] - 1
        else:
            out.append(a)
    return out


def _tone(path, freq, seconds, amp, rate, codec="pcm_s16le"):
    _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-threads", "2", "-f", "lavfi",
          "-i", f"sine=f={freq}:d={seconds}:sample_rate={rate},volume={amp}",
          "-ac", "2", "-c:a", codec, path])


def _zero_crossings_per_second(samples, start, end, rate=48000):
    chunk = samples[int(start * rate):int(end * rate)]
    crossings = sum(1 for a, b in zip(chunk, chunk[1:]) if (a < 0) != (b < 0))
    return crossings / (end - start)


@unittest.skipUnless(FFMPEG_OK, "ffmpeg and ffprobe are needed")
class RealCrowdMixTests(unittest.TestCase):
    """Synthetic tones through the real mix graph: 0.5 s card, 2 s clip, 0.5 s card, 2 s clip."""

    CARD = (2.65, 2.9)     # inside the second card (2.5 to 3.0 s)
    CLIP1 = (1.0, 2.0)     # inside the first clip (0.5 to 2.5 s)

    def render_mix(self, tmp, raw):
        base = os.path.join(tmp, "base.mov")
        _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-threads", "2",
              "-f", "lavfi", "-i", "color=c=black:s=64x64:r=30:d=5",
              "-f", "lavfi", "-i", "sine=f=220:d=5:sample_rate=48000,volume=0.002",
              "-c:v", "mpeg4", "-c:a", "pcm_s16le", "-ac", "2", base])
        parsed = rt.validate_spec(raw)
        tl = rt.build_timeline(parsed, {p: rt.probe_wav_duration(p) for p in (
            [parsed["crowd_bed"]["path"]] if parsed["crowd_bed"] else [])
            + [e["path"] for e in rt.build_timeline(parsed)["events"] if e["kind"] == "sfx"]})
        measured = rt.parse_loudnorm(rt.run_stderr(rt.measure_command(base, tl, None)))
        out = os.path.join(tmp, "out.mp4")
        _run(rt.mix_command(base, tl, None, out, measured))
        pcm = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-i", out, "-f", "f32le",
                    "-ac", "1", "-ar", "48000", "-"])
        samples = array("f")
        samples.frombytes(pcm)
        return tl, samples

    def make_spec(self, tmp, **extra):
        raw = {"caption": "A", "output": os.path.join(tmp, "o.mp4"), "card_seconds": 0.5,
               "sfx_dir": tmp, "clips": [{"source": "/x.mp4", "start": 10, "end": 12},
                                          {"source": "/x.mp4", "start": 20, "end": 22}]}
        raw.update(extra)
        return raw

    def test_bed_is_present_in_clips_silent_in_cards_and_a_96k_input_keeps_its_pitch(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tone(os.path.join(tmp, "bed.wav"), 440, 3, 0.3, 96000, "pcm_s24le")
            _tone(os.path.join(tmp, "card.wav"), 50, 0.5, 0.01, 48000)
            raw = self.make_spec(tmp, crowd_bed={"name": "bed", "gain_db": -12},
                                 card_sound={"name": "card", "gain_db": -30})
            tl, samples = self.render_mix(tmp, raw)
            self.assertEqual(len(tl["beds"]), 2)
            in_clip = _rms_db(samples, *self.CLIP1)
            in_card = _rms_db(samples, *self.CARD)
            self.assertGreater(in_clip - in_card, 25.0, (in_clip, in_card))
            # 440 Hz crosses zero 880 times a second; a 96 kHz file played as 48 kHz would not
            rate = _zero_crossings_per_second(samples, *self.CLIP1)
            self.assertAlmostEqual(rate, 880, delta=40)

    def test_a_stop_at_clip_end_sound_is_gone_during_the_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tone(os.path.join(tmp, "roar.wav"), 1000, 4, 0.3, 48000)
            _tone(os.path.join(tmp, "card.wav"), 50, 0.5, 0.01, 48000)
            raw = self.make_spec(tmp, card_sound={"name": "card", "gain_db": -30})
            raw["clips"][0]["sfx"] = [{"at": 0.3, "name": "roar", "stop_at_clip_end": True}]
            tl, samples = self.render_mix(tmp, raw)
            event = [e for e in tl["events"] if e["kind"] == "sfx"][0]
            self.assertAlmostEqual(event["t"] + event["length"], 2.5)
            in_clip = _rms_db(samples, *self.CLIP1)
            in_card = _rms_db(samples, *self.CARD)
            self.assertGreater(in_clip - in_card, 25.0, (in_clip, in_card))


class ExampleSpecTests(unittest.TestCase):
    """tiktoks/example.json is the file the README tells people to copy: it must stay valid and
    must carry placeholder paths only."""

    PATH = os.path.join(HERE, "tiktoks", "example.json")

    def test_the_example_spec_is_valid(self):
        spec = rt.load_spec(self.PATH)
        self.assertEqual(spec["layout"], "kit")
        self.assertIsNotNone(spec["hook"])
        self.assertIsNotNone(spec["crowd_stem"])
        self.assertEqual(len(spec["clips"]), 2)
        self.assertTrue(rt.build_timeline(spec)["total"] >= rt.MIN_TOTAL_SECONDS)

    def test_the_example_spec_uses_placeholder_paths_only(self):
        with open(self.PATH, encoding="utf-8") as handle:
            text = handle.read()
        paths = re.findall(r'"(/[^"]*)"', text)
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(path.startswith("/path/to/"), path)


if __name__ == "__main__":
    unittest.main()
