import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import MDAnalysis as mda
import numpy as np
from fastapi import HTTPException
from pydantic import ValidationError

import app.main as main
from app.analysis.comparison import align_residues, compare_reports, load_research_report
from app.analysis.research import analyze_universe, read_index_group, series_summary
from app.existing_analysis import create_existing_analysis
from app.job_store import JobStore
from app.models import ExistingAnalysisRequest, ResearchComparisonRequest


def molecular_fixture(frames=10, leave=False, rotate=False):
    universe = mda.Universe.empty(9, n_residues=7, atom_resindex=[0, 1, 2, 3, 4, 5, 6, 6, 6])
    universe.add_TopologyAttr("names", ["CA"] * 6 + ["C1", "C2", "C3"])
    universe.add_TopologyAttr("resnames", ["ALA", "GLY", "SER", "VAL", "LEU", "THR", "LIG"])
    universe.add_TopologyAttr("resids", [1, 2, 3, 4, 5, 6, 10])
    universe.add_TopologyAttr("chainIDs", ["A"] * 6 + ["B"] * 3)
    universe.add_TopologyAttr("icodes", [""] * 7)
    base = np.asarray(
        [[0, 0, 0], [7, 0, 0], [14, 0, 1], [0, 7, 0], [7, 7, 1], [14, 7, 0], [1, 1, 1], [1.5, 1, 1], [1, 1.5, 1]], dtype=np.float32
    )
    trajectory = []
    for frame in range(frames):
        coords = base.copy()
        if leave and frame >= frames // 2:
            coords[6:, 2] += 15
        if rotate:
            angle = frame * 0.1
            rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
            coords = coords @ rotation.T + np.array([frame * 0.2, frame * 0.3, frame * 0.1])
        trajectory.append(coords)
    universe.load_new(
        np.asarray(trajectory, dtype=np.float32), order="fac", dt=10, dimensions=np.tile([100, 100, 100, 90, 90, 90], (frames, 1))
    )
    return universe


def comparison_fixture(name, delta=0):
    residues = [
        {
            "chain": "A",
            "resid": i + 1,
            "icode": "",
            "resname": resname,
            "rmsf_nm": 0.1 + delta,
            "displacement_nm": 0.2,
            "contact_occupancy": 0.8 - delta,
        }
        for i, resname in enumerate(["ALA", "GLY", "SER", "VAL", "LEU", "THR"])
    ]
    return {
        "schema_version": 1,
        "job": {"id": name, "name": name},
        "residues": residues,
        "summary": {"ca_rg_nm": {"mean": 1 + delta}, "ligand_rmsd_nm": {"mean": 0.2 + delta}},
        "input_hashes": [{"path": "input.xtc", "sha256": name}],
        "window_ns": [10, 100],
        "stride": 10,
        "fit_group": "Backbone",
        "source": {"protocol": {"ref_t": "300"}},
        "binding": {"resname": "LIG", "heavy_atoms": 3, "atom_names": ["C1", "C2", "C3"], "cutoff_nm": 0.45},
    }


class ResearchAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = self.root / "index.ndx"
        self.index.write_text("[ Ligand ]\n7 8 9\n")
        self.request = ExistingAnalysisRequest(
            tpr_file="input.tpr",
            trajectory_file="input.xtc",
            index_file="index.ndx",
            do_research=True,
            research_stride=1,
            fit_group="C-alpha",
            research_ligand_group="Ligand",
            contact_cutoff_nm=0.2,
        )

    def test_rigid_body_motion_is_removed_from_rmsf_and_ligand_rmsd(self):
        report = analyze_universe(molecular_fixture(rotate=True), self.request, self.index)
        self.assertLess(max(row["rmsf_nm"] for row in report["residues"]), 1e-5)
        self.assertLess(report["summary"]["ca_rmsd_nm"]["mean"], 1e-5)
        self.assertLess(report["summary"]["ligand_rmsd_nm"]["mean"], 1e-5)
        self.assertLess(report["summary"]["ligand_self_rmsd_nm"]["mean"], 1e-5)
        self.assertEqual(report["pca"]["explained_variance"], [0, 0])
        json.dumps(report, allow_nan=False)

    def test_departure_changes_pose_but_not_internal_conformation_and_occupancy(self):
        report = analyze_universe(molecular_fixture(leave=True), self.request, self.index)
        self.assertAlmostEqual(report["residues"][0]["contact_occupancy"], 0.5)
        self.assertAlmostEqual(report["binding"]["contact_frame_fraction"], 0.5)
        self.assertAlmostEqual(report["summary"]["pocket_retention"]["mean"], 0.5)
        self.assertAlmostEqual(report["summary"]["ligand_rmsd_nm"]["mean"], 0.75, places=5)
        self.assertLess(report["summary"]["ligand_self_rmsd_nm"]["mean"], 1e-5)
        self.assertAlmostEqual(report["window_ns"][-1], 0.09)

    def test_contact_occupancy_uses_minimum_image(self):
        universe = molecular_fixture()
        universe.trajectory.coordinate_array[:, 6:, 0] += 100
        report = analyze_universe(universe, self.request, self.index)
        self.assertEqual(report["residues"][0]["contact_occupancy"], 1)

    def test_contact_count_counts_residues_not_atom_pairs(self):
        report = analyze_universe(molecular_fixture(), self.request, self.index)
        self.assertEqual(report["binding"]["initial_contact_residues"], 1)
        self.assertEqual(report["summary"]["contact_residues"]["mean"], 1)

    def test_invalid_named_fit_group_does_not_fall_back_to_builtin(self):
        self.index.write_text("[ C-alpha ]\n1 1 2\n[ Ligand ]\n7 8 9\n")
        with self.assertRaises(ValueError):
            analyze_universe(molecular_fixture(), self.request, self.index)
        self.index.write_text("[ C-alpha ]\n7 8 9\n[ Ligand ]\n7 8 9\n")
        with self.assertRaises(ValueError):
            analyze_universe(molecular_fixture(), self.request, self.index)

    def test_inadequate_sampling_invalid_index_and_protein_overlap_fail(self):
        with self.assertRaises(ValueError):
            analyze_universe(molecular_fixture(frames=2), self.request, self.index)
        for text in ("[ Ligand ]\n7 7 9", "[ Ligand ]\n0 8 9", "[ Ligand ]\n7 8 99", "[ Ligand ]\n1 2 3"):
            self.index.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                analyze_universe(molecular_fixture(), self.request, self.index)
        self.index.write_text("[ Ligand ]\n7 8 9\n[ Ligand ]\n7 8 9")
        with self.assertRaises(ValueError):
            read_index_group(self.index, "Ligand", 9)

    def test_all_frames_contribute_to_block_means(self):
        summary = series_summary(list(range(11)))
        self.assertEqual(summary["samples"], 11)
        self.assertEqual(summary["mean"], 5)
        self.assertEqual(summary["block_means"], [2.5, 8.0])

    def test_research_workflow_is_cancellable_and_window_applies_to_energy(self):
        store = JobStore(self.root / "jobs")
        source = store.create("source", {"workflow": "protein_md", "force_field": "amber19sb"}, [], [])
        root = store.job_dir(source["id"])
        for name in ("input.tpr", "input.xtc", "input.edr", "index.ndx"):
            (root / name).write_bytes(b"fixture")
        (root / "index.ndx").write_text("[ Protein ]\n1 2 3 4 5 6\n[ Ligand ]\n7 8 9\n")
        (root / "md.mdp").write_text("ref_t = 310\ndt = .002\n")
        store.start(source["id"], [])
        store.claim_next("fixture")
        store.update(source["id"], status="completed")
        request = self.request.model_copy(update={"begin_ns": 20, "end_ns": 80, "do_energy": True, "edr_file": "input.edr"})
        job = create_existing_analysis(store, source["id"], request, {"gmx_bin": "gmx"})
        command = next(item for item in job["commands"] if "app.analysis.research" in item["args"])
        self.assertEqual(command["kind"], "command")
        research_position = next(i for i, item in enumerate(job["commands"]) if "app.analysis.research" in item["args"])
        figures_position = next(i for i, item in enumerate(job["commands"]) if item["operation"] == "generate_analysis_figures")
        self.assertLess(research_position, figures_position)
        for command in [job["commands"][0], next(item for item in job["commands"] if "energy" in item["args"])]:
            self.assertEqual(command["args"][command["args"].index("-b") + 1], "20000")
            self.assertEqual(command["args"][command["args"].index("-e") + 1], "80000")
        self.assertEqual(job["analysis_source"]["protocol"]["ref_t"], "310")
        self.assertEqual(job["commands"][0]["stdin_text"], "ResearchSolute\nResearchSolute\nSystem\n")
        self.assertIn("research-index.ndx", job["uploaded_files"])
        self.assertNotIn("research-report.json", job["commands"][-1]["data"]["paths"])
        with patch.object(main, "STORE", store):
            groups = main.analysis_index_groups(source["id"], "index.ndx")
            self.assertEqual(groups["groups"], [{"name": "Protein", "atoms": 6}, {"name": "Ligand", "atoms": 3}])
            with self.assertRaises(HTTPException) as failure:
                main.analysis_index_groups(source["id"], "../index.ndx")
            self.assertEqual(failure.exception.status_code, 400)


class ResearchComparisonTests(unittest.TestCase):
    def test_mutation_and_insertion_align_by_sequence_not_residue_numbers(self):
        reference = comparison_fixture("ref")["residues"]
        other = copy.deepcopy(reference)
        other[2]["resname"] = "ALA"
        for row in other:
            row["resid"] += 100
        mapping = align_residues(reference, other)
        self.assertEqual(mapping[2], 2)
        inserted = copy.deepcopy(reference)
        inserted.insert(2, {**inserted[1], "resname": "TRP"})
        self.assertEqual(align_residues(reference, inserted)[2], 3)

    def test_different_chains_are_not_silently_mapped(self):
        reference = comparison_fixture("ref")["residues"]
        other = [{**item, "chain": "B"} for item in reference]
        with self.assertRaises(ValueError):
            align_residues(reference, other)

    def test_group_estimates_weight_trajectories_equally_and_do_not_test_frames(self):
        result = compare_reports([comparison_fixture("r1"), comparison_fixture("r2", 0.2)], [comparison_fixture("m1", 0.4)])
        rg = next(item for item in result["metrics"] if item["metric"] == "ca_rg_nm")
        self.assertAlmostEqual(rg["difference"], 0.3)
        self.assertEqual(rg["reference"]["n"], 2)
        self.assertIsNone(rg["comparison"]["sd"])
        self.assertAlmostEqual(result["residues"][0]["contact_occupancy"]["difference"], -0.3)
        self.assertNotIn("p_value", result)

    def test_same_input_trajectory_and_incompatible_ligands_are_not_pooled(self):
        reference = comparison_fixture("ref")
        duplicate = comparison_fixture("mut")
        duplicate["input_hashes"] = reference["input_hashes"]
        with self.assertRaises(ValueError):
            compare_reports([reference], [duplicate])
        other = comparison_fixture("mut")
        other["binding"]["cutoff_nm"] = 0.5
        result = compare_reports([reference], [other])
        self.assertEqual([item["metric"] for item in result["metrics"]], ["ca_rg_nm"])
        self.assertNotIn("contact_occupancy", result["residues"][0])

    def test_request_bounds_and_report_path_safety(self):
        for kwargs in ({"end_ns": 0}, {"research_stride": 0}, {"begin_ns": float("nan")}, {"contact_cutoff_nm": 3}):
            with self.assertRaises(ValidationError):
                ExistingAnalysisRequest(tpr_file="a.tpr", trajectory_file="a.xtc", **kwargs)
        with self.assertRaises(ValidationError):
            ResearchComparisonRequest(reference_jobs=["a"] * 7, comparison_jobs=["b"])
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder))
            job = store.create("fixture", {}, [], [])
            store.start(job["id"], [])
            store.claim_next("fixture")
            store.update(job["id"], status="completed")
            outside = Path(folder) / "outside.json"
            outside.write_text(json.dumps(comparison_fixture("fixture")))
            (store.job_dir(job["id"]) / "research-report.json").symlink_to(outside)
            with self.assertRaises(ValueError):
                load_research_report(store, job["id"])
            with patch.object(main, "STORE", store):
                with self.assertRaises(HTTPException) as failure:
                    main.research_report(job["id"])
                self.assertEqual(failure.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
