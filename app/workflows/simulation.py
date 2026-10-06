from __future__ import annotations

from pathlib import Path

from ..gromacs import (
    Step,
    _as_gmx,
    _bool_param,
    _file_param,
    _find_first,
    _float_param,
    _format_mdp_number,
    _int_param,
    _internal_step,
    _ion_args,
    _maybe_maxwarn,
    _mdrun_args,
    _output_name_param,
    _pdb2gmx_source_steps,
    _pdb2gmx_step,
    _production_steps,
    _restraint_release_steps,
    _selection_text,
    _solvent_template,
    _split_complex_pdb_multi_step,
    _split_complex_pdb_step,
    _str_param,
    _structure_preflight_step,
    _thermodynamic_gate_steps,
    _topology,
    parse_custom_command,
)
from ..ligands import LEGACY_KEY, ligand_title, ligand_work_names, normalize_ligands
from .analysis import _analysis_steps, _classic_complex_analysis_steps, _postprocess_steps


def _ligand_parameterization_steps(ligand: dict, params: dict, uploaded_files: list[str], can_split_complex: bool) -> list[Step]:
    """Steps that produce {key}_GMX.gro / {key}_GMX.itp for one ligand."""
    obabel_bin = _str_param(params, "obabel_bin", "obabel")
    acpype_bin = _str_param(params, "acpype_bin", "acpype")
    charge_method = _str_param(params, "charge_method", "bcc")
    atom_type = _str_param(params, "atom_type", "gaff2")

    if ligand["resolved"] == "prepared":
        gro = ligand["gro_file"]
        itp = ligand["itp_file"]
        if not gro or not itp:
            raise ValueError("Prepared ligand mode requires ligand .gro and .itp files.")
        return [
            _internal_step(
                ligand_title(ligand, "Stage prepared ligand files", "Stage prepared ligand {key} files"),
                "stage_prepared_ligand",
                {
                    "ligand_gro": gro,
                    "ligand_itp": itp,
                    "gro_output": ligand["work_gro"],
                    "itp_output": ligand["work_itp"],
                },
                outputs=[ligand["work_gro"], ligand["work_itp"]],
            )
        ]

    steps: list[Step] = []
    if ligand["resolved"] == "acpype-map":
        chemistry_sdf = ligand["chemistry_sdf"]
        if ligand["smiles"]:
            steps.append(
                _internal_step(
                    ligand_title(ligand, "Stage ligand SMILES chemistry", "Stage ligand {key} SMILES chemistry"),
                    "write_ligand_smiles",
                    {"smiles": ligand["smiles"], "output": ligand["chemistry_smi"]},
                    outputs=[ligand["chemistry_smi"]],
                )
            )
            chemistry_source = ligand["chemistry_smi"]
            chemistry_format = "smi"
        else:
            chemistry_source = ligand["chemistry_file"]
            chemistry_format = Path(chemistry_source).suffix.lower().lstrip(".")
        steps.append(
            Step(
                ligand_title(ligand, "Normalize ligand chemical topology", "Normalize ligand {key} chemical topology"),
                [obabel_bin, f"-i{chemistry_format}", chemistry_source, "-osdf", "-O", chemistry_sdf],
                outputs=[chemistry_sdf],
            )
        )
        steps.append(
            _internal_step(
                ligand_title(ligand, "Map ligand chemistry onto co-folded pose", "Map ligand {key} chemistry onto co-folded pose"),
                "prepare_ligand_chemistry",
                {
                    "pose_pdb": ligand["extracted_pdb"],
                    "chemistry_sdf": chemistry_sdf,
                    "output_sdf": ligand["pose_sdf"],
                    "report": ligand["chemistry_report"],
                    "expected_charge": ligand["charge"],
                },
                outputs=[ligand["pose_sdf"], ligand["chemistry_report"]],
            )
        )
        protonation_args = ["-p", _format_mdp_number(ligand["ph"])] if ligand["protonation_mode"] == "ph" else ["-h"]
        steps.append(
            Step(
                ligand_title(
                    ligand,
                    "Generate ligand parameterization conformer",
                    "Generate ligand {key} parameterization conformer",
                ),
                [
                    obabel_bin,
                    "-isdf",
                    chemistry_sdf,
                    "-osdf",
                    "-O",
                    ligand["parameter_sdf"],
                    "--gen3d",
                    *protonation_args,
                ],
                outputs=[ligand["parameter_sdf"]],
            )
        )
        steps.append(
            Step(
                ligand_title(ligand, "Add ligand hydrogens from mapped chemistry", "Add ligand {key} hydrogens from mapped chemistry"),
                [obabel_bin, "-isdf", ligand["pose_sdf"], "-osdf", "-O", ligand["prepared_sdf"], *protonation_args],
                outputs=[ligand["prepared_sdf"]],
            )
        )
        acpype_input = ligand["parameter_sdf"]
    else:
        structure = ligand["structure_file"]
        if not structure and ligand["key"] == LEGACY_KEY:
            structure = _find_first(uploaded_files, (".sdf", ".mol2")) or ""
        if not structure and can_split_complex:
            structure = ligand["extracted_pdb"]
        if not structure:
            raise ValueError("ACPYPE mode requires a ligand .sdf, .mol2, or .pdb file.")
        if _bool_param(params, "use_obabel", False):
            acpype_input = ligand["prepared_sdf"]
            steps.append(
                Step(
                    ligand_title(ligand, "Prepare ligand hydrogens with Open Babel", "Prepare ligand {key} hydrogens with Open Babel"),
                    [
                        obabel_bin,
                        f"-i{Path(structure).suffix.lstrip('.')}",
                        structure,
                        "-osdf",
                        "-O",
                        acpype_input,
                        "-p",
                        _format_mdp_number(ligand["ph"]),
                    ],
                    outputs=[acpype_input],
                )
            )
        else:
            acpype_input = structure

    acpype_stem = Path(acpype_input).stem
    steps.append(
        Step(
            ligand_title(ligand, "Parameterize ligand with ACPYPE", "Parameterize ligand {key} with ACPYPE"),
            [acpype_bin, "-i", acpype_input, "-c", charge_method, "-a", atom_type, "-n", str(ligand["charge"])],
            outputs=[f"{acpype_stem}.acpype"],
            operation="acpype_ligand",
            data={"ligand_key": ligand["key"], "charge_confirmed": ligand["charge_confirmed"]},
        )
    )
    steps.append(
        _internal_step(
            ligand_title(ligand, "Isolate ACPYPE output", "Isolate ACPYPE output for ligand {key}"),
            "isolate_acpype_output",
            {"expected_dir": f"{acpype_stem}.acpype", "output_dir": ligand["acpype_dir"]},
            outputs=[ligand["acpype_dir"]],
        )
    )
    steps.append(
        _internal_step(
            ligand_title(ligand, "Stage ACPYPE ligand outputs", "Stage ACPYPE ligand {key} outputs"),
            "stage_acpype_ligand",
            {"acpype_dir": ligand["acpype_dir"], "gro_output": ligand["work_gro"], "itp_output": ligand["work_itp"]},
            outputs=[ligand["work_gro"], ligand["work_itp"]],
        )
    )
    if ligand["resolved"] == "acpype-map":
        steps.append(
            _internal_step(
                ligand_title(ligand, "Restore co-folded ligand pose", "Restore co-folded ligand {key} pose"),
                "restore_ligand_pose_coordinates",
                {
                    "pose_sdf": ligand["prepared_sdf"],
                    "parameter_sdf": ligand["parameter_sdf"],
                    "gro_file": ligand["work_gro"],
                    "report": ligand["pose_restore_report"],
                },
                outputs=[ligand["work_gro"], ligand["pose_restore_report"]],
            )
        )
    return steps


def build_steps(
    params: dict, uploaded_files: list[str], force_field_base: Path | None = None, *, analysis_trajectory_prepared: bool = False
) -> list[Step]:
    workflow = params.get("workflow", "protein_md")
    gmx_bin = params.get("gmx_bin", "gmx")
    box_type = _str_param(params, "box_type", "dodecahedron")
    box_distance = str(_float_param(params, "box_distance", 1.0))
    ntmpi = _int_param(params, "ntmpi", 1)
    ntomp = _int_param(params, "ntomp", 4)
    pin = _str_param(params, "pin", "auto")
    gpu = _bool_param(params, "gpu", False)
    verbose = _bool_param(params, "verbose", False)
    pre_nvt = _bool_param(params, "pre_nvt", False)

    structure = _file_param(params, "structure_file", uploaded_files, (".pdb", ".gro"))
    protein_file = _file_param(params, "protein_file", uploaded_files, (".pdb", ".gro"))
    topology = _topology(uploaded_files)
    tpr = _file_param(params, "tpr_file", uploaded_files, (".tpr",))
    trajectory = _file_param(params, "trajectory_file", uploaded_files, (".xtc", ".trr"))
    production_deffnm = _output_name_param(params, "production_deffnm", "md", "Production output prefix")

    if workflow in {"protein_md", "em_only"} and not structure:
        raise ValueError("Upload at least one .pdb or .gro structure file.")
    if workflow == "protein_ligand_md" and not protein_file:
        raise ValueError("Upload a protein .pdb/.gro file or set protein_file.")
    if workflow == "run_tpr" and not tpr:
        raise ValueError("Upload a .tpr file for Run TPR.")
    if workflow in {"analysis_rmsd", "postprocess", "analysis_suite"} and (not tpr or not trajectory):
        raise ValueError("Upload one .tpr and one .xtc/.trr trajectory file for this workflow.")

    if workflow == "protein_md":
        pdb2gmx_source, pre_steps = _pdb2gmx_source_steps(params, structure, "protein_pdb2gmx_input.pdb")
        nvt_steps = []
        if pre_nvt:
            nvt_steps = [
                Step(
                    "Prepare optional NVT",
                    _as_gmx(
                        gmx_bin,
                        [
                            "grompp",
                            "-f",
                            "nvt.mdp",
                            "-c",
                            "em.gro",
                            "-r",
                            "em.gro",
                            "-p",
                            "topol.top",
                            "-o",
                            "nvt.tpr",
                            *_maybe_maxwarn(params),
                        ],
                    ),
                    outputs=["nvt.tpr"],
                ),
                Step(
                    "Run optional NVT",
                    _as_gmx(gmx_bin, _mdrun_args("nvt", ntmpi, ntomp, gpu, verbose, pin=pin)),
                    outputs=["nvt.gro", "nvt.cpt", "nvt.edr", "nvt.log"],
                ),
                _internal_step(
                    "Check optional NVT",
                    "validate_stage_log",
                    {"stage": "nvt", "log_file": "nvt.log", "output_report": "quality-nvt.txt"},
                    outputs=["quality-nvt.txt"],
                ),
            ]
        npt_coordinate = "nvt.gro" if pre_nvt else "em.gro"
        npt_checkpoint = ["-t", "nvt.cpt"] if pre_nvt else []
        return [
            _structure_preflight_step(structure),
            *pre_steps,
            _pdb2gmx_step(
                "Build topology",
                gmx_bin,
                params,
                pdb2gmx_source,
                "processed.gro",
                "topol.top",
                force_field_base,
            ),
            Step(
                "Define box",
                _as_gmx(
                    gmx_bin,
                    ["editconf", "-f", "processed.gro", "-o", "boxed.gro", "-c", "-d", box_distance, "-bt", box_type],
                ),
                outputs=["boxed.gro"],
            ),
            Step(
                "Solvate",
                _as_gmx(
                    gmx_bin, ["solvate", "-cp", "boxed.gro", "-cs", _solvent_template(params), "-o", "solvated.gro", "-p", "topol.top"]
                ),
                outputs=["solvated.gro"],
            ),
            Step(
                "Prepare ions",
                _as_gmx(
                    gmx_bin,
                    ["grompp", "-f", "ions.mdp", "-c", "solvated.gro", "-p", "topol.top", "-o", "ions.tpr", *_maybe_maxwarn(params)],
                ),
                outputs=["ions.tpr"],
            ),
            Step(
                "Neutralize",
                _ion_args(gmx_bin, params, "ions.tpr", "solv_ions.gro"),
                stdin_text=_selection_text(_str_param(params, "solvent_group", "SOL")),
                outputs=["solv_ions.gro"],
            ),
            Step(
                "Prepare minimization",
                _as_gmx(
                    gmx_bin,
                    ["grompp", "-f", "minim.mdp", "-c", "solv_ions.gro", "-p", "topol.top", "-o", "em.tpr", *_maybe_maxwarn(params)],
                ),
                outputs=["em.tpr"],
            ),
            Step(
                "Minimize", _as_gmx(gmx_bin, _mdrun_args("em", ntmpi, ntomp, gpu, verbose, pin=pin)), outputs=["em.gro", "em.edr", "em.log"]
            ),
            _internal_step(
                "Check minimization",
                "validate_stage_log",
                {"stage": "em", "log_file": "em.log", "output_report": "quality-em.txt"},
                outputs=["quality-em.txt"],
            ),
            *nvt_steps,
            _internal_step(
                "Configure NPT start",
                "prepare_npt_mdp",
                {"source_mdp": "npt.mdp", "output_mdp": "npt-run.mdp", "after_nvt": pre_nvt},
                outputs=["npt-run.mdp"],
            ),
            Step(
                "Prepare NPT",
                _as_gmx(
                    gmx_bin,
                    [
                        "grompp",
                        "-f",
                        "npt-run.mdp",
                        "-c",
                        npt_coordinate,
                        "-r",
                        npt_coordinate,
                        *npt_checkpoint,
                        "-p",
                        "topol.top",
                        "-o",
                        "npt.tpr",
                        *_maybe_maxwarn(params),
                    ],
                ),
                outputs=["npt.tpr"],
            ),
            Step(
                "Run NPT",
                _as_gmx(gmx_bin, _mdrun_args("npt", ntmpi, ntomp, gpu, verbose, pin=pin)),
                outputs=["npt.gro", "npt.cpt", "npt.edr", "npt.log"],
            ),
            _internal_step(
                "Check NPT",
                "validate_stage_log",
                {"stage": "npt", "log_file": "npt.log", "output_report": "quality-npt.txt"},
                outputs=["quality-npt.txt"],
            ),
            *_restraint_release_steps(gmx_bin, params, "npt-run.mdp", [], ntmpi, ntomp, gpu, verbose),
            *_thermodynamic_gate_steps(
                gmx_bin,
                "npt-free.edr" if _bool_param(params, "release_restraints", True) else "npt.edr",
                "npt-free" if _bool_param(params, "release_restraints", True) else "npt",
                "npt-unrestrained.mdp" if _bool_param(params, "release_restraints", True) else "npt-run.mdp",
            ),
            *_production_steps(
                gmx_bin,
                params,
                "md.mdp",
                "topol.top",
                [],
                ntmpi,
                ntomp,
                gpu,
                verbose,
                "npt-free" if _bool_param(params, "release_restraints", True) else "npt",
            ),
            _internal_step(
                "Generate publication figures",
                "generate_analysis_figures",
                {"output_dir": "figures", "dpi": 1600},
                outputs=["figures/manifest.json"],
            ),
        ]

    if workflow == "protein_ligand_md":
        ligands = normalize_ligands(params)
        total = len(ligands)
        for ligand in ligands:
            ligand.update(ligand_work_names(ligand, total))
        auto_split_complex = _bool_param(params, "auto_split_complex_pdb", True)
        create_index = _bool_param(params, "create_index", True)
        index_file = "index.ndx"
        nvt_mdp = "nvt.mdp"
        npt_mdp = "npt.mdp"
        md_mdp = "md.mdp"
        minim_mdp = "minim.mdp"
        steps: list[Step] = []
        steps.append(_structure_preflight_step(protein_file))
        can_split_complex = auto_split_complex and protein_file.lower().endswith(".pdb")

        # Decide how every ligand gets parameterized: prepared itp/gro, plain
        # ACPYPE, or ACPYPE with SDF/SMILES chemistry mapped onto the pose
        # extracted from the co-folded complex PDB.
        for ligand in ligands:
            if ligand["key"] == LEGACY_KEY and (not ligand["gro_file"] or not ligand["itp_file"]):
                ligand["gro_file"] = ligand["gro_file"] or (_find_first(uploaded_files, (".gro",)) or "")
                ligand["itp_file"] = ligand["itp_file"] or (_find_first(uploaded_files, (".itp",)) or "")
            has_prepared = bool(ligand["gro_file"] and ligand["itp_file"])
            structure = ligand["structure_file"]
            chemistry = ligand["chemistry_file"] or ligand["smiles"]
            extracted_available = can_split_complex and (bool(ligand["residue"]) or total == 1)
            if chemistry and extracted_available:
                ligand["resolved"] = "acpype-map"
            elif structure.lower().endswith((".sdf", ".mol2")) and extracted_available and not chemistry:
                ligand["chemistry_file"] = structure
                ligand["structure_file"] = ""
                ligand["resolved"] = "acpype-map"
            elif ligand["mode"] == "acpype":
                ligand["resolved"] = "acpype"
            elif has_prepared:
                ligand["resolved"] = "prepared"
            elif extracted_available:
                ligand["resolved"] = "acpype"
            else:
                raise ValueError("Prepared ligand mode requires ligand .gro and .itp files.")

        if can_split_complex:
            if total > 1:
                missing_residues = [ligand["key"] for ligand in ligands if not ligand["residue"]]
                if missing_residues:
                    raise ValueError(
                        f"Multi-ligand complexes need a PDB residue name for every ligand; missing: {', '.join(missing_residues)}."
                    )
                steps.append(
                    _split_complex_pdb_multi_step(
                        protein_file,
                        "protein_pdb2gmx_input.pdb",
                        [{"residue": ligand["residue"], "output_pdb": ligand["extracted_pdb"]} for ligand in ligands],
                    )
                )
                pdb2gmx_source = "protein_pdb2gmx_input.pdb"
            else:
                ligand = ligands[0]
                steps.append(_split_complex_pdb_step(protein_file, "protein_pdb2gmx_input.pdb", ligand["extracted_pdb"], ligand["residue"]))
                pdb2gmx_source = "protein_pdb2gmx_input.pdb"
        else:
            pdb2gmx_source, pre_steps = _pdb2gmx_source_steps(params, protein_file, "protein_pdb2gmx_input.pdb")
            steps.extend(pre_steps)

        # 2. Parameterize or import every ligand (ACPYPE/prepared).
        for ligand in ligands:
            steps.extend(_ligand_parameterization_steps(ligand, params, uploaded_files, can_split_complex))

        # 3. Each ACPYPE *_GMX.itp carries its own [ atomtypes ]; with several
        #    ligand itps included into one topol.top the shared atom types would
        #    be defined twice and grompp aborts. Merge them once.
        if total > 1:
            steps.append(
                _internal_step(
                    "Merge ligand itp atomtypes",
                    "merge_ligand_atomtypes",
                    {"itps": [ligand["work_itp"] for ligand in ligands], "report": "ligand-atomtypes-merge.txt"},
                    outputs=[*(ligand["work_itp"] for ligand in ligands), "ligand-atomtypes-merge.txt"],
                )
            )

        steps.append(
            _pdb2gmx_step(
                "Build protein topology",
                gmx_bin,
                params,
                pdb2gmx_source,
                "protein_processed.gro",
                "topol.top",
                force_field_base,
                outputs=["protein_processed.gro", "topol.top", "posre.itp"],
            )
        )

        # 4. Per-ligand position restraints (own index + genrestr per ligand).
        ligand_heavy_group = _str_param(params, "ligand_heavy_group", "System_&_!H*")
        posres_fc_x = _str_param(params, "posres_fc_x", "1000")
        posres_fc_y = _str_param(params, "posres_fc_y", "1000")
        posres_fc_z = _str_param(params, "posres_fc_z", "1000")
        for ligand in ligands:
            if not ligand["posres"]:
                continue
            steps.extend(
                [
                    Step(
                        ligand_title(ligand, "Create ligand heavy-atom index", "Create ligand {key} heavy-atom index"),
                        _as_gmx(gmx_bin, ["make_ndx", "-f", ligand["work_gro"], "-o", ligand["index_lig"]]),
                        stdin_text=_selection_text("0 & ! a H*", "q"),
                        outputs=[ligand["index_lig"]],
                    ),
                    Step(
                        ligand_title(ligand, "Create ligand position restraints", "Create ligand {key} position restraints"),
                        _as_gmx(
                            gmx_bin,
                            [
                                "genrestr",
                                "-f",
                                ligand["work_gro"],
                                "-n",
                                ligand["index_lig"],
                                "-o",
                                ligand["work_posre"],
                                "-fc",
                                posres_fc_x,
                                posres_fc_y,
                                posres_fc_z,
                            ],
                        ),
                        stdin_text=_selection_text(ligand_heavy_group),
                        outputs=[ligand["work_posre"]],
                    ),
                ]
            )

        # 5. Merge all ligand coordinates with the protein and patch topol.top
        #    with every ligand itp + posre include and [ molecules ] entries.
        steps.extend(
            [
                _internal_step(
                    "Merge protein and ligand coordinates",
                    "merge_gro",
                    {
                        "protein_gro": "protein_processed.gro",
                        "ligand_gros": [ligand["work_gro"] for ligand in ligands],
                        "output_gro": "complex.gro",
                    },
                    outputs=["complex.gro", "complex-pose-check.txt"],
                ),
                _internal_step(
                    "Patch topology for ligands" if total > 1 else "Patch topology for ligand",
                    "patch_topology",
                    {
                        "topology": "topol.top",
                        "ligands": [
                            {
                                "itp": ligand["work_itp"],
                                "posre": ligand["work_posre"] if ligand["posres"] else "",
                                "name": ligand["name"],
                                "count": ligand["count"],
                                "define": ligand["define"],
                            }
                            for ligand in ligands
                        ],
                    },
                    outputs=["topol.top"],
                ),
            ]
        )

        # Equilibration MDPs reference per-ligand POSRES_* defines; rewrite the
        # workdir copies (never the repo templates) to match this topology.
        posres_defines = [ligand["define"] for ligand in ligands if ligand["posres"]]
        if posres_defines and not _str_param(params, "mdp_define_equil", ""):
            steps.append(
                _internal_step(
                    "Configure equilibration restraint defines",
                    "patch_equilibration_defines",
                    {"mdps": [nvt_mdp, npt_mdp], "defines": ["POSRES", *posres_defines]},
                    outputs=[nvt_mdp, npt_mdp],
                )
            )

        steps.extend(
            [
                Step(
                    "Define complex box",
                    _as_gmx(gmx_bin, ["editconf", "-f", "complex.gro", "-o", "complex_box.gro", "-c", "-d", box_distance, "-bt", box_type]),
                    outputs=["complex_box.gro"],
                ),
                Step(
                    "Solvate complex",
                    _as_gmx(
                        gmx_bin,
                        [
                            "solvate",
                            "-cp",
                            "complex_box.gro",
                            "-cs",
                            _solvent_template(params),
                            "-o",
                            "complex_solv.gro",
                            "-p",
                            "topol.top",
                        ],
                    ),
                    outputs=["complex_solv.gro"],
                ),
                Step(
                    "Prepare ions",
                    _as_gmx(
                        gmx_bin,
                        [
                            "grompp",
                            "-f",
                            "ions.mdp",
                            "-c",
                            "complex_solv.gro",
                            "-p",
                            "topol.top",
                            "-o",
                            "ions.tpr",
                            *_maybe_maxwarn(params),
                        ],
                    ),
                    outputs=["ions.tpr"],
                ),
                Step(
                    "Neutralize or salt complex",
                    _ion_args(gmx_bin, params, "ions.tpr", "complex_ions.gro"),
                    stdin_text=_selection_text(_str_param(params, "solvent_group", "SOL")),
                    outputs=["complex_ions.gro"],
                ),
            ]
        )

        if create_index:
            index_script = _str_param(params, "index_script", "")
            if index_script:
                steps.append(
                    Step(
                        "Create custom complex index groups",
                        _as_gmx(gmx_bin, ["make_ndx", "-f", "complex_ions.gro", "-o", index_file]),
                        stdin_text=index_script,
                        outputs=[index_file],
                    )
                )
            else:
                steps.append(
                    _internal_step(
                        "Create robust complex index groups",
                        "create_complex_index",
                        {
                            "protein_gro": "protein_processed.gro",
                            "ligand_gros": [{"gro": ligand["work_gro"], "group": ligand["group"]} for ligand in ligands],
                            "system_gro": "complex_ions.gro",
                            "output_index": index_file,
                        },
                        outputs=[index_file],
                    )
                )

        n_arg = ["-n", index_file] if create_index else []
        nvt_steps = []
        if pre_nvt:
            nvt_steps = [
                Step(
                    "Prepare optional NVT",
                    _as_gmx(
                        gmx_bin,
                        [
                            "grompp",
                            "-f",
                            nvt_mdp,
                            "-c",
                            "em.gro",
                            "-r",
                            "em.gro",
                            "-p",
                            "topol.top",
                            *n_arg,
                            "-o",
                            "nvt.tpr",
                            *_maybe_maxwarn(params),
                        ],
                    ),
                    outputs=["nvt.tpr"],
                ),
                Step(
                    "Run optional NVT",
                    _as_gmx(gmx_bin, _mdrun_args("nvt", ntmpi, ntomp, gpu, verbose, pin=pin)),
                    outputs=["nvt.gro", "nvt.cpt", "nvt.edr", "nvt.log"],
                ),
                _internal_step(
                    "Check optional NVT",
                    "validate_stage_log",
                    {"stage": "nvt", "log_file": "nvt.log", "output_report": "quality-nvt.txt"},
                    outputs=["quality-nvt.txt"],
                ),
            ]
        npt_coordinate = "nvt.gro" if pre_nvt else "em.gro"
        npt_checkpoint = ["-t", "nvt.cpt"] if pre_nvt else []
        steps.extend(
            [
                Step(
                    "Prepare minimization",
                    _as_gmx(
                        gmx_bin,
                        [
                            "grompp",
                            "-f",
                            minim_mdp,
                            "-c",
                            "complex_ions.gro",
                            "-p",
                            "topol.top",
                            "-o",
                            "em.tpr",
                            *n_arg,
                            *_maybe_maxwarn(params),
                        ],
                    ),
                    outputs=["em.tpr"],
                ),
                Step(
                    "Minimize",
                    _as_gmx(gmx_bin, _mdrun_args("em", ntmpi, ntomp, gpu, verbose, pin=pin)),
                    outputs=["em.gro", "em.edr", "em.log"],
                ),
                _internal_step(
                    "Check minimization",
                    "validate_stage_log",
                    {"stage": "em", "log_file": "em.log", "output_report": "quality-em.txt"},
                    outputs=["quality-em.txt"],
                ),
                *nvt_steps,
                _internal_step(
                    "Configure NPT start",
                    "prepare_npt_mdp",
                    {"source_mdp": npt_mdp, "output_mdp": "npt-run.mdp", "after_nvt": pre_nvt},
                    outputs=["npt-run.mdp"],
                ),
                Step(
                    "Prepare NPT",
                    _as_gmx(
                        gmx_bin,
                        [
                            "grompp",
                            "-f",
                            "npt-run.mdp",
                            "-c",
                            npt_coordinate,
                            "-r",
                            npt_coordinate,
                            *npt_checkpoint,
                            "-p",
                            "topol.top",
                            *n_arg,
                            "-o",
                            "npt.tpr",
                            *_maybe_maxwarn(params),
                        ],
                    ),
                    outputs=["npt.tpr"],
                ),
                Step(
                    "Run NPT",
                    _as_gmx(gmx_bin, _mdrun_args("npt", ntmpi, ntomp, gpu, verbose, pin=pin)),
                    outputs=["npt.gro", "npt.cpt", "npt.edr", "npt.log"],
                ),
                _internal_step(
                    "Check NPT",
                    "validate_stage_log",
                    {"stage": "npt", "log_file": "npt.log", "output_report": "quality-npt.txt"},
                    outputs=["quality-npt.txt"],
                ),
                *_restraint_release_steps(gmx_bin, params, "npt-run.mdp", n_arg, ntmpi, ntomp, gpu, verbose),
                *_thermodynamic_gate_steps(
                    gmx_bin,
                    "npt-free.edr" if _bool_param(params, "release_restraints", True) else "npt.edr",
                    "npt-free" if _bool_param(params, "release_restraints", True) else "npt",
                    "npt-unrestrained.mdp" if _bool_param(params, "release_restraints", True) else "npt-run.mdp",
                ),
                *_production_steps(
                    gmx_bin,
                    params,
                    md_mdp,
                    "topol.top",
                    n_arg,
                    ntmpi,
                    ntomp,
                    gpu,
                    verbose,
                    "npt-free" if _bool_param(params, "release_restraints", True) else "npt",
                ),
            ]
        )

        replicas = _int_param(params, "replicas", 1)
        for replica in range(1, replicas + 1):
            name = production_deffnm if replicas == 1 else f"{production_deffnm}_r{replica:02d}"
            replica_params = dict(params)
            if replicas > 1:
                for key, default in (
                    ("center_output", "md_0_100_center.xtc"),
                    ("fit_output", "md_0_100_fit.xtc"),
                    ("start_pdb", "start.pdb"),
                ):
                    output = Path(_output_name_param(params, key, default, key))
                    replica_params[key] = str(output.with_name(f"{output.stem}_r{replica:02d}{output.suffix}"))
            replica_steps = []
            if _bool_param(params, "run_postprocess_after_md", True):
                replica_steps.extend(
                    _postprocess_steps(replica_params, gmx_bin, f"{name}.tpr", f"{name}.xtc", index_file if create_index else "")
                )
            if _bool_param(params, "run_classic_analysis_after_md", True):
                replica_steps.extend(
                    _classic_complex_analysis_steps(
                        replica_params,
                        gmx_bin,
                        f"{name}.tpr",
                        f"{name}.xtc",
                        index_file if create_index else "",
                        [ligand["group"] for ligand in ligands],
                    )
                )
            if replicas > 1:
                for step in replica_steps:
                    step.title = f"Replica {replica}: {step.title}"
                    if step.parallel_group:
                        step.parallel_group = f"replica-{replica}-{step.parallel_group}"
            steps.extend(replica_steps)
        steps.append(
            _internal_step(
                "Generate publication figures",
                "generate_analysis_figures",
                {"output_dir": "figures", "dpi": 1600},
                outputs=["figures/manifest.json"],
            )
        )
        return steps

    if workflow == "em_only":
        return [
            Step(
                "Prepare minimization",
                _as_gmx(gmx_bin, ["grompp", "-f", "minim.mdp", "-c", structure, "-p", topology, "-o", "em.tpr", *_maybe_maxwarn(params)]),
                outputs=["em.tpr"],
            ),
            Step(
                "Minimize", _as_gmx(gmx_bin, _mdrun_args("em", ntmpi, ntomp, gpu, verbose, pin=pin)), outputs=["em.gro", "em.edr", "em.log"]
            ),
        ]

    if workflow == "run_tpr":
        stem = Path(tpr).stem
        return [
            Step(
                "Run TPR",
                _as_gmx(gmx_bin, ["mdrun", "-s", tpr, *_mdrun_args(stem, ntmpi, ntomp, gpu, verbose, pin=pin)[1:]]),
                outputs=[f"{stem}.log"],
            )
        ]

    if workflow == "analysis_rmsd":
        rmsd_params = {
            **params,
            "do_rmsd": True,
            "do_energy": False,
            "do_rg": False,
            "do_dssp": False,
            "do_hbond": False,
            "do_pca": False,
            "do_sham": False,
        }
        return _analysis_steps(rmsd_params, gmx_bin, tpr, trajectory, _str_param(params, "index_file", ""))

    if workflow == "postprocess":
        return _postprocess_steps(params, gmx_bin, tpr, trajectory, _str_param(params, "index_file", ""))

    if workflow == "analysis_suite":
        return [
            *_analysis_steps(
                params, gmx_bin, tpr, trajectory, _str_param(params, "index_file", ""), trajectory_prepared=analysis_trajectory_prepared
            ),
            _internal_step(
                "Generate publication figures",
                "generate_analysis_figures",
                {"output_dir": "figures", "dpi": 1600},
                outputs=["figures/manifest.json"],
            ),
        ]

    if workflow == "custom":
        command = str(params.get("custom_command", "")).strip()
        args = parse_custom_command(command, gmx_bin)
        return [Step("Custom command", args)]

    raise ValueError(f"Unknown workflow: {workflow}")
