"""Unit tests for the pure parts of render_short (run with python3 -I -m unittest).

Nothing here encodes video or starts Chrome: command builders are checked by
their argument lists only.
"""
import copy
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import render_short as rs  # noqa: E402

BASE_SPEC = {
    "title": "TOP 3 GOALS",
    "subtitle": "YOUR EVENT 26 | MATCHDAY 1",
    "handle": "@yourchannel",
    "source": "/videos/stream-full.mp4",
    "output": "/videos/out/top3.mp4",
    "clips": [
        {"rank": 3, "start": "00:35:08.0", "end": "00:35:22.0", "player": "PLAYER ONE",
         "team": "BLUE TEAM", "caption": "AIR DRIBBLE FINISH",
         "detail": "vs ORANGE TEAM | GAME 3"},
        {"rank": 2, "start": "00:21:09", "end": "00:21:23", "player": "PLAYER ONE",
         "team": "BLUE TEAM", "caption": "FLIP RESET", "detail": "GAME 1"},
        {"rank": 1, "start": "01:26:41", "end": "01:26:55", "player": "PLAYER ONE",
         "team": "BLUE TEAM", "caption": "DOUBLE TAP", "detail": "GAME 5"},
    ],
}


def spec(**changes):
    out = copy.deepcopy(BASE_SPEC)
    out.update(changes)
    return out


def clip_list(n, length=10, ranked=True):
    clips = []
    for i in range(n):
        clip = {"start": f"00:{10 + i:02d}:00", "end": f"00:{10 + i:02d}:{length:02d}",
                "player": "P", "caption": "C"}
        if ranked:
            clip["rank"] = n - i
        clips.append(clip)
    return clips


class TimestampTests(unittest.TestCase):
    def test_hms_with_fraction(self):
        self.assertAlmostEqual(rs.parse_timestamp("00:35:08.5"), 2108.5)

    def test_hms_plain(self):
        self.assertEqual(rs.parse_timestamp("01:26:41"), 5201)

    def test_minutes_seconds(self):
        self.assertEqual(rs.parse_timestamp("35:08"), 2108)

    def test_bare_seconds(self):
        self.assertEqual(rs.parse_timestamp("90.5"), 90.5)

    def test_number_passes_through(self):
        self.assertEqual(rs.parse_timestamp(12), 12.0)

    def test_rejects_garbage(self):
        for bad in ("abc", "1:2:3:4", "", "00:61:00", "-5", "00:00:75", True, None):
            with self.assertRaises(rs.SpecError, msg=repr(bad)):
                rs.parse_timestamp(bad)


class BudgetTests(unittest.TestCase):
    def test_clip_seconds_snaps_to_frames(self):
        self.assertAlmostEqual(rs.frame_snap(14.0), 14.0)
        self.assertAlmostEqual(rs.frame_snap(1.01), 30 / 30)
        self.assertAlmostEqual(rs.frame_snap(1.02), 31 / 30)

    def test_trial_total(self):
        parsed = rs.validate_spec(BASE_SPEC)
        self.assertAlmostEqual(rs.total_duration(parsed), 1.5 + 3 * (1.1 + 14) + 3.0)

    def test_unranked_clips_have_no_rank_card(self):
        parsed = rs.validate_spec(spec(clips=clip_list(2, ranked=False)))
        self.assertAlmostEqual(rs.total_duration(parsed), 1.5 + 20 + 3.0)

    def test_guard_allows_exactly_limit(self):
        rs.check_budget(58.0)

    def test_guard_refuses_over_limit(self):
        with self.assertRaises(rs.SpecError) as ctx:
            rs.check_budget(58.1)
        self.assertIn("58.0", str(ctx.exception))

    def test_long_spec_is_refused_before_rendering(self):
        # 5 clips of 12 s: 1.5 + 5 * 13.1 + 3 = 70 s
        with self.assertRaises(rs.SpecError):
            rs.check_budget(rs.total_duration(rs.validate_spec(spec(clips=clip_list(5, 12)))))

    def test_verify_real_duration(self):
        rs.verify_duration(59.4)
        with self.assertRaises(rs.ToolError):
            rs.verify_duration(59.5)
        with self.assertRaises(rs.ToolError):
            rs.verify_duration(61.0)


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, bad, needle):
        with self.assertRaises(rs.SpecError) as ctx:
            rs.validate_spec(bad)
        self.assertIn(needle, str(ctx.exception))

    def test_valid_spec_normalised(self):
        parsed = rs.validate_spec(BASE_SPEC)
        self.assertEqual(parsed["handle"], "@yourchannel")
        self.assertEqual(len(parsed["clips"]), 3)
        first = parsed["clips"][0]
        self.assertAlmostEqual(first["start_s"], 2108.0)
        self.assertAlmostEqual(first["length"], 14.0)
        self.assertEqual(parsed["end_title"], "FOLLOW @YOURCHANNEL")

    def test_channel_and_handle_default_from_the_brand(self):
        raw = spec()
        del raw["handle"]
        brand = {"channel": "TEST CHANNEL", "handle": "@testchan"}
        parsed = rs.validate_spec(raw, brand)
        self.assertEqual((parsed["channel"], parsed["handle"]), ("TEST CHANNEL", "@testchan"))
        self.assertEqual(parsed["end_title"], "FOLLOW @TESTCHAN")

    def test_spec_channel_beats_the_brand(self):
        parsed = rs.validate_spec(spec(channel="MY CHANNEL"), {"channel": "TEST CHANNEL", "handle": "@x"})
        self.assertEqual(parsed["channel"], "MY CHANNEL")

    def test_channel_reaches_every_graphic_page(self):
        parsed = rs.validate_spec(spec(channel="My <Channel>"))
        for page, values in (("title", rs.title_values(parsed)), ("end", rs.end_values(parsed)),
                             ("clip", rs.clip_values(parsed, 0)),
                             ("clip-react", rs.clip_values(parsed, 0))):
            html = rs.page_html(page, values)
            self.assertIn("MY &lt;CHANNEL&gt;", html, page)
            self.assertNotIn("{{", html, page)

    def test_input_not_mutated(self):
        before = copy.deepcopy(BASE_SPEC)
        rs.validate_spec(BASE_SPEC)
        self.assertEqual(BASE_SPEC, before)

    def test_missing_required_fields(self):
        for key in ("title", "source", "output", "clips"):
            bad = spec()
            del bad[key]
            self.assertInvalid(bad, key)

    def test_clip_count_bounds(self):
        self.assertInvalid(spec(clips=[]), "1 to 5")
        self.assertInvalid(spec(clips=clip_list(6, 5)), "1 to 5")

    def test_end_before_start(self):
        bad = spec()
        bad["clips"][0]["end"] = "00:35:00"
        self.assertInvalid(bad, "clip 1")

    def test_clip_too_short_or_long(self):
        bad = spec()
        bad["clips"][0]["end"] = "00:35:08.5"
        self.assertInvalid(bad, "at least")
        bad = spec()
        bad["clips"][0]["end"] = "00:36:08"
        self.assertInvalid(bad, "at most")

    def test_ranks_all_or_none(self):
        bad = spec()
        del bad["clips"][1]["rank"]
        self.assertInvalid(bad, "rank")

    def test_duplicate_ranks(self):
        bad = spec()
        bad["clips"][1]["rank"] = 3
        self.assertInvalid(bad, "unique")

    def test_rank_must_be_small_int(self):
        bad = spec()
        bad["clips"][0]["rank"] = 12
        self.assertInvalid(bad, "rank")

    def test_dashes_rejected_everywhere(self):
        self.assertInvalid(spec(title="TOP — GOALS"), "dash")
        bad = spec()
        bad["clips"][0]["caption"] = "A – B"
        self.assertInvalid(bad, "dash")

    def test_text_length_limit(self):
        self.assertInvalid(spec(title="X" * 60), "title")

    def test_output_must_be_mp4(self):
        self.assertInvalid(spec(output="/videos/out/top3.mov"), ".mp4")

    def test_handle_must_start_with_at(self):
        self.assertInvalid(spec(handle="yourchannel"), "handle")

    def test_non_string_text(self):
        self.assertInvalid(spec(title=5), "title")

    def test_not_a_dict(self):
        self.assertInvalid([], "object")

    def test_text_fields_only_from_known_keys(self):
        bad = spec()
        bad["clips"][0]["bogus"] = "x"
        self.assertInvalid(bad, "bogus")


class ReactionTests(unittest.TestCase):
    def test_reaction_defaults_to_true(self):
        parsed = rs.validate_spec(BASE_SPEC)
        self.assertTrue(all(c["reaction"] is True for c in parsed["clips"]))

    def test_reaction_can_be_switched_off_per_clip(self):
        raw = spec()
        raw["clips"][1]["reaction"] = False
        parsed = rs.validate_spec(raw)
        self.assertEqual([c["reaction"] for c in parsed["clips"]], [True, False, True])

    def test_reaction_must_be_boolean(self):
        raw = spec()
        raw["clips"][0]["reaction"] = "yes"
        with self.assertRaises(rs.SpecError) as ctx:
            rs.validate_spec(raw)
        self.assertIn("reaction", str(ctx.exception))

    def test_blocks_tile_the_frame_exactly(self):
        # top block, game frame, reaction panel, bottom block fill 1920 px without overlap
        self.assertEqual(rs.REACT_TOP_H + rs.VIDEO_H + rs.CAM_H + rs.REACT_BOTTOM_H, rs.HEIGHT)
        self.assertEqual(rs.GAME_Y, rs.REACT_TOP_H)
        self.assertEqual(rs.CAM_Y, rs.GAME_Y + rs.VIDEO_H)
        self.assertEqual(rs.BOTTOM_Y, rs.CAM_Y + rs.CAM_H)

    def test_cam_crop_keeps_facecam_aspect(self):
        _, _, w, h = rs.CAM_CROP
        self.assertAlmostEqual(w / h, rs.WIDTH / rs.CAM_H, delta=0.01)

    def test_patch_covers_the_crop(self):
        cx, cy, cw, ch = rs.CAM_CROP
        px, py, pw, ph = rs.CAM_PATCH
        self.assertLessEqual(px, cx)
        self.assertLessEqual(py, cy)
        self.assertGreaterEqual(px + pw, cx + cw)
        self.assertGreaterEqual(py + ph, cy + ch)

    def test_values_use_the_reaction_layout_sizes(self):
        parsed = rs.validate_spec(BASE_SPEC)
        self.assertLessEqual(rs.clip_values(parsed, 0)["title_size"], 120)
        classic = rs.validate_spec(spec(clips=[dict(BASE_SPEC["clips"][0], reaction=False)]))
        self.assertGreater(rs.clip_values(classic, 0)["player_size"],
                           rs.clip_values(parsed, 0)["player_size"])


class FitTests(unittest.TestCase):
    def test_short_text_keeps_base_size(self):
        self.assertEqual(rs.fit_size("TOP 3", 130, 960), 130)

    def test_long_text_shrinks_to_fit(self):
        size = rs.fit_size("A VERY LONG GOAL TITLE HERE", 130, 960)
        self.assertLess(size, 130)
        self.assertLessEqual(len(" " * 27) * size * rs.CHAR_WIDTH, 960 + 1)

    def test_never_below_floor(self):
        self.assertEqual(rs.fit_size("X" * 200, 100, 500, floor=40), 40)


class ProgressTests(unittest.TestCase):
    def test_states(self):
        html = rs.progress_html([3, 2, 1], 1)
        self.assertEqual(html.count("done"), 1)
        self.assertEqual(html.count("current"), 1)
        self.assertEqual(html.count("todo"), 1)
        self.assertLess(html.index(">3<"), html.index(">2<"))

    def test_empty_when_unranked(self):
        self.assertEqual(rs.progress_html([None, None], 0), "")


class TemplateTests(unittest.TestCase):
    def test_fill_escapes_html(self):
        out = rs.fill_template("<b>{{name}}</b>", {"name": "A<&>\"B"})
        self.assertEqual(out, "<b>A&lt;&amp;&gt;&quot;B</b>")

    def test_raw_values_not_escaped(self):
        out = rs.fill_template("{{raw:bits}}", {"bits": "<i>x</i>"})
        self.assertEqual(out, "<i>x</i>")

    def test_unknown_placeholder_fails(self):
        with self.assertRaises(rs.ToolError):
            rs.fill_template("{{nope}}", {})

    def test_scene_values_for_clip(self):
        parsed = rs.validate_spec(BASE_SPEC)
        vals = rs.clip_values(parsed, 1)
        self.assertEqual(vals["rank"], "2")
        self.assertEqual(vals["caption"], "FLIP RESET")
        self.assertIn("current", vals["progress"])


class GraphicsRuleTests(unittest.TestCase):
    """Static check of the house design rules on the shipped graphics."""

    def files(self):
        gdir = os.path.join(HERE, "graphics")
        return [os.path.join(gdir, n) for n in sorted(os.listdir(gdir))
                if n.endswith((".html", ".css"))]

    def test_graphics_exist(self):
        names = {os.path.basename(p) for p in self.files()}
        for need in ("short.css", "title.html", "rank.html", "clip.html", "clip-react.html",
                     "react-ranked.part.html", "react-plain.part.html", "end.html"):
            self.assertIn(need, names)

    def test_no_tracking_glow_or_dashes(self):
        banned = ("text-shadow", "box-shadow", "drop-shadow", "radial-gradient",
                  "conic-gradient", "—", "–", "&mdash;", "&ndash;")
        for path in self.files():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for token in banned:
                self.assertNotIn(token, text, f"{token!r} in {path}")
            for value in re.findall(r"letter-spacing\s*:\s*([^;}]+)", text):
                self.assertEqual(value.strip(), "0", f"letter-spacing in {path}")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.parsed = rs.validate_spec(BASE_SPEC)
        self.clip = self.parsed["clips"][0]

    def test_clip_command_seeks_before_input(self):
        cmd = rs.clip_command(self.parsed, self.clip, "/t/ov.png", "/t/s.mov")
        src = cmd.index(self.parsed["source"])
        self.assertEqual(cmd[src - 1], "-i")
        self.assertEqual(cmd[src - 5:src - 1], ["-ss", "2108.000", "-t", "14.000"])
        self.assertEqual(cmd[0], "ffmpeg")

    def test_clip_command_encoder_and_format(self):
        cmd = rs.clip_command(self.parsed, self.clip, "/t/ov.png", "/t/s.mov")
        joined = " ".join(cmd)
        self.assertIn("h264_nvenc", joined)
        self.assertNotIn("libx264", joined)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "8M")
        self.assertEqual(cmd[-1], "/t/s.mov")
        self.assertEqual(cmd[cmd.index("-t", cmd.index("-map")) + 1], "14.000")

    def test_reaction_filter_layout(self):
        cmd = rs.clip_command(self.parsed, self.clip, "/t/ov.png", "/t/s.mov")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("gblur", graph)
        self.assertIn("scale=1080:608", graph)
        self.assertIn("overlay=0:380", graph)      # game frame below the 380 px top block
        self.assertIn("crop=440:244:36:786", graph)  # facecam rectangle of the source frame
        self.assertIn("scale=1080:600", graph)     # reaction panel
        self.assertIn("overlay=0:988", graph)      # directly under the game frame
        self.assertIn("crop=458:262:28:780", graph)  # blurred patch over the corner facecam
        self.assertIn("afade=t=in:st=0:d=0.15", graph)
        self.assertIn("afade=t=out:st=13.850:d=0.15", graph)

    def test_classic_filter_layout(self):
        clip = dict(self.clip, reaction=False)
        cmd = rs.clip_command(self.parsed, clip, "/t/ov.png", "/t/s.mov")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("overlay=0:656", graph)
        self.assertNotIn("crop=440:244", graph)
        self.assertNotIn("crop=458:262", graph)
        self.assertIn("afade=t=out:st=13.850:d=0.15", graph)

    def test_card_command_uses_silence_and_loops_nothing_real(self):
        cmd = rs.card_command("/t/fg.png", "/t/card.mov", 1.5, "up", "silence")
        joined = " ".join(cmd)
        self.assertIn("anullsrc", joined)
        self.assertIn("h264_nvenc", joined)
        self.assertEqual(cmd[cmd.index("-t") + 1], "1.500")

    def test_rank_card_has_whoosh(self):
        cmd = rs.card_command("/t/fg.png", "/t/card.mov", 1.1, "right", "whoosh")
        self.assertIn("anoisesrc", " ".join(cmd))

    def test_concat_command_copies_video(self):
        cmd = rs.concat_command("/t/list.txt", "/out/x.mp4")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "192k")
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "concat")
        self.assertEqual(cmd[-1], "/out/x.mp4")

    def test_concat_list_quotes_paths(self):
        text = rs.concat_list(["/t/a b.mov", "/t/it's.mov"])
        self.assertIn("file '/t/a b.mov'", text)
        self.assertIn("'\\''", text)

    def test_chrome_command(self):
        cmd = rs.chrome_command("/t/page.html", "/t/out.png", "/t/profile")
        self.assertEqual(cmd[0], "google-chrome")
        self.assertIn("--headless=new", cmd)
        self.assertIn("--screenshot=/t/out.png", cmd)
        self.assertIn("--window-size=1080,1920", cmd)
        self.assertIn("--default-background-color=00000000", cmd)
        self.assertIn("--user-data-dir=/t/profile", cmd)
        self.assertEqual(cmd[-1], "file:///t/page.html")


class PlanTests(unittest.TestCase):
    def test_scene_order(self):
        plan = rs.scene_plan(rs.validate_spec(BASE_SPEC))
        kinds = [s["kind"] for s in plan]
        self.assertEqual(kinds, ["title", "rank", "clip", "rank", "clip", "rank", "clip", "end"])
        self.assertAlmostEqual(sum(s["duration"] for s in plan), 49.8)

    def test_unranked_plan(self):
        plan = rs.scene_plan(rs.validate_spec(spec(clips=clip_list(2, ranked=False))))
        self.assertEqual([s["kind"] for s in plan], ["title", "clip", "clip", "end"])


if __name__ == "__main__":
    unittest.main()
