from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

from app.execution import WorkerEngine
from app.gromacs import Step
from app.job_store import JobStore


def analysis_steps(count):
    return [Step(f"analysis {index}", ["/bin/true"], parallel_group="analysis") for index in range(count)]


def enqueue(store, steps, name="analysis", params=None):
    job = store.create(name, params or {}, [], steps)
    store.start(job["id"], steps)
    return job


def test_concurrent_analysis_jobs_reserve_their_combined_cpu_demand(tmp_path, monkeypatch):
    monkeypatch.setattr("app.job_store.os.cpu_count", lambda: 4)
    store = JobStore(tmp_path, max_parallel=4, analysis_parallel=3)
    first = enqueue(store, analysis_steps(6))
    second = enqueue(store, analysis_steps(6))
    small = enqueue(store, analysis_steps(1))
    assert store.claim_next("worker")["id"] == first["id"]
    assert store.claim_next("worker")["id"] == small["id"]
    assert store.claim_next("worker") is None
    assert store.get(second["id"])["status"] == "queued"
    assert store.get(first["id"])["resource_reservation"] == {"cpu_threads": 3, "analysis_parallel": 3}


def test_settings_changes_keep_running_reservations_until_next_claim(tmp_path, monkeypatch):
    monkeypatch.setattr("app.job_store.os.cpu_count", lambda: 4)
    store = JobStore(tmp_path, max_parallel=4, analysis_parallel=3)
    first = enqueue(store, analysis_steps(6))
    store.claim_next("worker")
    store.configure(analysis_parallel=1)
    second = enqueue(store, analysis_steps(6))
    third = enqueue(store, analysis_steps(6))
    assert store.claim_next("worker")["id"] == second["id"]
    assert store.claim_next("worker") is None
    assert store.get(first["id"])["resource_reservation"]["cpu_threads"] == 3
    assert store.get(second["id"])["resource_reservation"]["cpu_threads"] == 1
    assert store.get(third["id"])["status"] == "queued"


def test_mdrun_reserves_peak_of_simulation_and_analysis_cpu_demand(tmp_path, monkeypatch):
    monkeypatch.setattr("app.job_store.os.cpu_count", lambda: 8)
    store = JobStore(tmp_path, analysis_parallel=3)
    steps = [Step("MD", ["gmx", "mdrun"]), *analysis_steps(6)]
    job = enqueue(store, steps, params={"ntmpi": 2, "ntomp": 3})
    assert store.claim_next("worker")["resource_reservation"] == {"cpu_threads": 6, "analysis_parallel": 3}
    assert store.get(job["id"])["status"] == "running"


def test_execution_keeps_frozen_limit_and_step_order_and_bounds_library_threads(tmp_path, monkeypatch):
    monkeypatch.setattr("app.job_store.os.cpu_count", lambda: 8)
    monkeypatch.setenv("OMP_NUM_THREADS", "12")
    store = JobStore(tmp_path, analysis_parallel=2)
    code = (
        "import os,pathlib,sys,time; "
        "assert all(os.environ[key]=='1' for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS')); "
        "time.sleep(0.15); pathlib.Path(sys.argv[1]).write_text('done')"
    )
    steps = [Step(f"analysis {index}", [sys.executable, "-c", code, f"{index}.done"], parallel_group="analysis") for index in range(5)]
    steps.append(Step("after", [sys.executable, "-c", "from pathlib import Path; assert len(list(Path('.').glob('*.done')))==5"]))
    job = enqueue(store, steps)
    claimed = store.claim_next("worker")
    store.configure(analysis_parallel=4)
    children, active_counts, original = [], [], subprocess.Popen

    def remember(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        active_counts.append(sum(proc.poll() is None for proc in children))
        return child

    engine = WorkerEngine(store, "worker")
    with patch("app.execution.subprocess.Popen", remember):
        engine._run_job(claimed, steps, 0)
    state = store.get(job["id"])
    assert state["status"] == "completed", state.get("error")
    assert max(active_counts) == 2
    assert [run["title"] for run in state["step_runs"]] == [step.title for step in steps]
    assert all(run["status"] == "completed" for run in state["step_runs"])
    assert os.environ["OMP_NUM_THREADS"] == "12"


def test_failed_wave_does_not_launch_later_analysis_or_followup(tmp_path):
    store = JobStore(tmp_path, analysis_parallel=2)
    steps = [Step("failed", ["/bin/false"], parallel_group="analysis"), *analysis_steps(4)]
    steps.append(Step("after", [sys.executable, "-c", "from pathlib import Path; Path('unexpected').touch()"]))
    job = enqueue(store, steps)
    assert WorkerEngine(store, "worker").run_once()
    state = store.get(job["id"])
    assert state["status"] == "failed"
    assert len(state["step_runs"]) == 2
    assert not (store.job_dir(job["id"]) / "unexpected").exists()
