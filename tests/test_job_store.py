import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.execution import WorkerEngine, command_fingerprint, identity_matches
from app.gromacs import JobStore, Step, write_json


class JobStoreTests(unittest.TestCase):
    def test_job_id_cannot_escape_jobs_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            with self.assertRaises(FileNotFoundError):
                store.get("../../metadata")

    def test_create_is_preparing_until_web_finishes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            step = Step("noop", ["/bin/true"])
            meta = store.create("staged", {"workflow": "custom"}, [], [step])
            self.assertEqual(meta["status"], "preparing")
            self.assertIsNone(store.claim_next("worker:test"))
            store.start(meta["id"], [step], dry_run=True)
            self.assertEqual(store.get(meta["id"])["status"], "queued")

    def test_paginated_list_contains_summaries_not_commands_or_params(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            for index in range(3):
                store.create(f"job-{index}", {"workflow": "custom", "secret_parameter": index}, [], [Step("noop", ["/bin/true"])])
            page = store.list_page(limit=2, offset=0)
            self.assertEqual(len(page["jobs"]), 2)
            self.assertEqual(page["total"], 3)
            self.assertTrue(page["has_more"])
            self.assertNotIn("params", page["jobs"][0])
            self.assertNotIn("commands", page["jobs"][0])

    def test_sqlite_rejects_invalid_status_transition(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            meta = store.create("state", {"workflow": "custom"}, [], [])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "invalid job status transition"):
                store.update(meta["id"], status="completed")

    def test_existing_json_metadata_is_migrated_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job_dir = root / "jobs" / "job-1"
            job_dir.mkdir(parents=True)
            write_json(job_dir / "metadata.json", {"id": "job-1", "status": "completed", "created_at": "2026-01-01T00:00:00Z"})
            store = JobStore(root)
            self.assertEqual(store.get("job-1")["status"], "completed")
            self.assertTrue((root / "jobs.sqlite3").is_file())

    def _interrupted_checkpoint_job(self, store: JobStore) -> dict:
        steps = [
            Step("Production MD", ["/opt/gromacs/bin/gmx", "mdrun", "-deffnm", "md", "-cpi", "md.cpt"]),
            Step("Postprocess", ["/bin/true"]),
        ]
        meta = store.create("recover", {"workflow": "protein_ligand_md"}, [], steps)
        (Path(meta["workdir"]) / "md.cpt").write_bytes(b"checkpoint")
        store.start(meta["id"], steps)
        return store.update(meta["id"], status="interrupted", desired_action=None, step_index=1, current_step="Production MD")

    def test_checkpoint_resume_is_transactionally_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            meta = self._interrupted_checkpoint_job(store)
            resumed = store.resume_from_checkpoint(meta["id"])
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["desired_action"], "resume")
            self.assertIn("-append", resumed["commands"][0]["args"])
            claimed = store.claim_next("worker:test")
            self.assertEqual(claimed["claimed_action"], "resume")

    def test_adopt_only_sends_control_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            meta = self._interrupted_checkpoint_job(store)
            instructed = store.adopt(meta["id"])
            self.assertEqual(instructed["status"], "interrupted")
            self.assertEqual(instructed["desired_action"], "adopt")

    def test_worker_executes_queued_job_outside_store(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            marker = root / "marker.txt"
            step = Step("write marker", ["/usr/bin/touch", str(marker)])
            meta = store.create("worker", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            engine = WorkerEngine(store, "worker:test")
            self.assertTrue(engine.run_once())
            self.assertTrue(marker.exists())
            self.assertEqual(store.get(meta["id"])["status"], "completed")

    def test_concurrent_submissions_create_distinct_transactional_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            barrier = threading.Barrier(8)
            job_ids: list[str] = []
            errors: list[Exception] = []

            def submit(index: int) -> None:
                try:
                    barrier.wait()
                    meta = store.create(f"job-{index}", {"workflow": "custom"}, [], [Step("noop", ["/bin/true"])])
                    job_ids.append(meta["id"])
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(set(job_ids)), 8)
            self.assertEqual(len(store.list()), 8)

    def test_concurrent_workers_claim_a_queued_job_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            step = Step("noop", ["/bin/true"])
            meta = store.create("claim", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            barrier = threading.Barrier(8)
            claims: list[dict | None] = []

            def claim(index: int) -> None:
                barrier.wait()
                claims.append(store.claim_next(f"worker:{index}"))

            threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(claim is not None for claim in claims), 1)
            self.assertEqual(store.get(meta["id"])["status"], "running")

    def test_claim_next_respects_max_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder), max_parallel=2)
            step = Step("noop", ["/bin/true"])
            for index in range(3):
                meta = store.create(f"job-{index}", {"workflow": "custom"}, [], [step])
                store.start(meta["id"], [step])
            self.assertIsNotNone(store.claim_next("worker:test"))
            self.assertIsNotNone(store.claim_next("worker:test"))
            self.assertIsNone(store.claim_next("worker:test"))
            running = sum(meta["status"] == "running" for meta in store.list())
            self.assertEqual(running, 2)

    def test_worker_threads_run_multiple_jobs_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root, max_parallel=2)
            markers = [root / f"marker-{index}.txt" for index in range(2)]
            for index, marker in enumerate(markers):
                step = Step(f"write {index}", ["/usr/bin/touch", str(marker)])
                meta = store.create(f"parallel-{index}", {"workflow": "custom"}, [], [step])
                store.start(meta["id"], [step])
            engine = WorkerEngine(store, "worker:parallel")
            barrier = threading.Barrier(2)

            def claim_loop() -> None:
                barrier.wait()
                while True:
                    if engine.run_once():
                        continue
                    if all(store.get(meta["id"])["status"] in {"completed", "failed"} for meta in store.list()):
                        return
                    time.sleep(0.02)

            threads = [threading.Thread(target=claim_loop) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(marker.exists() for marker in markers))
            statuses = {store.get(meta["id"])["status"] for meta in store.list()}
            self.assertEqual(statuses, {"completed"})

    def test_claim_next_keeps_gpu_jobs_exclusive_but_can_skip_to_cpu_work(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch("app.job_store.os.cpu_count", return_value=16):
            store = JobStore(Path(folder), max_parallel=2)
            gpu_step = Step("gpu", ["gmx", "mdrun", "-deffnm", "md", "-nb", "gpu"])
            first = store.create("gpu-1", {"workflow": "run_tpr", "gpu": True, "ntmpi": 1, "ntomp": 8}, [], [gpu_step])
            second = store.create("gpu-2", {"workflow": "run_tpr", "gpu": True, "ntmpi": 1, "ntomp": 8}, [], [gpu_step])
            cpu_step = Step("cpu", ["/bin/true"])
            cpu = store.create("cpu", {"workflow": "custom"}, [], [cpu_step])
            for meta, steps in ((first, [gpu_step]), (second, [gpu_step]), (cpu, [cpu_step])):
                store.start(meta["id"], steps)

            self.assertEqual(store.claim_next("worker:1")["id"], first["id"])
            self.assertEqual(store.claim_next("worker:2")["id"], cpu["id"])
            self.assertIsNone(store.claim_next("worker:3"))

    def test_cancel_and_delete_race_preserves_valid_state(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            step = Step("noop", ["/bin/true"])
            meta = store.create("race", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def cancel() -> None:
                barrier.wait()
                store.cancel(meta["id"])
                outcomes.append("cancelled")

            def delete() -> None:
                barrier.wait()
                try:
                    store.delete(meta["id"])
                    outcomes.append("deleted")
                except RuntimeError:
                    outcomes.append("delete-rejected")
                except FileNotFoundError:
                    outcomes.append("already-deleted")

            threads = [threading.Thread(target=cancel), threading.Thread(target=delete)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertIn("cancelled", outcomes)
            try:
                remaining = store.get(meta["id"])
            except FileNotFoundError:
                remaining = None
            self.assertTrue(remaining is None or remaining["status"] == "cancelled")

    def test_worker_restart_reruns_non_mdrun_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            step = Step("noop", ["/bin/true"])
            meta = store.create("restart", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            store.claim_next("worker:before-restart")
            store.update(meta["id"], step_index=1, process_pid=123_456, process_pgid=123_456)
            restarted_store = JobStore(root)
            with patch("app.execution.identity_matches", return_value=False):
                WorkerEngine(restarted_store, "worker:after-restart").recover_orphaned_jobs()
            recovered = restarted_store.get(meta["id"])
            self.assertEqual(recovered["status"], "interrupted")
            self.assertEqual(recovered["desired_action"], "run")
            self.assertEqual([c["title"] for c in recovered["commands"]], ["noop"])

    def test_worker_restart_marks_dead_mdrun_for_manual_resume(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            step = Step("Production MD", ["/opt/gromacs/bin/gmx", "mdrun", "-deffnm", "md"])
            meta = store.create("restart-mdrun", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            store.claim_next("worker:before-restart")
            store.update(meta["id"], step_index=1, process_pid=123_456, process_pgid=123_456)
            restarted_store = JobStore(Path(folder))
            with patch("app.execution.identity_matches", return_value=False):
                WorkerEngine(restarted_store, "worker:after-restart").recover_orphaned_jobs()
            recovered = restarted_store.get(meta["id"])
            self.assertEqual(recovered["status"], "interrupted")
            self.assertNotIn("desired_action", recovered)

    def test_adopt_reruns_non_mdrun_step(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            marker = root / "marker.txt"
            step = Step("write marker", ["/usr/bin/touch", str(marker)])
            meta = store.create("adopt-nonmdrun", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            store.update(meta["id"], status="running", step_index=1, process_pid=999_999, process_pgid=999_999)
            WorkerEngine(store, "worker:test")._adopt(store.get(meta["id"]))
            self.assertTrue(marker.exists())
            self.assertEqual(store.get(meta["id"])["status"], "completed")

    def test_retry_failed_step_requeues_from_failed_step(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            steps = [Step(f"step-{index}", ["/bin/true"]) for index in range(3)]
            meta = store.create("retry", {"workflow": "custom"}, [], steps)
            store.start(meta["id"], steps)
            store.claim_next("worker:test")
            store.update(meta["id"], status="failed", step_index=2, exit_code=1, error="boom")
            requeued = store.retry_failed_step(meta["id"])
            self.assertEqual(requeued["status"], "queued")
            self.assertEqual(requeued["desired_action"], "run")
            self.assertEqual(requeued["step_index"], 1)
            self.assertEqual([c["title"] for c in requeued["commands"]], ["step-1", "step-2"])

    def test_skip_failed_step_requeues_from_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            steps = [Step(f"step-{index}", ["gmx", "rms"]) for index in range(3)]
            meta = store.create("skip", {"workflow": "custom"}, [], steps)
            store.start(meta["id"], steps)
            store.claim_next("worker:test")
            store.update(meta["id"], status="failed", step_index=2, exit_code=1, error="boom")
            requeued = store.skip_failed_step(meta["id"])
            self.assertEqual(requeued["status"], "queued")
            self.assertEqual([c["title"] for c in requeued["commands"]], ["step-2"])
            self.assertEqual(requeued["step_index"], 2)

    def test_batch_steps_groups_consecutive_parallel_group(self) -> None:
        steps = [
            Step("a", ["/bin/true"]),
            Step("b1", ["/bin/true"], parallel_group="g"),
            Step("b2", ["/bin/true"], parallel_group="g"),
            Step("b3", ["/bin/true"], parallel_group="g"),
            Step("c", ["/bin/true"]),
        ]
        self.assertEqual([len(batch) for batch in WorkerEngine._batch_steps(steps)], [1, 3, 1])

    def test_parallel_batch_runs_steps_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            markers = [root / f"marker-{index}.txt" for index in range(3)]
            steps = [Step(f"m{index}", ["/usr/bin/touch", str(marker)], parallel_group="g") for index, marker in enumerate(markers)]
            meta = store.create("parallel", {"workflow": "custom"}, [], steps)
            store.start(meta["id"], steps)
            self.assertTrue(WorkerEngine(store, "worker:parallel").run_once())
            self.assertTrue(all(marker.exists() for marker in markers))
            self.assertEqual(store.get(meta["id"])["status"], "completed")

    def test_parallel_batch_failure_fails_job(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            steps = [
                Step("ok", ["/bin/true"], parallel_group="g"),
                Step("bad", ["/bin/false"], parallel_group="g"),
            ]
            meta = store.create("parallel-fail", {"workflow": "custom"}, [], steps)
            store.start(meta["id"], steps)
            WorkerEngine(store, "worker:parallel-fail").run_once()
            self.assertEqual(store.get(meta["id"])["status"], "failed")

    def test_web_cancel_instruction_terminates_worker_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            step = Step("wait", ["/bin/sh", "-c", "sleep 30 & wait"])
            meta = store.create("cancel", {"workflow": "custom"}, [], [step])
            store.start(meta["id"], [step])
            engine = WorkerEngine(store, "worker:test")
            runner = threading.Thread(target=engine.run_once)
            runner.start()
            deadline = time.monotonic() + 3
            while not store.get(meta["id"]).get("process_pid") and time.monotonic() < deadline:
                time.sleep(0.02)
            running = store.get(meta["id"])
            self.assertTrue(identity_matches(running, Path(running["workdir"])))
            store.cancel(meta["id"])
            runner.join(timeout=4)
            self.assertFalse(runner.is_alive())
            self.assertEqual(store.get(meta["id"])["status"], "cancelled")

    def test_command_fingerprint_is_stable_and_argument_sensitive(self) -> None:
        self.assertEqual(command_fingerprint(["gmx", "mdrun"]), command_fingerprint(["gmx", "mdrun"]))
        self.assertNotEqual(command_fingerprint(["gmx", "mdrun"]), command_fingerprint(["gmx", "mdrun", "-v"]))

    def test_terminal_job_can_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            meta = store.create("delete", {"workflow": "custom"}, [], [])
            store.cancel(meta["id"])
            deleted = store.delete(meta["id"])
            self.assertEqual(deleted["status"], "cancelled")
            with self.assertRaises(FileNotFoundError):
                store.get(meta["id"])


if __name__ == "__main__":
    unittest.main()
