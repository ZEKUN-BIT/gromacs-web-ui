from __future__ import annotations

import hashlib
import subprocess
import sys
import threading

import pytest

from app.job_store import JobStore
from app.steps import Step


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("app.reproducibility.detect_gromacs", lambda *_: {})
    monkeypatch.setattr("app.reproducibility.host_information", lambda: {})
    monkeypatch.setattr("app.reproducibility.gpu_information", lambda: {})
    return JobStore(tmp_path / "runtime")


def source_job(store, *, queued=False):
    steps = [Step("run", ["/bin/true"], parallel_group="analysis")]
    meta = store.create("source", {"workflow": "custom"}, ["input.tpr"], steps)
    (store.job_dir(meta["id"]) / "input.tpr").write_bytes(b"reusable input")
    store.start(meta["id"], steps)
    if not queued:
        store.claim_next("test:prepare")
        store.update(meta["id"], status="completed", desired_action=None)
    return store.get(meta["id"])


def start_thread(operation):
    outcome = {}

    def run():
        try:
            outcome["result"] = operation()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def assert_unrelated_update_proceeds(store):
    other = store.create("other", {"workflow": "custom"}, [], [])
    done = threading.Event()
    thread, outcome = start_thread(lambda: (store.update(other["id"], note="still responsive"), done.set()))
    try:
        assert done.wait(1), "File operations must not hold SQLite's writer lock"
        assert "error" not in outcome
    finally:
        thread.join(timeout=2)


@pytest.mark.parametrize("queued", [True, False])
def test_clone_reserves_source_against_worker_delete_and_cleanup(store, monkeypatch, queued):
    source = source_job(store, queued=queued)
    entered, release = threading.Event(), threading.Event()
    from app import job_store

    copy_file = job_store.shutil.copy2

    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return copy_file(*args, **kwargs)

    monkeypatch.setattr(job_store.shutil, "copy2", slow_copy)
    thread, outcome = start_thread(lambda: store.clone(source["id"]))
    try:
        assert entered.wait(2)
        assert store.claim_next("test:concurrent") is None
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(source["id"])
        with pytest.raises(RuntimeError, match="busy"):
            store.maintain_job(source["id"], lambda *_: {})
        assert_unrelated_update_proceeds(store)
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in outcome
    cloned = outcome["result"]
    assert cloned["status"] == "queued"
    assert cloned["commands"][0]["parallel_group"] == "analysis"
    assert cloned["input_hashes"][0]["sha256"] == hashlib.sha256(b"reusable input").hexdigest()


def test_missing_clone_input_is_rejected_before_an_incomplete_job_is_queued(store):
    source = source_job(store)
    (store.job_dir(source["id"]) / "input.tpr").unlink()
    with pytest.raises(RuntimeError, match="input is missing"):
        store.clone(source["id"])
    assert len(store.list()) == 1


def test_cancelled_clone_target_cannot_be_deleted_while_inputs_are_still_copying(store, monkeypatch):
    source = source_job(store)
    entered, release = threading.Event(), threading.Event()
    from app import job_store

    copy_file = job_store.shutil.copy2

    def slow_copy(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return copy_file(*args, **kwargs)

    monkeypatch.setattr(job_store.shutil, "copy2", slow_copy)
    thread, outcome = start_thread(lambda: store.clone(source["id"]))
    try:
        assert entered.wait(2)
        target = next(meta for meta in store.list() if meta["id"] != source["id"])
        store.cancel(target["id"])
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(target["id"])
    finally:
        release.set()
        thread.join(timeout=5)
    assert isinstance(outcome.get("error"), RuntimeError)
    assert store.get(target["id"])["status"] == "cancelled"
    store.delete(target["id"])


@pytest.mark.parametrize("state", ["preparing", "running", "maintenance", "deleting", "live"])
def test_clone_rejects_unstable_sources(store, monkeypatch, state):
    if state == "preparing":
        source = store.create("preparing", {"workflow": "custom"}, [], [])
    else:
        source = source_job(store, queued=state == "running")
        if state == "running":
            store.claim_next("test:running")
        elif state == "maintenance":
            store.update(source["id"], maintenance_pending=True)
        elif state == "deleting":
            store.update(source["id"], deletion_pending=True)
        elif state == "live":
            monkeypatch.setattr("app.execution.live_process_identities", lambda *_: [{"pid": 123}])
    with pytest.raises(RuntimeError, match="not available for cloning"):
        store.clone(source["id"])


def test_slow_maintenance_allows_unrelated_updates_and_blocks_task_actions(store):
    source = source_job(store)
    entered, release = threading.Event(), threading.Event()

    def clean(meta, workdir):
        entered.set()
        assert release.wait(5)
        return {"deleted_files": 0, "deleted_bytes": 0}

    thread, outcome = start_thread(lambda: store.maintain_job(source["id"], clean))
    try:
        assert entered.wait(2)
        assert store.get(source["id"])["maintenance_pending"]
        with pytest.raises(RuntimeError, match="pending"):
            store.update(source["id"], desired_action="cancel")
        with pytest.raises(RuntimeError, match="busy"):
            store.clone(source["id"])
        assert_unrelated_update_proceeds(store)
    finally:
        release.set()
        thread.join(timeout=5)
    assert "error" not in outcome
    current = store.get(source["id"])
    assert not current.get("maintenance_pending")
    assert current["storage_revision"] == 1


def test_maintenance_rejects_metadata_changed_during_preview_validation(store):
    source = source_job(store)
    operations = []

    def validate(meta, workdir):
        assert_unrelated_update_proceeds(store)
        store.update(source["id"], note="changed while validating")

    with pytest.raises(RuntimeError, match="changed"):
        store.maintain_job(source["id"], lambda *_: operations.append("cleaned"), validate=validate)
    assert not operations
    current = store.get(source["id"])
    assert current["storage_revision"] == 0
    assert not current.get("maintenance_pending")
    assert not current.get("last_cleanup_error")


def test_slow_deletion_releases_database_lock_and_keeps_durable_pending_marker(store, monkeypatch):
    source = source_job(store)
    workdir = store.job_dir(source["id"])
    entered, release = threading.Event(), threading.Event()
    from app import job_store

    remove = job_store.shutil.rmtree

    def slow_remove(path, *args, **kwargs):
        if path == workdir:
            entered.set()
            assert release.wait(5)
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(job_store.shutil, "rmtree", slow_remove)
    thread, outcome = start_thread(lambda: store.delete(source["id"]))
    try:
        assert entered.wait(2)
        assert store.get(source["id"])["deletion_pending"]
        with pytest.raises(RuntimeError, match="pending"):
            store.update(source["id"], desired_action="cancel")
        assert_unrelated_update_proceeds(store)
    finally:
        release.set()
        thread.join(timeout=5)
    assert "error" not in outcome
    assert not workdir.exists()
    with pytest.raises(FileNotFoundError):
        store.get(source["id"])


def test_partial_maintenance_failure_is_retryable_and_invalidates_old_previews(store):
    source = source_job(store)
    disposable = store.job_dir(source["id"]) / "nvt.trr"
    disposable.write_bytes(b"temporary")

    def partial_clean(*_):
        disposable.unlink()
        raise OSError("injected cleanup failure")

    with pytest.raises(OSError, match="injected"):
        store.maintain_job(source["id"], partial_clean)
    current = store.get(source["id"])
    assert not current.get("maintenance_pending")
    assert current["storage_revision"] == 1
    assert current["last_cleanup_error"]
    assert store.maintain_job(source["id"], lambda *_: {"deleted_files": 0}) == {"deleted_files": 0}


def test_restart_recovers_abandoned_maintenance_without_clearing_a_live_owner(store):
    source = source_job(store)
    store.update(source["id"], maintenance_pending=True)
    with store.job_files_lock(source["id"]):
        concurrent = JobStore(store.runtime_root)
        assert concurrent.get(source["id"])["maintenance_pending"]
    restarted = JobStore(store.runtime_root)
    current = restarted.get(source["id"])
    assert not current.get("maintenance_pending")
    assert current["storage_revision"] == 1


def test_failed_deletion_remains_pending_and_can_be_retried(store, monkeypatch):
    source = source_job(store)
    from app import job_store

    with monkeypatch.context() as failure:
        failure.setattr(job_store.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(OSError("injected delete failure")))
        with pytest.raises(OSError, match="injected"):
            store.delete(source["id"])
    assert store.get(source["id"])["deletion_pending"]
    with pytest.raises(RuntimeError, match="not available"):
        store.clone(source["id"])
    restarted = JobStore(store.runtime_root)
    assert restarted.get(source["id"])["deletion_pending"]
    restarted.delete(source["id"])
    with pytest.raises(FileNotFoundError):
        store.get(source["id"])


@pytest.mark.parametrize("action", ["adopt", "cancel"])
def test_existing_process_control_does_not_wait_for_a_file_lock(store, monkeypatch, action):
    source = source_job(store, queued=True)
    store.update(source["id"], status="interrupted", process_pid=123, desired_action=action)
    monkeypatch.setattr("app.execution.live_process_identities", lambda *_: [{"process_pid": 123}])
    with store.job_files_lock(source["id"]):
        claimed = store.claim_next("test:control")
    assert claimed["claimed_action"] == action


def test_get_and_delete_reject_a_symlinked_task_directory(store):
    source = source_job(store)
    workdir = store.job_dir(source["id"])
    moved = workdir.with_name(workdir.name + "-real")
    workdir.rename(moved)
    workdir.symlink_to(moved, target_is_directory=True)
    with pytest.raises(FileNotFoundError, match="symbolic link"):
        store.get(source["id"])
    with pytest.raises(RuntimeError, match="symbolic link"):
        store.delete(source["id"])
    assert (moved / "input.tpr").read_bytes() == b"reusable input"


def test_file_locks_are_shared_between_store_instances_and_do_not_follow_links(store, tmp_path):
    source = source_job(store)
    other = JobStore(store.runtime_root)
    with store.job_files_lock(source["id"]):
        with pytest.raises(RuntimeError, match="busy"):
            with other.job_files_lock(source["id"]):
                pytest.fail("Lock ownership must be exclusive")
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from pathlib import Path
import sys
from app.job_store import JobStore
store = JobStore(Path(sys.argv[1]))
try:
    with store.job_files_lock(sys.argv[2]):
        raise SystemExit('Child process acquired a lock held by the parent')
except RuntimeError as error:
    assert 'busy' in str(error)
""",
                str(store.runtime_root),
                source["id"],
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert child.returncode == 0, child.stderr
    path = store.runtime_root / "job-locks" / (hashlib.sha256(source["id"].encode()).hexdigest() + ".lock")
    outside = tmp_path / "external"
    outside.write_bytes(b"untouched")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        with store.job_files_lock(source["id"]):
            pytest.fail("Lock paths must not follow links")
    assert outside.read_bytes() == b"untouched"
