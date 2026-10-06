from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile


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


def _unique_safe_filename(filename: str, used: set[str]) -> str:
    candidate = filename
    suffix = Path(filename).suffix
    stem = filename[: -len(suffix)] if suffix else filename
    index = 2
    while candidate in used:
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    return candidate


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
) -> tuple[list[str], dict[str, str]]:
    uploads = [upload for upload in files if upload.filename]
    if len(uploads) > max_files:
        raise HTTPException(status_code=413, detail=f"Too many files; maximum is {max_files}.")

    uploaded_names: list[str] = []
    name_map: dict[str, str] = {}
    used_names: set[str] = set()
    total_bytes = 0
    for upload in uploads:
        raw_filename = _raw_upload_filename(upload.filename or "")
        filename = _unique_safe_filename(safe_filename(raw_filename), used_names)
        used_names.add(filename)
        name_map[raw_filename] = filename
        target = ensure_under_root(temp_dir / filename, temp_dir)
        file_bytes = 0
        try:
            with target.open("wb") as handle:
                while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > max_file_bytes:
                        raise HTTPException(status_code=413, detail=f"File '{filename}' exceeds the upload limit.")
                    if total_bytes > max_total_bytes:
                        raise HTTPException(status_code=413, detail="Total upload size exceeds the request limit.")
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        uploaded_names.append(filename)
    return uploaded_names, name_map


def _read_log_tail(path: Path, tail: int) -> str:
    if not path.exists():
        return ""
    byte_count = max(0, min(int(tail), MAX_LOG_TAIL_BYTES))
    if byte_count == 0:
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > byte_count:
            handle.seek(size - byte_count)
            text = handle.read().decode("utf-8", errors="replace")
            return f"[web] log truncated by server to last {byte_count // 1024} KB\n{text}"
        return handle.read().decode("utf-8", errors="replace")


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


def read_log_chunk(path: Path, offset: int, limit: int) -> dict:
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    if not path.exists():
        return {"text": "", "start": 0, "offset": 0, "size": 0, "reset": offset > 0}
    size = path.stat().st_size
    reset = offset > size
    start = 0 if reset else offset
    with path.open("rb") as handle:
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
