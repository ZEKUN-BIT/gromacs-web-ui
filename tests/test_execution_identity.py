"""Keep execution identity stable and make completed captures immediately visible."""

import json
import subprocess

from app import execution, main, reproducibility
from app.execution import WorkerEngine
from app.job_store import JobStore


def test_source_fingerprint_includes_untracked_runtime_modules(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True)

    git("init")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/tracked.py").write_text("VALUE = 1\n")
    git("add", "app/tracked.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    original = reproducibility._source_identity(tmp_path)
    untracked = tmp_path / "app/new_operation.py"
    untracked.write_text("VALUE = 2\n")
    added = reproducibility._source_identity(tmp_path)
    untracked.write_text("VALUE = 3\n")
    modified = reproducibility._source_identity(tmp_path)
    assert original["revision"] == added["revision"] == modified["revision"]
    assert original["dirty"] is False
    assert added["dirty"] is modified["dirty"] is True
    assert len({item["content_sha256"] for item in (original, added, modified)}) == 3


def test_worker_captures_software_at_startup_not_at_job_completion(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "runtime")
    job = store.create("startup identity", {"workflow": "custom"}, [], [])
    initial = {"source": {"content_sha256": "initial worker source"}}
    monkeypatch.setattr(reproducibility, "execution_software", lambda: initial)
    monkeypatch.setattr(reproducibility, "detect_gromacs", lambda _: {})
    engine = WorkerEngine(store, "test")
    monkeypatch.setattr(reproducibility, "execution_software", lambda: {"source": {"content_sha256": "changed on disk"}})
    store.start(job["id"], [])
    assert engine.run_once()
    manifest = json.loads((store.job_dir(job["id"]) / "execution-manifest.json").read_text())
    assert manifest["software"] == initial


def test_manifest_capture_invalidates_a_terminal_file_list_cached_before_write(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "runtime")
    job = store.create("manifest visibility", {"workflow": "custom"}, [], [])
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "FILE_LIST_CACHE", {})
    monkeypatch.setattr(reproducibility, "execution_software", dict)
    monkeypatch.setattr(reproducibility, "detect_gromacs", lambda _: {})

    def capture(meta, **kwargs):
        assert meta["status"] == "completed"
        assert "execution-manifest.json" not in {item["path"] for item in main.job_files(job["id"])["files"]}
        return reproducibility.write_execution_manifest(meta, **kwargs)

    monkeypatch.setattr(execution, "write_execution_manifest", capture)
    store.start(job["id"], [])
    assert WorkerEngine(store, "test").run_once()
    assert "execution-manifest.json" in {item["path"] for item in main.job_files(job["id"])["files"]}
