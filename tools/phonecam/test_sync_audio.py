"""Tests for sync_audio.py on synthetic audio (needs numpy; the CLI test also needs ffmpeg)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import numpy as np
    import sync_audio as sa
except ImportError as exc:  # numpy lives in the user site; python3 -I cannot see it
    raise unittest.SkipTest("numpy is needed (%s); run without -I" % exc)

RATE = sa.RATE


def speech_like(seconds: float, seed: int) -> np.ndarray:
    """Noise bursts with syllable-like loudness changes and pauses, a crude stand-in for talking."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    out = np.zeros(n, dtype=np.float32)
    pos = 0
    while pos < n:
        length = int(rng.uniform(0.08, 0.6) * RATE)
        gain = rng.uniform(0.05, 0.9) if rng.random() > 0.3 else 0.0
        t = np.arange(min(length, n - pos))
        carrier = rng.standard_normal(len(t)) * np.sin(2 * np.pi * rng.uniform(2, 6) * t / RATE + 1.0) ** 2
        out[pos:pos + len(t)] = (gain * carrier).astype(np.float32)
        pos += length
    return out


def recording(source: np.ndarray, start_s: float, length_s: float, seed: int, noise: float = 0.05) -> np.ndarray:
    """What a second microphone hears of `source` from start_s on: the same voice plus its own noise."""
    a = int(start_s * RATE)
    clip = source[a:a + int(length_s * RATE)].copy()
    rng = np.random.default_rng(seed)
    return clip + noise * rng.standard_normal(len(clip)).astype(np.float32)


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.voice = speech_like(300, 1)

    def test_recovers_a_positive_offset_to_a_millisecond(self):
        # The stream starts 7.1234 s after the phone began recording: the phone clip is the longer one.
        cam = recording(self.voice, 0.0, 280, 2)
        stream = recording(self.voice, 7.1234, 280, 3)
        result = sa.estimate(cam, stream)
        self.assertAlmostEqual(result["offset_s"], -7.1234, delta=0.001)

    def test_recovers_a_negative_offset(self):
        cam = recording(self.voice, 12.5, 250, 2)
        stream = recording(self.voice, 0.0, 280, 3)
        result = sa.estimate(cam, stream)
        self.assertAlmostEqual(result["offset_s"], 12.5, delta=0.001)

    def test_sub_millisecond_precision(self):
        cam = recording(self.voice, 3.0, 250, 2)
        stream = recording(self.voice, 0.0, 280, 3)
        self.assertAlmostEqual(sa.estimate(cam, stream)["offset_s"], 3.0, delta=0.0005)

    def test_clock_drift_is_reported(self):
        cam = recording(self.voice, 5.0, 250, 2)
        stretched = np.interp(np.arange(0, len(self.voice) - 1, 1.0005), np.arange(len(self.voice)), self.voice)
        stream = recording(stretched.astype(np.float32), 0.0, 280, 3)
        result = sa.estimate(cam, stream)
        self.assertGreater(abs(result["drift_ppm"]), 100)

    def test_unrelated_recordings_are_refused(self):
        with self.assertRaises(sa.SyncError):
            sa.estimate(speech_like(120, 10), speech_like(120, 11))

    def test_too_short_audio_is_refused(self):
        with self.assertRaises(sa.SyncError):
            sa.envelope(np.zeros(100, dtype=np.float32))

    def test_pick_windows_do_not_overlap_and_stay_inside(self):
        starts = sa.pick_windows(self.voice, 9, 20.0)
        size = 20 * RATE
        self.assertEqual(len(starts), 9)
        self.assertTrue(all(b - a >= size for a, b in zip(starts, starts[1:])))
        self.assertLessEqual(starts[-1] + size, len(self.voice))


class PrimitiveTests(unittest.TestCase):
    def test_cross_correlation_peak_is_the_shift(self):
        rng = np.random.default_rng(0)
        a = rng.standard_normal(500)
        b = np.concatenate((np.zeros(37), a, np.zeros(10)))
        corr = sa.cross_correlate(a, b)
        self.assertEqual(int(np.argmax(corr)) - (len(a) - 1), 37)

    def test_gcc_phat_finds_a_fractional_delay(self):
        rng = np.random.default_rng(1)
        ref = rng.standard_normal(RATE * 2)
        delay = 12.4  # samples
        positions = np.arange(len(ref)) - delay
        sig = np.interp(positions, np.arange(len(ref)), ref)
        self.assertAlmostEqual(sa.gcc_phat(ref, sig, RATE, 0.1) * RATE, delay, delta=0.5)  # half a sample is 62 us


@unittest.skipUnless(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0, "needs ffmpeg")
class CliTests(unittest.TestCase):
    def write_wav(self, path: Path, audio: np.ndarray):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())

    def test_cli_writes_sync_json_next_to_the_recording(self):
        voice = speech_like(200, 5)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "2026-11-07"
            (folder / "cam").mkdir(parents=True)
            cam_file = folder / "cam" / "cam-joined.mp4"
            self.write_wav(folder / "cam" / "c.wav", recording(voice, 2.0, 180, 6))
            self.write_wav(folder / "s.wav", recording(voice, 0.0, 200, 7))
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(folder / "cam" / "c.wav"), "-c:a", "aac", "-f", "mp4",
                            str(cam_file)], check=True)
            (folder / "cam" / "c.wav").unlink()
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(folder / "s.wav"), "-c:a", "aac", "-f", "mp4",
                            str(folder / "stream-full.mp4")], check=True)
            self.assertEqual(sa.main([str(folder)]), 0)
            data = json.loads((folder / "cam" / "sync.json").read_text())
        self.assertAlmostEqual(data["offset_s"], 2.0, delta=0.005)
        self.assertIn("stream_time = cam_time + offset_s", data["meaning"])

    def test_missing_file_gives_a_clear_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sa.main(["--cam", str(Path(tmp) / "nope.mp4"), "--stream", str(Path(tmp) / "nope2.mp4")]), 1)


if __name__ == "__main__":
    unittest.main()
