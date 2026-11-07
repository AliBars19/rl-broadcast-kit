"""tsmux.py: a minimal MPEG-TS muxer for one video stream (H.264 or HEVC), built for low delay.

Each access unit becomes one PES packet carrying the capture time as PTS, with a PCR on its
first TS packet. Every unit ends with an access unit delimiter NAL so that ffmpeg's parser knows
the frame is complete the moment its last byte arrives, instead of waiting for the next frame
(which would add one frame of delay).
"""
from typing import Iterator

TS_SIZE = 188
SYNC = 0x47
PAT_PID = 0
PMT_PID = 0x1000
VIDEO_PID = 0x100
STREAM_TYPES = {"h264": 0x1B, "hevc": 0x24}
AUD = {"h264": b"\x00\x00\x00\x01\x09\xf0", "hevc": b"\x00\x00\x00\x01\x46\x01\x50"}
PCR_LEAD_90K = 5400   # PCR runs 60 ms ahead of the PTS it accompanies
PSI_EVERY_FRAMES = 1  # PAT and PMT before every frame (about 1 percent): a reader that joins mid-stream probes only 32 KB
PTS_MASK = (1 << 33) - 1


def crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _section(table_id: int, body: bytes) -> bytes:
    length = len(body) + 4  # body plus CRC
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF])
    data = head + body
    return data + crc32_mpeg(data).to_bytes(4, "big")


def build_pat() -> bytes:
    body = bytes([0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF])
    return _section(0x00, body)


def build_pmt(codec: str) -> bytes:
    body = bytes([
        0x00, 0x01, 0xC1, 0x00, 0x00,
        0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF,  # PCR PID
        0xF0, 0x00,                                  # no program info
        STREAM_TYPES[codec], 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00,
    ])
    return _section(0x02, body)


def encode_pts(pts: int) -> bytes:
    pts &= PTS_MASK
    return bytes([
        0x21 | ((pts >> 29) & 0x0E),
        (pts >> 22) & 0xFF,
        0x01 | ((pts >> 14) & 0xFE),
        (pts >> 7) & 0xFF,
        0x01 | ((pts << 1) & 0xFE),
    ])


def encode_pcr(base: int) -> bytes:
    base &= PTS_MASK
    value = (base << 15) | (0x3F << 9)
    return value.to_bytes(6, "big")


def build_pes(pts: int, payload: bytes) -> bytes:
    inner = 3 + 5 + len(payload)
    length = inner if inner <= 0xFFFF else 0  # 0 means unbounded, allowed for video
    return b"\x00\x00\x01\xe0" + length.to_bytes(2, "big") + b"\x84\x80\x05" + encode_pts(pts) + payload


def _assemble(head: bytes, cc: int, af_flags: int, pcr: bytes | None, payload: bytes) -> bytes:
    room = TS_SIZE - 4
    af_core = (bytes([af_flags]) + (pcr or b"")) if af_flags else b""
    free = room - len(payload)  # bytes the adaptation field (with its length byte) must fill
    if free == 0 and not af_core:
        return head + bytes([0x10 | cc]) + payload
    if free == 1 and not af_core:
        return head + bytes([0x30 | cc, 0x00]) + payload
    if not af_core:
        af_core = b"\x00"
    stuffing = free - 1 - len(af_core)
    if stuffing < 0:
        raise ValueError("payload does not fit next to the adaptation field")
    return head + bytes([0x30 | cc, free - 1]) + af_core + b"\xff" * stuffing + payload


class TsMuxer:
    def __init__(self, codec: str, trailing_aud: bool = True):
        if codec not in STREAM_TYPES:
            raise ValueError("unknown codec %r" % codec)
        self.codec = codec
        self.trailing_aud = trailing_aud
        self._cc = {PAT_PID: 0, PMT_PID: 0, VIDEO_PID: 0}
        self._pat = build_pat()
        self._pmt = build_pmt(codec)
        self._frames = 0

    def _next_cc(self, pid: int) -> int:
        cc = self._cc[pid]
        self._cc[pid] = (cc + 1) & 0x0F
        return cc

    def _psi(self, pid: int, section: bytes) -> bytes:
        payload = (b"\x00" + section).ljust(TS_SIZE - 4, b"\xff")
        return _assemble(bytes([SYNC, 0x40 | (pid >> 8), pid & 0xFF]), self._next_cc(pid), 0, None, payload)

    def mux(self, pts_90k: int, access_unit: bytes, key: bool) -> bytes:
        """One access unit (Annex B) in, a whole number of 188 byte TS packets out."""
        out = []
        if key or self._frames % PSI_EVERY_FRAMES == 0:
            out.append(self._psi(PAT_PID, self._pat))
            out.append(self._psi(PMT_PID, self._pmt))
        self._frames += 1
        body = access_unit + AUD[self.codec] if self.trailing_aud else AUD[self.codec] + access_unit
        pes = build_pes(pts_90k, body)
        out.extend(self._video_packets(pes, pts_90k, key))
        return b"".join(out)

    def _video_packets(self, pes: bytes, pts_90k: int, key: bool) -> Iterator[bytes]:
        offset = 0
        first = True
        while offset < len(pes):
            pcr = encode_pcr(pts_90k - PCR_LEAD_90K) if first else None
            af_flags = ((0x40 if key else 0) | 0x10) if first else 0
            overhead = 4 + (2 + 6 if first else 0)
            take = min(len(pes) - offset, TS_SIZE - overhead)
            head = bytes([SYNC, (0x40 if first else 0) | (VIDEO_PID >> 8), VIDEO_PID & 0xFF])
            yield _assemble(head, self._next_cc(VIDEO_PID), af_flags, pcr, pes[offset:offset + take])
            offset += take
            first = False
