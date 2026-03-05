import threading
import time

from millstone.runtime.locks import AdvisoryLock
from millstone.runtime.parallel_state import ParallelState


class TestParallelState:
    def test_task_result_roundtrip(self, tmp_path):
        shared = tmp_path / "parallel"
        state = ParallelState(
            shared_state_dir=shared, state_lock=AdvisoryLock(tmp_path / "locks" / "state.lock")
        )

        state.write_task_result("t1", {"status": "ok", "n": 1})
        assert state.read_task_result("t1") == {"status": "ok", "n": 1}

    def test_atomic_write_no_partial(self, tmp_path):
        shared = tmp_path / "parallel"
        state = ParallelState(
            shared_state_dir=shared, state_lock=AdvisoryLock(tmp_path / "locks" / "state.lock")
        )

        stop = threading.Event()
        errors: list[str] = []

        def reader():
            while not stop.is_set():
                try:
                    data = state.read_task_result("t1")
                    if data is not None:
                        # Should always be valid JSON if present.
                        assert "n" in data
                except Exception as e:
                    errors.append(str(e))
                    stop.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for n in range(100):
                state.write_task_result("t1", {"n": n, "blob": "x" * 20000})
        finally:
            stop.set()
            t.join(timeout=2.0)

        assert errors == []

    def test_taskmap_persistence(self, tmp_path):
        shared = tmp_path / "parallel"
        lock = AdvisoryLock(tmp_path / "locks" / "state.lock")
        state = ParallelState(shared_state_dir=shared, state_lock=lock)

        mapping = {"a": 1, "b": {"index": 2}}
        state.save_taskmap(mapping)
        assert state.load_taskmap() == mapping

    def test_heartbeat_roundtrip(self, tmp_path):
        shared = tmp_path / "parallel"
        state = ParallelState(
            shared_state_dir=shared, state_lock=AdvisoryLock(tmp_path / "locks" / "state.lock")
        )
        before = time.time()
        state.write_heartbeat("t1")
        hb = state.read_heartbeat("t1")
        assert hb is not None
        assert abs(hb - before) < 1.0

    def test_load_control_state_missing(self, tmp_path):
        shared = tmp_path / "parallel"
        state = ParallelState(
            shared_state_dir=shared, state_lock=AdvisoryLock(tmp_path / "locks" / "state.lock")
        )
        assert state.load_control_state() is None
