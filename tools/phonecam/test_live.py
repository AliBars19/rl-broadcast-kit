"""Tests for proto.py, live_client.py and live_relay.py (run with python3 -m unittest)."""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_client  # noqa: E402
import live_relay  # noqa: E402
import proto  # noqa: E402

HELLO = proto.Hello(proto.CODEC_HEVC, 60, 1920, 1080, 20_000_000, 123_456_789_012)


class ProtoTests(unittest.TestCase):
    def test_hello_matches_the_android_layout(self):
        # Same bytes the Kotlin ProtocolTest checks: magic, codec, 0, fps, width, height.
        data = proto.encode_hello(HELLO)
        self.assertEqual(data[:8], b"PCM1\x02\x00\x00\x3c")
        self.assertEqual(data[8:12], bytes([0x07, 0x80, 0x04, 0x38]))
        self.assertEqual(proto.decode_hello(data), HELLO)

    def test_bad_magic_is_rejected(self):
        with self.assertRaises(proto.ProtocolError):
            proto.decode_hello(b"XXXX" + proto.encode_hello(HELLO)[4:])

    def test_unknown_codec_is_rejected(self):
        data = bytearray(proto.encode_hello(HELLO))
        data[4] = 9
        with self.assertRaises(proto.ProtocolError):
            proto.decode_hello(bytes(data))

    def test_packet_header_round_trip(self):
        data = proto.encode_packet(proto.TYPE_FRAME, proto.FLAG_KEY, 42, 99, b"abcde")
        header = proto.decode_header(data[:proto.HEADER_SIZE])
        self.assertEqual((header.type, header.length, header.pts_us, header.send_us), (2, 5, 42, 99))
        self.assertTrue(header.is_key)
        self.assertEqual(data[proto.HEADER_SIZE:], b"abcde")

    def test_oversized_payload_is_rejected(self):
        data = bytearray(proto.encode_packet(proto.TYPE_FRAME, 0, 1, 2))
        data[4] = 0x7F
        with self.assertRaises(proto.ProtocolError):
            proto.decode_header(bytes(data))

    def test_command_layout(self):
        self.assertEqual(proto.encode_command("PING", 1), b"PING" + (1).to_bytes(8, "big"))
        self.assertEqual(len(proto.encode_command("KEYF")), proto.COMMAND_SIZE)


class ClockSyncTests(unittest.TestCase):
    def pong(self, t1, t2, t3):
        return live_client.Packet(proto.Header(proto.TYPE_PONG, 0, 0, t1, t2), b"", t3)

    def test_offset_is_the_phone_minus_pc_difference(self):
        sync = live_client.ClockSync()
        # PC clock 1000 when the ping leaves, the phone is 5_000_000 ahead, 2 ms round trip.
        sync.on_pong(self.pong(1000, 1000 + 1000 + 5_000_000, 3000))
        self.assertEqual(sync.offset_us, 5_000_000)
        self.assertEqual(sync.rtt_us, 2000)
        self.assertEqual(sync.phone_to_pc_us(7_000_000), 2_000_000)

    def test_smallest_round_trip_wins(self):
        sync = live_client.ClockSync()
        sync.on_pong(self.pong(0, 50_000 + 100, 100_000))        # slow, offset error
        sync.on_pong(self.pong(200_000, 200_500 + 100, 201_000))  # fast, true offset 100
        self.assertEqual(sync.offset_us, 100)

    def test_negative_round_trip_is_ignored(self):
        sync = live_client.ClockSync()
        sync.on_pong(self.pong(5000, 0, 1000))
        self.assertFalse(sync.ready)


class HelperTests(unittest.TestCase):
    def test_parameter_set_detection(self):
        self.assertTrue(live_relay.has_parameter_sets("h264", b"\x00\x00\x00\x01\x67abc"))
        self.assertFalse(live_relay.has_parameter_sets("h264", b"\x00\x00\x00\x01\x65abc"))
        self.assertTrue(live_relay.has_parameter_sets("hevc", b"\x00\x00\x00\x01\x40\x01abc"))
        self.assertFalse(live_relay.has_parameter_sets("hevc", b"\x00\x00\x00\x01\x26\x01abc"))

    def test_percentile(self):
        self.assertEqual(live_relay.percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(live_relay.percentile([], 0.5), 0.0)

    def test_stats_window_is_bounded(self):
        stats = live_relay.Stats()
        for i in range(live_relay.STATS_WINDOW + 50):
            stats.add(1.0, 2.0)
        self.assertEqual(stats.summary()["total"]["p50"], 3.0)
        self.assertEqual(len(stats._data["total"]), live_relay.STATS_WINDOW)


class FakePhone(threading.Thread):
    """Serves a Hello, a config packet and frames, and answers PING with PONG like the app."""

    def __init__(self, frames, phone_offset_us=5_000_000):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.frames = frames
        self.offset = phone_offset_us
        self.commands = []

    def run(self):
        conn, _ = self.server.accept()
        conn.sendall(proto.encode_hello(HELLO))
        conn.sendall(proto.encode_packet(proto.TYPE_CONFIG, 0, 0, 0, b"\x00\x00\x00\x01\x40\x01CSD"))
        ping = conn.recv(proto.COMMAND_SIZE)
        self.commands.append(ping[:4])
        t1 = int.from_bytes(ping[4:], "big", signed=True)
        conn.sendall(proto.encode_packet(proto.TYPE_PONG, 0, t1, proto.pc_clock_us() + self.offset))
        base = proto.pc_clock_us() + self.offset
        for index, (data, key) in enumerate(self.frames):
            pts = base + index * 16_667
            conn.sendall(proto.encode_packet(proto.TYPE_FRAME, proto.FLAG_KEY if key else 0, pts, pts + 3000, data))
        conn.close()
        self.server.close()


class RelayTests(unittest.TestCase):
    def run_relay(self, frames):
        phone = FakePhone(frames)
        phone.start()
        udp_in = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_in.bind(("127.0.0.1", 0))
        udp_in.settimeout(0.5)
        udp_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock = live_client.connect("127.0.0.1", phone.port, 3)
        relay = live_relay.Relay(sock, udp_out, udp_in.getsockname(), None)
        with self.assertRaises(ConnectionError):
            relay.run()  # the fake phone hangs up after the last frame
        datagrams = []
        sock.close()
        try:
            while True:
                datagrams.append(udp_in.recvfrom(4096)[0])
        except socket.timeout:
            pass
        udp_in.close()
        udp_out.close()
        return relay, datagrams, phone

    def test_frames_become_ts_datagrams(self):
        frames = [(b"\x00\x00\x00\x01\x65" + b"k" * 3000, True)] + [
            (b"\x00\x00\x00\x01\x41" + b"p" * 500, False) for _ in range(9)]
        relay, datagrams, phone = self.run_relay(frames)
        self.assertEqual(relay.codec, "hevc")
        self.assertTrue(all(len(d) % 188 == 0 and len(d) <= 1316 for d in datagrams))
        self.assertTrue(all(d[0] == 0x47 for d in datagrams))
        self.assertEqual(relay.stats.frames, 10)
        self.assertEqual(phone.commands[0], b"PING")

    def test_keyframe_without_parameter_sets_gets_the_config(self):
        relay, datagrams, _ = self.run_relay([(b"\x00\x00\x00\x01\x26\x01IDR", True)])
        self.assertIn(b"CSD", b"".join(datagrams))

    def test_delta_frames_get_the_config_too_so_late_joiners_can_probe(self):
        relay, datagrams, _ = self.run_relay([(b"\x00\x00\x00\x01\x02\x01P", False)])
        self.assertIn(b"CSD", b"".join(datagrams))

    def test_latency_stats_reflect_the_phone_timestamps(self):
        relay, _, _ = self.run_relay([(b"\x00\x00\x00\x01\x26\x01IDR", True)] * 5)
        summary = relay.stats.summary()
        self.assertAlmostEqual(summary["encode"]["p50"], 3.0, places=1)
        self.assertGreaterEqual(summary["transport"]["max"], -5.0)


class StallPhone(threading.Thread):
    """Says hello, sends a few frames, then goes quiet without closing the socket."""

    def __init__(self):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.release = threading.Event()

    def run(self):
        conn, _ = self.server.accept()
        conn.sendall(proto.encode_hello(HELLO))
        for i in range(3):
            conn.sendall(proto.encode_packet(proto.TYPE_FRAME, proto.FLAG_KEY, i, i, b"\x00\x00\x00\x01\x65x"))
        self.release.wait(10)
        conn.close()


class WatchdogTests(unittest.TestCase):
    def test_a_silent_phone_ends_the_relay_with_a_connection_error(self):
        phone = StallPhone()
        phone.start()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock = live_client.connect("127.0.0.1", phone.port, 3)
        relay = live_relay.Relay(sock, udp, ("127.0.0.1", 9), None, stall_s=0.8)
        begin = time.monotonic()
        with self.assertRaises(ConnectionError) as ctx:
            relay.run()
        self.assertIn("no frame", str(ctx.exception))
        self.assertLess(time.monotonic() - begin, 3)
        self.assertEqual(relay.stats.frames, 0)  # no clock sync from this fake, frames still counted below
        phone.release.set()
        sock.close()
        udp.close()

    def test_a_timeout_in_the_middle_of_a_packet_loses_nothing(self):
        a, b = socket.socketpair()
        a.settimeout(0.1)
        reader = live_client.PacketReader(a)
        packet = proto.encode_packet(proto.TYPE_FRAME, 0, 5, 6, b"abcdef")
        b.sendall(packet[:27])  # whole header, part of the payload
        with self.assertRaises(TimeoutError):
            reader.read_packet()
        b.sendall(packet[27:])
        got = reader.read_packet()
        self.assertEqual((got.header.pts_us, got.payload), (5, b"abcdef"))
        a.close()
        b.close()


class ConnectTests(unittest.TestCase):
    def test_connect_gives_up_after_the_deadline(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with self.assertRaises(ConnectionError):
            live_client.connect("127.0.0.1", port, 0.6)


if __name__ == "__main__":
    unittest.main()
