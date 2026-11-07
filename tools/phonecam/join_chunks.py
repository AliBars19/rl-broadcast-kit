#!/usr/bin/env python3
"""join_chunks.py: join the offloaded chunks of one stream into a single MP4, without re-encoding.

Usage: python3 join_chunks.py CAM_DIR [--output PATH] [--force]
Writes CAM_DIR/cam-joined.mp4 by default (ffmpeg concat demuxer, -c copy). Chunks are joined in
the order of their first-frame time. Gaps in the numbering are reported, never hidden.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chunks  # noqa: E402

JOINED_NAME = "cam-joined.mp4"


class JoinError(Exception):
    """Nothing to join, or ffmpeg failed. The message is safe to print."""


def concat_list(files: list) -> str:
    """The ffmpeg concat demuxer list; single quotes in names are escaped as the format requires."""
    lines = []
    for path in files:
        escaped = str(path).replace("'", "'\\''")
        lines.append("file '%s'" % escaped)
    return "\n".join(lines) + "\n"


def missing_indexes(files: list) -> list:
    """Chunk numbers missing inside each session (a restart of the app may legitimately renumber)."""
    by_session: dict = {}
    for path in files:
        parsed = chunks.parse_chunk_name(path.name)
        by_session.setdefault(parsed.session, []).append(parsed.index)
    gaps = []
    for session, indexes in by_session.items():
        gaps += ["%s #%d" % (session, i) for i in range(1, max(indexes) + 1) if i not in indexes]
    return gaps


SHORT_AUDIO_RATIO = 0.5   # audio shorter than this fraction of the video counts as "no audio" (a mic dead all chunk)


class Signature(NamedTuple):
    video: tuple   # ((codec, width, height), ...)
    audio: tuple   # ((codec, sample_rate), ...)
    audio_short: bool = False  # the audio track exists but covers almost none of the chunk

    def __str__(self) -> str:
        return "video %s, audio %s" % (list(self.video) or "none", list(self.audio) or "none")


def stream_signature(path: Path, run=subprocess.run) -> Signature:
    """Codec, size and rate of every stream in a chunk."""
    done = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,sample_rate,duration",
                "-of", "json", str(path)], capture_output=True, text=True)
    try:
        streams = json.loads(done.stdout)["streams"] if done.returncode == 0 else []
    except (ValueError, KeyError):
        streams = []
    if not streams:
        raise JoinError("cannot read the streams of %s: %s" % (path.name, (done.stderr or "").strip()[:150]))
    video = tuple(sorted((s.get("codec_name"), s.get("width"), s.get("height")) for s in streams
                         if s.get("codec_type") == "video"))
    audio = tuple(sorted((s.get("codec_name"), s.get("sample_rate")) for s in streams if s.get("codec_type") == "audio"))
    return Signature(video, audio, _audio_is_short(streams))


def _audio_is_short(streams: list) -> bool:
    def length(kind: str) -> float:
        values = [float(s["duration"]) for s in streams if s.get("codec_type") == kind and s.get("duration")]
        return max(values) if values else 0.0
    video, audio = length("video"), length("audio")
    return bool(audio) and video > 0 and audio < SHORT_AUDIO_RATIO * video


def plan_streams(files: list, probe=stream_signature) -> tuple:
    """Return (files that have no audio track, the audio signature to give them) or raise JoinError.

    Chunks that lost their microphone have no audio track; they get silent audio so the joined file keeps
    one audio stream. Any other difference (video codec or size, audio codec or rate) is refused.
    """
    sigs = {path: probe(path) for path in files}
    first = sigs[files[0]]
    for path, sig in sigs.items():
        if sig.video != first.video:
            raise JoinError("%s has different video (%s) from %s (%s); join them separately, or move the odd "
                            "chunk out of the folder" % (path.name, sig.video, files[0].name, first.video))
    with_audio = {sig.audio for sig in sigs.values() if sig.audio and not sig.audio_short}
    if len(with_audio) > 1:
        raise JoinError("the chunks have different audio streams (%s); join them separately" % sorted(with_audio))
    silent = [path for path, sig in sigs.items() if not sig.audio or sig.audio_short]
    return (silent, next(iter(with_audio))) if with_audio and silent else ([], ())


def add_silent_audio(source: Path, target: Path, audio: tuple, run=subprocess.run) -> None:
    rate = audio[0][1] or "48000"
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
           "-f", "lavfi", "-i", "anullsrc=r=%s:cl=mono" % rate, "-map", "0:v", "-map", "1:a", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "128k", "-shortest", "-f", "mp4", str(target)]
    done = run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise JoinError("cannot add silent audio to %s: %s" % (source.name, (done.stderr or "").strip()[:200]))


def join(cam_dir: Path, output: Path | None = None, force: bool = False, run=subprocess.run, log=print,
         probe=stream_signature) -> Path:
    files = chunks.list_local_chunks(cam_dir)
    if not files:
        raise JoinError("no chunk files in %s" % cam_dir)
    target = output or cam_dir / JOINED_NAME
    if target.exists() and not force:
        raise JoinError("%s already exists (use --force to replace it)" % target)
    silent, audio = plan_streams(files, probe)
    gaps = missing_indexes(files)
    if gaps:
        log("warning: missing chunks %s; the joined file skips those minutes" % ", ".join(gaps))
    partial = target.with_name("." + target.name + ".part")
    scratch = Path(tempfile.mkdtemp(prefix=".join-", dir=cam_dir)) if silent else None
    try:
        names = list(files)
        for path in silent:
            fixed = scratch / path.name
            log("%s has no audio track: adding silent audio for the join" % path.name)
            add_silent_audio(path, fixed, audio, run)
            names[names.index(path)] = fixed
        _concat(names, partial, target, run)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    log("joined %d chunks into %s (%.1f GB)" % (len(files), target, target.stat().st_size / 1e9))
    return target


def _concat(names: list, partial: Path, target: Path, run) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(concat_list(names))
        list_path = handle.name
    try:
        cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
               "-i", list_path, "-c", "copy", "-f", "mp4", str(partial)]
        done = run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            partial.unlink(missing_ok=True)
            raise JoinError("ffmpeg failed: %s" % (done.stderr or "").strip()[:300])
        os.replace(partial, target)
    finally:
        os.unlink(list_path)


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("cam_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        join(args.cam_dir, args.output, args.force)
    except JoinError as exc:
        print("join: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
