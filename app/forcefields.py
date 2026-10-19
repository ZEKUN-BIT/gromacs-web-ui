"""Discover, select, and stage GROMACS force fields and water models."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

from .files import ensure_under_root
from .steps import Step

PDB2GMX_CLI_WATERS = {"select", "none", "spc", "spce", "tip3p", "tip4p", "tip5p", "tips3p"}


KNOWN_WATER_MENU_SELECTIONS = {"amber19sb": {"opc": "1", "opc3": "2", "tip4pew": "6"}}


def normalize_force_field_name(value: str | None, default: str = "amber19sb") -> str:
    raw = str(value or "").strip() or default
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name.lower().endswith(".ff"):
        name = name[:-3]
    return name or default


def local_force_field_roots(base_dir: Path) -> list[Path]:
    return [base_dir, base_dir / "forcefields"]


def parse_watermodels(path: Path) -> list[dict]:
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
    for root in local_force_field_roots(base_dir):
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
                    "water_models": parse_watermodels(source / "watermodels.dat"),
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
        models = parse_watermodels(top_dir / f"{normalize_force_field_name(force_field)}.ff" / "watermodels.dat")
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
