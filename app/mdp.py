"""MDP templates, protocol previews, overrides, and per-stage preparation."""

from __future__ import annotations

import math
import re
from pathlib import Path

from .files import ensure_under_root
from .workflow_parameters import bool_param, int_param

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


def load_mdp_folder(templates: dict[str, dict], folder: Path) -> None:
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

    load_mdp_folder(templates, base_dir / "sample_mdp")
    load_mdp_folder(templates, base_dir / "mdp")

    profile = str(profile or "").strip()
    for folder_name in MDP_PROFILE_DIRS.get(profile, ()):
        load_mdp_folder(templates, base_dir / "mdp" / folder_name)
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


def patch_mdp_key(text: str, key: str, value: str | None) -> str:
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


def read_mdp_values(text: str) -> dict[str, str]:
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
    if bool_param(params, "pre_nvt", False):
        stages.append(("NVT", "nvt.mdp", 1))
    stages.append(("NPT", "npt.mdp", 1))
    if bool_param(params, "release_restraints", True):
        stages.append(("NPT unrestrained", "npt.mdp", 1))
    stages.append(("Production", "md.mdp", int_param(params, "replicas", 3)))
    rows = []
    for stage, filename, replicas in stages:
        text = uploaded_mdp.get(filename, templates[filename]["content"])
        source = "uploaded" if filename in uploaded_mdp else templates[filename]["source"]
        changed = []
        for item in plan:
            if item["file"] == filename:
                text = patch_mdp_key(text, item["key"], None if item["value"] == "<remove>" else item["value"])
                changed.append(item["key"])
        values = read_mdp_values(text)
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


def optional_text(params: dict, key: str) -> str:
    return str(params.get(key) or "").strip()


def optional_float(params: dict, key: str, label: str, positive: bool = True) -> float | None:
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


def format_mdp_number(value: float) -> str:
    return f"{value:.8g}"


def tc_group_count(params: dict) -> int:
    groups = optional_text(params, "mdp_tc_grps")
    if not groups:
        groups = "Protein_Lig Water_and_Ions" if params.get("workflow") == "protein_ligand_md" else "Protein Water_and_Ions"
    return max(1, len(groups.split()))


def repeat_for_tc_groups(value: float, count: int) -> str:
    token = format_mdp_number(value)
    return " ".join(token for _ in range(count))


def nsteps_from_ns(duration_ns: float, dt_ps: float, label: str) -> int:
    steps = round((duration_ns * 1000.0) / dt_ps)
    if steps <= 0:
        raise ValueError(f"{label} produces zero MD steps; increase duration or reduce dt.")
    return steps


def mdp_override_add(plan: list[dict], stage: str, key: str, value: str | None, label: str) -> None:
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
    if not bool_param(params, "mdp_override_enabled", False):
        return []

    workflow = str(params.get("workflow") or "")
    if workflow not in {"protein_md", "protein_ligand_md"}:
        return []

    plan: list[dict] = []
    dt = optional_float(params, "mdp_dt_ps", "MDP dt")
    if dt is not None:
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "dt", format_mdp_number(dt), "Time step")

    for key, stage, label in (
        ("mdp_nvt_ns", "nvt", "NVT duration"),
        ("mdp_npt_ns", "npt", "NPT duration"),
        ("mdp_md_ns", "md", "Production duration"),
    ):
        duration = optional_float(params, key, label)
        if duration is not None:
            if dt is None:
                raise ValueError(f"{label} requires setting MDP dt.")
            mdp_override_add(plan, stage, "nsteps", str(nsteps_from_ns(duration, dt, label)), label)

    group_count = tc_group_count(params)
    temperature = optional_float(params, "mdp_temperature_k", "MDP temperature")
    if temperature is not None:
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "ref_t", repeat_for_tc_groups(temperature, group_count), "Temperature")
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "gen_temp", format_mdp_number(temperature), "Initial velocity temperature")

    pressure = optional_float(params, "mdp_pressure_bar", "MDP pressure", positive=False)
    if pressure is not None:
        for stage in ("npt", "md"):
            mdp_override_add(plan, stage, "ref_p", format_mdp_number(pressure), "Pressure")

    tc_grps = optional_text(params, "mdp_tc_grps")
    if tc_grps:
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "tc-grps", tc_grps, "Temperature coupling groups")

    tau_t = optional_float(params, "mdp_tau_t_ps", "MDP tau_t")
    if tau_t is not None:
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "tau_t", repeat_for_tc_groups(tau_t, group_count), "Thermostat tau_t")

    define = optional_text(params, "mdp_define_equil")
    if define:
        value = None if define == "__none__" else define
        for stage in ("nvt", "npt"):
            mdp_override_add(plan, stage, "define", value, "Position restraint define")

    gen_vel = optional_text(params, "mdp_gen_vel")
    if gen_vel in {"yes", "no"}:
        mdp_override_add(plan, "nvt", "gen_vel", gen_vel, "Velocity generation")

    pcoupl = optional_text(params, "mdp_pcoupl")
    if pcoupl:
        for stage in ("npt", "md"):
            mdp_override_add(plan, stage, "pcoupl", pcoupl, "Pressure coupling")

    constraints = optional_text(params, "mdp_constraints")
    if constraints:
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "constraints", constraints, "Constraints")

    for param_key, mdp_key, label in (
        ("mdp_rcoulomb", "rcoulomb", "Coulomb cutoff"),
        ("mdp_rvdw", "rvdw", "Van der Waals cutoff"),
    ):
        cutoff = optional_float(params, param_key, label)
        if cutoff is not None:
            for stage in MDP_ALL_PREP_STAGES:
                mdp_override_add(plan, stage, mdp_key, format_mdp_number(cutoff), label)

    output_interval = optional_float(params, "mdp_output_interval_ps", "MDP output interval")
    if output_interval is not None:
        if dt is None:
            raise ValueError("MDP output interval requires setting MDP dt.")
        interval_steps = max(1, round(output_interval / dt))
        for stage in MDP_DYNAMICS_STAGES:
            mdp_override_add(plan, stage, "nstenergy", str(interval_steps), "Energy output interval")
            mdp_override_add(plan, stage, "nstlog", str(interval_steps), "Log output interval")
        mdp_override_add(plan, "md", "nstxout-compressed", str(interval_steps), "Compressed trajectory interval")

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
            text = patch_mdp_key(text, str(item["key"]), value)
        path.write_text(text, encoding="utf-8")
    return plan


def prepare_replica_mdp(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
    output = ensure_under_root(workdir / str(data["output_mdp"]), workdir)
    text = source.read_text(encoding="utf-8")
    text = patch_mdp_key(text, "continuation", "no")
    text = patch_mdp_key(text, "gen_vel", "yes")
    text = patch_mdp_key(text, "gen_seed", str(int(data.get("seed", -1))))
    output.write_text(text, encoding="utf-8")


def prepare_unrestrained_npt_mdp(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
    output = ensure_under_root(workdir / str(data["output_mdp"]), workdir)
    text = source.read_text(encoding="utf-8")
    text = patch_mdp_key(text, "define", None)
    text = patch_mdp_key(text, "continuation", "yes")
    text = patch_mdp_key(text, "gen_vel", "no")
    output.write_text(text, encoding="utf-8")


def prepare_npt_mdp(workdir: Path, data: dict) -> None:
    source = ensure_under_root(workdir / str(data["source_mdp"]), workdir)
    output = ensure_under_root(workdir / str(data["output_mdp"]), workdir)
    after_nvt = bool(data.get("after_nvt", False))
    text = source.read_text(encoding="utf-8")
    text = patch_mdp_key(text, "continuation", "yes" if after_nvt else "no")
    text = patch_mdp_key(text, "gen_vel", "no" if after_nvt else "yes")
    if not after_nvt:
        if not re.search(r"^\s*gen_temp\s*=", text, flags=re.MULTILINE):
            text = patch_mdp_key(text, "gen_temp", "300")
        if not re.search(r"^\s*gen_seed\s*=", text, flags=re.MULTILINE):
            text = patch_mdp_key(text, "gen_seed", "-1")
    output.write_text(text, encoding="utf-8")


def patch_equilibration_defines(workdir: Path, data: dict) -> None:
    defines = [str(item) for item in data.get("defines") or [] if str(item).strip()]
    value = " ".join(f"-D{name}" for name in defines)
    if not value:
        raise ValueError("patch_equilibration_defines requires at least one define")
    for name in data.get("mdps") or []:
        path = ensure_under_root(workdir / str(name), workdir)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        path.write_text(patch_mdp_key(text, "define", value), encoding="utf-8")
