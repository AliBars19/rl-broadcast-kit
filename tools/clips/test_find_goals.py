"""Unit tests for the pure parts of find_goals (run with python3 -I -m unittest)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import find_goals as fg  # noqa: E402


def fp(level, size=16):
    """Synthetic flat fingerprint: every pixel has the same brightness."""
    return bytes([level]) * size


def series(*parts):
    """Build a series from (level, count) pairs."""
    return [fp(level) for level, count in parts for _ in range(count)]


class DistanceTests(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(fg.distance(fp(10), fp(10)), 0)

    def test_mean_absolute_difference(self):
        self.assertEqual(fg.distance(fp(10), fp(40)), 30)

    def test_symmetric(self):
        self.assertEqual(fg.distance(fp(200), fp(5)), fg.distance(fp(5), fp(200)))


class StableRunTests(unittest.TestCase):
    def test_empty_series(self):
        self.assertEqual(fg.stable_runs([], 5, 3), [])

    def test_single_run_covers_everything(self):
        runs = fg.stable_runs(series((50, 10)), 5, 3)
        self.assertEqual([(r.start, r.end) for r in runs], [(0, 10)])

    def test_noise_inside_tolerance_stays_one_run(self):
        data = [fp(50 + (i % 3)) for i in range(10)]
        self.assertEqual(len(fg.stable_runs(data, 5, 3)), 1)

    def test_short_run_is_dropped(self):
        runs = fg.stable_runs(series((50, 6), (200, 2), (120, 6)), 5, 3)
        self.assertEqual([(r.start, r.end) for r in runs], [(0, 6), (8, 14)])

    def test_flicker_between_equal_runs_is_merged(self):
        runs = fg.stable_runs(series((50, 6), (200, 2), (50, 6)), 5, 3)
        self.assertEqual([(r.start, r.end) for r in runs], [(0, 14)])


class ChangeTests(unittest.TestCase):
    def find(self, data, min_len=4, accept=None):
        return fg.find_changes(data, 5, min_len, accept)

    def test_constant_series_has_no_change(self):
        self.assertEqual(self.find(series((50, 20))), [])

    def test_persistent_change_is_found_at_first_new_sample(self):
        changes = self.find(series((50, 10), (200, 10)))
        self.assertEqual([c.index for c in changes], [10])
        self.assertEqual(changes[0].distance, 150)

    def test_change_that_does_not_persist_is_rejected(self):
        self.assertEqual(self.find(series((50, 10), (200, 3), (50, 10))), [])

    def test_exactly_min_len_counts(self):
        self.assertEqual(len(self.find(series((50, 10), (200, 4)))), 1)

    def test_one_sample_short_does_not_count(self):
        self.assertEqual(self.find(series((50, 10), (200, 3))), [])

    def test_change_index_is_the_first_sample_after_the_old_value(self):
        changes = self.find(series((50, 10), (120, 1), (200, 8)))
        self.assertEqual(changes[0].index, 10)

    def test_long_transition_still_reports_when_the_old_value_ended(self):
        changes = self.find(series((50, 10), (120, 1), (90, 1), (160, 1), (200, 8)))
        self.assertEqual(changes[0].index, 10)

    def test_rejected_runs_are_ignored(self):
        data = series((50, 10), (250, 10), (200, 10))
        changes = self.find(data, accept=lambda rep: rep[0] != 250)
        self.assertEqual([(c.index, c.start) for c in changes], [(10, 20)])

    def test_equal_runs_either_side_of_a_rejected_run_do_not_change(self):
        data = series((50, 10), (250, 10), (50, 10))
        self.assertEqual(self.find(data, accept=lambda rep: rep[0] != 250), [])

    def test_two_changes_in_a_row(self):
        changes = self.find(series((50, 8), (120, 8), (200, 8)))
        self.assertEqual([c.index for c in changes], [8, 16])


class StripGateTests(unittest.TestCase):
    def test_steady_structure_is_accepted(self):
        box = series((50, 10), (200, 10))
        strip = series((90, 20))
        result = fg.accepted_changes(box, strip, 5, 10, 4, 2)
        self.assertEqual([c.index for c, _ in result], [10])

    def test_scene_cut_is_rejected(self):
        box = series((50, 10), (200, 10))
        strip = series((90, 10), (20, 10))
        self.assertEqual(fg.accepted_changes(box, strip, 5, 10, 4, 2), [])

    def test_change_far_from_the_cut_is_still_accepted(self):
        box = series((50, 10), (200, 20))
        strip = series((90, 20), (20, 10))
        self.assertEqual(len(fg.accepted_changes(box, strip, 5, 10, 4, 2)), 1)

    def test_confidence_is_lower_when_structure_drifts(self):
        change = fg.find_changes(series((50, 10), (200, 10)), 5, 4)[0]
        steady = fg.goal_confidence(change, 0, 5, 10)
        drifting = fg.goal_confidence(change, 6, 5, 10)
        self.assertEqual(steady, 1.0)
        self.assertLess(drifting, steady)

    def test_confidence_is_lower_for_a_small_change(self):
        big = fg.find_changes(series((50, 10), (200, 10)), 5, 4)[0]
        small = fg.find_changes(series((50, 10), (60, 10)), 5, 4)[0]
        self.assertLess(fg.goal_confidence(small, 0, 5, 10),
                        fg.goal_confidence(big, 0, 5, 10))


class ReferenceTests(unittest.TestCase):
    def test_recurring_value_beats_one_long_static_stretch(self):
        data = series((100, 12), (30, 12), (100, 12), (110, 12), (100, 12),
                      (220, 400))
        ref = fg.reference_fingerprint(data, 5, 4, 20, cap=24)
        self.assertLessEqual(fg.distance(ref, fp(100)), 10)

    def test_single_static_value_is_not_a_scoreboard(self):
        self.assertIsNone(fg.reference_fingerprint(series((220, 400)), 5, 4, 20))

    def test_no_stable_runs_gives_none(self):
        self.assertIsNone(fg.reference_fingerprint(series((50, 2)), 5, 4, 20))

    def test_presence_flags(self):
        data = series((100, 2), (160, 2), (95, 2))
        self.assertEqual(fg.presence_flags(data, fp(100), 20),
                         [True, True, False, False, True, True])

    def test_presence_without_reference_is_all_false(self):
        self.assertEqual(fg.presence_flags(series((1, 3)), None, 20), [False] * 3)


class GlyphTests(unittest.TestCase):
    def fingerprint(self, base, bright=()):
        px = bytearray([base]) * 256
        for r, c in bright:
            px[r * 16 + c] = base + 100
        return bytes(px)

    def test_flat_box_has_empty_signature(self):
        self.assertEqual(set(fg.glyph_signature(self.fingerprint(100))), {0})

    def test_overall_brightness_does_not_matter(self):
        dim = fg.glyph_signature(self.fingerprint(60, [(5, 5), (6, 5)]))
        bright = fg.glyph_signature(self.fingerprint(120, [(5, 5), (6, 5)]))
        self.assertEqual(dim, bright)

    def test_stroke_inside_the_window_registers(self):
        sig = fg.glyph_signature(self.fingerprint(100, [(5, 5)]))
        self.assertGreater(sum(sig), 0)

    def test_pixels_outside_the_window_are_ignored(self):
        sig = fg.glyph_signature(self.fingerprint(100, [(0, 0), (15, 15), (8, 14)]))
        self.assertEqual(sum(sig), 0)

    def test_different_digits_are_far_apart(self):
        one = fg.glyph_signature(self.fingerprint(100, [(r, 7) for r in range(4, 12)]))
        zero = fg.glyph_signature(self.fingerprint(100, [(4, c) for c in range(4, 12)]))
        self.assertGreater(fg.distance(one, zero), fg.BOX_TOL)


class FlashTests(unittest.TestCase):
    def change(self, end, start):
        run = fg.Run(0, end, b"")
        return fg.Change(end, start, 50.0, run, fg.Run(start, start + 10, b""))

    def test_dropout_in_the_gap_counts(self):
        present = [True] * 10 + [False] + [True] * 10
        self.assertTrue(fg.flash_seen(present, self.change(10, 11), 2))

    def test_dropout_just_before_the_change_counts(self):
        present = [True] * 9 + [False] + [True] * 10
        self.assertTrue(fg.flash_seen(present, self.change(10, 10), 2))

    def test_steady_scoreboard_has_no_flash(self):
        self.assertFalse(fg.flash_seen([True] * 30, self.change(10, 10), 2))

    def test_distant_dropout_does_not_count(self):
        present = [True] * 3 + [False] + [True] * 26
        self.assertFalse(fg.flash_seen(present, self.change(15, 15), 2))


class SimultaneousTests(unittest.TestCase):
    def ev(self, sample, side, dist):
        return {"sample": sample, "side": side, "distance": dist}

    def test_weaker_echo_is_dropped(self):
        out = fg.drop_simultaneous([self.ev(100, "blue", 40), self.ev(102, "orange", 7)], 8)
        self.assertEqual([e["side"] for e in out], ["blue"])

    def test_order_of_arrival_does_not_matter(self):
        out = fg.drop_simultaneous([self.ev(100, "orange", 7), self.ev(103, "blue", 40)], 8)
        self.assertEqual([e["side"] for e in out], ["blue"])

    def test_far_apart_events_both_stay(self):
        out = fg.drop_simultaneous([self.ev(100, "blue", 40), self.ev(200, "orange", 7)], 8)
        self.assertEqual(len(out), 2)

    def test_same_side_events_both_stay(self):
        out = fg.drop_simultaneous([self.ev(100, "blue", 40), self.ev(104, "blue", 40)], 8)
        self.assertEqual(len(out), 2)


class SeriesTests(unittest.TestCase):
    def test_close_windows_share_a_series(self):
        windows = [(0, 100), (150, 300), (2000, 2200)]
        self.assertEqual(fg.group_series(windows, 600), [1, 1, 2])

    def test_no_windows(self):
        self.assertEqual(fg.group_series([], 600), [])


class DetectGoalsTests(unittest.TestCase):
    """End to end on a synthetic scoreboard: each score is a block shifted 2 columns."""

    def box(self, score, base):
        px = bytearray([base]) * 256
        cells = [(r, c) for r in range(4, 12) for c in range(3 + 2 * score, 9 + 2 * score)]
        for r, c in cells:
            px[r * 16 + c] = 250
        return bytes(px)

    def build(self, plan):
        """plan: list of (seconds, blue_score, orange_score) or (seconds, None, None) = no HUD."""
        blue, orange, strip = [], [], []
        for seconds, b, o in plan:
            for _ in range(int(seconds * fg.FPS)):
                if b is None:
                    blue.append(bytes([30]) * 256)
                    orange.append(bytes([30]) * 256)
                else:
                    blue.append(self.box(b, 100))
                    orange.append(self.box(o, 130))
                strip.append(bytes([90]) * 256)
        return blue, orange, strip

    def flash(self, b, o):
        return (0.5, None, None), (60, b, o)

    def test_two_games_reset_the_score(self):
        plan = [(200, 0, 0), (0.5, None, None), (60, 1, 0), (0.5, None, None),
                (80, 1, 1), (40, None, None), (200, 0, 0), (0.5, None, None),
                (140, 0, 1)]
        windows, events = fg.detect_goals(*self.build(plan))
        self.assertEqual(len(windows), 2)
        self.assertEqual([(e["match"], e["side"], e["blue"], e["orange"]) for e in events],
                         [(1, "blue", 1, 0), (1, "orange", 1, 1), (2, "orange", 0, 1)])

    def test_change_without_a_flash_is_not_a_goal(self):
        plan = [(200, 0, 0), (200, 1, 1), (200, 0, 0)]
        windows, events = fg.detect_goals(*self.build(plan))
        self.assertEqual((len(windows), events), (1, []))

    def test_no_hud_means_no_windows(self):
        windows, events = fg.detect_goals(*self.build([(300, None, None)]))
        self.assertEqual((windows, events), ([], []))


class WindowTests(unittest.TestCase):
    def test_single_window(self):
        present = [False] * 5 + [True] * 50 + [False] * 5
        self.assertEqual(fg.merge_windows(present, 3, 10), [(5, 55)])

    def test_short_dropout_is_merged(self):
        present = [True] * 30 + [False] * 4 + [True] * 30
        self.assertEqual(fg.merge_windows(present, 5, 10), [(0, 64)])

    def test_long_dropout_splits_windows(self):
        present = [True] * 30 + [False] * 40 + [True] * 30
        self.assertEqual(fg.merge_windows(present, 5, 10), [(0, 30), (70, 100)])

    def test_short_blip_is_not_a_window(self):
        present = [True] * 5 + [False] * 40 + [True] * 30
        self.assertEqual(fg.merge_windows(present, 5, 10), [(45, 75)])

    def test_window_running_to_the_end(self):
        self.assertEqual(fg.merge_windows([False] * 3 + [True] * 20, 5, 10), [(3, 23)])

    def test_window_index(self):
        windows = [(10, 20), (30, 40)]
        self.assertEqual(fg.window_index(windows, 10), 0)
        self.assertIsNone(fg.window_index(windows, 20))
        self.assertEqual(fg.window_index(windows, 35), 1)


class ScoreCountTests(unittest.TestCase):
    def test_running_score_per_match(self):
        events = [{"sample": 15, "side": "blue"}, {"sample": 25, "side": "orange"},
                  {"sample": 35, "side": "blue"}, {"sample": 105, "side": "orange"}]
        windows = [(10, 50), (100, 150)]
        tagged = fg.count_scores(events, windows)
        self.assertEqual([(e["match"], e["blue"], e["orange"]) for e in tagged],
                         [(1, 1, 0), (1, 1, 1), (1, 2, 1), (2, 0, 1)])

    def test_events_outside_windows_are_dropped(self):
        events = [{"sample": 5, "side": "blue"}, {"sample": 15, "side": "blue"}]
        tagged = fg.count_scores(events, [(10, 50)])
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["blue"], 1)

    def test_events_are_sorted_before_counting(self):
        events = [{"sample": 30, "side": "orange"}, {"sample": 12, "side": "blue"}]
        tagged = fg.count_scores(events, [(10, 50)])
        self.assertEqual([(e["blue"], e["orange"]) for e in tagged], [(1, 0), (1, 1)])


class ChunkPlanTests(unittest.TestCase):
    def test_exact_multiple(self):
        self.assertEqual(fg.chunk_plan(600, 300), [(0, 300), (300, 300)])

    def test_short_last_chunk(self):
        self.assertEqual(fg.chunk_plan(650.7, 300), [(0, 300), (300, 300), (600, 50)])

    def test_shorter_than_one_chunk(self):
        self.assertEqual(fg.chunk_plan(42, 300), [(0, 42)])


class SplitFrameTests(unittest.TestCase):
    def test_regions_are_separated_in_order(self):
        row = bytes([1] * 16 + [2] * 16 + [3] * 16)
        blue, orange, strip = fg.split_frame(row * 16)
        self.assertEqual((blue, orange, strip),
                         (bytes([1] * 256), bytes([2] * 256), bytes([3] * 256)))

    def test_pixels_keep_their_row_order(self):
        frame = b"".join(bytes([r] * 16 + [0] * 32) for r in range(16))
        blue = fg.split_frame(frame)[0]
        self.assertEqual(blue[:16], bytes([0] * 16))
        self.assertEqual(blue[-16:], bytes([15] * 16))


class HudCommandTests(unittest.TestCase):
    def test_filter_has_one_crop_per_region_and_stacks_them(self):
        graph = fg.hud_filter()
        self.assertEqual(graph.count("crop="), 3)
        self.assertIn("hstack=inputs=3", graph)
        self.assertIn("fps=4", graph)

    def test_command_seeks_and_limits_the_chunk(self):
        cmd = fg.hud_command("v.mp4", 600, 300)
        self.assertEqual(cmd[cmd.index("-ss") + 1], "600")
        self.assertEqual(cmd[cmd.index("-t") + 1], "300")


class ReportTests(unittest.TestCase):
    def event(self, sample, side, match, blue, orange):
        return {"sample": sample, "side": side, "match": match, "blue": blue,
                "orange": orange, "confidence": 0.9}

    def test_goal_time_is_the_first_new_sample_minus_the_lead(self):
        goal = fg.goal_record(1, self.event(400, "blue", 1, 1, 0))
        self.assertEqual(goal["t"], 99.75)
        self.assertEqual(goal["hh:mm:ss"], "00:01:39")
        self.assertEqual(goal["sheet"], "goal-sheets/goal-001.jpg")

    def test_goal_time_never_goes_negative(self):
        self.assertEqual(fg.goal_record(1, self.event(0, "blue", 1, 1, 0))["t"], 0.0)

    def test_window_records_carry_final_score_and_series(self):
        windows = [(0, 800), (1000, 2000), (20000, 21000)]
        events = [self.event(100, "blue", 1, 1, 0), self.event(300, "orange", 1, 1, 1),
                  self.event(1500, "blue", 2, 1, 0)]
        goals = [fg.goal_record(n, e) for n, e in enumerate(events, 1)]
        records = fg.window_records(windows, goals)
        self.assertEqual([(r["final_blue"], r["final_orange"]) for r in records],
                         [(1, 1), (1, 0), (0, 0)])
        self.assertEqual([r["series"] for r in records], [1, 1, 2])
        self.assertEqual(records[0]["end_hms"], "00:03:20")

    def test_report_is_json_serialisable_and_ordered(self):
        import json
        report = fg.build_report("/x/v.mp4", [(0, 800)],
                                 [self.event(100, "blue", 1, 1, 0)])
        self.assertEqual(report["video"], "v.mp4")
        self.assertEqual(json.loads(json.dumps(report))["goals"][0]["n"], 1)


class SheetTimesTests(unittest.TestCase):
    def test_offsets_around_the_goal(self):
        times = fg.sheet_times(100.0, 7000)
        self.assertEqual([o for o, _ in times], [-8, -4, -1, 2, 5, 9])
        self.assertEqual([t for _, t in times], [92.0, 96.0, 99.0, 102.0, 105.0, 109.0])

    def test_clamped_to_the_video(self):
        times = fg.sheet_times(3.0, 10)
        self.assertEqual(times[0][1], 0.0)
        self.assertEqual(times[-1][1], 9.0)


class FormatTests(unittest.TestCase):
    def test_hms(self):
        self.assertEqual(fg.format_hms(3723.9), "01:02:03")


if __name__ == "__main__":
    unittest.main()
