# ruff: noqa: F401
# Compatibility facade: moved symbols remain importable by existing integrations.
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, stdev

from .files import ensure_under_root
from .forcefields import (  # noqa: F401 - preserve legacy gromacs imports
    KNOWN_WATER_MENU_SELECTIONS as KNOWN_WATER_MENU_SELECTIONS,
)
from .forcefields import (
    PDB2GMX_CLI_WATERS as PDB2GMX_CLI_WATERS,
)
from .forcefields import (
    copy_local_force_fields as copy_local_force_fields,
)
from .forcefields import (
    discover_local_force_fields as discover_local_force_fields,
)
from .forcefields import (
    force_fields_used_by_steps as force_fields_used_by_steps,
)
from .forcefields import (
    installed_water_model_selection as installed_water_model_selection,
)
from .forcefields import (
    local_force_field as local_force_field,
)
from .forcefields import (
    local_force_field_roots as _local_force_field_roots,
)
from .forcefields import (
    local_water_model_selection as local_water_model_selection,
)
from .forcefields import (
    normalize_force_field_name as normalize_force_field_name,
)
from .forcefields import (
    parse_watermodels as _parse_watermodels,
)
from .forcefields import (
    steps_use_acpype as steps_use_acpype,
)
from .forcefields import (
    unconfirmed_acpype_ligands as unconfirmed_acpype_ligands,
)
from .ligand_operations import (  # noqa: F401 - preserve legacy gromacs imports
    _COVALENT_RADII as _COVALENT_RADII,
)
from .ligand_operations import (
    append_ligand_molecule as _append_ligand_molecule,
)
from .ligand_operations import (
    copy_to as _copy_to,
)
from .ligand_operations import (
    infer_pose_bonds as _infer_pose_bonds,
)
from .ligand_operations import (
    insert_ligand_includes as _insert_ligand_includes,
)
from .ligand_operations import (
    isolate_acpype_output as _isolate_acpype_output,
)
from .ligand_operations import (
    itp_section_data_key as _itp_section_data_key,
)
from .ligand_operations import (
    itp_sections as _itp_sections,
)
from .ligand_operations import (
    map_pose_atoms as _map_pose_atoms,
)
from .ligand_operations import (
    merge_ligand_atomtypes as _merge_ligand_atomtypes,
)
from .ligand_operations import (
    parse_moleculetype as _parse_moleculetype,
)
from .ligand_operations import (
    patch_topology as _patch_topology,
)
from .ligand_operations import (
    pdb_pose_atoms as _pdb_pose_atoms,
)
from .ligand_operations import (
    prepare_ligand_chemistry as _prepare_ligand_chemistry,
)
from .ligand_operations import (
    read_sdf_atoms as _read_sdf_atoms,
)
from .ligand_operations import (
    read_sdf_heavy as _read_sdf_heavy,
)
from .ligand_operations import (
    restore_ligand_pose_coordinates as _restore_ligand_pose_coordinates,
)
from .ligand_operations import (
    stage_acpype_ligand as _stage_acpype_ligand,
)
from .ligand_operations import (
    stage_prepared_ligand as _stage_prepared_ligand,
)
from .ligand_operations import (
    write_ligand_smiles as _write_ligand_smiles,
)
from .mdp import (  # noqa: F401 - preserve legacy gromacs imports
    MDP_ALL_PREP_STAGES as MDP_ALL_PREP_STAGES,
)
from .mdp import (
    MDP_DYNAMICS_STAGES as MDP_DYNAMICS_STAGES,
)
from .mdp import (
    MDP_PROFILE_DIRS as MDP_PROFILE_DIRS,
)
from .mdp import (
    MDP_STAGE_FILES as MDP_STAGE_FILES,
)
from .mdp import (
    MDP_TEMPLATES as MDP_TEMPLATES,
)
from .mdp import (
    WORKFLOW_MDP_PROFILES as WORKFLOW_MDP_PROFILES,
)
from .mdp import (
    apply_mdp_overrides as apply_mdp_overrides,
)
from .mdp import (
    format_mdp_number as _format_mdp_number,
)
from .mdp import (
    load_mdp_folder as _load_mdp_folder,
)
from .mdp import (
    load_mdp_templates as load_mdp_templates,
)
from .mdp import (
    mdp_override_add as _mdp_override_add,
)
from .mdp import (
    mdp_profile_for_workflow as mdp_profile_for_workflow,
)
from .mdp import (
    nsteps_from_ns as _nsteps_from_ns,
)
from .mdp import (
    optional_float as _optional_float,
)
from .mdp import (
    optional_text as _optional_text,
)
from .mdp import (
    patch_equilibration_defines as _patch_equilibration_defines,
)
from .mdp import (
    patch_mdp_key as _patch_mdp_key,
)
from .mdp import (
    prepare_npt_mdp as _prepare_npt_mdp,
)
from .mdp import (
    prepare_replica_mdp as _prepare_replica_mdp,
)
from .mdp import (
    prepare_unrestrained_npt_mdp as _prepare_unrestrained_npt_mdp,
)
from .mdp import (
    preview_mdp_overrides as preview_mdp_overrides,
)
from .mdp import (
    preview_protocol as preview_protocol,
)
from .mdp import (
    read_mdp_values as _read_mdp_values,
)
from .mdp import (
    repeat_for_tc_groups as _repeat_for_tc_groups,
)
from .mdp import (
    tc_group_count as _tc_group_count,
)
from .mdp import (
    write_missing_mdp_files as write_missing_mdp_files,
)
from .steps import (  # noqa: F401 - preserve legacy gromacs imports
    Step as Step,
)
from .steps import (
    internal_step as _internal_step,
)
from .structure import (  # noqa: F401 - preserve legacy gromacs imports
    PDB_ION_RESIDUES as PDB_ION_RESIDUES,
)
from .structure import (
    PDB_WATER_RESIDUES as PDB_WATER_RESIDUES,
)
from .structure import (
    clean_pdb_for_pdb2gmx as _clean_pdb_for_pdb2gmx,
)
from .structure import (
    conect_serials as _conect_serials,
)
from .structure import (
    create_complex_index as _create_complex_index,
)
from .structure import (
    format_removed_records as _format_removed_records,
)
from .structure import (
    is_pdb_atom_record as _is_pdb_atom_record,
)
from .structure import (
    is_pdb_ion as _is_pdb_ion,
)
from .structure import (
    is_pdb_ligand as _is_pdb_ligand,
)
from .structure import (
    is_pdb_water as _is_pdb_water,
)
from .structure import (
    merge_gro as _merge_gro,
)
from .structure import (
    pdb_atom_serial as _pdb_atom_serial,
)
from .structure import (
    pdb_residue_name as _pdb_residue_name,
)
from .structure import (
    read_gro as _read_gro,
)
from .structure import (
    split_complex_pdb as _split_complex_pdb,
)
from .structure import (
    structure_preflight as _structure_preflight,
)
from .structure import (
    write_index_group as _write_index_group,
)
from .structure import (
    write_pdb_subset as _write_pdb_subset,
)
from .workflow_parameters import (  # noqa: F401 - preserve legacy gromacs imports
    bool_param as _bool_param,
)
from .workflow_parameters import (
    directory_slug as directory_slug,
)
from .workflow_parameters import (
    file_param as _file_param,
)
from .workflow_parameters import (
    find_first as _find_first,
)
from .workflow_parameters import (
    float_param as _float_param,
)
from .workflow_parameters import (
    int_param as _int_param,
)
from .workflow_parameters import (
    maxwarn_args as _maxwarn_args,
)
from .workflow_parameters import (
    output_name_param as _output_name_param,
)
from .workflow_parameters import (
    slugify as slugify,
)
from .workflow_parameters import (
    str_param as _str_param,
)
from .workflow_parameters import (
    topology_file as _topology,
)
from .workflows.common import (  # noqa: F401 - preserve legacy gromacs imports
    ALLOWED_SUBCOMMANDS as ALLOWED_SUBCOMMANDS,
)
from .workflows.common import (
    as_gmx as _as_gmx,
)
from .workflows.common import (
    clean_pdb_step as _clean_pdb_step,
)
from .workflows.common import (
    index_arg as _index_arg,
)
from .workflows.common import (
    ion_args as _ion_args,
)
from .workflows.common import (
    maybe_maxwarn as _maybe_maxwarn,
)
from .workflows.common import (
    mdrun_args as _mdrun_args,
)
from .workflows.common import (
    parse_custom_command as parse_custom_command,
)
from .workflows.common import (
    pdb2gmx_source_steps as _pdb2gmx_source_steps,
)
from .workflows.common import (
    pdb2gmx_step as _pdb2gmx_step,
)
from .workflows.common import (
    pdb2gmx_water_choice as _pdb2gmx_water_choice,
)
from .workflows.common import (
    production_steps as _production_steps,
)
from .workflows.common import (
    restraint_release_steps as _restraint_release_steps,
)
from .workflows.common import (
    selection_text as _selection_text,
)
from .workflows.common import (
    should_clean_pdb_for_pdb2gmx as _should_clean_pdb_for_pdb2gmx,
)
from .workflows.common import (
    solvent_template as _solvent_template,
)
from .workflows.common import (
    split_complex_pdb_multi_step as _split_complex_pdb_multi_step,
)
from .workflows.common import (
    split_complex_pdb_step as _split_complex_pdb_step,
)
from .workflows.common import (
    structure_preflight_step as _structure_preflight_step,
)
from .workflows.common import (
    thermodynamic_gate_steps as _thermodynamic_gate_steps,
)

TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    return json.loads(path.read_text(encoding="utf-8"))


def default_settings(base_dir: Path) -> dict:
    return {
        "gmx_bin": os.environ.get("GMX_BIN", "gmx"),
        "runtime_root": os.environ.get("GROMACS_WEB_ROOT", str((base_dir / "runtime").resolve())),
        "max_parallel": int(os.environ.get("GROMACS_WEB_MAX_PARALLEL", "1")),
        "default_ntmpi": int(os.environ.get("GMX_NTMPI", "1")),
        "default_ntomp": int(os.environ.get("GMX_NTOMP", "4")),
    }


def load_settings(base_dir: Path) -> dict:
    path = base_dir / "settings.json"
    settings = default_settings(base_dir)
    settings.update(read_json(path, {}))
    return settings


def save_settings(base_dir: Path, settings: dict) -> dict:
    current = load_settings(base_dir)
    for key in ("gmx_bin", "max_parallel", "default_ntmpi", "default_ntomp"):
        if key in settings:
            current[key] = settings[key]
    current["max_parallel"] = max(1, int(current["max_parallel"]))
    current["default_ntmpi"] = max(1, int(current["default_ntmpi"]))
    current["default_ntomp"] = max(1, int(current["default_ntomp"]))
    write_json(base_dir / "settings.json", current)
    return current


def detect_gromacs(gmx_bin: str) -> dict:
    result = {
        "available": False,
        "binary": gmx_bin,
        "version": None,
        "message": "not found",
    }
    if shutil.which(gmx_bin) is None and not Path(gmx_bin).exists():
        return result
    try:
        proc = subprocess.run(
            [gmx_bin, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive platform boundary
        result["message"] = str(exc)
        return result
    first_line = next((line.strip() for line in proc.stdout.splitlines() if line.strip()), "")
    result.update(
        {
            "available": proc.returncode == 0,
            "version": first_line or "unknown",
            "message": first_line or proc.stdout[-180:],
        }
    )
    return result


WORKFLOWS = {
    "protein_md": {
        "name": "Protein MD",
        "badge": "prep + em + nvt + npt + md",
        "requires": ["pdb or gro"],
    },
    "protein_ligand_md": {
        "name": "Protein-Ligand MD",
        "badge": "acpype/prepared + complex + index + md",
        "requires": ["protein pdb", "ligand sdf/mol2 or ligand gro+itp"],
    },
    "em_only": {
        "name": "Energy Minimization",
        "badge": "grompp + mdrun",
        "requires": ["gro/pdb", "topol.top", "minim.mdp"],
    },
    "run_tpr": {
        "name": "Run TPR",
        "badge": "mdrun",
        "requires": ["tpr"],
    },
    "analysis_rmsd": {
        "name": "RMSD Analysis",
        "badge": "rms",
        "requires": ["tpr", "xtc/trr"],
    },
    "postprocess": {
        "name": "Trajectory Postprocess",
        "badge": "trjconv center + fit + dump",
        "requires": ["tpr", "xtc/trr"],
    },
    "analysis_suite": {
        "name": "Analysis Suite",
        "badge": "energy + rmsd + rg + dssp + hbond + pca",
        "requires": ["tpr", "xtc/trr"],
    },
    "custom": {
        "name": "Custom gmx Command",
        "badge": "validated argv",
        "requires": ["custom command"],
    },
}


def _normalize_interaction_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _final_newline(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"


def parse_stdin_overrides(text: str) -> dict[str, str]:
    overrides: dict[str, list[str]] = {}
    current_title = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        bracket_match = re.match(r"^\[(.+)\]$", stripped)
        heading_match = re.match(r"^#{1,6}\s+(.+)$", stripped)
        if bracket_match or heading_match:
            current_title = _normalize_interaction_title((bracket_match or heading_match).group(1))
            if current_title:
                overrides.setdefault(current_title, [])
            continue
        if current_title:
            overrides[current_title].append(raw_line)

    return {title: _final_newline("\n".join(lines).strip("\n")) for title, lines in overrides.items() if "\n".join(lines).strip()}


def apply_stdin_overrides(steps: list[Step], params: dict) -> list[Step]:
    if not _bool_param(params, "custom_stdin", False):
        return steps
    overrides = parse_stdin_overrides(_str_param(params, "stdin_overrides", ""))
    if not overrides:
        return steps
    for step in steps:
        override = overrides.get(_normalize_interaction_title(step.title))
        if override is not None:
            step.stdin_text = override
    return steps


def _validate_stage_log(workdir: Path, data: dict) -> None:
    log_path = ensure_under_root(workdir / str(data["log_file"]), workdir)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    lower = text.lower()
    failures: list[str] = []
    if "lincs warning" in lower or "constraint warning" in lower:
        failures.append("constraint/LINCS warning detected")
    if re.search(r"(?<![a-z])nan(?![a-z])", lower):
        failures.append("NaN detected")
    stage = str(data.get("stage", "stage"))
    report_lines = [f"stage={stage}", f"log={log_path.name}"]
    if stage == "em":
        converged = "converged to fmax" in lower
        report_lines.append(f"converged_to_fmax={'yes' if converged else 'no'}")
        if not converged:
            failures.append("energy minimization did not converge to Fmax")
    report_lines.append(f"status={'FAIL' if failures else 'PASS'}")
    report_lines.extend(f"failure={item}" for item in failures)
    output = ensure_under_root(workdir / str(data["output_report"]), workdir)
    output.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    if failures:
        raise ValueError(f"{stage} quality gate failed: {'; '.join(failures)}")


def _read_xvg(path: Path) -> tuple[list[str], list[list[float]]]:
    """Read scientific gate/statistics inputs strictly; plotting has its own reader."""
    legend_entries: dict[int, str] = {}
    rows: list[list[float]] = []
    columns = None
    legend_pattern = re.compile(r'^@\s+s(\d+)\s+legend\s+"([^"]+)"')
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        match = legend_pattern.match(line)
        if match:
            index = int(match.group(1))
            if index in legend_entries or match.group(2) in legend_entries.values():
                raise ValueError(f"{path.name}:{line_number}: duplicate XVG series legend")
            legend_entries[index] = match.group(2)
            continue
        if not line or line[0] in "#@":
            continue
        try:
            values = [float(value) for value in line.split()]
        except ValueError as error:
            raise ValueError(f"{path.name}:{line_number}: invalid numeric XVG row") from error
        if len(values) < 2:
            raise ValueError(f"{path.name}:{line_number}: XVG data needs a time/coordinate and at least one value")
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"{path.name}:{line_number}: non-finite XVG data (NaN/Inf)")
        if columns is not None and len(values) != columns:
            raise ValueError(f"{path.name}:{line_number}: inconsistent XVG columns (expected {columns}, found {len(values)})")
        columns = len(values)
        rows.append(values)
    if len(rows) < 10:
        raise ValueError(f"insufficient numeric samples in {path.name}")
    if legend_entries and max(legend_entries) >= columns - 1:
        raise ValueError(f"{path.name}: XVG legend refers to a missing numeric column")
    legends = [legend_entries.get(index, f"series_{index + 1}") for index in range(columns - 1)]
    return legends, rows


def _series_stats(values: list[float]) -> dict[str, float]:
    count = len(values)
    block_count = min(count, 10, max(2, count // 5))
    # Integer division previously dropped remainder samples from SEM. These
    # contiguous blocks cover every value, with sizes differing by at most one.
    boundaries = [index * count // block_count for index in range(block_count + 1)]
    blocks = [fmean(values[begin:end]) for begin, end in zip(boundaries, boundaries[1:])]
    sem = stdev(blocks) / math.sqrt(len(blocks)) if len(blocks) > 1 else 0.0
    quarter = max(1, count // 4)
    drift = fmean(values[-quarter:]) - fmean(values[:quarter])
    return {"mean": fmean(values), "std": stdev(values) if count > 1 else 0.0, "blocked_sem": sem, "drift": drift, "samples": count}


def _validate_thermodynamics(workdir: Path, data: dict) -> None:
    path = ensure_under_root(workdir / str(data["xvg"]), workdir)
    try:
        legends, rows = _read_xvg(path)
    except ValueError as error:
        ensure_under_root(workdir / str(data["output"]), workdir).write_text(
            json.dumps({"source": path.name, "series": {}, "failures": [str(error)], "status": "FAIL"}, indent=2) + "\n",
            encoding="utf-8",
        )
        raise ValueError(f"thermodynamic quality gate failed: {error}") from error
    start = max(1, len(rows) // 5)
    production = rows[start:]
    stats = {
        legend: _series_stats([row[index + 1] for row in production])
        for index, legend in enumerate(legends)
        if index + 1 < len(production[0])
    }
    failures: list[str] = []
    target = float(data.get("target_temperature", 300.0))
    target_source = None
    if data.get("source_mdp"):
        mdp = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
        settings = _read_mdp_values(mdp.read_text(encoding="utf-8"))
        try:
            targets = [float(value) for value in settings.get("ref_t", "").split()]
        except ValueError:
            targets = []
        if not targets or any(not math.isfinite(value) or value <= 0 for value in targets):
            raise ValueError(f"Invalid or missing ref_t in {mdp.name}; cannot validate temperature")
        if any(abs(value - targets[0]) > 1e-6 for value in targets):
            raise ValueError(f"Different ref_t targets in {mdp.name} require temperature checks per coupling group")
        target = targets[0]
        target_source = mdp.name
    temperature = next((value for key, value in stats.items() if "temperature" in key.lower()), None)
    density = next((value for key, value in stats.items() if "density" in key.lower()), None)
    volume = next((value for key, value in stats.items() if "volume" in key.lower()), None)
    for label, series in (("Temperature", temperature), ("Density", density), ("Volume", volume)):
        if series is None:
            failures.append(f"required observable {label} is missing")
    if temperature and abs(temperature["mean"] - target) > 5.0:
        failures.append(f"mean temperature {temperature['mean']:.2f} K differs from target {target:.2f} K")
    if density:
        if not 850.0 <= density["mean"] <= 1200.0:
            failures.append(f"aqueous density {density['mean']:.2f} kg/m^3 is implausible")
        if abs(density["drift"]) > max(10.0, density["mean"] * 0.02):
            failures.append(f"density drift {density['drift']:.2f} kg/m^3 has not stabilized")
    if volume and abs(volume["drift"]) > max(volume["mean"] * 0.03, 1e-9):
        failures.append(f"volume drift {volume['drift']:.3g} exceeds 3%")
    report = {
        "source": path.name,
        "discarded_fraction": 0.2,
        "target_temperature": target,
        "target_source_mdp": target_source,
        "series": stats,
        "failures": failures,
        "status": "FAIL" if failures else "PASS",
    }
    ensure_under_root(workdir / str(data["output"]), workdir).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if failures and bool(data.get("strict", True)):
        raise ValueError("thermodynamic quality gate failed: " + "; ".join(failures))


def _summarize_replicas(workdir: Path, data: dict) -> None:
    replicas = []
    for name in data.get("inputs", []):
        path = ensure_under_root(workdir / str(name), workdir)
        legends, rows = _read_xvg(path)
        start = max(1, len(rows) // 5)
        replicas.append(
            {
                "file": path.name,
                "series": {
                    legend: _series_stats([row[index + 1] for row in rows[start:]])
                    for index, legend in enumerate(legends)
                    if index + 1 < len(rows[0])
                },
            }
        )
    combined: dict[str, dict] = {}
    keys = sorted({key for replica in replicas for key in replica["series"]})
    for key in keys:
        means = [replica["series"][key]["mean"] for replica in replicas if key in replica["series"]]
        combined[key] = {
            "replica_mean": fmean(means),
            "between_replica_sd": stdev(means) if len(means) > 1 else None,
            "replicas": len(means),
        }
    report = {
        "method": "first 20% discarded; within-replica SEM of up to 10 contiguous blocks covering every retained sample; between-replica SD",
        "replicas": replicas,
        "combined": combined,
        "limitations": "Block sizes differ by at most one sample. This fixed-block SEM does not estimate autocorrelation or effective sample size; "
        "thermodynamic convergence does not prove configurational convergence. Inspect target structural observables separately.",
    }
    ensure_under_root(workdir / str(data["output"]), workdir).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _read_xvg_xy(path: Path) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "@")):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            values[parts[0]] = float(parts[1])
    return values


def _combine_pc_projection(workdir: Path, data: dict) -> None:
    pc1 = _read_xvg_xy(ensure_under_root(workdir / str(data["pc1"]), workdir))
    pc2 = _read_xvg_xy(ensure_under_root(workdir / str(data["pc2"]), workdir))
    rows = []
    for time_key in sorted(set(pc1) & set(pc2), key=lambda value: float(value)):
        rows.append(f"{time_key} {pc1[time_key]} {pc2[time_key]}")
    if not rows:
        raise ValueError("No overlapping PC projection frames found")
    ensure_under_root(workdir / str(data["output"]), workdir).write_text("\n".join(rows) + "\n", encoding="utf-8")


def _remove_files(workdir: Path, data: dict) -> None:
    for relative_path in data.get("paths", []):
        path = ensure_under_root(workdir / str(relative_path), workdir)
        if path.is_file():
            path.unlink()


def _generate_analysis_figures(workdir: Path, data: dict) -> None:
    from scripts.md_plot import generate_directory

    generate_directory(workdir, str(data.get("output_dir") or "figures"), int(data.get("dpi") or 1600))


def _summarize_benchmarks(workdir: Path, data: dict) -> None:
    performance_pattern = re.compile(r"\bPerformance:\s*([0-9.eE+\-]+)", re.IGNORECASE)
    results = []
    for profile in data.get("profiles") or []:
        log_path = ensure_under_root(workdir / str(profile["log"]), workdir)
        matches = performance_pattern.findall(log_path.read_text(encoding="utf-8", errors="replace")) if log_path.is_file() else []
        results.append(
            {
                "ntmpi": int(profile["ntmpi"]),
                "ntomp": int(profile["ntomp"]),
                "ns_per_day": float(matches[-1]) if matches else None,
                "log": log_path.name,
            }
        )
    valid = [item for item in results if item["ns_per_day"] is not None]
    best = max(valid, key=lambda item: item["ns_per_day"]) if valid else None
    payload = {"results": results, "best": best, "nsteps": int(data["nsteps"])}
    write_json(ensure_under_root(workdir / str(data["json_output"]), workdir), payload)
    rows = ["GROMACS thread benchmark", f"nsteps per profile: {payload['nsteps']}", ""]
    rows.extend(
        f"ntmpi={item['ntmpi']} ntomp={item['ntomp']} performance={item['ns_per_day'] if item['ns_per_day'] is not None else 'unavailable'} ns/day"
        for item in results
    )
    if best:
        rows.extend(["", f"BEST ntmpi={best['ntmpi']} ntomp={best['ntomp']} performance={best['ns_per_day']} ns/day"])
    ensure_under_root(workdir / str(data["text_output"]), workdir).write_text("\n".join(rows) + "\n", encoding="utf-8")


def execute_internal_step(workdir: Path, step: Step) -> None:
    if step.operation == "structure_preflight":
        _structure_preflight(workdir, step.data)
    elif step.operation == "clean_pdb_for_pdb2gmx":
        _clean_pdb_for_pdb2gmx(workdir, step.data)
    elif step.operation == "split_complex_pdb":
        _split_complex_pdb(workdir, step.data)
    elif step.operation == "write_ligand_smiles":
        _write_ligand_smiles(workdir, step.data)
    elif step.operation == "prepare_ligand_chemistry":
        _prepare_ligand_chemistry(workdir, step.data)
    elif step.operation == "stage_prepared_ligand":
        _stage_prepared_ligand(workdir, step.data)
    elif step.operation == "stage_acpype_ligand":
        _stage_acpype_ligand(workdir, step.data)
    elif step.operation == "restore_ligand_pose_coordinates":
        _restore_ligand_pose_coordinates(workdir, step.data)
    elif step.operation == "isolate_acpype_output":
        _isolate_acpype_output(workdir, step.data)
    elif step.operation == "merge_ligand_atomtypes":
        _merge_ligand_atomtypes(workdir, step.data)
    elif step.operation == "merge_gro":
        _merge_gro(workdir, step.data)
    elif step.operation == "create_complex_index":
        _create_complex_index(workdir, step.data)
    elif step.operation == "patch_equilibration_defines":
        _patch_equilibration_defines(workdir, step.data)
    elif step.operation == "validate_stage_log":
        _validate_stage_log(workdir, step.data)
    elif step.operation == "prepare_npt_mdp":
        _prepare_npt_mdp(workdir, step.data)
    elif step.operation == "validate_thermodynamics":
        _validate_thermodynamics(workdir, step.data)
    elif step.operation == "prepare_replica_mdp":
        _prepare_replica_mdp(workdir, step.data)
    elif step.operation == "prepare_unrestrained_npt_mdp":
        _prepare_unrestrained_npt_mdp(workdir, step.data)
    elif step.operation == "summarize_replicas":
        _summarize_replicas(workdir, step.data)
    elif step.operation == "patch_topology":
        _patch_topology(workdir, step.data)
    elif step.operation == "combine_pc_projection":
        _combine_pc_projection(workdir, step.data)
    elif step.operation == "remove_files":
        _remove_files(workdir, step.data)
    elif step.operation == "generate_analysis_figures":
        _generate_analysis_figures(workdir, step.data)
    elif step.operation == "summarize_benchmarks":
        _summarize_benchmarks(workdir, step.data)
    else:
        raise ValueError(f"Unknown internal operation: {step.operation}")


def __getattr__(name: str):
    """Load moved public symbols lazily without creating import cycles."""
    if name == "JobStore":
        from .job_store import JobStore

        return JobStore
    if name == "xvg_plots":
        from .analysis.xvg import xvg_plots

        return xvg_plots
    if name in {"build_steps", "_analysis_steps", "_classic_complex_analysis_steps", "_postprocess_steps"}:
        from . import workflows

        return getattr(workflows, name)
    raise AttributeError(name)
