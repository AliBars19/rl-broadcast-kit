"""live_client.py: connect to the phone's live socket, read packets, keep a phone-to-PC clock offset."""
import socket
import time
from collections import deque
from typing import Iterator, NamedTuple

import proto

PING_EVERY_S = 2.0
SYNC_WINDOW = 8


class Packet(NamedTuple):
    header: proto.Header
    payload: bytes
    recv_us: int


class PacketReader:
    """Buffered exact reads from a stream socket, with a receive timestamp per packet."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = bytearray()

    def _fill(self, count: int) -> None:
        """Buffer at least `count` bytes without consuming them. A socket timeout leaves the buffer intact,
        so the same read can simply be tried again."""
        while len(self._buf) < count:
            chunk = self.sock.recv(max(65536, count - len(self._buf)))
            if not chunk:
                raise ConnectionError("phone closed the live socket")
            self._buf.extend(chunk)

    def read_exact(self, count: int) -> bytes:
        self._fill(count)
        data = bytes(self._buf[:count])
        del self._buf[:count]
        return data

    def read_hello(self) -> proto.Hello:
        return proto.decode_hello(self.read_exact(proto.HELLO_SIZE))

    def read_packet(self) -> Packet:
        """The next whole packet. Raises TimeoutError (nothing lost) when the socket has a timeout and
        no complete packet arrived in time."""
        self._fill(proto.HEADER_SIZE)
        header = proto.decode_header(bytes(self._buf[:proto.HEADER_SIZE]))
        end = proto.HEADER_SIZE + header.length
        self._fill(end)
        payload = bytes(self._buf[proto.HEADER_SIZE:end])
        del self._buf[:end]
        return Packet(header, payload, proto.pc_clock_us())

    def packets(self) -> Iterator[Packet]:
        while True:
            yield self.read_packet()


class ClockSync:
    """Estimates (phone clock minus PC clock) from PING/PONG round trips, NTP style.

    The sample with the smallest round trip is the most trustworthy, so the offset comes from
    the best of the last few samples. The error is at most half that round trip.
    """

    def __init__(self):
        self._samples: deque[tuple[int, int]] = deque(maxlen=SYNC_WINDOW)  # (rtt_us, offset_us)
        self._last_ping = 0.0

    def ping_due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return now - self._last_ping >= PING_EVERY_S

    def make_ping(self) -> bytes:
        self._last_ping = time.monotonic()
        return proto.encode_command("PING", proto.pc_clock_us())

    def on_pong(self, packet: Packet) -> None:
        t1, t2, t3 = packet.header.pts_us, packet.header.send_us, packet.recv_us
        rtt = t3 - t1
        if rtt < 0:
            return
        self._samples.append((rtt, t2 - (t1 + t3) // 2))

    @property
    def ready(self) -> bool:
        return bool(self._samples)

    @property
    def offset_us(self) -> int:
        return min(self._samples)[1] if self._samples else 0

    @property
    def rtt_us(self) -> int:
        return min(self._samples)[0] if self._samples else 0

    def phone_to_pc_us(self, phone_us: int) -> int:
        return phone_us - self.offset_us


def connect(host: str, port: int, timeout_s: float, log=lambda msg: None) -> socket.socket:
    """Connect with retries until the deadline (the app takes a moment to open its port)."""
    deadline = time.monotonic() + timeout_s
    last_error = None
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=3)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise ConnectionError("cannot reach the phone at %s:%d (%s)" % (host, port, last_error))
            log("waiting for the phone's live socket (%s)" % exc)
            time.sleep(0.5)
