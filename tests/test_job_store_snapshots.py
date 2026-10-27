"""Connection lifetimes and immutable, cross-process history snapshots."""

import json
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest

from app.job_store import MAX_PAGE_SNAPSHOTS, JobStore


def test_connections_close_after_success_and_exception_and_keep_transactions(tmp_path):
    store = JobStore(tmp_path)
    with store._connect() as connection:
        connection.execute("BEGIN")
        connection.execute("INSERT INTO store_migrations VALUES('committed', 'now')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="rollback"):
        with store._connect() as failed_connection:
            failed_connection.execute("BEGIN")
            failed_connection.execute("INSERT INTO store_migrations VALUES('rolled-back', 'now')")
            raise RuntimeError("rollback")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_connection.execute("SELECT 1")
    with store._connect() as connection:
        assert connection.execute("SELECT 1 FROM store_migrations WHERE name='committed'").fetchone()
        assert connection.execute("SELECT 1 FROM store_migrations WHERE name='rolled-back'").fetchone() is None


def test_page_projects_summary_and_uses_index_for_stable_pinned_order(tmp_path):
    store = JobStore(tmp_path)
    first = store.create("one", {"workflow": "custom", "large_parameter": "x" * 10000}, [], [])
    second = store.create("two", {"workflow": "custom"}, [], [])
    store.update(first["id"], pinned=True, is_example=False, tags=["tag"], simulation_progress={"percent": 10})
    with patch.object(store, "_decode", side_effect=AssertionError("Do not decode full metadata")):
        page = store.list_page()
    assert [job["id"] for job in page["jobs"]] == [first["id"], second["id"]]
    assert page["jobs"][0] == store.summarize(store.get(first["id"]))
    assert page["jobs"][0]["pinned"] is True
    assert page["jobs"][0]["is_example"] is False
    with store._connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM jobs "
            "ORDER BY coalesce(json_extract(meta_json,'$.pinned'),0) DESC,created_at DESC,id DESC LIMIT 50"
        ).fetchall()
    assert any("jobs_pinned_created_id_idx" in row["detail"] for row in plan)
    assert all("TEMP B-TREE" not in row["detail"] for row in plan)


def test_snapshot_reuses_payload_and_invalidates_for_worker_process(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("before", {"workflow": "custom"}, [], [])
    original = store.list_page_snapshot()
    with patch.object(store, "_list_page", side_effect=AssertionError("Cached snapshot must skip the history query")):
        assert store.list_page_snapshot() == original
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from app.job_store import JobStore; import sys; "
            "JobStore(Path(sys.argv[1])).update(sys.argv[2], name='after')",
            str(tmp_path),
            job["id"],
        ],
        check=True,
    )
    updated = store.list_page_snapshot()
    assert updated[1] != original[1]
    assert json.loads(updated[0])["jobs"][0]["name"] == "after"
    store.update(job["id"], status="failed")
    store.delete(job["id"])
    assert json.loads(store.list_page_snapshot()[0])["total"] == 0


def test_snapshot_filters_are_isolated_bounded_and_do_not_expose_mutable_cache(tmp_path):
    store = JobStore(tmp_path / "one")
    other = JobStore(tmp_path / "two")
    store.create("alpha", {"workflow": "custom", "project": "project-a"}, [], [])
    store.create("beta", {"workflow": "custom", "project": "project-b"}, [], [])
    alpha = json.loads(store.list_page_snapshot(search=" alpha ", project="project-a")[0])
    assert alpha["search"] == "alpha" and alpha["total"] == 1
    alpha["jobs"].clear()
    assert len(json.loads(store.list_page_snapshot(search="alpha", project="project-a")[0])["jobs"]) == 1
    assert json.loads(store.list_page_snapshot(search="alpha", project="project-b")[0])["total"] == 0
    assert json.loads(other.list_page_snapshot()[0])["total"] == 0
    for index in range(MAX_PAGE_SNAPSHOTS + 1):
        store.list_page_snapshot(search=f"query-{index}")
    assert len(store._page_snapshots) == MAX_PAGE_SNAPSHOTS


def test_rolled_back_changes_leave_snapshot_revision_unchanged(tmp_path):
    store = JobStore(tmp_path)
    job = store.create("unchanged", {"workflow": "custom"}, [], [])
    original = store.list_page_snapshot()
    with store._connect() as connection:
        connection.execute("BEGIN")
        connection.execute("UPDATE jobs SET updated_at='not committed' WHERE id=?", (job["id"],))
        connection.rollback()
    with patch.object(store, "_list_page", side_effect=AssertionError("Rollback must preserve the cached revision")):
        assert store.list_page_snapshot() == original


def test_recover_preparing_jobs_only_runs_explicitly_and_keeps_queued_and_running(tmp_path):
    store = JobStore(tmp_path, max_parallel=2)
    preparing = store.create("preparing", {"workflow": "custom"}, [], [])
    running = store.create("running", {"workflow": "custom"}, [], [])
    store.start(running["id"], [])
    store.claim_next("worker")
    queued = store.create("queued", {"workflow": "custom"}, [], [])
    store.start(queued["id"], [])
    reopened = JobStore(tmp_path)
    assert reopened.get(preparing["id"])["status"] == "preparing"
    assert reopened.recover_preparing_jobs() == [preparing["id"]]
    recovered = reopened.get(preparing["id"])
    assert recovered["status"] == "failed" and "preparation was interrupted" in recovered["error"]
    assert recovered["preparation_interrupted_at"]
    assert reopened.get(running["id"])["status"] == "running"
    assert reopened.get(queued["id"])["status"] == "queued"
    assert reopened.recover_preparing_jobs() == []
