from __future__ import annotations

import hashlib
import json
import tempfile
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


def write_experiment_manifest(meta: dict, *, prepared_hashes: dict[str, dict] | None = None) -> dict:
    workdir = Path(meta["workdir"])
    selected_names = set(str(name) for name in meta.get("uploaded_files") or [])
    if any(Path(name).name == "experiment-manifest.json" for name in selected_names):
        raise ValueError("experiment-manifest.json is reserved for the generated experiment manifest.")
    selected_names.update(path.name for path in workdir.glob("*.mdp") if path.is_file())
    files = []
    for name in sorted(selected_names):
        try:
            path = ensure_under_root(workdir / name, workdir)
        except ValueError:
            continue
        if path.is_file():
            stat = path.stat()
            prepared = (prepared_hashes or {}).get(name, {})
            unchanged = (prepared.get("size"), prepared.get("mtime_ns"), prepared.get("inode")) == (
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ino,
            )
            digest = prepared["sha256"] if unchanged else sha256_file(path)
            files.append({"path": name, "size": stat.st_size, "sha256": digest})
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
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=workdir, prefix=".experiment-manifest-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manifest
