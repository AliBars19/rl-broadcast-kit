#!/usr/bin/env python3
"""Unit tests for stats.py. Run: python3 -I -m unittest test_stats.py"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # python3 -I drops the script dir from sys.path

import stats as S  # noqa: E402

TODAY = dt.date(2025, 6, 15)


def entry(vid, age=None, duration=30, views=100, timestamp=True, **extra):
    """Build a yt-dlp style entry; age is days before TODAY (None = no timestamp)."""
    data = {"id": vid, "title": f"video {vid}", "duration": duration,
            "view_count": views, "like_count": 10, "comment_count": 1,
            "repost_count": 0}
    if timestamp and age is not None:
        day = dt.datetime.combine(TODAY - dt.timedelta(days=age), dt.time(12),
                                  tzinfo=dt.timezone.utc)
        data["timestamp"] = int(day.timestamp())
    data.update(extra)
    return data


def account(entries):
    videos = [S.to_video(e, TODAY) for e in entries]
    return S.account_row("tester", "clip_page", entries, videos), videos


class PercentileTests(unittest.TestCase):
    def test_median_even_and_odd(self):
        self.assertEqual(S.median([1, 2, 3, 4]), 2.5)
        self.assertEqual(S.median([5, 1, 3]), 3)

    def test_empty_returns_none(self):
        self.assertIsNone(S.percentile([], 50))
        self.assertIsNone(S.median([]))

    def test_interpolated_quartiles(self):
        self.assertEqual(S.percentile([1, 2, 3, 4, 5], 25), 2.0)
        self.assertAlmostEqual(S.percentile([1, 2, 3, 4], 90), 3.7)

    def test_single_value(self):
        self.assertEqual(S.percentile([7], 90), 7.0)


class PostsPerWeekTests(unittest.TestCase):
    def setUp(self):
        # ages 0,6,7,29 fall in the last 30 days; 30 and 89 only in the last 90
        ages = [0, 6, 7, 29, 30, 89, 90]
        self.row, _ = account([entry(str(a), age=a) for a in ages])

    def test_counts_in_windows(self):
        self.assertEqual(self.row["posts_30d"], 4)
        self.assertEqual(self.row["posts_90d"], 6)

    def test_posts_per_week(self):
        self.assertAlmostEqual(self.row["posts_per_week_30d"], round(4 / (30 / 7), 3))
        self.assertAlmostEqual(self.row["posts_per_week_90d"], round(6 / (90 / 7), 3))

    def test_active_day_share(self):
        self.assertAlmostEqual(self.row["active_day_share_30d"], round(4 / 30, 3))

    def test_max_posts_one_day(self):
        entries = [entry("a", age=0), entry("b", age=0), entry("c", age=50)]
        row, _ = account(entries)
        self.assertEqual(row["max_posts_one_day_90d"], 2)

    def test_days_since_latest(self):
        self.assertEqual(self.row["days_since_latest"], 0)


class PinnedOrderTests(unittest.TestCase):
    def test_unsorted_timestamps_give_same_result(self):
        ordered = [entry(str(a), age=a, duration=d)
                   for a, d in [(0, 10), (2, 20), (5, 30), (40, 40)]]
        shuffled = [ordered[2], ordered[3], ordered[0], ordered[1]]
        row_a, _ = account(ordered)
        row_b, _ = account(shuffled)
        for key in ("posts_30d", "posts_90d", "max_posts_one_day_90d",
                    "days_since_latest", "median_duration", "active_day_share_30d"):
            self.assertEqual(row_a[key], row_b[key], key)
        self.assertEqual(row_b["posts_30d"], 3)
        self.assertEqual(row_b["days_since_latest"], 0)


class MissingFieldTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry("full", age=3, duration=30),
            entry("no_ts", age=None, duration=20),
            entry("no_dur", age=1, duration=None),
        ]
        self.row, self.videos = account(self.entries)

    def test_missing_counts_entries_lacking_timestamp_or_duration(self):
        self.assertEqual(self.row["missing"], 2)
        self.assertEqual(self.row["videos_fetched"], 3)

    def test_timestamp_gap_excluded_from_post_counts(self):
        self.assertEqual(self.row["posts_30d"], 2)

    def test_duration_gap_excluded_from_duration_stats(self):
        self.assertEqual(self.row["median_duration"], 25)

    def test_load_raw_reports_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries, err = S.load_raw(tmp, "nobody")
            self.assertIsNone(entries)
            self.assertIn("no raw file", err)


class TitleTests(unittest.TestCase):
    def test_newlines_removed_and_truncated(self):
        video = S.to_video(entry("t", age=1, title="a\nb\r\n" + "x" * 300), TODAY)
        self.assertNotIn("\n", video["title"])
        self.assertNotIn("\r", video["title"])
        self.assertEqual(len(video["title"]), 120)


class DurationBucketTests(unittest.TestCase):
    def test_bucket_edges(self):
        durations = [10, 14.9, 15, 29, 30, 59, 60, 89.9, 90, 200]
        videos = [S.to_video(entry(str(i), age=5, duration=d, views=100), TODAY)
                  for i, d in enumerate(durations)]
        table = {b["bucket"]: b["videos"] for b in S.bucket_table(videos)}
        self.assertEqual(table["under 15 s"], 2)
        self.assertEqual(table["15-30 s"], 2)
        self.assertEqual(table["30-45 s"], 1)
        self.assertEqual(table["45-60 s"], 1)
        self.assertEqual(table["60-90 s"], 2)
        self.assertEqual(table["over 90 s"], 2)

    def test_median_views_per_bucket(self):
        videos = [S.to_video(entry("a", age=5, duration=20, views=100), TODAY),
                  S.to_video(entry("b", age=5, duration=25, views=300), TODAY)]
        table = {b["bucket"]: b for b in S.bucket_table(videos)}
        self.assertEqual(table["15-30 s"]["median_views"], 200)

    def test_scope_summary_ignores_videos_older_than_365_days(self):
        rows = [account([entry("old", age=400, duration=10),
                         entry("new", age=5, duration=10)])[0]]
        videos = [S.to_video(e, TODAY) for e in
                  [entry("old", age=400, duration=10), entry("new", age=5, duration=10)]]
        summary = S.scope_summary(rows, videos)
        table = {b["bucket"]: b["videos"] for b in summary["duration_buckets_last_365d"]}
        self.assertEqual(table["under 15 s"], 1)


class LoadAccountsTests(unittest.TestCase):
    def test_dedupe_skip_unvalidated_and_bad_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "accounts-a.json"), "w", encoding="utf-8") as fh:
                json.dump([{"handle": "Foo", "segment": "x", "validated": True},
                           {"handle": "bar", "segment": "y", "validated": False}], fh)
            with open(os.path.join(tmp, "accounts-b.json"), "w", encoding="utf-8") as fh:
                json.dump([{"handle": "foo", "segment": "z", "validated": True},
                           {"handle": "@baz", "segment": "z", "validated": True}], fh)
            with open(os.path.join(tmp, "accounts-c.json"), "w", encoding="utf-8") as fh:
                fh.write('[{"handle": "half')
            accounts = S.load_accounts(tmp)
        self.assertEqual([(a["handle"], a["segment"]) for a in accounts],
                         [("Foo", "x"), ("baz", "z")])


class PercentileDistributionTests(unittest.TestCase):
    def test_distribution_keys(self):
        dist = S.distribution([1, 2, 3, 4, 5])
        self.assertEqual(dist["n"], 5)
        self.assertEqual(dist["median"], 3)
        self.assertEqual(dist["min"], 1)
        self.assertEqual(dist["max"], 5)


if __name__ == "__main__":
    unittest.main()
