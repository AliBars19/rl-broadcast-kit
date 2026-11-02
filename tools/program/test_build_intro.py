"""Unit tests for build_intro, intro_spec and intro_sfx (python3 -I -m unittest -v).

Nothing here encodes video or starts Chrome: validation, the frame and length math, the
SFX plan and every ffmpeg argument list are checked as plain values.
"""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_intro as bi  # noqa: E402
import intro_sfx as sfx  # noqa: E402
import intro_spec as sp  # noqa: E402


def shot(start="00:10:00", end="00:10:02", **extra):
    return {"source": "/v/stream.mp4", "start": start, "end": end, **extra}


def base_spec():
    return {
        "output": "/out/intro.mp4",
        "title": "YOUR EVENT 26",
        "subtitle": "YOUR LEAGUE",
        "matchday": 2,
        "opponent": "ORANGE TEAM",
        "sponsor_logo": "/logos/sponsor.png",
        "shots": [
            shot(end="00:10:02.6", graphic="presents"),                  # 2.6 s
            shot("00:11:00", "00:11:02", speed=1),                       # 2.0 s
            shot("00:12:00", "00:12:01.5", speed=0.5, graphic="wordmark"),  # 3.0 s
            shot("00:13:00", "00:13:02"),                                # 2.0 s
            shot("00:14:00", "00:14:02.6", graphic="sponsor"),           # 2.6 s
            shot("00:15:00", "00:15:01.6"),                              # 1.6 s
            shot("00:16:00", "00:16:01.5", speed=0.5, graphic="matchup"),  # 3.0 s
            shot("00:17:00", "00:17:03.6", graphic="lockup"),            # 3.6 s
        ],
    }


def spec(**changes):
    out = base_spec()
    out.update(changes)
    return out


def assert_invalid(case, bad, needle):
    with case.assertRaises(sp.SpecError) as ctx:
        sp.validate_spec(bad)
    case.assertIn(needle, str(ctx.exception))


class TimestampTests(unittest.TestCase):
    def test_same_parser_as_render_short(self):
        self.assertAlmostEqual(sp.parse_timestamp("00:29:14.5"), 1754.5)
        self.assertEqual(sp.parse_timestamp("01:06:37"), 3997)
        self.assertEqual(sp.parse_timestamp("35:08"), 2108)
        self.assertEqual(sp.parse_timestamp(12), 12.0)

    def test_garbage_is_a_spec_error(self):
        for bad in ("abc", "00:61:00", "-5", True, None):
            with self.assertRaises(sp.SpecError, msg=repr(bad)):
                sp.parse_timestamp(bad)


class ValidationTests(unittest.TestCase):
    def test_valid_spec_is_normalised(self):
        parsed = sp.validate_spec(base_spec())
        self.assertEqual(parsed["matchday"], "2")
        self.assertEqual(parsed["title"], "YOUR EVENT 26")
        self.assertIsNone(parsed["music"])
        self.assertEqual(len(parsed["shots"]), 8)
        self.assertEqual(parsed["shots"][0]["speed"], 1.0)
        self.assertEqual(parsed["shots"][2]["speed"], 0.5)
        self.assertIsNone(parsed["shots"][1]["graphic"])

    def test_sponsor_logo_is_optional(self):
        raw = base_spec()
        del raw["sponsor_logo"]
        raw["shots"][4]["graphic"] = None
        parsed = sp.validate_spec(raw)
        self.assertIsNone(parsed["sponsor_logo"])

    def test_sponsor_graphic_without_any_logo_is_rejected(self):
        raw = base_spec()
        del raw["sponsor_logo"]
        assert_invalid(self, raw, "sponsor_logo")

    def test_brand_supplies_defaults(self):
        raw = base_spec()
        del raw["sponsor_logo"]
        brand = {"channel": "TEST CHANNEL", "home_team": "HOME SIDE", "sponsor_logo": "/brand/logo.svg"}
        parsed = sp.validate_spec(raw, brand=brand)
        self.assertEqual(parsed["channel"], "TEST CHANNEL")
        self.assertEqual(parsed["home_team"], "HOME SIDE")
        self.assertEqual(parsed["sponsor_logo"], "/brand/logo.svg")

    def test_spec_fields_beat_the_brand(self):
        brand = {"channel": "TEST CHANNEL", "home_team": "HOME SIDE", "sponsor_logo": None}
        parsed = sp.validate_spec(spec(channel="MY CHANNEL", home_team="MY TEAM"), brand=brand)
        self.assertEqual((parsed["channel"], parsed["home_team"]), ("MY CHANNEL", "MY TEAM"))

    def test_input_not_mutated(self):
        before = copy.deepcopy(base_spec())
        raw = base_spec()
        sp.validate_spec(raw)
        self.assertEqual(raw, before)

    def test_missing_required_fields(self):
        for key in ("title", "subtitle", "matchday", "opponent", "shots"):
            bad = base_spec()
            del bad[key]
            assert_invalid(self, bad, key)

    def test_unknown_fields_rejected(self):
        assert_invalid(self, spec(colour="red"), "colour")
        bad = base_spec()
        bad["shots"][0]["zoom"] = 2
        assert_invalid(self, bad, "zoom")

    def test_output_must_be_mp4(self):
        assert_invalid(self, spec(output="/out/intro.mov"), ".mp4")

    def test_output_defaults_into_the_program_folder(self):
        bad = base_spec()
        del bad["output"]
        parsed = sp.validate_spec(bad)
        self.assertTrue(parsed["output"].endswith("/Videos/rl-broadcast-kit/intro.mp4"))

    def test_bare_output_name_lands_in_the_program_folder(self):
        parsed = sp.validate_spec(spec(output="trial.mp4"))
        self.assertTrue(parsed["output"].endswith("/Videos/rl-broadcast-kit/trial.mp4"))

    def test_relative_paths_resolve_against_the_base_dir(self):
        raw = spec(sponsor_logo="logo.png")
        raw["shots"] = [dict(s, source="clips/a.mp4") for s in raw["shots"]]
        parsed = sp.validate_spec(raw, base_dir="/work/specs")
        self.assertEqual(parsed["sponsor_logo"], "/work/specs/logo.png")
        self.assertEqual(parsed["shots"][0]["source"], "/work/specs/clips/a.mp4")

    def test_tilde_is_expanded(self):
        parsed = sp.validate_spec(spec(sponsor_logo="~/logo.png"))
        self.assertFalse(parsed["sponsor_logo"].startswith("~"))

    def test_dashes_rejected_in_every_text_field(self):
        for key in ("title", "subtitle", "opponent"):
            assert_invalid(self, spec(**{key: "A \u2014 B"}), "dash")
            assert_invalid(self, spec(**{key: "A \u2013 B"}), "dash")
        assert_invalid(self, spec(matchday="2 \u2013 3"), "dash")

    def test_text_limits(self):
        assert_invalid(self, spec(title="X" * 23), "title")
        assert_invalid(self, spec(subtitle="X" * 29), "subtitle")
        assert_invalid(self, spec(opponent="X" * 29), "opponent")
        assert_invalid(self, spec(opponent="  "), "opponent")

    def test_matchday_accepts_small_whole_numbers_or_short_text(self):
        self.assertEqual(sp.validate_spec(spec(matchday="3"))["matchday"], "3")
        self.assertEqual(sp.validate_spec(spec(matchday="FINALS"))["matchday"], "FINALS")
        assert_invalid(self, spec(matchday=0), "matchday")
        assert_invalid(self, spec(matchday=True), "matchday")
        assert_invalid(self, spec(matchday="X" * 9), "matchday")

    def test_music_is_optional_and_a_path(self):
        parsed = sp.validate_spec(spec(music="~/Music/track.mp3"))
        self.assertTrue(parsed["music"].endswith("/Music/track.mp3"))
        assert_invalid(self, spec(music=5), "music")

    def test_shot_count_bounds(self):
        assert_invalid(self, spec(shots=[]), "shots")
        assert_invalid(self, spec(shots=[shot()] * 13), "shots")

    def test_shot_needs_start_and_end(self):
        bad = base_spec()
        del bad["shots"][1]["end"]
        assert_invalid(self, bad, "shot 2")

    def test_end_must_follow_start(self):
        bad = base_spec()
        bad["shots"][1]["end"] = "00:10:30"
        assert_invalid(self, bad, "shot 2")

    def test_speed_range(self):
        for good in (0.25, 1, 2):
            raw = base_spec()
            raw["shots"][1]["speed"] = good
            raw["shots"][1]["end"] = "00:11:01"
            sp.validate_spec(raw)
        for bad_speed in (0.2, 2.5, "fast", True, 0):
            raw = base_spec()
            raw["shots"][1]["speed"] = bad_speed
            assert_invalid(self, raw, "speed")

    def test_graphic_id_validated(self):
        bad = base_spec()
        bad["shots"][1]["graphic"] = "explosion"
        assert_invalid(self, bad, "graphic")

    def test_graphic_none_is_allowed(self):
        raw = base_spec()
        raw["shots"][0]["graphic"] = None
        sp.validate_spec(raw)

    def test_graphic_must_fit_in_the_shot(self):
        bad = base_spec()
        bad["shots"][0]["end"] = "00:10:01"
        assert_invalid(self, bad, "presents")

    def test_each_graphic_at_most_once(self):
        bad = base_spec()
        bad["shots"][1]["graphic"] = "presents"
        bad["shots"][1]["end"] = "00:11:03"
        assert_invalid(self, bad, "once")

    def test_lockup_must_be_the_last_shot(self):
        bad = base_spec()
        bad["shots"][6]["graphic"] = "lockup"
        bad["shots"][6]["end"] = "00:16:04"
        bad["shots"][7]["graphic"] = None
        assert_invalid(self, bad, "last")


class LengthBudgetTests(unittest.TestCase):
    def test_frames_snap_after_speed(self):
        self.assertEqual(sp.shot_frames(2.0, 1.0), 120)
        self.assertEqual(sp.shot_frames(1.5, 0.5), 180)
        self.assertEqual(sp.shot_frames(2.0, 2.0), 60)
        self.assertEqual(sp.shot_frames(1.0, 0.25), 240)

    def test_timeline_starts_accumulate(self):
        line = sp.timeline(sp.validate_spec(base_spec()))
        self.assertEqual(line[0]["start_frame"], 0)
        self.assertEqual(line[0]["frames"], 156)
        self.assertEqual(line[1]["start_frame"], 156)
        self.assertEqual(line[1]["frames"], 120)

    def test_total_duration(self):
        parsed = sp.validate_spec(base_spec())
        self.assertAlmostEqual(sp.total_duration(parsed), 20.4)

    def test_budget_bounds(self):
        sp.check_budget(15.0)
        sp.check_budget(30.0)
        with self.assertRaises(sp.SpecError) as ctx:
            sp.check_budget(14.9)
        self.assertIn("15", str(ctx.exception))
        with self.assertRaises(sp.SpecError) as ctx:
            sp.check_budget(30.1)
        self.assertIn("30", str(ctx.exception))

    def test_validate_applies_the_budget(self):
        raw = base_spec()
        raw["shots"] = [shot(end="00:10:02"), shot("00:11:00", "00:11:02"),
                        shot("00:12:00", "00:12:02", graphic="lockup")]
        raw["shots"][2]["end"] = "00:12:04"
        assert_invalid(self, raw, "15")

    def test_graphic_duration_table_is_consistent(self):
        for gid, info in sp.GRAPHICS.items():
            self.assertEqual(info["page"], f"{gid}.html")
            self.assertGreater(info["duration"], 0)
            for lo, hi in info["active"]:
                self.assertLessEqual(lo, hi)
                self.assertLessEqual(hi, info["duration"])


class SfxPlanTests(unittest.TestCase):
    def setUp(self):
        self.parsed = sp.validate_spec(base_spec())

    def test_whoosh_on_every_cut_after_the_first(self):
        events = sp.sfx_events(self.parsed)
        whooshes = [e for e in events if e[1] == "whoosh"]
        cuts = [s["start_frame"] / 60 for s in sp.timeline(self.parsed)[1:]]
        self.assertGreaterEqual(len(whooshes), len(cuts))
        for cut in cuts:
            self.assertTrue(any(abs(t - cut) <= sp.WHOOSH_LEAD + 1e-9 for t, _ in whooshes))

    def test_boom_lands_on_the_wordmark_slam(self):
        line = sp.timeline(self.parsed)
        slam = line[2]["start_frame"] / 60 + sp.GRAPHICS["wordmark"]["sfx"][0][0]
        booms = [t for t, kind in sp.sfx_events(self.parsed) if kind == "boom"]
        self.assertTrue(any(abs(t - slam) < 1e-9 for t in booms))

    def test_riser_ends_at_the_final_lockup(self):
        line = sp.timeline(self.parsed)
        lock = line[-1]["start_frame"] / 60
        risers = [t for t, kind in sp.sfx_events(self.parsed) if kind == "riser"]
        self.assertEqual(len(risers), 1)
        self.assertAlmostEqual(risers[0] + sfx.RISER_SECONDS, lock)

    def test_events_are_sorted_and_non_negative(self):
        events = sp.sfx_events(self.parsed)
        self.assertEqual(events, sorted(events))
        self.assertTrue(all(t >= 0 for t, _ in events))


class SfxCommandTests(unittest.TestCase):
    def test_every_kind_has_a_chain(self):
        for kind in ("whoosh", "boom", "hit", "riser"):
            chain = sfx.chain_for(kind, 1.25)
            self.assertIn("adelay=1250|1250", chain)

    def test_unknown_kind_is_an_error(self):
        with self.assertRaises(sfx.ToolError):
            sfx.chain_for("laser", 0)

    def test_command_mixes_all_events_without_normalising(self):
        cmd = sfx.sfx_command([(0.5, "whoosh"), (2.0, "boom")], 6.0, "/tmp/sfx.wav")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2:normalize=0", graph)
        self.assertIn("anoisesrc", graph)
        self.assertEqual(cmd[-1], "/tmp/sfx.wav")
        self.assertEqual(cmd[cmd.index("-t") + 1], "6.000")

    def test_no_events_gives_silence(self):
        cmd = sfx.sfx_command([], 4.0, "/tmp/sfx.wav")
        self.assertIn("anullsrc", " ".join(cmd))

    def test_negative_time_is_clamped(self):
        self.assertIn("adelay=0|0", sfx.chain_for("whoosh", -0.2))


class AtempoTests(unittest.TestCase):
    def test_chain(self):
        self.assertEqual(bi.atempo_chain(1.0), "")
        self.assertEqual(bi.atempo_chain(2.0), "atempo=2")
        self.assertEqual(bi.atempo_chain(0.5), "atempo=0.5")
        self.assertEqual(bi.atempo_chain(0.75), "atempo=0.75")
        self.assertEqual(bi.atempo_chain(0.25), "atempo=0.5,atempo=0.5")


class SegmentCommandTests(unittest.TestCase):
    def setUp(self):
        self.parsed = sp.validate_spec(base_spec())

    def seg(self, index, overlay="/tmp/ov"):
        return bi.segment_command(self.parsed, index, overlay, "/tmp/seg.mov")

    def test_seek_before_input_and_exact_frame_count(self):
        cmd = self.seg(1, None)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-ss") + 1], "660.000")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "120")

    def test_speed_uses_setpts(self):
        graph = self.seg(2).__getitem__(self.seg(2).index("-filter_complex") + 1)
        self.assertIn("setpts=(PTS-STARTPTS)/0.5", graph)
        self.assertIn("atempo=0.5", graph)

    def test_normal_speed_has_no_atempo(self):
        cmd = self.seg(1, None)
        self.assertNotIn("atempo", cmd[cmd.index("-filter_complex") + 1])

    def test_scales_to_1080p60(self):
        graph = self.seg(1, None)[self.seg(1, None).index("-filter_complex") + 1]
        self.assertIn("fps=60", graph)
        self.assertIn("crop=1920:1080", graph)

    def test_overlay_sequence_is_an_input_only_with_a_graphic(self):
        with_graphic = self.seg(0)
        self.assertIn("/tmp/ov/%05d.png", with_graphic)
        self.assertNotIn("/tmp/ov/%05d.png", self.seg(1, None))

    def test_first_shot_has_no_flash_later_shots_do(self):
        first = self.seg(0)[self.seg(0).index("-filter_complex") + 1]
        later = self.seg(1, None)[self.seg(1, None).index("-filter_complex") + 1]
        self.assertNotIn("alpha=1", first)
        self.assertIn("fade=t=out:st=0:d=", later)
        self.assertIn("alpha=1", later)

    def test_only_the_last_shot_fades_to_black(self):
        last = self.seg(7)
        graph = last[last.index("-filter_complex") + 1]
        self.assertIn("fade=t=out:st=3.100:d=0.500:color=black", graph)
        self.assertNotIn("color=black", self.seg(1, None)[self.seg(1, None).index("-filter_complex") + 1])

    def test_encode_settings(self):
        cmd = self.seg(1, None)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "h264_nvenc")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "high")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-r") + 1], "60")
        self.assertEqual(cmd[-1], "/tmp/seg.mov")

    def test_flash_alternates_white_and_blue(self):
        self.assertNotEqual(bi.flash_colour(1), bi.flash_colour(2))


class FinalCommandTests(unittest.TestCase):
    def setUp(self):
        self.parsed = sp.validate_spec(base_spec())

    def test_copies_video_and_encodes_aac(self):
        cmd = bi.final_command(self.parsed, "/tmp/list.txt", "/tmp/sfx.wav", "/o.mp4")
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "192k")
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd[-1], "/o.mp4")

    def test_without_music_mixes_game_and_sfx_only(self):
        cmd = bi.final_command(self.parsed, "/tmp/list.txt", "/tmp/sfx.wav", "/o.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2:normalize=0", graph)
        self.assertNotIn("/m.mp3", cmd)

    def test_with_music_adds_a_third_input_and_ducks_the_game(self):
        raw = base_spec()
        raw["music"] = "/m.mp3"
        parsed = sp.validate_spec(raw)
        cmd = bi.final_command(parsed, "/tmp/list.txt", "/tmp/sfx.wav", "/o.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("/m.mp3", cmd)
        self.assertIn("amix=inputs=3:normalize=0", graph)
        self.assertIn(f"volume={bi.GAME_VOLUME_WITH_MUSIC}", graph)

    def test_audio_fades_out_with_the_picture(self):
        cmd = bi.final_command(self.parsed, "/tmp/list.txt", "/tmp/sfx.wav", "/o.mp4")
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("afade=t=out:st=19.900:d=0.500", graph)

    def test_concat_list_escapes_quotes(self):
        self.assertEqual(bi.concat_list(["/a b/c.mov", "/it's/d.mov"]),
                         "file '/a b/c.mov'\nfile '/it'\\''s/d.mov'\n")


class GraphicPageTests(unittest.TestCase):
    def setUp(self):
        raw = base_spec()
        raw["opponent"] = "A<b>&\"C\""
        self.parsed = sp.validate_spec(raw)

    def test_text_is_html_escaped(self):
        html = bi.graphic_page(self.parsed, "matchup")
        self.assertIn("A&lt;b&gt;&amp;&quot;C&quot;", html)
        self.assertNotIn("A<b>", html)

    def test_every_graphic_page_fills_all_placeholders(self):
        for gid in sp.GRAPHIC_IDS:
            html = bi.graphic_page(self.parsed, gid)
            self.assertNotIn("{{", html, gid)

    def test_matchday_and_title_reach_the_lockup(self):
        html = bi.graphic_page(self.parsed, "lockup")
        self.assertIn("MATCHDAY 2", html)
        self.assertIn("YOUR EVENT 26", html)

    def test_channel_reaches_presents_and_lockup(self):
        raw = base_spec()
        raw["channel"] = "TEST CHANNEL"
        parsed = sp.validate_spec(raw)
        self.assertIn("TEST CHANNEL", bi.graphic_page(parsed, "presents"))
        self.assertIn("TEST CHANNEL", bi.graphic_page(parsed, "lockup"))

    def test_sponsor_logo_is_a_file_uri(self):
        html = bi.graphic_page(self.parsed, "sponsor")
        self.assertIn("file:///logos/sponsor.png", html)


class ChromeUrlTests(unittest.TestCase):
    def test_all_graphics_are_one_second_resolution_safe(self):
        for gid, info in sp.GRAPHICS.items():
            self.assertTrue(os.path.isfile(os.path.join(bi.INTRO_DIR, info["page"])), gid)


if __name__ == "__main__":
    unittest.main()
