import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.main as main
from app.gromacs import Step
from app.job_store import JobStore
from app.reproducibility import write_experiment_manifest


class ReproducibilityTests(unittest.TestCase):
    def test_manifest_hashes_inputs_and_records_environment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = JobStore(root)
            meta = store.create("manifest", {"workflow": "custom", "gmx_bin": "/bin/true"}, ["input.dat"], [])
            (Path(meta["workdir"]) / "input.dat").write_bytes(b"input")
            with patch("app.reproducibility.gpu_information", return_value={"available": False, "devices": []}):
                manifest = write_experiment_manifest(store.get(meta["id"]))
            self.assertEqual(manifest["input_files"][0]["path"], "input.dat")
            self.assertEqual(len(manifest["input_files"][0]["sha256"]), 64)
            self.assertIn("platform", manifest["host"])

    def test_clone_copies_only_reusable_inputs_and_requeues(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            step = Step("run", ["/bin/true"])
            source = store.create("source", {"workflow": "custom", "alpha": 1}, ["input.dat"], [step])
            source_dir = Path(source["workdir"])
            (source_dir / "input.dat").write_text("input", encoding="utf-8")
            (source_dir / "run.xtc").write_text("large output", encoding="utf-8")
            (source_dir / "custom.mdp").write_text("nsteps = 10\n", encoding="utf-8")
            store.start(source["id"], [step])
            cloned = store.clone(source["id"])
            cloned_dir = Path(cloned["workdir"])
            self.assertEqual(cloned["status"], "queued")
            self.assertEqual(cloned["cloned_from"], source["id"])
            self.assertTrue((cloned_dir / "input.dat").is_file())
            self.assertTrue((cloned_dir / "custom.mdp").is_file())
            self.assertFalse((cloned_dir / "run.xtc").exists())

    def test_parameter_diff_reports_changed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            left = store.create("left", {"workflow": "custom", "nested": {"temperature": 300}}, [], [])
            right = store.create("right", {"workflow": "custom", "nested": {"temperature": 310}}, [], [])
            with patch.object(main, "STORE", store):
                result = main.compare_jobs(left["id"], right["id"])
            self.assertEqual(result["differences"], [{"path": "nested.temperature", "left": 300, "right": 310}])
