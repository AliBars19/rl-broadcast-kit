"""Tests for phonecam.py (the command line): argument handling and rec start/stop with a fake phone."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import phonecam  # noqa: E402
import state  # noqa: E402


class FakePhone:
    def __init__(self):
        self.commands = []

    def shell(self, command, **kw):
        self.commands.append(command)
        return "Starting service\n"

    def read_status(self):
        return {"running": True, "recording": True}


class ResolveTests(unittest.TestCase):
    def test_date_folder_and_cam_folder_resolve_to_the_stream_folder(self):
        root = Path("/videos")
        self.assertEqual(phonecam.resolve_stream_dir("2026-11-07", root), Path("/videos/2026-11-07"))
        self.assertEqual(phonecam.resolve_stream_dir("/videos/2026-11-07/cam", root),
                         Path("/videos/2026-11-07"))

    def test_default_is_the_newest_stream_with_a_cam_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-10-27").mkdir()
            (root / "2026-11-03" / "cam").mkdir(parents=True)
            (root / "2026-11-06").mkdir()
            self.assertEqual(phonecam.resolve_stream_dir(None, root).name, "2026-11-03")

    def test_nothing_to_resolve_exits_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            phonecam.resolve_stream_dir(None, Path(tmp))


class OffloadCommandTests(unittest.TestCase):
    def run_cli(self, results):
        out = io.StringIO()
        with mock.patch.object(phonecam.offload, "offload_all", return_value=results), \
                mock.patch.object(phonecam.adbphone, "AdbPhone", return_value=FakePhone()), \
                mock.patch.object(phonecam.lowprio, "lower_priority"), contextlib.redirect_stdout(out):
            return phonecam.main(["offload"]), out.getvalue()

    def test_chunks_held_by_the_daemon_are_not_a_failure(self):
        R = phonecam.offload.Result
        code, out = self.run_cli([R("a", "busy"), R("b", "done"), R("c", "gone")])
        self.assertEqual(code, 0)
        self.assertIn("daemon", out)

    def test_a_mismatch_is_a_failure_exit(self):
        code, _ = self.run_cli([phonecam.offload.Result("a", "mismatch")])
        self.assertEqual(code, 1)


class RecTests(unittest.TestCase):
    def run_cli(self, *argv):
        phone = FakePhone()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PHONECAM_STATE": tmp}), \
                mock.patch.object(phonecam.adbphone, "AdbPhone", return_value=phone), contextlib.redirect_stdout(out):
            code = phonecam.main(list(argv))
            flag = state.Store().read_manual()
        return code, phone, out.getvalue(), flag

    def test_rec_start_sends_the_intent_and_leaves_a_manual_flag(self):
        code, phone, out, flag = self.run_cli("rec", "start")
        self.assertEqual(code, 0)
        self.assertIn("REC_START", phone.commands[0])
        self.assertRegex(flag, r"^\d{8}-\d{6}$")
        self.assertIn(flag, phone.commands[0])

    def test_rec_stop_sends_the_intent_and_clears_the_flag(self):
        phone = FakePhone()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PHONECAM_STATE": tmp}), \
                mock.patch.object(phonecam.adbphone, "AdbPhone", return_value=phone), contextlib.redirect_stdout(io.StringIO()):
            state.Store().write_manual("20261107-100000")
            self.assertEqual(phonecam.main(["rec", "stop"]), 0)
            self.assertIsNone(state.Store().read_manual())
        self.assertIn("REC_STOP", phone.commands[0])

    def test_adb_failure_is_a_clean_error_exit(self):
        phone = FakePhone()
        phone.shell = lambda c, **kw: "Error: app is in background uid null"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PHONECAM_STATE": tmp}), \
                mock.patch.object(phonecam.adbphone, "AdbPhone", return_value=phone), \
                contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(phonecam.main(["rec", "start"]), 1)
        self.assertIn("phone-cam:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
