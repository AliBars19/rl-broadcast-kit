"""Tests for chunks.py, adbphone.py and offload.py with a fake phone (no adb, no USB)."""
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adbphone  # noqa: E402
import chunks  # noqa: E402
import offload  # noqa: E402

NAME1 = "cam-20261107-203015-0001-1794062677451.mp4"
NAME2 = "cam-20261107-203015-0002-1794062977451.mp4"
REMOTE = "/sdcard/Android/data/io.github.rlbroadcastkit.phonecam/files/chunks"


class FakeStream:
    def __init__(self, data: bytes, fail_on_exit=False):
        self.buf = io.BytesIO(data)
        self.fail_on_exit = fail_on_exit

    def read(self, size):
        return self.buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        if self.fail_on_exit and kind is None:
            raise adbphone.AdbError("adb died")
        return False


class FakePhone:
    """A dict of files standing in for the phone, with switches to break the transfer."""

    def __init__(self, files: dict):
        self.files = dict(files)
        self.events = []
        self.corrupt = False
        self.truncate = False
        self.stream_fails = False
        self.delete_fails = False

    def list_chunks(self):
        return sorted(adbphone.RemoteChunk(n, REMOTE + "/" + n, len(d)) for n, d in self.files.items())

    def sha256(self, path):
        return hashlib.sha256(self.files[path.rsplit("/", 1)[1]]).hexdigest()

    def open_stream(self, path):
        self.events.append("pull")
        data = self.files[path.rsplit("/", 1)[1]]
        if self.corrupt:
            data = bytes([data[0] ^ 1]) + data[1:]
        if self.truncate:
            data = data[:-10]
        return FakeStream(data, self.stream_fails)

    def exists(self, path):
        return path.rsplit("/", 1)[1] in self.files

    def count_partials(self):
        return 0

    def delete(self, path):
        self.events.append("delete")
        if self.delete_fails:
            raise adbphone.AdbError("rm failed")
        del self.files[path.rsplit("/", 1)[1]]


class OffloadBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = []
        self.data = os.urandom(3 * (1 << 20) + 123)

    def tearDown(self):
        self.tmp.cleanup()

    def sweep(self, phone, limit=0.0):
        return offload.offload_all(phone, self.root, limit, self.logs.append, sleep=lambda s: None)

    @property
    def cam(self):
        return self.root / "2026-11-07" / "cam"


class OffloadTests(OffloadBase):
    def test_verified_chunk_lands_in_the_stream_cam_folder_and_leaves_the_phone(self):
        phone = FakePhone({NAME1: self.data})
        results = self.sweep(phone)
        self.assertEqual([r.status for r in results], ["done"])
        self.assertEqual((self.cam / NAME1).read_bytes(), self.data)
        self.assertEqual(phone.files, {})

    def test_phone_copy_is_deleted_only_after_the_file_is_in_place(self):
        phone = FakePhone({NAME1: self.data})
        original = phone.delete

        def checked(path):
            self.assertTrue((self.cam / NAME1).exists(), "deleted before the verified file was in place")
            self.assertFalse(list(self.cam.glob(".*.part")))
            original(path)
        phone.delete = checked
        self.sweep(phone)
        self.assertEqual(phone.events, ["pull", "delete"])

    def test_corrupted_transfer_is_rejected_and_kept_on_the_phone(self):
        phone = FakePhone({NAME1: self.data})
        phone.corrupt = True
        results = self.sweep(phone)
        self.assertEqual(results[0].status, "mismatch")
        self.assertIn(NAME1, phone.files)
        self.assertEqual([p.name for p in self.cam.glob("*") if p.name != ".offload.lock"], [])
        self.assertNotIn("delete", phone.events)
        self.assertTrue(any("MISMATCH" in line for line in self.logs))

    def test_truncated_transfer_is_rejected(self):
        phone = FakePhone({NAME1: self.data})
        phone.truncate = True
        self.assertEqual(self.sweep(phone)[0].status, "mismatch")
        self.assertIn(NAME1, phone.files)

    def test_stream_failure_keeps_the_phone_copy_and_cleans_up(self):
        phone = FakePhone({NAME1: self.data})
        phone.stream_fails = True
        self.assertEqual(self.sweep(phone)[0].status, "error")
        self.assertIn(NAME1, phone.files)
        self.assertEqual([p.name for p in self.cam.glob("*") if p.name != ".offload.lock"], [])

    def test_failed_delete_leaves_a_verified_copy_and_reports_it(self):
        phone = FakePhone({NAME1: self.data})
        phone.delete_fails = True
        result = self.sweep(phone)[0]
        self.assertEqual(result.status, "error")
        self.assertEqual((self.cam / NAME1).read_bytes(), self.data)

    def test_identical_copy_already_on_the_pc_is_not_pulled_again(self):
        self.cam.mkdir(parents=True)
        (self.cam / NAME1).write_bytes(self.data)
        phone = FakePhone({NAME1: self.data})
        self.assertEqual(self.sweep(phone)[0].status, "already-there")
        self.assertEqual(phone.events, ["delete"])

    def test_different_file_with_the_same_name_is_never_overwritten_or_deleted_from_the_phone(self):
        self.cam.mkdir(parents=True)
        (self.cam / NAME1).write_bytes(b"something else")
        phone = FakePhone({NAME1: self.data})
        self.assertEqual(self.sweep(phone)[0].status, "conflict")
        self.assertEqual((self.cam / NAME1).read_bytes(), b"something else")
        self.assertIn(NAME1, phone.files)

    def test_unknown_names_are_left_alone(self):
        phone = FakePhone({"cam-20261107-test-0001-1794062677451.mp4": b"x"})
        self.assertEqual(self.sweep(phone)[0].status, "unknown-name")
        self.assertEqual(len(phone.files), 1)

    def test_a_bad_chunk_is_skipped_and_the_rest_still_arrive(self):
        phone = FakePhone({NAME1: self.data, NAME2: self.data})
        original = phone.open_stream

        def corrupt_first(path):
            phone.corrupt = path.endswith(NAME1)
            return original(path)
        phone.open_stream = corrupt_first
        results = self.sweep(phone)
        self.assertEqual([r.status for r in results], ["mismatch", "done"])
        self.assertIn(NAME1, phone.files)
        self.assertNotIn(NAME2, phone.files)

    def test_three_pull_errors_in_a_row_end_the_sweep(self):
        names = ["cam-20261107-203015-%04d-%d.mp4" % (i, 1794062677451 + i) for i in range(1, 6)]
        phone = FakePhone({n: self.data for n in names})
        phone.stream_fails = True
        self.assertEqual(len(self.sweep(phone)), 3)

    def test_broken_and_orphan_chunks_are_pulled_into_cam_broken(self):
        broken = NAME1 + ".broken"
        orphan = NAME2 + ".orphan"
        phone = FakePhone({broken: self.data, orphan: b"half a file"})
        results = self.sweep(phone)
        self.assertEqual([r.status for r in results], ["done", "done"])
        self.assertEqual((self.cam / "broken" / broken).read_bytes(), self.data)
        self.assertEqual((self.cam / "broken" / orphan).read_bytes(), b"half a file")
        self.assertEqual(phone.files, {})
        self.assertEqual(chunks.list_local_chunks(self.cam), [])

    def test_the_destination_folder_is_synced_before_the_phone_copy_is_deleted(self):
        phone = FakePhone({NAME1: self.data})
        synced = []
        with mock.patch.object(offload, "_fsync_dir", side_effect=lambda d: synced.append((d, list(phone.files)))):
            self.sweep(phone)
        self.assertEqual(synced, [(self.cam, [NAME1])])

    def test_two_chunks_go_in_order(self):
        phone = FakePhone({NAME2: self.data, NAME1: self.data})
        self.assertEqual([r.name for r in self.sweep(phone)], [NAME1, NAME2])

    def test_midnight_session_uses_the_date_of_the_stream_start(self):
        name = "cam-20261107-235900-0003-1794100000000.mp4"
        self.sweep(FakePhone({name: b"abc"}))
        self.assertTrue((self.cam / name).exists())


class ConcurrentOffloadTests(OffloadBase):
    def test_two_offloaders_never_touch_each_others_file_and_the_phone_copy_goes_once(self):
        phone = FakePhone({NAME1: self.data})
        inside = threading.Event()
        proceed = threading.Event()
        original = phone.open_stream

        class SlowStream:
            def __init__(self, inner):
                self.inner = inner

            def read(self, size):
                inside.set()
                proceed.wait(10)
                return self.inner.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        phone.open_stream = lambda path: SlowStream(original(path))
        first = []
        worker = threading.Thread(target=lambda: first.extend(self.sweep(phone)))
        worker.start()
        self.assertTrue(inside.wait(5))
        second = self.sweep(phone)               # `phone-cam offload` while the daemon is mid-transfer
        self.assertEqual([r.status for r in second], ["busy"])
        proceed.set()
        worker.join(10)
        self.assertEqual([r.status for r in first], ["done"])
        self.assertEqual((self.cam / NAME1).read_bytes(), self.data)
        self.assertEqual(phone.events.count("delete"), 1)
        self.assertEqual(phone.events.count("pull"), 1)
        self.assertEqual([p.name for p in self.cam.iterdir() if p.name.startswith(".") and p.name != ".offload.lock"], [])

    def test_a_chunk_already_moved_by_the_other_offloader_is_reported_not_failed(self):
        phone = FakePhone({NAME1: self.data})
        remote = phone.list_chunks()[0]
        self.sweep(phone)
        result = offload.offload_chunk(phone, remote, self.root, 0.0, self.logs.append)
        self.assertEqual(result.status, "gone")

    def test_busy_does_not_count_as_an_error_streak(self):
        names = ["cam-20261107-203015-%04d-%d.mp4" % (i, 1794062677451 + i) for i in range(1, 6)]
        phone = FakePhone({n: self.data for n in names})
        lock = offload._FolderLock(self.cam / offload.LOCK_NAME)
        self.cam.mkdir(parents=True)
        self.assertTrue(lock.try_acquire())
        results = self.sweep(phone)
        lock.release()
        self.assertEqual([r.status for r in results], ["busy"] * 5)

    def test_the_temporary_file_is_opened_exclusively(self):
        path = self.root / "x"
        path.write_bytes(b"other writer")
        with self.assertRaises(FileExistsError):
            offload.stream_to_file(FakeStream(b"abc"), path, 0)
        self.assertEqual(path.read_bytes(), b"other writer")


class AlreadyThereDurabilityTests(OffloadBase):
    def test_the_folder_is_synced_before_the_phone_copy_of_an_existing_file_is_deleted(self):
        self.cam.mkdir(parents=True)
        (self.cam / NAME1).write_bytes(self.data)
        phone = FakePhone({NAME1: self.data})
        order = []
        with mock.patch.object(offload, "_fsync_dir", side_effect=lambda d: order.append("fsync")):
            original = phone.delete
            phone.delete = lambda path: (order.append("delete"), original(path))
            self.sweep(phone)
        self.assertEqual(order, ["fsync", "delete"])


class PcSideErrorTests(OffloadBase):
    def test_a_failing_replace_is_an_error_result_not_a_dead_sweep(self):
        phone = FakePhone({NAME1: self.data})
        with mock.patch.object(offload.os, "replace", side_effect=OSError(28, "No space left on device")):
            results = self.sweep(phone)
        self.assertEqual(results[0].status, "error")
        self.assertIn("No space", results[0].detail)
        self.assertIn(NAME1, phone.files)
        self.assertEqual([p.name for p in self.cam.iterdir() if p.name.startswith(".cam")], [])

    def test_a_folder_that_cannot_be_created_is_an_error_result(self):
        phone = FakePhone({NAME1: self.data})
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError(13, "denied")):
            results = self.sweep(phone)
        self.assertEqual(results[0].status, "error")
        self.assertIn(NAME1, phone.files)

    def test_a_failing_stat_of_an_existing_file_is_an_error_result(self):
        self.cam.mkdir(parents=True)
        (self.cam / NAME1).write_bytes(self.data)
        phone = FakePhone({NAME1: self.data})
        with mock.patch.object(offload, "file_sha256", side_effect=OSError(5, "I/O error")):
            results = self.sweep(phone)
        self.assertEqual(results[0].status, "error")
        self.assertIn(NAME1, phone.files)

    def test_stale_partial_files_of_a_killed_offloader_are_cleaned(self):
        self.cam.mkdir(parents=True)
        (self.cam / ("." + NAME1 + ".part.deadbeef")).write_bytes(b"old")
        self.sweep(FakePhone({NAME1: self.data}))
        self.assertEqual([p.name for p in self.cam.iterdir() if p.name.startswith(".cam")], [])


class ThrottleTests(unittest.TestCase):
    def test_rate_limit_sleeps_to_the_requested_pace(self):
        now = [0.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        with tempfile.TemporaryDirectory() as tmp:
            data = b"x" * (4 * offload.BLOCK)
            size, digest = offload.stream_to_file(FakeStream(data), Path(tmp) / "f", 2 * offload.BLOCK,
                                                  clock=lambda: now[0], sleep=sleep)
        self.assertEqual(size, len(data))
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertAlmostEqual(sum(slept), 2.0, places=3)

    def test_no_limit_never_sleeps(self):
        with tempfile.TemporaryDirectory() as tmp:
            offload.stream_to_file(FakeStream(b"x" * 100), Path(tmp) / "f", 0, sleep=lambda s: self.fail("slept"))


class ChunkNameTests(unittest.TestCase):
    def test_parse(self):
        parsed = chunks.parse_chunk_name(NAME1)
        self.assertEqual((parsed.session, parsed.date, parsed.index, parsed.epoch_ms),
                         ("20261107-203015", "2026-11-07", 1, 1794062677451))

    def test_broken_and_orphan_suffixes_are_recognised(self):
        self.assertEqual(chunks.parse_chunk_name(NAME1 + ".broken").kind, "broken")
        self.assertEqual(chunks.parse_chunk_name(NAME1 + ".orphan").kind, "orphan")
        self.assertEqual(chunks.parse_chunk_name(NAME1).kind, "ok")

    def test_duplicate_names_made_by_the_phone_still_parse(self):
        self.assertEqual(chunks.parse_chunk_name(NAME1 + ".orphan.1").kind, "orphan")
        self.assertEqual(chunks.parse_chunk_name(NAME1 + ".broken.12").kind, "broken")
        self.assertIsNone(chunks.parse_chunk_name(NAME1 + ".1"))

    def test_rejects_part_files_and_test_sessions(self):
        self.assertIsNone(chunks.parse_chunk_name(NAME1 + ".part"))
        self.assertIsNone(chunks.parse_chunk_name("cam-20261107-test-0001-1.mp4"))

    def test_local_chunks_sort_by_first_frame_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in (NAME2, NAME1):
                (Path(tmp) / name).write_bytes(b"x")
            self.assertEqual([p.name for p in chunks.list_local_chunks(Path(tmp))], [NAME1, NAME2])


class AdbPhoneTests(unittest.TestCase):
    def phone(self, answers=None, code=0, state="device"):
        """A fake adb: `answers` maps a keyword of the shell command to its output."""
        calls = []
        answers = answers or {}

        def run(cmd, capture_output=True, timeout=None):
            calls.append(cmd)
            if "get-state" in cmd:
                return subprocess.CompletedProcess(cmd, 0 if state == "device" else 1, state.encode(), b"")
            text = cmd[-1]
            for key, out in answers.items():
                if key in text:
                    return subprocess.CompletedProcess(cmd, code, out.encode(), b"boom")
            return subprocess.CompletedProcess(cmd, code, b"", b"boom")
        return adbphone.AdbPhone(serial="SER", run=run), calls

    def test_list_asks_for_finished_chunks_and_names_the_serial(self):
        phone, calls = self.phone({"find": "100 %s/%s\n" % (REMOTE, NAME1)})
        self.assertEqual(phone.list_chunks(), [adbphone.RemoteChunk(NAME1, "%s/%s" % (REMOTE, NAME1), 100)])
        self.assertIn("-name '*.mp4.broken*'", calls[0][-1])
        self.assertIn("-name '*.mp4.orphan*'", calls[0][-1])
        self.assertEqual(calls[0][:3], ["adb", "-s", "SER"])

    def test_every_call_names_the_serial(self):
        phone, calls = self.phone({"pidof": "1", "cat": "{}"})
        phone.read_status()
        phone.state()
        self.assertTrue(all(c[1:3] == ["-s", "SER"] for c in calls))

    def test_missing_directory_means_no_chunks(self):
        phone, _ = self.phone({"find": "find: No such file or directory\n"}, code=1)
        self.assertEqual(phone.list_chunks(), [])

    def test_an_adb_failure_is_not_an_empty_phone(self):
        phone, _ = self.phone({"find": "error: device offline\n"}, code=1)
        with self.assertRaises(adbphone.AdbError):
            phone.list_chunks()

    def test_sha256_parsing_and_bad_output(self):
        phone, _ = self.phone({"sha256sum": "a" * 64 + "  /x\n"})
        self.assertEqual(phone.sha256("/x"), "a" * 64)
        bad, _ = self.phone({"sha256sum": "sha256sum: x: Permission denied\n"})
        with self.assertRaises(adbphone.AdbError):
            bad.sha256("/x")

    def test_adb_failure_raises_with_context(self):
        phone, _ = self.phone(code=1)
        with self.assertRaises(adbphone.AdbError) as ctx:
            phone.delete("/x")
        self.assertIn("boom", str(ctx.exception))

    def test_status_is_parsed_or_none_when_the_app_is_not_running(self):
        good, _ = self.phone({"pidof": "4242\n", "cat": '{"running": true}'})
        self.assertEqual(good.read_status(), {"running": True})
        stale, _ = self.phone({"cat": '{"running": true, "recording": true}'})  # no pid: the app crashed
        self.assertIsNone(stale.read_status())

    def test_an_unplugged_phone_is_an_error_not_a_stopped_app(self):
        phone, _ = self.phone({"pidof": ""}, state="offline")
        with self.assertRaises(adbphone.AdbError):
            phone.read_status()

    def test_default_serial_prefers_the_environment_then_the_device_with_the_app_then_the_first_ready(self):
        def run(cmd, **kw):
            if cmd[1] == "devices":
                return subprocess.CompletedProcess(
                    cmd, 0, b"List of devices attached\nAAA\toffline\nBBB\tdevice\nCCC\tdevice\n", b"")
            has_app = cmd[2] == "CCC"                      # only CCC has PhoneCam installed
            return subprocess.CompletedProcess(cmd, 0, b"package:io.github.rlbroadcastkit.phonecam\n" if has_app else b"", b"")
        self.assertEqual(adbphone.default_serial(run, {}), "CCC")
        self.assertEqual(adbphone.default_serial(run, {"PHONECAM_SERIAL": "ZZZ"}), "ZZZ")
        nobody = lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, b"List of devices attached\nBBB\tdevice\nCCC\tdevice\n" if cmd[1] == "devices" else b"", b"")
        self.assertEqual(adbphone.default_serial(nobody, {}), "BBB")     # app not installed yet: first ready device
        none = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"List of devices attached\n\n", b"")
        self.assertIsNone(adbphone.default_serial(none, {}))

    def test_exists_and_partials_use_the_shell(self):
        phone, _ = self.phone({"test -e": ""}, code=0)
        self.assertTrue(phone.exists("/x"))
        gone, _ = self.phone({"test -e": ""}, code=1)
        self.assertFalse(gone.exists("/x"))
        counted, _ = self.phone({"grep -c": "2\n"})
        self.assertEqual(counted.count_partials(), 2)

    def test_count_partials_fails_closed(self):
        failing, _ = self.phone({"grep -c": "error: device offline\n"}, code=1)
        with self.assertRaises(adbphone.AdbError):
            failing.count_partials()
        garbled, _ = self.phone({"grep -c": "oops\n"})
        with self.assertRaises(adbphone.AdbError):
            garbled.count_partials()
        empty, _ = self.phone({"grep -c": ""})
        with self.assertRaises(adbphone.AdbError):
            empty.count_partials()


class RemoteStreamTests(unittest.TestCase):
    def test_data_is_read_through(self):
        process = subprocess.Popen(["printf", "abcdef"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with adbphone._RemoteStream(process, "/x", 5.0) as stream:
            data = b""
            while True:
                block = stream.read(4)
                if not block:
                    break
                data += block
        self.assertEqual(data, b"abcdef")

    def test_a_transfer_that_delivers_nothing_is_cut_off(self):
        process = subprocess.Popen(["sleep", "30"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        begin = time.monotonic()
        with self.assertRaises(adbphone.AdbError) as ctx:
            with adbphone._RemoteStream(process, "/x", 0.3) as stream:
                stream.read(10)
        self.assertLess(time.monotonic() - begin, 5)
        self.assertIn("delivered nothing", str(ctx.exception))
        self.assertIsNotNone(process.poll())     # the hung adb process was killed


if __name__ == "__main__":
    unittest.main()
