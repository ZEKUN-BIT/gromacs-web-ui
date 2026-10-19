import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from unittest.mock import patch

from app.analysis import xvg_stream
from app.gromacs import xvg_plots


class XvgPlotTests(unittest.TestCase):
    def test_replica_filter_is_applied_before_file_cap(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for replica in range(1, 4):
                (root / f"md_r{replica:02d}-backbone-rmsd.xvg").write_text("0 1\n1 2\n")
            result = xvg_plots(root, max_files=1, replica=3, metric="md-backbone-rmsd.xvg")
            self.assertFalse(result["truncated"])
            self.assertEqual(result["replicas"], [1, 2, 3])
            self.assertEqual(result["metrics"], ["md-backbone-rmsd.xvg"])
            self.assertEqual(result["plots"][0]["replica"], 3)
            self.assertEqual(result["plots"][0]["path"], "md_r03-backbone-rmsd.xvg")

    def test_time_window_uses_raw_points_for_statistics_and_converts_ps(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "md_r01-rmsd.xvg").write_text('@ xaxis label "Time (ps)"\n' + "\n".join(f"{i * 1000} {i * i}" for i in range(10)))
            (root / "md_r01-rmsf.xvg").write_text('@ xaxis label "Residue"\n0 1\n1 2\n')
            result = xvg_plots(root, begin_ns=2, end_ns=6, max_points=2)
            self.assertEqual(len(result["plots"]), 1)
            plot = result["plots"][0]
            self.assertEqual(plot["sample_count"], 5)
            self.assertEqual(plot["original_count"], 10)
            self.assertEqual(plot["mean"], 18)
            self.assertEqual(plot["points"][0], [2000, 4])
            self.assertEqual(plot["points"][-1], [6000, 36])
            self.assertEqual(plot["time_unit"], "ps")

    def test_multi_ligand_metrics_remain_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for ligand in ("a", "b"):
                (root / f"md_r01-protein-ligand_{ligand}-hbonds.xvg").write_text("0 1\n1 2\n")
            result = xvg_plots(root)
            self.assertEqual(len(result["metrics"]), 2)
            self.assertTrue(all(plot["category"] == "interaction" for plot in result["plots"]))

    def test_relative_workdir_is_supported(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as folder:
            root = Path(folder)
            (root / "relative.xvg").write_text("0 1\n1 2\n", encoding="utf-8")
            result = xvg_plots(root)
            self.assertEqual(len(result["plots"]), 1)
            self.assertFalse(result["truncated"])
            self.assertEqual(result["total"], 1)

    def test_parses_metadata_series_and_points(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "rmsd.xvg").write_text(
                '@ title "Backbone RMSD"\n@ xaxis label "Time (ns)"\n@ yaxis label "RMSD (nm)"\n'
                '@ s0 legend "Backbone"\n0 0.10\n1 0.14\n2 0.12\n',
                encoding="utf-8",
            )
            plots = xvg_plots(root)["plots"]
            self.assertEqual(plots[0]["title"], "Backbone RMSD")
            self.assertEqual(plots[0]["series"], ["Backbone"])
            self.assertEqual(plots[0]["points"][-1], [2.0, 0.12])
            self.assertEqual(plots[0]["category"], "structure")

    def test_ignores_symlinks_and_bounds_point_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.xvg"
            source.write_text("\n".join(f"{i} {i * 2}" for i in range(50)), encoding="utf-8")
            (root / "linked.xvg").symlink_to(source)
            result = xvg_plots(root, max_points=10)
            self.assertEqual(len(result["plots"]), 1)
            self.assertLessEqual(len(result["plots"][0]["points"]), 11)

    def test_splits_mixed_unit_energy_series_into_separate_plots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "observables.xvg").write_text(
                '@ title "GROMACS Energies"\n@ xaxis label "Time (ps)"\n'
                '@ yaxis label "(kJ/mol), (K), (bar), (kg/m^3)"\n'
                '@ s0 legend "Potential"\n@ s1 legend "Temperature"\n'
                '@ s2 legend "Pressure"\n@ s3 legend "Density"\n'
                "0 -1000 300 1 997\n10 -990 301 2 998\n",
                encoding="utf-8",
            )
            plots = xvg_plots(root)["plots"]
            self.assertEqual(len(plots), 4)
            self.assertEqual(plots[0]["series"], ["Potential"])
            self.assertEqual(plots[0]["points"][-1], [10.0, -990.0])
            self.assertEqual(plots[1]["y_label"], "Temperature (K)")
            self.assertEqual(plots[3]["y_label"], "Density (kg/m³)")
            self.assertEqual(plots[3]["last"], 998.0)
            self.assertTrue(all(plot["category"] == "quality" for plot in plots))

    def test_reports_truncation_when_file_cap_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(5):
                (root / f"plot-{index}.xvg").write_text("0 1\n1 2\n", encoding="utf-8")
            result = xvg_plots(root, max_files=2)
            self.assertEqual(len(result["plots"]), 2)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["total"], 5)

    def test_multi_ligand_rmsd_is_an_interaction_plot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "md-ligand_lig2-rmsd.xvg").write_text("0 0.1\n1 0.2\n", encoding="utf-8")
            plot = xvg_plots(root)["plots"][0]
            self.assertEqual(plot["category"], "interaction")
            self.assertEqual(plot["title"], "Ligand RMSD relative to protein")

    def test_large_xvg_is_streamed_with_full_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "long-rmsd.xvg"
            count = 200_000
            with source.open("w") as stream:
                for index in range(count):
                    stream.write(f"{index} {index % 31:.18f}\n")
            self.assertGreater(source.stat().st_size, 5_000_000)
            with patch.object(Path, "read_text", side_effect=AssertionError("XVG must be streamed")):
                result = xvg_plots(root)
            self.assertFalse(result["truncated"])
            plot = result["plots"][0]
            self.assertEqual(plot["sample_count"], count)
            self.assertEqual(plot["original_count"], count)
            self.assertLessEqual(len(plot["points"]), 1200)
            self.assertAlmostEqual(plot["mean"], sum(index % 31 for index in range(count)) / count)
            self.assertEqual(plot["points"][0], [0, 0])
            self.assertEqual(plot["points"][-1], [count - 1, (count - 1) % 31])

    def test_downsampling_preserves_each_series_extrema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "observables.xvg").write_text(
                "\n".join(
                    f"{index} {900 if index == 101 else -800 if index == 102 else 1} {700 if index == 103 else -600 if index == 104 else 2}"
                    for index in range(1000)
                )
            )
            result = xvg_plots(root, max_points=20)
            first, second = result["plots"]
            self.assertIn([101, 900], first["points"])
            self.assertIn([102, -800], first["points"])
            self.assertIn([103, 700], second["points"])
            self.assertIn([104, -600], second["points"])
            self.assertEqual(first["sampling"], "minmax")
            self.assertAlmostEqual(first["mean"], (998 + 900 - 800) / 1000)
            for plot in result["plots"]:
                self.assertLessEqual(len(plot["points"]), 20)
                self.assertEqual([point[0] for point in plot["points"]], sorted(point[0] for point in plot["points"]))

    def test_cache_is_reused_isolated_and_invalidated_when_file_grows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "cached.xvg"
            source.write_text("0 1\n1 3\n")
            with patch.object(xvg_stream, "_summary", wraps=xvg_stream._summary) as scan:
                first = xvg_plots(root)
                first["plots"][0]["points"].clear()
                second = xvg_plots(root)
                self.assertEqual(scan.call_count, 1)
                self.assertEqual(second["plots"][0]["points"], [[0, 1], [1, 3]])
                with source.open("a") as stream:
                    stream.write("2 8\n")
                third = xvg_plots(root)
                self.assertEqual(scan.call_count, 2)
                self.assertEqual(third["plots"][0]["sample_count"], 3)
                self.assertEqual(third["plots"][0]["mean"], 4)

    def test_cache_respects_entry_and_memory_budgets(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(xvg_stream, "_CACHE", OrderedDict()),
            patch.object(xvg_stream, "MAX_CACHE_ENTRIES", 2),
            patch.object(xvg_stream, "MAX_CACHE_BYTES", 18_000),
        ):
            root = Path(temp)
            for index in range(5):
                (root / f"plot-{index}.xvg").write_text("0 1\n1 2\n")
            xvg_plots(root)
            self.assertLessEqual(len(xvg_stream._CACHE), 2)
            self.assertLessEqual(sum(entry[1] for entry in xvg_stream._CACHE.values()), 18_000)

    def test_skipped_files_have_specific_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "empty.xvg").write_text("# no rows\n")
            (root / "residues.xvg").write_text('@ xaxis label "Residue"\n0 1\n1 2\n')
            result = xvg_plots(root, begin_ns=1)
            reasons = {item["path"]: item["code"] for item in result["skipped_files"]}
            self.assertEqual(reasons["residues.xvg"], "non_time_axis")
            self.assertIn("empty.xvg", reasons)
            oversized = xvg_plots(root, max_bytes=1)
            self.assertTrue(oversized["truncated"])
            self.assertTrue(all(item["code"] == "file_too_large" for item in oversized["skipped_files"]))

    def test_overlong_lines_are_rejected_without_unbounded_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "long-line.xvg").write_text("0 " + "1 " * 40_000)
            result = xvg_plots(root)
            self.assertFalse(result["plots"])
            self.assertEqual(result["skipped_files"][0]["code"], "line_too_long")

    def test_cached_file_cannot_bypass_a_stricter_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cached.xvg").write_text("0 1\n1 2\n")
            self.assertEqual(len(xvg_plots(root)["plots"]), 1)
            result = xvg_plots(root, max_bytes=1)
            self.assertFalse(result["plots"])
            self.assertEqual(result["skipped_files"][0]["code"], "file_too_large")


if __name__ == "__main__":
    unittest.main()
