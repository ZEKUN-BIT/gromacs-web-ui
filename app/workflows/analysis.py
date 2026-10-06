from __future__ import annotations

import math
from pathlib import Path

from ..gromacs import Step, _as_gmx, _bool_param, _index_arg, _internal_step, _output_name_param, _selection_text, _str_param, slugify


def _analysis_window_args(params: dict, time_unit: str, *, include_begin: bool = True) -> list[str]:
    """GROMACS interprets -b/-e in the command's -tu unit (ps by default)."""
    if "begin_ns" not in params and "end_ns" not in params:
        return []
    scale = {"fs": 1_000_000, "ps": 1000, "ns": 1, "us": 0.001, "ms": 0.000001, "s": 0.000000001}.get(time_unit)
    if scale is None:
        raise ValueError("Unsupported analysis time unit.")
    begin = float(params.get("begin_ns", 0))
    end = params.get("end_ns")
    end = float(end) if end is not None else None
    if not math.isfinite(begin) or begin < 0 or end is not None and (not math.isfinite(end) or end <= begin):
        raise ValueError("Invalid analysis time window.")
    args = ["-b", f"{begin * scale:g}"] if include_begin else []
    if end is not None:
        args.extend(["-e", f"{end * scale:g}"])
    return args


def _postprocess_steps(params: dict, gmx_bin: str, tpr: str, trajectory: str, index_file: str = "") -> list[Step]:
    steps: list[Step] = []
    centered = _output_name_param(params, "center_output", "md_0_100_center.xtc", "Centered trajectory output")
    fitted = _output_name_param(params, "fit_output", "md_0_100_fit.xtc", "Fitted trajectory output")
    start_pdb = _output_name_param(params, "start_pdb", "start.pdb", "First-frame output")
    do_center = _bool_param(params, "do_center", True)
    do_fit = _bool_param(params, "do_fit", True)
    do_dump = _bool_param(params, "do_dump", True)
    keep_centered = _bool_param(params, "keep_centered_trajectory", False)
    keep_fitted = _bool_param(params, "keep_fitted_trajectory", False)

    if do_center:
        # For homo-/hetero-multimers, -pbc mol only makes each chain whole
        # independently and can leave the two subunits on opposite sides of the
        # box. -pbc cluster keeps the whole solute (all chains + ligand) in one
        # periodic image, which is what intra-complex analyses need.
        pbc_mode = _str_param(params, "pbc_mode", "cluster")
        center_group = _str_param(params, "center_group", "Protein_Lig")
        output_group = _str_param(params, "output_group", "System")
        center_args = [
            "trjconv",
            "-s",
            tpr,
            "-f",
            trajectory,
            "-o",
            centered,
            "-center",
            "-pbc",
            pbc_mode,
        ]
        if pbc_mode in {"mol", "res", "atom"}:
            center_args.extend(["-ur", _str_param(params, "unitcell", "compact")])
        center_args.extend(_index_arg(index_file))
        if pbc_mode == "cluster":
            # cluster prompts for: clustering group, centering group, output group
            center_stdin = _selection_text(center_group, center_group, output_group)
        else:
            center_stdin = _selection_text(center_group, output_group)
        steps.append(
            Step(
                "Center trajectory and repair PBC",
                _as_gmx(gmx_bin, center_args),
                stdin_text=center_stdin,
                outputs=[centered],
            )
        )

    fit_input = centered if do_center else trajectory
    if do_fit:
        steps.append(
            Step(
                "Fit trajectory",
                _as_gmx(
                    gmx_bin,
                    [
                        "trjconv",
                        "-s",
                        tpr,
                        "-f",
                        fit_input,
                        "-o",
                        fitted,
                        "-fit",
                        _str_param(params, "fit_mode", "rot+trans"),
                        *_index_arg(index_file),
                    ],
                ),
                stdin_text=_selection_text(_str_param(params, "fit_group", "Backbone"), _str_param(params, "output_group", "System")),
                outputs=[fitted],
            )
        )

    if do_dump:
        dump_source = centered if do_center else fit_input
        steps.append(
            Step(
                "Extract first frame",
                _as_gmx(
                    gmx_bin,
                    [
                        "trjconv",
                        "-s",
                        tpr,
                        "-f",
                        dump_source,
                        "-o",
                        start_pdb,
                        "-dump",
                        _str_param(params, "dump_time", "0"),
                        *_index_arg(index_file),
                    ],
                ),
                stdin_text=_selection_text(_str_param(params, "output_group", "System")),
                outputs=[start_pdb],
            )
        )
    if do_center and do_fit and not keep_centered and centered != fitted:
        steps.append(
            Step(
                "Remove intermediate centered trajectory",
                ["internal"],
                kind="internal",
                operation="remove_files",
                data={"paths": [centered]},
            )
        )
    if do_fit and not keep_fitted:
        steps.append(
            Step(
                "Remove fitted trajectory",
                ["internal"],
                kind="internal",
                operation="remove_files",
                data={"paths": [fitted]},
            )
        )
    return steps


def _classic_complex_analysis_steps(
    params: dict,
    gmx_bin: str,
    tpr: str,
    trajectory: str,
    index_file: str,
    ligand_groups: list[str] | None = None,
) -> list[Step]:
    """Core structural and protein-ligand analyses for a production trajectory."""
    prefix = Path(tpr).stem
    index_args = _index_arg(index_file)
    complex_trajectory = f"{prefix}-complex-whole.xtc"
    complex_reference = f"{prefix}-complex-reference.gro"
    steps: list[Step] = [
        Step(
            "Keep protein and ligand in the same periodic image",
            _as_gmx(gmx_bin, ["trjconv", "-s", tpr, "-f", trajectory, "-o", complex_trajectory, "-pbc", "cluster", "-center", *index_args]),
            # Cluster and center on the solute while retaining the full system
            # in the output for compatibility with the full-system TPR.
            stdin_text=_selection_text("Protein_Lig", "Protein_Lig", "System"),
            outputs=[complex_trajectory],
        ),
        Step(
            "Extract protein-ligand RMSD reference",
            _as_gmx(gmx_bin, ["trjconv", "-s", tpr, "-f", complex_trajectory, "-o", complex_reference, "-dump", "0", *index_args]),
            stdin_text=_selection_text("System"),
            outputs=[complex_reference],
        ),
        Step(
            "Compute protein C-alpha RMSF",
            _as_gmx(
                gmx_bin, ["rmsf", "-s", tpr, "-f", complex_trajectory, "-o", f"{prefix}-protein-rmsf.xvg", "-res", "-fit", *index_args]
            ),
            stdin_text=_selection_text("C-alpha"),
            outputs=[f"{prefix}-protein-rmsf.xvg"],
            parallel_group="complex-analysis",
        ),
        Step(
            "Compute protein radius of gyration",
            _as_gmx(
                gmx_bin,
                [
                    "gyrate",
                    "-s",
                    tpr,
                    "-f",
                    complex_trajectory,
                    "-o",
                    f"{prefix}-protein-rg.xvg",
                    "-sel",
                    "Protein",
                    "-tu",
                    "ns",
                    *index_args,
                ],
            ),
            outputs=[f"{prefix}-protein-rg.xvg"],
            parallel_group="complex-analysis",
        ),
        Step(
            "Compute protein solvent-accessible surface area",
            _as_gmx(
                gmx_bin,
                [
                    "sasa",
                    "-s",
                    tpr,
                    "-f",
                    complex_trajectory,
                    "-o",
                    f"{prefix}-protein-sasa.xvg",
                    "-surface",
                    "Protein",
                    "-tu",
                    "ns",
                    *index_args,
                ],
            ),
            outputs=[f"{prefix}-protein-sasa.xvg"],
            parallel_group="complex-analysis",
        ),
    ]

    # Per-ligand interaction analyses.  A single ligand keeps the legacy
    # "Ligand" group name and output naming; every further ligand gets its own
    # group (Ligand_<key>) and per-ligand output files.
    for group in [item for item in (ligand_groups or ["Ligand"]) if item]:
        if group == "Ligand":
            rmsd_output = f"{prefix}-ligand-rmsd.xvg"
            mindist_output = f"{prefix}-protein-ligand-mindist.xvg"
            contacts_output = f"{prefix}-protein-ligand-contacts.xvg"
            hbond_output = f"{prefix}-protein-ligand-hbonds.xvg"
            rmsd_title = "Compute ligand RMSD relative to protein"
            mindist_title = "Compute protein-ligand minimum distance and contacts"
            hbond_title = "Compute protein-ligand hydrogen bonds"
        else:
            slug = slugify(group.lower())
            rmsd_output = f"{prefix}-{slug}-rmsd.xvg"
            mindist_output = f"{prefix}-protein-{slug}-mindist.xvg"
            contacts_output = f"{prefix}-protein-{slug}-contacts.xvg"
            hbond_output = f"{prefix}-protein-{slug}-hbonds.xvg"
            rmsd_title = f"Compute {group} RMSD relative to protein"
            mindist_title = f"Compute protein-{group} minimum distance and contacts"
            hbond_title = f"Compute protein-{group} hydrogen bonds"
        steps.extend(
            [
                Step(
                    rmsd_title,
                    _as_gmx(
                        gmx_bin,
                        ["rms", "-s", tpr, "-f", complex_trajectory, "-o", rmsd_output, "-tu", "ns", "-fit", "rot+trans", *index_args],
                    ),
                    stdin_text=_selection_text("Backbone", group),
                    outputs=[rmsd_output],
                    parallel_group="complex-analysis",
                ),
                Step(
                    mindist_title,
                    _as_gmx(
                        gmx_bin,
                        [
                            "mindist",
                            "-s",
                            tpr,
                            "-f",
                            complex_trajectory,
                            "-od",
                            mindist_output,
                            "-on",
                            contacts_output,
                            "-d",
                            "0.45",
                            "-group",
                            "-tu",
                            "ns",
                            *index_args,
                        ],
                    ),
                    stdin_text=_selection_text(group, "Protein"),
                    outputs=[mindist_output, contacts_output],
                    parallel_group="complex-analysis",
                ),
                Step(
                    hbond_title,
                    _as_gmx(
                        gmx_bin,
                        [
                            "hbond-legacy",
                            "-s",
                            tpr,
                            "-f",
                            complex_trajectory,
                            "-num",
                            hbond_output,
                            "-tu",
                            "ns",
                            *index_args,
                        ],
                    ),
                    stdin_text=_selection_text(group, "Protein"),
                    outputs=[hbond_output],
                    parallel_group="complex-analysis",
                ),
            ]
        )

    steps.append(
        Step(
            "Remove intermediate whole trajectory",
            ["internal"],
            kind="internal",
            operation="remove_files",
            data={"paths": [complex_trajectory]},
        )
    )
    return steps


def _analysis_steps(
    params: dict, gmx_bin: str, tpr: str, trajectory: str, index_file: str = "", *, trajectory_prepared: bool = False
) -> list[Step]:
    steps: list[Step] = []
    time_unit = _str_param(params, "time_unit", "ns")
    edr_file = _str_param(params, "edr_file", "md_0_100.edr")
    fit_group = _str_param(params, "fit_group", "Backbone")
    rmsd_group = _str_param(params, "rmsd_group", "Backbone")
    do_pca = _bool_param(params, "do_pca", False)
    do_sham = _bool_param(params, "do_sham", False)
    if do_sham:
        do_pca = True
    do_rmsd = _bool_param(params, "do_rmsd", True)
    needs_structure = (
        any(_bool_param(params, key, default) for key, default in (("do_rg", True), ("do_dssp", False), ("do_hbond", True))) or do_pca
    )
    # These trajectories keep the original box orientation. Rigid-body fits
    # belong inside structural analyses, never in contact/H-bond inputs.
    prepared_trajectory = trajectory
    intermediates: list[str] = []

    def cluster_step(source: str, output: str, title: str) -> Step:
        group = _str_param(params, "center_group", "Protein")
        return Step(
            title,
            _as_gmx(
                gmx_bin,
                [
                    "trjconv",
                    "-s",
                    tpr,
                    "-f",
                    source,
                    "-o",
                    output,
                    "-pbc",
                    "cluster",
                    "-center",
                    *_index_arg(index_file),
                    *_analysis_window_args(params, "ps"),
                ],
            ),
            stdin_text=_selection_text(group, group, "System"),
            outputs=[output],
        )

    if _bool_param(params, "do_energy", True):
        energy_output = _output_name_param(params, "energy_output", "potential.xvg", "Energy output")
        steps.append(
            Step(
                "Extract energy term",
                _as_gmx(gmx_bin, ["energy", "-f", edr_file, "-o", energy_output]),
                stdin_text=_selection_text(_str_param(params, "energy_terms", "Potential"), "0"),
                outputs=[energy_output],
            )
        )

    if do_rmsd:
        rmsd_output = _output_name_param(params, "rmsd_output", "rmsd.xvg", "RMSD output")
        unwrapped_trajectory = f"{Path(rmsd_output).stem}-nojump.xtc"
        rmsd_whole_trajectory = f"{Path(rmsd_output).stem}-whole.xtc"
        rmsd_input = (
            _output_name_param(params, "rmsd_trajectory_file", trajectory, "RMSD trajectory input")
            if "rmsd_trajectory_file" in params
            else trajectory
        )
        if {unwrapped_trajectory, rmsd_whole_trajectory} & {trajectory, rmsd_input, tpr, index_file, edr_file}:
            raise ValueError("RMSD intermediate filenames must differ from analysis input files.")
        # Keep all history before the requested window for cumulative nojump
        # reconstruction. Only cap its end; clustering and gmx rms select the
        # final window and retain the selected TPR's reference coordinates.
        steps.append(
            Step(
                "Unwrap trajectory for RMSD",
                _as_gmx(
                    gmx_bin,
                    [
                        "trjconv",
                        "-s",
                        tpr,
                        "-f",
                        rmsd_input,
                        "-o",
                        unwrapped_trajectory,
                        "-pbc",
                        "nojump",
                        *_index_arg(index_file),
                        *_analysis_window_args(params, "ps", include_begin=False),
                    ],
                ),
                stdin_text=_selection_text("System"),
                outputs=[unwrapped_trajectory],
            )
        )
        steps.append(cluster_step(unwrapped_trajectory, rmsd_whole_trajectory, "Cluster and center trajectory for RMSD"))
        steps.append(_internal_step("Remove intermediate unwrapped trajectory", "remove_files", {"paths": [unwrapped_trajectory]}))
        steps.append(
            Step(
                "Compute RMSD",
                _as_gmx(
                    gmx_bin,
                    [
                        "rms",
                        "-s",
                        tpr,
                        "-f",
                        rmsd_whole_trajectory,
                        "-o",
                        rmsd_output,
                        "-tu",
                        time_unit,
                        "-fit",
                        "rot+trans",
                        *_index_arg(index_file),
                        *_analysis_window_args(params, time_unit),
                    ],
                ),
                stdin_text=_selection_text(fit_group, rmsd_group),
                outputs=[rmsd_output],
            )
        )
        if trajectory_prepared or not needs_structure or rmsd_input != trajectory:
            steps.append(_internal_step("Remove intermediate RMSD trajectory", "remove_files", {"paths": [rmsd_whole_trajectory]}))
        else:
            # Reuse the full-system, unrotated RMSD preparation for the other
            # metrics instead of writing another large trajectory.
            prepared_trajectory = rmsd_whole_trajectory
            intermediates.append(rmsd_whole_trajectory)
    if needs_structure and not trajectory_prepared and prepared_trajectory == trajectory:
        prepared_trajectory = f"{Path(trajectory).stem}-analysis-whole.xtc"
        steps.append(cluster_step(trajectory, prepared_trajectory, "Prepare whole-system trajectory for analysis"))
        intermediates.append(prepared_trajectory)

    if _bool_param(params, "do_rg", True):
        rg_output = _output_name_param(params, "rg_output", "gyrate.xvg", "Radius of gyration output")
        steps.append(
            Step(
                "Compute radius of gyration",
                _as_gmx(
                    gmx_bin,
                    [
                        "gyrate",
                        "-s",
                        tpr,
                        "-f",
                        prepared_trajectory,
                        "-o",
                        rg_output,
                        "-sel",
                        _str_param(params, "rg_selection", "Protein"),
                        "-tu",
                        time_unit,
                        *_index_arg(index_file),
                    ],
                ),
                outputs=[rg_output],
            )
        )

    if _bool_param(params, "do_dssp", False):
        dssp_output = _output_name_param(params, "dssp_output", "dssp.dat", "DSSP output")
        dssp_num_output = _output_name_param(params, "dssp_num_output", "dssp_num.xvg", "DSSP number output")
        steps.append(
            Step(
                "Assign secondary structure with DSSP",
                _as_gmx(
                    gmx_bin,
                    [
                        "dssp",
                        "-s",
                        tpr,
                        "-f",
                        prepared_trajectory,
                        "-tu",
                        time_unit,
                        "-o",
                        dssp_output,
                        "-num",
                        dssp_num_output,
                        *_index_arg(index_file),
                    ],
                ),
                outputs=[dssp_output, dssp_num_output],
            )
        )

    if _bool_param(params, "do_hbond", True):
        hbond_pairs = [
            (
                "mainchain",
                _str_param(params, "hbond_main_a", "MainChain+H"),
                _str_param(params, "hbond_main_b", "MainChain+H"),
                "hbnum_mainchain.xvg",
            ),
            (
                "sidechain",
                _str_param(params, "hbond_side_a", "SideChain"),
                _str_param(params, "hbond_side_b", "SideChain"),
                "hbnum_sidechain.xvg",
            ),
            ("protein-water", _str_param(params, "hbond_pw_a", "Protein"), _str_param(params, "hbond_pw_b", "Water"), "hbnum_prot_wat.xvg"),
        ]
        for label, group_a, group_b, output in hbond_pairs:
            if not group_a or not group_b:
                continue
            steps.append(
                Step(
                    f"Compute hydrogen bonds: {label}",
                    _as_gmx(
                        gmx_bin,
                        ["hbond-legacy", "-s", tpr, "-f", prepared_trajectory, "-tu", time_unit, "-num", output, *_index_arg(index_file)],
                    ),
                    stdin_text=_selection_text(group_a, group_b),
                    outputs=[output],
                )
            )

    if do_pca:
        steps.extend(
            [
                Step(
                    "Build covariance matrix",
                    _as_gmx(
                        gmx_bin,
                        [
                            "covar",
                            "-s",
                            tpr,
                            "-f",
                            prepared_trajectory,
                            "-fit",
                            "-o",
                            "eigenvalues.xvg",
                            "-v",
                            "eigenvectors.trr",
                            "-xpma",
                            "covapic.xpm",
                            *_index_arg(index_file),
                        ],
                    ),
                    stdin_text=_selection_text(
                        _str_param(params, "pca_align_group", "C-alpha"), _str_param(params, "pca_group", "C-alpha")
                    ),
                    outputs=["eigenvalues.xvg", "eigenvectors.trr", "covapic.xpm"],
                ),
                Step(
                    "Project PC1",
                    _as_gmx(
                        gmx_bin,
                        [
                            "anaeig",
                            "-s",
                            tpr,
                            "-f",
                            prepared_trajectory,
                            "-v",
                            "eigenvectors.trr",
                            "-first",
                            "1",
                            "-last",
                            "1",
                            "-proj",
                            "pc1.xvg",
                            *_index_arg(index_file),
                        ],
                    ),
                    stdin_text=_selection_text(_str_param(params, "pca_group", "C-alpha")),
                    outputs=["pc1.xvg"],
                ),
                Step(
                    "Project PC2",
                    _as_gmx(
                        gmx_bin,
                        [
                            "anaeig",
                            "-s",
                            tpr,
                            "-f",
                            prepared_trajectory,
                            "-v",
                            "eigenvectors.trr",
                            "-first",
                            "2",
                            "-last",
                            "2",
                            "-proj",
                            "pc2.xvg",
                            *_index_arg(index_file),
                        ],
                    ),
                    stdin_text=_selection_text(_str_param(params, "pca_group", "C-alpha")),
                    outputs=["pc2.xvg"],
                ),
                _internal_step(
                    "Combine PC projections",
                    "combine_pc_projection",
                    {"pc1": "pc1.xvg", "pc2": "pc2.xvg", "output": "pc12_sham_1.xvg"},
                    outputs=["pc12_sham_1.xvg"],
                ),
            ]
        )

    if do_sham:
        steps.append(
            Step(
                "Build Gibbs landscape with SHAM",
                _as_gmx(
                    gmx_bin,
                    [
                        "sham",
                        "-tsham",
                        _str_param(params, "sham_temp", "310"),
                        "-nlevels",
                        _str_param(params, "sham_levels", "100"),
                        "-f",
                        "pc12_sham_1.xvg",
                        "-ls",
                        "pc12_gibbs.xpm",
                        "-g",
                        "pc_12.log",
                        "-lsh",
                        "pc12_enthalpy.xpm",
                        "-lss",
                        "pc12_entropy.xpm",
                    ],
                ),
                outputs=["pc12_gibbs.xpm", "pc_12.log", "bindex.ndx"],
            )
        )

    if intermediates:
        steps.append(_internal_step("Remove intermediate analysis trajectory", "remove_files", {"paths": intermediates}))
    return steps
