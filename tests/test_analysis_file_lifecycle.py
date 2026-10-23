from __future__ import annotations

import threading

import pytest

from app import existing_analysis
from app.existing_analysis import analysis_inputs, analysis_source, create_existing_analysis
from app.job_store import JobStore
from app.models import ExistingAnalysisRequest


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr("app.reproducibility.detect_gromacs", lambda *_: {})
    monkeypatch.setattr("app.reproducibility.host_information", lambda: {})
    monkeypatch.setattr("app.reproducibility.gpu_information", lambda: {})
    monkeypatch.setattr(existing_analysis, "require_analysis_disk_space", lambda *_: None)
    store = JobStore(tmp_path / "runtime")
    meta = store.create("source", {"workflow": "protein_md"}, [], [])
    for filename in ("md.tpr", "md.xtc"):
        (store.job_dir(meta["id"]) / filename).write_bytes(f"source: {filename}".encode())
    store.start(meta["id"], [])
    store.claim_next("fixture")
    store.update(meta["id"], status="completed")
    return store, meta["id"]


def create_analysis(store, job_id):
    return create_existing_analysis(
        store,
        job_id,
        ExistingAnalysisRequest(tpr_file="md.tpr", trajectory_file="md.xtc", dry_run=True),
        {"gmx_bin": "/bin/true", "default_ntomp": 1},
    )


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


@pytest.mark.parametrize("phase", ["validation", "selection", "copy"])
def test_source_is_reserved_before_validation_until_inputs_finish_copying(source, monkeypatch, phase):
    store, job_id = source
    entered, release = threading.Event(), threading.Event()
    owner, attribute = {
        "validation": (existing_analysis, "analysis_source"),
        "selection": (existing_analysis, "source_file"),
        "copy": (existing_analysis, "_copy_analysis_input"),
    }[phase]
    original = getattr(owner, attribute)

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, attribute, pause)
    thread, outcome = start_thread(lambda: create_analysis(store, job_id))
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(job_id)
        with pytest.raises(RuntimeError, match="busy"):
            store.maintain_job(job_id, lambda *_: {})
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in outcome
    created = outcome["result"]
    assert created["status"] == "queued"
    assert len(created["input_hashes"]) == 2
    store.delete(job_id)
    assert (store.job_dir(created["id"]) / "input.xtc").read_bytes() == b"source: md.xtc"


@pytest.mark.parametrize("marker", ["maintenance_pending", "deletion_pending"])
def test_pending_source_is_rejected_without_creating_a_target(source, marker):
    store, job_id = source
    store.update(job_id, **{marker: True})
    for operation in (analysis_source, analysis_inputs, create_analysis):
        with pytest.raises(RuntimeError, match="pending"):
            operation(store, job_id)
    assert len(store.list()) == 1


@pytest.mark.parametrize("phase", ["copy", "manifest"])
def test_cancelled_target_is_reserved_through_copy_and_manifest(source, monkeypatch, phase):
    store, job_id = source
    entered, release = threading.Event(), threading.Event()
    owner, attribute = {
        "copy": (existing_analysis, "_copy_analysis_input"),
        "manifest": (existing_analysis, "write_experiment_manifest"),
    }[phase]
    original = getattr(owner, attribute)

    def pause(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, attribute, pause)
    thread, outcome = start_thread(lambda: create_analysis(store, job_id))
    try:
        assert entered.wait(2)
        target = next(meta for meta in store.list() if meta["id"] != job_id)
        assert store.cancel(target["id"])["status"] == "cancelled"
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(target["id"])
        with pytest.raises(RuntimeError, match="busy"):
            store.maintain_job(target["id"], lambda *_: {})
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(job_id)
        assert store.claim_next("racing-worker") is None
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), RuntimeError)
    assert "cancelled" in str(outcome["error"])
    assert store.get(target["id"])["status"] == "cancelled"
    assert list(store.job_dir(target["id"]).glob("input.*")) == []
    assert store.claim_next("worker") is None
    store.delete(target["id"])


def test_failed_staging_keeps_target_reserved_until_partial_files_are_removed(source, monkeypatch):
    store, job_id = source
    entered, release = threading.Event(), threading.Event()
    original_unlink = existing_analysis.Path.unlink

    def fail_copy(*_):
        raise OSError("disk full")

    def slow_unlink(path, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(existing_analysis, "_copy_analysis_input", fail_copy)
    monkeypatch.setattr(existing_analysis.Path, "unlink", slow_unlink)
    thread, outcome = start_thread(lambda: create_analysis(store, job_id))
    try:
        assert entered.wait(2)
        target = next(meta for meta in store.list() if meta["id"] != job_id)
        assert target["status"] == "cancelled"
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(target["id"])
        with pytest.raises(RuntimeError, match="busy"):
            store.maintain_job(target["id"], lambda *_: {})
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), OSError)
    store.delete(target["id"])


@pytest.mark.parametrize("delete_before_lock", [False, True])
def test_target_cancelled_or_deleted_before_reservation_receives_no_files(source, monkeypatch, delete_before_lock):
    store, job_id = source
    original_create = store.create
    targets = []

    def cancel_immediately(*args, **kwargs):
        created = original_create(*args, **kwargs)
        targets.append(created["id"])
        store.cancel(created["id"])
        if delete_before_lock:
            store.delete(created["id"])
        return created

    def unexpected_copy(*_):
        pytest.fail("Files must not be staged after cancellation or deletion")

    monkeypatch.setattr(store, "create", cancel_immediately)
    monkeypatch.setattr(existing_analysis, "_copy_analysis_input", unexpected_copy)
    expected_error = FileNotFoundError if delete_before_lock else RuntimeError
    with pytest.raises(expected_error):
        create_analysis(store, job_id)
    if delete_before_lock:
        assert not store.job_dir(targets[0]).exists()
        with pytest.raises(FileNotFoundError):
            store.get(targets[0])
    else:
        assert store.get(targets[0])["status"] == "cancelled"
        assert list(store.job_dir(targets[0]).glob("input.*")) == []
    assert store.claim_next("worker") is None


def test_cancellation_racing_the_final_queue_transition_is_a_conflict(source, monkeypatch):
    store, job_id = source
    original_start = store.start
    targets = []

    def cancel_before_start(target_id, *args, **kwargs):
        targets.append(target_id)
        store.cancel(target_id)
        return original_start(target_id, *args, **kwargs)

    monkeypatch.setattr(store, "start", cancel_before_start)
    with pytest.raises(RuntimeError, match="cancelled"):
        create_analysis(store, job_id)
    assert store.get(targets[0])["status"] == "cancelled"
    assert list(store.job_dir(targets[0]).glob("input.*")) == []
    assert store.claim_next("worker") is None


def test_input_listing_cannot_race_source_cleanup(source, monkeypatch):
    store, job_id = source
    entered, release = threading.Event(), threading.Event()
    original_list = existing_analysis.relative_files

    def slow_list(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_list(*args, **kwargs)

    monkeypatch.setattr(existing_analysis, "relative_files", slow_list)
    thread, outcome = start_thread(lambda: analysis_inputs(store, job_id))
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="busy"):
            store.maintain_job(job_id, lambda *_: {})
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert "error" not in outcome
    assert [item["path"] for item in outcome["result"]["files"]["trajectory_file"]] == ["md.xtc"]
