from __future__ import annotations

import fcntl
import json
import tempfile
import time
from pathlib import Path

HEARTBEAT_MAX_AGE_SECONDS = 10.0


def write_worker_heartbeat(runtime_root: Path, status: dict) -> None:
    """Publish a complete snapshot without exposing partially written JSON."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=runtime_root, prefix=".worker-heartbeat-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({**status, "heartbeat_at": time.time()}, handle)
        temporary.replace(runtime_root / "worker-heartbeat.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def worker_status(runtime_root: Path) -> dict:
    status = {"available": False, "worker_id": None, "state": "offline"}
    try:
        with (runtime_root / "worker.lock").open("r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                status["worker_id"] = handle.read(1024).strip() or None
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
                return status
        with (runtime_root / "worker-heartbeat.json").open(encoding="utf-8") as handle:
            heartbeat = json.loads(handle.read(65536))
        if not status["worker_id"] or heartbeat.get("worker_id") != status["worker_id"]:
            return {**status, "state": "starting"}
        age = time.time() - float(heartbeat["heartbeat_at"])
        for key in ("state", "thread_count", "alive_threads", "ready_threads", "max_parallel", "thread_capacity"):
            status[key] = heartbeat[key]
        status["heartbeat_age_seconds"] = round(max(0.0, age), 2)
        if age < -1 or age > HEARTBEAT_MAX_AGE_SECONDS:
            status["state"] = "stale"
            return status
        status["available"] = (
            status["state"] == "ready"
            and int(status["alive_threads"]) == int(status["thread_count"])
            and int(status["thread_capacity"]) >= int(status["max_parallel"]) > 0
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        status["state"] = "unavailable" if status["worker_id"] else "offline"
    return status
