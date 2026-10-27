from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from .diagnostics import gpu_information, host_information
from .files import ensure_under_root
from .gromacs import detect_gromacs, utc_now
from .operations import current_operation


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if operation := current_operation():
                operation.checkpoint()
            digest.update(chunk)
    return digest.hexdigest()


def write_experiment_manifest(meta: dict, *, prepared_hashes: dict[str, dict] | None = None) -> dict:
    workdir = Path(meta["workdir"])
    selected_names = set(str(name) for name in meta.get("uploaded_files") or [])
    if any(Path(name).name.lower() in {"experiment-manifest.json", "execution-manifest.json"} for name in selected_names):
        raise ValueError("experiment-manifest.json and execution-manifest.json are reserved for generated manifests.")
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


@lru_cache(maxsize=1)
def execution_software() -> dict:
    """Resolve once per worker, including a source identifier for editable installs."""
    packages = {}
    for name in ("gromacs-web-ui", "numpy", "MDAnalysis", "biopython", "matplotlib", "fastapi"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    root = Path(__file__).resolve().parents[1]
    return {"captured_at": utc_now(), "python": platform.python_version(), "packages": packages, "source": _source_identity(root)}


def _source_identity(root: Path) -> dict:
    source = {"revision": None, "dirty": None, "installation_sha256": os.environ.get("GROMACS_CONSOLE_INSTALLATION_SHA256") or None}
    # Include untracked modules and protocol templates, which a Git diff alone
    # cannot identify. Contents never leave this process, only their digest.
    digest = hashlib.sha256()
    candidates = {root / name for name in ("pyproject.toml", "requirements.txt")}
    for directory, pattern in (("app", "*.py"), ("scripts", "*.py"), ("mdp", "*.mdp"), ("requirements", "*.lock")):
        candidates.update((root / directory).rglob(pattern))
    try:
        for path in sorted(candidates):
            if path.is_file() and not path.is_symlink():
                digest.update(path.relative_to(root).as_posix().encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        source["content_sha256"] = digest.hexdigest()
    except OSError:
        source["content_sha256"] = None
    if (root / ".git").exists():
        try:
            revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=2, check=True)
            status = subprocess.run(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    "app",
                    "scripts",
                    "mdp",
                    "requirements",
                    "pyproject.toml",
                    "requirements.txt",
                ],
                cwd=root,
                capture_output=True,
                timeout=2,
                check=True,
            )
            source.update(revision=revision.stdout.strip(), dirty=bool(status.stdout))
        except (OSError, subprocess.SubprocessError):
            pass
    return source


def _protocol_record(workdir: Path, name: str) -> dict:
    """Hash regular protocol files without traversing symlink components."""
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or any(part in {".", ".."} for part in parts):
        raise ValueError("Invalid protocol path")
    directory = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Protocol file is not a regular file")
        digest, mdp = hashlib.sha256(), []
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            if Path(name).suffix.lower() == ".mdp" and before.st_size <= 2 * 1024**2:
                mdp.append(chunk)
        after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Protocol file changed while hashing")
    record = {"path": name, "size": before.st_size, "sha256": digest.hexdigest()}
    if Path(name).suffix.lower() == ".mdp" and before.st_size <= 2 * 1024**2:
        parameters = {}
        for line in b"".join(mdp).decode("utf-8", errors="replace").splitlines():
            key, separator, value = line.split(";", 1)[0].partition("=")
            if separator and key.strip():
                parameters[key.strip().lower().replace("_", "-")] = value.strip()
        record["parameters"] = parameters
        record["seeds"] = {key: value for key, value in parameters.items() if key.endswith("-seed")}
        record["unresolved_seeds"] = [key for key, value in record["seeds"].items() if value == "-1"]
    return record


def write_execution_manifest(meta: dict, software: dict | None = None) -> dict:
    """Keep the submitted-input manifest intact and record protocols actually present after execution."""
    workdir = Path(meta["workdir"])
    if workdir.is_symlink():
        raise ValueError("Execution manifest work directory must not be a symbolic link")
    names = {path.name for suffix in ("*.mdp", "*.tpr") for path in workdir.glob(suffix)}
    for command in [*(meta.get("commands") or []), *(meta.get("step_runs") or [])]:
        args = command.get("args") or []
        for flag in ("-f", "-s", "-o", "-po"):
            if flag in args and args.index(flag) + 1 < len(args):
                name = str(args[args.index(flag) + 1])
                if Path(name).suffix.lower() in {".mdp", ".tpr"}:
                    names.add(name)
    files, unavailable = [], []
    for name in sorted(names):
        try:
            files.append(_protocol_record(workdir, name))
        except (OSError, ValueError):
            unavailable.append({"path": name, "reason": "Missing, unsafe, unreadable or changed during capture"})
    original = None
    try:
        original = _protocol_record(workdir, "experiment-manifest.json")
    except (OSError, ValueError):
        pass
    manifest = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "job": {
            key: meta.get(key)
            for key in ("id", "name", "workflow", "status", "dry_run", "started_at", "completed_at", "cancelled_at", "error")
        },
        "software": software if software is not None else execution_software(),
        "gromacs": detect_gromacs(str((meta.get("params") or {}).get("gmx_bin") or "gmx")),
        "input_manifest": original,
        "protocol_files": files,
        "unavailable_files": unavailable,
        "step_runs": meta.get("step_runs") or [],
        "commands": meta.get("commands") or [],
        "seed_note": "MDP seed -1 requests automatic generation; only explicit values and recorded GROMACS seed messages identify seeds.",
    }
    # Replacement of a symlink must never overwrite its target.
    target = workdir / "execution-manifest.json"
    if target.is_symlink():
        raise ValueError("Execution manifest target must not be a symbolic link")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=workdir, prefix=".execution-manifest-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manifest
