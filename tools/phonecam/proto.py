"""proto.py: the live socket wire format, mirrored from android/.../Protocol.kt.

Phone to PC: one Hello (24 bytes), then packets of a 24 byte header plus payload.
PC to phone: 12 byte commands (4 ASCII letters and a big-endian long).
All integers are big-endian. Times are microseconds on the phone's elapsed-realtime clock.
"""
import struct
import time
from typing import NamedTuple

MAGIC = b"PCM1"
HELLO_SIZE = 24
HEADER_SIZE = 24
COMMAND_SIZE = 12
MAX_PAYLOAD = 8 * 1024 * 1024

TYPE_CONFIG = 1
TYPE_FRAME = 2
TYPE_PONG = 3
TYPE_MARK = 4
FLAG_KEY = 1

CODEC_H264 = 1
CODEC_HEVC = 2
CODEC_NAMES = {CODEC_H264: "h264", CODEC_HEVC: "hevc"}

_HELLO = struct.Struct(">4sBBHHHIq")
_HEADER = struct.Struct(">BBHIqq")
_COMMAND = struct.Struct(">4sq")


class ProtocolError(Exception):
    """The peer sent bytes that do not follow the protocol. The message is safe to log."""


class Hello(NamedTuple):
    codec: int
    fps: int
    width: int
    height: int
    bitrate: int
    phone_clock_us: int


class Header(NamedTuple):
    type: int
    flags: int
    length: int
    pts_us: int
    send_us: int

    @property
    def is_key(self) -> bool:
        return bool(self.flags & FLAG_KEY)


def decode_hello(data: bytes) -> Hello:
    if len(data) != HELLO_SIZE:
        raise ProtocolError("hello must be %d bytes, got %d" % (HELLO_SIZE, len(data)))
    magic, codec, _, fps, width, height, bitrate, clock = _HELLO.unpack(data)
    if magic != MAGIC:
        raise ProtocolError("bad hello magic %r" % magic)
    if codec not in CODEC_NAMES:
        raise ProtocolError("unknown codec id %d" % codec)
    return Hello(codec, fps, width, height, bitrate, clock)


def encode_hello(hello: Hello) -> bytes:
    return _HELLO.pack(MAGIC, hello.codec, 0, hello.fps, hello.width, hello.height, hello.bitrate,
                       hello.phone_clock_us)


def decode_header(data: bytes) -> Header:
    if len(data) != HEADER_SIZE:
        raise ProtocolError("header must be %d bytes, got %d" % (HEADER_SIZE, len(data)))
    kind, flags, _, length, pts, send = _HEADER.unpack(data)
    if length > MAX_PAYLOAD:
        raise ProtocolError("payload length out of range: %d" % length)
    return Header(kind, flags, length, pts, send)


def encode_packet(kind: int, flags: int, pts_us: int, send_us: int, payload: bytes = b"") -> bytes:
    return _HEADER.pack(kind, flags, 0, len(payload), pts_us, send_us) + payload


def encode_command(name: str, arg: int = 0) -> bytes:
    return _COMMAND.pack(name.encode("ascii"), arg)


def pc_clock_us() -> int:
    """The PC clock used for all latency maths (CLOCK_MONOTONIC, microseconds)."""
    return time.monotonic_ns() // 1000
