"""Real preparation smoke checks using locally generated, synthetic molecules.

These exercise the application builders and internal preparation operations,
then stop after compiling NPT. Twenty NVT steps verify restart compatibility;
they are not equilibration or scientific validation of either fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_gromacs_integration import real_gmx as real_gmx

from app.analysis.research import read_index_group
from app.gromacs import build_steps, execute_internal_step
from app.mdp import patch_mdp_key, read_mdp_values, write_missing_mdp_files
from app.structure import read_gro

pytestmark = pytest.mark.gromacs_integration
BASE_DIR = Path(__file__).resolve().parents[1]


def write_peptide(workdir: Path) -> None:
    # Ideal heavy-atom geometry for Gly-Gly-Gly, generated locally from peptide
    # internal coordinates. pdb2gmx supplies terminal groups and hydrogens.
    atoms = [
        (1, "N", (1.338, 0.000, -0.572)),
        (1, "CA", (0.000, 0.000, 0.000)),
        (1, "C", (0.000, 0.000, 1.517)),
        (1, "O", (-0.788, 0.713, 2.142)),
        (2, "N", (0.884, -0.796, 2.114)),
        (2, "CA", (0.987, -0.859, 3.563)),
        (2, "C", (1.379, 0.464, 4.192)),
        (2, "O", (0.826, 0.855, 5.223)),
        (3, "N", (2.331, 1.159, 3.575)),
        (3, "CA", (2.771, 2.451, 4.077)),
        (3, "C", (1.669, 3.494, 4.088)),
        (3, "O", (0.588, 3.266, 3.541)),
        (3, "OXT", (1.855, 4.596, 4.647)),
    ]
    rows = [
        f"ATOM  {serial:5d} {name:^4} GLY A{residue:4d}    {xyz[0]:8.3f}{xyz[1]:8.3f}{xyz[2]:8.3f}  1.00  0.00          {name[0]:>2}  "
        for serial, (residue, name, xyz) in enumerate(atoms, 1)
    ]
    (workdir / "peptide.pdb").write_text("\n".join([*rows, "TER", "END", ""]), encoding="utf-8")


def write_prepared_ligand(workdir: Path) -> None:
    # Neutral, three-site united-atom propane-like test molecule. The explicit
    # synthetic bonded/nonbonded parameters test integration, not chemistry.
    coordinates = [(1.000, 0.000, 0.300), (1.154, 0.000, 0.300), (1.212, 0.143, 0.300)]
    rows = [f"{1:5d}{'LIG':<5}{f'C{index}':>5}{index:5d}{x:8.3f}{y:8.3f}{z:8.3f}" for index, (x, y, z) in enumerate(coordinates, 1)]
    (workdir / "prepared.gro").write_text("\n".join(["Synthetic prepared ligand", "3", *rows, "3.5 3.5 3.5", ""]), encoding="utf-8")
    (workdir / "prepared.itp").write_text(
        "; Synthetic integration fixture, not a validated ligand force field\n"
        "[ atomtypes ]\nTEST_C3 6 15.035 0.0 A 0.39 0.50\nTEST_C2 6 14.027 0.0 A 0.39 0.50\n"
        "[ moleculetype ]\nLIG 3\n[ atoms ]\n"
        "1 TEST_C3 1 LIG C1 1 0.0 15.035\n2 TEST_C2 1 LIG C2 2 0.0 14.027\n3 TEST_C3 1 LIG C3 3 0.0 15.035\n"
        "[ bonds ]\n1 2 1 0.154 250000\n2 3 1 0.154 250000\n[ angles ]\n1 2 3 1 112 400\n",
        encoding="utf-8",
    )


def prepare_short_protocol(workdir: Path, workflow: str) -> None:
    write_missing_mdp_files(workdir, BASE_DIR, workflow)
    for name in ("minim.mdp", "nvt.mdp", "npt.mdp"):
        path = workdir / name
        text = patch_mdp_key(path.read_text(encoding="utf-8"), "nsteps", "2000" if name == "minim.mdp" else "20")
        if name != "minim.mdp":
            for key, value in {
                "dt": "0.001",
                "nstenergy": "5",
                "nstlog": "5",
                "nstxout-compressed": "5",
                "gen_seed": "20261028",
                "ld_seed": "20261028",
            }.items():
                text = patch_mdp_key(text, key, value)
        path.write_text(text, encoding="utf-8")


def run_preparation(workdir: Path, steps: list) -> list[dict]:
    records = []
    for index, step in enumerate(steps, 1):
        if step.kind == "internal":
            execute_internal_step(workdir, step)
        else:
            args = list(step.args)
            if args[1] == "mdrun":
                args.extend(["-nb", "cpu", "-pme", "cpu"])
            elif args[1] == "genion":
                args.extend(["-seed", "20261028"])
            assert "-maxwarn" not in args
            result = subprocess.run(
                args,
                cwd=workdir,
                input=step.stdin_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={
                    **os.environ,
                    "OMP_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "GMX_DISABLE_GPU_DETECTION": "1",
                    "CUDA_VISIBLE_DEVICES": "",
                    "GMX_MAXBACKUP": "-1",
                },
                timeout=120,
                check=False,
            )
            (workdir / f"preparation-{index:02d}.log").write_text(result.stdout, encoding="utf-8")
            assert result.returncode == 0, f"{step.title}\n{result.stdout}"
        for output in step.outputs:
            assert (workdir / output).stat().st_size > 0, f"{step.title}: missing {output}"
        records.append({"title": step.title, "operation": step.operation, "args": step.args, "outputs": step.outputs})
        (workdir / "preparation-steps.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        if "npt.tpr" in step.outputs:
            return records
    pytest.fail("Workflow did not compile NPT input")


def test_real_ligand_index_excludes_hydrogens_with_stable_name(real_gmx, tmp_path):
    write_prepared_ligand(tmp_path)
    rows = (tmp_path / "prepared.gro").read_text().splitlines()
    rows[1] = "4"
    rows.insert(-1, f"{1:5d}{'LIG':<5}{'H1':>5}{4:5d}{0.9:8.3f}{0.0:8.3f}{0.3:8.3f}")
    (tmp_path / "ligand_GMX.gro").write_text("\n".join(rows) + "\n")
    steps = build_steps(
        {
            "workflow": "protein_ligand_md",
            "gmx_bin": real_gmx,
            "protein_file": "peptide.pdb",
            "auto_split_complex_pdb": False,
            "ligand_mode": "prepared",
            "ligand_gro_file": "prepared.gro",
            "ligand_itp_file": "prepared.itp",
        },
        ["peptide.pdb", "prepared.gro", "prepared.itp"],
        BASE_DIR,
    )
    step = next(step for step in steps if step.title == "Create ligand heavy-atom index")
    from test_gromacs_integration import run_gmx

    run_gmx(step.args, tmp_path)
    selected = read_index_group(tmp_path / "index_lig.ndx", "System_&_!H*", 4)
    assert selected.tolist() == [0, 1, 2]


@pytest.mark.parametrize("workflow", ["protein_md", "protein_ligand_md"])
def test_real_protein_and_prepared_ligand_preparation(real_gmx, tmp_path, workflow):
    write_peptide(tmp_path)
    files = ["peptide.pdb"]
    params = {
        "workflow": workflow,
        "gmx_bin": real_gmx,
        "force_field": "amber99sb-ildn",
        "water_model": "tip3p",
        "structure_file": "peptide.pdb",
        "protein_file": "peptide.pdb",
        "box_type": "cubic",
        "box_distance": 1.1,
        "pre_nvt": True,
        "release_restraints": False,
        "replicas": 1,
        "ntmpi": 1,
        "ntomp": 1,
        "pin": "off",
        "gpu": False,
        "neutral": True,
        "ion_concentration": 0.05,
    }
    if workflow == "protein_ligand_md":
        write_prepared_ligand(tmp_path)
        files.extend(["prepared.gro", "prepared.itp"])
        params.update(
            auto_split_complex_pdb=False,
            ligand_mode="prepared",
            ligand_name="LIG",
            ligand_gro_file="prepared.gro",
            ligand_itp_file="prepared.itp",
            ligand_posres=True,
        )
    prepare_short_protocol(tmp_path, workflow)
    steps = build_steps(params, files, BASE_DIR)
    executed = run_preparation(tmp_path, steps)
    commands = [record["args"][1] for record in executed if record["args"][0] == real_gmx]
    assert all(command in commands for command in ("pdb2gmx", "solvate", "genion", "grompp", "mdrun"))
    assert "status=PASS" in (tmp_path / "quality-em.txt").read_text()
    assert "status=PASS" in (tmp_path / "quality-nvt.txt").read_text()
    assert all((tmp_path / name).stat().st_size > 0 for name in ("em.tpr", "em.gro", "nvt.tpr", "nvt.cpt", "npt.tpr"))
    npt = read_mdp_values((tmp_path / "npt-run.mdp").read_text())
    assert npt["continuation"] == "yes" and npt["gen_vel"] == "no"
    if workflow == "protein_ligand_md":
        topology = (tmp_path / "topol.top").read_text()
        assert '#include "ligand_GMX.itp"' in topology and '#include "posre_lig.itp"' in topology
        assert "POSRES_LIG" in read_mdp_values((tmp_path / "nvt.mdp").read_text())["define"]
        atom_count = len(read_gro(tmp_path / "complex_ions.gro")[1])
        ligand = read_index_group(tmp_path / "index.ndx", "Ligand", atom_count)
        solute = read_index_group(tmp_path / "index.ndx", "Protein_Lig", atom_count)
        assert len(ligand) == 3 and set(ligand).issubset(solute)
        assert "status=PASS" in (tmp_path / "complex-pose-check.txt").read_text()
