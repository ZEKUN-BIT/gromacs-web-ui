from __future__ import annotations

import fcntl
import hashlib
import heapq
import os
import re
import shutil
import stat
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from .operations import OperationContext


def copy_file_cancellable(source: Path, target: Path, operation: OperationContext) -> None:
    """Copy an input in bounded chunks, leaving cleanup to the staging owner."""
    copied, updated = 0, 0.0
    total = source.stat().st_size
    with source.open("rb") as reader, target.open("wb") as writer:
        while True:
            operation.checkpoint()
            chunk = reader.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            copied += len(chunk)
            tick = time.monotonic()
            if tick - updated >= 0.25:
                operation.report({"phase": "copy", "file": source.name, "copied_bytes": copied, "total_bytes": total})
                updated = tick
    operation.report({"phase": "copy", "file": source.name, "copied_bytes": copied, "total_bytes": total})
    shutil.copystat(source, target)


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


UPLOAD_CHUNK_BYTES = 1024 * 1024

MAX_UPLOAD_FILES = _positive_env_int("GROMACS_WEB_MAX_UPLOAD_FILES", 20)

MAX_UPLOAD_FILE_BYTES = _positive_env_int("GROMACS_WEB_MAX_UPLOAD_FILE_BYTES", 5 * 1024**3)

MAX_UPLOAD_TOTAL_BYTES = _positive_env_int("GROMACS_WEB_MAX_UPLOAD_TOTAL_BYTES", 20 * 1024**3)

MAX_LOG_TAIL_BYTES = 2_000_000

DEFAULT_LOG_TAIL_BYTES = 300_000

FILE_REFERENCE_FIELDS = {
    "structure_file",
    "protein_file",
    "ligand_structure_file",
    "ligand_chemistry_file",
    "ligand_gro_file",
    "ligand_itp_file",
    "tpr_file",
    "trajectory_file",
    "index_file",
    "edr_file",
}

LIGAND_FILE_FIELDS = ("gro_file", "itp_file", "structure_file", "chemistry_file")

GENERATED_PDB_PATTERN = re.compile(r"^[A-Za-z0-9._-]+_from_complex\.pdb$")

RESERVED_UPLOAD_NAMES = {"experiment-manifest.json", "execution-manifest.json", "metadata.json", "run.log"}


def safe_filename(name: str) -> str:
    raw = str(name or "").replace("\\", "/")
    clean = raw.rsplit("/", 1)[-1]
    clean = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return clean or f"upload-{uuid4().hex[:8]}"


def ensure_under_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"{resolved} is outside {root_resolved}")
    return resolved


def _bool_value(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _raw_upload_filename(filename: str) -> str:
    return str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]


def _rewrite_uploaded_file_references(params: dict, name_map: dict[str, str]) -> None:
    for field in FILE_REFERENCE_FIELDS:
        value = str(params.get(field) or "").strip()
        if value in name_map:
            params[field] = name_map[value]
    for ligand in params.get("ligands") or []:
        if not isinstance(ligand, dict):
            continue
        for field in LIGAND_FILE_FIELDS:
            value = str(ligand.get(field) or "").strip()
            if value in name_map:
                ligand[field] = name_map[value]


async def stage_uploads(
    files: list[UploadFile],
    temp_dir: Path,
    max_files: int = MAX_UPLOAD_FILES,
    max_file_bytes: int = MAX_UPLOAD_FILE_BYTES,
    max_total_bytes: int = MAX_UPLOAD_TOTAL_BYTES,
    *,
    hashes: dict[str, dict] | None = None,
) -> tuple[list[str], dict[str, str]]:
    uploads = [upload for upload in files if upload.filename]
    if len(uploads) > max_files:
        raise HTTPException(status_code=413, detail=f"Too many files; maximum is {max_files}.")

    uploaded_names: list[str] = []
    name_map: dict[str, str] = {}
    seen: set[str] = set()
    for upload in uploads:
        raw_filename = _raw_upload_filename(upload.filename or "")
        filename = safe_filename(raw_filename)
        key = filename.casefold()
        if key in RESERVED_UPLOAD_NAMES:
            raise HTTPException(status_code=400, detail=f"File '{raw_filename}' uses a reserved task filename.")
        if key in seen or raw_filename in name_map:
            raise HTTPException(status_code=422, detail=f"输入文件名重复或规范化后重名：{raw_filename}。请改名并重新选择文件。")
        seen.add(key)
        name_map[raw_filename] = filename

    total_bytes = 0
    for upload in uploads:
        filename = name_map[_raw_upload_filename(upload.filename or "")]
        target = ensure_under_root(temp_dir / filename, temp_dir)
        file_bytes = 0
        digest = hashlib.sha256()

        def write_chunk(handle, chunk: bytes) -> None:
            handle.write(chunk)
            digest.update(chunk)

        try:
            with target.open("wb") as handle:
                while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > max_file_bytes:
                        raise HTTPException(status_code=413, detail=f"File '{filename}' exceeds the upload limit.")
                    if total_bytes > max_total_bytes:
                        raise HTTPException(status_code=413, detail="Total upload size exceeds the request limit.")
                    await run_in_threadpool(write_chunk, handle, chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        uploaded_names.append(filename)
        if hashes is not None:
            stat = target.stat()
            hashes[filename] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "inode": stat.st_ino,
                "sha256": digest.hexdigest(),
            }
    return uploaded_names, name_map


def _read_log_tail(path: Path, tail: int) -> str:
    byte_count = max(0, min(int(tail), MAX_LOG_TAIL_BYTES))
    if not byte_count:
        return ""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return ""
    with handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        size = os.fstat(handle.fileno()).st_size
        handle.seek(max(0, size - byte_count))
        text = handle.read(byte_count).decode("utf-8", errors="replace")
    return f"[web] log truncated by server to last {byte_count // 1024} KB\n{text}" if size > byte_count else text


def validate_uploaded_file_references(params: dict, uploaded_names: list[str]) -> None:
    uploaded = set(uploaded_names)

    def check(field: str, label: str, allow_generated: set[str] | None = None) -> None:
        value = str(params.get(field) or "").strip()
        if not value or value in uploaded or value in (allow_generated or set()):
            return
        raise HTTPException(
            status_code=400,
            detail=f"{label} references '{value}', but that file is not in the current upload list.",
        )

    def check_in(ligand: dict, field: str, label: str, allow_generated: set[str] | None = None) -> None:
        value = str(ligand.get(field) or "").strip()
        if not value or value in uploaded or value in (allow_generated or set()):
            return
        if GENERATED_PDB_PATTERN.match(value):
            return
        raise HTTPException(
            status_code=400,
            detail=f"{label} references '{value}', but that file is not in the current upload list.",
        )

    workflow = str(params.get("workflow") or "")
    if workflow in {"protein_md", "em_only"}:
        check("structure_file", "Structure file")
    if workflow == "protein_ligand_md":
        check("protein_file", "Protein file")
        ligands = [item for item in (params.get("ligands") or []) if isinstance(item, dict)]
        if ligands:
            generated = {"ligand_from_complex.pdb", *(f"{item.get('key') or ''}_from_complex.pdb" for item in ligands)}
            for index, ligand in enumerate(ligands, 1):
                label = f"Ligand {index}"
                if str(ligand.get("mode") or "") == "prepared":
                    check_in(ligand, "gro_file", f"{label} .gro")
                    check_in(ligand, "itp_file", f"{label} .itp")
                check_in(ligand, "structure_file", f"{label} structure file", generated)
                check_in(ligand, "chemistry_file", f"{label} chemistry file")
        else:
            if str(params.get("ligand_mode") or "") == "prepared":
                check("ligand_gro_file", "Ligand .gro")
                check("ligand_itp_file", "Ligand .itp")
            check("ligand_structure_file", "Ligand structure file", {"ligand_from_complex.pdb"})
            check("ligand_chemistry_file", "Ligand chemistry file")
    if workflow in {"run_tpr", "analysis_rmsd", "postprocess", "analysis_suite"}:
        check("tpr_file", "TPR file")
    if workflow in {"analysis_rmsd", "postprocess", "analysis_suite"}:
        check("trajectory_file", "Trajectory file")
        check("index_file", "Index file")
    if workflow == "analysis_suite" and _bool_value(params.get("do_energy"), True):
        check("edr_file", "EDR file")


LOG_ROTATION_PREFIX = b"\n[web] log rotated by worker (older output dropped) at "


def read_log_chunk(path: Path, offset: int, limit: int, generation: str | None = None, *, from_tail: bool = False) -> dict:
    """Read the cursor identity, size and bytes from a single locked snapshot."""
    offset, limit = max(0, int(offset)), max(1, int(limit))
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return {"text": "", "start": 0, "offset": 0, "size": 0, "generation": None, "reset": offset > 0 or generation is not None}
    with handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        info = os.fstat(handle.fileno())
        size = info.st_size
        current = f"{info.st_dev:x}-{info.st_ino:x}"
        if handle.read(len(LOG_ROTATION_PREFIX)) == LOG_ROTATION_PREFIX:
            marker = handle.readline(128)
            if marker.endswith(b"\n"):
                current += "-" + hashlib.sha256(marker).hexdigest()[:32]
        reset = offset > size or (generation is not None and generation != current)
        start = 0 if reset else max(0, size - limit) if from_tail else offset
        handle.seek(start)
        data = handle.read(limit + 4)
    while data:
        try:
            text = data.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.end == len(data):
                data = data[: exc.start]
                continue
            text = data.decode("utf-8", errors="replace")
            break
    else:
        text = ""
    return {
        "text": text,
        "start": start,
        "offset": start + len(data),
        "size": size,
        "generation": current,
        "reset": reset,
    }


def relative_files(workdir: Path, max_files: int = 1000) -> list[dict]:
    items = []
    limit = max(1, int(max_files))
    for path in workdir.rglob("*"):
        try:
            if path.is_symlink() or path.is_dir():
                continue
            rel = path.relative_to(workdir).as_posix()
            if rel == "metadata.json":
                continue
            stat = path.stat()
            items.append({"path": rel, "size": stat.st_size, "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()})
            if len(items) >= limit:
                break
        except OSError:
            continue
    return sorted(items, key=lambda item: item["path"])


_PREFERRED_STRUCTURE = re.compile(r"(?:^|/)(?:confout|final|md(?:_[^/]*)?)\.(?:gro|pdb)$", re.I)


def relative_file_page(workdir: Path, *, search: str, offset: int, limit: int, structures_only: bool = False) -> dict:
    """Search every regular output without following directory or file links."""
    total = 0
    needle = search.casefold()

    def matches():
        nonlocal total
        for directory, folders, filenames in os.walk(workdir, followlinks=False):
            folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
            for name in filenames:
                if structures_only and not name.lower().endswith((".pdb", ".gro")):
                    continue
                path = Path(directory) / name
                rel = path.relative_to(workdir).as_posix()
                if rel == "metadata.json" or needle not in rel.casefold():
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                total += 1
                yield {"path": rel, "size": info.st_size, "modified": datetime.fromtimestamp(info.st_mtime).isoformat()}

    def rank(item):
        path = item["path"]
        return (structures_only and not _PREFERRED_STRUCTURE.search(path), path)

    items = heapq.nsmallest(offset + limit, matches(), key=rank)
    return {
        "files": items[offset : offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
        "truncated": False,
    }
