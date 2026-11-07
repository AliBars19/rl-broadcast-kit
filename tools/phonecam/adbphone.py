"""adbphone.py: the phone as the offload code sees it, plus the commands that control the app."""
import os
import select
import shlex
import subprocess
from typing import NamedTuple

import chunks

ADB_TIMEOUT_S = 30
SERIAL_ENV = "PHONECAM_SERIAL"
STALL_S = 60.0   # a transfer that delivers nothing for this long is dead (adb hung, cable half out)


def default_serial(run=subprocess.run, environ=None) -> str | None:
    """PHONECAM_SERIAL, else the ready device that has the PhoneCam app installed, else the first ready one.

    The same rule as adbx_init in system/bin/phone-cam."""
    import os
    env = os.environ if environ is None else environ
    if env.get(SERIAL_ENV):
        return env[SERIAL_ENV]
    try:
        out = run(["adb", "devices"], capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    ready = [f[0] for f in (line.split() for line in out.splitlines()[1:]) if len(f) >= 2 and f[1] == "device"]
    for serial in ready:
        if _has_app(run, serial):
            return serial
    return ready[0] if ready else None


def _has_app(run, serial: str) -> bool:
    try:
        done = run(["adb", "-s", serial, "shell", "pm list packages %s" % chunks.PACKAGE], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return ("package:%s" % chunks.PACKAGE) in done.stdout.decode("utf-8", "replace").split()


class AdbError(Exception):
    """An adb call failed. The message is safe to log."""


class RemoteChunk(NamedTuple):
    name: str
    path: str
    size: int


class AdbPhone:
    def __init__(self, adb: str = "adb", serial: str | None = None, remote_dir: str = chunks.REMOTE_DIR,
                 run=subprocess.run, popen=subprocess.Popen):
        self.adb = adb
        self.serial = serial
        self._auto = serial is None and run is subprocess.run  # tests inject `run` and want no detection
        self.remote_dir = remote_dir
        self._run = run
        self._popen = popen

    def _base(self) -> list:
        if self.serial is None and self._auto:
            self.serial = default_serial()  # stays None while no device is ready, so we look again next time
        return [self.adb] + (["-s", self.serial] if self.serial else [])

    def run(self, *args: str, timeout: float = ADB_TIMEOUT_S, check: bool = True) -> str:
        cmd = self._base() + list(args)
        try:
            done = self._run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError("adb %s failed: %s" % (" ".join(args[:3]), exc))
        text = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else str(done.stdout)
        if check and done.returncode != 0:
            err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else str(done.stderr)
            raise AdbError("adb %s exited %d: %s" % (" ".join(args[:3]), done.returncode, (err or text).strip()[:200]))
        return text

    def shell(self, command: str, **kwargs) -> str:
        return self.run("shell", command, **kwargs)

    def shell_rc(self, command: str, timeout: float = ADB_TIMEOUT_S) -> tuple:
        """(exit status of the remote command, its output); raises AdbError only when adb itself fails."""
        cmd = self._base() + ["shell", command]
        try:
            done = self._run(cmd, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError("adb shell failed: %s" % exc)
        text = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else str(done.stdout)
        return done.returncode, text

    def state(self) -> str:
        try:
            return self.run("get-state", timeout=10, check=False).strip() or "none"
        except AdbError:
            return "none"

    # --- offload interface ---

    def list_chunks(self) -> list:
        names = "\\( -name '*.mp4' -o -name '*.mp4.broken*' -o -name '*.mp4.orphan*' \\)"
        command = "find %s -maxdepth 1 %s -exec stat -c '%%s %%n' {} +" % (shlex.quote(self.remote_dir), names)
        code, out = self.shell_rc(command)
        if code != 0 and "No such file" not in out:
            # An adb failure must not look like an empty phone: the caller would think the offload is done.
            raise AdbError("cannot list the phone's chunk folder (exit %d): %s" % (code, out.strip()[:150]))
        found = []
        for line in out.splitlines():
            size, _, path = line.strip().partition(" ")
            if size.isdigit() and path.startswith(self.remote_dir + "/"):
                found.append(RemoteChunk(path.rsplit("/", 1)[1], path, int(size)))
        return sorted(found)

    def exists(self, path: str) -> bool:
        code, _ = self.shell_rc("test -e %s" % shlex.quote(path))
        return code == 0

    def count_partials(self) -> int:
        """Chunks still being written or closed (NAME.mp4.part). Fails closed: an error is never read as zero."""
        command = "n=$(ls %s 2>/dev/null | grep -c '\\.mp4\\.part$'); echo $n" % shlex.quote(self.remote_dir)
        code, out = self.shell_rc(command)
        text = out.strip()
        if code != 0 or not text.isdigit():
            raise AdbError("cannot count unfinished chunks (exit %d): %s" % (code, text[:100]))
        return int(text)

    def sha256(self, path: str) -> str:
        out = self.shell("sha256sum %s" % shlex.quote(path), timeout=300).split()
        if not out or len(out[0]) != 64:
            raise AdbError("unexpected sha256sum output for %s" % path)
        return out[0]

    def open_stream(self, path: str, stall_s: float = STALL_S):
        return _RemoteStream(self._popen(self._base() + ["exec-out", "cat", path], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE), path, stall_s)

    def delete(self, path: str) -> None:
        self.shell("rm %s" % shlex.quote(path))

    # --- app control ---

    def read_status(self) -> dict | None:
        """The app's status.json, or None when the app is not running (the file outlives a crash)."""
        import json
        state = self.state()
        if state != "device":
            raise AdbError("phone not connected (adb state %s)" % state)
        if not self.shell("pidof %s" % chunks.PACKAGE, check=False).strip():
            return None
        text = self.shell("cat %s/../status.json" % shlex.quote(self.remote_dir), check=False).strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


class _RemoteStream:
    """File-like wrapper over `adb exec-out cat`; closing it checks the exit status."""

    def __init__(self, process, path: str, stall_s: float = STALL_S):
        self.process = process
        self.path = path
        self.stall_s = stall_s

    def read(self, size: int) -> bytes:
        fd = self.process.stdout.fileno()
        poller = select.poll()  # poll, not select: select breaks for descriptors above 1023
        poller.register(fd, select.POLLIN | select.POLLHUP)
        if not poller.poll(int(self.stall_s * 1000)):
            self.process.kill()  # also releases the folder lock the caller holds
            raise AdbError("adb transfer of %s delivered nothing for %d s; giving up" % (self.path, self.stall_s))
        return os.read(fd, size)

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        self.process.stdout.close()
        if kind is not None:
            self.process.kill()
        code = self.process.wait()
        if kind is None and code != 0:
            err = self.process.stderr.read().decode("utf-8", "replace").strip()[:200]
            raise AdbError("adb exec-out cat %s exited %d: %s" % (self.path, code, err))
        self.process.stderr.close()
        return False
