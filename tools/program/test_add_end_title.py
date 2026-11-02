"""Unit tests for add_end_title (python3 -I -m unittest -v).

No video is encoded and no Chrome starts: path checks, frame math, hit detection on synthetic
audio and the ffmpeg argument lists are checked as plain values.
"""
import math
import os
import random
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import add_end_title as et  # noqa: E402
from chrome_frames import ToolError  # noqa: E402

INFO = et.VideoInfo(frames=1127, fps=60.0, duration=18.794)


def synthetic_audio(hit, total=1.0, rate=48000, bed=0.02, burst=0.5):
    """Quiet noise bed with a loud decaying burst starting at `hit` seconds."""
    rng = random.Random(7)
    out = []
    for i in range(int(total * rate)):
        t = i / rate
        value = rng.uniform(-bed, bed)
        if t >= hit:
            value += burst * math.exp(-(t - hit) * 12) * math.sin(2 * math.pi * 90 * t)
        out.append(value)
    return out


class PathTests(unittest.TestCase):
    def test_same_path_refused(self):
        with self.assertRaises(ToolError):
            et.check_paths("/v/intro.mp4", "/v/intro.mp4")

    def test_same_path_spelled_differently_refused(self):
        with self.assertRaises(ToolError):
            et.check_paths("/v/intro.mp4", "/v/sub/../intro.mp4")

    def test_symlink_to_input_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, link = os.path.join(tmp, "a.mp4"), os.path.join(tmp, "b.mp4")
            open(src, "w").close()
            os.symlink(src, link)
            with self.assertRaises(ToolError):
                et.check_paths(src, link)

    def test_output_must_be_mp4(self):
        with self.assertRaises(ToolError):
            et.check_paths("/v/intro.mp4", "/v/new.mkv")

    def test_different_paths_pass(self):
        et.check_paths("/v/intro.mp4", "/v/new/intro.mp4")


class PlanTests(unittest.TestCase):
    def test_first_frame_is_at_or_after_the_hit(self):
        plan = et.plan_overlay(14.058, 60, 1127)
        self.assertEqual(plan.first_frame, 844)
        self.assertEqual(plan.count, 1127 - 844)
        self.assertGreaterEqual(plan.offset, 0)
        self.assertLess(plan.offset, 1 / 60)
        self.assertAlmostEqual(plan.first_frame / 60 - plan.offset, 14.058)

    def test_hit_on_a_frame_boundary_has_no_offset(self):
        plan = et.plan_overlay(14.0, 60, 1127)
        self.assertEqual(plan.first_frame, 840)
        self.assertAlmostEqual(plan.offset, 0.0, places=9)

    def test_hit_too_late_refused(self):
        with self.assertRaises(ToolError) as ctx:
            et.plan_overlay(18.0, 60, 1127)
        self.assertIn("too little video", str(ctx.exception))

    def test_bad_hit_refused(self):
        for bad in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ToolError):
                et.plan_overlay(bad, 60, 1127)

    def test_active_range_ends_after_the_motion(self):
        plan = et.plan_overlay(14.058, 60, 1127)
        (lo, hi), = et.active_ranges(plan, 60)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, et.MOTION_END - plan.offset + 1 / 60)
        self.assertLess(hi, plan.count / 60)


class HitDetectionTests(unittest.TestCase):
    def test_onset_found_near_the_burst(self):
        found = et.find_onset(synthetic_audio(0.4), 48000, 13.0)
        self.assertAlmostEqual(found, 13.4, delta=0.006)

    def test_steady_noise_has_no_hit(self):
        with self.assertRaises(ToolError):
            et.find_onset(synthetic_audio(5.0, total=1.0), 48000)

    def test_silence_has_no_hit(self):
        with self.assertRaises(ToolError):
            et.find_onset([0.0] * 48000, 48000)

    def test_parse_hit(self):
        self.assertIsNone(et.parse_hit("auto"))
        self.assertAlmostEqual(et.parse_hit("14.058"), 14.058)
        with self.assertRaises(ToolError):
            et.parse_hit("soon")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.plan = et.plan_overlay(14.058, 60, 1127)
        self.cmd = et.encode_command("/in.mp4", "/tmp/f", self.plan, INFO, "/out.mp4.part.mp4")
        self.graph = self.cmd[self.cmd.index("-filter_complex") + 1]

    def arg(self, flag):
        return self.cmd[self.cmd.index(flag) + 1]

    def test_audio_is_copied_not_reencoded(self):
        self.assertEqual(self.arg("-c:a"), "copy")
        self.assertEqual(self.cmd[self.cmd.index("0:a") - 1], "-map")

    def test_input_is_only_read(self):
        self.assertEqual(self.cmd[self.cmd.index("-i") + 1], "/in.mp4")
        self.assertEqual(self.cmd[-1], "/out.mp4.part.mp4")
        self.assertEqual(self.cmd.count("/in.mp4"), 1)

    def test_overlay_converted_with_bt709_limited(self):
        self.assertIn("out_color_matrix=bt709", self.graph)
        self.assertIn("out_range=tv", self.graph)
        self.assertIn("format=yuva420p", self.graph)

    def test_overlay_starts_on_the_first_title_frame(self):
        self.assertIn(f"setpts=PTS-STARTPTS+{844 / 60:.6f}/TB", self.graph)
        self.assertIn("eof_action=repeat", self.graph)

    def test_output_tagged_bt709_tv(self):
        for flag, value in (("-colorspace", "bt709"), ("-color_primaries", "bt709"),
                            ("-color_trc", "bt709"), ("-color_range", "tv")):
            self.assertEqual(self.arg(flag), value)
        self.assertIn("range=tv", self.graph)

    def test_encoder_settings(self):
        self.assertEqual(self.arg("-c:v"), "h264_nvenc")
        self.assertEqual(self.arg("-profile:v"), "high")
        self.assertEqual(self.arg("-pix_fmt"), "yuv420p")
        self.assertEqual(self.arg("-cq"), "19")
        self.assertEqual(self.arg("-r"), "60")
        self.assertEqual(self.arg("-fps_mode"), "cfr")
        self.assertEqual(self.arg("-movflags"), "+faststart")
        self.assertIn("+cgop", self.cmd)

    def test_no_frame_limit_that_would_cut_the_audio_tail(self):
        # The audio runs a little past the last video frame; -frames:v would drop its last packets.
        self.assertNotIn("-frames:v", self.cmd)
        self.assertNotIn("-shortest", self.cmd)

    def test_pcm_command_reads_only_the_window(self):
        cmd = et.pcm_command("/in.mp4", 13.8, 14.3)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "13.800")
        self.assertEqual(cmd[cmd.index("-t") + 1], "0.500")
        self.assertEqual(cmd[-1], "-")


class MainTests(unittest.TestCase):
    def test_same_path_exits_nonzero_before_any_work(self):
        self.assertEqual(et.main(["/v/intro.mp4", "/v/intro.mp4", "--hit", "14.05"]), 1)

    def test_page_exists_and_is_templated(self):
        with open(et.PAGE, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("{{offset}}", text)
        for placeholder in ("{{raw:wordmark}}", "{{raw:event_line}}", "{{sponsor_logo}}", "{{raw:presented_attr}}"):
            self.assertIn(placeholder, text)

    def test_wordmark_puts_last_word_in_amber(self):
        self.assertEqual(et.wordmark_html("YOUR EVENT 26"), 'YOUR EVENT <span class="amber">26</span>')
        self.assertEqual(et.wordmark_html("SOLO"), "SOLO")
        self.assertEqual(et.wordmark_html("A & B 1"), 'A &amp; B <span class="amber">1</span>')

    def test_event_line_splits_on_bars(self):
        self.assertEqual(et.event_line_html("YOUR LEAGUE | ROCKET LEAGUE"),
                         '<span>YOUR LEAGUE</span><i class="sep"></i><span>ROCKET LEAGUE</span>')

    def test_presented_block_hidden_without_sponsor(self):
        plan = et.plan_overlay(14.0, 60, 1127)
        base = {"event_name": "YOUR EVENT 26", "event_sub": "A | B"}
        none = et.title_values(plan, {**base, "sponsor_logo": None})
        self.assertEqual(none["presented_attr"], "hidden")
        self.assertEqual(none["sponsor_logo"], "")
        some = et.title_values(plan, {**base, "sponsor_logo": "/tmp/logo.svg"})
        self.assertEqual(some["presented_attr"], "")
        self.assertEqual(some["sponsor_logo"], "file:///tmp/logo.svg")


if __name__ == "__main__":
    unittest.main()
