from __future__ import annotations

import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.execution import WorkerEngine, identity_matches, process_identity
from app.gromacs import Step
from app.job_store import JobStore
from app.worker import WorkerService
from app.worker_health import worker_status, write_worker_heartbeat


def service(tmp_path, run_once=None):
    engine = Mock()
    engine.store = JobStore(tmp_path)
    engine.worker_id = "test-worker"
    engine.run_once = run_once or Mock(return_value=False)
    return WorkerService(engine, lambda: {"max_parallel": 1, "analysis_parallel": 2})


def test_storage_errors_back_off_and_reset_after_success(tmp_path):
    run_once = Mock(side_effect=[sqlite3.OperationalError("busy"), OSError("unavailable"), True, OSError("busy again"), True])
    worker = service(tmp_path, run_once)
    worker.stop = Mock()
    worker.stop.is_set.side_effect = [False] * 5 + [True]
    worker._worker_loop(0)
    assert [call.args[0] for call in worker.stop.wait.call_args_list] == [0.5, 1.0, 0.5]
    assert not worker.failed.is_set()
    assert run_once.call_count == 5


def test_launch_guard_exit_during_requested_shutdown_is_not_a_fatal_error(tmp_path):
    worker = service(tmp_path)

    def stopping():
        worker.stop.set()
        raise SystemExit("Execution worker is stopping")

    worker.engine.run_once.side_effect = stopping
    worker._worker_loop(0)
    assert not worker.failed.is_set()
    assert worker._states[0] == "stopped"


@pytest.mark.parametrize("error", [ValueError("fatal"), SystemExit(3), sqlite3.OperationalError("persistent storage failure")])
def test_fatal_or_exhausted_worker_errors_return_nonzero_and_publish_failure(tmp_path, monkeypatch, error):
    monkeypatch.setattr("app.worker.RECOVERY_BACKOFF_SECONDS", 0)
    worker = service(tmp_path, Mock(side_effect=error))
    assert worker.run() == 1
    assert worker.failed.is_set()
    worker.engine.prevent_new_launches.assert_called_once_with()
    for thread in worker.threads:
        thread.join(timeout=1)
    assert json.loads((tmp_path / "worker-heartbeat.json").read_text())["state"] == "failed"
    if isinstance(error, sqlite3.OperationalError):
        assert 8 <= worker.engine.run_once.call_count <= 16
    else:
        assert 1 <= worker.engine.run_once.call_count <= 2


def test_settings_increase_provisions_execution_capacity(tmp_path):
    settings = {"max_parallel": 1, "analysis_parallel": 2}
    worker = service(tmp_path)
    worker.settings_loader = lambda: settings
    try:
        worker._configure()
        assert len(worker.threads) == 2, "A control slot remains available while the execution slot is occupied"
        settings.update(max_parallel=4, analysis_parallel=3)
        worker._configure()
        deadline = time.monotonic() + 2
        while worker.snapshot()["ready_threads"] < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker.snapshot()["thread_capacity"] == 4
        assert worker.snapshot()["state"] == "ready"
        settings["max_parallel"] = 1
        worker._configure()
        assert worker.engine.store.max_parallel == 1
        assert worker.snapshot()["thread_capacity"] == 1
    finally:
        worker.stop.set()
        for thread in worker.threads:
            thread.join(timeout=1)


def test_pending_recovery_after_storage_outage_never_recovers_another_threads_job(tmp_path):
    store = JobStore(tmp_path, max_parallel=2)
    steps = [Step("safe", ["/bin/true"])]
    jobs = [store.create(str(index), {}, [], steps) for index in range(2)]
    for job in jobs:
        store.start(job["id"], steps)
    other = store.claim_next("other-thread")
    engine = WorkerEngine(store, "worker")
    original = engine._run_job
    with patch.object(engine, "_run_job", side_effect=sqlite3.OperationalError("storage outage")):
        with pytest.raises(sqlite3.OperationalError):
            engine.run_once()
    with patch.object(engine, "_run_job", wraps=original):
        assert engine.run_once()
    assert store.get(other["id"])["status"] == "running"
    failed = next(job for job in jobs if job["id"] != other["id"])
    assert store.get(failed["id"])["status"] == "completed"


def test_second_worker_does_not_erase_live_owner_identity(tmp_path, monkeypatch):
    from app.worker import main

    lock = tmp_path / "worker.lock"
    lock.write_text("original-owner\n")
    monkeypatch.setattr("sys.argv", ["worker"])
    monkeypatch.setattr("app.worker.load_settings", lambda _: {"runtime_root": str(tmp_path)})
    with lock.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert main() == 1
        assert lock.read_text() == "original-owner\n"


@pytest.mark.parametrize("raises", [False, True])
def test_main_restores_signal_handlers_after_service_stops(tmp_path, monkeypatch, raises):
    from app.worker import main

    worker = service(tmp_path)
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}

    def run():
        for signum in previous:
            worker.stop.clear()
            handler = signal.getsignal(signum)
            assert handler != previous[signum]
            handler(signum, None)
            assert worker.stop.is_set()
        if raises:
            raise RuntimeError("isolated service failure")
        return 0

    monkeypatch.setattr("sys.argv", ["worker"])
    monkeypatch.setattr("app.worker.load_settings", lambda _: {"runtime_root": str(tmp_path)})
    monkeypatch.setattr("app.worker.WorkerService", lambda *args, **kwargs: worker)
    monkeypatch.setattr(worker, "run", run)
    if raises:
        with pytest.raises(RuntimeError, match="isolated service failure"):
            main()
    else:
        assert main() == 0
    assert {signum: signal.getsignal(signum) for signum in previous} == previous


@pytest.mark.parametrize("already_stopping", [False, True])
def test_shutdown_during_startup_recovery_never_starts_execution_slots(tmp_path, already_stopping):
    worker = service(tmp_path)
    if already_stopping:
        worker.stop.set()
    else:

        def recover():
            worker.stop.set()
            raise sqlite3.OperationalError("storage unavailable during shutdown")

        worker.engine.recover_orphaned_jobs.side_effect = recover
    assert worker.run() == 0
    assert not worker.threads
    worker.engine.run_once.assert_not_called()
    assert worker.engine.recover_orphaned_jobs.call_count == (0 if already_stopping else 1)
    worker.engine.prevent_new_launches.assert_called_once_with()
    assert json.loads((tmp_path / "worker-heartbeat.json").read_text())["state"] == "stopped"


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("storage_failure", [False, True])
def test_actual_worker_signal_waits_for_registration_or_cleanup(tmp_path, signum, storage_failure):
    code = r"""
import json, sqlite3, sys, time
from pathlib import Path
from app import worker
from app.execution import process_identity
from app.gromacs import Step
from app.job_store import JobStore

runtime = Path(sys.argv[1])
fail_registration = sys.argv[2] == "1"
store = JobStore(runtime)
step = Step("finite child", [sys.executable, "-c", "import time; time.sleep(15)"])
job = store.create("signal during registration", {}, [], [step])
store.start(job["id"], [step])
following = Step("must remain queued", [sys.executable, "-c", "from pathlib import Path; Path('unexpected').touch()"])
queued = store.create("next task", {}, [], [following])
store.start(queued["id"], [following])
original_update = JobStore.update

def delayed(self, job_id, **values):
    if "process_started_at" in values:
        (runtime / "spawned.tmp").write_text(json.dumps(process_identity(values["process_pid"])))
        (runtime / "spawned.tmp").replace(runtime / "spawned.json")
        deadline = time.monotonic() + 8
        while not (runtime / "allow-registration").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("test did not release registration")
            time.sleep(0.01)
        if fail_registration:
            raise sqlite3.OperationalError("isolated registration failure")
    return original_update(self, job_id, **values)

JobStore.update = delayed
worker.load_settings = lambda _: {"runtime_root": str(runtime), "max_parallel": 1, "analysis_parallel": 2}
sys.argv = ["worker"]
raise SystemExit(worker.main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(int(storage_failure))],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identity = None
    try:
        marker = tmp_path / "spawned.json"
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "Isolated worker did not reach identity registration"
        identity = json.loads(marker.read_text())
        assert identity is not None
        process.send_signal(signum)
        deadline = time.monotonic() + 3
        heartbeat_file = tmp_path / "worker-heartbeat.json"
        while time.monotonic() < deadline:
            if json.loads(heartbeat_file.read_text()).get("state") == "stopped":
                break
            time.sleep(0.01)
        assert json.loads(heartbeat_file.read_text())["state"] == "stopped"
        assert process.poll() is None, "Signal must wait for the child registration or cleanup before exiting"
        (tmp_path / "allow-registration").touch()
        assert process.wait(timeout=4) == 0
        jobs = JobStore(tmp_path).list()
        job = next(job for job in jobs if job["name"] == "signal during registration")
        queued = next(job for job in jobs if job["name"] == "next task")
        assert queued["status"] == "queued"
        assert not (tmp_path / "jobs" / queued["id"] / "unexpected").exists()
        if storage_failure:
            assert process_identity(identity["process_pid"]) is None
        else:
            assert job["process_pid"] == identity["process_pid"]
            assert identity in job["process_identities"]
    finally:
        (tmp_path / "allow-registration").touch()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if identity is not None and identity_matches(identity, Path(identity["process_cwd"])):
            os.killpg(identity["process_pgid"], signal.SIGTERM)


@pytest.mark.parametrize("storage_failure", [False, True])
def test_fatal_shutdown_waits_for_inflight_registration_or_cleanup_and_prevents_new_children(tmp_path, storage_failure):
    store = JobStore(tmp_path)
    step = Step("finite child", [sys.executable, "-c", "import time; time.sleep(8)"])
    job = store.create("launch guard", {}, [], [step])
    engine = WorkerEngine(store, "worker")
    entered, allow_save, shutdown_complete = threading.Event(), threading.Event(), threading.Event()
    children, errors, waiting_errors = [], [], []
    original_update, original_popen = store.update, subprocess.Popen

    def pause_first_registration(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert allow_save.wait(3)
            if storage_failure:
                raise sqlite3.OperationalError("isolated registration failure")
        return original_update(*args, **kwargs)

    def remember(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def launch():
        try:
            engine._spawn_command(job["id"], store.job_dir(job["id"]), step, [])
        except BaseException as error:
            errors.append(error)

    def shutdown():
        engine.prevent_new_launches()
        shutdown_complete.set()

    def waiting_launch():
        try:
            engine._spawn_command(job["id"], store.job_dir(job["id"]), step, [])
        except BaseException as error:
            waiting_errors.append(error)

    launcher, waiter, stopper = threading.Thread(target=launch), threading.Thread(target=waiting_launch), threading.Thread(target=shutdown)
    try:
        with patch.object(store, "update", pause_first_registration), patch("app.execution.subprocess.Popen", remember):
            launcher.start()
            assert entered.wait(2)
            waiter.start()
            stopper.start()
            assert not shutdown_complete.wait(0.05), "Shutdown must wait until the already spawned child is registered or stopped"
            allow_save.set()
            launcher.join(timeout=3)
            waiter.join(timeout=3)
            stopper.join(timeout=3)
        assert shutdown_complete.is_set() and not launcher.is_alive()
        assert len(children) == 1, "A launch waiting for registration must observe the stop request before creating another child"
        assert len(waiting_errors) == 1 and isinstance(waiting_errors[0], SystemExit)
        if storage_failure:
            assert isinstance(errors[0], sqlite3.OperationalError)
            assert children[0].poll() is not None
        else:
            assert not errors
            assert store.get(job["id"])["process_identities"][0]["process_pid"] == children[0].pid
        with patch("app.execution.subprocess.Popen") as popen, pytest.raises(SystemExit, match="stopping"):
            engine._spawn_command(job["id"], store.job_dir(job["id"]), step, [])
        popen.assert_not_called()
    finally:
        allow_save.set()
        launcher.join(timeout=3)
        if waiter.ident is not None:
            waiter.join(timeout=3)
        if stopper.ident is not None:
            stopper.join(timeout=3)
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()


def heartbeat(**updates):
    return {
        "worker_id": "test-worker",
        "state": "ready",
        "thread_count": 2,
        "alive_threads": 2,
        "ready_threads": 2,
        "max_parallel": 2,
        "thread_capacity": 2,
        **updates,
    }


def test_health_requires_lock_fresh_identity_and_full_capacity(tmp_path):
    lock = tmp_path / "worker.lock"
    lock.write_text("test-worker\n")
    with lock.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_worker_heartbeat(tmp_path, heartbeat())
        assert worker_status(tmp_path)["available"]
        write_worker_heartbeat(tmp_path, heartbeat(alive_threads=1, ready_threads=1, thread_capacity=1))
        assert not worker_status(tmp_path)["available"]
        with patch("app.worker_health.time.time", return_value=time.time() - 11):
            write_worker_heartbeat(tmp_path, heartbeat())
        assert worker_status(tmp_path)["state"] == "stale"
        write_worker_heartbeat(tmp_path, heartbeat(worker_id="old-worker"))
        assert worker_status(tmp_path)["state"] == "starting"
        write_worker_heartbeat(tmp_path, heartbeat())
    assert not worker_status(tmp_path)["available"]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_exits_on_worker_loss_and_stops_web_process(tmp_path, exit_code):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "run.sh").write_bytes((root / "run.sh").read_bytes())
    binary = tmp_path / ".venv" / "bin"
    binary.mkdir(parents=True)
    (binary / "activate").write_text("")
    python = binary / "python"
    python.write_text(f'#!/bin/bash\nif [ "$1" = "-c" ]; then exit 0; fi\nsleep 0.2\nexit {exit_code}\n')
    python.chmod(0o755)
    uvicorn = binary / "uvicorn"
    uvicorn.write_text("#!/bin/bash\necho $$ > web.pid\nexec sleep 10\n")
    uvicorn.chmod(0o755)
    result = subprocess.run(
        ["bash", "run.sh"],
        cwd=tmp_path,
        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "WEB_CONCURRENCY": "1"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == (exit_code or 1)
    web_pid = int((tmp_path / "web.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(web_pid, 0)
