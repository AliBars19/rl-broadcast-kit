"""Unit tests for the pure parts of find_candidates (run with python3 -I -m unittest)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import find_candidates as fc  # noqa: E402


class FormatTimestampTests(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(fc.format_hms(0), "00:00:00")

    def test_minutes_and_seconds(self):
        self.assertEqual(fc.format_hms(125), "00:02:05")

    def test_over_one_hour(self):
        self.assertEqual(fc.format_hms(3 * 3600 + 7 * 60 + 9), "03:07:09")

    def test_fractional_seconds_truncate(self):
        self.assertEqual(fc.format_hms(59.9), "00:00:59")


class SilenceTests(unittest.TestCase):
    def test_long_quiet_stretch_is_flagged(self):
        db = [-20.0] * 5 + [-80.0] * 25 + [-20.0] * 5
        mask = fc.silent_mask(db, threshold_db=-60.0, min_len=20)
        self.assertEqual(mask, [False] * 5 + [True] * 25 + [False] * 5)

    def test_short_quiet_stretch_is_kept(self):
        db = [-20.0] * 5 + [-80.0] * 19 + [-20.0] * 5
        self.assertFalse(any(fc.silent_mask(db, -60.0, 20)))

    def test_exact_min_length_is_flagged(self):
        db = [-80.0] * 20
        self.assertTrue(all(fc.silent_mask(db, -60.0, 20)))

    def test_trailing_stretch_is_flagged(self):
        db = [-20.0] * 3 + [-90.0] * 22
        mask = fc.silent_mask(db, -60.0, 20)
        self.assertEqual(mask[3:], [True] * 22)
        self.assertFalse(any(mask[:3]))


class PeakPickingTests(unittest.TestCase):
    def test_picks_highest_first(self):
        scores = [0, 1, 5, 1, 0, 0, 0, 9, 0, 0, 0, 0, 3, 0]
        peaks = fc.pick_peaks(scores, [True] * len(scores), min_sep=3, limit=3)
        self.assertEqual(peaks, [7, 2, 12])

    def test_min_separation_enforced(self):
        scores = [0.0] * 40
        scores[10], scores[14], scores[25] = 9.0, 8.0, 7.0
        peaks = fc.pick_peaks(scores, [True] * 40, min_sep=10, limit=10)
        self.assertEqual(peaks[:2], [10, 25])
        self.assertTrue(all(abs(a - b) >= 10 for a in peaks for b in peaks if a != b))

    def test_exactly_min_sep_apart_is_allowed(self):
        scores = [0.0] * 30
        scores[5], scores[15] = 9.0, 8.0
        self.assertEqual(fc.pick_peaks(scores, [True] * 30, 10, 2), [5, 15])

    def test_ineligible_seconds_are_skipped(self):
        scores = [0.0, 9.0, 0.0, 5.0, 0.0]
        eligible = [True, False, True, True, True]
        self.assertEqual(fc.pick_peaks(scores, eligible, 1, 1), [3])
        self.assertNotIn(1, fc.pick_peaks(scores, eligible, 1, 5))

    def test_limit_respected(self):
        scores = [float(i % 7) for i in range(100)]
        self.assertEqual(len(fc.pick_peaks(scores, [True] * 100, 10, 4)), 4)

    def test_empty_input(self):
        self.assertEqual(fc.pick_peaks([], [], 10, 5), [])


class EligibilityTests(unittest.TestCase):
    def test_intro_and_silence_are_excluded(self):
        silent = [False] * 10 + [True] * 5 + [False] * 5
        eligible = fc.eligible_mask(silent, skip_first=4)
        self.assertEqual(eligible[:4], [False] * 4)
        self.assertEqual(eligible[4:10], [True] * 6)
        self.assertEqual(eligible[10:15], [False] * 5)
        self.assertEqual(eligible[15:], [True] * 5)


class ScoreTests(unittest.TestCase):
    def test_louder_second_scores_higher(self):
        rms = [-40.0] * 60
        high = [-50.0] * 60
        rms[30], high[30] = -15.0, -20.0
        scores = fc.combined_scores(rms, high, [True] * 60)
        best = max(range(60), key=scores.__getitem__)
        self.assertLessEqual(abs(best - 30), 1)

    def test_constant_signal_does_not_crash(self):
        scores = fc.combined_scores([-30.0] * 20, [-40.0] * 20, [True] * 20)
        self.assertEqual(len(scores), 20)


if __name__ == "__main__":
    unittest.main()
