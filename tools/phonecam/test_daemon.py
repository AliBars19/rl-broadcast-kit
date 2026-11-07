"""Tests for obs_watch.py, rec.py and daemon.py: a fake OBS websocket server and a fake phone."""
import base64
import hashlib
import contextlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import adbphone  # noqa: E402
import daemon  # noqa: E402
import obs_watch  # noqa: E402
import obsws  # noqa: E402
import rec  # noqa: E402
import state  # noqa: E402

NAME1 = "cam-20261107-203015-0001-1794062677451.mp4"
REMOTE = "/sdcard/Android/data/io.github.rlbroadcastkit.phonecam/files/chunks"


def event(output_state: str) -> dict:
    return {"op": 5, "d": {"eventType": "StreamStateChanged", "eventIntent": 64,
                           "eventData": {"outputActive": output_state.endswith("STARTED"), "outputState": output_state}}}


class FakeObs(threading.Thread):
    """Just enough of obs-websocket v5: handshake, no auth, GetStreamStatus, scripted events."""

    def __init__(self, active_at_connect=False, script=()):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(2)
        self.port = self.server.getsockname()[1]
        self.active = active_at_connect
        self.script = list(script)
        self.requests = []
        self.subscriptions = None

    @staticmethod
    def send(conn, obj):
        data = json.dumps(obj).encode()
        head = bytes([0x81])
        head += bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack(">H", len(data))
        conn.sendall(head + data)

    def run(self):
        conn, _ = self.server.accept()
        head = obsws.read_http_head(conn)
        key = [l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1((key + obsws.WS_GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        self.send(conn, {"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}})
        identify = json.loads(obsws.read_frame(conn)[2])
        self.subscriptions = identify["d"]["eventSubscriptions"]
        self.send(conn, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
        request = json.loads(obsws.read_frame(conn)[2])["d"]
        self.requests.append(request["requestType"])
        self.send(conn, {"op": 7, "d": {"requestType": request["requestType"], "requestId": request["requestId"],
                                        "requestStatus": {"result": True, "code": 100},
                                        "responseData": {"outputActive": self.active}}})
        for item in self.script:
            time.sleep(0.05)
            self.send(conn, item)
        time.sleep(0.5)
        conn.close()
        self.server.close()


class ParseTests(unittest.TestCase):
    def test_states(self):
        self.assertTrue(obs_watch.parse_stream_event(event("OBS_WEBSOCKET_OUTPUT_STARTING")))
        self.assertTrue(obs_watch.parse_stream_event(event("OBS_WEBSOCKET_OUTPUT_STARTED")))
        self.assertIs(obs_watch.parse_stream_event(event("OBS_WEBSOCKET_OUTPUT_STOPPED")), False)

    def test_other_states_and_events_are_ignored(self):
        self.assertIsNone(obs_watch.parse_stream_event(event("OBS_WEBSOCKET_OUTPUT_STOPPING")))
        self.assertIsNone(obs_watch.parse_stream_event(event("OBS_WEBSOCKET_OUTPUT_RECONNECTED")))
        self.assertIsNone(obs_watch.parse_stream_event({"op": 5, "d": {"eventType": "RecordStateChanged"}}))
        self.assertIsNone(obs_watch.parse_stream_event({"op": 7, "d": {}}))
        self.assertIsNone(obs_watch.parse_stream_event({"op": 5, "d": "junk"}))


class WatcherTests(unittest.TestCase):
    def watch(self, fake, seconds=1.2):
        fake.start()
        changes = []
        stop = threading.Event()
        watcher = obs_watch.ObsWatcher(changes.append, lambda m: None)
        with mock.patch.object(obsws, "PORT", fake.port):
            thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
            thread.start()
            time.sleep(seconds)
            stop.set()
            thread.join(3)
        return changes

    def test_start_and_stop_events_reach_the_callback(self):
        fake = FakeObs(script=[event("OBS_WEBSOCKET_OUTPUT_STARTING"), event("OBS_WEBSOCKET_OUTPUT_STARTED"),
                               event("OBS_WEBSOCKET_OUTPUT_STOPPING"), event("OBS_WEBSOCKET_OUTPUT_STOPPED")])
        self.assertEqual(self.watch(fake), [False, True, True, False])
        self.assertEqual(fake.subscriptions, obs_watch.SUBSCRIBE_OUTPUTS)
        self.assertEqual(fake.requests, ["GetStreamStatus"])

    def test_stream_already_running_when_the_daemon_connects(self):
        self.assertEqual(self.watch(FakeObs(active_at_connect=True)), [True])

    def test_unreachable_obs_is_retried_without_raising(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        stop = threading.Event()
        messages = []
        watcher = obs_watch.ObsWatcher(lambda c: None, messages.append)
        with mock.patch.object(obsws, "PORT", port):
            thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
            thread.start()
            time.sleep(0.5)
            stop.set()
            thread.join(3)
        self.assertTrue(any("not reachable" in m for m in messages))

    def test_down_time_is_reported_and_cleared_on_connect(self):
        downs, ups = [], []
        now = [0.0]
        attempts = {"n": 0}
        fake = FakeObs()
        fake.start()
        real = obsws.open_session

        def flaky(subs, timeout):
            attempts["n"] += 1
            now[0] += 10
            if attempts["n"] < 3:
                raise OSError("refused")
            return real(subs, timeout)
        stop = threading.Event()
        watcher = obs_watch.ObsWatcher(lambda c: None, lambda m: None, open_session=flaky, on_down=downs.append,
                                       on_up=lambda: ups.append(1), clock=lambda: now[0])
        with mock.patch.object(obsws, "PORT", fake.port), mock.patch.object(obs_watch, "BACKOFF_START", 0.01):
            thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
            thread.start()
            time.sleep(1.0)
            stop.set()
            thread.join(3)
        self.assertEqual(downs[:2], [0, 10])
        self.assertEqual(ups, [1])


class FakeAppPhone:
    """Records the control commands and serves a status dict and a chunk list."""

    def __init__(self, status=None):
        self.status = status
        self.commands = []
        self.chunks = []
        self.unreachable = False
        self.adb_state = "device"
        self.partials = 0

    def state(self):
        return self.adb_state

    def count_partials(self):
        return self.partials

    def read_status(self):
        if self.unreachable:
            raise adbphone.AdbError("no device")
        return self.status

    def shell(self, command, **kwargs):
        self.commands.append(command)
        return "Starting service: Intent { ... }\n"

    def list_chunks(self):
        return list(self.chunks)


class RobustnessTests(unittest.TestCase):
    def test_a_hung_notify_send_does_not_raise(self):
        boom = subprocess.TimeoutExpired("notify-send", 10)
        with mock.patch.object(daemon.shutil, "which", return_value="/usr/bin/notify-send"), \
                mock.patch.object(daemon.subprocess, "run", side_effect=boom), \
                contextlib.redirect_stdout(io.StringIO()):
            daemon.desktop_notify("title", "body")

    def test_obs_process_check_failures_assume_it_is_running(self):
        with mock.patch.object(daemon.subprocess, "run", side_effect=FileNotFoundError("pgrep")):
            self.assertTrue(daemon.obs_process_exists())
        with mock.patch.object(daemon.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
            self.assertFalse(daemon.obs_process_exists())
        with mock.patch.object(daemon.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"123\n", b"")) as run:
            self.assertTrue(daemon.obs_process_exists())
        self.assertEqual(run.call_args[0][0], ["pgrep", "-x", "obs"])

    def test_the_watcher_survives_an_unexpected_exception(self):
        calls = []

        def broken(subs, timeout):
            calls.append(1)
            raise ValueError("bug in a callback")
        stop = threading.Event()
        watcher = obs_watch.ObsWatcher(lambda c: None, lambda m: None, open_session=broken)
        with mock.patch.object(obs_watch, "BACKOFF_START", 0.01):
            thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
            thread.start()
            time.sleep(0.3)
            alive = thread.is_alive()
            stop.set()
            thread.join(3)
        self.assertTrue(alive)
        self.assertGreaterEqual(len(calls), 2)

    def test_the_control_loop_exits_with_an_error_when_the_watcher_thread_died(self):
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        controller = mock.Mock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.control_loop(controller, dead, threading.Event(), 0.01), 1)
        controller.tick.assert_not_called()

    def test_the_control_loop_survives_a_failing_tick_and_stops_on_request(self):
        stop = threading.Event()
        live = threading.Thread(target=stop.wait)
        live.start()
        controller = mock.Mock()
        controller.wake = threading.Event()
        controller.tick.side_effect = [RuntimeError("one bad tick"), None]
        controller.wake.wait = lambda interval: stop.set() if controller.tick.call_count >= 2 else None
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.control_loop(controller, live, stop, 0.01), 0)
        self.assertEqual(controller.tick.call_count, 2)
        live.join(2)


class DecideTests(unittest.TestCase):
    def test_table(self):
        running = {"running": True, "recording": False}
        recording = {"running": True, "recording": True}
        self.assertEqual(daemon.decide(True, running), "start")
        self.assertEqual(daemon.decide(False, recording), "stop")
        self.assertIsNone(daemon.decide(True, recording))
        self.assertIsNone(daemon.decide(False, running))
        self.assertIsNone(daemon.decide(True, None))
        self.assertIsNone(daemon.decide(True, {"running": False}))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = state.Store(self.root / "state")
        self.phone = FakeAppPhone({"running": True, "recording": False})
        self.clock = [0.0]
        self.notes = []
        self.obs_alive = False
        self.controller = daemon.Controller(
            self.phone, self.store, self.root / "videos", 0.0, notify=lambda t, b: self.notes.append((t, b)),
            now=lambda: self.clock[0], session_factory=lambda: "20261107-203015", logger=lambda m: None,
            sleep=lambda s: None, obs_running=lambda: self.obs_alive)

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, seconds=1.0):
        self.clock[0] += seconds
        self.controller.tick()
        self.controller.wait_sweep()

    def test_stream_start_starts_recording_with_one_session_id(self):
        self.controller.set_stream(True)
        self.tick()
        self.assertTrue(any("REC_START" in c and "20261107-203015" in c for c in self.phone.commands))

    def test_nothing_happens_without_a_stream_or_manual_request(self):
        self.tick()
        self.assertFalse(any("REC_" in c for c in self.phone.commands))

    def test_stream_stop_stops_recording(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": True, "session": "20261107-203015"}
        self.controller.set_stream(False)
        self.tick()
        self.assertTrue(any("REC_STOP" in c for c in self.phone.commands))

    def test_manual_request_records_without_a_stream_and_survives_a_stream_stop(self):
        self.store.write_manual("20261107-100000")
        self.tick()
        self.assertTrue(any("REC_START" in c and "20261107-100000" in c for c in self.phone.commands))
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.assertFalse(any("REC_STOP" in c for c in self.phone.commands))

    def test_clearing_the_manual_request_stops_the_recording(self):
        self.store.write_manual("20261107-100000")
        self.store.clear_manual()
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.assertTrue(any("REC_STOP" in c for c in self.phone.commands))

    def test_a_phone_that_is_unplugged_is_tolerated(self):
        self.controller.set_stream(True)
        self.phone.unreachable = True
        self.tick()
        self.assertEqual(self.phone.commands, [])
        self.phone.unreachable = False
        self.tick()
        self.assertTrue(any("REC_START" in c for c in self.phone.commands))

    def test_already_recording_phone_gets_no_second_start(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.assertFalse(any("REC_START" in c for c in self.phone.commands))

    def test_after_the_stream_the_daemon_notifies_once_the_phone_is_empty(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.tick()
        self.assertEqual(self.notes, [])            # one clean sweep is not enough
        self.tick(25)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("phone-cam join 2026-11-07", self.notes[0][1])
        self.tick(200)
        self.assertEqual(len(self.notes), 1)

    def test_notice_also_when_the_recording_was_ended_from_the_command_line(self):
        self.store.write_manual("20261107-100000")
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.store.clear_manual()  # `phone-cam rec stop` clears the flag and stops the app itself
        self.phone.status = {"running": True, "recording": False}
        self.tick(daemon.SWEEP_BUSY_S + 1)
        self.tick(daemon.SWEEP_BUSY_S + 1)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("2026-11-07", self.notes[0][1])

    def test_gives_up_with_a_notice_when_chunks_never_leave_the_phone(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.phone.chunks = [adbphone.RemoteChunk(NAME1, REMOTE + "/" + NAME1, 10)]
        with mock.patch.object(daemon.offload, "offload_all", return_value=[]):
            self.tick()
            self.tick(daemon.GIVE_UP_AFTER_S + 1)
        self.assertEqual([n[0] for n in self.notes], ["Camera recording incomplete"])

    def test_no_notice_while_chunks_remain_on_the_phone(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.phone.chunks = [adbphone.RemoteChunk(NAME1, REMOTE + "/" + NAME1, 10)]
        with mock.patch.object(daemon.offload, "offload_all", return_value=[]):
            self.tick()
        self.assertEqual(self.notes, [])

    def test_a_stream_start_is_not_held_up_by_a_slow_offload(self):
        release = threading.Event()
        started = threading.Event()

        def slow_offload(*args, **kwargs):
            started.set()
            release.wait(10)
            return []
        with mock.patch.object(daemon.offload, "offload_all", side_effect=slow_offload):
            self.controller.tick()               # idle sweep begins and blocks, like a 95 s pull
            self.assertTrue(started.wait(2))
            self.controller.set_stream(True)     # StreamStateChanged arrives meanwhile
            begin = time.monotonic()
            self.clock[0] += 5
            self.controller.tick()
            elapsed = time.monotonic() - begin
            self.assertTrue(any("REC_START" in c for c in self.phone.commands))
            self.assertLess(elapsed, 1.0)
            release.set()
            self.controller.wait_sweep()

    def test_the_ready_notice_waits_for_the_last_chunk_to_finish_closing(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False, "closing_chunks": 1}
        self.tick(25)
        self.assertEqual(self.notes, [])
        self.phone.status = {"running": True, "recording": False, "closing_chunks": 0}
        self.phone.partials = 1                      # a .mp4.part is still on the phone
        self.tick(25)
        self.assertEqual(self.notes, [])
        self.phone.partials = 0
        self.tick(25)
        self.tick(25)
        self.assertEqual(len(self.notes), 1)

    def test_a_chunk_that_finishes_between_the_reads_does_not_give_a_false_ready(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False, "closing_chunks": 0}
        real_status = self.phone.read_status

        def status_then_a_chunk_lands():
            result = real_status()
            self.phone.chunks = [adbphone.RemoteChunk(NAME1, REMOTE + "/" + NAME1, 10)]  # .part became .mp4 just now
            return result
        self.phone.read_status = status_then_a_chunk_lands
        with mock.patch.object(daemon.offload, "offload_all", return_value=[]):
            self.tick(25)
            self.tick(25)
        self.assertEqual(self.notes, [])

    def test_an_adb_failure_or_a_new_wish_to_record_restarts_the_confirmation(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.tick(25)                                  # clean 1
        self.phone.count_partials = mock.Mock(side_effect=adbphone.AdbError("device offline"))
        self.tick(25)                                  # sweep fails: confirmation restarts
        del self.phone.count_partials
        self.tick(25)                                  # clean 1 again
        self.assertEqual(self.notes, [])
        self.controller.set_stream(True)               # recording again: the count restarts too
        self.tick(25)
        self.controller.set_stream(False)
        self.tick(25)
        self.assertEqual(self.notes, [])
        self.tick(25)
        self.assertEqual(len(self.notes), 1)

    def test_a_dirty_sweep_between_two_clean_ones_restarts_the_confirmation(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.tick(25)                                # clean 1
        self.phone.partials = 1
        self.tick(25)                                # dirty
        self.phone.partials = 0
        self.tick(25)                                # clean 1 again
        self.assertEqual(self.notes, [])
        self.tick(25)
        self.assertEqual(len(self.notes), 1)

    def test_busy_sweeps_do_not_clear_an_offload_warning(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": True}
        self.phone.chunks = [adbphone.RemoteChunk(NAME1, REMOTE + "/" + NAME1, 10)]
        bad = [daemon.offload.Result(NAME1, "error", "disk full")]
        with mock.patch.object(daemon.offload, "offload_all", return_value=bad):
            for _ in range(3):
                self.tick(25)
        self.assertIn("offload", self.controller.warnings)
        busy = [daemon.offload.Result(NAME1, "busy"), daemon.offload.Result("d.mp4", "gone")]
        with mock.patch.object(daemon.offload, "offload_all", return_value=busy):
            self.tick(25)
        self.assertIn("offload", self.controller.warnings)       # the failing chunk is still on the phone
        self.phone.chunks = []                                     # pulled by hand meanwhile
        with mock.patch.object(daemon.offload, "offload_all", return_value=[]):
            self.tick(25)
        self.assertNotIn("offload", self.controller.warnings)

    def test_repeated_offload_problems_are_alerted_once_and_cleared_when_fixed(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": True}
        bad = [daemon.offload.Result("c.mp4", "mismatch", "size 1/2")]
        with mock.patch.object(daemon.offload, "offload_all", return_value=bad):
            for _ in range(4):
                self.tick(25)
        self.assertEqual([n[0] for n in self.notes], ["Phone camera: offload"])
        with mock.patch.object(daemon.offload, "offload_all", return_value=[daemon.offload.Result("c.mp4", "done")]):
            self.tick(25)
        self.assertNotIn("offload", self.controller.warnings)

    def test_a_crashing_sweep_is_contained_and_the_next_one_still_runs(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": True}
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise OSError(5, "I/O error")
        with mock.patch.object(daemon.offload, "offload_all", side_effect=boom):
            for _ in range(3):
                self.tick(25)
        self.assertEqual(len(calls), 3)
        self.assertEqual([n[0] for n in self.notes], ["Phone camera: offload"])

    def test_the_backoff_resets_on_a_new_wish_to_record(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": False, "error": "no space"}
        for _ in range(6):
            self.tick(5)
        before = len([c for c in self.phone.commands if "REC_START" in c])
        self.controller.set_stream(False)
        self.tick(5)
        self.controller.set_stream(True)
        self.tick(1)                                   # inside the old back-off window
        after = len([c for c in self.phone.commands if "REC_START" in c])
        self.assertEqual(after, before + 1)

    def test_a_failed_start_command_still_backs_off_and_the_sweep_still_runs(self):
        self.controller.set_stream(True)
        self.phone.shell = mock.Mock(side_effect=adbphone.AdbError("device offline"))
        with mock.patch.object(daemon.offload, "offload_all", return_value=[]) as sweep:
            self.tick(5)
            self.controller.wait_sweep()
        self.assertEqual(sweep.call_count, 1)
        calls = self.phone.shell.call_count
        self.tick(1)
        self.assertEqual(self.phone.shell.call_count, calls)   # backed off, not hammering

    def test_a_stream_event_wakes_the_control_loop(self):
        self.assertFalse(self.controller.wake.is_set())
        self.controller.set_stream(True)
        self.assertTrue(self.controller.wake.is_set())

    def test_only_one_sweep_runs_at_a_time(self):
        release = threading.Event()
        calls = []

        def slow(*args, **kwargs):
            calls.append(1)
            release.wait(10)
            return []
        with mock.patch.object(daemon.offload, "offload_all", side_effect=slow):
            self.controller.tick()
            self.clock[0] += 500
            self.controller.tick()
            time.sleep(0.1)
            self.assertEqual(len(calls), 1)
            release.set()
            self.controller.wait_sweep()

    def test_an_unplugged_phone_is_never_announced_as_finished(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.phone.adb_state = "offline"
        self.tick(25)
        self.assertEqual(self.notes, [])
        self.phone.adb_state = "device"
        self.tick(25)
        self.tick(25)
        self.assertEqual(len(self.notes), 1)

    def test_a_failure_listing_the_chunks_is_not_an_empty_phone(self):
        self.controller.set_stream(True)
        self.controller.set_stream(False)
        self.phone.status = {"running": True, "recording": False}
        self.phone.list_chunks = mock.Mock(side_effect=adbphone.AdbError("device offline"))
        self.tick(25)
        self.assertEqual(self.notes, [])

    def test_storage_failure_backs_off_and_warns_once(self):
        self.controller.set_stream(True)
        self.phone.status = {"running": True, "recording": False, "error": "cannot open chunk 1: only 3 MB free"}
        for _ in range(40):
            self.tick(5)
        starts = [c for c in self.phone.commands if "REC_START" in c]
        self.assertLessEqual(len(starts), 6)             # 40 ticks of 5 s, not 40 commands
        self.assertGreaterEqual(len(starts), 2)
        self.assertEqual([n[0] for n in self.notes], ["Phone camera: recording"])
        self.assertIn("3 MB free", self.notes[0][1])
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.assertEqual(self.controller.warnings, {})

    def test_obs_unreachable_for_a_while_warns_once(self):
        self.controller.obs_down(30)
        self.assertEqual(self.notes, [])
        self.controller.obs_down(130)
        self.controller.obs_down(160)
        self.assertEqual([n[0] for n in self.notes], ["Phone camera: obs"])
        self.controller.obs_up()
        self.assertEqual(self.controller.warnings, {})

    def test_obs_process_gone_for_a_while_ends_the_stream_and_the_recording(self):
        self.controller.set_stream(True)
        self.controller.obs_down(10)            # first sign: the process check starts the clock
        self.assertTrue(self.controller.stream_active)
        self.clock[0] += daemon.OBS_LOST_STREAM_S - 5
        self.controller.obs_down(180)
        self.assertTrue(self.controller.stream_active)      # not long enough yet
        self.clock[0] += 10
        self.controller.obs_down(190)
        self.assertFalse(self.controller.stream_active)
        self.assertEqual(len(self.notes), 1)
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.assertTrue(any("REC_STOP" in c for c in self.phone.commands))

    def test_obs_process_still_running_keeps_the_recording_and_only_warns(self):
        self.obs_alive = True
        self.controller.set_stream(True)
        self.controller.obs_down(10)
        self.clock[0] += 1000
        self.controller.obs_down(1010)
        self.assertTrue(self.controller.stream_active)
        self.assertEqual([n[0] for n in self.notes], ["Phone camera: obs"])
        self.assertIn("websocket does not answer", self.notes[0][1])
        self.phone.status = {"running": True, "recording": True}
        self.tick()
        self.assertFalse(any("REC_STOP" in c for c in self.phone.commands))

    def test_a_returning_obs_process_resets_the_gone_clock(self):
        self.controller.set_stream(True)
        self.controller.obs_down(10)
        self.clock[0] += 150
        self.obs_alive = True
        self.controller.obs_down(160)            # OBS is back as a process (websocket still down)
        self.obs_alive = False
        self.clock[0] += 100
        self.controller.obs_down(260)            # gone again, but only for 100 s
        self.assertTrue(self.controller.stream_active)

    def test_warnings_are_written_for_phone_cam_status(self):
        path = self.root / "daemon.json"
        self.controller.warnings_file = path
        self.controller.obs_down(130)
        self.assertIn("OBS is not running", json.loads(path.read_text())["warnings"][0])
        self.controller.obs_up()
        self.assertEqual(json.loads(path.read_text()), {"warnings": []})

    def test_phone_away_while_recording_is_wanted_warns(self):
        self.controller.set_stream(True)
        self.phone.unreachable = True
        self.tick(5)
        self.assertEqual(self.notes, [])
        self.tick(70)
        self.assertEqual([n[0] for n in self.notes], ["Phone camera: phone"])


class RecCommandTests(unittest.TestCase):
    def test_session_id_is_the_local_start_time(self):
        self.assertRegex(rec.new_session(0), r"^\d{8}-\d{6}$")

    def test_error_output_from_am_is_raised(self):
        phone = FakeAppPhone()
        phone.shell = lambda command, **kw: "Error: app is in background uid null\n"
        with self.assertRaises(adbphone.AdbError):
            rec.start_recording(phone, "20261107-203015")

    def test_start_command_names_the_service_action_and_session(self):
        phone = FakeAppPhone()
        rec.start_recording(phone, "20261107-203015")
        self.assertIn("-a io.github.rlbroadcastkit.phonecam.REC_START", phone.commands[0])
        self.assertIn("--es session 20261107-203015", phone.commands[0])

    def test_stopping_an_app_that_is_not_running_is_not_an_error(self):
        phone = FakeAppPhone()
        phone.shell = lambda command, **kw: "Error: app is in background uid null\n"
        rec.stop_app(phone)


class StoreTests(unittest.TestCase):
    def test_manual_flag_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = state.Store(Path(tmp) / "s")
            self.assertIsNone(store.read_manual())
            store.write_manual("20261107-100000")
            self.assertEqual(store.read_manual(), "20261107-100000")
            store.clear_manual()
            self.assertIsNone(store.read_manual())


if __name__ == "__main__":
    unittest.main()
