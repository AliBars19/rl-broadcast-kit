"""Unit tests for chrome_frames and render_stinger (python3 -I -m unittest -v).

Nothing here starts Chrome or encodes video: command builders are checked by their
argument lists, the frame math and the alpha check by pure calls.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chrome_frames as cf  # noqa: E402
import render_stinger as rst  # noqa: E402


class FrameMathTests(unittest.TestCase):
    def test_frame_count_rounds_to_whole_frames(self):
        self.assertEqual(cf.frame_count(1.3, 60), 78)
        self.assertEqual(cf.frame_count(2.0, 60), 120)
        self.assertEqual(cf.frame_count(0.0083, 60), 0)

    def test_frame_time_is_index_over_fps(self):
        self.assertAlmostEqual(cf.frame_time(0, 60), 0.0)
        self.assertAlmostEqual(cf.frame_time(36, 60), 0.6)
        self.assertAlmostEqual(cf.frame_time(77, 60), 77 / 60)

    def test_plan_renders_everything_when_always_active(self):
        self.assertEqual(cf.plan_frames(5, 10, [(0, 10)]), [0, 1, 2, 3, 4])

    def test_plan_reuses_last_rendered_frame_while_static(self):
        plan = cf.plan_frames(10, 10, [(0.2, 0.4)])
        self.assertEqual(plan, [0, 0, 2, 3, 4, 4, 4, 4, 4, 4])

    def test_plan_handles_two_active_ranges(self):
        plan = cf.plan_frames(12, 10, [(0.0, 0.2), (0.7, 0.9)])
        self.assertEqual(plan, [0, 1, 2, 2, 2, 2, 2, 7, 8, 9, 9, 9])

    def test_plan_first_frame_is_always_rendered(self):
        self.assertEqual(cf.plan_frames(3, 10, [(0.5, 0.6)])[0], 0)

    def test_plan_empty_active_renders_only_frame_zero(self):
        self.assertEqual(cf.plan_frames(4, 10, []), [0, 0, 0, 0])

    def test_plan_zero_frames(self):
        self.assertEqual(cf.plan_frames(0, 60, [(0, 1)]), [])

    def test_plan_rejects_reversed_range(self):
        with self.assertRaises(cf.ToolError):
            cf.plan_frames(5, 10, [(0.4, 0.2)])


class ChromeCommandTests(unittest.TestCase):
    def test_page_url_carries_time(self):
        url = cf.page_url("/tmp/a b/page.html", 0.6)
        self.assertTrue(url.startswith("file:///tmp/a%20b/page.html?t="))
        self.assertTrue(url.endswith("t=0.6000"))

    def test_command_is_transparent_and_pinned_to_size(self):
        cmd = cf.chrome_command("file:///x.html?t=0.1", "/tmp/o.png", "/tmp/profile")
        self.assertEqual(cmd[0], "google-chrome")
        self.assertIn("--headless=new", cmd)
        self.assertIn("--screenshot=/tmp/o.png", cmd)
        self.assertIn("--window-size=1920,1080", cmd)
        self.assertIn("--default-background-color=00000000", cmd)
        self.assertIn("--user-data-dir=/tmp/profile", cmd)
        self.assertEqual(cmd[-1], "file:///x.html?t=0.1")

    def test_worker_count_is_bounded(self):
        self.assertLessEqual(cf.CHROME_WORKERS, 6)


class SequenceLinkTests(unittest.TestCase):
    def test_static_frames_are_linked_to_their_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "00000.png"), "wb") as handle:
                handle.write(b"png")
            cf.link_static(tmp, [0, 0, 0])
            for n in (1, 2):
                path = os.path.join(tmp, f"{n:05d}.png")
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), b"png")


class StingerConstantsTests(unittest.TestCase):
    def test_total_frames_match_duration(self):
        self.assertEqual(rst.FRAMES, 78)
        self.assertAlmostEqual(rst.DURATION, 1.3)

    def test_transition_point_sits_inside_the_covered_window(self):
        self.assertGreaterEqual(rst.TRANSITION_MS, rst.COVER_START_MS)
        self.assertLessEqual(rst.TRANSITION_MS, rst.COVER_END_MS)

    def test_cover_frame_is_inside_the_covered_window(self):
        t_ms = rst.COVER_FRAME / rst.FPS * 1000
        self.assertGreaterEqual(t_ms, rst.COVER_START_MS)
        self.assertLessEqual(t_ms, rst.COVER_END_MS)


class StingerCommandTests(unittest.TestCase):
    def test_encode_keeps_alpha(self):
        cmd = rst.encode_command("/tmp/f/%05d.png", "/tmp/out.webm")
        self.assertIn("libvpx-vp9", cmd)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuva420p")
        self.assertEqual(cmd[cmd.index("-auto-alt-ref") + 1], "0")
        self.assertEqual(cmd[cmd.index("-framerate") + 1], "60")
        self.assertEqual(cmd[-1], "/tmp/out.webm")

    def test_preview_decodes_with_libvpx_before_input(self):
        cmd = rst.preview_command("/tmp/s.webm", "/tmp/p.mp4")
        codec_at = cmd.index("libvpx-vp9")
        self.assertLess(codec_at, cmd.index("/tmp/s.webm"))
        self.assertEqual(cmd[codec_at - 1], "-c:v")
        self.assertIn("h264_nvenc", cmd)
        self.assertIn("+faststart", cmd)

    def test_pixel_probe_selects_frame_and_crops_one_pixel(self):
        cmd = rst.pixel_command("/tmp/s.webm", 36, 5, 7)
        self.assertLess(cmd.index("libvpx-vp9"), cmd.index("/tmp/s.webm"))
        graph = cmd[cmd.index("-vf") + 1]
        self.assertIn("select=eq(n\\,36)", graph)
        self.assertIn("crop=1:1:5:7", graph)
        self.assertIn("rgba", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_parse_rgba(self):
        self.assertEqual(rst.parse_rgba(b"\x01\x02\x03\x04"), (1, 2, 3, 4))
        with self.assertRaises(rst.ToolError):
            rst.parse_rgba(b"\x01\x02")


class AlphaCheckTests(unittest.TestCase):
    def test_passes_for_clear_corners_and_opaque_cover(self):
        rst.check_alpha((0, 0, 0, 0), (0, 0, 0, 0), (7, 9, 13, 255))

    def test_fails_when_first_frame_is_not_clear(self):
        with self.assertRaises(rst.ToolError) as ctx:
            rst.check_alpha((0, 0, 0, 255), (0, 0, 0, 0), (7, 9, 13, 255))
        self.assertIn("first", str(ctx.exception))

    def test_fails_when_last_frame_is_not_clear(self):
        with self.assertRaises(rst.ToolError) as ctx:
            rst.check_alpha((0, 0, 0, 0), (0, 0, 0, 40), (7, 9, 13, 255))
        self.assertIn("last", str(ctx.exception))

    def test_fails_when_cover_frame_is_see_through(self):
        with self.assertRaises(rst.ToolError) as ctx:
            rst.check_alpha((0, 0, 0, 0), (0, 0, 0, 0), (7, 9, 13, 200))
        self.assertIn("cover", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
