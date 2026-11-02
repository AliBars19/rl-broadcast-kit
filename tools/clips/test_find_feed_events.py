"""Unit tests for the pure parts of find_feed_events (run with python3 -I -m unittest)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import find_feed_events as ff  # noqa: E402

BG = (70, 100, 130)
BLUE = (110, 135, 215)
ORANGE = (240, 155, 110)
GOLD = (255, 225, 150)


def blank():
    return bytearray(bytes(BG) * (ff.W2 * ff.H2))


def paint(frame, x0, x1, y0, y1, color):
    for y in range(y0, min(y1, ff.H2)):
        for x in range(x0, x1):
            frame[(y * ff.W2 + x) * 3:(y * ff.W2 + x) * 3 + 3] = bytes(color)


def chip(frame, k, x0, x1, color):
    top = ff.row_top(k)
    paint(frame, x0, x1, top, top + 17, color)


def icon(frame, k, color=GOLD):
    x0, y0 = ff.ICON_X, ff.icon_top(k)
    paint(frame, x0 + 5, x0 + 25, y0 + 5, y0 + 25, color)


class ChipColourTests(unittest.TestCase):
    def test_blue_body(self):
        self.assertEqual(ff.classify_chip(*BLUE), "blue")

    def test_orange_body(self):
        self.assertEqual(ff.classify_chip(*ORANGE), "orange")

    def test_sky_is_not_a_chip(self):
        self.assertIsNone(ff.classify_chip(120, 160, 200))

    def test_white_text_is_not_a_chip(self):
        self.assertIsNone(ff.classify_chip(255, 235, 215))


class ObserveTests(unittest.TestCase):
    def test_empty_frame(self):
        self.assertEqual(ff.observe(bytes(blank())), [])

    def test_single_blue_chip_with_icon(self):
        frame = blank()
        chip(frame, 0, 90, 148, BLUE)
        icon(frame, 0)
        rows = ff.observe(bytes(frame))
        self.assertEqual([(r.k, r.colours, r.kind) for r in rows], [(0, ("blue",), "single")])
        self.assertEqual(len(rows[0].fp), ff.FP_BYTES)

    def test_orange_chip_second_row(self):
        frame = blank()
        chip(frame, 1, 80, 148, ORANGE)
        icon(frame, 1)
        rows = ff.observe(bytes(frame))
        self.assertEqual([(r.k, r.colours) for r in rows], [(1, ("orange",))])

    def test_chip_without_icon_is_ignored(self):
        frame = blank()
        chip(frame, 0, 90, 148, BLUE)
        self.assertEqual(ff.observe(bytes(frame)), [])

    def test_icon_without_chip_is_ignored(self):
        frame = blank()
        icon(frame, 0)
        self.assertEqual(ff.observe(bytes(frame)), [])

    def test_two_stacked_rows(self):
        frame = blank()
        for k, color in ((0, BLUE), (1, ORANGE)):
            chip(frame, k, 90, 148, color)
            icon(frame, k)
        rows = ff.observe(bytes(frame))
        self.assertEqual([(r.k, r.colours) for r in rows], [(0, ("blue",)), (1, ("orange",))])

    def test_demolition_without_bomb_is_ignored(self):
        frame = blank()
        chip(frame, 0, 60, 100, BLUE)
        chip(frame, 0, 122, 166, ORANGE)
        self.assertEqual(ff.observe(bytes(frame)), [])

    def test_demolition_two_chips(self):
        frame = blank()
        chip(frame, 0, 60, 100, BLUE)
        chip(frame, 0, 122, 166, ORANGE)
        paint(frame, 100, 122, 12, 32, (250, 250, 250))
        rows = ff.observe(bytes(frame))
        self.assertEqual([(r.k, r.colours, r.kind) for r in rows],
                         [(0, ("blue", "orange"), "demo")])

    def test_short_run_is_noise(self):
        frame = blank()
        chip(frame, 0, 130, 142, BLUE)
        icon(frame, 0)
        self.assertEqual(ff.observe(bytes(frame)), [])

    def test_third_row_is_clipped_and_has_no_fingerprint(self):
        frame = blank()
        chip(frame, 2, 90, 148, ORANGE)
        icon(frame, 2)
        rows = ff.observe(bytes(frame))
        self.assertEqual([(r.k, r.clipped, r.fp) for r in rows], [(2, True, None)])


class FingerprintTests(unittest.TestCase):
    def test_distance_zero_for_equal(self):
        self.assertAlmostEqual(ff.fp_distance(b"\x05" * 4, b"\x05" * 4), 0)

    def test_distance_ignores_brightness(self):
        self.assertAlmostEqual(ff.fp_distance(b"\x05\x00\x0a", b"\x32\x00\x64"), 0)

    def test_distance_one_when_disjoint(self):
        self.assertAlmostEqual(ff.fp_distance(b"\xff\x00", b"\x00\xff"), 1.0)

    def test_empty_fingerprints(self):
        self.assertEqual(ff.fp_distance(b"\x00\x00", b"\x00\x00"), 0.0)
        self.assertEqual(ff.fp_distance(b"\x00\x00", b"\x00\xff"), 1.0)

    def test_icon_fp_depends_on_shape(self):
        a, b = blank(), blank()
        paint(a, ff.ICON_X + 2, ff.ICON_X + 12, ff.icon_top(0) + 2, ff.icon_top(0) + 12, GOLD)
        paint(b, ff.ICON_X + 18, ff.ICON_X + 28, ff.icon_top(0) + 18, ff.icon_top(0) + 28, GOLD)
        fa = ff.icon_fp(bytes(a), ff.ICON_X, ff.icon_top(0))
        fb = ff.icon_fp(bytes(b), ff.ICON_X, ff.icon_top(0))
        self.assertGreater(ff.fp_distance(fa, fb), 0.5)

    def test_icon_fp_ignores_background_colour(self):
        a = blank()
        b = bytearray(bytes((140, 60, 200)) * (ff.W2 * ff.H2))
        for frame in (a, b):
            paint(frame, ff.ICON_X + 5, ff.ICON_X + 25, ff.icon_top(0) + 5, ff.icon_top(0) + 25, GOLD)
        self.assertEqual(ff.icon_fp(bytes(a), ff.ICON_X, ff.icon_top(0)),
                         ff.icon_fp(bytes(b), ff.ICON_X, ff.icon_top(0)))


def shape(n, value=255):
    """Synthetic fingerprint: a block of 24 cells lit; different n never overlap."""
    fp = bytearray(ff.FP_BYTES)
    fp[n * 24:(n + 1) * 24] = bytes([value]) * 24
    return bytes(fp)


def row(k, colours=("blue",), kind="single", level=0, clipped=False, value=255):
    """Synthetic observation row whose icon is shape number `level`."""
    return ff.Row(k, colours, kind, None if clipped else shape(level, value), clipped, ff.ICON_X)


def run_tracker(samples):
    return ff.track_events([(i / ff.FPS, rows) for i, rows in enumerate(samples)])


class TrackerTests(unittest.TestCase):
    def test_one_popup(self):
        events = run_tracker([[]] * 3 + [[row(0)]] * 10 + [[]] * 3)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].t, 0.75)
        self.assertAlmostEqual(events[0].duration, 2.5)

    def test_single_sample_noise_dropped(self):
        events = run_tracker([[]] * 3 + [[row(0)]] + [[]] * 5)
        self.assertEqual(events, [])

    def test_detection_gap_is_bridged(self):
        samples = [[row(0)]] * 6 + [[]] * 2 + [[row(0)]] * 6
        self.assertEqual(len(run_tracker(samples)), 1)

    def test_long_gap_splits(self):
        samples = [[row(0)]] * 6 + [[]] * 8 + [[row(0)]] * 6
        self.assertEqual(len(run_tracker(samples)), 2)

    def test_stack_shifts_up_without_new_event(self):
        a, b = row(0, ("blue",)), row(1, ("orange",), level=2)
        moved = row(0, ("orange",), level=2)
        samples = [[a]] * 3 + [[a, b]] * 8 + [[moved]] * 8
        events = run_tracker(samples)
        self.assertEqual([e.colours for e in events], [("blue",), ("orange",)])
        self.assertAlmostEqual(events[1].t, 3 / ff.FPS)

    def test_identical_popups_shift_still_two_events(self):
        a, b = row(0), row(1)
        samples = [[a]] * 4 + [[a, b]] * 8 + [[row(0)]] * 8
        self.assertEqual(len(run_tracker(samples)), 2)

    def test_same_row_other_side_is_a_new_event(self):
        samples = [[row(0, level=1)]] * 8 + [[row(0, ("orange",), level=1)]] * 8
        self.assertEqual(len(run_tracker(samples)), 2)

    def test_clipped_row_has_no_fingerprint_but_still_tracks(self):
        a, b, c = row(0), row(1, level=1), row(2, level=2, clipped=True)
        c_full = row(1, level=2)
        samples = [[a, b]] * 4 + [[a, b, c]] * 4 + [[b, c_full]] * 8
        events = run_tracker(samples)
        self.assertEqual(len(events), 3)
        self.assertIsNotNone(events[2].fp)

    def test_event_fingerprint_is_median(self):
        samples = [[row(0, value=v)] for v in (30, 30, 50, 50, 50, 50, 50, 50, 70, 50)]
        events = run_tracker(samples)
        self.assertEqual(events[0].fp, shape(0, 50))


def vec(i, noise=0):
    """8 cell fingerprint with cell i lit; `noise` lights cell i+1 a little."""
    out = [0] * 8
    out[i] = 200
    out[(i + 1) % 8] = noise
    return bytes(out)


class ClusterTests(unittest.TestCase):
    def test_groups_by_shape_and_sorts_by_size(self):
        fps = [vec(0), vec(0, 20), vec(4), vec(0, 40), vec(4, 30)]
        self.assertEqual(ff.cluster_fps(fps, 0.2), [1, 1, 2, 1, 2])

    def test_everything_different_is_its_own_cluster(self):
        fps = [vec(0), vec(2), vec(4)]
        self.assertEqual(sorted(ff.cluster_fps(fps, 0.2)), [1, 2, 3])

    def test_empty(self):
        self.assertEqual(ff.cluster_fps([], 0.2), [])

    def test_threshold_controls_merging(self):
        fps = [vec(0), vec(0, 130)]
        self.assertEqual(ff.cluster_fps(fps, 0.05), [1, 2])
        self.assertEqual(ff.cluster_fps(fps, 0.3), [1, 1])

    def test_merge_close_joins_near_centres_only(self):
        centres = [vec(0), vec(0, 20), vec(4)]
        labels = ff.merge_close(centres, [0, 1, 2, 1], 0.1)
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[1], labels[3])
        self.assertNotEqual(labels[0], labels[2])


class AbsorbTests(unittest.TestCase):
    def test_small_cluster_joins_nearby_big_one(self):
        fps = [vec(0)] * 4 + [vec(0, 150)]
        self.assertEqual(ff.absorb_small(fps, [0, 0, 0, 0, 1], 0.45), [0] * 5)

    def test_small_cluster_far_away_stays(self):
        fps = [vec(0)] * 4 + [vec(4)]
        self.assertEqual(ff.absorb_small(fps, [0, 0, 0, 0, 1], 0.45), [0, 0, 0, 0, 1])

    def test_no_big_cluster_changes_nothing(self):
        fps = [vec(0), vec(1)]
        self.assertEqual(ff.absorb_small(fps, [0, 1], 0.45), [0, 1])


class PlanTests(unittest.TestCase):
    def test_chunks_cover_window(self):
        plan = ff.chunk_plan([(10.0, 40.0)], chunk=12)
        self.assertEqual(plan, [(10.0, 48), (22.0, 48), (34.0, 24)])

    def test_two_windows(self):
        plan = ff.chunk_plan([(0.0, 5.0), (100.0, 103.0)], chunk=60)
        self.assertEqual(plan, [(0.0, 20), (100.0, 12)])


class ReportTests(unittest.TestCase):
    def test_match_for_time(self):
        windows = [{"match": 1, "start": 10.0, "end": 20.0}, {"match": 2, "start": 30.0, "end": 40.0}]
        self.assertEqual(ff.match_for(windows, 35.0), 2)
        self.assertIsNone(ff.match_for(windows, 25.0))

    def test_goal_hits(self):
        goals = [{"t": 100.0, "side": "blue"}]
        self.assertTrue(ff.near_goal(goals, 102.0, "blue"))
        self.assertFalse(ff.near_goal(goals, 102.0, "orange"))
        self.assertFalse(ff.near_goal(goals, 110.0, "blue"))

    def test_pick_examples_spreads_evenly(self):
        self.assertEqual(ff.pick_examples(list(range(100)), 4), [0, 33, 66, 99])
        self.assertEqual(ff.pick_examples([1, 2], 12), [1, 2])


if __name__ == "__main__":
    unittest.main()
