from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Protocol


class StateLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, _exc_type, _exc, _tb) -> None: ...


class ParallelState:
    """Manages shared state under .millstone/parallel/ for control plane and workers."""

    def __init__(self, shared_state_dir: Path, state_lock: StateLock):
        self.shared_state_dir = Path(shared_state_dir)
        self.state_lock = state_lock

    def _atomic_write_bytes(self, path: Path, data: bytes) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "wb") as f:
                fd = None
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

    def _atomic_write_json(self, path: Path, obj: dict) -> None:
        data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        self._atomic_write_bytes(path, data)

    def _read_json(self, path: Path) -> dict | None:
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            return None

    def _task_dir(self, task_id: str) -> Path:
        return self.shared_state_dir / "tasks" / task_id

    # Control plane state (.millstone/parallel/state.json)
    def save_control_state(
        self,
        base_ref_sha: str,
        base_branch: str,
        integration_branch: str,
        integration_worktree: Path,
        task_records: dict,
        merge_queue: list,
    ) -> None:
        state = {
            "base_ref_sha": base_ref_sha,
            "base_branch": base_branch,
            "integration_branch": integration_branch,
            "integration_worktree": str(integration_worktree),
            "task_records": task_records,
            "merge_queue": merge_queue,
            "saved_at": time.time(),
        }
        with self.state_lock:
            self._atomic_write_json(self.shared_state_dir / "state.json", state)

    def load_control_state(self) -> dict | None:
        return self._read_json(self.shared_state_dir / "state.json")

    # Per-task results (.millstone/parallel/tasks/<task_id>/result.json)
    def write_task_result(self, task_id: str, result: dict) -> None:
        path = self._task_dir(task_id) / "result.json"
        self._atomic_write_json(path, result)

    def read_task_result(self, task_id: str) -> dict | None:
        return self._read_json(self._task_dir(task_id) / "result.json")

    # Heartbeats (.millstone/parallel/tasks/<task_id>/heartbeat)
    def write_heartbeat(self, task_id: str) -> None:
        path = self._task_dir(task_id) / "heartbeat"
        self._atomic_write_bytes(path, f"{time.time():.6f}\n".encode())

    def read_heartbeat(self, task_id: str) -> float | None:
        path = self._task_dir(task_id) / "heartbeat"
        try:
            raw = path.read_text().strip()
        except FileNotFoundError:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # Task ID mapping (.millstone/parallel/taskmap.json)
    def save_taskmap(self, mapping: dict) -> None:
        with self.state_lock:
            self._atomic_write_json(self.shared_state_dir / "taskmap.json", mapping)

    def load_taskmap(self) -> dict:
        return self._read_json(self.shared_state_dir / "taskmap.json") or {}
