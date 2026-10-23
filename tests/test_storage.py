from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.job_store import JobStore
from app.storage import CleanupRequest, execute_cleanup, preview_cleanup, storage_overview


def terminal_job(store, *, workflow="protein_md", uploaded=None, params=None):
    meta = store.create("cleanup test", {"workflow": workflow, **(params or {})}, uploaded or [], [])
    store.start(meta["id"], [])
    store.claim_next("worker:test")
    return store.update(meta["id"], status="completed", desired_action=None)


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "runtime")


def write_files(store, job, names):
    root = store.job_dir(job["id"])
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test data")
    return root


def execute_preview(store, request):
    preview = preview_cleanup(store, request)
    return preview, execute_cleanup(store, request.model_copy(update={"preview_token": preview["preview_token"]}))


def test_default_cleanup_preserves_inputs_production_checkpoint_and_reports(store):
    job = terminal_job(store, uploaded=["em.trr", "protein.pdb"])
    protected = [
        "em.trr",
        "protein.pdb",
        "md.xtc",
        "md.trr",
        "md.cpt",
        "nvt.cpt",
        "topol.top",
        "nvt.mdp",
        "analysis/report.json",
        "report.pdf",
        "md.log",
        "#md.xtc.1#",
        "#nvt.cpt.1#",
    ]
    disposable = ["nvt.trr", "npt.xtc", "#nvt.trr.1#"]
    root = write_files(store, job, protected + disposable)
    request = CleanupRequest(job_ids=[job["id"]])
    preview = preview_cleanup(store, request)
    assert {item["path"] for item in preview["files"]} == set(disposable)
    assert all((root / name).exists() for name in disposable), "Preview must never delete files"
    result = execute_cleanup(store, request.model_copy(update={"preview_token": preview["preview_token"]}))
    assert result["deleted_files"] == 3
    assert not result["skipped_jobs"]
    assert all((root / name).exists() for name in protected)
    assert all(not (root / name).exists() for name in disposable)
    assert store.get(job["id"])["storage_revision"] == 1
    assert store.get(job["id"])["last_cleanup"]["deleted_files"] == 3


def test_logs_need_explicit_option_and_preserve_uploaded_logs(store):
    job = terminal_job(store, uploaded=["input.log"])
    root = write_files(store, job, ["md.log", "#md.log.1#", "input.log", "#input.log.1#"])
    preview, result = execute_preview(store, CleanupRequest(job_ids=[job["id"]], include_logs=True))
    assert {file["path"] for file in preview["files"]} == {"md.log", "#md.log.1#", "run.log"}
    assert result["deleted_files"] == 3
    assert (root / "input.log").exists()
    assert (root / "#input.log.1#").exists()


@pytest.mark.parametrize("workflow,params", [("em_only", {}), ("custom", {}), ("protein_md", {"production_deffnm": "nvt"})])
def test_final_simulation_stage_is_preserved(store, workflow, params):
    job = terminal_job(store, workflow=workflow, params=params)
    root = write_files(store, job, ["em.trr", "nvt.xtc"])
    _, result = execute_preview(store, CleanupRequest(job_ids=[job["id"]]))
    if workflow in {"em_only", "custom"}:
        assert result["deleted_files"] == 0
        assert (root / "em.trr").exists()
    assert (root / "nvt.xtc").exists()


def test_explicit_disposable_internal_trajectory_can_be_removed(store):
    job = terminal_job(store, workflow="analysis_suite")
    store.update(job["id"], commands=[{"kind": "internal", "operation": "remove_files", "data": {"paths": ["tmp/whole.xtc"]}}])
    root = write_files(store, job, ["tmp/whole.xtc", "md.xtc"])
    _, result = execute_preview(store, CleanupRequest(job_ids=[job["id"]]))
    assert result["deleted_files"] == 1
    assert not (root / "tmp/whole.xtc").exists()
    assert (root / "md.xtc").exists()


@pytest.mark.parametrize("status", ["preparing", "queued", "running", "failed", "interrupted"])
def test_nonterminal_tasks_are_protected(store, status):
    job = store.create("protected", {"workflow": "protein_md"}, [], [])
    if status != "preparing":
        store.start(job["id"], [])
        if status not in {"queued"}:
            store.claim_next("worker:test")
            if status != "running":
                store.update(job["id"], status=status, desired_action=None)
    root = write_files(store, job, ["nvt.trr"])
    preview, result = execute_preview(store, CleanupRequest(job_ids=[job["id"]], mode="delete_jobs"))
    assert preview["protected_jobs"]
    assert not result["deleted_jobs"]
    assert (root / "nvt.trr").exists()


def test_full_task_delete_requires_matching_preview_and_removes_record(store):
    job = terminal_job(store)
    root = write_files(store, job, ["md.xtc", "md.cpt", "topol.top", "report.json"])
    request = CleanupRequest(job_ids=[job["id"]], mode="delete_jobs")
    preview, result = execute_preview(store, request)
    assert result["deleted_jobs"] == [job["id"]]
    assert result["deleted_bytes"] == preview["total_bytes"]
    assert not result["skipped_jobs"]
    assert not root.exists()
    with pytest.raises(FileNotFoundError):
        store.get(job["id"])


@pytest.mark.parametrize("mode", ["intermediates", "delete_jobs"])
def test_files_modified_since_preview_are_not_deleted(store, mode):
    job = terminal_job(store)
    root = write_files(store, job, ["nvt.trr"])
    request = CleanupRequest(job_ids=[job["id"]], mode=mode)
    preview = preview_cleanup(store, request)
    (root / "nvt.trr").write_bytes(b"changed data")
    result = execute_cleanup(store, request.model_copy(update={"preview_token": preview["preview_token"]}))
    assert result["skipped_jobs"]
    assert not result["deleted_files"]
    assert (root / "nvt.trr").exists()


def test_state_change_after_preview_is_rechecked_inside_transaction(store):
    job = terminal_job(store)
    root = write_files(store, job, ["nvt.trr"])
    request = CleanupRequest(job_ids=[job["id"]])
    preview = preview_cleanup(store, request)
    store.update(job["id"], desired_action="cancel")
    result = execute_cleanup(store, request.model_copy(update={"preview_token": preview["preview_token"]}))
    assert result["skipped_jobs"]
    assert (root / "nvt.trr").exists()


def test_active_saved_process_is_protected(store):
    job = terminal_job(store)
    write_files(store, job, ["nvt.trr"])
    with patch("app.storage.live_process_identities", return_value=[{"pid": os.getpid()}]):
        preview = preview_cleanup(store, CleanupRequest(job_ids=[job["id"]]))
    assert preview["protected_jobs"]


def test_preview_is_bound_to_selection_and_single_use(store):
    job = terminal_job(store)
    write_files(store, job, ["nvt.trr"])
    request = CleanupRequest(job_ids=[job["id"]])
    preview = preview_cleanup(store, request)
    token = preview["preview_token"]
    with pytest.raises(ValueError, match="不一致"):
        execute_cleanup(store, CleanupRequest(job_ids=[job["id"]], mode="delete_jobs", preview_token=token))
    execute_cleanup(store, request.model_copy(update={"preview_token": token}))
    with pytest.raises(ValueError, match="过期或已使用"):
        execute_cleanup(store, request.model_copy(update={"preview_token": token}))


def test_expired_preview_is_rejected(store):
    job = terminal_job(store)
    request = CleanupRequest(job_ids=[job["id"]])
    with patch("app.storage.time.time", return_value=1000):
        preview = preview_cleanup(store, request)
    with patch("app.storage.time.time", return_value=1601), pytest.raises(ValueError, match="过期"):
        execute_cleanup(store, request.model_copy(update={"preview_token": preview["preview_token"]}))


@pytest.mark.parametrize("job_id", ["..", ".", "../../outside", "one/two", "one\\two", "a\x00b"])
def test_cleanup_rejects_traversal_and_invalid_ids(job_id):
    with pytest.raises(ValidationError):
        CleanupRequest(job_ids=[job_id])


def test_symlinked_file_or_task_is_never_cleaned(store, tmp_path):
    job = terminal_job(store)
    outside = tmp_path / "outside"
    outside.write_bytes(b"important")
    root = write_files(store, job, ["nvt.trr"])
    (root / "link").symlink_to(outside)
    request = CleanupRequest(job_ids=[job["id"]], mode="delete_jobs")
    preview, result = execute_preview(store, request)
    assert preview["protected_jobs"]
    assert not result["deleted_files"]
    assert outside.read_bytes() == b"important"
    (root / "link").unlink()
    renamed = root.with_name(root.name + "-real")
    root.rename(renamed)
    root.symlink_to(renamed, target_is_directory=True)
    preview = preview_cleanup(store, request)
    assert preview["protected_jobs"]
    assert outside.exists()


def test_symlink_inserted_after_preview_preserves_external_target(store, tmp_path):
    job = terminal_job(store, workflow="analysis_suite")
    store.update(job["id"], commands=[{"kind": "internal", "operation": "remove_files", "data": {"paths": ["tmp/whole.xtc"]}}])
    root = write_files(store, job, ["tmp/whole.xtc"])
    request = CleanupRequest(job_ids=[job["id"]])
    preview = preview_cleanup(store, request)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "whole.xtc").write_bytes(b"important")
    (root / "tmp/whole.xtc").unlink()
    (root / "tmp").rmdir()
    (root / "tmp").symlink_to(outside, target_is_directory=True)
    result = execute_cleanup(store, request.model_copy(update={"preview_token": preview["preview_token"]}))
    assert result["skipped_jobs"]
    assert (outside / "whole.xtc").read_bytes() == b"important"


def test_overview_reports_runtime_and_protected_tasks_without_following_links(store, tmp_path):
    job = terminal_job(store, uploaded=["input.pdb"])
    root = write_files(store, job, ["input.pdb", "nvt.trr", "md.xtc", "md.cpt", "topol.top"])
    overview = storage_overview(store)
    assert overview["disk"]["free_bytes"] > 0
    assert overview["runtime"]["bytes"] >= overview["runtime"]["task_bytes"]
    assert overview["categories"]["inputs"] == 9
    assert overview["categories"]["trajectories"] == 18
    assert overview["reclaimable_bytes"] == 9
    external = tmp_path / "big-external"
    external.write_bytes(b"x" * 10000)
    (root / "external").symlink_to(external)
    protected = storage_overview(store)
    assert protected["jobs"][0]["bytes"] == overview["jobs"][0]["bytes"]
    assert protected["jobs"][0]["eligible"] is False


def test_two_previews_cannot_both_clean_the_same_revision(store):
    job = terminal_job(store)
    write_files(store, job, ["nvt.trr"])
    request = CleanupRequest(job_ids=[job["id"]])
    previews = [preview_cleanup(store, request), preview_cleanup(store, request)]
    results = []
    threads = [
        threading.Thread(
            target=lambda item=item: results.append(
                execute_cleanup(store, request.model_copy(update={"preview_token": item["preview_token"]}))
            )
        )
        for item in previews
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result["deleted_files"] for result in results) == 1
    assert sum(bool(result["skipped_jobs"]) for result in results) == 1
    assert store.get(job["id"])["storage_revision"] == 1


def test_stale_preview_preserves_successful_cleanup_record(store):
    job = terminal_job(store)
    write_files(store, job, ["nvt.trr"])
    request = CleanupRequest(job_ids=[job["id"]])
    first, stale = preview_cleanup(store, request), preview_cleanup(store, request)
    result = execute_cleanup(store, request.model_copy(update={"preview_token": first["preview_token"]}))
    assert result["deleted_files"] == 1
    completed = store.get(job["id"])

    rejected = execute_cleanup(store, request.model_copy(update={"preview_token": stale["preview_token"]}))
    assert rejected["skipped_jobs"]
    assert rejected["deleted_files"] == 0
    current = store.get(job["id"])
    assert current["storage_revision"] == completed["storage_revision"] == 1
    assert current["last_cleanup"] == completed["last_cleanup"]
    assert not current.get("last_cleanup_error")
