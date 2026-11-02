"""Capture executed protocols without changing the submitted input record."""

import hashlib
import json
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from app.execution import WorkerEngine
from app.gromacs import Step, build_steps
from app.job_store import JobStore
from app.mdp import prepare_replica_mdp
from app.reproducibility import write_execution_manifest, write_experiment_manifest
from app.result_bundle import build_result_bundle, quality_summary
from app.workflows.common import as_gmx


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr("app.reproducibility.detect_gromacs", lambda _: {"version": "test GROMACS"})
    monkeypatch.setattr("app.reproducibility.execution_software", lambda: {"source": {"revision": "test revision"}})
    monkeypatch.setattr("app.reproducibility.host_information", dict)
    monkeypatch.setattr("app.reproducibility.gpu_information", dict)


def create_job(tmp_path, steps):
    store = JobStore(tmp_path / "runtime")
    job = store.create("protocol capture", {"workflow": "custom"}, [], steps)
    return store, job, store.job_dir(job["id"])


def test_final_protocols_and_hashes_preserve_the_original_manifest(tmp_path):
    (tmp_path / "md.mdp").write_text("nsteps = 10\ngen_seed = -1\n")
    meta = {"workdir": str(tmp_path), "status": "completed"}
    submitted = write_experiment_manifest(meta)
    original = (tmp_path / "experiment-manifest.json").read_bytes()
    (tmp_path / "md.mdp").write_text("nsteps = 20\ngen_seed = 42\nld_seed = -1\n")
    (tmp_path / "md.tpr").write_bytes(b"actual compiled protocol")
    manifest = write_execution_manifest(meta)
    files = {item["path"]: item for item in manifest["protocol_files"]}
    assert (tmp_path / "experiment-manifest.json").read_bytes() == original
    assert files["md.mdp"]["sha256"] != submitted["input_files"][0]["sha256"]
    assert files["md.mdp"]["parameters"]["nsteps"] == "20"
    assert files["md.mdp"]["seeds"] == {"gen-seed": "42", "ld-seed": "-1"}
    assert files["md.mdp"]["unresolved_seeds"] == ["ld-seed"]
    assert files["md.tpr"]["sha256"] == hashlib.sha256(b"actual compiled protocol").hexdigest()
    assert manifest["input_manifest"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert manifest["software"]["source"]["revision"] == "test revision"


def test_unsafe_protocol_paths_are_reported_without_reading_their_targets(tmp_path):
    outside = tmp_path / "private.mdp"
    outside.write_text("gen_seed = 777\n")
    root = tmp_path / "job"
    root.mkdir()
    (root / "linked.mdp").symlink_to(outside)
    (root / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    names = ["../private.mdp", str(outside), "linked-dir/private.mdp", "missing.tpr"]
    meta = {"workdir": str(root), "commands": [{"args": ["gmx", "grompp", "-f", name]} for name in names]}
    manifest = write_execution_manifest(meta)
    assert manifest["protocol_files"] == []
    assert {item["path"] for item in manifest["unavailable_files"]} == {*names, "linked.mdp"}
    target = root / "execution-manifest.json"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        write_execution_manifest(meta)
    assert outside.read_text() == "gen_seed = 777\n"


def test_processed_mdp_outputs_are_unique_for_every_preparation_stage():
    steps = build_steps({"workflow": "protein_md", "structure_file": "protein.pdb", "replicas": 3}, ["protein.pdb"])
    names = []
    for step in steps:
        if "grompp" in step.args:
            output = step.args[step.args.index("-o") + 1]
            processed = step.args[step.args.index("-po") + 1]
            assert processed == str(Path(output).with_suffix(".mdout.mdp"))
            names.append(processed)
    assert len(names) >= 6
    assert len(names) == len(set(names))
    explicit = ["grompp", "-o", "md.tpr", "-po", "custom.mdp"]
    assert as_gmx("gmx", explicit) == ["gmx", *explicit]


def test_replica_auto_seed_is_selected_and_saved_before_grompp(tmp_path):
    (tmp_path / "md.mdp").write_text("continuation = yes\ngen_vel = no\ngen_seed = -1\n")
    with patch("app.mdp.secrets.randbelow", side_effect=[98, 199]):
        for replica in (1, 2):
            prepare_replica_mdp(tmp_path, {"source_mdp": "md.mdp", "output_mdp": f"replica-{replica}.mdp", "seed": -1})
    manifest = write_execution_manifest({"workdir": str(tmp_path)})
    files = {item["path"]: item for item in manifest["protocol_files"]}
    assert files["replica-1.mdp"]["seeds"]["gen-seed"] == "99"
    assert files["replica-2.mdp"]["seeds"]["gen-seed"] == "200"
    assert files["md.mdp"]["seeds"]["gen-seed"] == "-1"


def test_worker_completion_captures_protocol_changes_seed_messages_and_export(tmp_path):
    code = (
        "from pathlib import Path; Path('md.mdp').write_text('nsteps = 20\\ngen_seed = 123\\n'); "
        "Path('md.tpr').write_bytes(b'final tpr'); print('Setting the LD random seed to 456')"
    )
    steps = [Step("prepare", [sys.executable, "-c", code])]
    store, job, root = create_job(tmp_path, steps)
    (root / "md.mdp").write_text("nsteps = 10\ngen_seed = -1\n")
    write_experiment_manifest(job)
    original = (root / "experiment-manifest.json").read_bytes()
    store.start(job["id"], steps)
    assert WorkerEngine(store, "test").run_once()
    manifest = json.loads((root / "execution-manifest.json").read_text())
    assert manifest["job"]["status"] == "completed"
    assert manifest["step_runs"][0]["seed_messages"] == ["Setting the LD random seed to 456"]
    assert manifest["step_runs"][0]["status"] == "completed"
    assert (root / "experiment-manifest.json").read_bytes() == original
    assert quality_summary(root) == []
    with build_result_bundle(store, job["id"]) as output, zipfile.ZipFile(output) as bundle:
        assert "files/execution-manifest.json" in bundle.namelist()
        assert json.loads(bundle.read("files/execution-manifest.json")) == manifest


def test_failed_step_still_gets_a_final_protocol_manifest(tmp_path):
    steps = [Step("failure", [sys.executable, "-c", "raise SystemExit(3)"])]
    store, job, root = create_job(tmp_path, steps)
    store.start(job["id"], steps)
    assert WorkerEngine(store, "test").run_once()
    manifest = json.loads((root / "execution-manifest.json").read_text())
    assert manifest["job"]["status"] == "failed"
    assert manifest["step_runs"][0]["exit_code"] == 3


def test_cancelled_and_orphaned_runs_get_final_manifests(tmp_path):
    steps = [Step("MD", ["gmx", "mdrun", "-deffnm", "md"])]
    for outcome in ("cancelled", "interrupted"):
        store, job, root = create_job(tmp_path / outcome, steps)
        store.start(job["id"], steps)
        store.claim_next("lost worker")
        store.update(job["id"], step_index=1, step_runs=[{"status": "running"}])
        engine = WorkerEngine(store, "test")
        if outcome == "cancelled":
            engine._cancel_job(store.get(job["id"]))
        else:
            engine.recover_orphaned_jobs()
        manifest = json.loads((root / "execution-manifest.json").read_text())
        assert manifest["job"]["status"] == outcome
        assert manifest["step_runs"][0]["status"] == outcome


def test_adopted_completion_records_finished_step(tmp_path):
    steps = [Step("MD", ["gmx", "mdrun", "-deffnm", "md"])]
    store, job, root = create_job(tmp_path, steps)
    store.start(job["id"], steps)
    store.claim_next("test")
    meta = store.update(
        job["id"], step_index=1, process_pid=12345, process_pgid=12345, process_args=steps[0].args, step_runs=[{"status": "running"}]
    )
    (root / "md.log").write_text("Finished mdrun")
    identities = iter([True])
    with patch("app.execution.identity_matches", side_effect=lambda *_: next(identities, False)):
        WorkerEngine(store, "test")._adopt(meta)
    manifest = json.loads((root / "execution-manifest.json").read_text())
    assert manifest["job"]["status"] == "completed"
    assert manifest["step_runs"][0]["status"] == "completed"
    assert "completion_evidence" in manifest["step_runs"][0]


def test_manifest_failure_preserves_simulation_success_and_live_process_defers_capture(tmp_path):
    store, job, root = create_job(tmp_path, [])
    store.start(job["id"], [])
    with patch("app.execution.write_execution_manifest", side_effect=OSError("injected storage failure")):
        assert WorkerEngine(store, "test").run_once()
    state = store.get(job["id"])
    assert state["status"] == "completed"
    assert "injected storage failure" in state["manifest_error"]
    with (
        patch("app.execution.live_process_identities", return_value=[{"process_pid": 123}]),
        patch("app.execution.write_execution_manifest") as capture,
    ):
        WorkerEngine(store, "test")._finalize_job(job["id"], status="completed")
    capture.assert_not_called()
    assert "still active" in store.get(job["id"])["manifest_error"]
    assert not (root / "execution-manifest.json").exists()


def test_terminal_state_and_manifest_are_protected_by_the_deletion_lock(tmp_path):
    store, job, _ = create_job(tmp_path, [])
    store.start(job["id"], [])

    def capture(meta, **kwargs):
        assert store.get(job["id"])["status"] == "completed"
        with pytest.raises(RuntimeError, match="busy"):
            store.delete(job["id"])
        return write_execution_manifest(meta, **kwargs)

    with patch("app.execution.write_execution_manifest", side_effect=capture):
        assert WorkerEngine(store, "test").run_once()
    assert store.get(job["id"])["manifest_error"] is None


def test_manifest_metadata_storage_failure_cannot_reclassify_a_completed_simulation(tmp_path, caplog):
    store, job, root = create_job(tmp_path, [])
    store.start(job["id"], [])
    update = store.update

    def fail_manifest_metadata(job_id, **updates):
        if "execution_manifest" in updates or "manifest_error" in updates:
            raise sqlite3.OperationalError("injected manifest metadata outage")
        return update(job_id, **updates)

    with patch.object(store, "update", side_effect=fail_manifest_metadata):
        assert WorkerEngine(store, "test").run_once()
    assert store.get(job["id"])["status"] == "completed"
    assert (root / "execution-manifest.json").is_file()
    assert "injected manifest metadata outage" in caplog.text


def test_transient_terminal_file_lock_failure_preserves_success(tmp_path):
    store, job, root = create_job(tmp_path, [])
    store.start(job["id"], [])
    original_lock = store.job_files_lock
    failed_once = False

    @contextmanager
    def fail_terminal_lock_once(job_id):
        nonlocal failed_once
        if store.get(job_id)["status"] == "running" and not failed_once:
            failed_once = True
            raise OSError("transient manifest lock failure")
        with original_lock(job_id):
            yield

    with patch.object(store, "job_files_lock", fail_terminal_lock_once):
        assert WorkerEngine(store, "test").run_once()
    state = store.get(job["id"])
    assert failed_once
    assert state["status"] == "completed"
    assert state["exit_code"] == 0
    assert "transient manifest lock failure" in state["manifest_error"]
    assert not (root / "execution-manifest.json").exists()


def test_failed_file_lock_does_not_restore_a_concurrently_deleted_job(tmp_path):
    store, job, root = create_job(tmp_path, [])
    store.start(job["id"], [])
    store.claim_next("test")
    store.update(job["id"], status="completed", desired_action=None, worker_id=None)
    original_lock = store.job_files_lock

    @contextmanager
    def deletion_wins_file_lock(job_id):
        with patch.object(store, "job_files_lock", original_lock):
            store.delete(job_id)
        raise OSError("manifest lock became unavailable after deletion")
        yield  # pragma: no cover - marks this as a context manager

    with patch.object(store, "job_files_lock", deletion_wins_file_lock):
        WorkerEngine(store, "test")._finalize_job(job["id"], status="completed")
    with pytest.raises(FileNotFoundError):
        store.get(job["id"])
    assert not root.exists()
