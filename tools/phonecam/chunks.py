"""chunks.py: names of the phone's chunk files and where they go on the PC."""
import os
import re
from pathlib import Path
from typing import NamedTuple

PACKAGE = "io.github.rlbroadcastkit.phonecam"
REMOTE_DIR = "/sdcard/Android/data/%s/files/chunks" % PACKAGE
CHUNK_RE = re.compile(r"^cam-(\d{4})(\d{2})(\d{2})-(\d{6})-(\d{4})-(\d+)\.mp4(?:(\.broken|\.orphan)(?:\.\d+)?)?$")
SESSION_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{6})$")
OUT_ROOT_ENV = "PHONECAM_OUT_ROOT"
DEFAULT_OUT_ROOT = os.path.join("~", "Videos", "phonecam")
DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BROKEN_DIR = "broken"


class ChunkName(NamedTuple):
    session: str
    date: str       # YYYY-MM-DD of the session start (the stream's date)
    index: int
    epoch_ms: int   # wall clock of the chunk's first frame
    kind: str = "ok"  # ok | broken (the muxer failed) | orphan (left unfinished by a killed run)


def parse_chunk_name(name: str) -> ChunkName | None:
    match = CHUNK_RE.match(name)
    if not match:
        return None
    year, month, day, clock, index, epoch, suffix = match.groups()
    kind = suffix[1:] if suffix else "ok"
    return ChunkName("%s%s%s-%s" % (year, month, day, clock), "%s-%s-%s" % (year, month, day), int(index), int(epoch), kind)


def out_root() -> Path:
    """Where recordings go on the PC: $PHONECAM_OUT_ROOT, else ~/Videos/phonecam."""
    return Path(os.environ.get(OUT_ROOT_ENV) or DEFAULT_OUT_ROOT).expanduser()


def stream_dir(root: Path, date: str) -> Path:
    """The per-stream folder (it can also hold your stream-full.mp4): <root>/YYYY-MM-DD."""
    return root / date


def cam_dir(root: Path, date: str) -> Path:
    return stream_dir(root, date) / "cam"


def dest_dir(root: Path, parsed: ChunkName) -> Path:
    """Good chunks go in cam/, broken and orphaned ones in cam/broken/ (never joined, never lost)."""
    folder = cam_dir(root, parsed.date)
    return folder if parsed.kind == "ok" else folder / BROKEN_DIR


def sort_key(path: Path):
    """Order chunk files by the wall clock of their first frame (robust to a restart that renumbers)."""
    parsed = parse_chunk_name(path.name)
    return (parsed.epoch_ms, parsed.index) if parsed else (0, 0)


def list_local_chunks(directory: Path) -> list:
    return sorted((p for p in directory.glob("cam-*.mp4") if parse_chunk_name(p.name)), key=sort_key)
