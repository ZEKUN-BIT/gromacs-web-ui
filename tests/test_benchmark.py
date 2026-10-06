import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.main as main
from app.execution import WorkerEngine
from app.gromacs import Step
from app.job_store import JobStore
from app.models import BenchmarkRequest


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_job_ranks_thread_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_gmx = root / "gmx"
            fake_gmx.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "stem = sys.argv[sys.argv.index('-deffnm') + 1]\n"
                "threads = int(sys.argv[sys.argv.index('-ntomp') + 1])\n"
                "value = threads * 10.0\n"
                "pathlib.Path(stem + '.log').write_text(f'Performance: {value}  0.1\\n', encoding='utf-8')\n"
                "print(f'Performance: {value}  0.1', flush=True)\n",
                encoding="utf-8",
            )
            fake_gmx.chmod(0o755)
            store = JobStore(root, max_parallel=1)
            source_step = Step("Production", [str(fake_gmx), "mdrun", "-deffnm", "production"])
            source = store.create("source", {"workflow": "run_tpr"}, [], [source_step])
            (store.job_dir(source["id"]) / "production.tpr").write_bytes(b"tpr")

            with (
                patch.object(main, "STORE", store),
                patch.object(main, "current_settings", return_value={"gmx_bin": str(fake_gmx)}),
                patch.object(main, "host_information", return_value={"physical_core_count": 4, "cpu_count": 8}),
                patch.object(main, "write_experiment_manifest", return_value={"input_files": {}}),
            ):
                created = main.benchmark_job(
                    source["id"],
                    BenchmarkRequest(threads=[2, 4], nsteps=1000, gpu=False),
                )

            self.assertEqual(created["status"], "queued")
            self.assertTrue((store.job_dir(created["id"]) / "benchmark.tpr").is_file())
            self.assertTrue(WorkerEngine(store, "worker:benchmark").run_once())
            completed = store.get(created["id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["benchmark_results"]["best"]["ntomp"], 4)
            self.assertEqual(completed["performance_ns_per_day"], 40.0)


if __name__ == "__main__":
    unittest.main()
