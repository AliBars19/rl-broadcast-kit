"""Tests for the pure parts of measure_latency.py."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import measure_latency as ml  # noqa: E402

FRAME_US = 16_667


def series(change_at_us, start_level=10.0, end_level=120.0, ramp=0, total=200):
    samples = []
    for i in range(total):
        t = i * FRAME_US
        level = end_level if t >= change_at_us + ramp else start_level
        samples.append((t, level))
    return samples


class TransitionTests(unittest.TestCase):
    def test_latency_is_time_from_the_request_to_the_first_bright_frame(self):
        at = 40 * FRAME_US
        found = ml.find_transition(series(at + 5 * FRAME_US), at)
        self.assertEqual(round(found[0] / FRAME_US), 5)

    def test_falling_edge_is_found_too(self):
        at = 40 * FRAME_US
        found = ml.find_transition(series(at + 3 * FRAME_US, 120.0, 10.0), at)
        self.assertEqual(round(found[0] / FRAME_US), 3)

    def test_small_changes_are_not_trusted(self):
        at = 40 * FRAME_US
        self.assertIsNone(ml.find_transition(series(at, 10.0, 12.0), at))

    def test_too_little_data_gives_none(self):
        self.assertIsNone(ml.find_transition([(0, 1.0)], 100))

    def test_summary_of_nothing(self):
        self.assertEqual(ml.summarize([{"error": "x"}]), {"samples": 0})

    def test_summary_statistics(self):
        s = ml.summarize([{"latency_ms": 100.0}, {"latency_ms": 120.0}, {"latency_ms": 110.0}])
        self.assertEqual((s["samples"], s["min_ms"], s["p50_ms"], s["max_ms"]), (3, 100.0, 110.0, 120.0))

    def test_decoder_command_uses_low_delay_options(self):
        cmd = ml.decoder_command(49410)
        self.assertIn("-flush_packets", cmd)
        self.assertIn("udp://127.0.0.1:49410?fifo_size=65536&overrun_nonfatal=1", cmd)
        self.assertEqual(cmd[cmd.index("-flags") + 1], "low_delay")


if __name__ == "__main__":
    unittest.main()
