import signal
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.execution import WorkerEngine
from app.job_store import JobStore
from app.steps import Step


def test_disk_guard_signals_owned_process_when_database_is_full(tmp_path):
    store = Mock()
    store.job_dir.return_value = tmp_path
    store.update.side_effect = sqlite3.OperationalError("database or disk is full")
    store.get.side_effect = sqlite3.OperationalError("database is locked")
    identity = {"process_pid": 123456, "process_pgid": 123456}
    state = {"process_identities": [identity]}
    engine = WorkerEngine(store, "worker")
    with (
        patch("app.execution.shutil.disk_usage", return_value=SimpleNamespace(free=1, total=100)),
        patch("app.execution.live_process_identities", side_effect=lambda meta, root: meta["process_identities"]),
        patch("app.execution.identity_matches", return_value=True),
        patch("app.execution.os.killpg") as kill,
    ):
        engine._inspect_running_disk("job", state, None)
    kill.assert_called_once_with(123456, signal.SIGTERM)
    assert state["term_sent"]


def test_disk_guard_does_not_signal_reused_process_identity(tmp_path):
    store = Mock()
    store.job_dir.return_value = tmp_path
    engine = WorkerEngine(store, "worker")
    with (
        patch("app.execution.shutil.disk_usage", return_value=SimpleNamespace(free=1, total=100)),
        patch("app.execution.live_process_identities", return_value=[{"process_pgid": 123456}]),
        patch("app.execution.identity_matches", return_value=False),
        patch("app.execution.os.killpg") as kill,
    ):
        engine._inspect_running_disk("job", {}, None)
    kill.assert_not_called()


def test_low_space_during_run_stops_and_preserves_checkpoint(tmp_path, monkeypatch):
    script = tmp_path / "calculate.py"
    script.write_text(
        "import pathlib,signal,time,sys\n"
        "def stop(*args):\n pathlib.Path('md.cpt').write_bytes(b'checkpoint'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "pathlib.Path('ready').touch()\n"
        "while True: time.sleep(0.01)\n"
    )
    store = JobStore(tmp_path)
    step = Step("MD", [sys.executable, str(script), "mdrun", "-cpi", "md.cpt"])
    job = store.create("disk guard", {"workflow": "custom"}, [], [step])
    workdir = store.job_dir(job["id"])

    def available(path):
        return SimpleNamespace(free=1 if (workdir / "ready").exists() else 10 * 1024**3, total=20 * 1024**3)

    monkeypatch.setattr("app.execution.DISK_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr("app.execution.shutil.disk_usage", available)
    store.start(job["id"], [step])
    assert WorkerEngine(store, "worker:disk").run_once()
    stopped = store.get(job["id"])
    assert stopped["status"] == "interrupted"
    assert stopped["disk_stop"]["checkpoint_file"] == "md.cpt"
    assert not stopped["disk_stop"]["forced"]
    assert (workdir / "md.cpt").read_bytes() == b"checkpoint"
    assert stopped["can_resume_checkpoint"]
