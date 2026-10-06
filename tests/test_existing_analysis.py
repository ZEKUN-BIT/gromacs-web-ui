import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

import app.main as main
from app.existing_analysis import analysis_disk_budget, analysis_inputs, create_existing_analysis
from app.job_store import JobStore
from app.models import ExistingAnalysisRequest


class ExistingAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name))
        self.source = self.store.create("source", {"workflow": "protein_md"}, [], [])
        self.root = self.store.job_dir(self.source["id"])
        self.store.start(self.source["id"], [])
        self.store.claim_next("fixture")
        self.store.update(self.source["id"], status="completed")
        for name in ("md_r01.tpr", "md_r01.xtc", "md_r01.edr", "index.ndx"):
            (self.root / name).write_bytes(f"fixture: {name}".encode())
        self.settings = {"gmx_bin": "gmx", "default_ntomp": 2}

    def request(self, **kwargs):
        return ExistingAnalysisRequest(**{"tpr_file": "md_r01.tpr", "trajectory_file": "md_r01.xtc", **kwargs})

    def test_independent_inputs_provenance_and_manifest_exist_before_queue(self):
        start = self.store.start

        def checked_start(job_id, steps, dry_run=False):
            staged = self.store.get(job_id)
            self.assertEqual(staged["status"], "preparing")
            self.assertEqual(staged["analysis_source"]["job_id"], self.source["id"])
            self.assertEqual(len(staged["input_hashes"]), 4)
            manifest = json.loads((self.store.job_dir(job_id) / "experiment-manifest.json").read_text())
            self.assertEqual(manifest["analysis_source"], staged["analysis_source"])
            start(job_id, steps, dry_run)

        with patch.object(self.store, "start", side_effect=checked_start):
            created = create_existing_analysis(
                self.store,
                self.source["id"],
                self.request(
                    edr_file="md_r01.edr",
                    index_file="index.ndx",
                    do_energy=True,
                    dry_run=True,
                ),
                self.settings,
            )
        self.assertEqual(created["status"], "queued")
        self.assertTrue(created["dry_run"])
        self.assertEqual(created["commands"][0]["stdin_text"], "Protein\nProtein\nSystem\n")
        self.assertEqual(created["params"]["trajectory_file"], "analysis-whole.xtc")
        self.assertEqual(created["params"]["rmsd_trajectory_file"], "input.xtc")
        unwrap = next(cmd for cmd in created["commands"] if cmd["title"] == "Unwrap trajectory for RMSD")
        self.assertIn("input.xtc", unwrap["args"])
        self.assertNotIn("analysis-whole.xtc", unwrap["args"])
        energy = next(cmd for cmd in created["commands"] if "energy" in cmd["args"])
        self.assertIn("input.edr", energy["args"])
        rg = next(cmd for cmd in created["commands"] if "gyrate" in cmd["args"])
        self.assertIn("analysis-whole.xtc", rg["args"])
        self.assertIn("input.ndx", rg["args"])
        cleanup = created["commands"][-1]
        self.assertEqual(cleanup["operation"], "remove_files")
        self.assertFalse(set(cleanup["data"]["paths"]) & set(created["uploaded_files"]))
        target = self.store.job_dir(created["id"])
        self.assertNotEqual((target / "input.xtc").stat().st_ino, (self.root / "md_r01.xtc").stat().st_ino)
        self.store.delete(self.source["id"])
        self.assertEqual((target / "input.xtc").read_bytes(), b"fixture: md_r01.xtc")

    def test_rejects_unsafe_missing_empty_or_wrong_type_inputs_without_creating_job(self):
        (self.root / "empty.xtc").touch()
        (self.root / "linked.xtc").symlink_to(self.root / "md_r01.xtc")
        (self.root / "linked-dir").symlink_to(self.root, target_is_directory=True)
        for filename in (
            "../md_r01.xtc",
            str(self.root / "md_r01.xtc"),
            "md_r01.edr",
            "missing.xtc",
            "empty.xtc",
            "linked.xtc",
            "linked-dir/md_r01.xtc",
            "..\\md_r01.xtc",
        ):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                create_existing_analysis(self.store, self.source["id"], self.request(trajectory_file=filename), self.settings)
        self.assertEqual(len(self.store.list()), 1)

    def test_source_must_be_completed_and_not_dry_run(self):
        for status in ("preparing", "queued", "running", "interrupted", "failed", "cancelled"):
            with patch.object(self.store, "get", return_value={**self.source, "status": status}):
                with self.subTest(status=status), self.assertRaises(ValueError):
                    analysis_inputs(self.store, self.source["id"])
                with self.assertRaises(ValueError):
                    create_existing_analysis(self.store, self.source["id"], self.request(), self.settings)
        self.store.update(self.source["id"], status="completed", dry_run=True)
        with self.assertRaises(ValueError):
            analysis_inputs(self.store, self.source["id"])

    def test_metrics_and_energy_dependency(self):
        for request in (self.request(do_rmsd=False, do_rg=False), self.request(do_energy=True)):
            with self.assertRaises(ValueError):
                create_existing_analysis(self.store, self.source["id"], request, self.settings)
        self.assertEqual(len(self.store.list()), 1)

    def test_rmsd_output_window_uses_ns_and_preserves_nojump_history_and_tpr_reference(self):
        created = create_existing_analysis(self.store, self.source["id"], self.request(begin_ns=20, end_ns=80), self.settings)
        commands = created["commands"]
        whole = commands[0]
        unwrap = next(command for command in commands if command["title"] == "Unwrap trajectory for RMSD")
        rmsd = next(command for command in commands if command["title"] == "Compute RMSD")
        self.assertEqual(whole["args"][whole["args"].index("-b") + 1], "20000")
        self.assertEqual(whole["args"][whole["args"].index("-e") + 1], "80000")
        self.assertNotIn("-b", unwrap["args"])
        self.assertEqual(unwrap["args"][unwrap["args"].index("-e") + 1], "80000")
        self.assertEqual(unwrap["args"][unwrap["args"].index("-f") + 1], "input.xtc")
        self.assertEqual(rmsd["args"][rmsd["args"].index("-tu") + 1], "ns")
        self.assertEqual(rmsd["args"][rmsd["args"].index("-b") + 1], "20")
        self.assertEqual(rmsd["args"][rmsd["args"].index("-e") + 1], "80")
        self.assertEqual(rmsd["args"][rmsd["args"].index("-s") + 1], "input.tpr")
        immediate_cleanup = commands[commands.index(rmsd) + 1]
        self.assertEqual(immediate_cleanup["operation"], "remove_files")
        self.assertEqual(immediate_cleanup["data"]["paths"], ["rmsd-whole.xtc"])
        cluster = next(command for command in commands if command["title"] == "Cluster and center trajectory for RMSD")
        self.assertEqual(cluster["stdin_text"], "Protein\nProtein\nSystem\n")
        self.assertEqual(cluster["args"][cluster["args"].index("-f") + 1], "rmsd-nojump.xtc")
        self.assertEqual(cluster["args"][cluster["args"].index("-b") + 1], "20000")
        self.assertEqual(cluster["args"][cluster["args"].index("-e") + 1], "80000")
        self.assertEqual(rmsd["args"][rmsd["args"].index("-f") + 1], "rmsd-whole.xtc")
        self.assertEqual(rmsd["args"][rmsd["args"].index("-fit") + 1], "rot+trans")
        before_rmsd_cleanup = commands[commands.index(rmsd) - 1]
        self.assertEqual(before_rmsd_cleanup["data"]["paths"], ["rmsd-nojump.xtc"])
        self.assertEqual(sum(command["title"] == "Prepare whole-system trajectory for analysis" for command in commands), 1)
        self.assertEqual(created["analysis_source"]["window_ns"], [20, 80])
        self.assertEqual(created["analysis_source"]["rmsd_reference"]["source"], "selected TPR coordinates")
        self.assertEqual(created["analysis_source"]["rmsd_reference"]["fit"], "rot+trans within gmx rms")

    def test_missing_end_keeps_nojump_source_history_and_filters_rmsd_begin(self):
        created = create_existing_analysis(self.store, self.source["id"], self.request(begin_ns=20), self.settings)
        unwrap = next(command for command in created["commands"] if command["title"] == "Unwrap trajectory for RMSD")
        rmsd = next(command for command in created["commands"] if command["title"] == "Compute RMSD")
        self.assertNotIn("-b", unwrap["args"])
        self.assertNotIn("-e", unwrap["args"])
        self.assertEqual(rmsd["args"][rmsd["args"].index("-b") + 1], "20")
        self.assertNotIn("-e", rmsd["args"])

    def test_disk_budget_rejects_large_input_before_creating_or_copying_job(self):
        trajectory = self.root / "md_r01.xtc"
        with trajectory.open("r+b") as stream:
            stream.truncate(4 * 1024**3)  # Sparse fixture: no multi-GB allocation.
        with (
            patch("app.existing_analysis.shutil.disk_usage", return_value=SimpleNamespace(free=6 * 1024**3)),
            patch("app.existing_analysis.shutil.copy2") as copy,
        ):
            with self.assertRaisesRegex(ValueError, "磁盘空间不足.*GiB"):
                create_existing_analysis(self.store, self.source["id"], self.request(), self.settings)
            copy.assert_not_called()
        self.assertEqual(len(self.store.list()), 1)

    def test_space_consumed_after_preflight_cancels_staging_without_copy(self):
        free = [SimpleNamespace(free=100 * 1024**3), SimpleNamespace(free=0)]
        with patch("app.existing_analysis.shutil.disk_usage", side_effect=free), patch("app.existing_analysis.shutil.copy2") as copy:
            with self.assertRaisesRegex(ValueError, "磁盘空间不足"):
                create_existing_analysis(self.store, self.source["id"], self.request(), self.settings)
            copy.assert_not_called()
        created = next(job for job in self.store.list() if job["id"] != self.source["id"])
        self.assertEqual(created["status"], "cancelled")
        self.assertIsNone(created.get("desired_action"))
        self.assertIsNone(self.store.claim_next("test"))

    def test_disk_estimate_tracks_requested_metrics_and_dry_run(self):
        selected = {"tpr_file": self.root / "md_r01.tpr", "trajectory_file": self.root / "md_r01.xtc"}
        basic = analysis_disk_budget(selected, self.request())
        rg = analysis_disk_budget(selected, self.request(do_rmsd=False))
        pca = analysis_disk_budget(selected, self.request(do_pca=True))
        dry = analysis_disk_budget(selected, self.request(dry_run=True))
        self.assertGreater(basic["required_free_bytes"], rg["required_free_bytes"])
        self.assertEqual(basic["derived_trajectory_bytes"], 6 * selected["trajectory_file"].stat().st_size)
        self.assertGreater(pca["required_free_bytes"], basic["required_free_bytes"])
        self.assertEqual(dry["derived_trajectory_bytes"], 0)
        self.assertEqual(dry["output_reserve_bytes"], 0)
        self.assertEqual(dry["copied_input_bytes"], sum(path.stat().st_size for path in selected.values()))

    def test_copy_or_manifest_failure_cancels_and_removes_partial_inputs(self):
        for operation in ("shutil.copy2", "write_experiment_manifest"):
            with self.subTest(operation=operation):
                with patch(f"app.existing_analysis.{operation}", side_effect=OSError("disk full")):
                    with self.assertRaises(OSError):
                        create_existing_analysis(self.store, self.source["id"], self.request(), self.settings)
                created = next(job for job in self.store.list() if job["id"] != self.source["id"])
                self.assertEqual(created["status"], "cancelled")
                self.assertEqual(list(self.store.job_dir(created["id"]).glob("input.*")), [])
                self.assertIsNone(self.store.claim_next("test"))
                self.assertEqual((self.root / "md_r01.xtc").read_bytes(), b"fixture: md_r01.xtc")
                self.store.delete(created["id"])

    def test_file_choices_exclude_symlinks_empty_and_unrelated_outputs(self):
        (self.root / "empty.trr").touch()
        (self.root / "alias.tpr").symlink_to(self.root / "md_r01.tpr")
        (self.root / "plot.xvg").write_text("0 1\n")
        result = analysis_inputs(self.store, self.source["id"])
        self.assertEqual([item["path"] for item in result["files"]["tpr_file"]], ["md_r01.tpr"])
        self.assertEqual([item["path"] for item in result["files"]["trajectory_file"]], ["md_r01.xtc"])

    def test_cleanup_failure_still_leaves_task_unrunnable(self):
        with (
            patch("app.existing_analysis.shutil.copy2", side_effect=OSError("disk full")),
            patch("app.existing_analysis.Path.unlink", side_effect=PermissionError("read only")),
        ):
            with self.assertRaises(OSError):
                create_existing_analysis(self.store, self.source["id"], self.request(), self.settings)
        created = next(job for job in self.store.list() if job["id"] != self.source["id"])
        self.assertEqual(created["status"], "cancelled")
        self.assertIsNone(self.store.claim_next("test"))

    def test_api_rejects_injected_commands_and_returns_meaningful_status(self):
        for kwargs in ({"gmx_bin": "evil"}, {"custom_command": "rm"}, {"fit_group": "Backbone\nSystem"}):
            with self.assertRaises(ValidationError):
                self.request(**kwargs)
        with patch.object(main, "STORE", self.store), patch.object(main, "current_settings", return_value=self.settings):
            self.assertEqual(main.existing_analysis_inputs(self.source["id"])["source_name"], "source")
            with self.assertRaises(HTTPException) as failure:
                main.analyze_existing_job(self.source["id"], self.request(trajectory_file="../outside.xtc"))
            self.assertEqual(failure.exception.status_code, 400)
            with self.assertRaises(HTTPException) as failure:
                main.existing_analysis_inputs("missing")
            self.assertEqual(failure.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
