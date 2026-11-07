"""obs_watch.py: tell a callback when OBS starts or stops streaming (obs-websocket StreamStateChanged).

Uses the repo's obsws.py client, so authentication works exactly as for the other OBS tools and
the password is never read here, logged or stored. Reconnects with a backoff while OBS is not running.
"""
import os
import select
import sys
import threading
import time
from typing import Any, Callable

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import obsws  # noqa: E402

SUBSCRIBE_OUTPUTS = 1 << 6
OP_EVENT = 5
BACKOFF_START = 2
BACKOFF_MAX = 10  # short, so a stream started right after OBS opens is picked up within seconds
POLL_S = 1.0
STARTED = ("OBS_WEBSOCKET_OUTPUT_STARTING", "OBS_WEBSOCKET_OUTPUT_STARTED")
STOPPED = ("OBS_WEBSOCKET_OUTPUT_STOPPED",)


def parse_stream_event(message: dict) -> bool | None:
    """True when streaming begins, False when it has stopped, None for anything else."""
    if message.get("op") != OP_EVENT:
        return None
    body = message.get("d")
    if not isinstance(body, dict) or body.get("eventType") != "StreamStateChanged":
        return None
    state = (body.get("eventData") or {}).get("outputState")
    if state in STARTED:
        return True
    if state in STOPPED:
        return False
    return None


class ObsWatcher:
    def __init__(self, on_change: Callable[[bool], None], log: Callable[[str], None], open_session=None,
                 on_down: Callable[[float], None] | None = None, on_up: Callable[[], None] | None = None,
                 clock=time.monotonic):
        self.on_change = on_change
        self.log = log
        self._open = open_session or obsws.open_session
        self.on_down = on_down or (lambda seconds: None)
        self.on_up = on_up or (lambda: None)
        self._clock = clock
        self._down_since: float | None = None

    def run(self, stop: threading.Event) -> None:
        delay = BACKOFF_START
        while not stop.is_set():
            try:
                self._watch_once(stop)
                delay = BACKOFF_START
            except (obsws.ObsError, OSError) as exc:
                self.log("OBS not reachable (%s); retrying in %d s" % (exc, delay))
                self._note_down()
            except Exception as exc:  # a bug in a callback must not end the watcher for good
                self.log("OBS watcher error (%s: %s); reconnecting in %d s" % (type(exc).__name__, exc, delay))
                self._note_down()
            stop.wait(delay)
            delay = min(BACKOFF_MAX, delay * 2)

    def _note_down(self) -> None:
        if self._down_since is None:
            self._down_since = self._clock()
        try:
            self.on_down(self._clock() - self._down_since)
        except Exception as exc:
            self.log("on_down callback failed: %s: %s" % (type(exc).__name__, exc))

    def _watch_once(self, stop: threading.Event) -> None:
        sock = self._open(SUBSCRIBE_OUTPUTS, 10)
        try:
            self.log("connected to OBS")
            self._down_since = None
            self.on_up()
            self._announce_current(sock)
            while not stop.is_set():
                ready, _, _ = select.select([sock], [], [], POLL_S)
                if not ready:
                    continue
                change = parse_stream_event(obsws.receive_json(sock))
                if change is not None:
                    self.log("OBS stream %s" % ("started" if change else "stopped"))
                    self.on_change(change)
        finally:
            obsws.close_session(sock)

    def _announce_current(self, sock) -> None:
        """Sync with OBS on connect: the daemon may have restarted mid-stream, or OBS may have been restarted."""
        response: dict[str, Any] = obsws.call_request(sock, "GetStreamStatus", {})
        data = response.get("responseData") or {}
        active = data.get("outputActive") is True
        if active:
            self.log("OBS is already streaming")
        self.on_change(active)
