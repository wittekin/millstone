import multiprocessing
from pathlib import Path

import pytest

from millstone.runtime.locks import AdvisoryLock


def _hold_lock(path: str, ready: multiprocessing.Event, release: multiprocessing.Event) -> None:
    lock = AdvisoryLock(Path(path), timeout=5.0)
    lock.acquire()
    try:
        ready.set()
        release.wait(timeout=5.0)
    finally:
        lock.release()


class TestAdvisoryLock:
    def test_lock_acquire_release(self, tmp_path):
        lock_path = tmp_path / "locks" / "git.lock"
        lock = AdvisoryLock(lock_path, timeout=0.5)
        lock.acquire()
        lock.release()
        # Idempotent release
        lock.release()

    def test_lock_context_manager(self, tmp_path):
        lock_path = tmp_path / "locks" / "state.lock"
        with AdvisoryLock(lock_path, timeout=0.5):
            assert lock_path.exists()

        # Should be acquirable again after context exit.
        with AdvisoryLock(lock_path, timeout=0.5):
            pass

    def test_lock_timeout(self, tmp_path):
        lock_path = tmp_path / "locks" / "tasklist.lock"
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        proc = multiprocessing.Process(
            target=_hold_lock,
            args=(str(lock_path), ready, release),
            daemon=True,
        )
        proc.start()
        try:
            assert ready.wait(timeout=2.0)
            with pytest.raises(TimeoutError):
                AdvisoryLock(lock_path, timeout=0.1, poll_interval=0.02).acquire()
        finally:
            release.set()
            proc.join(timeout=2.0)
            if proc.is_alive():
                proc.kill()

    def test_lock_creates_parent_dirs(self, tmp_path):
        lock_path = tmp_path / "nested" / "locks" / "git.lock"
        assert not lock_path.parent.exists()
        lock = AdvisoryLock(lock_path, timeout=0.5)
        lock.acquire()
        try:
            assert lock_path.parent.exists()
        finally:
            lock.release()
