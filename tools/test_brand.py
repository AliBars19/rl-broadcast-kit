"""Tests for brand.py: reading brand values out of config.js."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brand  # noqa: E402


class FromStreamTests(unittest.TestCase):
    def test_empty_stream_gives_placeholders(self):
        self.assertEqual(brand.from_stream({}), {**brand.DEFAULTS, "sponsor_logo": None})

    def test_values_are_read(self):
        got = brand.from_stream({
            "channel": "Test Channel", "mark": "tc",
            "handles": {"twitch": "", "youtube": "testchan", "tiktok": "@other"},
            "scoreboard": {"blue": {"name": "HOME"}},
            "programDefaults": {"eventName": "MY CUP 1", "eventSub": "A | B"},
        })
        self.assertEqual(got["channel"], "Test Channel")
        self.assertEqual(got["mark"], "T")
        self.assertEqual(got["handle"], "@testchan")
        self.assertEqual(got["home_team"], "HOME")
        self.assertEqual((got["event_name"], got["event_sub"]), ("MY CUP 1", "A | B"))

    def test_mark_falls_back_to_the_channel_initial(self):
        self.assertEqual(brand.from_stream({"channel": "zeta"})["mark"], "Z")

    def test_sponsor_logo_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            logo = os.path.join(tmp, "logo.svg")
            open(logo, "w").close()
            stream = {"sponsor": {"logo": "logo.svg"}}
            self.assertEqual(brand.from_stream(stream, tmp)["sponsor_logo"], logo)
            self.assertIsNone(brand.from_stream({"sponsor": {"logo": "missing.svg"}}, tmp)["sponsor_logo"])

    def test_null_sponsor_means_no_logo(self):
        self.assertIsNone(brand.from_stream({"sponsor": None})["sponsor_logo"])


class LoadBrandTests(unittest.TestCase):
    def test_node_failure_falls_back_to_defaults(self):
        def boom(*args, **kwargs):
            raise FileNotFoundError("node")
        self.assertEqual(brand.load_brand("/x/config.js", run=boom), brand.defaults())

    def test_bad_output_falls_back_to_defaults(self):
        fake = mock.Mock(stdout="not json")
        self.assertEqual(brand.load_brand("/x/config.js", run=lambda *a, **k: fake), brand.defaults())

    def test_failed_process_falls_back_to_defaults(self):
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "node")
        self.assertEqual(brand.load_brand("/x/config.js", run=fail), brand.defaults())

    def test_shipped_config_has_no_sponsor_and_generic_names(self):
        got = brand.load_brand()
        self.assertIsNone(got["sponsor_logo"])
        self.assertEqual(got["channel"], "YOUR CHANNEL")


if __name__ == "__main__":
    unittest.main()
