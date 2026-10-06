"""Exercise lifecycle controls only with finite, isolated child processes."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from app import execution
from app.execution import WorkerEngine, live_process_identities, process_identity, wait_process_identity
from app.gromacs import Step
from app.job_store import JobStore

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Process identity checks use Linux /proc")


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Isolated child/worker did not reach the expected state"


@pytest.fixture
def finite_children(tmp_path):
    children = []

    def spawn(workdir, *, ignore_term=False):
        marker = tmp_path / f"ready-{len(children)}"
        code = (
            "import pathlib,signal,sys,time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "pathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(8)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(marker), "mdrun", "-deffnm", "md", "-cpi", "md.cpt"],
            cwd=workdir,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(child)
        wait_until(marker.exists)
        return child

    yield spawn
    for child in children:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait(timeout=2)


def running_job(store, name="job", *, gpu=False, ntomp=1, steps=None):
    steps = steps or [Step("Production MD", ["gmx", "mdrun", "-deffnm", "md", "-cpi", "md.cpt"])]
    job = store.create(name, {"workflow": "custom", "gpu": gpu, "ntmpi": 1, "ntomp": ntomp}, [], steps)
    store.start(job["id"], steps)
    assert store.claim_next("old-worker")["id"] == job["id"]
    return job


def register(store, job, children, *, index=1):
    identities = [wait_process_identity(child.pid) for child in children]
    assert all(identities)
    return store.update(job["id"], step_index=index, process_identities=identities, **identities[-1])


def test_interrupted_cancellation_is_claimed_and_files_wait_until_verified_process_stops(tmp_path, finite_children):
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    child = finite_children(store.job_dir(job["id"]))
    unrelated = finite_children(tmp_path)
    register(store, job, [child])
    engine = WorkerEngine(store, "new-worker")
    engine.recover_orphaned_jobs()
    assert store.get(job["id"])["desired_action"] == "adopt"
    requested = store.cancel(job["id"])
    assert requested["status"] == "interrupted"
    assert requested["desired_action"] == "cancel"
    with pytest.raises(RuntimeError, match="still active"):
        store.delete(job["id"])
    assert engine.run_once()
    assert store.get(job["id"])["status"] == "cancelled"
    child.wait(timeout=2)
    assert unrelated.poll() is None
    store.delete(job["id"])
    assert not store.job_dir(job["id"]).exists()


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_legacy_terminal_metadata_cannot_delete_live_registered_process(tmp_path, finite_children, status):
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    child = finite_children(store.job_dir(job["id"]))
    register(store, job, [child])
    store.update(job["id"], status=status, desired_action=None, worker_id=None)
    with pytest.raises(RuntimeError, match="still active"):
        store.delete(job["id"])
    store.cancel(job["id"])
    assert WorkerEngine(store, "cleanup-worker").run_once()
    child.wait(timeout=2)
    store.delete(job["id"])


@pytest.mark.parametrize("field", ["process_started_ticks", "process_command_fingerprint", "process_cwd", "process_pgid"])
def test_termination_refuses_mismatched_process_identity(tmp_path, finite_children, field):
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    child = finite_children(store.job_dir(job["id"]))
    identity = wait_process_identity(child.pid)
    wrong = dict(identity)
    wrong[field] = {
        "process_started_ticks": identity["process_started_ticks"] + 1,
        "process_command_fingerprint": "wrong command",
        "process_cwd": str(tmp_path),
        "process_pgid": identity["process_pgid"] + 100000,
    }[field]
    assert not execution.identity_matches(wrong, store.job_dir(job["id"]))
    state = store.update(job["id"], process_identities=[wrong], **wrong)
    with patch("app.execution.os.killpg") as signals:
        WorkerEngine(store, "worker")._terminate_leftover(state)
    signals.assert_not_called()
    assert child.poll() is None


def test_cancellation_escalates_only_verified_process_group_after_deadline(tmp_path, finite_children, monkeypatch):
    monkeypatch.setattr(execution, "TERMINATION_GRACE_SECONDS", 0.05)
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    child = finite_children(store.job_dir(job["id"]), ignore_term=True)
    unrelated = finite_children(tmp_path, ignore_term=True)
    register(store, job, [child])
    store.update(job["id"], status="interrupted", desired_action=None, worker_id=None)
    store.cancel(job["id"])
    original, signalled = os.killpg, []

    def remember(group, signum):
        signalled.append((group, signum))
        original(group, signum)

    with patch("app.execution.os.killpg", remember):
        assert WorkerEngine(store, "worker").run_once()
    assert child.wait(timeout=2) == -signal.SIGKILL
    assert signalled == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]
    assert unrelated.poll() is None
    assert store.get(job["id"])["status"] == "cancelled"


def test_partial_parallel_launch_failure_reaps_earlier_child_and_retains_registration(tmp_path):
    store = JobStore(tmp_path / "runtime")
    steps = [
        Step("finite analysis", [sys.executable, "-c", "import time; time.sleep(8)"], parallel_group="analysis"),
        Step("missing analyzer", [str(tmp_path / "missing-executable")], parallel_group="analysis"),
    ]
    job = running_job(store, steps=steps)
    # Requeue this already claimed job to exercise the complete public worker path.
    store.update(job["id"], status="interrupted", desired_action="run", worker_id=None)
    children, original = [], subprocess.Popen

    def remember(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    try:
        with patch("app.execution.subprocess.Popen", remember):
            assert WorkerEngine(store, "worker").run_once()
        state = store.get(job["id"])
        assert state["status"] == "failed"
        assert "missing-executable" in state["error"]
        assert len(children) == 1 and children[0].poll() is not None
        assert state["process_identities"][0]["process_pid"] == children[0].pid
        assert not live_process_identities(state, store.job_dir(job["id"]))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


def test_parallel_cancellation_persists_and_stops_every_member(tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "TERMINATION_GRACE_SECONDS", 0.05)
    store = JobStore(tmp_path / "runtime")
    steps = [
        Step(f"analysis {index}", [sys.executable, "-c", "import time; time.sleep(8)"], parallel_group="analysis") for index in range(2)
    ]
    job = store.create("parallel", {"workflow": "custom"}, [], steps)
    store.start(job["id"], steps)
    engine, children, original = WorkerEngine(store, "worker"), [], subprocess.Popen

    def remember(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    runner = threading.Thread(target=engine.run_once)
    try:
        with patch("app.execution.subprocess.Popen", remember):
            runner.start()
            wait_until(lambda: len(store.get(job["id"]).get("process_identities") or []) == 2)
            assert len(live_process_identities(store.get(job["id"]), store.job_dir(job["id"]))) == 2
            store.cancel(job["id"])
            runner.join(timeout=4)
        assert not runner.is_alive()
        assert store.get(job["id"])["status"] == "cancelled"
        assert len(children) == 2 and all(child.poll() is not None for child in children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        runner.join(timeout=4)


def test_restart_stops_whole_parallel_group_before_requeuing_it(tmp_path, finite_children):
    store = JobStore(tmp_path / "runtime")
    steps = [Step(f"analysis {index}", ["/bin/true"], parallel_group="analysis") for index in range(2)]
    steps.append(Step("after analysis", ["/bin/true"]))
    job = running_job(store, steps=steps)
    children = [finite_children(store.job_dir(job["id"])) for _ in range(2)]
    unrelated = finite_children(tmp_path)
    register(store, job, children, index=2)
    engine = WorkerEngine(store, "restarted-worker")
    engine.recover_orphaned_jobs()
    for child in children:
        child.wait(timeout=2)
    assert unrelated.poll() is None
    state = store.get(job["id"])
    assert state["status"] == "interrupted" and state["desired_action"] == "run"
    assert state["step_index"] == 0
    assert len(state["commands"]) == 3
    assert engine.run_once()
    assert store.get(job["id"])["status"] == "completed"


@pytest.mark.parametrize("action", ["adopt", "resume", "run"])
def test_live_orphan_is_claimed_before_older_gpu_retry(tmp_path, finite_children, action):
    store = JobStore(tmp_path / "runtime")
    older = running_job(store, "older retry", gpu=True)
    store.update(older["id"], status="failed", step_index=1, desired_action=None, worker_id=None)
    newer = running_job(store, "newer live GPU", gpu=True)
    child = finite_children(store.job_dir(newer["id"]))
    register(store, newer, [child])
    store.retry_failed_step(older["id"])
    store.update(newer["id"], status="interrupted", desired_action=action, worker_id=None)
    claimed = store.claim_next("new-worker")
    assert claimed["id"] == newer["id"]
    assert claimed["claimed_action"] == "adopt", "A live saved process must be controlled, never launched twice"
    assert store.claim_next("second-worker") is None
    assert child.poll() is None


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("status", ["interrupted", "failed", "cancelled", "completed"])
def test_unclaimed_live_registry_keeps_cpu_and_gpu_reservation_including_terminal_metadata(
    tmp_path, finite_children, monkeypatch, gpu, status
):
    monkeypatch.setattr("app.job_store.os.cpu_count", lambda: 4)
    store = JobStore(tmp_path / "runtime", max_parallel=2)
    live = running_job(store, "orphan", gpu=gpu, ntomp=4)
    child = finite_children(store.job_dir(live["id"]))
    register(store, live, [child])
    store.update(live["id"], status=status, desired_action=None, worker_id=None)
    steps = [Step("Production MD", ["gmx", "mdrun", "-nb", "gpu" if gpu else "cpu"])]
    queued = store.create("new task", {"workflow": "custom", "gpu": gpu, "ntomp": 1}, [], steps)
    store.start(queued["id"], steps)
    assert store.claim_next("worker") is None
    assert child.poll() is None


@pytest.mark.parametrize("fault", ["read", "write"])
def test_parallel_cleanup_uses_local_owned_registry_when_database_fails_after_spawn(tmp_path, finite_children, monkeypatch, fault):
    store = JobStore(tmp_path / "runtime")
    steps = [Step("finite analysis", [sys.executable, "-c", "import time; time.sleep(8)"], parallel_group="analysis")]
    job = running_job(store, steps=steps)
    unrelated = finite_children(tmp_path)
    children, original_popen = [], subprocess.Popen
    original_get, original_update = store.get, store.update

    def fail_after_spawn(*args, **kwargs):
        if children:
            raise sqlite3.OperationalError("isolated audit database unavailable")
        return (original_get if fault == "read" else original_update)(*args, **kwargs)

    def remember(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(store, "get" if fault == "read" else "update", fail_after_spawn)
    try:
        with patch("app.execution.subprocess.Popen", remember):
            with pytest.raises((sqlite3.OperationalError, RuntimeError), match="database unavailable|could not be saved"):
                WorkerEngine(store, "worker")._run_parallel_batch(job["id"], store.job_dir(job["id"]), steps, 1, 1, False)
        assert len(children) == 1 and children[0].poll() is not None
        assert children[0].stdout.closed
        assert unrelated.poll() is None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


def test_checkpoint_resume_checks_every_registered_identity(tmp_path, finite_children):
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    child = finite_children(store.job_dir(job["id"]))
    record = wait_process_identity(child.pid)
    store.update(
        job["id"], status="failed", step_index=1, desired_action=None, worker_id=None, process_pid=99999999, process_identities=[record]
    )
    (store.job_dir(job["id"]) / "md.cpt").write_bytes(b"checkpoint fixture")
    with pytest.raises(RuntimeError, match="still active"):
        store.resume_from_checkpoint(job["id"])
    with pytest.raises(RuntimeError, match="still active"):
        store.retry_failed_step(job["id"])
    assert child.poll() is None


def test_identity_without_start_time_cannot_authorize_signal(tmp_path, finite_children):
    child = finite_children(tmp_path)
    identity = process_identity(child.pid)
    identity["process_started_ticks"] = None
    assert not execution.identity_matches(identity, tmp_path)


def test_restart_preserves_pending_cancellation_instead_of_replacing_it_with_adoption(tmp_path, finite_children):
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    child = finite_children(store.job_dir(job["id"]))
    register(store, job, [child])
    store.cancel(job["id"])
    engine = WorkerEngine(store, "new-worker")
    engine.recover_orphaned_jobs()
    recovered = store.get(job["id"])
    assert recovered["status"] == "interrupted" and recovered["desired_action"] == "cancel"
    assert engine.run_once()
    child.wait(timeout=2)
    assert store.get(job["id"])["status"] == "cancelled"


def test_control_claim_can_cleanup_orphan_when_new_parallel_limit_is_already_full(tmp_path, finite_children):
    store = JobStore(tmp_path / "runtime", max_parallel=2)
    orphan = running_job(store, "orphan")
    child = finite_children(store.job_dir(orphan["id"]))
    register(store, orphan, [child])
    other = running_job(store, "other running")
    store.update(orphan["id"], status="interrupted", desired_action=None, worker_id=None)
    store.configure(max_parallel=1)
    store.cancel(orphan["id"])
    assert WorkerEngine(store, "cleanup-worker").run_once()
    child.wait(timeout=2)
    assert store.get(orphan["id"])["status"] == "cancelled"
    assert store.get(other["id"])["status"] == "running"


def test_escalation_keeps_verified_descendant_after_its_parent_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "TERMINATION_GRACE_SECONDS", 0.05)
    store = JobStore(tmp_path / "runtime")
    job = running_job(store)
    marker = tmp_path / "descendant-ready"
    descendant = (
        "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(8)"
    )
    parent_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(8)"
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_code, descendant, str(marker)],
        cwd=store.job_dir(job["id"]),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_identity = None
    try:
        wait_until(marker.exists)
        child_identity = process_identity(int(marker.read_text()))
        assert child_identity
        register(store, job, [parent])
        store.update(job["id"], status="interrupted", desired_action=None, worker_id=None)
        store.cancel(job["id"])
        assert WorkerEngine(store, "worker").run_once()
        parent.wait(timeout=2)
        state = store.get(job["id"])
        assert state["status"] == "cancelled"
        assert child_identity["process_pid"] in {item["process_pid"] for item in state["process_identities"]}
        assert not live_process_identities(state, store.job_dir(job["id"]))
        assert process_identity(child_identity["process_pid"]) is None
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=2)
        if child_identity and execution.identity_matches(child_identity, store.job_dir(job["id"])):
            os.killpg(child_identity["process_pgid"], signal.SIGKILL)


def test_cancellation_tracks_an_exec_of_our_owned_child_without_relaxing_identity(tmp_path):
    store = JobStore(tmp_path / "runtime")
    replacement = "import time; time.sleep(8)"
    launcher = "import os,sys,time; time.sleep(0.05); os.execv(sys.executable,[sys.executable,'-c',sys.argv[1]])"
    step = Step("tool launcher", [sys.executable, "-c", launcher, replacement])
    job = store.create("exec launcher", {"workflow": "custom"}, [], [step])
    store.start(job["id"], [step])
    engine, children, original = WorkerEngine(store, "worker"), [], subprocess.Popen

    def remember(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    runner = threading.Thread(target=engine.run_once)
    try:
        with patch("app.execution.subprocess.Popen", remember):
            runner.start()
            wait_until(lambda: bool(store.get(job["id"]).get("process_pid")))
            wait_until(lambda: children and process_identity(children[0].pid)["process_args"] == [sys.executable, "-c", replacement])
            store.cancel(job["id"])
            runner.join(timeout=4)
        assert not runner.is_alive()
        assert store.get(job["id"])["status"] == "cancelled"
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        runner.join(timeout=4)


def test_parallel_reader_failure_promptly_stops_whole_owned_group(tmp_path, finite_children, monkeypatch):
    store = JobStore(tmp_path / "runtime")
    gate = tmp_path / "emit-failure"
    emitter = (
        "import pathlib,sys,time\n"
        "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
        "print('trigger-reader-error', flush=True)\ntime.sleep(8)\n"
    )
    steps = [
        Step("failing reader", [sys.executable, "-c", emitter, str(gate)], parallel_group="analysis"),
        Step("long analysis", [sys.executable, "-c", "import time; time.sleep(8)"], parallel_group="analysis"),
    ]
    job = store.create("reader failure", {"workflow": "custom"}, [], steps)
    store.start(job["id"], steps)
    unrelated = finite_children(tmp_path)
    children, original_popen, original_log = [], subprocess.Popen, store.append_log

    def remember(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def failed_log(job_id, text):
        if text == "trigger-reader-error\n":
            raise OSError("isolated output log failure")
        original_log(job_id, text)

    monkeypatch.setattr(store, "append_log", failed_log)
    runner = threading.Thread(target=WorkerEngine(store, "worker").run_once)
    try:
        with patch("app.execution.subprocess.Popen", remember):
            runner.start()
            wait_until(lambda: len(store.get(job["id"]).get("process_identities") or []) == 2)
            started = time.monotonic()
            gate.touch()
            runner.join(timeout=3)
        assert not runner.is_alive(), "The first reader error must stop peers without waiting for their eight-second work"
        assert time.monotonic() - started < 3
        assert len(children) == 2 and all(child.poll() is not None for child in children)
        assert unrelated.poll() is None
        state = store.get(job["id"])
        assert state["status"] == "failed"
        assert "Parallel output reader failed: isolated output log failure" in state["error"]
        assert not live_process_identities(state, store.job_dir(job["id"]))
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        runner.join(timeout=4)
