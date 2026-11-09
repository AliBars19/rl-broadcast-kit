"""Tests for the `broadcast` layout of render_tiktok, and a golden check on the commands and
timeline of a spec that uses the other layouts.

Everything is checked on argument lists, except BroadcastEndToEndTests, which encodes a
2.5 s synthetic clip with the software encoder (no GPU needed).
"""
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import render_tiktok as rt  # noqa: E402

GOLDEN_MEASURED = {"input_i": -23.5, "input_lra": 7.2, "input_tp": -3.1, "input_thresh": -34.0,
                   "target_offset": 0.4}


class SfxDirCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.sfx_dir = self._dir.name
        for name in ("card_hit", "impact", "riser", "hit"):
            open(os.path.join(self.sfx_dir, name + ".wav"), "wb").close()

    def validate(self, raw):
        return rt.validate_spec(raw, sfx_dir=self.sfx_dir)

    def assertInvalid(self, bad, needle):
        with self.assertRaises(rt.SpecError) as ctx:
            self.validate(bad)
        self.assertIn(needle, str(ctx.exception))


def golden_raw(with_stem=False):
    """A spec that exercises every layout, the hook, cam windows, bed or stem, and music."""
    clips = [
        {"source": "/v/s.mp4", "start": "00:27:33", "end": "00:27:42", "layout": "kit",
         "sfx": [{"at": 6.2, "name": "impact", "fade_out": 0.1}],
         "cam": [{"start": 2, "end": 4, "mode": "full"},
                 {"start": 5, "end": 7.5, "mode": "inset"}]},
        {"source": "/v/s.mp4", "start": "01:23:26", "end": "01:23:35", "layout": "bottom-cam",
         "sfx": [{"at": -0.5, "name": "riser"}],
         "cam": [{"start": 1, "end": 3, "mode": "full", "x": 150}]},
        {"source": "/v/t.mp4", "start": 100, "end": 108.5, "layout": "kit-reaction"},
    ]
    raw = {"caption": "Defense.", "output": "/o/d.mp4", "layout": "kit", "card_seconds": 0.75,
           "clip_comp": True, "music": {"path": "/m/m.wav", "gain_db": -18, "start": 3},
           "hook": {"source": "/v/h.mp4", "start": "00:10:00", "end": "00:10:05",
                    "sfx": [{"at": 3.0, "name": "impact"}]},
           "clips": clips}
    raw["crowd_stem" if with_stem else "crowd_bed"] = (
        {"path": "/s/stem.wav", "gain_db": -3} if with_stem else {"name": "hit"})
    return raw


def all_commands(parsed, tmp):
    """Every ffmpeg command of a render of `parsed`, plus its timeline, as one JSON text."""
    durations = {c_sfx["path"]: 2.0 for _, c in rt.scene_clips(parsed) for c_sfx in c["sfx"]}
    if parsed["crowd_bed"]:
        durations[parsed["crowd_bed"]["path"]] = 11.0
    if parsed["crowd_stem"]:
        durations[parsed["crowd_stem"]["path"]] = 60.0
    timeline = rt.build_timeline(parsed, durations)
    scenes = [cmd for _, cmd in rt._scene_commands(parsed, timeline, tmp)]
    joined = os.path.join(tmp, "joined.mov")
    cmds = scenes + [rt.concat_command(os.path.join(tmp, "scenes.txt"), joined),
                     rt.measure_command(joined, timeline, parsed["music"]),
                     rt.mix_command(joined, timeline, parsed["music"], "/o/d.part.mp4",
                                    GOLDEN_MEASURED)]
    return json.dumps({"cmds": cmds, "timeline": timeline,
                       "lines": rt.format_timeline(timeline)}, sort_keys=True)


class GoldenCommandTests(SfxDirCase):
    """Hashes of the commands and timeline of a spec that uses every layout except broadcast, the
    hook, cam windows, a bed or a stem, and music. A change to any ffmpeg argument or timeline line
    shows up here; if it is deliberate, print the new digest and update GOLDEN. Temp and sound
    folders are replaced by placeholders, so the hashes do not depend on where the tests run.
    """

    GOLDEN = {
        False: "c42729cac6a8980f7c21f4633edfd6ba16c3e3fd2e6dbdb341e120672c87af53",
        True: "2a91ebac944d5e3e851eb517b653656ae876baa81f23720125d35b8739fcfda5"}

    def digest(self, with_stem):
        parsed = self.validate(golden_raw(with_stem))
        with tempfile.TemporaryDirectory() as tmp:
            text = all_commands(parsed, tmp)
        text = text.replace(tmp, "<TMP>").replace(self.sfx_dir, "<SFX>")
        text = text.replace(rt.SFX_DIR, "<SFX>")      # the built-in card sound, wherever we are
        text = text.replace(rt.FONT_PATH, "<FONT>")   # TIKTOK_FONT may point elsewhere
        return hashlib.sha256(text.encode()).hexdigest()

    def test_bed_spec_commands_and_timeline_are_unchanged(self):
        self.assertEqual(self.digest(False), self.GOLDEN[False])

    def test_stem_spec_commands_and_timeline_are_unchanged(self):
        self.assertEqual(self.digest(True), self.GOLDEN[True])


# ---------------------------------------------------------------- broadcast layout

FONT = rt.FONT_PATH
TEXT_FILTER = ("drawtext=fontfile=" + FONT + ":textfile=/tmp/t/caption.txt:expansion=none"
               ":fontsize=64:fontcolor=white:borderw=4:bordercolor=black"
               ":x=(w-text_w)/2:y=640")
BG_CHAIN = ("[k]scale=270:480:force_original_aspect_ratio=increase:flags=bilinear,crop=270:480,"
            "format=yuv420p,gblur=sigma=8,eq=brightness=-0.25:saturation=0.8,"
            "scale=1080:1920:flags=bicubic[bg];")


def broadcast_filter(zoom="1.25", y=706):
    """The expected picture part of the filter graph of a broadcast clip."""
    return ("[0:v]fps=60:start_time=0,split=2[m][k];" + BG_CHAIN +
            f"[m]crop=trunc(iw/{zoom}/2)*2:trunc(ih/{zoom}/2)*2,scale=1080:608:flags=lanczos[fg];"
            f"[bg][fg]overlay=0:{y},setsar=1," + TEXT_FILTER + ",format=yuv420p[v]")


def filter_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def broadcast_spec(layout="broadcast", clip_layout=True, **clip_changes):
    """Two 9 s clips; the first is a broadcast clip (by its own field or by the top level)."""
    raw = {"caption": "Defense.", "output": "/o/d.mp4", "card_seconds": 0.75,
           "layout": layout if not clip_layout else "bottom-cam",
           "clips": [{"source": "/v/broadcast.mp4", "start": "00:00:10", "end": "00:00:19",
                      **clip_changes},
                     {"source": "/v/s.mp4", "start": "00:27:33", "end": "00:27:42"}]}
    if clip_layout:
        raw["clips"][0]["layout"] = layout
    return raw


class BroadcastValidationTests(SfxDirCase):
    def test_accepted_on_a_clip_the_hook_and_the_top_level(self):
        on_clip = self.validate(broadcast_spec())
        self.assertEqual(rt.clip_layout(on_clip["clips"][0], on_clip["layout"]), "broadcast")
        self.assertEqual(rt.clip_layout(on_clip["clips"][1], on_clip["layout"]), "bottom-cam")
        raw = broadcast_spec("bottom-cam")
        raw["hook"] = {"source": "/v/broadcast.mp4", "start": 5, "end": 9, "layout": "broadcast"}
        parsed = self.validate(raw)
        self.assertEqual(rt.clip_layout(parsed["hook"], parsed["layout"]), "broadcast")
        top = self.validate(broadcast_spec(clip_layout=False))
        self.assertEqual(top["layout"], "broadcast")
        self.assertEqual(top["clips"][0]["zoom"], rt.BROADCAST_DEFAULT_ZOOM)

    def test_the_default_layout_is_still_kit(self):
        self.assertEqual(rt.DEFAULT_LAYOUT, "kit")
        raw = broadcast_spec()
        del raw["layout"]
        self.assertEqual(self.validate(raw)["layout"], "kit")

    def test_the_error_for_an_unknown_layout_lists_broadcast(self):
        self.assertInvalid(broadcast_spec("wide"), "broadcast")

    def test_zoom_defaults_to_1_25_and_is_kept_as_a_float(self):
        self.assertEqual(rt.BROADCAST_DEFAULT_ZOOM, 1.25)
        self.assertEqual(self.validate(broadcast_spec())["clips"][0]["zoom"], 1.25)
        self.assertEqual(self.validate(broadcast_spec(zoom=1))["clips"][0]["zoom"], 1.0)

    def test_zoom_range_is_1_to_1_6_inclusive(self):
        for good in (1.0, 1.0001, 1.3, 1.6):
            self.assertEqual(self.validate(broadcast_spec(zoom=good))["clips"][0]["zoom"], good)
        for bad in (0.99, 0, -1, 1.61, 2, 160):
            self.assertInvalid(broadcast_spec(zoom=bad), "clip 1 zoom")
            self.assertInvalid(broadcast_spec(zoom=bad), "outside 1 to 1.6")

    def test_zoom_must_be_a_finite_number(self):
        for bad in ("1.2", True, None, [1.2], float("nan"), float("inf")):
            self.assertInvalid(broadcast_spec(zoom=bad), "zoom")

    def test_zoom_on_the_hook_is_checked_and_named(self):
        raw = broadcast_spec("bottom-cam")
        raw["hook"] = {"source": "/v/r.mp4", "start": 5, "end": 9, "layout": "broadcast",
                       "zoom": 3}
        self.assertInvalid(raw, "hook zoom")
        raw["hook"]["zoom"] = 1.4
        self.assertEqual(self.validate(raw)["hook"]["zoom"], 1.4)

    def test_zoom_is_only_for_broadcast_clips(self):
        for layout in ("bottom-cam", "kit", "kit-reaction"):
            raw = broadcast_spec(layout, zoom=1.2)
            self.assertInvalid(raw, "clip 1: zoom is only for layout broadcast")
            self.assertInvalid(raw, f"(got {layout})")

    def test_clips_of_other_layouts_get_no_zoom_key(self):
        parsed = self.validate(broadcast_spec())
        self.assertNotIn("zoom", parsed["clips"][1])

    def test_cam_windows_are_refused_with_a_clear_message(self):
        cam = [{"start": 2, "end": 4, "mode": "full"}]
        self.assertInvalid(broadcast_spec(cam=cam),
                           "clip 1: cam windows are not allowed on a broadcast clip")
        self.assertInvalid(broadcast_spec(cam=[]), "not allowed on a broadcast clip")
        raw = broadcast_spec("bottom-cam")
        raw["hook"] = {"source": "/v/r.mp4", "start": 5, "end": 9, "layout": "broadcast",
                       "cam": cam}
        self.assertInvalid(raw, "hook: cam windows are not allowed on a broadcast clip")
        top = broadcast_spec(clip_layout=False)
        top["clips"][0]["cam"] = cam
        self.assertInvalid(top, "clip 1: cam windows are not allowed on a broadcast clip")

    def test_cam_is_still_fine_on_the_other_clip_of_a_broadcast_spec(self):
        raw = broadcast_spec()
        raw["clips"][1]["cam"] = [{"start": 2, "end": 4, "mode": "full"}]
        self.assertEqual(len(self.validate(raw)["clips"][1]["cam"]), 1)

    def test_sfx_work_on_a_broadcast_clip(self):
        parsed = self.validate(broadcast_spec(sfx=[{"at": 3, "name": "impact"}]))
        events = rt.build_timeline(parsed)["events"]
        self.assertIn(("impact", 3.75), [(e["name"], e["t"]) for e in events])

    def test_validation_does_not_change_its_input(self):
        raw = broadcast_spec(zoom=1.3)
        raw["hook"] = {"source": "/v/r.mp4", "start": 5, "end": 9, "layout": "broadcast"}
        before = copy.deepcopy(raw)
        self.validate(raw)
        self.assertEqual(raw, before)

    def test_the_timeline_names_the_layout_and_has_no_cam_windows(self):
        timeline = rt.build_timeline(self.validate(broadcast_spec()))
        self.assertEqual(timeline["cams"], [])
        lines = rt.format_timeline(timeline)
        self.assertIn("00:00.750  clip 1 (broadcast)  9.000 s", lines)


class BroadcastGeometryTests(unittest.TestCase):
    def test_picture_is_the_whole_frame_scaled_to_1080_wide_and_even(self):
        w, h = rt.BROADCAST_SIZE
        self.assertEqual(w, 1080)
        self.assertEqual(h, 608)                   # 607.5 rounded to an even number
        self.assertAlmostEqual(h / w, 9 / 16, delta=0.001)

    def test_it_is_centred_where_the_kit_game_block_is(self):
        kit_centre = (rt.CAM_GEOMETRY["kit"].game_y + rt.CAM_GEOMETRY["kit"].game_height / 2)
        x, y = rt.BROADCAST_AT
        self.assertEqual((x, y), (0, 706))
        self.assertAlmostEqual(y + rt.BROADCAST_SIZE[1] / 2, kit_centre, delta=1.5)
        self.assertEqual(y % 2, 0)

    def test_it_keeps_clear_of_the_tiktok_bottom_zone(self):
        self.assertLessEqual(rt.BROADCAST_AT[1] + rt.BROADCAST_SIZE[1], rt.HEIGHT - 370)

    def test_the_background_has_the_aspect_of_the_frame_and_is_small(self):
        w, h = rt.BROADCAST_BG_SIZE
        self.assertEqual(w * rt.HEIGHT, h * rt.WIDTH)
        self.assertLessEqual(w * h, rt.WIDTH * rt.HEIGHT // 16)

    def test_broadcast_is_not_one_of_the_stacked_layouts(self):
        self.assertNotIn("broadcast", rt.LAYOUTS)
        self.assertNotIn("broadcast", rt.CAM_LAYOUTS)
        self.assertNotIn("broadcast", rt.PAD_Y)


class BroadcastCommandTests(SfxDirCase):
    def clip(self, **changes):
        return self.validate(broadcast_spec(**changes))["clips"][0]

    def command(self, clip, layout="bottom-cam", clip_comp=False):
        return rt.clip_command(clip, "/tmp/t/caption.txt", "/o.mov", layout, clip_comp)

    def test_filter_graph_exact_at_the_default_zoom(self):
        got = filter_of(self.command(self.clip()))
        self.assertTrue(got.startswith(broadcast_filter()), got)

    def test_zoom_changes_only_the_crop(self):
        for zoom, text in ((1.0, "1"), (1.6, "1.6"), (1.3, "1.3")):
            got = filter_of(self.command(self.clip(zoom=zoom)))
            self.assertTrue(got.startswith(broadcast_filter(text)), (zoom, got))

    def test_the_crop_keeps_16_by_9_and_is_centred(self):
        got = filter_of(self.command(self.clip(zoom=1.25)))
        self.assertIn("crop=trunc(iw/1.25/2)*2:trunc(ih/1.25/2)*2,", got)
        self.assertNotRegex(got, r"crop=trunc\(iw/1\.25/2\)\*2:trunc\(ih/1\.25/2\)\*2:")

    def test_the_background_is_downscaled_before_the_blur_and_covers_the_frame(self):
        got = filter_of(self.command(self.clip()))
        scale = got.index("[k]scale=270:480:force_original_aspect_ratio=increase")
        self.assertLess(scale, got.index("gblur"))
        self.assertLess(got.index("gblur"), got.index("scale=1080:1920"))
        self.assertIn("eq=brightness=-0.25", got)
        self.assertIn("overlay=0:706,", got)

    def test_the_frame_rate_is_normalised_from_time_zero(self):
        self.assertIn("[0:v]fps=60:start_time=0,split=2", filter_of(self.command(self.clip())))

    def test_caption_is_drawn_as_on_other_layouts(self):
        got = filter_of(self.command(self.clip()))
        self.assertIn(TEXT_FILTER + ",format=yuv420p[v]", got)

    def test_audio_chain_is_the_one_other_layouts_get(self):
        audio = ";[0:a]aresample=48000,aformat=channel_layouts=stereo,"
        for comp in (False, True):
            broadcast = filter_of(self.command(self.clip(), clip_comp=comp))
            other = filter_of(rt.clip_command(self.clip(), "/tmp/t/caption.txt", "/o.mov",
                                              "kit", comp))
            self.assertEqual(broadcast[broadcast.index(audio):], other[other.index(audio):])
        self.assertIn("acompressor=", broadcast)

    def test_other_arguments_are_those_of_every_clip(self):
        clip = self.clip()
        drop = lambda cmd: [a for a in cmd if a != filter_of(cmd)]  # noqa: E731
        self.assertEqual(drop(self.command(clip)),
                         drop(rt.clip_command(clip, "/tmp/t/caption.txt", "/o.mov", "kit")))

    def test_a_top_level_broadcast_layout_reaches_a_clip_without_its_own(self):
        parsed = self.validate(broadcast_spec(clip_layout=False))
        got = filter_of(rt.clip_command(parsed["clips"][0], "/tmp/t/caption.txt", "/o.mov",
                                        parsed["layout"]))
        self.assertTrue(got.startswith(broadcast_filter()))

    def test_scene_commands_mix_broadcast_with_other_layouts(self):
        parsed = self.validate(broadcast_spec())
        timeline = rt.build_timeline(parsed)
        with tempfile.TemporaryDirectory() as tmp:
            cmds = [c for _, c in rt._scene_commands(parsed, timeline, tmp) if "-ss" in c]
        self.assertIn("overlay=0:706", filter_of(cmds[0]))
        self.assertIn("pad=1080:1920:0:432:black", filter_of(cmds[1]))

    def test_the_picture_builder_refuses_cam_windows_on_broadcast(self):
        with self.assertRaises(rt.ToolError):
            rt._picture_graph("broadcast", [{"start": 0, "end": 1, "mode": "full", "x": 162}])


class BroadcastSourceCheckTests(SfxDirCase):
    def check(self, raw, width, height):
        probe = lambda path: {"duration": 6000.0, "width": width, "height": height}  # noqa: E731
        return rt.check_sources(self.validate(raw), probe, exists=lambda p: True,
                                wav_duration=lambda p: 1.0)

    def raw(self, source="/v/broadcast.mp4"):
        raw = broadcast_spec(source=source)
        raw["clips"][1]["source"] = source
        raw["clips"][1]["layout"] = "broadcast"
        return raw

    def test_16_by_9_sources_from_720p_up_are_accepted(self):
        for size in ((1280, 720), (1914, 1080), (1918, 1080), (1920, 1080), (1920, 1090),
                     (2560, 1440), (3840, 2160)):
            self.check(self.raw(), *size)

    def test_smaller_or_other_aspect_sources_are_refused(self):
        for size in ((960, 540), (1024, 576), (640, 360), (1440, 1080), (1920, 800),
                     (3440, 1440), (1080, 1920), (1920, 1168), (1920, 1100)):
            with self.assertRaises(rt.SpecError) as ctx:
                self.check(self.raw(), *size)
            self.assertIn("broadcast clip needs a 16:9 video of at least 1280x720",
                          str(ctx.exception))

    def test_other_layouts_keep_the_exact_1920_by_1080_rule(self):
        for layout in rt.LAYOUTS:
            raw = broadcast_spec(layout=layout, clip_layout=False)
            for size in ((1914, 1080), (1280, 720), (2560, 1440)):
                with self.assertRaises(rt.SpecError, msg=(layout, size)) as ctx:
                    self.check(raw, *size)
                self.assertIn("the crops need 1920x1080", str(ctx.exception))
            self.check(raw, 1920, 1080)

    def test_stream_clips_still_need_exactly_1920_by_1080(self):
        raw = broadcast_spec()                  # clip 2 is a bottom-cam clip of /v/s.mp4
        probe = lambda p: ({"duration": 6000.0, "width": 3840, "height": 2160}  # noqa: E731
                           if "broadcast" in p else {"duration": 6000.0, "width": 2560,
                                                "height": 1440})
        with self.assertRaises(rt.SpecError) as ctx:
            rt.check_sources(self.validate(raw), probe, exists=lambda p: True,
                             wav_duration=lambda p: 1.0)
        self.assertIn("1920x1080", str(ctx.exception))
        self.assertIn("/v/s.mp4", str(ctx.exception))

    def test_the_clip_must_still_fit_inside_the_source(self):
        probe = lambda p: {"duration": 12.0, "width": 1920, "height": 1080}  # noqa: E731
        with self.assertRaises(rt.SpecError) as ctx:
            rt.check_sources(self.validate(broadcast_spec()), probe, exists=lambda p: True,
                             wav_duration=lambda p: 1.0)
        self.assertIn("clip 1 ends at", str(ctx.exception))


# ---------------------------------------------------------------- real ffmpeg

FFMPEG_OK = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
SOFTWARE_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-g", "60",
                   "-pix_fmt", "yuv420p", "-r", "60"]


def _run(cmd):
    import subprocess
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, check=True).stdout


def make_source(path, size="1920x1080", rate=30, seconds=3.0):
    """A synthetic 16:9 broadcast: testsrc2 picture, a sine for the casters."""
    _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-threads", "2", "-f", "lavfi",
          "-i", f"testsrc2=s={size}:r={rate}:d={seconds}", "-f", "lavfi", "-i",
          f"sine=f=440:d={seconds}:sample_rate=48000", "-c:v", "libx264", "-preset",
          "ultrafast", "-pix_fmt", "yuv420p", "-g", "10", "-c:a", "aac", "-shortest", path])


def probe_json(path, entries):
    out = _run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", entries, "-of",
                "json", path])
    return json.loads(out)


def rgb_at(path, t, crop):
    raw = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-ss", str(t), "-i", path,
                "-frames:v", "1", "-vf", f"crop={crop},scale=1:1:flags=area", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"])
    return tuple(raw)


@unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
class BroadcastFrameGridTests(unittest.TestCase):
    """The scene of a broadcast clip has exactly length * 60 frames, whatever the source rate
    and size, and starting at a time that is not a frame of the source."""

    def frames_of(self, size, rate, start):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mp4")
            make_source(src, size, rate, 3.0)
            cap = os.path.join(tmp, "caption.txt")
            with open(cap, "w", encoding="utf-8") as handle:
                handle.write("hi")
            clip = {"source": src, "start_s": start, "length": 1.5, "layout": "broadcast",
                    "zoom": 1.25}
            out = os.path.join(tmp, "o.mov")
            with mock.patch.object(rt, "ENCODE", SOFTWARE_ENCODE):
                cmd = rt.clip_command(clip, cap, out)
            _run(cmd)
            data = probe_json(out, "stream=nb_read_frames,width,height")["streams"][0]
        return int(data["nb_read_frames"]), (data["width"], data["height"])

    def test_30_fps_1080p_from_an_off_grid_start(self):
        self.assertEqual(self.frames_of("1920x1080", 30, 0.517), (90, (1080, 1920)))

    def test_50_fps_1440p(self):
        self.assertEqual(self.frames_of("2560x1440", 50, 0.31), (90, (1080, 1920)))

    def test_30_fps_1914_wide_broadcast_is_1080_by_1920(self):
        self.assertEqual(self.frames_of("1914x1080", 30, 0.4), (90, (1080, 1920)))

    def test_30_fps_720p_broadcast_is_upscaled_to_1080_by_1920(self):
        self.assertEqual(self.frames_of("1280x720", 30, 0.4), (90, (1080, 1920)))

    def test_59_94_fps_4k_starting_at_zero(self):
        self.assertEqual(self.frames_of("3840x2160", 59.94, 0), (90, (1080, 1920)))


@unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
class BroadcastEndToEndTests(SfxDirCase):
    """validate, check, timeline and render of a 2.4 s broadcast clip, software encoder."""

    def test_full_pipeline_makes_a_1080x1920_video_with_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "broadcast.mp4")
            make_source(src, "1920x1080", 30, 3.0)
            raw = {"caption": "Defense.", "output": os.path.join(tmp, "out", "o.mp4"),
                   "card_seconds": 0.4, "sfx_dir": rt.SFX_DIR, "clip_comp": True,
                   "layout": "bottom-cam",
                   "clips": [{"source": src, "start": 0.25, "end": 2.65, "layout": "broadcast",
                              "zoom": 1.2, "sfx": [{"at": 1.0, "name": "impact"}]}]}
            parsed = rt.validate_spec(raw)
            durations = rt.check_sources(parsed)
            timeline = rt.build_timeline(parsed, durations)
            with mock.patch.object(rt, "ENCODE", SOFTWARE_ENCODE):
                out, real, norm_type = rt.render(parsed, durations=durations)
            self.assertAlmostEqual(real, timeline["total"], delta=0.1)
            info = probe_json(out, "stream=codec_type,width,height,nb_read_frames,"
                                   "sample_aspect_ratio")["streams"]
            video = [s for s in info if s["codec_type"] == "video"][0]
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertEqual(video["sample_aspect_ratio"], "1:1")
            self.assertEqual(int(video["nb_read_frames"]), round(timeline["total"] * 60))
            self.assertEqual([s["codec_type"] for s in info if s["codec_type"] == "audio"],
                             ["audio"])
            top = rgb_at(out, 1.5, "1080:300:0:60")      # the blurred background above the picture
            self.assertGreater(sum(top), 3 * 12, top)     # no black bar
            self.assertLess(max(top), 200)                 # but darkened
            if os.environ.get("BROADCAST_FRAME_DIR"):
                _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-ss", "1.5", "-i",
                      out, "-frames:v", "1",
                      os.path.join(os.environ["BROADCAST_FRAME_DIR"], "broadcast.png")])


if __name__ == "__main__":
    unittest.main()
