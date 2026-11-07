#!/usr/bin/env python3
"""phonecam.py: command line for the phone camera recording (`phone-cam rec|offload|join|sync|trash`).

  rec start|stop|status      start or stop a recording by hand (the OBS trigger is the normal path)
  offload [--watch]          pull finished chunks to <out root>/DATE/cam (verified, then deleted on the phone)
  join [DATE|DIR]            join the chunks into one file, no re-encode (cam-joined.mp4)
  sync [DATE|DIR]            find the camera-to-stream audio offset, writes cam/sync.json (needs numpy)
  trash DATE|DIR [--dry-run] move that stream's camera recording to the Trash (gio trash)

DATE is YYYY-MM-DD; with nothing given, join and sync use the newest dated folder that has a cam folder.
The out root is $PHONECAM_OUT_ROOT (default ~/Videos/phonecam).
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adbphone  # noqa: E402
import chunks  # noqa: E402
import lowprio  # noqa: E402
import offload  # noqa: E402
import rec  # noqa: E402
import state  # noqa: E402

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def say(message: str) -> None:
    print(message)


def resolve_stream_dir(target: str | None, root: Path) -> Path:
    """A date, a stream folder, its cam folder, or (None) the newest stream folder with a cam folder."""
    if target is None:
        candidates = sorted(p for p in root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]")
                            if chunks.DATE_FOLDER_RE.match(p.name) and (p / "cam").is_dir())
        if not candidates:
            raise SystemExit("phone-cam: no YYYY-MM-DD folder with a cam folder under %s" % root)
        return candidates[-1]
    if DATE_RE.match(target):
        return chunks.stream_dir(root, target)
    path = Path(target).expanduser()
    return path.parent if path.name == "cam" else path


def cmd_rec(args, phone) -> int:
    store = state.Store()
    if args.action == "start":
        session = rec.new_session()
        store.write_manual(session)
        rec.start_recording(phone, session)
        say("recording requested (session %s); chunks arrive in %s" % (
            session, chunks.cam_dir(chunks.out_root(), "-".join((session[:4], session[4:6], session[6:8])))))
        say("the phone-cam daemon offloads them; `phone-cam rec stop` ends it")
    elif args.action == "stop":
        store.clear_manual()
        rec.stop_recording(phone)
        say("recording stopped; remaining chunks are offloaded by the daemon (or run `phone-cam offload`)")
    else:
        say(json.dumps(phone.read_status(), indent=2))
    return 0


def cmd_offload(args, phone) -> int:
    lowprio.lower_priority(say)
    root = chunks.out_root()
    while True:
        results = offload.offload_all(phone, root, args.limit_mb_s, say)
        busy = [r for r in results if r.status == "busy"]
        if busy:
            say("%d chunks are being offloaded by the phone-cam daemon right now; nothing to do for them" % len(busy))
        if not args.watch:
            return 0 if all(r.status in ("done", "already-there", "busy", "gone") for r in results) else 1
        time.sleep(20)


def cmd_join(args, phone) -> int:
    import join_chunks
    try:
        join_chunks.join(resolve_stream_dir(args.target, chunks.out_root()) / "cam", force=args.force, log=say)
    except join_chunks.JoinError as exc:
        print("join: %s" % exc, file=sys.stderr)
        return 1
    return 0


def cmd_sync(args, phone) -> int:
    import sync_audio
    folder = resolve_stream_dir(args.target, chunks.out_root())
    return sync_audio.main([str(folder)])


def cmd_trash(args, phone) -> int:
    import retention
    try:
        retention.trash(args.target, chunks.out_root(), dry_run=args.dry_run, log=say)
    except retention.RetentionError as exc:
        print("trash: %s" % exc, file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("rec")
    p.add_argument("action", choices=("start", "stop", "status"))
    p.set_defaults(run=cmd_rec)
    p = sub.add_parser("offload")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--limit-mb-s", type=float, default=offload.DEFAULT_LIMIT_MB_S)
    p.set_defaults(run=cmd_offload)
    p = sub.add_parser("join")
    p.add_argument("target", nargs="?")
    p.add_argument("--force", action="store_true")
    p.set_defaults(run=cmd_join)
    p = sub.add_parser("sync")
    p.add_argument("target", nargs="?")
    p.set_defaults(run=cmd_sync)
    p = sub.add_parser("trash")
    p.add_argument("target")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(run=cmd_trash)
    return parser


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args, adbphone.AdbPhone())
    except adbphone.AdbError as exc:
        print("phone-cam: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
