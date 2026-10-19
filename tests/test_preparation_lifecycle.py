import asyncio
import hashlib
import io
import json
import threading
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile

import app.main as main
from app.job_store import JobStore
from app.models import BenchmarkRequest
from app.reproducibility import write_experiment_manifest


def settings(root):
    return {"runtime_root": str(root), "gmx_bin": "/bin/true", "max_parallel": 1, "default_ntmpi": 1, "default_ntomp": 1}


def test_manifest_is_complete_before_worker_can_claim(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(main, "current_settings", lambda: settings(tmp_path))
    observed = []

    def manifest(meta, **kwargs):
        observed.append(meta["status"])
        assert store.claim_next("racing-worker") is None
        return {"input_files": [{"path": "run.tpr", "sha256": "a" * 64}]}

    monkeypatch.setattr(main, "write_experiment_manifest", manifest)
    job = asyncio.run(
        main.create_job(
            params=json.dumps({"workflow": "run_tpr", "tpr_file": "run.tpr", "dry_run": True}),
            files=[UploadFile(filename="run.tpr", file=io.BytesIO(b"test input"))],
        )
    )
    assert observed == ["preparing"]
    assert job["status"] == "queued"
    assert store.claim_next("worker")["input_hashes"][0]["sha256"] == "a" * 64


def test_manifest_failure_never_queues_task(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(main, "current_settings", lambda: settings(tmp_path))
    with patch.object(main, "write_experiment_manifest", side_effect=OSError("disk full")):
        with pytest.raises(HTTPException, match="experiment manifest"):
            asyncio.run(main.create_job(params='{"workflow":"custom","custom_command":"gmx rms --help","dry_run":true}', files=[]))
    assert store.list()[0]["status"] == "failed"
    assert store.claim_next("worker") is None
    assert list((tmp_path / "incoming").iterdir()) == []


def test_preparation_keeps_async_requests_responsive(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(main, "current_settings", lambda: settings(tmp_path))
    entered, release = threading.Event(), threading.Event()
    released_in_time = []

    def slow_manifest(meta, **kwargs):
        entered.set()
        released_in_time.append(release.wait(2))
        return {"input_files": []}

    async def scenario():
        task = asyncio.create_task(
            main.create_job(params='{"workflow":"custom","custom_command":"gmx rms --help","dry_run":true}', files=[])
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0)
            release.set()
            await task
        finally:
            release.set()

    monkeypatch.setattr(main, "write_experiment_manifest", slow_manifest)
    asyncio.run(scenario())
    assert released_in_time == [True]


def test_cancel_upload_during_manifest_cannot_delete_or_requeue_target(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(main, "current_settings", lambda: settings(tmp_path))
    entered, release = threading.Event(), threading.Event()
    preparing_ids = []

    def slow_manifest(meta, **kwargs):
        preparing_ids.append(meta["id"])
        entered.set()
        assert release.wait(5), "Test did not release the blocked manifest writer"
        return {"input_files": [{"path": "run.tpr", "sha256": "a" * 64}]}

    async def scenario():
        task = asyncio.create_task(
            main.create_job(
                params='{"workflow":"run_tpr","tpr_file":"run.tpr","dry_run":true}',
                files=[UploadFile(filename="run.tpr", file=io.BytesIO(b"input being prepared"))],
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            job_id = preparing_ids[0]
            assert store.get(job_id)["status"] == "preparing"
            assert store.claim_next("racing-worker") is None
            assert main.cancel_job(job_id)["status"] == "cancelled"
            with pytest.raises(HTTPException) as failure:
                main.delete_job(job_id)
            assert failure.value.status_code == 409
            assert "busy" in failure.value.detail
            assert (store.job_dir(job_id) / "run.tpr").read_bytes() == b"input being prepared"
        finally:
            release.set()
            result = await asyncio.wait_for(task, 5)
        return result

    monkeypatch.setattr(main, "write_experiment_manifest", slow_manifest)
    job = asyncio.run(scenario())
    assert job["status"] == "cancelled"
    assert store.get(job["id"])["status"] == "cancelled"
    assert store.claim_next("worker-after-preparation") is None
    assert main.delete_job(job["id"])["deleted"]


@pytest.mark.parametrize("blocked_stage", ["copy", "manifest"])
def test_benchmark_preparation_locks_source_and_cancelled_target(tmp_path, monkeypatch, blocked_stage):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "current_settings", lambda: settings(tmp_path))
    monkeypatch.setattr(main, "host_information", lambda: {"physical_core_count": 1, "cpu_count": 2})
    source = store.create("completed source", {"workflow": "run_tpr", "gmx_bin": "/bin/true"}, ["source.tpr"], [])
    source_tpr = store.job_dir(source["id"]) / "source.tpr"
    source_tpr.write_bytes(b"source TPR must survive the whole copy")
    store.start(source["id"], [])
    assert store.claim_next("source-worker")["id"] == source["id"]
    store.update(source["id"], status="completed", desired_action=None, worker_id=None)
    entered, release = threading.Event(), threading.Event()
    original_copy = main.shutil.copy2

    def slow_copy(source_path, destination_path, *args, **kwargs):
        if blocked_stage == "copy":
            entered.set()
            assert release.wait(5), "Test did not release the blocked benchmark copy"
        return original_copy(source_path, destination_path, *args, **kwargs)

    def slow_manifest(meta, **kwargs):
        if blocked_stage == "manifest":
            entered.set()
            assert release.wait(5), "Test did not release the blocked benchmark manifest"
        return {"input_files": [{"path": "benchmark.tpr", "sha256": "b" * 64}]}

    async def scenario():
        task = asyncio.create_task(
            asyncio.to_thread(main.benchmark_job, source["id"], BenchmarkRequest(threads=[1], nsteps=1000, gpu=False))
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            target = next(job for job in store.list() if job["workflow"] == "benchmark")
            assert target["status"] == "preparing"
            assert store.claim_next("racing-benchmark-worker") is None
            assert main.cancel_job(target["id"])["status"] == "cancelled"
            for job_id in (target["id"], source["id"]):
                with pytest.raises(HTTPException) as failure:
                    main.delete_job(job_id)
                assert failure.value.status_code == 409
                assert "busy" in failure.value.detail
                assert store.job_dir(job_id).is_dir()
            assert source_tpr.read_bytes() == b"source TPR must survive the whole copy"
        finally:
            release.set()
            result = await asyncio.wait_for(task, 5)
        return result

    monkeypatch.setattr(main.shutil, "copy2", slow_copy)
    monkeypatch.setattr(main, "write_experiment_manifest", slow_manifest)
    job = asyncio.run(scenario())
    assert job["status"] == "cancelled"
    assert store.get(source["id"])["status"] == "completed"
    assert (store.job_dir(job["id"]) / "benchmark.tpr").read_bytes() == source_tpr.read_bytes()
    assert store.claim_next("worker-after-benchmark-preparation") is None
    assert main.delete_job(job["id"])["deleted"]
    assert main.delete_job(source["id"])["deleted"]


def test_upload_hash_is_reused_only_for_unchanged_file(tmp_path):
    from app.files import stage_uploads

    digest = {}
    asyncio.run(stage_uploads([UploadFile(filename="run.tpr", file=io.BytesIO(b"input"))], tmp_path, hashes=digest))
    meta = {"workdir": str(tmp_path), "uploaded_files": ["run.tpr"]}
    with patch("app.reproducibility.detect_gromacs", return_value={}), patch("app.reproducibility.gpu_information", return_value={}):
        with patch("app.reproducibility.sha256_file", side_effect=AssertionError("unnecessary reread")):
            manifest = write_experiment_manifest(meta, prepared_hashes=digest)
        assert manifest["input_files"][0]["sha256"] == hashlib.sha256(b"input").hexdigest()
        (tmp_path / "run.tpr").write_bytes(b"changed input")
        manifest = write_experiment_manifest(meta, prepared_hashes=digest)
        assert manifest["input_files"][0]["sha256"] == hashlib.sha256(b"changed input").hexdigest()


def test_sqlite_state_does_not_depend_on_json_export(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("canonical", {}, [], [])
    with patch.object(store, "export_metadata", side_effect=OSError("export unavailable")):
        store.cancel(job["id"])
    assert JobStore(tmp_path).get(job["id"])["status"] == "cancelled"


def test_stale_export_cannot_resurrect_deleted_task(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("gone", {}, [], [])
    store.cancel(job["id"])
    exported = store.export_metadata(job["id"]).read_bytes()
    store.delete(job["id"])
    old_dir = tmp_path / "jobs" / job["id"]
    old_dir.mkdir()
    (old_dir / "metadata.json").write_bytes(exported)
    restarted = JobStore(tmp_path)
    assert restarted.list() == []


def test_old_research_reports_remain_searchable_after_200_other_jobs(tmp_path):
    store = JobStore(tmp_path)
    report = store.create("很早的研究%_", {}, [], [])
    (store.job_dir(report["id"]) / "research-report.json").write_text("{}")
    store.start(report["id"], [])
    store.claim_next("worker")
    store.update(report["id"], status="completed")
    for index in range(205):
        store.create(f"ordinary-{index}", {}, [], [])
    page = store.research_page(search="%_")
    assert page["total"] == 1
    assert page["jobs"][0]["id"] == report["id"]
