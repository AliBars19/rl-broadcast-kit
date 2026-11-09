"""Tests for the `top` layout of render_tiktok: the 16:9 game picture at full width with its top
edge at `top_y` (default 300), the caption above it, and the facecam block below it ("below" cam
windows, footage recorded with the kit or bottom-cam overlay only).

Most checks run on argument lists and validated specs. TopRenderTests encodes a few 2.4 s clips
of synthetic lavfi sources with the software encoder (no GPU needed) and samples pixels.
"""
import copy
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

import render_tiktok as rt  # noqa: E402

FFMPEG_OK = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
SOFTWARE_ENCODE = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-g", "60",
                   "-pix_fmt", "yuv420p", "-r", "60"]
THREADS2 = ["-threads", "2"]


class SfxDirCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.sfx_dir = self._dir.name
        for name in ("card_hit", "impact"):
            open(os.path.join(self.sfx_dir, name + ".wav"), "wb").close()

    def validate(self, raw):
        return rt.validate_spec(raw, sfx_dir=self.sfx_dir)

    def assertInvalid(self, bad, needle):
        with self.assertRaises(rt.SpecError) as ctx:
            self.validate(bad)
        self.assertIn(needle, str(ctx.exception))


def top_spec(**over):
    """A valid top-layout spec: a broadcast clip then a kit clip with a cam block below."""
    raw = {"caption": "Opener.", "output": "/o/t.mp4", "layout": "top",
           "clips": [{"source": "/v/broadcast.mp4", "start": 10, "end": 16.5},
                     {"source": "/v/stream.mp4", "start": 660, "end": 676.75,
                      "source_kind": "kit",
                      "cam": [{"start": 12.25, "end": 16.75, "mode": "below"}]}]}
    raw.update(over)
    return raw


def one_clip_spec(clip, **over):
    return top_spec(clips=[clip], **over)


class TopValidationTests(SfxDirCase):
    def test_top_is_a_layout_and_the_defaults_are_top_y_300_broadcast_and_zoom_1(self):
        spec = self.validate(top_spec())
        self.assertEqual(spec["layout"], "top")
        self.assertEqual(spec["top_y"], 300)
        first, second = spec["clips"]
        self.assertEqual((first["source_kind"], first["zoom"]), ("broadcast", 1.0))
        self.assertEqual((second["source_kind"], second["zoom"]), ("kit", 1.0))

    def test_old_layouts_keep_their_clip_dicts_and_never_get_source_kind(self):
        raw = {"caption": "A.", "output": "/o/a.mp4", "layout": "kit",
               "clips": [{"source": "/v/s.mp4", "start": 0, "end": 5}]}
        clip = self.validate(raw)["clips"][0]
        self.assertNotIn("source_kind", clip)
        self.assertNotIn("zoom", clip)

    def test_the_hook_and_a_per_clip_layout_can_be_top(self):
        raw = top_spec(layout="kit")
        raw["hook"] = {"source": "/v/h.mp4", "start": 1, "end": 6, "layout": "top"}
        raw["clips"][0]["layout"] = "top"
        raw["clips"][1]["layout"] = "top"
        spec = self.validate(raw)
        self.assertEqual(spec["hook"]["source_kind"], "broadcast")
        self.assertEqual(spec["layout"], "kit")

    def test_unknown_source_kind_is_refused(self):
        raw = top_spec()
        raw["clips"][0]["source_kind"] = "other"
        self.assertInvalid(raw, "source_kind")
        raw["clips"][0]["source_kind"] = 3
        self.assertInvalid(raw, "source_kind")

    def test_source_kind_is_only_for_layout_top(self):
        raw = {"caption": "A.", "output": "/o/a.mp4", "layout": "kit",
               "clips": [{"source": "/v/s.mp4", "start": 0, "end": 5, "source_kind": "kit"}]}
        self.assertInvalid(raw, "only for layout top")

    def test_zoom_must_be_one_or_more(self):
        for bad in (0.99, 0, -1, 1.61, "1.2", True, float("nan")):
            raw = top_spec()
            raw["clips"][0]["zoom"] = bad
            with self.subTest(bad=bad):
                self.assertInvalid(raw, "zoom")
        for good in (1, 1.0, 1.25, 1.6):
            raw = top_spec()
            raw["clips"][0]["zoom"] = good
            with self.subTest(good=good):
                self.assertEqual(self.validate(raw)["clips"][0]["zoom"], float(good))

    def test_zoom_error_names_the_clip(self):
        raw = top_spec()
        raw["clips"][1]["zoom"] = 0.5
        self.assertInvalid(raw, "clip 2 zoom")

    def test_the_broadcast_layout_default_zoom_is_untouched(self):
        raw = {"caption": "A.", "output": "/o/a.mp4", "layout": "broadcast",
               "clips": [{"source": "/v/s.mp4", "start": 0, "end": 5}]}
        self.assertEqual(self.validate(raw)["clips"][0]["zoom"], 1.25)

    def test_top_y_bounds_are_inclusive_and_documented_in_constants(self):
        low, high = rt.TOP_Y_RANGE
        self.assertEqual((low, high), (280, 400))
        for good in (low, 300, 340, high):
            with self.subTest(good=good):
                self.assertEqual(self.validate(top_spec(top_y=good))["top_y"], good)

    def test_top_y_that_pushes_the_picture_into_a_ui_zone_is_refused(self):
        for bad in (278, 200, 0, -300, 402, 500, 942):
            with self.subTest(bad=bad):
                self.assertInvalid(top_spec(top_y=bad), "top_y")

    def test_top_y_must_be_a_whole_even_number(self):
        for bad in (301, 300.5, "300", True, None, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                self.assertInvalid(top_spec(top_y=bad), "top_y")

    def test_below_windows_need_a_stream_source(self):
        raw = top_spec()
        raw["clips"][0]["cam"] = [{"start": 1, "end": 3, "mode": "below"}]
        self.assertInvalid(raw, "broadcast")
        raw["clips"][0]["source_kind"] = "broadcast"
        self.assertInvalid(raw, "facecam")

    def test_below_windows_are_fine_for_kit_and_bottom_cam(self):
        for kind in ("kit", "bottom-cam"):
            raw = top_spec()
            raw["clips"][1]["source_kind"] = kind
            with self.subTest(kind=kind):
                cam = self.validate(raw)["clips"][1]["cam"]
                self.assertEqual([(w["mode"], w["start"], w["end"]) for w in cam],
                                 [("below", 12.25, 16.75)])

    def test_cover_defaults_to_true_and_is_only_kept_when_false(self):
        self.assertNotIn("cover", self.validate(top_spec())["clips"][1])
        raw = top_spec()
        raw["clips"][1]["cover"] = True
        self.assertNotIn("cover", self.validate(raw)["clips"][1])
        raw["clips"][1]["cover"] = False
        self.assertIs(self.validate(raw)["clips"][1]["cover"], False)

    def test_cover_must_be_a_boolean(self):
        for bad in (0, 1, "false", "no", None, 0.0):
            raw = top_spec()
            raw["clips"][1]["cover"] = bad
            with self.subTest(bad=bad):
                self.assertInvalid(raw, "cover must be true or false")

    def test_cover_is_only_for_layout_top(self):
        raw = {"caption": "A.", "output": "/o/a.mp4", "layout": "kit",
               "clips": [{"source": "/v/s.mp4", "start": 0, "end": 5, "cover": False}]}
        self.assertInvalid(raw, "cover is only for layout top")
        raw = top_spec()
        raw["clips"][1] = {"source": "/v/s.mp4", "start": 0, "end": 5, "layout": "kit",
                           "cover": True}
        self.assertInvalid(raw, "cover is only for layout top")

    def test_cover_is_refused_on_a_broadcast_source(self):
        for value in (True, False):
            raw = top_spec()
            raw["clips"][0]["cover"] = value
            with self.subTest(value=value):
                self.assertInvalid(raw, "not for a broadcast source")

    def test_touching_below_windows_merge_into_one(self):
        raw = top_spec()
        raw["clips"][1]["cam"] = [{"start": 1, "end": 2, "mode": "below"},
                                  {"start": 2, "end": 3.5, "mode": "below"}]
        cam = self.validate(raw)["clips"][1]["cam"]
        self.assertEqual([(w["mode"], w["start"], w["end"]) for w in cam],
                         [("below", 1, 3.5)])

    def test_touching_below_windows_must_share_x_to_merge(self):
        raw = top_spec()
        raw["clips"][1]["cam"] = [{"start": 1, "end": 2, "mode": "below", "x": 40},
                                  {"start": 2, "end": 3.5, "mode": "below", "x": 44}]
        self.assertInvalid(raw, "share x")

    def test_a_gap_between_below_windows_keeps_them_apart(self):
        raw = top_spec()
        raw["clips"][1]["cam"] = [{"start": 1, "end": 2, "mode": "below"},
                                  {"start": 2.1, "end": 3.5, "mode": "below"}]
        self.assertEqual(len(self.validate(raw)["clips"][1]["cam"]), 2)

    def test_below_is_only_for_layout_top(self):
        raw = {"caption": "A.", "output": "/o/a.mp4", "layout": "kit",
               "clips": [{"source": "/v/s.mp4", "start": 0, "end": 5,
                          "cam": [{"start": 1, "end": 3, "mode": "below"}]}]}
        self.assertInvalid(raw, "mode must be one of full, inset")

    def test_a_top_clip_never_has_its_game_replaced_by_the_cam(self):
        for mode in ("full", "inset"):
            raw = top_spec()
            raw["clips"][1]["cam"][0]["mode"] = mode
            with self.subTest(mode=mode):
                self.assertInvalid(raw, "below")

    def test_existing_window_rules_apply_to_below_windows(self):
        raw = top_spec()
        raw["clips"][1]["cam"] = [{"start": 1, "end": 1.2, "mode": "below"}]
        self.assertInvalid(raw, "at least 0.5 s")
        raw["clips"][1]["cam"] = [{"start": 1, "end": 99, "mode": "below"}]
        self.assertInvalid(raw, "past the")
        raw["clips"][1]["cam"] = [{"start": 1, "end": 3, "mode": "below"},
                                  {"start": 2, "end": 5, "mode": "below"}]
        self.assertInvalid(raw, "overlaps")
        raw["clips"][1]["cam"] = [{"start": 1, "end": 3, "mode": "below", "extra": 1}]
        self.assertInvalid(raw, "unknown field")

    def test_below_x_is_even_whole_and_inside_the_facecam_picture(self):
        for kind, (low, high) in (("kit", (36, 44)), ("bottom-cam", (36, 42))):
            raw = top_spec()
            raw["clips"][1]["source_kind"] = kind
            for x in (low, high):
                raw["clips"][1]["cam"][0]["x"] = x
                with self.subTest(kind=kind, x=x):
                    self.assertEqual(self.validate(raw)["clips"][1]["cam"][0]["x"], x)
            for bad in (low - 2, high + 2, low + 1, "40"):
                raw["clips"][1]["cam"][0]["x"] = bad
                with self.subTest(kind=kind, bad=bad):
                    self.assertInvalid(raw, " x")

    def test_below_x_defaults_to_the_centre_of_the_picture(self):
        spec = self.validate(top_spec())
        self.assertEqual(spec["clips"][1]["cam"][0]["x"], 40)

    def test_validation_does_not_change_its_input(self):
        raw = top_spec()
        before = copy.deepcopy(raw)
        self.validate(raw)
        self.assertEqual(raw, before)

    def test_old_specs_still_get_the_old_validation(self):
        raw = {"caption": "A.", "output": "/o/a.mp4", "layout": "kit",
               "clips": [{"source": "/v/s.mp4", "start": 0, "end": 5,
                          "cam": [{"start": 1, "end": 3, "mode": "full", "x": 150}]}]}
        self.assertEqual(self.validate(raw)["clips"][0]["cam"][0]["mode"], "full")
        raw["clips"][0]["zoom"] = 1.1
        self.assertInvalid(raw, "zoom is only for layout")


class TopGeometryTests(unittest.TestCase):
    def test_game_block_is_full_width_16_by_9(self):
        self.assertEqual(rt.TOP_GAME_SIZE, (1080, 608))

    def test_game_block_stays_clear_of_the_bottom_zone_for_every_top_y(self):
        for top_y in range(rt.TOP_Y_RANGE[0], rt.TOP_Y_RANGE[1] + 1, 2):
            self.assertLessEqual(top_y + rt.TOP_GAME_SIZE[1], rt.HEIGHT - rt.BOTTOM_UI_ZONE)

    def test_cam_block_sits_40_px_below_the_game_and_clear_of_the_bottom_zone(self):
        for top_y in range(rt.TOP_Y_RANGE[0], rt.TOP_Y_RANGE[1] + 1, 2):
            x, y, w, h = rt.below_block(top_y)
            self.assertEqual((x, y, w), (0, top_y + 608 + 40, 1080))
            self.assertLessEqual(y + h, rt.HEIGHT - rt.BOTTOM_UI_ZONE)
            self.assertEqual(h % 2, 0)
            self.assertLessEqual(h, 608)
            self.assertGreaterEqual(h, 450, top_y)    # still a usable picture of a face

    def test_default_cam_block_is_602_px_high(self):
        self.assertEqual(rt.below_block(300), (0, 948, 1080, 602))

    def test_the_caption_clears_the_tiktok_top_zone_at_the_lowest_top_y(self):
        self.assertGreaterEqual(rt.TOP_Y_RANGE[0] - rt.TOP_CAPTION_GAP - rt.FONT_SIZE, 150)

    def test_cam_crops_lie_inside_the_facecam_picture_and_are_even(self):
        for kind, (px, py, pw, ph) in (("kit", (34, 713, 571, 319)),
                                       ("bottom-cam", (36, 788, 440, 242))):
            for top_y in (280, 300, 400):
                block_h = rt.below_block(top_y)[3]
                cw, ch, x, y = rt.below_crop(kind, block_h)
                with self.subTest(kind=kind, top_y=top_y):
                    self.assertEqual((cw % 2, ch % 2, x % 2, y % 2), (0, 0, 0, 0))
                    self.assertGreaterEqual(x, px)
                    self.assertLessEqual(x + cw, px + pw)
                    self.assertGreaterEqual(y, py)
                    self.assertLessEqual(y + ch, py + ph)
                    self.assertAlmostEqual(cw / ch, 1080 / block_h, delta=0.02)

    def test_the_source_kinds_are_the_kits_own_overlay_geometries(self):
        self.assertEqual(rt.SOURCE_KINDS, ("broadcast", "kit", "bottom-cam"))
        self.assertEqual(set(rt.TOP_SOURCES), {"kit", "bottom-cam"})

    def test_the_cover_patch_spans_the_facecam_box_with_its_brackets_and_is_even(self):
        for kind, (bx, by, bw, bh) in (("kit", (30, 708, 580, 327)),
                                       ("bottom-cam", (36, 788, 440, 242))):
            cw, ch, cx, cy = rt.TOP_SOURCES[kind].cover
            with self.subTest(kind=kind):
                self.assertEqual((cw % 2, ch % 2, cx % 2, cy % 2), (0, 0, 0, 0))
                self.assertLessEqual((cx, cy), (bx, by))
                self.assertGreaterEqual(cx + cw, bx + bw)
                self.assertGreaterEqual(cy + ch, by + bh)
                self.assertGreaterEqual(rt.TOP_SOURCES[kind].donor_x, cx + cw)


class TopTimelineTests(SfxDirCase):
    def timeline(self, raw):
        return rt.build_timeline(self.validate(raw))

    def test_no_hook_and_opening_card_starts_on_the_card_with_its_sound_at_zero(self):
        raw = top_spec(opening_card=True)
        timeline = self.timeline(raw)
        first = timeline["segments"][0]
        self.assertEqual((first["kind"], first["start"]), ("card", 0.0))
        self.assertEqual(timeline["segments"][1]["clip"], 0)
        card_events = [e for e in timeline["events"] if e["kind"] == "card"]
        self.assertEqual(card_events[0]["t"], 0.0)
        self.assertEqual(card_events[0]["name"], "card_hit")
        self.assertEqual(timeline["events"][0], card_events[0])      # nothing sounds before it

    def test_opening_card_is_the_default(self):
        self.assertEqual(self.timeline(top_spec())["segments"][0]["kind"], "card")

    def test_a_single_runs_card_broadcast_clip_card_own_clip(self):
        kinds = [(s["kind"], s.get("clip")) for s in self.timeline(top_spec())["segments"]]
        self.assertEqual(kinds, [("card", None), ("clip", 0), ("card", None), ("clip", 1)])

    def test_cam_windows_reach_the_timeline_with_absolute_times(self):
        timeline = self.timeline(top_spec())
        # card 0.75 + clip 6.5 + card 0.75 = 8.0 s, then the window starts 12.25 s into the clip
        self.assertEqual([(c["mode"], round(c["t"], 3), c["length"]) for c in timeline["cams"]],
                         [("below", 20.25, 4.5)])
        lines = rt.format_timeline(timeline)
        self.assertTrue(any("cam below" in line for line in lines), lines)

    def test_segments_of_a_top_spec_carry_no_layout_note(self):
        for seg in self.timeline(top_spec())["segments"]:
            self.assertNotIn("layout", seg)


class TopSourceCheckTests(SfxDirCase):
    def check(self, clip, width, height):
        parsed = self.validate(one_clip_spec(clip))
        info = {"duration": 1000.0, "width": width, "height": height}
        return rt.check_sources(parsed, probe=lambda p: info, exists=lambda p: True,
                                wav_duration=lambda p: 1.0)

    def clip(self, kind="broadcast"):
        return {"source": "/v/s.mp4", "start": 10, "end": 16, "source_kind": kind}

    def test_broadcast_sources_may_be_any_16_by_9_from_720p(self):
        for size in ((1920, 1080), (1914, 1080), (1280, 720), (3840, 2160)):
            with self.subTest(size=size):
                self.check(self.clip(), *size)

    def test_broadcast_sources_that_are_small_or_not_16_by_9_are_refused(self):
        for size in ((1000, 563), (1920, 1200), (1000, 1000)):
            with self.subTest(size=size):
                with self.assertRaises(rt.SpecError):
                    self.check(self.clip(), *size)

    def test_stream_sources_need_exactly_1920_by_1080(self):
        for kind in ("kit", "bottom-cam"):
            self.check(self.clip(kind), 1920, 1080)
            for size in ((1280, 720), (1914, 1080), (3840, 2160)):
                with self.subTest(kind=kind, size=size):
                    with self.assertRaises(rt.SpecError):
                        self.check(self.clip(kind), *size)


class TopCommandTests(SfxDirCase):
    def graph(self, clip, **over):
        parsed = self.validate(one_clip_spec(clip, **over))
        cmd = rt.clip_command(parsed["clips"][0], "/t/c.txt", "/t/s.mov", parsed["layout"],
                              top_y=parsed["top_y"])
        return cmd[cmd.index("-filter_complex") + 1]

    def video(self, clip, **over):
        return self.graph(clip, **over).split(";[0:a]")[0]

    def broadcast_clip(self, **extra):
        return {"source": "/v/r.mp4", "start": 551, "end": 557.5, **extra}

    def kit_clip(self, cam=(), **extra):
        clip = {"source": "/v/s.mp4", "start": 660, "end": 676.75, "source_kind": "kit", **extra}
        if cam:
            clip["cam"] = list(cam)
        return clip

    def test_broadcast_picture_is_the_whole_frame_at_1080_by_608_at_top_y(self):
        video = self.video(self.broadcast_clip())
        self.assertIn("scale=1080:608:flags=lanczos[fg]", video)
        self.assertIn("[bg][fg]overlay=0:300", video)
        self.assertNotIn("hflip", video)                   # no facecam to cover
        self.assertNotIn("split=3", video)

    def test_top_y_moves_the_picture_the_caption_and_the_cam_block(self):
        video = self.video(self.kit_clip([{"start": 1, "end": 3, "mode": "below"}]), top_y=340)
        self.assertIn("[bg][fg]overlay=0:340", video)
        self.assertIn(f"y={340 - 40}-ascent", video)
        self.assertIn(f"overlay=0:{340 + 608 + 40}:", video)

    def test_zoom_crops_anchored_at_the_top_centre(self):
        video = self.video(self.broadcast_clip(zoom=1.25))
        self.assertIn("crop=trunc(iw/1.25/2)*2:trunc(ih/1.25/2)*2:trunc((iw-ow)/4)*2:0", video)

    def test_zoom_one_is_the_whole_frame(self):
        self.assertIn("crop=trunc(iw/1/2)*2:trunc(ih/1/2)*2:trunc((iw-ow)/4)*2:0",
                      self.video(self.broadcast_clip()))

    def test_background_is_the_blurred_darkened_cover_of_the_picture(self):
        video = self.video(self.broadcast_clip())
        self.assertIn(f"gblur=sigma={rt.BROADCAST_BG_BLUR}", video)
        self.assertIn("eq=brightness=-0.25:saturation=0.8", video)
        self.assertIn("force_original_aspect_ratio=increase", video)
        self.assertIn(f"scale={rt.WIDTH}:{rt.HEIGHT}:flags=bicubic[bg]", video)

    def test_frame_grid_and_square_pixels_like_broadcast(self):
        video = self.video(self.broadcast_clip())
        self.assertIn(f"fps={rt.FPS}:start_time=0", video)
        self.assertIn(",setsar=1,", video)
        self.assertTrue(video.endswith("format=yuv420p[v]"))

    def test_caption_sits_above_the_game_with_its_baseline_40_px_above_top_y(self):
        video = self.video(self.broadcast_clip())
        self.assertIn("y=260-ascent", video)
        self.assertNotIn(f":y={rt.CAPTION_Y}", video)
        self.assertIn(":x=(w-text_w)/2", video)
        self.assertNotIn("enable=", video.split("drawtext")[1])

    def test_kit_covers_the_leftover_facecam_with_a_mirrored_blurred_patch(self):
        video = self.video(self.kit_clip())
        self.assertIn("split=2[b][p]", video)
        self.assertIn("hflip,boxblur", video)
        self.assertIn("[b][patch]overlay=", video)
        self.assertNotIn("crop=17", video)                      # kit keeps the full frame

    def test_bottom_cam_takes_the_game_region_without_the_spectator_bar(self):
        video = self.video(self.kit_clip(source_kind="bottom-cam"))
        self.assertIn("crop=1714:964:102:0", video)
        self.assertIn("hflip,boxblur", video)

    def test_cam_windows_overlay_a_block_below_the_game_only_in_their_window(self):
        video = self.video(self.kit_clip([{"start": 12.25, "end": 16.75, "mode": "below"}]))
        self.assertIn("split=3[b][p][w0]", video)           # the cam is cut from the source itself
        self.assertIn("[w0]crop=560:312:40:718,scale=1080:602:flags=lanczos", video)
        self.assertIn("overlay=0:948:format=auto:enable='gte(t,12.2417)*lt(t,16.7417)'", video)

    def test_cam_block_fades_in_over_0_15_s(self):
        video = self.video(self.kit_clip([{"start": 12.25, "end": 16.75, "mode": "below"}]))
        start = f"{rt.frame_snap(12.25):.4f}"
        self.assertIn(f"fade=t=in:st={start}:d=0.15:alpha=1", video)

    def test_two_windows_have_their_own_chains_and_enables(self):
        video = self.video(self.kit_clip([{"start": 1, "end": 3, "mode": "below"},
                                          {"start": 5, "end": 7, "mode": "below", "x": 44}]))
        self.assertIn("split=4[b][p][w0][w1]", video)
        self.assertIn("[w1]crop=560:312:44:718", video)
        self.assertEqual(video.count("overlay=0:948:"), 2)

    def test_the_cam_window_never_removes_the_game(self):
        video = self.video(self.kit_clip([{"start": 1, "end": 3, "mode": "below"}]))
        # the game overlay is not gated by any enable expression
        self.assertIn("[bg][fg]overlay=0:300[st]", video)
        self.assertNotIn("overlay=0:300:enable", video)

    def test_a_clip_without_windows_has_no_cam_input_at_all(self):
        video = self.video(self.kit_clip())
        self.assertNotIn("[w0]", video)
        self.assertNotIn("fade=", video)

    def test_cover_defaults_to_true_so_the_patch_is_in_the_graph(self):
        for kind in ("kit", "bottom-cam"):
            with self.subTest(kind=kind):
                video = self.video(self.kit_clip(source_kind=kind))
                self.assertIn("hflip,boxblur", video)
                self.assertEqual(video, self.video(self.kit_clip(source_kind=kind, cover=True)))

    def test_cover_false_removes_the_patch_and_keeps_the_game_region(self):
        video = self.video(self.kit_clip(cover=False))
        self.assertNotIn("hflip", video)
        self.assertNotIn("boxblur", video)
        self.assertNotIn("[patch]", video)
        self.assertIn(f"[0:v]fps={rt.FPS}:start_time=0[b];[b]split=2[m][k];", video)
        bottom_cam = self.video(self.kit_clip(source_kind="bottom-cam", cover=False))
        self.assertNotIn("hflip", bottom_cam)
        self.assertIn("crop=1714:964:102:0", bottom_cam)

    def test_cover_false_keeps_the_cam_windows(self):
        video = self.video(self.kit_clip([{"start": 12.25, "end": 16.75, "mode": "below"}],
                                         cover=False))
        self.assertIn("overlay=0:948:format=auto:enable='gte(t,12.2417)*lt(t,16.7417)'", video)

    def test_touching_below_windows_are_one_fade_and_one_overlay(self):
        video = self.video(self.kit_clip([{"start": 1, "end": 2, "mode": "below"},
                                          {"start": 2, "end": 3, "mode": "below"}]))
        self.assertEqual(video.count("fade=t=in"), 1)
        self.assertEqual(video.count("overlay=0:948:format=auto:enable="), 1)

    def test_the_audio_chain_is_the_one_other_layouts_get(self):
        graph = self.graph(self.broadcast_clip())
        self.assertIn("[0:a]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.05",
                      graph)

    def test_the_builder_refuses_below_windows_on_a_broadcast_source(self):
        clip = {"cam": [{"start": 1, "end": 3, "mode": "below", "x": 40}]}
        with self.assertRaises(rt.ToolError):
            rt._picture_graph("top", clip["cam"], source_kind="broadcast")

    def test_card_scenes_keep_the_caption_at_y_640(self):
        cmd = rt.card_command(0.75, "/t/c.txt", "/t/card.mov")
        self.assertIn(f"y={rt.CAPTION_Y}", cmd[cmd.index("-filter_complex") + 1])


# ---------------------------------------------------------------- real ffmpeg

W, H = rt.WIDTH, rt.HEIGHT
CLIP_SECONDS = 2.4
CAM_WINDOW = (0.8, 1.8)
SAMPLE_BEFORE, SAMPLE_INSIDE, SAMPLE_AFTER = 0.4, 1.4, 2.2


def _run(cmd):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, check=True).stdout


def make_fixture(path, boxes, seconds=3.0):
    """1920x1080 30 fps: dark blue, a red scoreline across the top 100 rows, `boxes` as drawbox
    arguments (x, y, w, h, colour), and a sine."""
    vf = ",".join(["drawbox=x=0:y=0:w=1920:h=100:color=red:t=fill"] +
                  [f"drawbox=x={x}:y={y}:w={w}:h={h}:color={c}:t=fill"
                   for x, y, w, h, c in boxes])
    _run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin"] + THREADS2 +
         ["-f", "lavfi", "-i", f"color=c=0x1020a0:s=1920x1080:r=30:d={seconds}",
          "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:sample_rate=48000",
          "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
          "-g", "10", "-c:a", "aac", "-shortest", path])


def make_broadcast(path):
    make_fixture(path, [])


def make_kit(path):
    """The kit facecam box (x 30..610, y 708..1035) in lime."""
    make_fixture(path, [(30, 708, 580, 327, "0x00ff00")])


def make_bottom_cam(path):
    """The bottom-cam facecam picture (x 36..476, y 788..1030) in lime."""
    make_fixture(path, [(36, 788, 440, 242, "0x00ff00")])


def render_scene(tmp, spec_raw, name="scene.mov"):
    """Validate a one-scene spec and encode its first clip with the software encoder."""
    parsed = rt.validate_spec(spec_raw)
    caption = rt.write_caption_file(tmp, parsed["caption"])
    out = os.path.join(tmp, name)
    clip = parsed["clips"][0]
    with mock.patch.object(rt, "ENCODE", SOFTWARE_ENCODE + THREADS2):
        cmd = rt.clip_command(clip, caption, out, parsed["layout"], top_y=parsed["top_y"])
    _run(cmd)
    return out, parsed


class Frame:
    """One decoded 1080x1920 frame, rgb24."""

    def __init__(self, path, t):
        raw = _run(["ffmpeg", "-loglevel", "error", "-nostdin", "-ss", str(t), "-i", path,
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        assert len(raw) == W * H * 3, len(raw)
        self.raw = raw

    def at(self, x, y):
        i = (y * W + x) * 3
        return tuple(self.raw[i:i + 3])

    def white_rows(self, y0, y1):
        """Rows of y0..y1 that hold a (near) white pixel: the caption text."""
        return [y for y in range(y0, y1)
                if any(min(self.at(x, y)) > 235 for x in range(0, W, 3))]


def is_red(p):
    return p[0] > 180 and p[1] < 70 and p[2] < 70


def is_lime(p):
    return p[1] > 170 and p[0] < 90 and p[2] < 90


def probe_size(path):
    out = json.loads(_run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                           "-show_entries", "stream=width,height,nb_read_frames", "-of", "json",
                           path]))["streams"][0]
    return out["width"], out["height"], int(out["nb_read_frames"])


def raw_spec(source, kind, cam=True, top_y=300, **clip_extra):
    clip = {"source": source, "start": 0.2, "end": 0.2 + CLIP_SECONDS, "source_kind": kind,
            **clip_extra}
    if cam:
        clip["cam"] = [{"start": CAM_WINDOW[0], "end": CAM_WINDOW[1], "mode": "below"}]
    return {"caption": "Hi", "output": "/tmp/unused.mp4", "layout": "top", "top_y": top_y,
            "clips": [clip]}


@unittest.skipUnless(FFMPEG_OK and os.path.exists(rt.FONT_PATH), "ffmpeg and the font are needed")
class TopRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.tmp = cls._dir.name
        cls.broadcast = os.path.join(cls.tmp, "broadcast.mp4")
        cls.kit = os.path.join(cls.tmp, "kit.mp4")
        cls.bottom_cam = os.path.join(cls.tmp, "bottom-cam.mp4")
        make_broadcast(cls.broadcast)
        make_kit(cls.kit)
        make_bottom_cam(cls.bottom_cam)
        cls.kit_scene, _ = render_scene(cls.tmp, raw_spec(cls.kit, "kit"), "kit.mov")

    @classmethod
    def tearDownClass(cls):
        cls._dir.cleanup()

    def test_broadcast_scene_is_1080x1920_with_the_game_block_at_top_y(self):
        out, _ = render_scene(self.tmp, raw_spec(self.broadcast, "broadcast", cam=False))
        self.assertEqual(probe_size(out), (1080, 1920, round(CLIP_SECONDS * 60)))
        frame = Frame(out, 1.0)
        top_y = 300
        self.assertTrue(is_red(frame.at(540, top_y + 4)), frame.at(540, top_y + 4))   # scoreline
        self.assertTrue(is_red(frame.at(5, top_y + 4)))
        self.assertTrue(is_red(frame.at(1074, top_y + 4)))
        above = frame.at(540, top_y - 6)
        self.assertFalse(is_red(above), above)               # the blurred, darkened background
        self.assertGreater(sum(above), 3 * 8)                # not a black bar
        below = frame.at(540, top_y + 608 + 6)
        self.assertFalse(is_red(below))
        self.assertGreater(sum(below), 3 * 8)
        inside = frame.at(540, top_y + 400)
        self.assertEqual(inside[2] > inside[0], True)        # the blue game picture

    def test_the_game_block_follows_top_y(self):
        out, _ = render_scene(self.tmp, raw_spec(self.broadcast, "broadcast", cam=False,
                                                 top_y=360), "t360.mov")
        frame = Frame(out, 1.0)
        self.assertTrue(is_red(frame.at(540, 360 + 4)))
        self.assertFalse(is_red(frame.at(540, 360 - 6)))
        self.assertFalse(is_red(frame.at(540, 300 + 4)))

    def test_zoom_keeps_the_scoreline_because_the_crop_is_anchored_at_the_top(self):
        flat, _ = render_scene(self.tmp, raw_spec(self.broadcast, "broadcast", cam=False),
                               "z1.mov")
        zoomed, _ = render_scene(self.tmp, raw_spec(self.broadcast, "broadcast", cam=False,
                                                    zoom=1.6), "z16.mov")
        # the 100 source rows are 56 output rows at zoom 1 and 90 at zoom 1.6
        self.assertFalse(is_red(Frame(flat, 1.0).at(540, 300 + 70)))
        self.assertTrue(is_red(Frame(zoomed, 1.0).at(540, 300 + 70)))
        self.assertTrue(is_red(Frame(zoomed, 1.0).at(540, 300 + 4)))
        self.assertFalse(is_red(Frame(zoomed, 1.0).at(540, 300 + 100)))

    def test_caption_is_above_the_game_with_its_baseline_about_40_px_above_top_y(self):
        frame = Frame(self.kit_scene, SAMPLE_BEFORE)
        rows = frame.white_rows(0, H)
        self.assertTrue(rows)
        self.assertTrue(all(150 < y < 300 for y in rows), (rows[0], rows[-1]))   # not over the game
        self.assertTrue(255 <= rows[-1] <= 264, rows[-1])    # baseline at 260, "Hi" has no tail
        self.assertEqual(frame.white_rows(rt.CAPTION_Y - 20, rt.CAPTION_Y + 100), [])

    def test_the_cam_block_appears_below_the_game_only_during_the_window(self):
        x, y, w, h = rt.below_block(300)
        probe = (540, y + h // 2)
        edge = (540, y + 4)
        for t, expected in ((SAMPLE_BEFORE, False), (SAMPLE_INSIDE, True), (SAMPLE_AFTER, False)):
            frame = Frame(self.kit_scene, t)
            with self.subTest(t=t):
                self.assertEqual(is_lime(frame.at(*probe)), expected, frame.at(*probe))
                self.assertEqual(is_lime(frame.at(*edge)), expected, frame.at(*edge))
                self.assertEqual(is_lime(frame.at(1070, y + h - 4)), expected)
        gap = Frame(self.kit_scene, SAMPLE_INSIDE).at(540, y - 20)      # between game and cam
        self.assertFalse(is_lime(gap))

    def test_the_game_keeps_playing_and_the_facecam_is_hidden_in_it(self):
        # source (300, 870) is inside the facecam box: the game block must show the pitch
        gx, gy = round(300 * 1080 / 1920), 300 + round(870 * 608 / 1080)
        for t in (SAMPLE_BEFORE, SAMPLE_INSIDE, SAMPLE_AFTER):
            frame = Frame(self.kit_scene, t)
            with self.subTest(t=t):
                self.assertFalse(is_lime(frame.at(gx, gy)), frame.at(gx, gy))
                self.assertTrue(is_red(frame.at(540, 300 + 4)))
        # and the cam block stays inside the bottom zone
        bottom = Frame(self.kit_scene, SAMPLE_INSIDE).at(540, H - rt.BOTTOM_UI_ZONE + 20)
        self.assertFalse(is_lime(bottom))

    def test_cam_block_fades_in_instead_of_cutting(self):
        x, y, w, h = rt.below_block(300)
        early = Frame(self.kit_scene, CAM_WINDOW[0] + 0.03).at(540, y + h // 2)
        late = Frame(self.kit_scene, CAM_WINDOW[0] + 0.3).at(540, y + h // 2)
        self.assertFalse(is_lime(early), early)
        self.assertTrue(is_lime(late), late)

    def test_bottom_cam_scene_shows_the_cam_below_and_hides_it_in_the_game(self):
        out, _ = render_scene(self.tmp, raw_spec(self.bottom_cam, "bottom-cam"), "bc.mov")
        self.assertEqual(probe_size(out)[:2], (1080, 1920))
        x, y, w, h = rt.below_block(300)
        inside, before = Frame(out, SAMPLE_INSIDE), Frame(out, SAMPLE_BEFORE)
        self.assertTrue(is_lime(inside.at(540, y + h // 2)))
        self.assertFalse(is_lime(before.at(540, y + h // 2)))
        # source (200, 900) is inside the facecam picture (the crop starts at x 102)
        gx, gy = round((200 - 102) * 1080 / 1714), 300 + round(900 * 608 / 964)
        self.assertFalse(is_lime(inside.at(gx, gy)), inside.at(gx, gy))
        self.assertTrue(is_red(inside.at(540, 300 + 4)))

    def test_cover_false_shows_the_facecam_corner_inside_the_game_block(self):
        out, _ = render_scene(self.tmp, raw_spec(self.kit, "kit", cover=False), "nocover.mov")
        gx, gy = round(300 * 1080 / 1920), 300 + round(870 * 608 / 1080)
        for t in (SAMPLE_BEFORE, SAMPLE_INSIDE):
            frame = Frame(out, t)
            with self.subTest(t=t):
                self.assertTrue(is_lime(frame.at(gx, gy)), frame.at(gx, gy))
                self.assertTrue(is_red(frame.at(540, 300 + 4)))

    def test_touching_below_windows_do_not_fade_out_at_the_join(self):
        spec = raw_spec(self.kit, "kit", cam=False)
        spec["clips"][0]["cam"] = [{"start": 0.4, "end": 1.3, "mode": "below"},
                                   {"start": 1.3, "end": 2.2, "mode": "below"}]
        out, _ = render_scene(self.tmp, spec, "touch.mov")
        x, y, w, h = rt.below_block(300)
        for t in (1.0, 1.36, 1.8):
            with self.subTest(t=t):
                self.assertTrue(is_lime(Frame(out, t).at(540, y + h // 2)))

    def test_full_render_opens_on_the_card_and_keeps_the_card_caption_where_it_was(self):
        raw = {"caption": "Hi", "output": os.path.join(self.tmp, "out", "full.mp4"),
               "layout": "top", "card_seconds": 0.4, "sfx_dir": rt.SFX_DIR,
               "clips": [{"source": self.broadcast, "start": 0.2, "end": 2.6},
                         {"source": self.kit, "start": 0.2, "end": 2.6, "source_kind": "kit",
                          "cam": [{"start": 0.8, "end": 1.8, "mode": "below"}]}]}
        parsed = rt.validate_spec(raw)
        durations = rt.check_sources(parsed)
        timeline = rt.build_timeline(parsed, durations)
        self.assertEqual(timeline["segments"][0]["kind"], "card")
        self.assertEqual(timeline["events"][0]["t"], 0.0)
        with mock.patch.object(rt, "ENCODE", SOFTWARE_ENCODE + THREADS2):
            out, real, _ = rt.render(parsed, durations=durations)
        self.assertAlmostEqual(real, timeline["total"], delta=0.1)
        width, height, frames = probe_size(out)
        self.assertEqual((width, height), (1080, 1920))
        self.assertEqual(frames, round(timeline["total"] * 60))
        card = Frame(out, 0.1)
        self.assertLess(max(card.at(50, 1000)), 40)               # black card
        self.assertTrue(card.white_rows(rt.CAPTION_Y, rt.CAPTION_Y + 90))
        game = Frame(out, 0.4 + 0.5)
        self.assertTrue(is_red(game.at(540, 300 + 4)))
        cam_t = 0.4 + 2.4 + 0.4 + 1.3
        x, y, w, h = rt.below_block(300)
        self.assertTrue(is_lime(Frame(out, cam_t).at(540, y + h // 2)))


if __name__ == "__main__":
    unittest.main()
