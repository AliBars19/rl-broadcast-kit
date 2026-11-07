"""Tests for join_chunks.py and retention.py (mock subprocess; one real ffmpeg join)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunks  # noqa: E402
import join_chunks  # noqa: E402
import retention  # noqa: E402

N1 = "cam-20261107-203015-0001-1794062677451.mp4"
N2 = "cam-20261107-203015-0002-1794062977451.mp4"
N4 = "cam-20261107-203015-0004-1794063577451.mp4"
SIG = join_chunks.Signature((("h264", 1920, 1080),), (("aac", "48000"),))
SIG_NO_AUDIO = join_chunks.Signature((("h264", 1920, 1080),), ())


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cam = Path(self.tmp.name) / "2026-11-07" / "cam"
        self.cam.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concat_list_escapes_quotes(self):
        self.assertEqual(join_chunks.concat_list([Path("/a/it's.mp4")]), "file '/a/it'\\''s.mp4'\n")

    def test_nothing_to_join(self):
        with self.assertRaises(join_chunks.JoinError):
            join_chunks.join(self.cam)

    def test_refuses_to_overwrite_without_force(self):
        (self.cam / N1).write_bytes(b"x")
        (self.cam / "cam-joined.mp4").write_bytes(b"old")
        with self.assertRaises(join_chunks.JoinError):
            join_chunks.join(self.cam)

    def test_gaps_in_numbering_are_reported(self):
        files = [self.cam / n for n in (N1, N2, N4)]
        self.assertEqual(join_chunks.missing_indexes(files), ["20261107-203015 #3"])

    def test_ffmpeg_is_called_with_stream_copy_and_in_time_order(self):
        for name in (N2, N1):
            (self.cam / name).write_bytes(b"x")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen["list"] = Path(cmd[cmd.index("-i") + 1]).read_text()
            Path(cmd[-1]).write_bytes(b"joined")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        out = join_chunks.join(self.cam, run=fake_run, log=lambda m: None, probe=lambda p: SIG)
        self.assertEqual(out, self.cam / "cam-joined.mp4")
        self.assertEqual(seen["cmd"][seen["cmd"].index("-c") + 1], "copy")
        self.assertLess(seen["list"].index(N1), seen["list"].index(N2))
        self.assertFalse(list(self.cam.glob(".*.part")))

    def test_ffmpeg_failure_leaves_no_output(self):
        (self.cam / N1).write_bytes(b"x")
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "moov atom not found")
        with self.assertRaises(join_chunks.JoinError):
            join_chunks.join(self.cam, run=fail, log=lambda m: None, probe=lambda p: SIG)
        self.assertEqual([p.name for p in self.cam.iterdir()], [N1])

    def test_chunks_with_different_video_are_refused(self):
        for name in (N1, N2):
            (self.cam / name).write_bytes(b"x")
        sigs = {N1: SIG, N2: join_chunks.Signature((("hevc", 1920, 1080),), (("aac", "48000"),))}
        ran = []
        with self.assertRaises(join_chunks.JoinError) as ctx:
            join_chunks.join(self.cam, run=lambda *a, **k: ran.append(a), log=lambda m: None, probe=lambda p: sigs[p.name])
        self.assertIn(N2, str(ctx.exception))
        self.assertEqual(ran, [])

    def test_different_audio_codecs_are_refused(self):
        for name in (N1, N2):
            (self.cam / name).write_bytes(b"x")
        sigs = {N1: SIG, N2: join_chunks.Signature(SIG.video, (("mp3", "44100"),))}
        with self.assertRaises(join_chunks.JoinError):
            join_chunks.join(self.cam, run=lambda *a, **k: None, log=lambda m: None, probe=lambda p: sigs[p.name])

    def test_a_chunk_without_audio_gets_silent_audio_instead_of_refusing_the_join(self):
        for name in (N1, N2, N4):
            (self.cam / name).write_bytes(b"x")
        sigs = {N1: SIG, N2: SIG_NO_AUDIO, N4: SIG}
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            if "anullsrc=r=48000:cl=mono" in cmd:
                Path(cmd[-1]).write_bytes(b"fixed")
            else:
                seen.append(Path(cmd[cmd.index("-i") + 1]).read_text())
                Path(cmd[-1]).write_bytes(b"joined")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        logs = []
        out = join_chunks.join(self.cam, run=fake_run, log=logs.append, probe=lambda p: sigs[p.name])
        self.assertEqual(out.read_bytes(), b"joined")
        listing = seen[-1]
        self.assertIn(".join-", listing.splitlines()[1])             # the odd chunk comes from the scratch folder
        self.assertIn(N1, listing.splitlines()[0])
        self.assertIn(N4, listing.splitlines()[2])
        self.assertTrue(any("silent audio" in l for l in logs))
        self.assertEqual([p.name for p in self.cam.iterdir() if p.name.startswith(".join-")], [])

    def test_no_chunk_has_audio_is_a_consistent_set(self):
        for name in (N1, N2):
            (self.cam / name).write_bytes(b"x")
        ran = []

        def fake_run(cmd, **kw):
            ran.append(cmd)
            Path(cmd[-1]).write_bytes(b"joined")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        join_chunks.join(self.cam, run=fake_run, log=lambda m: None, probe=lambda p: SIG_NO_AUDIO)
        self.assertEqual(len(ran), 1)

    def test_audio_that_covers_almost_none_of_the_chunk_is_padded_like_a_missing_track(self):
        for name in (N1, N2):
            (self.cam / name).write_bytes(b"x")
        short = join_chunks.Signature(SIG.video, SIG.audio, audio_short=True)
        sigs = {N1: SIG, N2: short}
        padded = []

        def fake_run(cmd, **kw):
            if "anullsrc=r=48000:cl=mono" in cmd:
                padded.append(cmd[cmd.index("-i") + 1])
                Path(cmd[-1]).write_bytes(b"fixed")
            else:
                Path(cmd[-1]).write_bytes(b"joined")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        join_chunks.join(self.cam, run=fake_run, log=lambda m: None, probe=lambda p: sigs[p.name])
        self.assertEqual([Path(p).name for p in padded], [N2])

    def test_signature_flags_audio_much_shorter_than_the_video(self):
        def run_for(audio_s):
            out = json.dumps({"streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "duration": "120.0"},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "duration": audio_s}]})
            return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")
        self.assertTrue(join_chunks.stream_signature(self.cam / N1, run=run_for("0.021")).audio_short)
        self.assertTrue(join_chunks.stream_signature(self.cam / N1, run=run_for("59.9")).audio_short)
        self.assertFalse(join_chunks.stream_signature(self.cam / N1, run=run_for("118.8")).audio_short)

    def test_stream_signature_reads_ffprobe_json(self):
        out = json.dumps({"streams": [{"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"},
                                      {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}]})
        run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")
        self.assertEqual(join_chunks.stream_signature(self.cam / N1, run=run), SIG)
        bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "moov atom not found")
        with self.assertRaises(join_chunks.JoinError):
            join_chunks.stream_signature(self.cam / N1, run=bad)

    @unittest.skipUnless(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0, "needs ffmpeg")
    def test_real_join_keeps_every_frame(self):
        for name, color in ((N1, "red"), (N2, "blue")):
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=%s:s=160x90:r=30:d=2" % color,
                            "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-g", "30", "-c:a", "aac", "-shortest",
                            str(self.cam / name)], check=True)
        out = join_chunks.join(self.cam, log=lambda m: None)
        probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                                "stream=nb_read_frames", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "120")


    @unittest.skipUnless(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0, "needs ffmpeg")
    def test_real_join_of_a_chunk_that_lost_its_audio(self):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=160x90:r=30:d=2", "-f", "lavfi",
                        "-i", "sine=d=2:r=48000", "-ac", "1", "-c:v", "libx264", "-g", "30", "-c:a", "aac", "-shortest",
                        str(self.cam / N1)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=160x90:r=30:d=2", "-an",
                        "-c:v", "libx264", "-g", "30", str(self.cam / N2)], check=True)
        out = join_chunks.join(self.cam, log=lambda m: None)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(probe), ["audio", "video"])
        frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                                 "stream=nb_read_frames", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
        self.assertEqual(frames.stdout.strip(), "120")


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cam = self.root / "2026-11-07" / "cam"
        self.cam.mkdir(parents=True)
        (self.cam / N1).write_bytes(b"x" * 10)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_ok(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def test_date_resolves_to_the_cam_folder_and_uses_gio_trash(self):
        retention.trash("2026-11-07", self.root, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [["gio", "trash", str(self.cam)]])

    def test_stream_folder_and_cam_folder_are_both_accepted(self):
        retention.trash(str(self.cam.parent), self.root, run=self.run_ok, log=lambda m: None)
        retention.trash(str(self.cam), self.root, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls[0], self.calls[1])

    def test_dry_run_touches_nothing(self):
        retention.trash("2026-11-07", self.root, dry_run=True, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [])

    def test_other_folders_are_refused(self):
        other = self.root / "2026-11-07"
        for target in (str(other / "stream-full.mp4"), str(self.root), "/", str(self.root / "cam")):
            with self.assertRaises(retention.RetentionError, msg=target):
                retention.trash(target, self.root, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [])

    def test_a_cam_folder_outside_the_out_root_is_refused(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            cam = Path(elsewhere) / "2026-11-07" / "cam"
            cam.mkdir(parents=True)
            with self.assertRaises(retention.RetentionError):
                retention.trash(str(cam), self.root, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [])

    def test_a_cam_folder_under_a_folder_with_another_name_is_refused(self):
        cam = self.root / "holiday-2026" / "cam"
        cam.mkdir(parents=True)
        with self.assertRaises(retention.RetentionError):
            retention.trash(str(cam), self.root, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [])

    def test_a_cam_folder_holding_files_phonecam_did_not_write_is_refused(self):
        (self.cam / "my-notes.txt").write_text("keep me")
        with self.assertRaises(retention.RetentionError) as ctx:
            retention.trash("2026-11-07", self.root, run=self.run_ok, log=lambda m: None)
        self.assertIn("my-notes.txt", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_a_subfolder_or_odd_file_in_broken_is_refused(self):
        (self.cam / "broken").mkdir()
        (self.cam / "broken" / N1).write_bytes(b"x")
        retention.trash("2026-11-07", self.root, run=self.run_ok, log=lambda m: None)   # allowed
        (self.cam / "broken" / "extra.mov").write_bytes(b"x")
        with self.assertRaises(retention.RetentionError):
            retention.trash("2026-11-07", self.root, run=self.run_ok, log=lambda m: None)

    def test_everything_phonecam_writes_is_allowed(self):
        for name in ("cam-joined.mp4", "sync.json", ".offload.lock", ".cam-joined.mp4.part",
                     "." + N1 + ".part.1a2b3c4d"):
            (self.cam / name).write_bytes(b"x")
        retention.trash("2026-11-07", self.root, run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [["gio", "trash", str(self.cam)]])

    def test_the_out_root_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {chunks.OUT_ROOT_ENV: str(self.root)}):
            retention.trash("2026-11-07", run=self.run_ok, log=lambda m: None)
        self.assertEqual(self.calls, [["gio", "trash", str(self.cam)]])

    def test_a_missing_recording_is_reported(self):
        with self.assertRaises(retention.RetentionError):
            retention.trash("2026-01-01", self.root, run=self.run_ok, log=lambda m: None)

    def test_symlinks_are_refused(self):
        real = self.root / "elsewhere"
        real.mkdir()
        link_parent = self.root / "2026-12-01"
        link_parent.mkdir()
        (link_parent / "cam").symlink_to(real)
        with self.assertRaises(retention.RetentionError):
            retention.trash("2026-12-01", self.root, run=self.run_ok, log=lambda m: None)

    def test_trash_failure_is_an_error(self):
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "Unable to trash")
        with self.assertRaises(retention.RetentionError):
            retention.trash("2026-11-07", self.root, run=fail, log=lambda m: None)
        self.assertTrue(self.cam.exists())


if __name__ == "__main__":
    unittest.main()
