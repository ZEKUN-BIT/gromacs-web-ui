import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.gromacs import (
    Step,
    _create_complex_index,
    _read_gro,
    _solvent_template,
    build_steps,
    execute_internal_step,
    installed_water_model_selection,
)
from app.main import PreviewRequest, preview


def write_gro(path: Path, atom_count: int, water_from: int | None = None) -> None:
    atoms = []
    protein_names = ("N", "CA", "C")
    for index in range(1, atom_count + 1):
        is_water = water_from is not None and index >= water_from
        residue = "SOL" if is_water else "RES"
        atom = "OW" if is_water else protein_names[(index - 1) % len(protein_names)]
        atoms.append(f"{1:5d}{residue:<5}{atom:>5}{index:5d}{index * 0.1:8.3f}{0.0:8.3f}{0.0:8.3f}")
    path.write_text("test\n" + f"{atom_count}\n" + "\n".join(atoms) + "\n2.0 2.0 2.0\n", encoding="utf-8")


class MolecularDynamicsWorkflowTests(unittest.TestCase):
    def test_new_md_jobs_finish_with_organized_publication_figures(self) -> None:
        for params, files in (
            ({"workflow": "protein_md", "structure_file": "protein.pdb"}, ["protein.pdb"]),
            (
                {
                    "workflow": "protein_ligand_md",
                    "protein_file": "complex.pdb",
                    "ligand_smiles": "CCO",
                    "ligand_charge": 0,
                },
                ["complex.pdb"],
            ),
        ):
            final_step = build_steps(params, files)[-1]
            self.assertEqual(final_step.operation, "generate_analysis_figures")
            self.assertEqual(final_step.outputs, ["figures/manifest.json"])

    def test_generate_analysis_figures_creates_category_folders_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "md-backbone-rmsd.xvg").write_text(
                '@ title "Backbone RMSD"\n@ xaxis label "Time (ns)"\n@ yaxis label "RMSD (nm)"\n0 0.1\n1 0.2\n',
                encoding="utf-8",
            )
            step = Step(
                "figures",
                ["internal"],
                kind="internal",
                operation="generate_analysis_figures",
                data={"output_dir": "figures", "dpi": 72},
            )
            execute_internal_step(root, step)
            manifest = json.loads((root / "figures" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["figure_count"], 1)
            self.assertTrue(all((root / "figures" / category).is_dir() for category in ("structure", "interaction", "sampling", "quality")))
            self.assertTrue((root / "figures" / "structure" / "md-backbone-rmsd.png").is_file())
            self.assertTrue((root / "figures" / "structure" / "md-backbone-rmsd.pdf").is_file())

    def test_cofolded_pose_uses_independent_conformer_for_acpype(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "complex.pdb",
            "ligand_chemistry_file": "ligand.sdf",
            "ligand_charge": -1,
            "ligand_charge_confirmed": True,
        }
        steps = build_steps(params, ["complex.pdb", "ligand.sdf"])
        titles = [step.title for step in steps]
        self.assertLess(titles.index("Map ligand chemistry onto co-folded pose"), titles.index("Parameterize ligand with ACPYPE"))
        mapped = next(step for step in steps if step.operation == "prepare_ligand_chemistry")
        self.assertEqual(mapped.data["pose_pdb"], "ligand_from_complex.pdb")
        self.assertEqual(mapped.data["expected_charge"], -1)
        conformer = next(step for step in steps if step.title == "Generate ligand parameterization conformer")
        self.assertIn("--gen3d", conformer.args)
        self.assertIn("ligand_chemistry.sdf", conformer.args)
        self.assertIn("ligand_parameterization.sdf", conformer.args)
        acpype = next(step for step in steps if step.title == "Parameterize ligand with ACPYPE")
        self.assertIn("ligand_parameterization.sdf", acpype.args)
        restore = next(step for step in steps if step.operation == "restore_ligand_pose_coordinates")
        self.assertEqual(restore.data["pose_sdf"], "ligand_prepared.sdf")
        self.assertEqual(restore.data["gro_file"], "ligand_GMX.gro")

    def test_smiles_is_staged_as_data_not_a_shell_command(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "complex.pdb",
            "ligand_smiles": "CC(=O)[O-]",
            "ligand_charge": -1,
        }
        steps = build_steps(params, ["complex.pdb"])
        stage = next(step for step in steps if step.operation == "write_ligand_smiles")
        self.assertEqual(stage.kind, "internal")
        normalize = next(step for step in steps if step.title == "Normalize ligand chemical topology")
        self.assertNotIn("CC(=O)[O-]", normalize.args)
        self.assertIn("ligand_chemistry.smi", normalize.args)

    def test_ligand_chemistry_sources_are_unambiguous_and_limited(self) -> None:
        base = {"workflow": "protein_ligand_md", "protein_file": "complex.pdb"}
        with self.assertRaisesRegex(ValueError, "either"):
            build_steps({**base, "ligand_chemistry_file": "ligand.sdf", "ligand_smiles": "CC"}, ["complex.pdb", "ligand.sdf"])
        with self.assertRaisesRegex(ValueError, "sdf or .mol2"):
            build_steps({**base, "ligand_chemistry_file": "ligand.txt"}, ["complex.pdb", "ligand.txt"])

    def test_ligand_chemistry_mapping_preserves_pose_coordinates_and_charge(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pose.pdb").write_text(
                "HETATM    1  C1  LIG A   1       1.000   2.000   3.000  1.00 20.00           C\n"
                "HETATM    2  O1  LIG A   1       2.200   2.000   3.000  1.00 20.00           O\nEND\n",
                encoding="utf-8",
            )
            (root / "chem.sdf").write_text(
                "lig\n  test\n\n  2  1  0  0  0  0            999 V2000\n"
                "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "    1.2000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "  1  2  1  0  0  0  0\nM  CHG  1   2  -1\nM  END\n$$$$\n",
                encoding="utf-8",
            )
            step = Step(
                "map",
                ["internal"],
                kind="internal",
                operation="prepare_ligand_chemistry",
                data={
                    "pose_pdb": "pose.pdb",
                    "chemistry_sdf": "chem.sdf",
                    "output_sdf": "out.sdf",
                    "report": "report.txt",
                    "expected_charge": -1,
                },
            )
            execute_internal_step(root, step)
            output = (root / "out.sdf").read_text(encoding="utf-8")
            self.assertIn("    1.0000    2.0000    3.0000 C", output)
            self.assertIn("M  CHG  1   2  -1", output)
            self.assertIn("status=PASS", (root / "report.txt").read_text(encoding="utf-8"))

    def test_ligand_chemistry_charge_mismatch_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pose.pdb").write_text(
                "HETATM    1  C1  LIG A   1       0.000   0.000   0.000  1.00 20.00           C\n", encoding="utf-8"
            )
            (root / "chem.sdf").write_text(
                "lig\n test\n\n  1  0  0  0  0  0            999 V2000\n    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n$$$$\n",
                encoding="utf-8",
            )
            step = Step(
                "map",
                ["internal"],
                kind="internal",
                operation="prepare_ligand_chemistry",
                data={
                    "pose_pdb": "pose.pdb",
                    "chemistry_sdf": "chem.sdf",
                    "output_sdf": "out.sdf",
                    "report": "report.txt",
                    "expected_charge": -1,
                },
            )
            with self.assertRaisesRegex(ValueError, "formal charge"):
                execute_internal_step(root, step)

    def test_restore_ligand_pose_coordinates_preserves_acpype_gro_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sdf_template = (
                "lig\n  test\n\n  2  1  0  0  0  0            999 V2000\n"
                "{carbon} C   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "{hydrogen} H   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "  1  2  1  0  0  0  0\nM  END\n$$$$\n"
            )
            (root / "pose.sdf").write_text(
                sdf_template.format(carbon="   10.0000   20.0000   30.0000", hydrogen="   11.0000   20.0000   30.0000"),
                encoding="utf-8",
            )
            (root / "parameter.sdf").write_text(
                sdf_template.format(carbon="    0.0000    0.0000    0.0000", hydrogen="    1.0000    0.0000    0.0000"),
                encoding="utf-8",
            )
            (root / "ligand.gro").write_text(
                "ACPYPE ligand\n2\n"
                "    1MOL     C1    1   0.000   0.000   0.000\n"
                "    1MOL     H1    2   0.100   0.000   0.000\n"
                "   1.00000   1.00000   1.00000\n",
                encoding="utf-8",
            )
            execute_internal_step(
                root,
                Step(
                    "restore",
                    ["internal"],
                    kind="internal",
                    operation="restore_ligand_pose_coordinates",
                    data={
                        "pose_sdf": "pose.sdf",
                        "parameter_sdf": "parameter.sdf",
                        "gro_file": "ligand.gro",
                        "report": "restore.txt",
                    },
                ),
            )
            title, atoms, box = _read_gro(root / "ligand.gro")
            self.assertEqual(title, "ACPYPE ligand")
            self.assertEqual(atoms[0][10:20], "   C1    1")
            self.assertEqual(atoms[0][20:44], "   1.000   2.000   3.000")
            self.assertEqual(atoms[1][20:44], "   1.100   2.000   3.000")
            self.assertEqual(box, "   1.00000   1.00000   1.00000")
            self.assertIn("atom_order=verified", (root / "restore.txt").read_text(encoding="utf-8"))

    def test_restore_ligand_pose_coordinates_rejects_changed_atom_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pose.sdf").write_text(
                "lig\n test\n\n  1  0  0  0  0  0            999 V2000\n"
                "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n$$$$\n",
                encoding="utf-8",
            )
            (root / "parameter.sdf").write_text(
                "lig\n test\n\n  1  0  0  0  0  0            999 V2000\n"
                "    0.0000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\nM  END\n$$$$\n",
                encoding="utf-8",
            )
            (root / "ligand.gro").write_text(
                "ligand\n1\n    1MOL     C1    1   0.000   0.000   0.000\n1.0 1.0 1.0\n",
                encoding="utf-8",
            )
            step = Step(
                "restore",
                ["internal"],
                kind="internal",
                operation="restore_ligand_pose_coordinates",
                data={
                    "pose_sdf": "pose.sdf",
                    "parameter_sdf": "parameter.sdf",
                    "gro_file": "ligand.gro",
                    "report": "restore.txt",
                },
            )
            with self.assertRaisesRegex(ValueError, "atom order changed"):
                execute_internal_step(root, step)

    def test_ligand_chemistry_maps_reordered_atoms_without_pdb_conect(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pose.pdb").write_text(
                "HETATM    1  C1  LIG A   1       0.000   0.000   0.000  1.00 20.00           C\n"
                "HETATM    2  O1  LIG A   1       1.250   0.000   0.000  1.00 20.00           O\n"
                "HETATM    3  N1  LIG A   1      -1.300   0.000   0.000  1.00 20.00           N\nEND\n",
                encoding="utf-8",
            )
            # Chemistry order is N, C, O while the pose order is C, O, N.
            (root / "chem.sdf").write_text(
                "lig\n test\n\n  3  2  0  0  0  0            999 V2000\n"
                "   -1.3000    0.0000    0.0000 N   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "    1.2500    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n"
                "  1  2  1  0  0  0  0\n  2  3  1  0  0  0  0\nM  END\n$$$$\n",
                encoding="utf-8",
            )
            step = Step(
                "map",
                ["internal"],
                kind="internal",
                operation="prepare_ligand_chemistry",
                data={
                    "pose_pdb": "pose.pdb",
                    "chemistry_sdf": "chem.sdf",
                    "output_sdf": "out.sdf",
                    "report": "report.txt",
                    "expected_charge": 0,
                },
            )
            execute_internal_step(root, step)
            output = (root / "out.sdf").read_text(encoding="utf-8")
            self.assertIn("   -1.3000    0.0000    0.0000 N", output)
            self.assertIn("mapping=graph-isomorphism", (root / "report.txt").read_text(encoding="utf-8"))
            self.assertIn("mapping_source=distance-inferred-graph", (root / "report.txt").read_text(encoding="utf-8"))

    def test_water_coordinate_templates_match_site_count(self) -> None:
        self.assertEqual(_solvent_template({"water_model": "opc"}), "tip4p.gro")
        self.assertEqual(_solvent_template({"water_model": "opc3"}), "spc216.gro")
        self.assertEqual(_solvent_template({"water_model": "tip3p"}), "spc216.gro")

    def test_installed_amber19sb_opc_uses_numeric_menu_selection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            gmx = prefix / "bin" / "gmx"
            gmx.parent.mkdir()
            gmx.touch()
            top = prefix / "share" / "gromacs" / "top"
            force_field = top / "amber19sb.ff"
            force_field.mkdir(parents=True)
            (force_field / "watermodels.dat").write_text("opc OPC 4-site water model\nopc3 OPC3 3-site water model\n")
            with patch.dict("os.environ", {"GMXLIB": str(top)}):
                self.assertEqual(installed_water_model_selection(str(gmx), "amber19sb", "opc"), "1")
                params = {"workflow": "protein_md", "structure_file": "protein.pdb", "water_model": "opc", "gmx_bin": str(gmx)}
                topology = next(step for step in build_steps(params, ["protein.pdb"]) if step.title == "Build topology")
                self.assertEqual(topology.stdin_text, "1\n")

    def test_scientific_frontend_values_are_validated_server_side(self) -> None:
        base = {"workflow": "protein_ligand_md", "protein_file": "complex.pdb", "ligand_charge_confirmed": True}
        with self.assertRaisesRegex(ValueError, "ion_concentration"):
            build_steps({**base, "ion_concentration": "not-a-number"}, ["complex.pdb"])
        with self.assertRaisesRegex(ValueError, "ligand_ph"):
            build_steps({**base, "use_obabel": True, "ligand_ph": 15}, ["complex.pdb"])

    def test_numeric_salt_concentration_from_frontend_previews(self) -> None:
        request = PreviewRequest(workflow="protein_md", structure_file="protein.pdb", files=["protein.pdb"], ion_concentration=0.15)
        commands = preview(request)["commands"]
        genion = next(item for item in commands if item["title"] == "Neutralize")
        self.assertIn("0.15", genion["args"])

    def test_production_uses_checkpoint_continuation(self) -> None:
        params = {"workflow": "protein_md", "structure_file": "protein.pdb", "water_model": "opc"}
        production = next(step for step in build_steps(params, ["protein.pdb"]) if step.title == "Production MD")
        self.assertIn("-cpi", production.args)
        self.assertIn("md.cpt", production.args)

    def test_postprocess_removes_centered_intermediate_by_default(self) -> None:
        params = {"workflow": "postprocess", "tpr_file": "run.tpr", "trajectory_file": "run.xtc"}
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        cleanup = next(step for step in steps if step.title == "Remove intermediate centered trajectory")
        self.assertEqual(cleanup.operation, "remove_files")
        self.assertEqual(cleanup.data["paths"], ["md_0_100_center.xtc"])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            centered = root / "md_0_100_center.xtc"
            centered.write_bytes(b"trajectory")
            execute_internal_step(root, cleanup)
            self.assertFalse(centered.exists())

    def test_postprocess_removes_fitted_trajectory_by_default(self) -> None:
        params = {"workflow": "postprocess", "tpr_file": "run.tpr", "trajectory_file": "run.xtc"}
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        cleanup = next(step for step in steps if step.title == "Remove fitted trajectory")
        self.assertEqual(cleanup.operation, "remove_files")
        self.assertEqual(cleanup.data["paths"], ["md_0_100_fit.xtc"])

    def test_postprocess_can_keep_centered_intermediate(self) -> None:
        params = {
            "workflow": "postprocess",
            "tpr_file": "run.tpr",
            "trajectory_file": "run.xtc",
            "keep_centered_trajectory": True,
        }
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        self.assertNotIn("Remove intermediate centered trajectory", [step.title for step in steps])

    def test_postprocess_can_keep_fitted_trajectory(self) -> None:
        params = {
            "workflow": "postprocess",
            "tpr_file": "run.tpr",
            "trajectory_file": "run.xtc",
            "keep_fitted_trajectory": True,
        }
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        self.assertNotIn("Remove fitted trajectory", [step.title for step in steps])

    def test_backbone_rmsd_runs_on_unwrapped_trajectory(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "structure_file": "complex.pdb",
            "water_model": "opc",
            "ligand_charge": -1,
            "production_deffnm": "md_0_100",
            "replicas": 1,
        }
        steps = build_steps(params, ["complex.pdb"])
        whole = next(step for step in steps if step.title == "Unwrap replica 1 trajectory for backbone RMSD")
        self.assertIn("-pbc", whole.args)
        self.assertIn("nojump", whole.args)
        self.assertNotIn("-center", whole.args)
        self.assertIn("md_0_100-nojump.xtc", whole.args)
        self.assertEqual(whole.stdin_text, "System\n")
        rmsd = next(step for step in steps if step.title == "Compute replica 1 backbone RMSD")
        clustered = next(step for step in steps if step.title == "Cluster and center replica 1 trajectory for backbone RMSD")
        self.assertIn("md_0_100-nojump.xtc", clustered.args)
        self.assertIn("cluster", clustered.args)
        self.assertIn("-center", clustered.args)
        self.assertEqual(clustered.stdin_text, "Protein_Lig\nProtein_Lig\nSystem\n")
        self.assertIn("md_0_100-rmsd-whole.xtc", rmsd.args)
        self.assertEqual(rmsd.args[rmsd.args.index("-fit") + 1], "rot+trans")
        self.assertIn("md_0_100.tpr", rmsd.args)
        self.assertNotIn("md_0_100.xtc", rmsd.args)

    def test_protein_only_workflow_runs_rmsd_on_unwrapped_trajectory(self) -> None:
        params = {"workflow": "protein_md", "structure_file": "protein.pdb", "water_model": "opc", "replicas": 1}
        steps = build_steps(params, ["protein.pdb"])
        whole = next(step for step in steps if step.title == "Unwrap replica 1 trajectory for backbone RMSD")
        self.assertEqual(whole.stdin_text, "System\n")
        self.assertIn("nojump", whole.args)
        rmsd = next(step for step in steps if step.title == "Compute replica 1 backbone RMSD")
        cluster = next(step for step in steps if step.title == "Cluster and center replica 1 trajectory for backbone RMSD")
        self.assertEqual(cluster.stdin_text, "Protein\nProtein\nSystem\n")
        self.assertIn("md-rmsd-whole.xtc", rmsd.args)
        self.assertNotIn("md.xtc", rmsd.args)

    def test_postprocess_defaults_to_cluster_pbc_for_multimers(self) -> None:
        params = {"workflow": "postprocess", "tpr_file": "run.tpr", "trajectory_file": "run.xtc"}
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        center = next(step for step in steps if step.title == "Center trajectory and repair PBC")
        self.assertIn("cluster", center.args)
        self.assertNotIn("-ur", center.args)
        self.assertEqual(center.stdin_text, "Protein_Lig\nProtein_Lig\nSystem\n")

    def test_analysis_rmsd_unwraps_before_rmsd(self) -> None:
        params = {"workflow": "analysis_rmsd", "tpr_file": "run.tpr", "trajectory_file": "run.xtc"}
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        whole = next(step for step in steps if step.title == "Unwrap trajectory for RMSD")
        self.assertIn("nojump", whole.args)
        self.assertIn("rmsd-nojump.xtc", whole.args)
        self.assertEqual(whole.stdin_text, "System\n")
        rmsd = next(step for step in steps if step.title == "Compute RMSD")
        cluster = next(step for step in steps if step.title == "Cluster and center trajectory for RMSD")
        self.assertIn("rmsd-nojump.xtc", cluster.args)
        self.assertEqual(cluster.stdin_text, "Protein\nProtein\nSystem\n")
        self.assertIn("rmsd-whole.xtc", rmsd.args)
        self.assertEqual(rmsd.args[rmsd.args.index("-fit") + 1], "rot+trans")
        self.assertNotIn("run.xtc", rmsd.args)

    def test_analysis_suite_rmsd_runs_on_unwrapped_trajectory(self) -> None:
        params = {"workflow": "analysis_suite", "tpr_file": "run.tpr", "trajectory_file": "run.xtc"}
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        whole = next(step for step in steps if step.title == "Unwrap trajectory for RMSD")
        self.assertIn("nojump", whole.args)
        self.assertEqual(whole.stdin_text, "System\n")
        rmsd = next(step for step in steps if step.title == "Compute RMSD")
        self.assertIn("rmsd-whole.xtc", rmsd.args)
        self.assertNotIn("run.xtc", rmsd.args)

    def test_analysis_suite_reuses_unrotated_clustered_trajectory_for_all_structure_metrics(self) -> None:
        params = {
            "workflow": "analysis_suite",
            "tpr_file": "run.tpr",
            "trajectory_file": "run.xtc",
            "index_file": "custom.ndx",
            "center_group": "MySolute",
            "do_dssp": True,
            "do_pca": True,
            "do_energy": False,
            "begin_ns": 20,
            "end_ns": 80,
        }
        steps = build_steps(params, ["run.tpr", "run.xtc", "custom.ndx"])
        cluster = next(step for step in steps if step.title == "Cluster and center trajectory for RMSD")
        self.assertEqual(cluster.stdin_text, "MySolute\nMySolute\nSystem\n")
        self.assertNotIn("-fit", cluster.args)
        self.assertEqual(cluster.args[cluster.args.index("-b") + 1], "20000")
        self.assertEqual(cluster.args[cluster.args.index("-e") + 1], "80000")
        for step in steps:
            if len(step.args) > 1 and step.args[1] in {"trjconv", "rms", "gyrate", "dssp", "hbond-legacy", "covar", "anaeig"}:
                self.assertEqual(step.args[step.args.index("-n") + 1], "custom.ndx")
                if step.args[1] != "trjconv":
                    self.assertEqual(step.args[step.args.index("-f") + 1], "rmsd-whole.xtc")
        unwrap = next(step for step in steps if step.title == "Unwrap trajectory for RMSD")
        self.assertEqual(unwrap.args[unwrap.args.index("-f") + 1], "run.xtc")
        self.assertNotIn("-b", unwrap.args)
        covariance = next(step for step in steps if step.title == "Build covariance matrix")
        self.assertIn("-fit", covariance.args)
        cleanup = next(step for step in steps if step.title == "Remove intermediate analysis trajectory")
        self.assertEqual(cleanup.data["paths"], ["rmsd-whole.xtc"])
        self.assertGreater(steps.index(cleanup), steps.index(covariance))
        self.assertEqual(sum("cluster" in step.args for step in steps), 1)

    def test_analysis_suite_prepares_structure_when_rmsd_is_disabled(self) -> None:
        params = {"workflow": "analysis_suite", "tpr_file": "run.tpr", "trajectory_file": "run.xtc", "do_rmsd": False, "do_energy": False}
        steps = build_steps(params, ["run.tpr", "run.xtc"])
        prep = steps[0]
        self.assertEqual(prep.title, "Prepare whole-system trajectory for analysis")
        self.assertEqual(prep.stdin_text, "Protein\nProtein\nSystem\n")
        self.assertIn("cluster", prep.args)
        rg = next(step for step in steps if step.title == "Compute radius of gyration")
        self.assertEqual(rg.args[rg.args.index("-f") + 1], "run-analysis-whole.xtc")

    def test_distinct_rmsd_source_does_not_replace_other_analysis_input(self) -> None:
        params = {
            "workflow": "analysis_suite",
            "tpr_file": "run.tpr",
            "trajectory_file": "run.xtc",
            "rmsd_trajectory_file": "other.xtc",
            "do_energy": False,
        }
        steps = build_steps(params, ["run.tpr", "run.xtc", "other.xtc"])
        unwrap = next(step for step in steps if step.title == "Unwrap trajectory for RMSD")
        self.assertEqual(unwrap.args[unwrap.args.index("-f") + 1], "other.xtc")
        prep = next(step for step in steps if step.title == "Prepare whole-system trajectory for analysis")
        self.assertEqual(prep.args[prep.args.index("-f") + 1], "run.xtc")
        cleanup = next(step for step in steps if step.title == "Remove intermediate RMSD trajectory")
        self.assertLess(steps.index(cleanup), steps.index(prep))

    def test_rmsd_only_has_index_and_removes_intermediates_after_last_use(self) -> None:
        params = {
            "workflow": "analysis_rmsd",
            "tpr_file": "run.tpr",
            "trajectory_file": "run.xtc",
            "index_file": "custom.ndx",
            "fit_group": "MyBackbone",
            "rmsd_group": "MyLigand",
            "center_group": "MySolute",
        }
        steps = build_steps(params, ["run.tpr", "run.xtc", "custom.ndx"])
        rmsd = next(step for step in steps if step.title == "Compute RMSD")
        self.assertEqual(rmsd.stdin_text, "MyBackbone\nMyLigand\n")
        self.assertEqual(rmsd.args[rmsd.args.index("-n") + 1], "custom.ndx")
        self.assertEqual(steps[steps.index(rmsd) - 1].data["paths"], ["rmsd-nojump.xtc"])
        self.assertEqual(steps[steps.index(rmsd) + 1].data["paths"], ["rmsd-whole.xtc"])

    def test_rmsd_intermediates_cannot_overwrite_source_trajectory(self) -> None:
        for source in ("rmsd-nojump.xtc", "rmsd-whole.xtc"):
            with self.subTest(source=source):
                params = {"workflow": "analysis_rmsd", "tpr_file": "run.tpr", "trajectory_file": source}
                with self.assertRaisesRegex(ValueError, "intermediate filenames"):
                    build_steps(params, ["run.tpr", source])

    def test_analysis_suite_rejects_rmsd_trajectory_path_traversal(self) -> None:
        params = {
            "workflow": "analysis_suite",
            "tpr_file": "run.tpr",
            "trajectory_file": "run.xtc",
            "rmsd_trajectory_file": "../other-job.xtc",
        }
        with self.assertRaises(ValueError):
            build_steps(params, ["run.tpr", "run.xtc"])

    def test_complex_md_adds_classic_structural_and_interaction_analysis(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "structure_file": "complex.pdb",
            "water_model": "opc",
            "ligand_charge": -1,
            "production_deffnm": "md_0_100",
        }
        steps = build_steps(params, ["complex.pdb"])
        titles = [step.title for step in steps]
        for title in (
            "Keep protein and ligand in the same periodic image",
            "Extract protein-ligand RMSD reference",
            "Compute protein C-alpha RMSF",
            "Compute protein radius of gyration",
            "Compute protein solvent-accessible surface area",
            "Compute ligand RMSD relative to protein",
            "Compute protein-ligand minimum distance and contacts",
            "Compute protein-ligand hydrogen bonds",
        ):
            self.assertIn(title, titles)
        hbond = next(step for step in steps if step.title == "Compute protein-ligand hydrogen bonds")
        self.assertIn("hbond-legacy", hbond.args)
        self.assertEqual(hbond.stdin_text, "Ligand\nProtein\n")
        cluster = next(step for step in steps if step.title == "Keep protein and ligand in the same periodic image")
        self.assertEqual(cluster.stdin_text, "Protein_Lig\nProtein_Lig\nSystem\n")
        self.assertIn("md_0_100-complex-whole.xtc", hbond.args)
        ligand_rmsd = next(step for step in steps if step.title == "Compute ligand RMSD relative to protein")
        self.assertIn("md_0_100.tpr", ligand_rmsd.args)
        self.assertNotIn("md_0_100-complex-reference.gro", ligand_rmsd.args)
        self.assertEqual(ligand_rmsd.args[ligand_rmsd.args.index("-fit") + 1], "rot+trans")
        rmsf = next(step for step in steps if step.title == "Compute protein C-alpha RMSF")
        self.assertIn("-fit", rmsf.args)

    def test_three_replicas_get_independent_velocity_generation(self) -> None:
        params = {"workflow": "protein_md", "structure_file": "protein.pdb", "water_model": "opc", "replicas": 3}
        steps = build_steps(params, ["protein.pdb"])
        productions = [step for step in steps if step.title.startswith("Production MD")]
        self.assertEqual(len(productions), 3)
        self.assertTrue(all("-cpi" in step.args for step in productions))
        configure = next(step for step in steps if step.title == "Configure replica 1")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "md.mdp").write_text("continuation = yes\ngen_vel = no\n", encoding="utf-8")
            execute_internal_step(root, configure)
            text = (root / "md-replica-01.mdp").read_text(encoding="utf-8")
            self.assertIn("continuation            = no", text)
            self.assertIn("gen_vel                 = yes", text)

    def test_structure_preflight_reports_altloc_and_hetero(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdb = "ATOM      1  CA AALA A   1      0.000   0.000   0.000  1.00 20.00           C\nHETATM    2  C1  LIG A 101      1.000   0.000   0.000  1.00 20.00           C\n"
            (root / "input.pdb").write_text(pdb, encoding="utf-8")
            step = Step(
                "inspect",
                ["internal"],
                kind="internal",
                operation="structure_preflight",
                data={"source": "input.pdb", "output": "report.txt"},
            )
            execute_internal_step(root, step)
            report = (root / "report.txt").read_text(encoding="utf-8")
            self.assertIn("altlocs=A", report)
            self.assertIn("hetero_residues=LIG", report)
            self.assertIn("status=REVIEW", report)

    def test_thermodynamic_gate_rejects_density_drift(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            lines = ['@ s0 legend "Temperature"', '@ s1 legend "Pressure"', '@ s2 legend "Density"', '@ s3 legend "Volume"']
            lines.extend(f"{i} 300 1 {900 + i * 5} {100 + i}" for i in range(20))
            (root / "npt.xvg").write_text("\n".join(lines) + "\n", encoding="utf-8")
            step = Step(
                "gate",
                ["internal"],
                kind="internal",
                operation="validate_thermodynamics",
                data={"xvg": "npt.xvg", "output": "quality.json", "target_temperature": 300, "strict": True},
            )
            with self.assertRaisesRegex(ValueError, "thermodynamic quality gate failed"):
                execute_internal_step(root, step)

    def test_default_protein_workflow_goes_directly_from_em_to_npt(self) -> None:
        params = {"workflow": "protein_md", "structure_file": "protein.pdb", "water_model": "opc"}
        steps = build_steps(params, ["protein.pdb"])
        titles = [step.title for step in steps]
        self.assertNotIn("Run optional NVT", titles)
        prepare_npt = next(step for step in steps if step.title == "Prepare NPT")
        self.assertIn("em.gro", prepare_npt.args)
        self.assertNotIn("nvt.cpt", prepare_npt.args)

    def test_optional_nvt_preserves_checkpoint_for_npt(self) -> None:
        params = {"workflow": "protein_md", "structure_file": "protein.pdb", "water_model": "opc", "pre_nvt": True}
        steps = build_steps(params, ["protein.pdb"])
        self.assertIn("Run optional NVT", [step.title for step in steps])
        prepare_npt = next(step for step in steps if step.title == "Prepare NPT")
        self.assertIn("nvt.gro", prepare_npt.args)
        self.assertIn("nvt.cpt", prepare_npt.args)

    def test_npt_mdp_is_configured_for_direct_or_continued_start(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "npt.mdp").write_text("continuation = yes\ngen_vel = no\n", encoding="utf-8")
            direct = Step(
                "configure",
                ["internal"],
                kind="internal",
                operation="prepare_npt_mdp",
                data={"source_mdp": "npt.mdp", "output_mdp": "direct.mdp", "after_nvt": False},
            )
            continued = Step(
                "configure",
                ["internal"],
                kind="internal",
                operation="prepare_npt_mdp",
                data={"source_mdp": "npt.mdp", "output_mdp": "continued.mdp", "after_nvt": True},
            )
            execute_internal_step(root, direct)
            execute_internal_step(root, continued)
            self.assertIn("continuation            = no", (root / "direct.mdp").read_text(encoding="utf-8"))
            self.assertIn("gen_vel                 = yes", (root / "direct.mdp").read_text(encoding="utf-8"))
            self.assertIn("continuation            = yes", (root / "continued.mdp").read_text(encoding="utf-8"))
            self.assertIn("gen_vel                 = no", (root / "continued.mdp").read_text(encoding="utf-8"))

    def test_complex_index_is_generated_from_atom_counts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_gro(root / "protein.gro", 2)
            write_gro(root / "ligand.gro", 1)
            write_gro(root / "system.gro", 7, water_from=4)
            _create_complex_index(
                root,
                {
                    "protein_gro": "protein.gro",
                    "ligand_gro": "ligand.gro",
                    "system_gro": "system.gro",
                    "output_index": "index.ndx",
                },
            )
            index = (root / "index.ndx").read_text(encoding="utf-8")
            self.assertIn("[ Protein_Lig ]\n1 2 3", index)
            self.assertIn("[ Water_and_Ions ]\n4 5 6 7", index)
            self.assertIn("[ Backbone ]", index)
            self.assertIn("[ C-alpha ]", index)
            self.assertIn("[ Water ]\n4 5 6 7", index)


if __name__ == "__main__":
    unittest.main()
