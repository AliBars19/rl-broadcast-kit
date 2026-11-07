"""state.py: the small files the CLI and the daemon share (a manual recording request)."""
import os
from pathlib import Path


def runtime_dir() -> Path:
    """Where the supervisor keeps its pid, log and status files (XDG_RUNTIME_DIR, else /tmp)."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")


def state_dir() -> Path:
    base = os.environ.get("PHONECAM_STATE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), "phonecam")
    return Path(base)


class Store:
    """`manual-session` exists while a recording was started by hand (`phone-cam rec start`)."""

    def __init__(self, directory: Path | None = None):
        self.dir = directory or state_dir()

    @property
    def _manual(self) -> Path:
        return self.dir / "manual-session"

    def read_manual(self) -> str | None:
        try:
            text = self._manual.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return text or None

    def write_manual(self, session: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._manual.with_suffix(".tmp")
        tmp.write_text(session + "\n", encoding="utf-8")
        os.replace(tmp, self._manual)

    def clear_manual(self) -> None:
        self._manual.unlink(missing_ok=True)
