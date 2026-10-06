"""Multi-ligand normalization and naming for the protein-ligand workflow.

`SimulationParams.ligands` holds zero or more per-ligand specs.  When the list
is empty, the workflow falls back to the legacy single-ligand fields
(`ligand_name`, `ligand_gro_file`, ...) so old API clients and saved jobs keep
working.  `normalize_ligands` converts either shape into one uniform list of
plain dicts used by the workflow builder and the internal steps.

Naming rules (kept deterministic so command previews and stdin override
templates stay stable):

- A single ligand with key ``lig`` keeps the legacy working file names
  (`ligand_GMX.gro`, `ligand_GMX.itp`, `posre_lig.itp`, `ligand_from_complex.pdb`),
  the legacy index group ``Ligand`` and the legacy define macro ``POSRES_LIG``.
- Any other key uses ``{key}_GMX.gro`` / ``{key}_GMX.itp`` / ``posre_{key}.itp``
  / ``{key}_from_complex.pdb``, index group ``Ligand_{key}`` and define macro
  ``POSRES_{KEY}``.  With more than one ligand, even key ``lig`` uses
  ``Ligand_{key}`` so every ligand gets a distinct group.
"""

from __future__ import annotations

import re
from pathlib import Path

LEGACY_KEY = "lig"
LEGACY_LIGAND_FIELDS = {
    "ligand_mode": "mode",
    "ligand_name": "name",
    "ligand_count": "count",
    "ligand_gro_file": "gro_file",
    "ligand_itp_file": "itp_file",
    "ligand_structure_file": "structure_file",
    "ligand_chemistry_file": "chemistry_file",
    "ligand_smiles": "smiles",
    "ligand_protonation_mode": "protonation_mode",
    "ligand_charge": "charge",
    "ligand_charge_confirmed": "charge_confirmed",
    "ligand_ph": "ph",
    "ligand_posres": "posres",
}
SPEC_FILE_FIELDS = ("gro_file", "itp_file", "structure_file", "chemistry_file")


def ligand_slug(value: str, fallback: str = "lig") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    return value[:64] or fallback


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ligand_define(key: str) -> str:
    return f"POSRES_{ligand_slug(key, LEGACY_KEY).upper()}"


def ligand_group(key: str, total: int) -> str:
    return "Ligand" if total == 1 else f"Ligand_{key}"


def ligand_work_names(ligand: dict, total: int) -> dict:
    """Working-file names, index group and define macro for one ligand."""
    key = ligand["key"]
    legacy = key == LEGACY_KEY
    return {
        "work_gro": "ligand_GMX.gro" if legacy else f"{key}_GMX.gro",
        "work_itp": "ligand_GMX.itp" if legacy else f"{key}_GMX.itp",
        "work_posre": "posre_lig.itp" if legacy else f"posre_{key}.itp",
        "index_lig": "index_lig.ndx" if legacy else f"index_{key}.ndx",
        "extracted_pdb": "ligand_from_complex.pdb" if legacy else f"{key}_from_complex.pdb",
        "chemistry_sdf": "ligand_chemistry.sdf" if legacy else f"{key}_chemistry.sdf",
        "chemistry_smi": "ligand_chemistry.smi" if legacy else f"{key}_chemistry.smi",
        "pose_sdf": "ligand_pose_chemistry.sdf" if legacy else f"{key}_pose_chemistry.sdf",
        "prepared_sdf": "ligand_prepared.sdf" if legacy else f"{key}_prepared.sdf",
        "parameter_sdf": "ligand_parameterization.sdf" if legacy else f"{key}_parameterization.sdf",
        "chemistry_report": "ligand-chemistry-map.txt" if legacy else f"{key}-chemistry-map.txt",
        "pose_restore_report": "ligand-pose-restore.txt" if legacy else f"{key}-pose-restore.txt",
        "acpype_dir": f"{key}.acpype",
        "group": ligand_group(key, total),
        "define": ligand_define(key),
    }


def ligand_title(ligand: dict, template: str, keyed_template: str | None = None) -> str:
    """Step title: legacy keys keep the plain template, others get the key."""
    if ligand["key"] == LEGACY_KEY:
        return template
    return (keyed_template or template).format(key=ligand["key"])


def _validated_spec(key: str, spec: dict, index: int) -> dict:
    mode = str(spec.get("mode") or "prepared").strip()
    if mode not in {"prepared", "acpype"}:
        raise ValueError(f"ligand '{key}': mode must be prepared or acpype")
    protonation = str(spec.get("protonation_mode") or "preserve").strip()
    if protonation not in {"preserve", "ph"}:
        raise ValueError(f"ligand '{key}': protonation_mode must be preserve or ph")
    smiles = str(spec.get("smiles") or "").strip()
    if len(smiles) > 2000 or any(character in smiles for character in "\r\n\0"):
        message = "ligand_smiles must be a single line no longer than 2000 characters"
        if key != LEGACY_KEY:
            message = f"ligand '{key}': smiles must be a single line no longer than 2000 characters"
        raise ValueError(message)
    chemistry = str(spec.get("chemistry_file") or "").strip()
    if chemistry and Path(chemistry).suffix.lower() not in {".sdf", ".mol2"}:
        raise ValueError(f"ligand '{key}': chemistry_file must be an .sdf or .mol2 file")
    if chemistry and smiles:
        raise ValueError(f"ligand '{key}': provide either chemistry_file or smiles, not both")
    charge = _as_int(spec.get("charge"), 0)
    try:
        ph = float(spec.get("ph", 7.4))
    except (TypeError, ValueError):
        ph = 7.4
    if not 0 <= ph <= 14:
        message = "ligand_ph must be between 0 and 14"
        if key != LEGACY_KEY:
            message = f"ligand '{key}': ph must be between 0 and 14"
        raise ValueError(message)
    count = _as_int(spec.get("count"), 1)
    if count < 1:
        raise ValueError(f"ligand '{key}': count must be at least 1")
    return {
        "index": index,
        "key": key,
        "name": ligand_slug(str(spec.get("name") or ""), key),
        "residue": str(spec.get("residue") or "").strip().upper(),
        "mode": mode,
        "count": count,
        "gro_file": str(spec.get("gro_file") or "").strip(),
        "itp_file": str(spec.get("itp_file") or "").strip(),
        "structure_file": str(spec.get("structure_file") or "").strip(),
        "chemistry_file": chemistry,
        "smiles": smiles,
        "protonation_mode": protonation,
        "charge": charge,
        "charge_confirmed": _as_bool(spec.get("charge_confirmed"), False),
        "ph": ph,
        "posres": _as_bool(spec.get("posres", True), True),
    }


def normalize_ligands(params: dict) -> list[dict]:
    """Return uniform per-ligand dicts from `ligands` or legacy fields."""
    raw = params.get("ligands") or []
    if not isinstance(raw, list):
        raw = []
    used_keys: set[str] = set()
    ligands: list[dict] = []
    for index, item in enumerate(raw, 1):
        spec = dict(item) if isinstance(item, dict) else {}
        key = ligand_slug(str(spec.get("key") or ""), f"lig{index}")
        if key in used_keys:
            raise ValueError(f"duplicate ligand key '{key}'; ligand keys must be unique")
        used_keys.add(key)
        ligands.append(_validated_spec(key, spec, index))
    if ligands:
        # Explicit list: legacy "complex_ligand_residue" does not apply; residue
        # must live in each spec.  Legacy global fields must not be set.
        return ligands
    legacy = {
        "key": LEGACY_KEY,
        "name": _str_param(params, "ligand_name", "lig"),
        "residue": _str_param(params, "complex_ligand_residue", "").strip().upper(),
        "mode": _str_param(params, "ligand_mode", "prepared"),
        "count": max(1, _as_int(params.get("ligand_count"), 1)),
        "gro_file": _str_param(params, "ligand_gro_file", ""),
        "itp_file": _str_param(params, "ligand_itp_file", ""),
        "structure_file": _str_param(params, "ligand_structure_file", ""),
        "chemistry_file": _str_param(params, "ligand_chemistry_file", ""),
        "smiles": _str_param(params, "ligand_smiles", ""),
        "protonation_mode": _str_param(params, "ligand_protonation_mode", "preserve"),
        "charge": _as_int(params.get("ligand_charge"), 0),
        "charge_confirmed": _as_bool(params.get("ligand_charge_confirmed"), False),
        "ph": _as_float(params.get("ligand_ph"), 7.4),
        "posres": _as_bool(params.get("ligand_posres", True), True),
    }
    return [_validated_spec(LEGACY_KEY, legacy, 1)]


def _str_param(params: dict, key: str, default: str = "") -> str:
    value = params.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
