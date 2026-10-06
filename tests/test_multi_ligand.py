import tempfile
import unittest
from pathlib import Path

from app.files import _rewrite_uploaded_file_references, validate_uploaded_file_references
from app.gromacs import (
    Step,
    _create_complex_index,
    build_steps,
    execute_internal_step,
    unconfirmed_acpype_ligands,
)
from app.ligands import ligand_define, ligand_group, ligand_work_names, normalize_ligands
from app.main import PreviewRequest, preview


def internal(operation: str, data: dict) -> Step:
    return Step("test", ["internal"], kind="internal", operation=operation, data=data)


def write_gro(path: Path, atom_count: int, water_from: int | None = None, x_offset: float = 0.0) -> None:
    atoms = []
    names = ("N", "CA", "C")
    for index in range(1, atom_count + 1):
        is_water = water_from is not None and index >= water_from
        residue = "SOL" if is_water else "RES"
        atom = "OW" if is_water else names[(index - 1) % len(names)]
        atoms.append(f"{1:5d}{residue:<5}{atom:>5}{index:5d}{x_offset + index * 0.1:8.3f}{0.0:8.3f}{0.0:8.3f}")
    path.write_text("test\n" + f"{atom_count}\n" + "\n".join(atoms) + "\n2.0 2.0 2.0\n", encoding="utf-8")


def acpype_itp(molecule: str, atomtypes: str, atom_rows: str = "") -> str:
    return f"{atomtypes}\n[ moleculetype ]\n;name            nrexcl\n{molecule:<12}      3\n\n[ atoms ]\n{atom_rows}"


class LigandNormalizationTests(unittest.TestCase):
    def test_legacy_fields_become_a_single_ligand_with_legacy_names(self) -> None:
        params = {"ligand_name": "MOL", "ligand_count": 2, "complex_ligand_residue": "bta", "ligand_charge": -1}
        ligands = normalize_ligands(params)
        self.assertEqual(len(ligands), 1)
        ligand = ligands[0]
        self.assertEqual(ligand["key"], "lig")
        self.assertEqual(ligand["name"], "MOL")
        self.assertEqual(ligand["count"], 2)
        self.assertEqual(ligand["residue"], "BTA")
        names = ligand_work_names(ligand, len(ligands))
        self.assertEqual(names["work_gro"], "ligand_GMX.gro")
        self.assertEqual(names["work_itp"], "ligand_GMX.itp")
        self.assertEqual(names["work_posre"], "posre_lig.itp")
        self.assertEqual(names["extracted_pdb"], "ligand_from_complex.pdb")
        self.assertEqual(names["group"], "Ligand")
        self.assertEqual(names["define"], "POSRES_LIG")

    def test_explicit_ligand_list_gets_per_key_names(self) -> None:
        params = {
            "ligands": [
                {"key": "ligA", "residue": "BTA", "mode": "acpype", "charge": -1},
                {"key": "ligB", "residue": "ATP", "mode": "prepared", "count": 2},
            ]
        }
        ligands = normalize_ligands(params)
        self.assertEqual([ligand["key"] for ligand in ligands], ["ligA", "ligB"])
        names_a = ligand_work_names(ligands[0], len(ligands))
        names_b = ligand_work_names(ligands[1], len(ligands))
        self.assertEqual(names_a["work_gro"], "ligA_GMX.gro")
        self.assertEqual(names_a["work_posre"], "posre_ligA.itp")
        self.assertEqual(names_a["extracted_pdb"], "ligA_from_complex.pdb")
        self.assertEqual(names_a["group"], "Ligand_ligA")
        self.assertEqual(names_a["define"], "POSRES_LIGA")
        self.assertEqual(names_b["group"], "Ligand_ligB")
        self.assertEqual(names_b["define"], "POSRES_LIGB")
        self.assertEqual(ligand_group("ligA", 1), "Ligand")
        self.assertEqual(ligand_define("ligA"), "POSRES_LIGA")

    def test_single_explicit_ligand_keeps_the_legacy_group_name(self) -> None:
        ligands = normalize_ligands({"ligands": [{"key": "ligA", "residue": "BTA"}]})
        self.assertEqual(ligand_work_names(ligands[0], 1)["group"], "Ligand")

    def test_duplicate_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate ligand key"):
            normalize_ligands({"ligands": [{"key": "ligA", "residue": "X"}, {"key": "ligA", "residue": "Y"}]})

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "mode must be prepared or acpype"):
            normalize_ligands({"ligands": [{"key": "ligA", "residue": "X", "mode": "custom"}]})

    def test_legacy_chemistry_conflicts_still_raise_known_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "either"):
            normalize_ligands({"ligand_chemistry_file": "a.sdf", "ligand_smiles": "CC"})
        with self.assertRaisesRegex(ValueError, "sdf or .mol2"):
            normalize_ligands({"ligand_chemistry_file": "a.txt"})

    def test_multi_ligand_chemistry_conflict_names_the_ligand(self) -> None:
        with self.assertRaisesRegex(ValueError, "ligA"):
            normalize_ligands({"ligands": [{"key": "ligA", "chemistry_file": "a.sdf", "smiles": "CC"}]})


class MultiLigandInternalStepTests(unittest.TestCase):
    def test_split_complex_pdb_routes_each_residue_to_its_own_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdb = (
                "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 20.00           C\n"
                "HETATM    2  C1  BTA A 101       1.000   0.000   0.000  1.00 20.00           C\n"
                "HETATM    3  C2  BTA A 101       1.500   0.000   0.000  1.00 20.00           C\n"
                "HETATM    4  P1  ATP A 201       2.000   0.000   0.000  1.00 20.00           P\n"
                "CONECT    2    3\n"
                "END\n"
            )
            (root / "complex.pdb").write_text(pdb, encoding="utf-8")
            execute_internal_step(
                root,
                internal(
                    "split_complex_pdb",
                    {
                        "source_pdb": "complex.pdb",
                        "protein_output_pdb": "protein.pdb",
                        "ligand_outputs": [
                            {"residue": "BTA", "output_pdb": "bta.pdb"},
                            {"residue": "ATP", "output_pdb": "atp.pdb"},
                        ],
                    },
                ),
            )
            protein = (root / "protein.pdb").read_text(encoding="utf-8")
            bta = (root / "bta.pdb").read_text(encoding="utf-8")
            atp = (root / "atp.pdb").read_text(encoding="utf-8")
            self.assertIn("ALA", protein)
            self.assertNotIn("BTA", protein)
            self.assertNotIn("ATP", protein)
            self.assertIn("BTA", bta)
            self.assertNotIn("ATP", bta)
            self.assertIn("ATP", atp)
            self.assertNotIn("BTA", atp)
            self.assertIn("CONECT", bta)  # CONECT follows the serials into the ligand file
            report = (root / "complex-pdb-split-report.txt").read_text(encoding="utf-8")
            self.assertIn("ligand_BTA_atoms: 2", report)
            self.assertIn("ligand_ATP_atoms: 1", report)

    def test_split_complex_pdb_rejects_unmapped_hetero_residues(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdb = (
                "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 20.00           C\n"
                "HETATM    2  C1  BTA A 101       1.000   0.000   0.000  1.00 20.00           C\n"
                "HETATM    3  C2  COF A 201       2.000   0.000   0.000  1.00 20.00           C\n"
            )
            (root / "complex.pdb").write_text(pdb, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unmapped HETATM residues.*COF"):
                execute_internal_step(
                    root,
                    internal(
                        "split_complex_pdb",
                        {
                            "source_pdb": "complex.pdb",
                            "protein_output_pdb": "protein.pdb",
                            "ligand_outputs": [{"residue": "BTA", "output_pdb": "bta.pdb"}],
                        },
                    ),
                )

    def test_split_complex_pdb_reports_missing_residue_atoms(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdb = "ATOM      1  CA  ALA A   1       0.000   0.000   0.000  1.00 20.00           C\n"
            (root / "complex.pdb").write_text(pdb, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "No ligand atoms found for residue BTA"):
                execute_internal_step(
                    root,
                    internal(
                        "split_complex_pdb",
                        {
                            "source_pdb": "complex.pdb",
                            "protein_output_pdb": "protein.pdb",
                            "ligand_outputs": [{"residue": "BTA", "output_pdb": "bta.pdb"}],
                        },
                    ),
                )

    def test_merge_ligand_atomtypes_deduplicates_and_strips_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ligA_GMX.itp").write_text(
                acpype_itp(
                    "LIGA",
                    "[ atomtypes ]\n  c3    c3            0.00000  0.00000   A       3.39967e-01  4.57730e-01\n  ca    ca            0.00000  0.00000   A       3.39967e-01  3.59824e-01",
                    "    1    c3    1  LIGA   C01    1   0.5\n",
                ),
                encoding="utf-8",
            )
            (root / "ligB_GMX.itp").write_text(
                acpype_itp(
                    "LIGB",
                    "[ atomtypes ]\n  c3    c3            0.00000  0.00000   A       3.39967e-01  4.57730e-01\n  n     n             0.00000  0.00000   A       3.25000e-01  7.11280e-01",
                    "    1     n    1  LIGB   N01    1   -0.5\n",
                ),
                encoding="utf-8",
            )
            execute_internal_step(
                root, internal("merge_ligand_atomtypes", {"itps": ["ligA_GMX.itp", "ligB_GMX.itp"], "report": "merge.txt"})
            )
            merged = (root / "ligA_GMX.itp").read_text(encoding="utf-8")
            stripped = (root / "ligB_GMX.itp").read_text(encoding="utf-8")
            self.assertEqual(merged.count("[ atomtypes ]"), 1)
            self.assertIn("ca    ca", merged)
            self.assertIn("n     n", merged)
            self.assertNotIn("ca    ca", stripped)
            self.assertNotIn("[ atomtypes ]", stripped)
            self.assertIn("LIGA", merged)
            self.assertIn("LIGB", stripped)
            report = (root / "merge.txt").read_text(encoding="utf-8")
            self.assertIn("merged_atomtypes=3", report)
            self.assertIn("status=PASS", report)

    def test_merge_ligand_atomtypes_rejects_conflicting_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.itp").write_text(
                acpype_itp("A", "[ atomtypes ]\n  c3    c3            0.00000  0.00000   A       3.39967e-01  4.57730e-01"),
                encoding="utf-8",
            )
            (root / "b.itp").write_text(
                acpype_itp("B", "[ atomtypes ]\n  c3    c3            0.10000  0.00000   A       1.00000e-01  2.00000e-01"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Conflicting atomtypes definition for 'c3'"):
                execute_internal_step(root, internal("merge_ligand_atomtypes", {"itps": ["a.itp", "b.itp"], "report": "r.txt"}))

    def test_merge_ligand_atomtypes_skips_when_no_sections_exist(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.itp").write_text("[ moleculetype ]\nPLN 3\n[ atoms ]\n1 x 1 PLN A 1 0.0\n", encoding="utf-8")
            original = (root / "a.itp").read_text(encoding="utf-8")
            execute_internal_step(root, internal("merge_ligand_atomtypes", {"itps": ["a.itp"], "report": "r.txt"}))
            self.assertEqual((root / "a.itp").read_text(encoding="utf-8"), original)
            self.assertIn("status=SKIP", (root / "r.txt").read_text(encoding="utf-8"))

    def test_merge_gro_concatenates_ligands_in_order_and_checks_poses(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_gro(root / "protein.gro", 3, x_offset=0.0)
            write_gro(root / "ligA.gro", 2, x_offset=0.5)
            write_gro(root / "ligB.gro", 1, x_offset=1.0)
            execute_internal_step(
                root,
                internal(
                    "merge_gro",
                    {"protein_gro": "protein.gro", "ligand_gros": ["ligA.gro", "ligB.gro"], "output_gro": "complex.gro"},
                ),
            )
            lines = (root / "complex.gro").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[1].strip(), "6")
            report = (root / "complex-pose-check.txt").read_text(encoding="utf-8")
            self.assertIn("ligand_atoms[ligA.gro]=2", report)
            self.assertIn("ligand_atoms[ligB.gro]=1", report)
            self.assertIn("minimum_distance_nm[ligA.gro|ligB.gro]", report)
            self.assertIn("status=PASS", report)

    def test_merge_gro_rejects_ligand_ligand_clash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "protein.gro").write_text("t\n1\n    1RES   OW    1   0.000   0.000   0.000\n1.0 1.0 1.0\n", encoding="utf-8")
            ligand = "t\n1\n    1RES   OW    1   0.000   0.000   0.000\n1.0 1.0 1.0\n"
            (root / "ligA.gro").write_text(ligand, encoding="utf-8")
            (root / "ligB.gro").write_text(ligand, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "clash"):
                execute_internal_step(
                    root,
                    internal(
                        "merge_gro",
                        {"protein_gro": "protein.gro", "ligand_gros": ["ligA.gro", "ligB.gro"], "output_gro": "complex.gro"},
                    ),
                )

    def test_patch_topology_inserts_every_ligand_with_its_own_define(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "topol.top").write_text(
                '#include "amber19sb.ff/forcefield.itp"\n\n[ system ]\nComplex\n\n[ molecules ]\nProtein_chain_A     1\n',
                encoding="utf-8",
            )
            (root / "ligA_GMX.itp").write_text(acpype_itp("LIGA", ""), encoding="utf-8")
            (root / "ligB_GMX.itp").write_text(acpype_itp("LIGB", ""), encoding="utf-8")
            execute_internal_step(
                root,
                internal(
                    "patch_topology",
                    {
                        "topology": "topol.top",
                        "ligands": [
                            {"itp": "ligA_GMX.itp", "posre": "posre_ligA.itp", "name": "ligA", "count": 1, "define": "POSRES_LIGA"},
                            {"itp": "ligB_GMX.itp", "posre": "posre_ligB.itp", "name": "ligB", "count": 2, "define": "POSRES_LIGB"},
                        ],
                    },
                ),
            )
            text = (root / "topol.top").read_text(encoding="utf-8")
            self.assertIn('#include "ligA_GMX.itp"', text)
            self.assertIn('#include "ligB_GMX.itp"', text)
            self.assertIn("#ifdef POSRES_LIGA", text)
            self.assertIn('#include "posre_ligA.itp"', text)
            self.assertIn("#ifdef POSRES_LIGB", text)
            self.assertIn('#include "posre_ligB.itp"', text)
            molecules = text.split("[ molecules ]", 1)[1]
            self.assertIn("LIGA", molecules)
            self.assertIn("LIGB", molecules)
            self.assertRegex(molecules, r"LIGA\s+1")
            self.assertRegex(molecules, r"LIGB\s+2")

    def test_patch_topology_uses_real_moleculetype_names_from_itp(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "topol.top").write_text("[ molecules ]\n", encoding="utf-8")
            (root / "ligX_GMX.itp").write_text(acpype_itp("MOLX", ""), encoding="utf-8")
            execute_internal_step(
                root,
                internal(
                    "patch_topology",
                    {
                        "topology": "topol.top",
                        "ligands": [{"itp": "ligX_GMX.itp", "posre": "", "name": "wrong", "count": 1, "define": "POSRES_LIGX"}],
                    },
                ),
            )
            self.assertIn("MOLX", (root / "topol.top").read_text(encoding="utf-8"))

    def test_create_complex_index_builds_per_ligand_groups(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_gro(root / "protein.gro", 2)
            write_gro(root / "ligA.gro", 1)
            write_gro(root / "ligB.gro", 1)
            write_gro(root / "system.gro", 8, water_from=5)
            _create_complex_index(
                root,
                {
                    "protein_gro": "protein.gro",
                    "ligand_gros": [{"gro": "ligA.gro", "group": "Ligand_ligA"}, {"gro": "ligB.gro", "group": "Ligand_ligB"}],
                    "system_gro": "system.gro",
                    "output_index": "index.ndx",
                },
            )
            index = (root / "index.ndx").read_text(encoding="utf-8")
            self.assertIn("[ Ligand_ligA ]\n3", index)
            self.assertIn("[ Ligand_ligB ]\n4", index)
            self.assertIn("[ Protein_Lig ]\n1 2 3 4", index)
            self.assertIn("[ Water_and_Ions ]\n5 6 7 8", index)
            self.assertIn("[ Backbone ]", index)

    def test_patch_equilibration_defines_rewrites_workdir_mdps(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "nvt.mdp").write_text("define = -DPOSRES -DPOSRES_LIG ; comment\nnsteps = 10\n", encoding="utf-8")
            (root / "npt.mdp").write_text("nsteps = 10\n", encoding="utf-8")
            execute_internal_step(
                root,
                internal(
                    "patch_equilibration_defines",
                    {"mdps": ["nvt.mdp", "npt.mdp"], "defines": ["POSRES", "POSRES_LIGA", "POSRES_LIGB"]},
                ),
            )
            self.assertRegex((root / "nvt.mdp").read_text(encoding="utf-8"), r"^define\s+= -DPOSRES -DPOSRES_LIGA -DPOSRES_LIGB")
            self.assertRegex((root / "npt.mdp").read_text(encoding="utf-8"), r"define\s+= -DPOSRES -DPOSRES_LIGA -DPOSRES_LIGB")


class MultiLigandWorkflowTests(unittest.TestCase):
    MULTI_PARAMS = {
        "workflow": "protein_ligand_md",
        "protein_file": "complex.pdb",
        "water_model": "opc",
        "replicas": 1,
        "ligands": [
            {"key": "ligA", "residue": "BTA", "mode": "acpype", "charge": -1, "charge_confirmed": True, "smiles": "CC(=O)[O-]"},
            {
                "key": "ligB",
                "residue": "ATP",
                "mode": "prepared",
                "gro_file": "atp.gro",
                "itp_file": "atp.itp",
                "count": 2,
                "charge_confirmed": True,
            },
        ],
    }

    def test_multi_ligand_workflow_builds_the_full_chain(self) -> None:
        steps = build_steps(self.MULTI_PARAMS, ["complex.pdb", "atp.gro", "atp.itp"])
        titles = [step.title for step in steps]
        self.assertIn("Merge ligand itp atomtypes", titles)
        self.assertIn("Parameterize ligand ligA with ACPYPE", titles)
        self.assertIn("Stage prepared ligand ligB files", titles)
        self.assertIn("Create ligand ligA heavy-atom index", titles)
        self.assertIn("Create ligand ligB heavy-atom index", titles)
        self.assertIn("Compute Ligand_ligA RMSD relative to protein", titles)
        self.assertIn("Compute Ligand_ligB RMSD relative to protein", titles)
        self.assertIn("Compute protein-Ligand_ligA hydrogen bonds", titles)
        self.assertIn("Compute protein-Ligand_ligB hydrogen bonds", titles)
        ligand_rmsd = [step for step in steps if "RMSD relative to protein" in step.title]
        self.assertTrue(all(step.args[step.args.index("-s") + 1] == "md.tpr" for step in ligand_rmsd))
        self.assertTrue(all("md-complex-reference.gro" not in step.args for step in ligand_rmsd))

        split = next(step for step in steps if step.operation == "split_complex_pdb")
        self.assertEqual(
            split.data["ligand_outputs"],
            [{"residue": "BTA", "output_pdb": "ligA_from_complex.pdb"}, {"residue": "ATP", "output_pdb": "ligB_from_complex.pdb"}],
        )
        atomtypes = next(step for step in steps if step.operation == "merge_ligand_atomtypes")
        self.assertEqual(atomtypes.data["itps"], ["ligA_GMX.itp", "ligB_GMX.itp"])
        patch = next(step for step in steps if step.operation == "patch_topology")
        self.assertEqual([entry["itp"] for entry in patch.data["ligands"]], ["ligA_GMX.itp", "ligB_GMX.itp"])
        self.assertEqual([entry["define"] for entry in patch.data["ligands"]], ["POSRES_LIGA", "POSRES_LIGB"])
        self.assertEqual([entry["count"] for entry in patch.data["ligands"]], [1, 2])
        defines = next(step for step in steps if step.operation == "patch_equilibration_defines")
        self.assertEqual(defines.data["defines"], ["POSRES", "POSRES_LIGA", "POSRES_LIGB"])
        index = next(step for step in steps if step.operation == "create_complex_index")
        self.assertEqual(
            index.data["ligand_gros"],
            [{"gro": "ligA_GMX.gro", "group": "Ligand_ligA"}, {"gro": "ligB_GMX.gro", "group": "Ligand_ligB"}],
        )
        merge = next(step for step in steps if step.operation == "merge_gro")
        self.assertEqual(merge.data["ligand_gros"], ["ligA_GMX.gro", "ligB_GMX.gro"])

    def test_multi_ligand_split_requires_residue_names(self) -> None:
        params = {
            **self.MULTI_PARAMS,
            "ligands": [
                {"key": "ligA", "residue": "BTA", "gro_file": "a.gro", "itp_file": "a.itp"},
                {"key": "ligB", "residue": "", "gro_file": "b.gro", "itp_file": "b.itp"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "PDB residue name"):
            build_steps(params, ["complex.pdb", "a.gro", "a.itp", "b.gro", "b.itp"])

    def test_multi_ligand_without_split_uses_prepared_files(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "protein.gro",
            "auto_split_complex_pdb": False,
            "ligands": [
                {"key": "ligA", "mode": "prepared", "gro_file": "a.gro", "itp_file": "a.itp"},
                {"key": "ligB", "mode": "prepared", "gro_file": "b.gro", "itp_file": "b.itp", "posres": False},
            ],
        }
        steps = build_steps(params, ["protein.gro", "a.gro", "a.itp", "b.gro", "b.itp"])
        titles = [step.title for step in steps]
        self.assertNotIn("Split complex PDB", titles)
        self.assertIn("Stage prepared ligand ligA files", titles)
        self.assertIn("Stage prepared ligand ligB files", titles)
        self.assertIn("Create ligand ligA heavy-atom index", titles)
        self.assertNotIn("Create ligand ligB heavy-atom index", titles)
        defines = next(step for step in steps if step.operation == "patch_equilibration_defines")
        self.assertEqual(defines.data["defines"], ["POSRES", "POSRES_LIGA"])

    def test_ligand_without_any_source_is_rejected(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "protein.gro",
            "auto_split_complex_pdb": False,
            "ligands": [{"key": "ligA", "mode": "prepared"}],
        }
        with self.assertRaisesRegex(ValueError, "Prepared ligand mode requires"):
            build_steps(params, ["protein.gro"])

    def test_single_new_style_ligand_keeps_legacy_group_and_own_define(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "complex.pdb",
            "ligands": [{"key": "ligA", "residue": "BTA", "mode": "acpype", "charge_confirmed": True}],
        }
        steps = build_steps(params, ["complex.pdb"])
        hbond = next(step for step in steps if step.title == "Compute protein-ligand hydrogen bonds")
        self.assertEqual(hbond.stdin_text, "Ligand\nProtein\n")
        defines = next(step for step in steps if step.operation == "patch_equilibration_defines")
        self.assertEqual(defines.data["defines"], ["POSRES", "POSRES_LIGA"])

    def test_unconfirmed_acpype_ligands_are_reported_per_key(self) -> None:
        steps = build_steps(self.MULTI_PARAMS, ["complex.pdb", "atp.gro", "atp.itp"])
        self.assertEqual(unconfirmed_acpype_ligands(steps), [])
        params = {
            **self.MULTI_PARAMS,
            "ligands": [
                {"key": "ligA", "residue": "BTA", "mode": "acpype", "charge": -1, "charge_confirmed": False},
                {"key": "ligB", "residue": "ATP", "mode": "prepared", "gro_file": "atp.gro", "itp_file": "atp.itp"},
            ],
        }
        steps = build_steps(params, ["complex.pdb", "atp.gro", "atp.itp"])
        self.assertEqual(unconfirmed_acpype_ligands(steps), ["ligA"])

    def test_preview_accepts_the_ligands_list(self) -> None:
        request = PreviewRequest.model_validate({**self.MULTI_PARAMS, "files": ["complex.pdb", "atp.gro", "atp.itp"]})
        commands = preview(request)["commands"]
        self.assertIn("Merge ligand itp atomtypes", [item["title"] for item in commands])
        acpype = next(item for item in commands if item["title"] == "Parameterize ligand ligA with ACPYPE")
        self.assertIn("-n -1", " ".join(acpype["args"]))


class MultiLigandFileReferenceTests(unittest.TestCase):
    def test_upload_rewrite_reaches_nested_ligand_fields(self) -> None:
        params = {"ligands": [{"key": "ligA", "gro_file": "raw gro.gro", "itp_file": "raw.itp"}]}
        _rewrite_uploaded_file_references(params, {"raw gro.gro": "safe-gro.gro", "raw.itp": "safe-itp.itp"})
        self.assertEqual(params["ligands"][0]["gro_file"], "safe-gro.gro")
        self.assertEqual(params["ligands"][0]["itp_file"], "safe-itp.itp")

    def test_validation_allows_generated_extracted_pdb_names(self) -> None:
        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "complex.pdb",
            "ligands": [
                {"key": "ligA", "residue": "BTA", "mode": "acpype", "structure_file": "ligA_from_complex.pdb", "charge_confirmed": True},
                {"key": "ligB", "residue": "ATP", "mode": "prepared", "gro_file": "atp.gro", "itp_file": "atp.itp"},
            ],
        }
        validate_uploaded_file_references(params, ["complex.pdb", "atp.gro", "atp.itp"])

    def test_validation_rejects_stale_nested_references(self) -> None:
        from fastapi import HTTPException

        params = {
            "workflow": "protein_ligand_md",
            "protein_file": "complex.pdb",
            "ligands": [{"key": "ligA", "residue": "BTA", "mode": "prepared", "gro_file": "missing.gro", "itp_file": "missing.itp"}],
        }
        with self.assertRaises(HTTPException):
            validate_uploaded_file_references(params, ["complex.pdb"])


if __name__ == "__main__":
    unittest.main()
