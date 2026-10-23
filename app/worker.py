from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import socket
import sqlite3
import threading
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .execution import WorkerEngine
from .gromacs import load_settings
from .job_store import JobStore
from .worker_health import write_worker_heartbeat

BASE_DIR = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger(__name__)
MAX_RECOVERY_ATTEMPTS = 8
RECOVERY_BACKOFF_SECONDS = 0.5
RECOVERABLE_ERRORS = (sqlite3.OperationalError, OSError)


class WorkerService:
    """Supervise execution slots and publish health independently of SQLite."""

    def __init__(
        self, engine: WorkerEngine, settings_loader: Callable[[], dict], *, poll_interval: float = 0.5, parallel: int | None = None
    ):
        self.engine = engine
        self.settings_loader = settings_loader
        self.poll_interval = max(0.1, poll_interval)
        self.parallel = parallel
        self.stop = threading.Event()
        self.failed = threading.Event()
        self.threads: list[threading.Thread] = []
        self._states: dict[int, str] = {}
        self._state_lock = threading.Lock()

    def _set_state(self, slot: int, state: str) -> None:
        with self._state_lock:
            self._states[slot] = state

    def _worker_loop(self, slot: int) -> None:
        failures = 0
        try:
            while not self.stop.is_set():
                self._set_state(slot, "ready")
                try:
                    worked = self.engine.run_once()
                except RECOVERABLE_ERRORS:
                    failures += 1
                    if failures >= MAX_RECOVERY_ATTEMPTS:
                        raise
                    self._set_state(slot, "backoff")
                    delay = min(30.0, RECOVERY_BACKOFF_SECONDS * 2 ** (failures - 1))
                    LOGGER.exception(
                        "Worker slot %s hit a storage error; retrying in %.1fs (%s/%s)", slot, delay, failures, MAX_RECOVERY_ATTEMPTS
                    )
                    self.stop.wait(delay)
                    continue
                failures = 0
                if not worked:
                    self.stop.wait(self.poll_interval)
        except BaseException as error:
            if self.stop.is_set() and isinstance(error, SystemExit):
                self._set_state(slot, "stopped")
                return
            self._set_state(slot, "failed")
            LOGGER.exception("Worker slot %s stopped unexpectedly", slot)
            self.failed.set()
            self.stop.set()

    def _configure(self) -> None:
        settings = self.settings_loader()
        max_parallel = max(1, min(16, int(self.parallel if self.parallel is not None else settings.get("max_parallel", 1))))
        self.engine.store.configure(max_parallel=max_parallel, analysis_parallel=settings.get("analysis_parallel", 2))
        # Keep a spare slot to claim cancellation/re-adoption of an orphan even
        # while every normal execution slot is occupied. claim_next still
        # enforces max_parallel and resource limits for all new launches.
        while len(self.threads) < max_parallel + 1 and not self.stop.is_set():
            slot = len(self.threads)
            thread = threading.Thread(target=self._worker_loop, args=(slot,), name=f"execution-slot-{slot}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def snapshot(self, state: str | None = None) -> dict:
        with self._state_lock:
            ready = sum(thread.is_alive() and self._states.get(slot) == "ready" for slot, thread in enumerate(self.threads))
        alive = sum(thread.is_alive() for thread in self.threads)
        maximum = self.engine.store.max_parallel
        if state is None:
            state = "failed" if self.failed.is_set() else ("ready" if ready >= maximum and alive == len(self.threads) else "degraded")
        return {
            "worker_id": self.engine.worker_id,
            "state": state,
            "thread_count": len(self.threads),
            "alive_threads": alive,
            "ready_threads": ready,
            "max_parallel": maximum,
            "thread_capacity": min(ready, maximum),
        }

    def _publish(self, state: str | None = None) -> None:
        write_worker_heartbeat(self.engine.store.runtime_root, self.snapshot(state))

    def run(self) -> int:
        try:
            self._publish("starting")
            for attempt in range(MAX_RECOVERY_ATTEMPTS):
                if self.stop.is_set():
                    return 0
                try:
                    self.engine.recover_orphaned_jobs()
                    break
                except RECOVERABLE_ERRORS:
                    if self.stop.is_set():
                        return 0
                    if attempt + 1 == MAX_RECOVERY_ATTEMPTS:
                        raise
                    LOGGER.exception("Worker startup recovery will retry")
                    if self.stop.wait(min(30.0, RECOVERY_BACKOFF_SECONDS * 2**attempt)):
                        return 0
                    self._publish("starting")
            while not self.stop.is_set():
                self._configure()
                if any(not thread.is_alive() for thread in self.threads):
                    raise RuntimeError("An execution slot exited unexpectedly")
                self._publish()
                self.stop.wait(1.0)
        except Exception:
            LOGGER.exception("Execution worker failed")
            self.failed.set()
        finally:
            self.stop.set()
            # Active subprocess identities stay persisted for the next worker.
            # Do not claim a clean exit while execution capacity has been lost.
            try:
                self._publish("failed" if self.failed.is_set() else "stopped")
            except OSError:
                LOGGER.exception("Could not publish final worker health")
            self.engine.prevent_new_launches()
        return 1 if self.failed.is_set() else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GROMACS Web UI execution worker")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--parallel", type=int, default=None, help="Concurrent jobs (overrides the max_parallel setting)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(BASE_DIR)
    runtime_root = Path(settings["runtime_root"]).resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    with (runtime_root / "worker.lock").open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.error("Another execution worker already owns this runtime directory.")
            return 1
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(worker_id + "\n")
        lock_handle.flush()
        store = JobStore(runtime_root, max_parallel=settings.get("max_parallel", 1), analysis_parallel=settings.get("analysis_parallel", 2))
        service = WorkerService(
            WorkerEngine(store, worker_id), lambda: load_settings(BASE_DIR), poll_interval=args.poll_interval, parallel=args.parallel
        )

        def request_shutdown(signum: int, frame: object) -> None:
            service.stop.set()

        previous_handlers = {}
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, request_shutdown)
            return service.run()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
