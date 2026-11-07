#!/usr/bin/env python3
"""daemon.py: the PC side of phone recording. Runs for as long as phone-cam runs.

* Follows OBS (StreamStateChanged): streaming means the phone should be recording.
* Also records while a manual request exists (`phone-cam rec start`).
* Every few seconds reconciles: starts or stops the app's recording to match, and offloads
  finished chunks (verified, one at a time, idle CPU and disk priority).
* After the stream ends it keeps offloading until the phone is empty, then notifies.

Usage: python3 daemon.py [--out-root DIR] [--limit-mb-s 40] [--interval 5]
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adbphone  # noqa: E402
import chunks  # noqa: E402
import lowprio  # noqa: E402
import obs_watch  # noqa: E402
import offload  # noqa: E402
import rec  # noqa: E402
import state  # noqa: E402

SWEEP_BUSY_S = 20
SWEEP_IDLE_S = 120
GIVE_UP_AFTER_S = 600
OBS_UNREACHABLE_S = 120    # OBS closed this long: tell you the stream will not be recorded
OBS_LOST_STREAM_S = 180    # OBS gone this long mid-stream: the stream is over
PHONE_AWAY_S = 60
RESTART_BACKOFF_MAX_S = 300
CLEAN_SWEEPS_NEEDED = 2     # consecutive empty-phone sweeps before "recording ready" is announced
OFFLOAD_ALERT_AFTER = 3     # consecutive bad sweeps before you are told


def log(message: str) -> None:
    now = time.time()
    stamp = "%s.%03d" % (time.strftime("%F %T", time.localtime(now)), int(now * 1000) % 1000)
    sys.stdout.write("%s phonecam-daemon: %s\n" % (stamp, message))
    sys.stdout.flush()


def decide(desired: bool, status: dict | None) -> str | None:
    """What to tell the app: 'start', 'stop', or None when it already matches."""
    if status is None or not status.get("running"):
        return None
    recording = bool(status.get("recording"))
    if desired and not recording:
        return "start"
    if not desired and recording:
        return "stop"
    return None


def desktop_notify(title: str, body: str) -> None:
    """Best effort: a missing, hung or failing notify-send must never take a thread down."""
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.run(["notify-send", "-a", "phone-cam", title, body], capture_output=True, timeout=10, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        log("cannot show a desktop notification: %s" % exc)


def obs_process_exists() -> bool:
    """Read-only, exact-name check for a running OBS (no signals): `pgrep -x obs`."""
    try:
        done = subprocess.run(["pgrep", "-x", "obs"], capture_output=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return True  # cannot tell: assume it is there, so a recording is never ended on a guess
    return done.returncode == 0


class Controller:
    """Decides what the phone should do. tick() only talks to the app and returns quickly; the slow
    offload runs on its own thread, so a stream start is never held up behind a chunk transfer."""

    def __init__(self, phone, store: state.Store, root: Path, limit_mb_s: float, notify=desktop_notify,
                 now=time.monotonic, session_factory=rec.new_session, logger=log, sleep=time.sleep,
                 warnings_file: Path | None = None, obs_running=obs_process_exists):
        self.phone = phone
        self.store = store
        self.root = root
        self.limit = limit_mb_s
        self.notify = notify
        self.now = now
        self.new_session = session_factory
        self.log = logger
        self.sleep = sleep
        self.warnings_file = warnings_file
        self.obs_running = obs_running
        self._no_obs_process_since: float | None = None
        self._offload_failures = 0
        self._clean_sweeps = 0
        self._offload_bad_names: set = set()
        self._lock = threading.RLock()
        self.wake = threading.Event()   # set when OBS says something, so the control loop reacts at once
        self.stream_active = False
        self.stream_session: str | None = None
        self.finalizing: str | None = None
        self.warnings: dict = {}
        self._finalize_since = 0.0
        self._was_desired = False
        self._last_session: str | None = None
        self._last_sweep = float("-inf")
        self._sweep_thread: threading.Thread | None = None
        self._unreachable_since: float | None = None
        self._start_attempts = 0
        self._next_start_at = 0.0
        self._warned = False

    # --- events from OBS ---

    def set_stream(self, active: bool) -> None:
        with self._lock:
            if active and not self.stream_active:
                self.stream_session = self.stream_session or self.new_session()
                self.finalizing = None
            if not active and self.stream_active:
                self.finalizing = self.stream_session
                self._finalize_since = self.now()
                self.stream_session = None
            self.stream_active = active
        self.wake.set()

    def obs_up(self) -> None:
        self._no_obs_process_since = None
        self._clear("obs")

    def obs_down(self, seconds: float) -> None:
        """The OBS websocket has been unreachable for `seconds`.

        A stream is only ended when the OBS *process* has been gone for OBS_LOST_STREAM_S: while OBS still
        runs (websocket server off, busy, restarting) the recording continues and you are warned.
        """
        if self.obs_running():
            self._no_obs_process_since = None
            if seconds > OBS_UNREACHABLE_S:
                self._alert("obs", "OBS is running but its websocket does not answer: the camera will not follow "
                                   "the stream (Tools, WebSocket Server Settings)")
            return
        if self._no_obs_process_since is None:
            self._no_obs_process_since = self.now()
        gone_for = self.now() - self._no_obs_process_since
        if self.stream_active and gone_for > OBS_LOST_STREAM_S:
            self.log("OBS has been gone for %d s during a stream: treating the stream as ended" % gone_for)
            self.set_stream(False)
            self._alert("obs", "OBS disappeared during the stream: the camera recording was ended")
        elif not self.stream_active and seconds > OBS_UNREACHABLE_S:
            self._alert("obs", "OBS is not running: the camera will not record when the stream starts")

    # --- warnings you should see ---

    def _alert(self, kind: str, message: str) -> None:
        with self._lock:
            if self.warnings.get(kind) == message:
                return
            new_kind = kind not in self.warnings
            self.warnings[kind] = message
            self._write_warnings()
        self.log("WARNING %s: %s" % (kind, message))
        if new_kind:
            self.notify("Phone camera: " + kind, message)

    def _clear(self, kind: str) -> None:
        with self._lock:
            if self.warnings.pop(kind, None) is not None:
                self.log("warning cleared: %s" % kind)
                self._write_warnings()

    def _write_warnings(self) -> None:
        if not self.warnings_file:
            return
        tmp = self.warnings_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"warnings": sorted(self.warnings.values())}) + "\n", encoding="utf-8")
            os.replace(tmp, self.warnings_file)
        except OSError as exc:
            self.log("cannot write %s: %s" % (self.warnings_file, exc))

    # --- control ---

    def _desired(self):
        with self._lock:
            manual = self.store.read_manual()
            session = manual or self.stream_session
            return bool(self.stream_active or manual), session

    def tick(self) -> None:
        desired, session = self._desired()
        self._track_end_of_recording(desired, session)
        try:
            status = self.phone.read_status()
        except adbphone.AdbError as exc:
            self._phone_unreachable(desired, exc)
            return
        self._phone_reachable()
        self._control(desired, session, status)
        self._maybe_sweep(desired)

    def _control(self, desired: bool, session, status) -> None:
        action = decide(desired, status)
        if status and status.get("recording"):
            self._start_attempts = 0
            self._next_start_at = 0.0
            self._clear("recording")
        if action == "start" and session:
            if status.get("error") and self._start_attempts >= 1:
                self._alert("recording", "the phone cannot record: %s" % status["error"])
            if self.now() >= self._next_start_at:
                self._send_start(session)
        elif action == "stop":
            try:
                rec.stop_recording(self.phone)
                self.log("recording stopped on the phone")
            except adbphone.AdbError as exc:
                self.log("cannot stop the recording yet (%s); will retry" % exc)

    def _send_start(self, session: str) -> None:
        self._start_attempts += 1
        self._next_start_at = self.now() + min(RESTART_BACKOFF_MAX_S, 5 * 2 ** self._start_attempts)
        try:
            rec.start_recording(self.phone, session)
            self.log("recording started on the phone (session %s, attempt %d)" % (session, self._start_attempts))
        except adbphone.AdbError as exc:
            self.log("cannot start the recording (%s); next try in %d s" % (exc, self._next_start_at - self.now()))

    def _phone_unreachable(self, desired: bool, exc: Exception) -> None:
        if self._unreachable_since is None:
            self._unreachable_since = self.now()
        self._warn_once("phone not reachable: %s" % exc)
        if desired and self.now() - self._unreachable_since > PHONE_AWAY_S:
            self._alert("phone", "the phone is not reachable while a recording is wanted (it keeps recording if the app is up)")

    def _phone_reachable(self) -> None:
        self._unreachable_since = None
        self._warned = False
        self._clear("phone")

    def _track_end_of_recording(self, desired: bool, session: str | None) -> None:
        """Whoever ended the recording (OBS, `phone-cam rec stop`), the after-stream steps must run."""
        with self._lock:
            if desired and not self._was_desired:
                self._start_attempts = 0       # a new wish to record starts with a clean slate
                self._next_start_at = 0.0
            if desired:
                self._last_session = session
            elif self._was_desired and self.finalizing is None:
                self.finalizing = self._last_session
                self._finalize_since = self.now()
            self._was_desired = desired

    # --- offload (own thread) ---

    def _maybe_sweep(self, desired: bool) -> None:
        if self._sweep_thread is not None and self._sweep_thread.is_alive():
            return
        busy = desired or self.finalizing is not None
        if self.now() - self._last_sweep < (SWEEP_BUSY_S if busy else SWEEP_IDLE_S):
            return
        self._last_sweep = self.now()
        self._sweep_thread = threading.Thread(target=self._sweep, name="offload", daemon=True)
        self._sweep_thread.start()

    def wait_sweep(self, timeout: float = 30.0) -> None:
        thread = self._sweep_thread
        if thread is not None:
            thread.join(timeout)

    def _sweep(self) -> None:
        try:
            self._sweep_once()
        except Exception as exc:  # a sweep must never die silently: the next due sweep starts a new thread
            self.log("offload sweep crashed: %s: %s" % (type(exc).__name__, exc))
            self._count_offload_problem("the offload crashed: %s" % exc)

    def _sweep_once(self) -> None:
        try:
            results = offload.offload_all(self.phone, self.root, self.limit, self.log, sleep=self.sleep)
            # Order matters: a chunk that finishes between these reads must not slip through. Status and the
            # unfinished count first, the list of finished chunks last (a chunk only ever moves .part to .mp4).
            status = self.phone.read_status()
            connected = self.phone.state() == "device"
            partials = self.phone.count_partials()
            remaining = [c for c in self.phone.list_chunks() if chunks.parse_chunk_name(c.name)]
        except adbphone.AdbError as exc:
            self._clean_sweeps = 0
            self._warn_once("offload sweep failed: %s" % exc)
            return
        bad = [r for r in results if r.status in ("error", "mismatch", "conflict")]
        if bad:
            self._offload_bad_names = {r.name for r in bad}
            self._count_offload_problem("%s: %s %s" % (bad[0].name, bad[0].status, bad[0].detail))
        elif any(r.status in ("done", "already-there") for r in results) or (
                self._offload_bad_names and not self._offload_bad_names & {c.name for c in remaining}):
            # busy and gone say nothing about whether pulling works, but a chunk that is no longer on the phone
            # (pulled by hand, removed) ends the problem it caused
            self._offload_failures = 0
            self._offload_bad_names = set()
            self._clear("offload")
        recording = bool((status or {}).get("recording"))
        closing = int((status or {}).get("closing_chunks", 0) or 0)
        desired, _ = self._desired()
        gave_up = False
        with self._lock:
            finalizing = self.finalizing
            if finalizing is None or desired:
                self._clean_sweeps = 0
                return
            if connected and not remaining and not recording and not closing and not partials:
                self._clean_sweeps += 1
                if self._clean_sweeps < CLEAN_SWEEPS_NEEDED:
                    return                       # confirm on the next sweep before announcing
                self.finalizing = None
            elif self.now() - self._finalize_since > GIVE_UP_AFTER_S:
                self.finalizing = None
                gave_up = True
            else:
                self._clean_sweeps = 0
                return
        self._clean_sweeps = 0
        if gave_up:
            self.log("gave up waiting: %d chunks still on the phone" % len(remaining))
            self.notify("Camera recording incomplete",
                        "%d chunks are still on the phone; they transfer next time phone-cam runs" % len(remaining))
            return
        self._finish(finalizing)

    def _count_offload_problem(self, detail: str) -> None:
        self._offload_failures += 1
        if self._offload_failures >= OFFLOAD_ALERT_AFTER:
            self._alert("offload", "chunks are not reaching the PC (%d sweeps in a row): %s" % (self._offload_failures, detail))

    def _finish(self, session: str) -> None:
        parsed = chunks.SESSION_RE.match(session or "")
        date = "%s-%s-%s" % parsed.groups()[:3] if parsed else None
        if not date:
            self.log("recording finished (session %s)" % session)
            return
        folder = chunks.cam_dir(self.root, date)
        files = chunks.list_local_chunks(folder)
        size_gb = sum(f.stat().st_size for f in files) / 1e9
        broken = list((folder / chunks.BROKEN_DIR).glob("cam-*")) if (folder / chunks.BROKEN_DIR).is_dir() else []
        self.log("camera recording complete: %d chunks, %.1f GB in %s" % (len(files), size_gb, folder))
        extra = " %d damaged chunks are in cam/broken." % len(broken) if broken else ""
        self.notify("Camera recording ready",
                    "%d chunks, %.1f GB for %s. Run: phone-cam join %s.%s" % (len(files), size_gb, date, date, extra))

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            self.log(message)
            self._warned = True


def control_loop(controller, watcher_thread, stop: threading.Event, interval: float) -> int:
    """0 when asked to stop; 1 when the OBS watcher thread has died (the supervisor then restarts the daemon)."""
    while not stop.is_set():
        if not watcher_thread.is_alive():
            log("the OBS watcher thread died: exiting so the supervisor restarts the daemon")
            return 1
        try:
            controller.tick()
        except Exception as exc:  # one bad tick must not end the daemon
            log("tick failed: %s: %s" % (type(exc).__name__, exc))
        controller.wake.wait(interval)
        controller.wake.clear()
    return 0


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out-root", default=None,
                        help="folder that gets the YYYY-MM-DD/cam folders (default: $PHONECAM_OUT_ROOT or ~/Videos/phonecam)")
    parser.add_argument("--limit-mb-s", type=float, default=offload.DEFAULT_LIMIT_MB_S)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    lowprio.lower_priority(log)
    stop = threading.Event()
    wakers: list = []

    def request_stop(*_):
        stop.set()
        for event in wakers:
            event.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    root = Path(args.out_root).expanduser() if args.out_root else chunks.out_root()
    warnings_file = state.runtime_dir() / "phone-cam.daemon.json"
    controller = Controller(adbphone.AdbPhone(), state.Store(), root, args.limit_mb_s, warnings_file=warnings_file)
    wakers.append(controller.wake)
    controller._write_warnings()
    watcher = obs_watch.ObsWatcher(controller.set_stream, log, on_down=controller.obs_down, on_up=controller.obs_up)
    watcher_thread = threading.Thread(target=watcher.run, args=(stop,), name="obs-watcher", daemon=True)
    watcher_thread.start()
    log("started, chunks go to %s/YYYY-MM-DD/cam" % root)
    code = control_loop(controller, watcher_thread, stop, args.interval)
    if code:
        warnings_file.unlink(missing_ok=True)
        return code
    warnings_file.unlink(missing_ok=True)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
