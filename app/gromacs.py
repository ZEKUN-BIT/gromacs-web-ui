from __future__ import annotations

import itertools
import json
import math
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean, stdev
from typing import Iterable

from .files import ensure_under_root

TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
PDB2GMX_CLI_WATERS = {"select", "none", "spc", "spce", "tip3p", "tip4p", "tip5p", "tips3p"}
KNOWN_WATER_MENU_SELECTIONS = {"amber19sb": {"opc": "1", "opc3": "2", "tip4pew": "6"}}
PDB_WATER_RESIDUES = {"HOH", "WAT", "SOL", "H2O", "TIP3", "TIP3P", "TIP4P", "OPC", "OPC3"}
PDB_ION_RESIDUES = {
    "AG",
    "AL",
    "BA",
    "BR",
    "CA",
    "CAL",
    "CD",
    "CL",
    "CLA",
    "CO",
    "CS",
    "CU",
    "F",
    "FE",
    "FE2",
    "FE3",
    "HG",
    "IOD",
    "K",
    "LI",
    "MG",
    "MG2",
    "MN",
    "NA",
    "NI",
    "PB",
    "POT",
    "RB",
    "SOD",
    "SR",
    "ZN",
}

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


@dataclass
class Step:
    title: str
    args: list[str]
    stdin_text: str | None = None
    outputs: list[str] = field(default_factory=list)
    kind: str = "command"
    operation: str | None = None
    data: dict = field(default_factory=dict)
    parallel_group: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str, fallback: str = "job") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return value[:64] or fallback


def directory_slug(value: str, fallback: str = "job") -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-._")
    return value[:48] or fallback


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


def normalize_force_field_name(value: str | None, default: str = "amber19sb") -> str:
    raw = str(value or "").strip() or default
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name.lower().endswith(".ff"):
        name = name[:-3]
    return name or default


def _local_force_field_roots(base_dir: Path) -> list[Path]:
    return [base_dir, base_dir / "forcefields"]


def _parse_watermodels(path: Path) -> list[dict]:
    if not path.exists():
        return []
    models: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 2)
        name = parts[0]
        label = parts[1] if len(parts) > 1 else name
        description = parts[2] if len(parts) > 2 else ""
        index = len(models) + 1
        models.append({"name": name, "label": label, "description": description, "selection": str(index)})
    return models


def discover_local_force_fields(base_dir: Path) -> list[dict]:
    fields: dict[str, dict] = {}
    for root in _local_force_field_roots(base_dir):
        if not root.exists() or not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or not child.name.lower().endswith(".ff"):
                continue
            source = ensure_under_root(child, base_dir)
            name = normalize_force_field_name(child.name)
            key = name.lower()
            fields.setdefault(
                key,
                {
                    "name": name,
                    "directory": child.name,
                    "path": str(source),
                    "water_models": _parse_watermodels(source / "watermodels.dat"),
                },
            )
    return sorted(fields.values(), key=lambda item: item["name"].lower())


def local_force_field(base_dir: Path, force_field: str) -> dict | None:
    name = normalize_force_field_name(force_field)
    for item in discover_local_force_fields(base_dir):
        if item["name"].lower() == name.lower():
            return item
    return None


def local_water_model_selection(base_dir: Path, force_field: str, water_model: str) -> str | None:
    field = local_force_field(base_dir, force_field)
    if not field:
        return None
    requested = str(water_model or "").strip().lower()
    for model in field.get("water_models", []):
        aliases = {str(model.get("name", "")).lower(), str(model.get("label", "")).lower()}
        if requested in aliases:
            return str(model.get("selection") or "")
    return None


def installed_water_model_selection(gmx_bin: str, force_field: str, water_model: str) -> str | None:
    executable = Path(gmx_bin)
    if not executable.is_absolute():
        resolved = shutil.which(gmx_bin)
        if not resolved:
            return None
        executable = Path(resolved)
    try:
        prefix = executable.resolve().parent.parent
    except OSError:
        return None
    candidates = []
    gmxlibrary = os.environ.get("GMXLIB", "").strip()
    if gmxlibrary:
        candidates.append(Path(gmxlibrary))
    candidates.append(prefix / "share" / "gromacs" / "top")
    requested = str(water_model or "").strip().lower()
    for top_dir in candidates:
        models = _parse_watermodels(top_dir / f"{normalize_force_field_name(force_field)}.ff" / "watermodels.dat")
        for model in models:
            aliases = {str(model.get("name", "")).lower(), str(model.get("label", "")).lower()}
            if requested in aliases:
                return str(model["selection"])
    return None


def force_fields_used_by_steps(steps: Iterable[Step], fallback: str | None = None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    fallback_name = normalize_force_field_name(fallback) if fallback else ""
    for step in steps:
        if "pdb2gmx" not in step.args:
            continue
        value = fallback_name
        if "-ff" in step.args:
            index = step.args.index("-ff")
            if index + 1 < len(step.args):
                value = normalize_force_field_name(step.args[index + 1])
        if value and value.lower() not in seen:
            seen.add(value.lower())
            names.append(value)
    return names


def steps_use_acpype(steps: Iterable[Step]) -> bool:
    for step in steps:
        if step.operation == "acpype_ligand":
            return True
        if step.title == "Parameterize ligand with ACPYPE":
            return True
        if step.args and Path(step.args[0]).name.lower() == "acpype":
            return True
    return False


def unconfirmed_acpype_ligands(steps: Iterable[Step]) -> list[str]:
    """Ligand keys whose ACPYPE parameterization lacks a confirmed net charge."""
    keys: list[str] = []
    for step in steps:
        if step.operation != "acpype_ligand":
            continue
        key = str(step.data.get("ligand_key") or "lig")
        if not step.data.get("charge_confirmed") and key not in keys:
            keys.append(key)
    return keys


def copy_local_force_fields(base_dir: Path, workdir: Path, force_fields: Iterable[str]) -> list[dict]:
    available = {item["name"].lower(): item for item in discover_local_force_fields(base_dir)}
    copied: list[dict] = []
    for raw_name in force_fields:
        name = normalize_force_field_name(raw_name)
        source_info = available.get(name.lower())
        if not source_info:
            continue
        source = ensure_under_root(Path(source_info["path"]), base_dir)
        target = ensure_under_root(workdir / f"{name}.ff", workdir)
        if target.exists():
            copied.append({"name": name, "directory": target.name, "source": str(source), "target": str(target), "status": "present"})
            continue
        shutil.copytree(source, target)
        copied.append({"name": name, "directory": target.name, "source": str(source), "target": str(target), "status": "copied"})
    return copied


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


MDP_TEMPLATES = {
    "ions.mdp": """integrator  = steep
emtol       = 1000.0
emstep      = 0.01
nsteps      = 500
cutoff-scheme = Verlet
coulombtype = PME
rcoulomb    = 1.0
rvdw        = 1.0
pbc         = xyz
""",
    "minim.mdp": """integrator  = steep
emtol       = 1000.0
emstep      = 0.01
nsteps      = 50000
cutoff-scheme = Verlet
coulombtype = PME
rcoulomb    = 1.0
rvdw        = 1.0
pbc         = xyz
""",
    "nvt.mdp": """define      = -DPOSRES
integrator  = md
nsteps      = 50000
dt          = 0.002
nstxout-compressed = 500
nstenergy   = 500
nstlog      = 500
continuation = no
constraint_algorithm = lincs
constraints = h-bonds
cutoff-scheme = Verlet
coulombtype = PME
rcoulomb    = 1.0
rvdw        = 1.0
tcoupl      = V-rescale
tc-grps     = Protein Non-Protein
tau_t       = 0.1 0.1
ref_t       = 300 300
pcoupl      = no
pbc         = xyz
gen_vel     = yes
gen_temp    = 300
gen_seed    = -1
""",
    "npt.mdp": """define      = -DPOSRES
integrator  = md
nsteps      = 50000
dt          = 0.002
nstxout-compressed = 500
nstenergy   = 500
nstlog      = 500
continuation = yes
constraint_algorithm = lincs
constraints = h-bonds
cutoff-scheme = Verlet
coulombtype = PME
rcoulomb    = 1.0
rvdw        = 1.0
tcoupl      = V-rescale
tc-grps     = Protein Non-Protein
tau_t       = 0.1 0.1
ref_t       = 300 300
pcoupl      = Parrinello-Rahman
pcoupltype  = isotropic
tau_p       = 2.0
ref_p       = 1.0
compressibility = 4.5e-5
pbc         = xyz
gen_vel     = no
""",
    "md.mdp": """integrator  = md
nsteps      = 500000
dt          = 0.002
nstxout-compressed = 5000
nstenergy   = 1000
nstlog      = 1000
continuation = yes
constraint_algorithm = lincs
constraints = h-bonds
cutoff-scheme = Verlet
coulombtype = PME
rcoulomb    = 1.0
rvdw        = 1.0
tcoupl      = V-rescale
tc-grps     = Protein Non-Protein
tau_t       = 0.1 0.1
ref_t       = 300 300
pcoupl      = Parrinello-Rahman
pcoupltype  = isotropic
tau_p       = 2.0
ref_p       = 1.0
compressibility = 4.5e-5
pbc         = xyz
gen_vel     = no
""",
}


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


MDP_PROFILE_DIRS = {
    "protein": ("protein", "protein_md"),
    "complex": ("complex", "protein_ligand", "protein_ligand_md"),
}

WORKFLOW_MDP_PROFILES = {
    "protein_md": "protein",
    "em_only": "protein",
    "protein_ligand_md": "complex",
}


def mdp_profile_for_workflow(workflow: str | None) -> str | None:
    return WORKFLOW_MDP_PROFILES.get(str(workflow or "").strip())


def _load_mdp_folder(templates: dict[str, dict], folder: Path) -> None:
    if not folder.exists() or not folder.is_dir():
        return
    for path in sorted(folder.glob("*.mdp")):
        source = ensure_under_root(path, folder)
        templates[source.name] = {
            "content": source.read_text(encoding="utf-8", errors="replace"),
            "source": str(source),
        }


def load_mdp_templates(base_dir: Path | None = None, profile: str | None = None) -> dict[str, dict]:
    templates = {filename: {"content": content, "source": "built-in"} for filename, content in MDP_TEMPLATES.items()}
    if not base_dir:
        return templates

    _load_mdp_folder(templates, base_dir / "sample_mdp")
    _load_mdp_folder(templates, base_dir / "mdp")

    profile = str(profile or "").strip()
    for folder_name in MDP_PROFILE_DIRS.get(profile, ()):
        _load_mdp_folder(templates, base_dir / "mdp" / folder_name)
    return templates


def write_missing_mdp_files(workdir: Path, base_dir: Path | None = None, workflow: str | None = None) -> list[dict]:
    written: list[dict] = []
    for filename, template in load_mdp_templates(base_dir, mdp_profile_for_workflow(workflow)).items():
        target = ensure_under_root(workdir / filename, workdir)
        if target.exists():
            continue
        target.write_text(str(template["content"]), encoding="utf-8")
        written.append({"name": filename, "source": str(template["source"])})
    return sorted(written, key=lambda item: item["name"])


MDP_STAGE_FILES = {
    "ions": "ions.mdp",
    "minim": "minim.mdp",
    "nvt": "nvt.mdp",
    "npt": "npt.mdp",
    "md": "md.mdp",
}

MDP_DYNAMICS_STAGES = ("nvt", "npt", "md")
MDP_ALL_PREP_STAGES = ("ions", "minim", "nvt", "npt", "md")


def _patch_mdp_key(text: str, key: str, value: str | None) -> str:
    lines = text.splitlines()
    normalized_key = "".join("[-_]" if char in "-_" else re.escape(char) for char in key)
    pattern = re.compile(rf"^\s*{normalized_key}\s*=", re.IGNORECASE)
    replaced = False
    output: list[str] = []
    for line in lines:
        if pattern.match(line):
            replaced = True
            if value is not None:
                output.append(f"{key:<24}= {value}")
        else:
            output.append(line)
    if not replaced and value is not None:
        output.append(f"{key:<24}= {value}")
    return "\n".join(output).rstrip() + "\n"


def _read_mdp_values(text: str) -> dict[str, str]:
    settings = {}
    for line in text.splitlines():
        key, separator, value = line.split(";", 1)[0].partition("=")
        if separator:
            settings[key.strip().lower().replace("-", "_")] = value.strip()
    return settings


def preview_protocol(params: dict, base_dir: Path, uploaded_mdp: dict[str, str]) -> list[dict]:
    if params.get("workflow") not in {"protein_md", "protein_ligand_md"}:
        return []
    templates = load_mdp_templates(base_dir, mdp_profile_for_workflow(params.get("workflow")))
    plan = preview_mdp_overrides(params)
    stages = []
    if _bool_param(params, "pre_nvt", False):
        stages.append(("NVT", "nvt.mdp", 1))
    stages.append(("NPT", "npt.mdp", 1))
    if _bool_param(params, "release_restraints", True):
        stages.append(("NPT unrestrained", "npt.mdp", 1))
    stages.append(("Production", "md.mdp", _int_param(params, "replicas", 3)))
    rows = []
    for stage, filename, replicas in stages:
        text = uploaded_mdp.get(filename, templates[filename]["content"])
        source = "uploaded" if filename in uploaded_mdp else templates[filename]["source"]
        changed = []
        for item in plan:
            if item["file"] == filename:
                text = _patch_mdp_key(text, item["key"], None if item["value"] == "<remove>" else item["value"])
                changed.append(item["key"])
        values = _read_mdp_values(text)
        try:
            dt = float(values["dt"])
            duration = dt * int(values["nsteps"]) / 1000
            if not math.isfinite(dt) or not math.isfinite(duration) or dt <= 0 or duration <= 0:
                raise ValueError
        except (KeyError, ValueError):
            dt, duration = None, None
        rows.append(
            {
                "stage": stage,
                "file": filename,
                "source": source,
                "overrides": changed,
                "dt_ps": dt,
                "duration_ns": duration,
                "replicas": replicas,
                "temperature_k": values.get("ref_t", ""),
                "pressure_bar": values.get("ref_p", ""),
                "pcoupl": values.get("pcoupl", "no"),
            }
        )
    return rows


def _optional_text(params: dict, key: str) -> str:
    return str(params.get(key) or "").strip()


def _optional_float(params: dict, key: str, label: str, positive: bool = True) -> float | None:
    raw = params.get(key, "")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if positive and value <= 0:
        raise ValueError(f"{label} must be greater than 0.")
    if not positive and value < 0:
        raise ValueError(f"{label} must not be negative.")
    return value


def _format_mdp_number(value: float) -> str:
    return f"{value:.8g}"


def _tc_group_count(params: dict) -> int:
    groups = _optional_text(params, "mdp_tc_grps")
    if not groups:
        groups = "Protein_Lig Water_and_Ions" if params.get("workflow") == "protein_ligand_md" else "Protein Water_and_Ions"
    return max(1, len(groups.split()))


def _repeat_for_tc_groups(value: float, count: int) -> str:
    token = _format_mdp_number(value)
    return " ".join(token for _ in range(count))


def _nsteps_from_ns(duration_ns: float, dt_ps: float, label: str) -> int:
    steps = round((duration_ns * 1000.0) / dt_ps)
    if steps <= 0:
        raise ValueError(f"{label} produces zero MD steps; increase duration or reduce dt.")
    return steps


def _mdp_override_add(plan: list[dict], stage: str, key: str, value: str | None, label: str) -> None:
    plan.append(
        {
            "stage": stage,
            "file": MDP_STAGE_FILES[stage],
            "key": key,
            "value": value if value is not None else "<remove>",
            "label": label,
        }
    )


def preview_mdp_overrides(params: dict) -> list[dict]:
    if not _bool_param(params, "mdp_override_enabled", False):
        return []

    workflow = str(params.get("workflow") or "")
    if workflow not in {"protein_md", "protein_ligand_md"}:
        return []

    plan: list[dict] = []
    dt = _optional_float(params, "mdp_dt_ps", "MDP dt")
    if dt is not None:
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "dt", _format_mdp_number(dt), "Time step")

    for key, stage, label in (
        ("mdp_nvt_ns", "nvt", "NVT duration"),
        ("mdp_npt_ns", "npt", "NPT duration"),
        ("mdp_md_ns", "md", "Production duration"),
    ):
        duration = _optional_float(params, key, label)
        if duration is not None:
            if dt is None:
                raise ValueError(f"{label} requires setting MDP dt.")
            _mdp_override_add(plan, stage, "nsteps", str(_nsteps_from_ns(duration, dt, label)), label)

    group_count = _tc_group_count(params)
    temperature = _optional_float(params, "mdp_temperature_k", "MDP temperature")
    if temperature is not None:
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "ref_t", _repeat_for_tc_groups(temperature, group_count), "Temperature")
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "gen_temp", _format_mdp_number(temperature), "Initial velocity temperature")

    pressure = _optional_float(params, "mdp_pressure_bar", "MDP pressure", positive=False)
    if pressure is not None:
        for stage in ("npt", "md"):
            _mdp_override_add(plan, stage, "ref_p", _format_mdp_number(pressure), "Pressure")

    tc_grps = _optional_text(params, "mdp_tc_grps")
    if tc_grps:
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "tc-grps", tc_grps, "Temperature coupling groups")

    tau_t = _optional_float(params, "mdp_tau_t_ps", "MDP tau_t")
    if tau_t is not None:
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "tau_t", _repeat_for_tc_groups(tau_t, group_count), "Thermostat tau_t")

    define = _optional_text(params, "mdp_define_equil")
    if define:
        value = None if define == "__none__" else define
        for stage in ("nvt", "npt"):
            _mdp_override_add(plan, stage, "define", value, "Position restraint define")

    gen_vel = _optional_text(params, "mdp_gen_vel")
    if gen_vel in {"yes", "no"}:
        _mdp_override_add(plan, "nvt", "gen_vel", gen_vel, "Velocity generation")

    pcoupl = _optional_text(params, "mdp_pcoupl")
    if pcoupl:
        for stage in ("npt", "md"):
            _mdp_override_add(plan, stage, "pcoupl", pcoupl, "Pressure coupling")

    constraints = _optional_text(params, "mdp_constraints")
    if constraints:
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "constraints", constraints, "Constraints")

    for param_key, mdp_key, label in (
        ("mdp_rcoulomb", "rcoulomb", "Coulomb cutoff"),
        ("mdp_rvdw", "rvdw", "Van der Waals cutoff"),
    ):
        cutoff = _optional_float(params, param_key, label)
        if cutoff is not None:
            for stage in MDP_ALL_PREP_STAGES:
                _mdp_override_add(plan, stage, mdp_key, _format_mdp_number(cutoff), label)

    output_interval = _optional_float(params, "mdp_output_interval_ps", "MDP output interval")
    if output_interval is not None:
        if dt is None:
            raise ValueError("MDP output interval requires setting MDP dt.")
        interval_steps = max(1, round(output_interval / dt))
        for stage in MDP_DYNAMICS_STAGES:
            _mdp_override_add(plan, stage, "nstenergy", str(interval_steps), "Energy output interval")
            _mdp_override_add(plan, stage, "nstlog", str(interval_steps), "Log output interval")
        _mdp_override_add(plan, "md", "nstxout-compressed", str(interval_steps), "Compressed trajectory interval")

    return plan


def apply_mdp_overrides(workdir: Path, params: dict) -> list[dict]:
    plan = preview_mdp_overrides(params)
    if not plan:
        return []

    by_file: dict[str, list[dict]] = {}
    for item in plan:
        by_file.setdefault(str(item["file"]), []).append(item)

    for filename, items in by_file.items():
        path = ensure_under_root(workdir / filename, workdir)
        if not path.exists():
            raise FileNotFoundError(f"MDP file not found for override: {filename}")
        text = path.read_text(encoding="utf-8", errors="replace")
        for item in items:
            value = None if item["value"] == "<remove>" else str(item["value"])
            text = _patch_mdp_key(text, str(item["key"]), value)
        path.write_text(text, encoding="utf-8")
    return plan


def _find_first(files: Iterable[str], suffixes: tuple[str, ...]) -> str | None:
    for name in files:
        if name.lower().endswith(suffixes):
            return name
    return None


def _topology(files: Iterable[str]) -> str:
    return _find_first(files, (".top",)) or "topol.top"


def _maxwarn_args(maxwarn: int) -> list[str]:
    return ["-maxwarn", str(max(0, int(maxwarn)))] if int(maxwarn) > 0 else []


def _as_gmx(gmx_bin: str, args: list[str]) -> list[str]:
    return [gmx_bin, *args]


def _bool_param(params: dict, key: str, default: bool = False) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _str_param(params: dict, key: str, default: str = "") -> str:
    value = params.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _output_name_param(params: dict, key: str, default: str, label: str) -> str:
    value = _str_param(params, key, default)
    if value in {".", ".."} or ".." in value or "/" in value or "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"{label} must be a file name only, without absolute paths, '..', '/', or '\\'.")
    return value


def _int_param(params: dict, key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_param(params: dict, key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _file_param(params: dict, key: str, files: Iterable[str], suffixes: tuple[str, ...], fallback: str | None = None) -> str | None:
    requested = _str_param(params, key, "")
    if requested:
        return requested
    found = _find_first(files, suffixes)
    return found or fallback


def _solvent_template(params: dict) -> str:
    explicit = _str_param(params, "solvent_template", "auto")
    if explicit and explicit != "auto":
        return explicit
    water = _str_param(params, "water_model", "opc").lower()
    if water in {"opc", "tip4p", "tip4p-ew", "tip4pew", "tip4p-ice"}:
        return "tip4p.gro"
    return "spc216.gro"


def _mdrun_args(
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


def _internal_step(title: str, operation: str, data: dict, outputs: list[str] | None = None) -> Step:
    args = ["internal", operation]
    for key, value in data.items():
        if value is None or value == "":
            continue
        args.append(f"--{key}={value}")
    return Step(title=title, args=args, outputs=outputs or [], kind="internal", operation=operation, data=data)


def _structure_preflight_step(source: str, output: str = "structure-preflight.txt") -> Step:
    return _internal_step(
        "Inspect input structure",
        "structure_preflight",
        {"source": source, "output": output},
        outputs=[output],
    )


def _thermodynamic_gate_steps(gmx_bin: str, edr: str, prefix: str, source_mdp: str) -> list[Step]:
    xvg = f"{prefix}-thermodynamics.xvg"
    report = f"quality-{prefix}-thermodynamics.txt"
    return [
        Step(
            f"Extract {prefix.upper()} thermodynamics",
            _as_gmx(gmx_bin, ["energy", "-f", edr, "-o", xvg]),
            stdin_text=_selection_text("Temperature", "Pressure", "Density", "Volume", "Potential", "0"),
            outputs=[xvg],
        ),
        _internal_step(
            f"Validate {prefix.upper()} thermodynamics",
            "validate_thermodynamics",
            {"xvg": xvg, "output": report, "source_mdp": source_mdp, "strict": True},
            outputs=[report],
        ),
    ]


def _restraint_release_steps(
    gmx_bin: str, params: dict, npt_mdp: str, index_args: list[str], ntmpi: int, ntomp: int, gpu: bool, verbose: bool
) -> list[Step]:
    if not _bool_param(params, "release_restraints", True):
        return []
    return [
        _internal_step(
            "Configure unrestrained NPT",
            "prepare_unrestrained_npt_mdp",
            {"source_mdp": npt_mdp, "output_mdp": "npt-unrestrained.mdp"},
            outputs=["npt-unrestrained.mdp"],
        ),
        Step(
            "Prepare unrestrained NPT",
            _as_gmx(
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
                    *_maybe_maxwarn(params),
                ],
            ),
            outputs=["npt-free.tpr"],
        ),
        Step(
            "Run unrestrained NPT",
            _as_gmx(gmx_bin, _mdrun_args("npt-free", ntmpi, ntomp, gpu, verbose, pin=_str_param(params, "pin", "auto"))),
            outputs=["npt-free.gro", "npt-free.cpt", "npt-free.edr", "npt-free.log"],
        ),
        _internal_step(
            "Check unrestrained NPT",
            "validate_stage_log",
            {"stage": "npt-free", "log_file": "npt-free.log", "output_report": "quality-npt-free.txt"},
            outputs=["quality-npt-free.txt"],
        ),
    ]


def _production_steps(
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
    replicas = _int_param(params, "replicas", 1)
    if replicas < 1 or replicas > 16:
        raise ValueError("replicas must be between 1 and 16")
    base = _output_name_param(params, "production_deffnm", "md", "Production output prefix")
    steps: list[Step] = []
    outputs: list[str] = []
    structural_outputs: list[str] = []
    rmsd_center_group = "Protein_Lig" if params.get("workflow") == "protein_ligand_md" else "Protein"
    for replica in range(1, replicas + 1):
        deffnm = base if replicas == 1 else f"{base}_r{replica:02d}"
        replica_mdp = md_mdp if replicas == 1 else f"md-replica-{replica:02d}.mdp"
        if replicas > 1:
            steps.append(
                _internal_step(
                    f"Configure replica {replica}",
                    "prepare_replica_mdp",
                    {"source_mdp": md_mdp, "output_mdp": replica_mdp, "seed": -1},
                    outputs=[replica_mdp],
                )
            )
        grompp_args = ["grompp", "-f", replica_mdp, "-c", f"{equil_prefix}.gro"]
        if replicas == 1:
            grompp_args.extend(["-t", f"{equil_prefix}.cpt"])
        grompp_args.extend(["-p", topology, *index_args, "-o", f"{deffnm}.tpr", *_maybe_maxwarn(params)])
        steps.extend(
            [
                Step(
                    f"Prepare production{f' replica {replica}' if replicas > 1 else ''}",
                    _as_gmx(gmx_bin, grompp_args),
                    outputs=[f"{deffnm}.tpr"],
                ),
                Step(
                    f"Production MD{f' replica {replica}' if replicas > 1 else ''}",
                    _as_gmx(
                        gmx_bin,
                        _mdrun_args(
                            deffnm,
                            ntmpi,
                            ntomp,
                            gpu,
                            verbose,
                            cpi=True,
                            pin=_str_param(params, "pin", "auto"),
                        ),
                    ),
                    outputs=[f"{deffnm}.xtc", f"{deffnm}.edr", f"{deffnm}.log"],
                ),
                Step(
                    f"Extract replica {replica} observables",
                    _as_gmx(gmx_bin, ["energy", "-f", f"{deffnm}.edr", "-o", f"{deffnm}-observables.xvg"]),
                    stdin_text=_selection_text("Temperature", "Pressure", "Density", "Potential", "0"),
                    outputs=[f"{deffnm}-observables.xvg"],
                ),
                Step(
                    f"Unwrap replica {replica} trajectory for backbone RMSD",
                    _as_gmx(
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
                    stdin_text=_selection_text("System"),
                    outputs=[f"{deffnm}-nojump.xtc"],
                ),
                Step(
                    f"Cluster and center replica {replica} trajectory for backbone RMSD",
                    _as_gmx(
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
                    stdin_text=_selection_text(rmsd_center_group, rmsd_center_group, "System"),
                    outputs=[f"{deffnm}-rmsd-whole.xtc"],
                ),
                _internal_step(
                    f"Remove replica {replica} unwrapped trajectory",
                    "remove_files",
                    {"paths": [f"{deffnm}-nojump.xtc"]},
                ),
                Step(
                    f"Compute replica {replica} backbone RMSD",
                    _as_gmx(
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
                    stdin_text=_selection_text("Backbone", "Backbone"),
                    outputs=[f"{deffnm}-backbone-rmsd.xvg"],
                ),
                _internal_step(
                    f"Remove replica {replica} RMSD trajectory",
                    "remove_files",
                    {"paths": [f"{deffnm}-rmsd-whole.xtc"]},
                ),
            ]
        )
        outputs.append(f"{deffnm}-observables.xvg")
        structural_outputs.append(f"{deffnm}-backbone-rmsd.xvg")
    steps.append(
        _internal_step(
            "Summarize sampling uncertainty",
            "summarize_replicas",
            {"inputs": outputs, "output": "sampling-summary.json"},
            outputs=["sampling-summary.json"],
        )
    )
    steps.append(
        _internal_step(
            "Summarize structural convergence",
            "summarize_replicas",
            {"inputs": structural_outputs, "output": "structural-convergence.json"},
            outputs=["structural-convergence.json"],
        )
    )
    return steps


def _maybe_maxwarn(params: dict) -> list[str]:
    return _maxwarn_args(_int_param(params, "maxwarn", 0))


def _selection_text(*groups: str) -> str:
    return "".join(f"{group}\n" for group in groups if group)


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


def _should_clean_pdb_for_pdb2gmx(params: dict, source: str) -> bool:
    return _bool_param(params, "clean_pdb", False) and source.lower().endswith(".pdb")


def _clean_pdb_step(source: str, output: str, remove_ligands: bool = True, ligand_residue: str = "") -> Step:
    return _internal_step(
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


def _split_complex_pdb_step(source: str, protein_output: str, ligand_output: str, ligand_residue: str = "") -> Step:
    return _internal_step(
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


def _split_complex_pdb_multi_step(source: str, protein_output: str, ligand_outputs: list[dict], title: str = "Split complex PDB") -> Step:
    return _internal_step(
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


def _pdb2gmx_source_steps(params: dict, source: str, output: str, remove_ligands: bool = True) -> tuple[str, list[Step]]:
    if not _should_clean_pdb_for_pdb2gmx(params, source):
        return source, []
    return output, [_clean_pdb_step(source, output, remove_ligands=remove_ligands)]


def _pdb2gmx_water_choice(params: dict, gmx_bin: str, force_field: str, force_field_base: Path | None) -> tuple[str, str | None]:
    water_model = _str_param(params, "water_model", "opc").lower()
    if water_model in PDB2GMX_CLI_WATERS:
        return water_model, None

    selection = local_water_model_selection(force_field_base, force_field, water_model) if force_field_base else None
    selection = selection or installed_water_model_selection(gmx_bin, force_field, water_model)
    selection = selection or KNOWN_WATER_MENU_SELECTIONS.get(force_field.lower(), {}).get(water_model)
    if not selection:
        raise ValueError(f"Water model {water_model!r} is not listed in {force_field}.ff/watermodels.dat")
    return "select", _selection_text(selection)


def _pdb2gmx_step(
    title: str,
    gmx_bin: str,
    params: dict,
    source: str,
    output: str,
    topology: str = "topol.top",
    force_field_base: Path | None = None,
    outputs: list[str] | None = None,
) -> Step:
    force_field = normalize_force_field_name(_str_param(params, "force_field", "amber19sb"))
    water_arg, stdin_text = _pdb2gmx_water_choice(params, gmx_bin, force_field, force_field_base)
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
    if _bool_param(params, "ignh", False):
        args.append("-ignh")
    return Step(title, _as_gmx(gmx_bin, args), stdin_text=stdin_text, outputs=outputs or [output, topology])


def _ion_args(gmx_bin: str, params: dict, tpr: str, output: str, topology: str = "topol.top") -> list[str]:
    args = [
        "genion",
        "-s",
        tpr,
        "-o",
        output,
        "-p",
        topology,
        "-pname",
        _str_param(params, "positive_ion", "NA"),
        "-nname",
        _str_param(params, "negative_ion", "CL"),
    ]
    ion_conc = _str_param(params, "ion_concentration", "")
    if ion_conc:
        try:
            concentration = float(ion_conc)
        except ValueError:
            raise ValueError("ion_concentration must be a number between 0 and 5 mol/L")
        if not 0 <= concentration <= 5:
            raise ValueError("ion_concentration must be between 0 and 5 mol/L")
        args.extend(["-conc", _format_mdp_number(concentration)])
    if _bool_param(params, "neutral", True):
        args.append("-neutral")
    return _as_gmx(gmx_bin, args)


def _index_arg(index_file: str) -> list[str]:
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


def _copy_to(workdir: Path, source: str, target: str) -> None:
    src = ensure_under_root(workdir / source, workdir)
    dst = ensure_under_root(workdir / target, workdir)
    if src.resolve() == dst.resolve():
        return
    shutil.copyfile(src, dst)


def _stage_prepared_ligand(workdir: Path, data: dict) -> None:
    _copy_to(workdir, str(data["ligand_gro"]), str(data.get("gro_output") or "ligand_GMX.gro"))
    _copy_to(workdir, str(data["ligand_itp"]), str(data.get("itp_output") or "ligand_GMX.itp"))


def _pdb_residue_name(line: str) -> str:
    return line[17:20].strip().upper()


def _pdb_atom_serial(line: str) -> int | None:
    try:
        return int(line[6:11])
    except ValueError:
        return None


def _is_pdb_atom_record(line: str) -> bool:
    return line[:6].strip().upper() in {"ATOM", "HETATM"}


def _is_pdb_water(line: str) -> bool:
    return _pdb_residue_name(line) in PDB_WATER_RESIDUES


def _is_pdb_ion(line: str) -> bool:
    return _pdb_residue_name(line) in PDB_ION_RESIDUES


def _is_pdb_ligand(line: str, ligand_residue: str = "") -> bool:
    residue = _pdb_residue_name(line)
    requested = ligand_residue.strip().upper()
    if requested:
        return _is_pdb_atom_record(line) and residue == requested
    return line[:6].strip().upper() == "HETATM" and not _is_pdb_water(line) and not _is_pdb_ion(line)


def _conect_serials(line: str) -> list[int]:
    serials: list[int] = []
    for token in line.split()[1:]:
        try:
            serials.append(int(token))
        except ValueError:
            continue
    return serials


def _write_pdb_subset(path: Path, lines: list[str]) -> None:
    output = [line for line in lines if line[:6].strip().upper() != "MASTER"]
    if not output or output[-1].strip().upper() != "END":
        output.append("END")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _format_removed_records(records: dict[str, int]) -> list[str]:
    if not records:
        return []
    return ["removed_residues:", *(f"  {name}: {count}" for name, count in sorted(records.items()))]


def _clean_pdb_for_pdb2gmx(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_pdb"]), workdir)
    target = ensure_under_root(workdir / str(data["output_pdb"]), workdir)
    remove_waters = _bool_param(data, "remove_waters", True)
    remove_ions = _bool_param(data, "remove_ions", True)
    remove_ligands = _bool_param(data, "remove_ligands", True)
    ligand_residue = str(data.get("ligand_residue") or "")
    kept: list[str] = []
    removed_waters = 0
    removed_ions = 0
    removed_ligands = 0
    removed_records: dict[str, int] = {}
    removed_serials: set[int] = set()

    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        record = line[:6].strip().upper()
        if record in {"ATOM", "HETATM"}:
            residue = _pdb_residue_name(line)
            remove = False
            if remove_waters and residue in PDB_WATER_RESIDUES:
                removed_waters += 1
                remove = True
            elif remove_ions and residue in PDB_ION_RESIDUES:
                removed_ions += 1
                remove = True
            elif remove_ligands and _is_pdb_ligand(line, ligand_residue):
                removed_ligands += 1
                remove = True
            if remove:
                serial = _pdb_atom_serial(line)
                if serial is not None:
                    removed_serials.add(serial)
                removed_records[residue] = removed_records.get(residue, 0) + 1
                continue
        if record == "CONECT" and any(serial in removed_serials for serial in _conect_serials(line)):
            continue
        kept.append(line)

    if not kept or not any(line[:6].strip().upper() in {"ATOM", "HETATM"} for line in kept):
        raise ValueError(f"{source.name} has no atoms left after removing waters, ions, and ligands")
    _write_pdb_subset(target, kept)

    report_lines = [
        f"source: {source.name}",
        f"output: {target.name}",
        f"removed_water_atoms: {removed_waters}",
        f"removed_ion_atoms: {removed_ions}",
        f"removed_ligand_atoms: {removed_ligands}",
    ]
    report_lines.extend(_format_removed_records(removed_records))
    (workdir / "pdb2gmx-clean-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def _split_complex_pdb(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_pdb"]), workdir)
    protein_target = ensure_under_root(workdir / str(data["protein_output_pdb"]), workdir)
    remove_waters = _bool_param(data, "remove_waters", True)
    remove_ions = _bool_param(data, "remove_ions", True)
    multi_outputs = data.get("ligand_outputs") or []

    if multi_outputs:
        ligand_targets = {
            str(entry.get("residue") or "").strip().upper(): ensure_under_root(workdir / str(entry["output_pdb"]), workdir)
            for entry in multi_outputs
        }
        if any(not residue for residue in ligand_targets):
            raise ValueError("Every ligand in a multi-ligand split needs a PDB residue name.")
        ligand_lines: dict[str, list[str]] = {residue: [] for residue in ligand_targets}
        ligand_serials: dict[str, set[int]] = {residue: set() for residue in ligand_targets}
        ligand_residue = ""
    else:
        ligand_targets = {}
        ligand_lines = {}
        ligand_serials = {}
        ligand_residue = str(data.get("ligand_residue") or "")

    protein_lines: list[str] = []
    protein_serials: set[int] = set()
    skipped_waters = 0
    skipped_ions = 0
    ligand_atoms = 0
    ligand_records: dict[str, int] = {}
    unknown_hetero: set[str] = set()
    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()

    for line in lines:
        record = line[:6].strip().upper()
        if record in {"ATOM", "HETATM"}:
            serial = _pdb_atom_serial(line)
            residue = _pdb_residue_name(line)
            if remove_waters and _is_pdb_water(line):
                skipped_waters += 1
                continue
            if remove_ions and _is_pdb_ion(line):
                skipped_ions += 1
                continue
            if multi_outputs:
                if record == "HETATM" and residue in ligand_targets:
                    ligand_lines[residue].append(line)
                    ligand_atoms += 1
                    ligand_records[residue] = ligand_records.get(residue, 0) + 1
                    if serial is not None:
                        ligand_serials[residue].add(serial)
                    continue
                if record == "HETATM":
                    unknown_hetero.add(residue)
                    continue
            elif _is_pdb_ligand(line, ligand_residue):
                bucket = ligand_lines.setdefault("_single", [])
                bucket.append(line)
                ligand_atoms += 1
                ligand_records[residue] = ligand_records.get(residue, 0) + 1
                if serial is not None:
                    ligand_serials.setdefault("_single", set()).add(serial)
                continue
            protein_lines.append(line)
            if serial is not None:
                protein_serials.add(serial)
            continue
        if record not in {"CONECT", "MASTER"}:
            protein_lines.append(line)

    for line in lines:
        if line[:6].strip().upper() != "CONECT":
            continue
        serials = _conect_serials(line)
        if not serials:
            continue
        if multi_outputs:
            for residue, serial_set in ligand_serials.items():
                if all(serial in serial_set for serial in serials):
                    ligand_lines[residue].append(line)
                    break
            else:
                if all(serial in protein_serials for serial in serials):
                    protein_lines.append(line)
            continue
        single_serials = ligand_serials.get("_single", set())
        if serials and all(serial in single_serials for serial in serials):
            ligand_lines.setdefault("_single", []).append(line)
        elif serials and all(serial in protein_serials for serial in serials):
            protein_lines.append(line)

    if not any(_is_pdb_atom_record(line) for line in protein_lines):
        raise ValueError(f"{source.name} has no protein atoms after complex splitting")

    if multi_outputs:
        if unknown_hetero:
            listing = ", ".join(sorted(unknown_hetero))
            raise ValueError(
                f"Unmapped HETATM residues in {source.name}: {listing}. "
                "Add a ligand with a matching residue name for each one, or remove them from the PDB."
            )
        for residue, target in ligand_targets.items():
            if not any(_is_pdb_atom_record(line) for line in ligand_lines[residue]):
                raise ValueError(f"No ligand atoms found for residue {residue} in {source.name}.")
        for residue, target in ligand_targets.items():
            _write_pdb_subset(target, ligand_lines[residue])
    else:
        single_lines = ligand_lines.get("_single", [])
        if not any(_is_pdb_atom_record(line) for line in single_lines):
            residue_hint = f" for residue {ligand_residue.strip().upper()}" if ligand_residue.strip() else ""
            raise ValueError(
                f"No ligand atoms found{residue_hint} in {source.name}. Set the ligand residue name or upload ligand files separately."
            )
        ligand_target = ensure_under_root(workdir / str(data["ligand_output_pdb"]), workdir)
        _write_pdb_subset(ligand_target, single_lines)

    _write_pdb_subset(protein_target, protein_lines)
    report_lines = [
        f"source: {source.name}",
        f"protein_output: {protein_target.name}",
        f"skipped_water_atoms: {skipped_waters}",
        f"skipped_ion_atoms: {skipped_ions}",
        f"ligand_atoms: {ligand_atoms}",
    ]
    if multi_outputs:
        for residue in ligand_targets:
            report_lines.append(f"ligand_{residue}_output: {ligand_targets[residue].name}")
            report_lines.append(f"ligand_{residue}_atoms: {ligand_records.get(residue, 0)}")
    else:
        report_lines.append(f"ligand_output: {str(data['ligand_output_pdb'])}")
        if ligand_residue.strip():
            report_lines.append(f"ligand_residue_filter: {ligand_residue.strip().upper()}")
    report_lines.extend(_format_removed_records(ligand_records))
    (workdir / "complex-pdb-split-report.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def _write_ligand_smiles(workdir: Path, data: dict) -> None:
    smiles = str(data.get("smiles", "")).strip()
    if not smiles or len(smiles) > 2000 or any(character in smiles for character in "\r\n\0"):
        raise ValueError("Ligand SMILES must be a single non-empty line no longer than 2000 characters")
    output = ensure_under_root(workdir / str(data["output"]), workdir)
    output.write_text(f"{smiles}\tligand\n", encoding="utf-8")


def _pdb_pose_atoms(path: Path) -> tuple[list[dict], set[tuple[int, int]]]:
    atoms: list[dict] = []
    serial_to_index: dict[int, int] = {}
    conect_serials: list[tuple[int, int]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        record = line[:6].strip().upper()
        if record in {"ATOM", "HETATM"} and len(line) >= 54:
            element = line[76:78].strip().upper() if len(line) >= 78 else ""
            if not element:
                element = re.sub(r"[^A-Za-z]", "", line[12:16]).upper()[:1]
            serial = int(line[6:11])
            serial_to_index[serial] = len(atoms)
            atoms.append({"element": element, "xyz": (float(line[30:38]), float(line[38:46]), float(line[46:54]))})
        elif record == "CONECT":
            fields = line[6:].split()
            if fields:
                source = int(fields[0])
                conect_serials.extend((source, int(target)) for target in fields[1:])
    heavy_indices = [index for index, atom in enumerate(atoms) if atom["element"] not in {"H", "D"}]
    old_to_heavy = {old: new for new, old in enumerate(heavy_indices)}
    heavy_atoms = [atoms[index] for index in heavy_indices]
    bonds: set[tuple[int, int]] = set()
    for left_serial, right_serial in conect_serials:
        left = serial_to_index.get(left_serial)
        right = serial_to_index.get(right_serial)
        if left in old_to_heavy and right in old_to_heavy:
            pair = tuple(sorted((old_to_heavy[left], old_to_heavy[right])))
            if pair[0] != pair[1]:
                bonds.add(pair)
    return heavy_atoms, bonds


def _read_sdf_atoms(path: Path) -> tuple[list[dict], list[tuple[int, int, int]], dict[int, int]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 4 or "V3000" in lines[3]:
        raise ValueError("Ligand chemistry must normalize to an SDF V2000 record")
    try:
        atom_count = int(lines[3][0:3])
        bond_count = int(lines[3][3:6])
    except ValueError as exc:
        raise ValueError("Invalid SDF counts line") from exc
    if len(lines) < 4 + atom_count + bond_count:
        raise ValueError("Truncated SDF record")
    raw_atoms = []
    for line in lines[4 : 4 + atom_count]:
        raw_atoms.append({"element": line[31:34].strip().upper(), "xyz": (float(line[0:10]), float(line[10:20]), float(line[20:30]))})
    raw_bonds: list[tuple[int, int, int]] = []
    for line in lines[4 + atom_count : 4 + atom_count + bond_count]:
        raw_bonds.append((int(line[0:3]) - 1, int(line[3:6]) - 1, int(line[6:9])))
    formal_charges: dict[int, int] = {}
    for line in lines[4 + atom_count + bond_count :]:
        if line.startswith("M  CHG"):
            fields = line.split()
            for offset in range(int(fields[2])):
                formal_charges[int(fields[3 + 2 * offset]) - 1] = int(fields[4 + 2 * offset])
    return raw_atoms, raw_bonds, formal_charges


def _read_sdf_heavy(path: Path) -> tuple[list[dict], list[tuple[int, int, int]], int]:
    raw_atoms, raw_bonds, formal_charges = _read_sdf_atoms(path)
    heavy_old = [index for index, atom in enumerate(raw_atoms) if atom["element"] not in {"H", "D"}]
    old_to_heavy = {old: new for new, old in enumerate(heavy_old)}
    atoms = [{**raw_atoms[old], "charge": formal_charges.get(old, 0)} for old in heavy_old]
    bonds = [
        (old_to_heavy[left], old_to_heavy[right], order)
        for left, right, order in raw_bonds
        if left in old_to_heavy and right in old_to_heavy
    ]
    return atoms, bonds, sum(atom["charge"] for atom in atoms)


_COVALENT_RADII = {
    "B": 0.84,
    "C": 0.76,
    "N": 0.71,
    "O": 0.66,
    "F": 0.57,
    "P": 1.07,
    "S": 1.05,
    "CL": 1.02,
    "BR": 1.20,
    "I": 1.39,
}


def _infer_pose_bonds(pose_atoms: list[dict]) -> set[tuple[int, int]]:
    """Infer an organic ligand's connectivity from PDB coordinates.

    PDB files produced by structure predictors commonly omit CONECT records.
    The inferred graph is used only to find an atom permutation; bond orders
    still come exclusively from the supplied SDF/MOL2 chemistry source.
    """
    bonds: set[tuple[int, int]] = set()
    for left, left_atom in enumerate(pose_atoms):
        left_radius = _COVALENT_RADII.get(left_atom["element"])
        if left_radius is None:
            raise ValueError(f"Cannot infer ligand connectivity for element {left_atom['element']}; provide a pose PDB with CONECT records")
        for right in range(left + 1, len(pose_atoms)):
            right_atom = pose_atoms[right]
            right_radius = _COVALENT_RADII.get(right_atom["element"])
            if right_radius is None:
                raise ValueError(
                    f"Cannot infer ligand connectivity for element {right_atom['element']}; provide a pose PDB with CONECT records"
                )
            distance = math.dist(left_atom["xyz"], right_atom["xyz"])
            if 0.4 < distance <= 1.25 * (left_radius + right_radius):
                bonds.add((left, right))
    return bonds


def _map_pose_atoms(
    pose_atoms: list[dict], pose_bonds: set[tuple[int, int]], chemistry_atoms: list[dict], chemistry_bonds: list[tuple[int, int, int]]
) -> list[int]:
    if len(pose_atoms) != len(chemistry_atoms):
        raise ValueError(f"Ligand heavy-atom count mismatch: pose={len(pose_atoms)}, chemistry={len(chemistry_atoms)}")
    if not pose_bonds:
        pose_bonds = _infer_pose_bonds(pose_atoms)
    if not pose_bonds and chemistry_bonds:
        raise ValueError("Could not infer ligand bonds from the pose PDB coordinates; provide a pose PDB with CONECT records")
    chemistry_edges = {tuple(sorted((left, right))) for left, right, _ in chemistry_bonds}
    pose_neighbors = [
        {right if left == index else left for left, right in pose_bonds if index in {left, right}} for index in range(len(pose_atoms))
    ]
    chemistry_neighbors = [
        {right if left == index else left for left, right in chemistry_edges if index in {left, right}}
        for index in range(len(chemistry_atoms))
    ]
    candidates = [
        [
            pose_index
            for pose_index, pose in enumerate(pose_atoms)
            if pose["element"] == atom["element"] and len(pose_neighbors[pose_index]) == len(chemistry_neighbors[index])
        ]
        for index, atom in enumerate(chemistry_atoms)
    ]
    order = sorted(range(len(chemistry_atoms)), key=lambda index: len(candidates[index]))
    mapping: dict[int, int] = {}
    used: set[int] = set()

    def search(position: int) -> bool:
        if position == len(order):
            return True
        chemistry_index = order[position]
        for pose_index in candidates[chemistry_index]:
            if pose_index in used:
                continue
            if any(
                (mapped_neighbor in pose_neighbors[pose_index]) != (neighbor in chemistry_neighbors[chemistry_index])
                for neighbor, mapped_neighbor in mapping.items()
            ):
                continue
            mapping[chemistry_index] = pose_index
            used.add(pose_index)
            if search(position + 1):
                return True
            used.remove(pose_index)
            del mapping[chemistry_index]
        return False

    if not search(0):
        raise ValueError("Could not map ligand chemistry graph onto the co-folded PDB pose")
    return [mapping[index] for index in range(len(chemistry_atoms))]


def _prepare_ligand_chemistry(workdir: Path, data: dict) -> None:
    pose_path = ensure_under_root(workdir / str(data["pose_pdb"]), workdir)
    chemistry_path = ensure_under_root(workdir / str(data["chemistry_sdf"]), workdir)
    output_path = ensure_under_root(workdir / str(data["output_sdf"]), workdir)
    report_path = ensure_under_root(workdir / str(data["report"]), workdir)
    pose_atoms, pose_bonds = _pdb_pose_atoms(pose_path)
    chemistry_atoms, chemistry_bonds, formal_charge = _read_sdf_heavy(chemistry_path)
    expected_charge = int(data["expected_charge"])
    if formal_charge != expected_charge:
        raise ValueError(f"Ligand formal charge ({formal_charge}) does not match confirmed ACPYPE charge ({expected_charge})")
    mapping_source = "pdb-conect" if pose_bonds else "distance-inferred-graph"
    mapping = _map_pose_atoms(pose_atoms, pose_bonds, chemistry_atoms, chemistry_bonds)
    rows = ["Mapped ligand chemistry onto co-folded pose", "  GROMACS Web UI", ""]
    rows.append(f"{len(chemistry_atoms):>3}{len(chemistry_bonds):>3}  0  0  0  0            999 V2000")
    for chemistry_index, atom in enumerate(chemistry_atoms):
        x, y, z = pose_atoms[mapping[chemistry_index]]["xyz"]
        rows.append(f"{x:10.4f}{y:10.4f}{z:10.4f} {atom['element']:<3} 0  0  0  0  0  0  0  0  0  0  0  0")
    rows.extend(f"{left + 1:>3}{right + 1:>3}{order:>3}  0  0  0  0" for left, right, order in chemistry_bonds)
    charged = [(index + 1, atom["charge"]) for index, atom in enumerate(chemistry_atoms) if atom["charge"]]
    for start in range(0, len(charged), 8):
        chunk = charged[start : start + 8]
        rows.append(f"M  CHG{len(chunk):>3}" + "".join(f"{index:>4}{charge:>4}" for index, charge in chunk))
    rows.extend(["M  END", "$$$$"])
    output_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    report_path.write_text(
        f"pose={pose_path.name}\nchemistry={chemistry_path.name}\nheavy_atoms={len(chemistry_atoms)}\n"
        f"mapping={'atom-order' if mapping == list(range(len(mapping))) else 'graph-isomorphism'}\nmapping_source={mapping_source}\nformal_charge={formal_charge}\n"
        f"expected_charge={expected_charge}\nheavy_atom_coordinates=preserved\nstatus=PASS\n",
        encoding="utf-8",
    )


def _stage_acpype_ligand(workdir: Path, data: dict) -> None:
    gro_output = str(data.get("gro_output") or "ligand_GMX.gro")
    itp_output = str(data.get("itp_output") or "ligand_GMX.itp")
    acpype_dir_name = str(data.get("acpype_dir") or "")
    acpype_dir = ensure_under_root(workdir / acpype_dir_name, workdir) if acpype_dir_name else None
    if acpype_dir is not None and not acpype_dir.exists():
        source_stem = str(data.get("source_stem") or "")
        matches = sorted(workdir.glob("*.acpype"))
        if source_stem and (workdir / f"{source_stem}.acpype").exists():
            acpype_dir = ensure_under_root(workdir / f"{source_stem}.acpype", workdir)
        elif len(matches) == 1:
            acpype_dir = matches[0]
        else:
            raise FileNotFoundError(f"ACPYPE output folder not found: {acpype_dir_name or source_stem}.acpype")
    if acpype_dir is None:
        source_stem = str(data["source_stem"])
        acpype_dir = ensure_under_root(workdir / f"{source_stem}.acpype", workdir)
        if not acpype_dir.exists():
            matches = sorted(workdir.glob("*.acpype"))
            if len(matches) == 1:
                acpype_dir = matches[0]
            else:
                raise FileNotFoundError(f"ACPYPE output folder not found: {source_stem}.acpype")
    gro_files = sorted(acpype_dir.glob("*_GMX.gro"))
    itp_files = sorted(acpype_dir.glob("*_GMX.itp"))
    if not gro_files or not itp_files:
        raise FileNotFoundError("ACPYPE output must contain *_GMX.gro and *_GMX.itp")
    shutil.copyfile(gro_files[0], ensure_under_root(workdir / gro_output, workdir))
    shutil.copyfile(itp_files[0], ensure_under_root(workdir / itp_output, workdir))


def _restore_ligand_pose_coordinates(workdir: Path, data: dict) -> None:
    pose_path = ensure_under_root(workdir / str(data["pose_sdf"]), workdir)
    parameter_path = ensure_under_root(workdir / str(data["parameter_sdf"]), workdir)
    gro_path = ensure_under_root(workdir / str(data["gro_file"]), workdir)
    report_path = ensure_under_root(workdir / str(data["report"]), workdir)
    pose_atoms, _, _ = _read_sdf_atoms(pose_path)
    parameter_atoms, _, _ = _read_sdf_atoms(parameter_path)
    pose_elements = [atom["element"] for atom in pose_atoms]
    parameter_elements = [atom["element"] for atom in parameter_atoms]
    if pose_elements != parameter_elements:
        raise ValueError("Ligand atom order changed between the bound pose and parameterization conformer")
    title, gro_atoms, box = _read_gro(gro_path)
    if len(gro_atoms) != len(pose_atoms):
        raise ValueError(f"ACPYPE coordinate atom count ({len(gro_atoms)}) does not match the bound ligand pose ({len(pose_atoms)})")
    restored: list[str] = []
    for line, atom in zip(gro_atoms, pose_atoms):
        x, y, z = (coordinate / 10.0 for coordinate in atom["xyz"])
        restored.append(f"{line[:20]}{x:8.3f}{y:8.3f}{z:8.3f}{line[44:]}")
    gro_path.write_text("\n".join([title, str(len(restored)), *restored, box]) + "\n", encoding="utf-8")
    report_path.write_text(
        f"pose={pose_path.name}\nparameterization={parameter_path.name}\ngro={gro_path.name}\n"
        f"atoms={len(restored)}\natom_order=verified\ncoordinates=co-folded-pose\nstatus=PASS\n",
        encoding="utf-8",
    )


def _isolate_acpype_output(workdir: Path, data: dict) -> None:
    expected = ensure_under_root(workdir / str(data["expected_dir"]), workdir)
    target = ensure_under_root(workdir / str(data["output_dir"]), workdir)
    if not expected.exists():
        raise FileNotFoundError(f"ACPYPE output folder not found: {expected.name}")
    if target.exists():
        shutil.rmtree(target)
    expected.rename(target)


def _itp_sections(path: Path) -> list[tuple[str | None, list[str]]]:
    blocks: list[tuple[str | None, list[str]]] = []
    current_name: str | None = None
    current: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*\[\s*([^\]\[]+)\s*\]", line)
        if match:
            blocks.append((current_name, current))
            current_name = match.group(1).strip().lower()
            current = [line]
        else:
            current.append(line)
    blocks.append((current_name, current))
    return blocks


def _itp_section_data_key(section: str, line: str) -> str | None:
    clean = line.split(";", 1)[0].split("#", 1)[0].strip()
    if not clean or clean.startswith("["):
        return None
    tokens = clean.split()
    if section == "atomtypes":
        return tokens[0]
    return " ".join(tokens[:2])


def _merge_ligand_atomtypes(workdir: Path, data: dict) -> None:
    """Merge `[ atomtypes ]` (and pairtypes/nonbond_params) across ligand itps.

    Every ACPYPE `*_GMX.itp` carries its own `[ atomtypes ]` section.  When two
    ligand itps are `#include`d into the same `topol.top`, GROMACS aborts with
    "Atomtype X multiply defined" for every shared atom type.  This step unions
    those sections (deduplicated by type name) into the first ligand itp and
    strips them from the others.  Conflicting definitions for the same type
    name are a hard error instead of a silent override.
    """
    itp_names = [str(name) for name in data.get("itps") or []]
    if not itp_names:
        raise ValueError("merge_ligand_atomtypes requires at least one ligand itp")
    paths = [ensure_under_root(workdir / name, workdir) for name in itp_names]
    blocks_by_path = {path: _itp_sections(path) for path in paths}
    merge_sections = ("atomtypes", "nonbond_params", "pairtypes")
    merged: dict[str, dict[str, str]] = {section: {} for section in merge_sections}
    found: dict[str, int] = {section: 0 for section in merge_sections}

    for path in paths:
        for name, lines in blocks_by_path[path]:
            if name not in merge_sections:
                continue
            for line in lines:
                key = _itp_section_data_key(name, line)
                if key is None:
                    continue
                found[name] += 1
                existing = merged[name].get(key)
                if existing is not None and existing != line.strip():
                    raise ValueError(
                        f"Conflicting {name} definition for '{key}' between ligand itp files: '{existing}' vs '{line.strip()}'"
                    )
                merged[name][key] = line.strip()

    if not any(found.values()):
        (workdir / str(data.get("report") or "ligand-atomtypes-merge.txt")).write_text(
            "status=SKIP\nreason=no atomtypes/pairtypes/nonbond_params sections found in ligand itps\n", encoding="utf-8"
        )
        return

    keeper_index = next(
        (index for index, path in enumerate(paths) if any(name in merge_sections for name, _ in blocks_by_path[path])),
        0,
    )

    for index, path in enumerate(paths):
        keep = index == keeper_index
        output_lines: list[str] = []
        insert_at: int | None = None
        for name, lines in blocks_by_path[path]:
            if name in merge_sections:
                if keep and insert_at is None:
                    insert_at = len(output_lines)
                continue
            output_lines.extend(lines)
        if keep:
            combined = []
            for section in merge_sections:
                if not merged[section]:
                    continue
                combined.append(f"[ {section} ]")
                combined.append(f"; merged from {len(paths)} ligand itp file(s)")
                combined.extend(merged[section].values())
                combined.append("")
            if insert_at is None:
                raise ValueError(f"{path.name} lost its atomtypes insertion point while merging ligand itps")
            output_lines[insert_at:insert_at] = combined
        path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")

    report_lines = [f"itp_files={', '.join(itp_names)}"]
    for section in merge_sections:
        report_lines.append(f"merged_{section}={len(merged[section])}")
    report_lines.append("status=PASS")
    ensure_under_root(workdir / str(data.get("report") or "ligand-atomtypes-merge.txt"), workdir).write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )


def _read_gro(path: Path) -> tuple[str, list[str], str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 3:
        raise ValueError(f"Invalid .gro file: {path.name}")
    try:
        atom_count = int(lines[1].strip())
    except ValueError as exc:
        raise ValueError(f"Invalid atom count in {path.name}") from exc
    atom_lines = lines[2:-1]
    if len(atom_lines) != atom_count:
        raise ValueError(f"{path.name} declares {atom_count} atoms but contains {len(atom_lines)} atom lines")
    return lines[0], atom_lines, lines[-1]


def _merge_gro(workdir: Path, data: dict) -> None:
    protein_path = ensure_under_root(workdir / str(data["protein_gro"]), workdir)
    ligand_names = [str(name) for name in data.get("ligand_gros") or []]
    if not ligand_names and data.get("ligand_gro"):
        ligand_names = [str(data["ligand_gro"])]
    if not ligand_names:
        raise ValueError("merge_gro requires at least one ligand coordinate file")
    output_path = ensure_under_root(workdir / str(data["output_gro"]), workdir)
    _, protein_atoms, box = _read_gro(protein_path)
    if not protein_atoms:
        raise ValueError("Protein and ligand coordinate files must both contain atoms")
    protein_xyz = [tuple(float(line[start : start + 8]) for start in (20, 28, 36)) for line in protein_atoms]
    ligand_atom_blocks: list[list[str]] = []
    ligand_xyz: list[tuple[str, list[tuple[float, float, float]]]] = []
    for name in ligand_names:
        _, atoms, _ = _read_gro(ensure_under_root(workdir / name, workdir))
        if not atoms:
            raise ValueError("Protein and ligand coordinate files must both contain atoms")
        ligand_atom_blocks.append(atoms)
        ligand_xyz.append((name, [tuple(float(line[start : start + 8]) for start in (20, 28, 36)) for line in atoms]))

    distances: list[tuple[str, str, float]] = []
    for name, xyz in ligand_xyz:
        min_distance = min(((px - lx) ** 2 + (py - ly) ** 2 + (pz - lz) ** 2) ** 0.5 for px, py, pz in protein_xyz for lx, ly, lz in xyz)
        distances.append(("protein", name, min_distance))
        if min_distance < 0.06:
            raise ValueError(f"Protein-ligand atom clash detected ({min_distance:.3f} nm) for {name}; check the ligand pose")
        if min_distance > 1.2:
            raise ValueError(f"Ligand {name} is {min_distance:.3f} nm from the protein; provide coordinates in the intended bound pose")
    for (left_name, left_xyz), (right_name, right_xyz) in itertools.combinations(ligand_xyz, 2):
        min_distance = min(((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) ** 0.5 for ax, ay, az in left_xyz for bx, by, bz in right_xyz)
        distances.append((left_name, right_name, min_distance))
        if min_distance < 0.06:
            raise ValueError(f"Ligand-ligand atom clash detected ({min_distance:.3f} nm) between {left_name} and {right_name}")

    merged_atoms = protein_atoms + [atom for block in ligand_atom_blocks for atom in block]
    merged = ["Complex generated by GROMACS Console", f"{len(merged_atoms):5d}", *merged_atoms, box]
    output_path.write_text("\n".join(merged) + "\n", encoding="utf-8")
    report_lines = [f"protein_atoms={len(protein_atoms)}"]
    report_lines.extend(f"ligand_atoms[{name}]={len(block)}" for (name, _), block in zip(ligand_xyz, ligand_atom_blocks))
    report_lines.extend(f"minimum_distance_nm[{left}|{right}]={distance:.4f}" for left, right, distance in distances)
    report_lines.append("status=PASS")
    (workdir / "complex-pose-check.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")


def _write_index_group(handle, name: str, atom_ids: Iterable[int]) -> None:
    handle.write(f"[ {name} ]\n")
    row: list[str] = []
    for atom_id in atom_ids:
        row.append(str(atom_id))
        if len(row) == 15:
            handle.write(" ".join(row) + "\n")
            row = []
    if row:
        handle.write(" ".join(row) + "\n")


def _create_complex_index(workdir: Path, data: dict) -> None:
    _, protein_atoms, _ = _read_gro(ensure_under_root(workdir / str(data["protein_gro"]), workdir))
    ligand_entries = [dict(entry) for entry in data.get("ligand_gros") or []]
    if not ligand_entries and data.get("ligand_gro"):
        ligand_entries = [{"gro": str(data["ligand_gro"]), "group": "Ligand"}]
    if not ligand_entries:
        raise ValueError("create_complex_index requires at least one ligand coordinate file")
    _, system_atoms, _ = _read_gro(ensure_under_root(workdir / str(data["system_gro"]), workdir))
    protein_end = len(protein_atoms)
    ligand_atoms = [
        (str(entry.get("group") or "Ligand"), _read_gro(ensure_under_root(workdir / str(entry["gro"]), workdir))[1])
        for entry in ligand_entries
    ]
    solute_end = protein_end + sum(len(atoms) for _, atoms in ligand_atoms)
    if solute_end >= len(system_atoms):
        raise ValueError("Solvated complex contains no water/ion atoms after the solute")
    output = ensure_under_root(workdir / str(data["output_index"]), workdir)

    def atom_name(line: str) -> str:
        return line[10:15].strip().upper()

    def residue_name(line: str) -> str:
        return line[5:10].strip().upper()

    backbone_names = {"N", "CA", "C"}
    mainchain_names = backbone_names | {"O", "O1", "O2", "OT", "OXT", "OC1", "OC2"}
    mainchain_h_names = mainchain_names | {"H", "H1", "H2", "H3", "HN", "HT1", "HT2", "HT3"}
    backbone = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) in backbone_names]
    c_alpha = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) == "CA"]
    mainchain = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) in mainchain_names]
    mainchain_h = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) in mainchain_h_names]
    sidechain = [index for index, line in enumerate(protein_atoms, 1) if atom_name(line) not in mainchain_h_names]
    water = [index for index, line in enumerate(system_atoms, 1) if residue_name(line) in PDB_WATER_RESIDUES]
    if not backbone or not c_alpha or not water:
        raise ValueError("Could not build standard Backbone/C-alpha/Water index groups from the solvated complex")
    with output.open("w", encoding="utf-8") as handle:
        _write_index_group(handle, "System", range(1, len(system_atoms) + 1))
        _write_index_group(handle, "Protein", range(1, protein_end + 1))
        cursor = protein_end
        for group, atoms in ligand_atoms:
            _write_index_group(handle, group, range(cursor + 1, cursor + len(atoms) + 1))
            cursor += len(atoms)
        _write_index_group(handle, "Protein_Lig", range(1, solute_end + 1))
        _write_index_group(handle, "Water_and_Ions", range(solute_end + 1, len(system_atoms) + 1))
        _write_index_group(handle, "Backbone", backbone)
        _write_index_group(handle, "C-alpha", c_alpha)
        _write_index_group(handle, "MainChain", mainchain)
        _write_index_group(handle, "MainChain+H", mainchain_h)
        _write_index_group(handle, "SideChain", sidechain)
        _write_index_group(handle, "Water", water)


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


def _structure_preflight(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source"]), workdir)
    output = ensure_under_root(workdir / str(data["output"]), workdir)
    if source.suffix.lower() != ".pdb":
        output.write_text(f"source={source.name}\nformat={source.suffix.lower()}\nstatus=MANUAL_REVIEW\n", encoding="utf-8")
        return
    atoms = 0
    models: set[str] = set()
    altlocs: set[str] = set()
    hetero: set[str] = set()
    chains: dict[str, list[int]] = {}
    zero_occupancy = 0
    failures = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        record = line[:6].strip().upper()
        if record == "MODEL":
            models.add(line[10:14].strip() or str(len(models) + 1))
        if record not in {"ATOM", "HETATM"}:
            continue
        atoms += 1
        if len(line) < 54:
            failures.append(f"{source.name}:{line_number}: truncated {record} coordinate record")
            continue
        try:
            coordinates = [float(line[start : start + 8]) for start in (30, 38, 46)]
        except ValueError:
            failures.append(f"{source.name}:{line_number}: invalid XYZ coordinate fields")
            continue
        if not all(math.isfinite(value) for value in coordinates):
            failures.append(f"{source.name}:{line_number}: non-finite XYZ coordinates")
            continue
        if not line[12:16].strip() or not line[17:20].strip():
            failures.append(f"{source.name}:{line_number}: missing atom or residue name")
            continue
        altloc = line[16:17].strip()
        if altloc:
            altlocs.add(altloc)
        try:
            occupancy = float(line[54:60].strip() or "1")
            if not math.isfinite(occupancy) or occupancy <= 0:
                zero_occupancy += 1
        except ValueError:
            zero_occupancy += 1
        residue = line[17:20].strip().upper()
        chain = line[21:22].strip() or "_"
        try:
            residue_number = int(line[22:26])
        except ValueError:
            failures.append(f"{source.name}:{line_number}: invalid residue number")
            continue
        chains.setdefault(chain, []).append(residue_number)
        if record == "HETATM" and residue not in PDB_WATER_RESIDUES and residue not in PDB_ION_RESIDUES:
            hetero.add(residue)
    if atoms == 0:
        failures.append(f"{source.name}: no PDB atoms found")
    gaps = []
    for chain, values in chains.items():
        unique = sorted(set(values))
        gaps.extend(f"{chain}:{left}-{right}" for left, right in zip(unique, unique[1:]) if right - left > 1)
    warnings = []
    if len(models) > 1:
        warnings.append("multiple MODEL records; confirm the intended conformer")
    if altlocs:
        warnings.append("alternate locations present; confirm occupancy/conformer selection")
    if gaps:
        warnings.append("residue-number gaps present; distinguish missing residues from chain numbering")
    if hetero:
        warnings.append("non-water HETATM residues present; confirm cofactors, metals, and ligands")
    if zero_occupancy:
        warnings.append("zero or invalid occupancy atoms present")
    rows = [
        f"source={source.name}",
        f"atoms={atoms}",
        f"models={max(1, len(models))}",
        f"altlocs={','.join(sorted(altlocs)) or 'none'}",
        f"hetero_residues={','.join(sorted(hetero)) or 'none'}",
        f"sequence_gaps={','.join(gaps) or 'none'}",
        f"zero_occupancy_atoms={zero_occupancy}",
    ]
    rows.extend(f"warning={item}" for item in warnings)
    rows.extend(f"failure={item}" for item in failures[:20])
    rows.extend(
        [
            "manual_checks=protonation,tautomers,termini,disulfides,missing_atoms,biological_assembly",
            f"status={'FAIL' if failures else 'REVIEW' if warnings else 'PASS'}",
        ]
    )
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")
    if failures:
        raise ValueError("structure preflight failed: " + "; ".join(failures[:3]))


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


def _prepare_replica_mdp(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
    output = ensure_under_root(workdir / str(data["output_mdp"]), workdir)
    text = source.read_text(encoding="utf-8")
    text = _patch_mdp_key(text, "continuation", "no")
    text = _patch_mdp_key(text, "gen_vel", "yes")
    text = _patch_mdp_key(text, "gen_seed", str(int(data.get("seed", -1))))
    output.write_text(text, encoding="utf-8")


def _prepare_unrestrained_npt_mdp(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
    output = ensure_under_root(workdir / str(data["output_mdp"]), workdir)
    text = source.read_text(encoding="utf-8")
    text = _patch_mdp_key(text, "define", None)
    text = _patch_mdp_key(text, "continuation", "yes")
    text = _patch_mdp_key(text, "gen_vel", "no")
    output.write_text(text, encoding="utf-8")


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


def _prepare_npt_mdp(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
    output = ensure_under_root(workdir / str(data["output_mdp"]), workdir)
    after_nvt = bool(data.get("after_nvt", False))
    text = source.read_text(encoding="utf-8")
    text = _patch_mdp_key(text, "continuation", "yes" if after_nvt else "no")
    text = _patch_mdp_key(text, "gen_vel", "no" if after_nvt else "yes")
    if not after_nvt:
        if not re.search(r"^\s*gen_temp\s*=", text, flags=re.MULTILINE):
            text = _patch_mdp_key(text, "gen_temp", "300")
        if not re.search(r"^\s*gen_seed\s*=", text, flags=re.MULTILINE):
            text = _patch_mdp_key(text, "gen_seed", "-1")
    output.write_text(text, encoding="utf-8")


def _parse_moleculetype(itp_path: Path) -> str | None:
    lines = itp_path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_section = False
    for line in lines:
        clean = line.split(";", 1)[0].strip()
        if not clean:
            continue
        if clean.lower() == "[ moleculetype ]":
            in_section = True
            continue
        if in_section and clean.startswith("["):
            return None
        if in_section:
            return clean.split()[0]
    return None


def _insert_ligand_includes(text: str, ligand_itp: str, posre_lig: str, define: str = "POSRES_LIG") -> str:
    include_lines = [f'#include "{ligand_itp}"']
    if posre_lig:
        include_lines.extend([f"#ifdef {define}", f'#include "{posre_lig}"', "#endif"])
    if ligand_itp in text:
        return text
    lines = text.splitlines()
    insert_at = None
    for index, line in enumerate(lines):
        if "forcefield.itp" in line:
            insert_at = index + 1
            break
    if insert_at is None:
        for index, line in enumerate(lines):
            if line.strip().lower() == "[ moleculetype ]":
                insert_at = index
                break
    if insert_at is None:
        insert_at = 0
    # Append after previously inserted ligand blocks so topol.top lists
    # ligands in input order.
    marker = "; Include ligand topology"
    last_block_start = None
    for index, line in enumerate(lines[insert_at:], insert_at):
        if line.strip() == marker:
            last_block_start = index
    if last_block_start is not None:
        index = last_block_start + 1
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped == "" or stripped.startswith(("#include", "#ifdef", "#endif")):
                index += 1
                continue
            break
        insert_at = index
    lines[insert_at:insert_at] = ["", marker, *include_lines, ""]
    return "\n".join(lines) + "\n"


def _append_ligand_molecule(text: str, molecule_name: str, count: int) -> str:
    lines = text.splitlines()
    molecules_index = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "[ molecules ]":
            molecules_index = index
    if molecules_index is None:
        lines.extend(["", "[ molecules ]", "; Compound        #mols"])
        molecules_index = len(lines) - 2
    for line in lines[molecules_index + 1 :]:
        clean = line.split(";", 1)[0].strip()
        if clean and clean.split()[0] == molecule_name:
            return "\n".join(lines) + "\n"
    lines.append(f"{molecule_name:<20} {max(1, int(count))}")
    return "\n".join(lines) + "\n"


def _patch_topology(workdir: Path, data: dict) -> None:
    topology = ensure_under_root(workdir / str(data["topology"]), workdir)
    ligand_entries = [dict(entry) for entry in data.get("ligands") or []]
    if not ligand_entries:
        ligand_entries = [
            {
                "itp": str(data["ligand_itp"]),
                "posre": str(data.get("posre_lig") or ""),
                "name": str(data.get("ligand_name") or "lig"),
                "count": int(data.get("ligand_count") or 1),
                "define": "POSRES_LIG",
            }
        ]
    if not ligand_entries:
        raise ValueError("patch_topology requires at least one ligand entry")
    text = topology.read_text(encoding="utf-8", errors="replace")
    included: dict[str, str] = {}
    for entry in ligand_entries:
        ligand_itp = str(entry["itp"])
        ligand_itp_path = ensure_under_root(workdir / ligand_itp, workdir)
        molecule_name = _parse_moleculetype(ligand_itp_path) or str(entry.get("name") or "lig")
        if ligand_itp in included and included[ligand_itp] != molecule_name:
            raise ValueError(
                f"Ligand itp {ligand_itp} defines moleculetype {included[ligand_itp]} but is also referenced as {molecule_name}"
            )
        included[ligand_itp] = molecule_name
        text = _insert_ligand_includes(text, ligand_itp, str(entry.get("posre") or ""), str(entry.get("define") or "POSRES_LIG"))
        text = _append_ligand_molecule(text, molecule_name, int(entry.get("count") or 1))
    topology.write_text(text, encoding="utf-8")


def _patch_equilibration_defines(workdir: Path, data: dict) -> None:
    defines = [str(item) for item in data.get("defines") or [] if str(item).strip()]
    value = " ".join(f"-D{name}" for name in defines)
    if not value:
        raise ValueError("patch_equilibration_defines requires at least one define")
    for name in data.get("mdps") or []:
        path = ensure_under_root(workdir / str(name), workdir)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(_patch_mdp_key(text, "define", value), encoding="utf-8")


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
