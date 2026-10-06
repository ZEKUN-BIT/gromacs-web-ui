import unittest
from unittest.mock import Mock, patch

from app.execution import WorkerEngine
from app.gromacs import _mdrun_args


class ExecutionPerformanceTests(unittest.TestCase):
    def test_mdrun_cpu_pinning_is_an_argument_not_shell_text(self) -> None:
        args = _mdrun_args("md", 1, 8, True, pin="on")
        self.assertEqual(args[-2:], ["-pin", "on"])
        self.assertIn("gpu", args)
        self.assertNotIn("-pin", _mdrun_args("md", 1, 8, False, pin="auto"))

    def test_progress_is_throttled_and_calculates_throughput(self) -> None:
        store = Mock()
        engine = WorkerEngine(store, "worker:test")
        with patch("app.execution.time.monotonic", side_effect=[100.0, 100.2, 102.0]):
            engine._record_progress("job", "step 1000", 1, 1, 10000, 0.002)
            engine._record_progress("job", "step 1100", 1, 1, 10000, 0.002)
            engine._record_progress("job", "step 2000", 1, 1, 10000, 0.002)

        self.assertEqual(store.update.call_count, 2)
        update = store.update.call_args.kwargs
        self.assertAlmostEqual(update["simulation_progress"]["ns_per_day"], 86.4)
        self.assertEqual(update["simulation_progress"]["eta_seconds"], 16)

    def test_final_gromacs_performance_replaces_estimate(self) -> None:
        store = Mock()
        store.get.return_value = {"simulation_progress": {"step": 10000, "total_steps": 10000}}
        engine = WorkerEngine(store, "worker:test")
        engine._record_progress("job", "Performance: 123.456  0.194", 1, 1, 10000, 0.002)

        update = store.update.call_args.kwargs
        self.assertEqual(update["performance_ns_per_day"], 123.456)
        self.assertEqual(update["simulation_progress"]["eta_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
