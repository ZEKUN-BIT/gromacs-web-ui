import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
