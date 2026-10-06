from __future__ import annotations

import argparse
import fcntl
import os
import socket
import threading
import time
from pathlib import Path

from .execution import WorkerEngine
from .gromacs import load_settings
from .job_store import JobStore

BASE_DIR = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="GROMACS Web UI execution worker")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--parallel", type=int, default=None, help="Concurrent jobs (overrides the max_parallel setting)")
    args = parser.parse_args()
    settings = load_settings(BASE_DIR)
    runtime_root = Path(settings["runtime_root"]).resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock_handle = (runtime_root / "worker.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another execution worker already owns this runtime directory.")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    lock_handle.write(worker_id + "\n")
    lock_handle.flush()
    # GROMACS jobs are subprocesses, so a thread per running job is I/O-bound
    # and scales without a GIL problem. claim_next() enforces the same limit
    # across threads, so each thread just claims-and-runs one job at a time.
    max_parallel = max(1, min(16, int(args.parallel if args.parallel is not None else settings.get("max_parallel", 1))))
    engine = WorkerEngine(JobStore(runtime_root, max_parallel=max_parallel), worker_id)
    engine.recover_orphaned_jobs()

    def worker_loop() -> None:
        while True:
            if args.parallel is None:
                # Pick up max_parallel changes from the settings panel without a restart.
                engine.store.configure(max_parallel=int(load_settings(BASE_DIR).get("max_parallel", 1)))
            if not engine.run_once():
                time.sleep(max(0.1, args.poll_interval))

    thread_count = max(1, min(16, int(args.parallel) if args.parallel else (os.cpu_count() or 4)))
    threads = [threading.Thread(target=worker_loop, daemon=True) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    raise SystemExit(main())
