"""History size must not become repeated scheduler/worker recovery work."""

import json
from contextlib import contextmanager
from unittest.mock import patch

from app.execution import WorkerEngine
from app.gromacs import Step
from app.job_store import JobStore


def add_history(store, count):
    with store._connect() as connection:
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO jobs(id,status,created_at,updated_at,meta_json) VALUES(?,?,?,?,?)",
            [
                (
                    f"done-{index}",
                    "completed",
                    "2026-01-01",
                    "2026-01-01",
                    json.dumps({"id": f"done-{index}", "status": "completed", "process_pid": 999999999}),
                )
                for index in range(count)
            ],
        )


def test_verified_dead_history_is_checked_once_and_audit_identity_is_preserved(tmp_path):
    store = JobStore(tmp_path)
    add_history(store, 1000)
    with patch("app.execution.live_process_identities", return_value=[]) as identities:
        assert store.claim_next("worker") is None
        assert identities.call_count == 1000
    with patch("app.execution.live_process_identities", side_effect=AssertionError("History must not be checked again")):
        for _ in range(3):
            assert store.claim_next("worker") is None
        store.update("done-0", notes="audit note")
        assert store.claim_next("worker") is None
    assert store.get("done-0")["process_pid"] == 999999999
    statements = []
    connect = store._connect

    @contextmanager
    def traced_connection():
        with connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    with patch.object(store, "_connect", traced_connection):
        assert store.claim_next("worker") is None
    with connect() as connection:
        plans = [
            row["detail"]
            for statement in statements
            if statement.startswith("SELECT")
            for row in connection.execute("EXPLAIN QUERY PLAN " + statement)
        ]
    assert "SCAN jobs" not in plans and "SCAN j" not in plans, plans
    assert any("jobs_pending_action_idx" in plan for plan in plans)
    # A new process registration, including a direct update by an older worker,
    # must re-enter verification even when this job was previously pruned.
    store.update("done-0", process_pid=999999998)
    with patch("app.execution.live_process_identities", return_value=[]) as identities:
        assert store.claim_next("worker") is None
        assert identities.call_count == 1


def test_legacy_jobs_are_migrated_once_and_live_terminal_processes_keep_reservations(tmp_path):
    store = JobStore(tmp_path)
    add_history(store, 2)
    with store._connect() as connection:
        connection.execute("DROP TRIGGER job_process_insert")
        connection.execute("DROP TRIGGER job_process_update")
        connection.execute("DROP TABLE active_job_processes")
        connection.execute("DELETE FROM store_migrations WHERE name='process_registry_v1'")
    reopened = JobStore(tmp_path)
    step = Step("new work", ["true"])
    queued = reopened.create("queued", {"workflow": "custom"}, [], [step])
    reopened.start(queued["id"], [step])
    with patch("app.execution.live_process_identities", side_effect=lambda meta, _: [meta] if meta["id"] == "done-0" else []):
        assert reopened.claim_next("worker") is None
    with reopened._connect() as connection:
        assert [row[0] for row in connection.execute("SELECT job_id FROM active_job_processes")] == ["done-0"]
    reopened = JobStore(tmp_path)
    with reopened._connect() as connection:
        assert [row[0] for row in connection.execute("SELECT job_id FROM active_job_processes")] == ["done-0"]


def test_recovery_reads_only_running_jobs(tmp_path):
    store = JobStore(tmp_path)
    add_history(store, 1000)
    step = Step("mdrun", ["gmx", "mdrun", "-deffnm", "md"])
    job = store.create("running", {}, [], [step])
    store.start(job["id"], [step])
    store.update(job["id"], status="running", step_index=1)
    with patch("app.reproducibility.execution_software", return_value={}):
        engine = WorkerEngine(store, "replacement")
    with patch.object(store, "list", wraps=store.list) as listing, patch.object(engine, "_finalize_job") as finalize:
        engine.recover_orphaned_jobs()
    listing.assert_called_once_with(status="running")
    assert finalize.call_args.args == (job["id"],)
