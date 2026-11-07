"""lowprio.py: put this process at idle CPU and disk priority (nice 19, ionice class 3)."""
import os
import subprocess


def lower_priority(log=lambda message: None) -> None:
    """Children inherit both, so adb and sha256 work started afterwards stays out of the way."""
    try:
        os.nice(19)
    except OSError as exc:
        log("cannot lower CPU priority: %s" % exc)
    try:
        subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        log("cannot lower disk priority: %s" % exc)
