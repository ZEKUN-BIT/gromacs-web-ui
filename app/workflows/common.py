"""Command and preparation-step builders shared by simulation and analysis workflows."""

from __future__ import annotations

import shlex
from pathlib import Path

from ..forcefields import (
    KNOWN_WATER_MENU_SELECTIONS,
    PDB2GMX_CLI_WATERS,
    installed_water_model_selection,
    local_water_model_selection,
    normalize_force_field_name,
)
from ..mdp import format_mdp_number
from ..steps import Step, internal_step
from ..workflow_parameters import bool_param, int_param, maxwarn_args, output_name_param, str_param

ALLOWED_SUBCOMMANDS = {
    "pdb2gmx",
    "editconf",
    "solvate",
    "grompp",
    "genion",
    "mdrun",
    "energy",
    "rms",
    "rmsf",
    "gyrate",
    "sasa",
    "mindist",
    "dssp",
    "hbond",
    "hbond-legacy",
    "distance",
    "pairdist",
    "select",
    "trjconv",
    "make_ndx",
    "genrestr",
    "covar",
    "anaeig",
    "sham",
}


def as_gmx(gmx_bin: str, args: list[str]) -> list[str]:
    return [gmx_bin, *args]


def solvent_template(params: dict) -> str:
    explicit = str_param(params, "solvent_template", "auto")
    if explicit and explicit != "auto":
        return explicit
    water = str_param(params, "water_model", "opc").lower()
    if water in {"opc", "tip4p", "tip4p-ew", "tip4pew", "tip4p-ice"}:
        return "tip4p.gro"
    return "spc216.gro"


def mdrun_args(
    deffnm: str,
    ntmpi: int,
    ntomp: int,
    gpu: bool,
    verbose: bool = False,
    cpi: bool = False,
    pin: str = "auto",
) -> list[str]:
    args = ["mdrun"]
    if verbose:
        args.append("-v")
    args.extend(["-deffnm", deffnm, "-ntmpi", str(max(1, ntmpi)), "-ntomp", str(max(1, ntomp))])
    if cpi:
        args.extend(["-cpi", f"{deffnm}.cpt"])
    if gpu:
        args.extend(["-nb", "gpu"])
    pin = str(pin or "auto").strip().lower()
    if pin not in {"auto", "on", "off"}:
        raise ValueError("pin must be auto, on, or off")
    if pin != "auto":
        args.extend(["-pin", pin])
    return args


def structure_preflight_step(source: str, output: str = "structure-preflight.txt") -> Step:
    return internal_step(
        "Inspect input structure",
        "structure_preflight",
        {"source": source, "output": output},
        outputs=[output],
    )


def thermodynamic_gate_steps(gmx_bin: str, edr: str, prefix: str, source_mdp: str) -> list[Step]:
    xvg = f"{prefix}-thermodynamics.xvg"
    report = f"quality-{prefix}-thermodynamics.txt"
    return [
        Step(
            f"Extract {prefix.upper()} thermodynamics",
            as_gmx(gmx_bin, ["energy", "-f", edr, "-o", xvg]),
            stdin_text=selection_text("Temperature", "Pressure", "Density", "Volume", "Potential", "0"),
            outputs=[xvg],
        ),
        internal_step(
            f"Validate {prefix.upper()} thermodynamics",
            "validate_thermodynamics",
            {"xvg": xvg, "output": report, "source_mdp": source_mdp, "strict": True},
            outputs=[report],
        ),
    ]


def restraint_release_steps(
    gmx_bin: str, params: dict, npt_mdp: str, index_args: list[str], ntmpi: int, ntomp: int, gpu: bool, verbose: bool
) -> list[Step]:
    if not bool_param(params, "release_restraints", True):
        return []
    return [
        internal_step(
            "Configure unrestrained NPT",
            "prepare_unrestrained_npt_mdp",
            {"source_mdp": npt_mdp, "output_mdp": "npt-unrestrained.mdp"},
            outputs=["npt-unrestrained.mdp"],
        ),
        Step(
            "Prepare unrestrained NPT",
            as_gmx(
                gmx_bin,
                [
                    "grompp",
                    "-f",
                    "npt-unrestrained.mdp",
                    "-c",
                    "npt.gro",
                    "-t",
                    "npt.cpt",
                    "-p",
                    "topol.top",
                    *index_args,
                    "-o",
                    "npt-free.tpr",
                    *maybe_maxwarn(params),
                ],
            ),
            outputs=["npt-free.tpr"],
        ),
        Step(
            "Run unrestrained NPT",
            as_gmx(gmx_bin, mdrun_args("npt-free", ntmpi, ntomp, gpu, verbose, pin=str_param(params, "pin", "auto"))),
            outputs=["npt-free.gro", "npt-free.cpt", "npt-free.edr", "npt-free.log"],
        ),
        internal_step(
            "Check unrestrained NPT",
            "validate_stage_log",
            {"stage": "npt-free", "log_file": "npt-free.log", "output_report": "quality-npt-free.txt"},
            outputs=["quality-npt-free.txt"],
        ),
    ]


def production_steps(
    gmx_bin: str,
    params: dict,
    md_mdp: str,
    topology: str,
    index_args: list[str],
    ntmpi: int,
    ntomp: int,
    gpu: bool,
    verbose: bool,
    equil_prefix: str = "npt",
) -> list[Step]:
    replicas = int_param(params, "replicas", 1)
    if replicas < 1 or replicas > 16:
        raise ValueError("replicas must be between 1 and 16")
    base = output_name_param(params, "production_deffnm", "md", "Production output prefix")
    steps: list[Step] = []
    outputs: list[str] = []
    structural_outputs: list[str] = []
    rmsd_center_group = "Protein_Lig" if params.get("workflow") == "protein_ligand_md" else "Protein"
    for replica in range(1, replicas + 1):
        deffnm = base if replicas == 1 else f"{base}_r{replica:02d}"
        replica_mdp = md_mdp if replicas == 1 else f"md-replica-{replica:02d}.mdp"
        if replicas > 1:
            steps.append(
                internal_step(
                    f"Configure replica {replica}",
                    "prepare_replica_mdp",
                    {"source_mdp": md_mdp, "output_mdp": replica_mdp, "seed": -1},
                    outputs=[replica_mdp],
                )
            )
        grompp_args = ["grompp", "-f", replica_mdp, "-c", f"{equil_prefix}.gro"]
        if replicas == 1:
            grompp_args.extend(["-t", f"{equil_prefix}.cpt"])
        grompp_args.extend(["-p", topology, *index_args, "-o", f"{deffnm}.tpr", *maybe_maxwarn(params)])
        steps.extend(
            [
                Step(
                    f"Prepare production{f' replica {replica}' if replicas > 1 else ''}",
                    as_gmx(gmx_bin, grompp_args),
                    outputs=[f"{deffnm}.tpr"],
                ),
                Step(
                    f"Production MD{f' replica {replica}' if replicas > 1 else ''}",
                    as_gmx(
                        gmx_bin,
                        mdrun_args(
                            deffnm,
                            ntmpi,
                            ntomp,
                            gpu,
                            verbose,
                            cpi=True,
                            pin=str_param(params, "pin", "auto"),
                        ),
                    ),
                    outputs=[f"{deffnm}.xtc", f"{deffnm}.edr", f"{deffnm}.log"],
                ),
                Step(
                    f"Extract replica {replica} observables",
                    as_gmx(gmx_bin, ["energy", "-f", f"{deffnm}.edr", "-o", f"{deffnm}-observables.xvg"]),
                    stdin_text=selection_text("Temperature", "Pressure", "Density", "Potential", "0"),
                    outputs=[f"{deffnm}-observables.xvg"],
                ),
                Step(
                    f"Unwrap replica {replica} trajectory for backbone RMSD",
                    as_gmx(
                        gmx_bin,
                        [
                            "trjconv",
                            "-s",
                            f"{deffnm}.tpr",
                            "-f",
                            f"{deffnm}.xtc",
                            "-o",
                            f"{deffnm}-nojump.xtc",
                            "-pbc",
                            "nojump",
                            *index_args,
                        ],
                    ),
                    stdin_text=selection_text("System"),
                    outputs=[f"{deffnm}-nojump.xtc"],
                ),
                Step(
                    f"Cluster and center replica {replica} trajectory for backbone RMSD",
                    as_gmx(
                        gmx_bin,
                        [
                            "trjconv",
                            "-s",
                            f"{deffnm}.tpr",
                            "-f",
                            f"{deffnm}-nojump.xtc",
                            "-o",
                            f"{deffnm}-rmsd-whole.xtc",
                            "-pbc",
                            "cluster",
                            "-center",
                            *index_args,
                        ],
                    ),
                    stdin_text=selection_text(rmsd_center_group, rmsd_center_group, "System"),
                    outputs=[f"{deffnm}-rmsd-whole.xtc"],
                ),
                internal_step(
                    f"Remove replica {replica} unwrapped trajectory",
                    "remove_files",
                    {"paths": [f"{deffnm}-nojump.xtc"]},
                ),
                Step(
                    f"Compute replica {replica} backbone RMSD",
                    as_gmx(
                        gmx_bin,
                        [
                            "rms",
                            "-s",
                            f"{deffnm}.tpr",
                            "-f",
                            f"{deffnm}-rmsd-whole.xtc",
                            "-o",
                            f"{deffnm}-backbone-rmsd.xvg",
                            "-tu",
                            "ns",
                            "-fit",
                            "rot+trans",
                            *index_args,
                        ],
                    ),
                    stdin_text=selection_text("Backbone", "Backbone"),
                    outputs=[f"{deffnm}-backbone-rmsd.xvg"],
                ),
                internal_step(
                    f"Remove replica {replica} RMSD trajectory",
                    "remove_files",
                    {"paths": [f"{deffnm}-rmsd-whole.xtc"]},
                ),
            ]
        )
        outputs.append(f"{deffnm}-observables.xvg")
        structural_outputs.append(f"{deffnm}-backbone-rmsd.xvg")
    steps.append(
        internal_step(
            "Summarize sampling uncertainty",
            "summarize_replicas",
            {"inputs": outputs, "output": "sampling-summary.json"},
            outputs=["sampling-summary.json"],
        )
    )
    steps.append(
        internal_step(
            "Summarize structural convergence",
            "summarize_replicas",
            {"inputs": structural_outputs, "output": "structural-convergence.json"},
            outputs=["structural-convergence.json"],
        )
    )
    return steps


def maybe_maxwarn(params: dict) -> list[str]:
    return maxwarn_args(int_param(params, "maxwarn", 0))


def selection_text(*groups: str) -> str:
    return "".join(f"{group}\n" for group in groups if group)


def should_clean_pdb_for_pdb2gmx(params: dict, source: str) -> bool:
    return bool_param(params, "clean_pdb", False) and source.lower().endswith(".pdb")


def clean_pdb_step(source: str, output: str, remove_ligands: bool = True, ligand_residue: str = "") -> Step:
    return internal_step(
        "Clean structure for pdb2gmx",
        "clean_pdb_for_pdb2gmx",
        {
            "source_pdb": source,
            "output_pdb": output,
            "remove_waters": True,
            "remove_ions": True,
            "remove_ligands": remove_ligands,
            "ligand_residue": ligand_residue,
        },
        outputs=[output, "pdb2gmx-clean-report.txt"],
    )


def split_complex_pdb_step(source: str, protein_output: str, ligand_output: str, ligand_residue: str = "") -> Step:
    return internal_step(
        "Split complex PDB",
        "split_complex_pdb",
        {
            "source_pdb": source,
            "protein_output_pdb": protein_output,
            "ligand_output_pdb": ligand_output,
            "ligand_residue": ligand_residue,
            "remove_waters": True,
            "remove_ions": True,
        },
        outputs=[protein_output, ligand_output, "complex-pdb-split-report.txt"],
    )


def split_complex_pdb_multi_step(source: str, protein_output: str, ligand_outputs: list[dict], title: str = "Split complex PDB") -> Step:
    return internal_step(
        title,
        "split_complex_pdb",
        {
            "source_pdb": source,
            "protein_output_pdb": protein_output,
            "ligand_outputs": ligand_outputs,
            "remove_waters": True,
            "remove_ions": True,
        },
        outputs=[protein_output, *(str(entry["output_pdb"]) for entry in ligand_outputs), "complex-pdb-split-report.txt"],
    )


def pdb2gmx_source_steps(params: dict, source: str, output: str, remove_ligands: bool = True) -> tuple[str, list[Step]]:
    if not should_clean_pdb_for_pdb2gmx(params, source):
        return source, []
    return output, [clean_pdb_step(source, output, remove_ligands=remove_ligands)]


def pdb2gmx_water_choice(params: dict, gmx_bin: str, force_field: str, force_field_base: Path | None) -> tuple[str, str | None]:
    water_model = str_param(params, "water_model", "opc").lower()
    if water_model in PDB2GMX_CLI_WATERS:
        return water_model, None

    selection = local_water_model_selection(force_field_base, force_field, water_model) if force_field_base else None
    selection = selection or installed_water_model_selection(gmx_bin, force_field, water_model)
    selection = selection or KNOWN_WATER_MENU_SELECTIONS.get(force_field.lower(), {}).get(water_model)
    if not selection:
        raise ValueError(f"Water model {water_model!r} is not listed in {force_field}.ff/watermodels.dat")
    return "select", selection_text(selection)


def pdb2gmx_step(
    title: str,
    gmx_bin: str,
    params: dict,
    source: str,
    output: str,
    topology: str = "topol.top",
    force_field_base: Path | None = None,
    outputs: list[str] | None = None,
) -> Step:
    force_field = normalize_force_field_name(str_param(params, "force_field", "amber19sb"))
    water_arg, stdin_text = pdb2gmx_water_choice(params, gmx_bin, force_field, force_field_base)
    args = [
        "pdb2gmx",
        "-f",
        source,
        "-o",
        output,
        "-p",
        topology,
        "-ff",
        force_field,
        "-water",
        water_arg,
    ]
    if bool_param(params, "ignh", False):
        args.append("-ignh")
    return Step(title, as_gmx(gmx_bin, args), stdin_text=stdin_text, outputs=outputs or [output, topology])


def ion_args(gmx_bin: str, params: dict, tpr: str, output: str, topology: str = "topol.top") -> list[str]:
    args = [
        "genion",
        "-s",
        tpr,
        "-o",
        output,
        "-p",
        topology,
        "-pname",
        str_param(params, "positive_ion", "NA"),
        "-nname",
        str_param(params, "negative_ion", "CL"),
    ]
    ion_conc = str_param(params, "ion_concentration", "")
    if ion_conc:
        try:
            concentration = float(ion_conc)
        except ValueError:
            raise ValueError("ion_concentration must be a number between 0 and 5 mol/L")
        if not 0 <= concentration <= 5:
            raise ValueError("ion_concentration must be between 0 and 5 mol/L")
        args.extend(["-conc", format_mdp_number(concentration)])
    if bool_param(params, "neutral", True):
        args.append("-neutral")
    return as_gmx(gmx_bin, args)


def index_arg(index_file: str) -> list[str]:
    return ["-n", index_file] if index_file else []


def parse_custom_command(command: str, gmx_bin: str) -> list[str]:
    if not command:
        raise ValueError("Custom command is empty.")
    args = shlex.split(command)
    first = Path(args[0]).name
    configured = Path(gmx_bin).name
    if first not in {"gmx", configured} and args[0] != gmx_bin:
        raise ValueError("Custom commands must start with the configured gmx binary.")
    if len(args) < 2:
        raise ValueError("Custom command must include a gmx subcommand.")
    if args[1] not in ALLOWED_SUBCOMMANDS:
        raise ValueError(f"Unsupported gmx subcommand: {args[1]}")
    return [gmx_bin if args[0] in {"gmx", configured} else args[0], *args[1:]]
