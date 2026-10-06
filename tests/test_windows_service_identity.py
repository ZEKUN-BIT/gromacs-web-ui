"""Protect disposable job processes even when installed app code and venv are absent."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.execution import wait_process_identity

SOURCE = Path(__file__).resolve().parents[1] / "packaging/windows/desktop_service.py"
spec = importlib.util.spec_from_file_location("windows_identity_service", SOURCE)
assert spec and spec.loader
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="WSL job identities use Linux /proc")


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "installation"
    workdir = root / "runtime/jobs/job"
    workdir.mkdir(parents=True)
    (workdir / "simulation.cpt").write_bytes(b"preserve checkpoint")
    (root / "tools").mkdir()
    (root / "tools/keep").write_text("retain")
    (root / "service.json").write_text('{"processes": []}')
    with sqlite3.connect(root / "runtime/jobs.sqlite3") as connection:
        connection.execute("CREATE TABLE jobs(id TEXT, status TEXT, desired_action TEXT, meta_json TEXT)")
    return root, workdir


@pytest.fixture
def finite_children():
    children = []

    def spawn(workdir):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "isolated-job"],
            cwd=workdir,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        identity = wait_process_identity(child.pid)
        assert identity is not None
        return child, identity

    yield spawn
    for child in children:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait(timeout=2)


def save_job(root, meta, *, status="failed", action=None, job_id="job"):
    with sqlite3.connect(root / "runtime/jobs.sqlite3") as connection:
        connection.execute("DELETE FROM jobs")
        connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)", (job_id, status, action, json.dumps(meta)))


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed", "interrupted"])
@pytest.mark.parametrize("registry", [False, True])
def test_stop_and_remove_protect_live_legacy_and_parallel_job_identities(installation, finite_children, tmp_path, status, registry):
    root, workdir = installation
    child, identity = finite_children(workdir)
    # A parallel member must still protect the job when the top-level PID is stale.
    meta = {"process_pid": 99999999, "process_identities": [identity]} if registry else identity
    save_job(root, meta, status=status)
    assert not (root / "app").exists()
    assert not (root / ".venv").exists()
    standalone = tmp_path / "payload/desktop_service.py"
    standalone.parent.mkdir()
    shutil.copyfile(SOURCE, standalone)
    for action in ("stop", "remove"):
        with pytest.raises(RuntimeError, match="verified live processes"):
            service.manage(root, action)
        # Isolated mode excludes repository/PYTHONPATH imports. Only stdlib is available.
        result = subprocess.run(
            [sys.executable, "-I", str(standalone), action, "--app-root", str(root)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 1
        assert "verified live processes" in result.stderr
        assert child.poll() is None
        assert (root / "service.json").exists()
        assert (root / "tools/keep").read_text() == "retain"
        assert (workdir / "simulation.cpt").read_bytes() == b"preserve checkpoint"


def test_subdirectory_job_identity_matches_execution_schema(installation, finite_children):
    root, workdir = installation
    nested = workdir / "ligand.acpype"
    nested.mkdir()
    _, identity = finite_children(nested)
    save_job(root, {"process_identities": [identity]})
    assert service.active_jobs(root)


@pytest.mark.parametrize("field", ["process_started_ticks", "process_command_fingerprint", "process_pgid", "process_cwd"])
def test_reused_pid_and_mismatched_identity_do_not_block_or_signal(installation, finite_children, field):
    root, workdir = installation
    child, identity = finite_children(workdir)
    if field in {"process_started_ticks", "process_pgid"}:
        identity[field] -= 1
    elif field == "process_cwd":
        identity[field] = str(root / "tools")
    else:
        identity[field] = "stale fingerprint"
    save_job(root, {"process_identities": [identity]})
    assert service.manage(root, "stop") == {"status": "stopped"}
    assert child.poll() is None
    assert (workdir / "simulation.cpt").exists()


@pytest.mark.parametrize("escape", ["record", "job_id", "job_symlink", "jobs_symlink"])
def test_external_workdirs_are_not_treated_as_this_installations_processes(installation, finite_children, tmp_path, escape):
    root, workdir = installation
    outside = tmp_path / "external-job"
    outside.mkdir()
    child, identity = finite_children(outside)
    job_id = "job"
    if escape == "job_id":
        job_id = str(outside)
    elif escape == "job_symlink":
        shutil.rmtree(workdir)
        workdir.symlink_to(outside, target_is_directory=True)
    elif escape == "jobs_symlink":
        shutil.rmtree(workdir.parent)
        workdir.parent.symlink_to(tmp_path, target_is_directory=True)
        job_id = outside.name
    save_job(root, identity, job_id=job_id)
    assert service.manage(root, "stop") == {"status": "stopped"}
    assert child.poll() is None


def test_exited_registered_process_does_not_block_remove(installation, finite_children):
    root, workdir = installation
    child, identity = finite_children(workdir)
    save_job(root, {"process_identities": [identity]}, status="cancelled")
    child.terminate()
    child.wait(timeout=2)
    assert service.manage(root, "remove") == {"status": "stopped"}
    assert (workdir / "simulation.cpt").read_bytes() == b"preserve checkpoint"
    assert not (root / "tools").exists()


def test_interrupted_pending_cancel_blocks_without_live_process_or_installed_code(installation):
    root, _ = installation
    save_job(root, {}, status="interrupted", action="cancel")
    for action in ("stop", "remove"):
        with pytest.raises(RuntimeError, match="waiting to resume/adopt/cancel"):
            service.manage(root, action)
