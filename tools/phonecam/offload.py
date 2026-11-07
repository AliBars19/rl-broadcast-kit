"""offload.py: move finished chunks from the phone to the PC, verified before anything is deleted.

For each finished chunk (NAME.mp4, never NAME.mp4.part) the phone computes sha256 and size, the
PC streams the file over `adb exec-out cat` into a hidden .part file while hashing it on the fly
(so no second read of the file), and only when size and sha256 both match is the file renamed
into place and then deleted from the phone. One chunk at a time, throttled, run at idle priority.
"""
import fcntl
import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import NamedTuple

import chunks
from adbphone import AdbError

BLOCK = 1 << 20
DEFAULT_LIMIT_MB_S = 40.0


class Result(NamedTuple):
    name: str
    status: str   # done | already-there | mismatch | conflict | unknown-name | error
    detail: str = ""


def stream_to_file(stream, dest_path: Path, limit_bytes_s: float, clock=time.monotonic, sleep=time.sleep):
    """Copy `stream` to `dest_path`, return (size, sha256 hex). Sleeps to stay under the rate limit."""
    digest = hashlib.sha256()
    size = 0
    start = clock()
    with open(dest_path, "xb") as out:  # exclusive: never write into a file another writer has open
        while True:
            block = stream.read(BLOCK)
            if not block:
                break
            out.write(block)
            digest.update(block)
            size += len(block)
            if limit_bytes_s > 0:
                ahead = size / limit_bytes_s - (clock() - start)
                if ahead > 0:
                    sleep(ahead)
        out.flush()
        os.fsync(out.fileno())
    return size, digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


LOCK_NAME = ".offload.lock"


class _FolderLock:
    """flock on a file in the destination folder: the daemon's sweep and `phone-cam offload` never work on the
    same folder at once (the loser skips; the winner deletes the phone copy only after its own verification)."""

    def __init__(self, path: Path):
        self.path = path
        self._fd = None

    def try_acquire(self) -> bool:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the flock
            self._fd = None


def offload_chunk(phone, remote, root: Path, limit_mb_s: float, log, clock=time.monotonic, sleep=time.sleep) -> Result:
    parsed = chunks.parse_chunk_name(remote.name)
    if parsed is None:
        return Result(remote.name, "unknown-name", "left on the phone")
    dest_dir = chunks.dest_dir(root, parsed)
    lock = _FolderLock(dest_dir / LOCK_NAME)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if not lock.try_acquire():
            return Result(remote.name, "busy", "another offloader is working in this folder")
    except OSError as exc:
        return Result(remote.name, "error", "cannot prepare %s: %s" % (dest_dir, exc))
    try:
        return _offload_locked(phone, remote, dest_dir, limit_mb_s, log, clock, sleep)
    except OSError as exc:
        return Result(remote.name, "error", "PC file error: %s" % exc)
    finally:
        lock.release()


def _offload_locked(phone, remote, dest_dir: Path, limit_mb_s: float, log, clock, sleep) -> Result:
    final = dest_dir / remote.name
    if not phone.exists(remote.path):
        return Result(remote.name, "gone", "already moved by another offloader")
    expected = phone.sha256(remote.path)
    if final.exists():
        return _already_there(phone, remote, final, expected, log)
    for stale in dest_dir.glob("." + remote.name + ".part*"):
        stale.unlink(missing_ok=True)  # left by an offloader that was killed; we hold the lock, so it is dead
    partial = dest_dir / ("." + remote.name + ".part." + uuid.uuid4().hex[:8])
    try:
        with phone.open_stream(remote.path) as stream:
            size, actual = stream_to_file(stream, partial, limit_mb_s * 1e6, clock, sleep)
    except (AdbError, OSError) as exc:
        partial.unlink(missing_ok=True)
        return Result(remote.name, "error", "pull failed: %s" % exc)
    if size != remote.size or actual != expected:
        partial.unlink(missing_ok=True)
        log("MISMATCH %s: size %d vs phone %d, sha256 %s vs phone %s; kept on the phone"
            % (remote.name, size, remote.size, actual[:12], expected[:12]))
        return Result(remote.name, "mismatch", "size %d/%d" % (size, remote.size))
    try:
        os.replace(partial, final)
        _fsync_dir(dest_dir)  # the directory entry must be on disk before the phone's copy goes
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return Result(remote.name, "error", "cannot move the verified file into place: %s" % exc)
    return _delete_remote(phone, remote, "done", "%d bytes sha256 %s" % (size, actual[:12]))


def _already_there(phone, remote, final: Path, expected: str, log) -> Result:
    if final.stat().st_size == remote.size and file_sha256(final) == expected:
        _fsync_dir(final.parent)  # same rule as a fresh pull: the PC copy is durable before the phone's goes
        return _delete_remote(phone, remote, "already-there", "identical copy on the PC")
    log("CONFLICT %s: a different file with that name exists on the PC; kept on the phone" % remote.name)
    return Result(remote.name, "conflict", "different file on the PC")


def _delete_remote(phone, remote, status: str, detail: str) -> Result:
    try:
        phone.delete(remote.path)
    except AdbError as exc:
        return Result(remote.name, "error", "verified but cannot delete from the phone: %s" % exc)
    return Result(remote.name, status, detail)


MAX_CONSECUTIVE_ERRORS = 3


def offload_all(phone, root: Path, limit_mb_s: float, log, clock=time.monotonic, sleep=time.sleep) -> list:
    """One sweep: every finished chunk currently on the phone, oldest first.

    A chunk that fails (mismatch, conflict, bad name) is kept on the phone, warned about, and the
    sweep goes on with the others. Several pull failures in a row mean the phone is probably gone.
    """
    results = []
    errors_in_a_row = 0
    for remote in phone.list_chunks():
        try:
            result = offload_chunk(phone, remote, root, limit_mb_s, log, clock, sleep)
        except AdbError as exc:
            result = Result(remote.name, "error", str(exc))
        log("offload %s: %s %s" % (result.name, result.status, result.detail))
        results.append(result)
        errors_in_a_row = errors_in_a_row + 1 if result.status == "error" else 0 if result.status != "busy" else errors_in_a_row
        if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
            break
    return results
