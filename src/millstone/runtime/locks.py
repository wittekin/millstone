from __future__ import annotations

import errno
import fcntl
import time
from contextlib import suppress
from pathlib import Path
from typing import TextIO


class AdvisoryLock:
    """File-based advisory lock using POSIX fcntl/lockf.

    This is intentionally POSIX-only (Linux/WSL target). Workers must not acquire
    these locks; they are owned by the control plane.
    """

    def __init__(self, path: Path, timeout: float = 30.0, poll_interval: float = 0.05):
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._fh: TextIO | None = None

    def acquire(self) -> None:
        """Acquire an exclusive lock, waiting up to timeout seconds."""
        if self._fh is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)

        fh = self.path.open("a+")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fcntl.lockf(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fh = fh
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out acquiring lock: {self.path}") from None
                    time.sleep(self.poll_interval)
                except OSError as e:
                    if e.errno in (errno.EACCES, errno.EAGAIN):
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Timed out acquiring lock: {self.path}") from e
                        time.sleep(self.poll_interval)
                        continue
                    raise
        except Exception:
            fh.close()
            raise

    def release(self) -> None:
        """Release the lock (idempotent)."""
        fh = self._fh
        if fh is None:
            return
        try:
            with suppress(OSError):
                fcntl.lockf(fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                fh.close()
            finally:
                self._fh = None

    def __enter__(self) -> AdvisoryLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
