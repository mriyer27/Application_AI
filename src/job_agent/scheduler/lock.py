"""Single-instance guard: an OS-level exclusive lock on ``<data_dir>/daemon.lock``.

The lock is held by the open file handle (``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows), so the operating system releases it
automatically when the process dies -- a crashed or killed daemon never leaves
a stale lock behind. The file only records who holds it, for error messages;
it is truncated (not deleted) on release so no other process can be holding a
lock on an unlinked file.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import IO, Any

from job_agent.config import AppConfig
from job_agent.errors import JobAgentError
from job_agent.logging_config import get_logger
from job_agent.models import ErrorKind
from job_agent.utils.time import utcnow_iso

log = get_logger(__name__)

LOCK_FILE_NAME = "daemon.lock"
#: Exit code used by the CLI when another daemon is running; autostart units
#: treat it as "do not restart" (systemd RestartPreventExitStatus).
ALREADY_RUNNING_EXIT_CODE = 3
# On Windows the locked byte range cannot be read by other processes, so lock a
# byte far past the JSON content; the holder info stays readable.
_WINDOWS_LOCK_OFFSET = 1 << 30


class DaemonAlreadyRunning(JobAgentError):
    kind = ErrorKind.HUMAN_ACTION_REQUIRED
    human_action = ("Another job-agent daemon is already running. Check it with `job-agent schedule`; stop it "
                    "(Ctrl+C in its window, or through your autostart tool) before starting a new one.")


def daemon_lock_path(config: AppConfig) -> Path:
    return config.data_dir / LOCK_FILE_NAME


def _os_lock(fh: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        fh.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fh: IO[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        fh.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_holder(path: Path) -> dict[str, Any] | None:
    """Who last wrote the lock file (pid/host/started_at), if readable."""
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8") or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class InstanceLock:
    """Non-blocking exclusive process lock. Use ``acquire()``/``release()`` or ``with``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - the handle *is* the lock; closed in release()
        try:
            _os_lock(fh)
        except OSError:
            fh.close()
            holder = read_holder(self.path)
            detail = ""
            if holder:
                detail = (f" (pid {holder.get('pid')} on {holder.get('host')}, "
                          f"started {holder.get('started_at')})")
            raise DaemonAlreadyRunning(f"another job-agent daemon is already running{detail}; "
                                       f"lock file {self.path}") from None
        except BaseException:
            fh.close()
            raise
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                 "started_at": utcnow_iso()}).encode("utf-8"))
            fh.flush()
        except OSError as exc:  # the lock itself is held; the note is informational
            log.warning("could not write daemon lock details to %s: %s", self.path, exc)
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
        except OSError as exc:
            log.warning("could not clear daemon lock file %s: %s", self.path, exc)
        try:
            _os_unlock(fh)
        except OSError as exc:  # closing the handle below releases the lock anyway
            log.warning("could not unlock %s explicitly (%s); closing the handle releases it", self.path, exc)
        finally:
            fh.close()

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
