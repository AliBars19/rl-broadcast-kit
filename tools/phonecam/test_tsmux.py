"""Tests for tsmux.py (run with python3 -m unittest in tools/phonecam; needs ffmpeg and ffprobe)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tsmux  # noqa: E402


def make_units(codec: str, count: int = 30):
    """Encode a test pattern and split it into (access unit without AUD, is_key) pairs."""
    encoder = ["-c:v", "libx264", "-x264-params", "aud=1:keyint=10:min-keyint=10:bframes=0:scenecut=0"]
    fmt = "h264"
    if codec == "hevc":
        encoder = ["-c:v", "libx265", "-x265-params", "aud=1:keyint=10:min-keyint=10:bframes=0:log-level=none"]
        fmt = "hevc"
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=60", "-frames:v", str(count),
         *encoder, "-f", fmt, "-"], capture_output=True, check=True).stdout
    return split_units(raw, codec)


def nal_type(codec: str, first_byte: int) -> int:
    return first_byte & 0x1F if codec == "h264" else (first_byte >> 1) & 0x3F


def split_units(raw: bytes, codec: str):
    aud_type = 9 if codec == "h264" else 35
    idr = (5,) if codec == "h264" else (19, 20)
    starts = []
    pos = raw.find(b"\x00\x00\x01")
    while pos != -1:
        starts.append(pos)
        pos = raw.find(b"\x00\x00\x01", pos + 3)
    units, current = [], None
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(raw)
        nal = raw[start:end].rstrip(b"\x00") if end == len(raw) else raw[start:end]
        if nal_type(codec, raw[start + 3]) == aud_type:
            if current is not None:
                units.append(current)
            current = bytearray()
            continue
        current.extend(b"\x00" + nal if not nal.startswith(b"\x00\x00\x00") else nal)
    units.append(current)
    return [(bytes(u), any(nal_type(codec, u[i + 3]) in idr for i in range(len(u) - 4) if u[i:i + 3] == b"\x00\x00\x01"))
            for u in units]


class PrimitiveTests(unittest.TestCase):
    def test_crc32_mpeg_check_value(self):
        self.assertEqual(tsmux.crc32_mpeg(b"123456789"), 0x0376E6E7)

    def test_pts_encoding_of_zero(self):
        self.assertEqual(tsmux.encode_pts(0), bytes([0x21, 0x00, 0x01, 0x00, 0x01]))

    def test_pts_encoding_decodes_back(self):
        for pts in (1, 90000, 123456789, (1 << 33) - 1):
            b = tsmux.encode_pts(pts)
            value = ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
            self.assertEqual(value, pts)

    def test_pcr_has_reserved_bits_set(self):
        self.assertEqual(tsmux.encode_pcr(1)[4] & 0x7E, 0x7E)

    def test_pat_and_pmt_end_in_a_valid_crc(self):
        for section in (tsmux.build_pat(), tsmux.build_pmt("hevc")):
            self.assertEqual(tsmux.crc32_mpeg(section), 0)

    def test_pmt_announces_the_codec(self):
        self.assertEqual(tsmux.build_pmt("h264")[12], 0x1B)
        self.assertEqual(tsmux.build_pmt("hevc")[12], 0x24)

    def test_pes_length_is_exact_when_it_fits(self):
        pes = tsmux.build_pes(0, b"x" * 100)
        self.assertEqual(int.from_bytes(pes[4:6], "big"), 108)
        self.assertEqual(len(pes), 6 + 108)

    def test_pes_length_is_zero_when_too_big(self):
        self.assertEqual(tsmux.build_pes(0, b"x" * 70000)[4:6], b"\x00\x00")

    def test_unknown_codec_is_rejected(self):
        with self.assertRaises(ValueError):
            tsmux.TsMuxer("vp9")


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.mux = tsmux.TsMuxer("h264")

    def packets(self, data):
        self.assertEqual(len(data) % 188, 0)
        return [data[i:i + 188] for i in range(0, len(data), 188)]

    def test_every_packet_is_188_bytes_with_sync(self):
        for size in (1, 100, 175, 176, 177, 183, 184, 185, 500, 5000):
            data = self.mux.mux(1000, b"\x00\x00\x00\x01\x65" + b"a" * size, True)
            for packet in self.packets(data):
                self.assertEqual(packet[0], 0x47)

    def test_continuity_counters_increment_per_pid(self):
        counters = {}
        for n in range(20):
            for p in self.packets(self.mux.mux(1000 * n, b"\x00\x00\x00\x01\x41" + b"b" * 3000, False)):
                pid = (p[1] & 0x1F) << 8 | p[2]
                cc = p[3] & 0x0F
                if pid in counters:
                    self.assertEqual(cc, (counters[pid] + 1) & 0x0F, "pid %x" % pid)
                counters[pid] = cc

    def test_keyframe_starts_with_pat_and_pmt(self):
        data = self.mux.mux(0, b"\x00\x00\x00\x01\x65aaa", True)
        pids = [(p[1] & 0x1F) << 8 | p[2] for p in self.packets(data)]
        self.assertEqual(pids[:3], [tsmux.PAT_PID, tsmux.PMT_PID, tsmux.VIDEO_PID])

    def test_first_video_packet_carries_pcr_and_random_access(self):
        data = self.mux.mux(90000, b"\x00\x00\x00\x01\x65aaa", True)
        video = self.packets(data)[2]
        self.assertEqual(video[1] & 0x40, 0x40)
        self.assertEqual(video[5] & 0x50, 0x50)

    def test_payload_ends_with_an_aud(self):
        data = self.mux.mux(0, b"\x00\x00\x00\x01\x41zz", False)
        self.assertTrue(data.rstrip(b"\xff").endswith(tsmux.AUD["h264"]))


@unittest.skipUnless(subprocess.run(["which", "ffprobe"], capture_output=True).returncode == 0, "needs ffprobe")
class FfmpegRoundTripTests(unittest.TestCase):
    def roundtrip(self, codec):
        units = make_units(codec)
        mux = tsmux.TsMuxer(codec)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.ts")
            with open(path, "wb") as handle:
                for index, (unit, key) in enumerate(units):
                    handle.write(mux.mux(90000 + index * 1500, unit, key))
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v", "-show_entries",
                 "stream=codec_name,nb_read_frames", "-show_entries", "packet=pts", "-of", "json", path],
                capture_output=True, check=True, text=True)
            decode = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                                    capture_output=True, text=True)
        return units, json.loads(probe.stdout), decode

    def check(self, codec):
        units, info, decode = self.roundtrip(codec)
        self.assertEqual(len(units), 30)
        self.assertEqual(info["streams"][0]["codec_name"], codec)
        self.assertEqual(info["streams"][0]["nb_read_frames"], "30")
        # The trailing AUD makes ffmpeg label each frame with the previous PES time, so the second
        # packet has no pts and the last packet is the bare AUD. The spacing of the rest is exact.
        pts = [int(p["pts"]) for p in info["packets"] if "pts" in p]
        self.assertGreaterEqual(len(pts), 28)
        self.assertEqual({b - a for a, b in zip(pts, pts[1:])}, {1500})
        known = ("missing picture in access unit", "non monotonically increasing dts", "no frame!",
                 "Decoding error")  # all caused by the bare AUD left at the end of the file
        unexpected = [l for l in decode.stderr.splitlines() if l and not any(k in l for k in known)]
        self.assertEqual(unexpected, [])

    def test_h264_decodes_with_exact_pts(self):
        self.check("h264")

    def test_hevc_decodes_with_exact_pts(self):
        self.check("hevc")


if __name__ == "__main__":
    unittest.main()
