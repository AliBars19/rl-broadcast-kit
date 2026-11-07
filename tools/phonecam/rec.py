"""rec.py: start and stop the phone app's recording, launch and stop the app (all through adb)."""
import shlex
import time

import chunks
from adbphone import AdbError

SERVICE = "%s/.CamService" % chunks.PACKAGE
ACTIVITY = "%s/.LaunchActivity" % chunks.PACKAGE
ACTION = chunks.PACKAGE + ".%s"


def new_session(now=None) -> str:
    """Session ids are the local start time; their date names the stream folder."""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def _check(output: str, what: str) -> None:
    if "Error" in output or "not allowed" in output.lower() or "Exception" in output:
        raise AdbError("%s failed: %s" % (what, output.strip()[:200]))


def send(phone, action: str, **extras: str) -> None:
    cmd = "am startservice -n %s -a %s" % (SERVICE, ACTION % action)
    for key, value in extras.items():
        cmd += " --es %s %s" % (key, shlex.quote(str(value)))
    _check(phone.shell(cmd), "am startservice %s" % action)


def start_recording(phone, session: str) -> None:
    send(phone, "REC_START", session=session)


def stop_recording(phone) -> None:
    send(phone, "REC_STOP")


def stop_app(phone) -> None:
    """The service may already be gone: a 'not running' answer is fine here."""
    try:
        send(phone, "STOP")
    except AdbError:
        pass


def launch_app(phone, **extras: str) -> None:
    """Bring the app to the foreground for a moment so Android lets its service open the camera."""
    cmd = "am start -n %s" % ACTIVITY
    for key, value in extras.items():
        cmd += " --es %s %s" % (key, shlex.quote(str(value)))
    _check(phone.shell(cmd), "am start")

