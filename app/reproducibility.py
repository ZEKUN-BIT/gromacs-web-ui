from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .diagnostics import gpu_information, host_information
from .files import ensure_under_root
from .gromacs import detect_gromacs, utc_now


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_experiment_manifest(meta: dict) -> dict:
    workdir = Path(meta["workdir"])
    selected_names = set(str(name) for name in meta.get("uploaded_files") or [])
    selected_names.update(path.name for path in workdir.glob("*.mdp") if path.is_file())
    files = []
    for name in sorted(selected_names):
        try:
            path = ensure_under_root(workdir / name, workdir)
        except ValueError:
            continue
        if path.is_file():
            files.append({"path": name, "size": path.stat().st_size, "sha256": sha256_file(path)})
    gmx_bin = str((meta.get("params") or {}).get("gmx_bin") or "gmx")
    manifest = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "job": {"id": meta.get("id"), "name": meta.get("name"), "workflow": meta.get("workflow")},
        "gromacs": detect_gromacs(gmx_bin),
        "commands": meta.get("commands") or [],
        "parameters": meta.get("params") or {},
        "analysis_source": meta.get("analysis_source"),
        "input_files": files,
        "force_fields": meta.get("local_force_fields")
        or [{"name": (meta.get("params") or {}).get("force_field"), "source": "GROMACS installation"}],
        "mdp_templates": meta.get("mdp_templates") or [],
        "mdp_overrides": meta.get("mdp_overrides") or [],
        "host": host_information(),
        "gpu": gpu_information(),
    }
    target = ensure_under_root(workdir / "experiment-manifest.json", workdir)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
