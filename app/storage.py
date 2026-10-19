"""Preview-bound cleanup of inactive task files inside the runtime directory."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .execution import live_process_identities

PREVIEW_TTL_SECONDS = 600
MAX_PREVIEWS = 16
MAX_PREVIEW_ENTRIES = 100_000
_PREVIEWS: dict[str, dict] = {}
_PREVIEW_LOCK = threading.Lock()
_TERMINAL = {"completed", "cancelled"}
_BACKUP = re.compile(r"^#(.+)\.\d+#$")
_TRAJECTORIES = {".xtc", ".trr", ".tng"}


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[str] = Field(min_length=1, max_length=200)
    mode: Literal["intermediates", "delete_jobs"] = "intermediates"
    include_logs: bool = False
    preview_token: str | None = Field(default=None, max_length=128)

    @field_validator("job_ids")
    @classmethod
    def validate_job_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or len(value) > 255 or value in {".", ".."} or any(char in value for char in ("/", "\\", "\x00")):
                raise ValueError("任务 ID 无效。")
        return list(dict.fromkeys(values))


def _job_fd(store, job_id: str) -> int:
    """Open each boundary without following a symlink, including on unlink."""
    CleanupRequest(job_ids=[job_id])
    runtime = Path(store.runtime_root).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(runtime, flags)
    try:
        jobs_fd = os.open("jobs", flags, dir_fd=root_fd)
    finally:
        os.close(root_fd)
    try:
        return os.open(job_id, flags, dir_fd=jobs_fd)
    finally:
        os.close(jobs_fd)


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _scan_fd(directory_fd: int, *, tolerate_changes: bool = False) -> dict:
    files, entries, unsafe = {}, {}, []

    def visit(parent_fd: int, prefix: str = "") -> None:
        with os.scandir(parent_fd) as children:
            for child in children:
                relative = f"{prefix}{child.name}"
                try:
                    info = os.stat(child.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    if tolerate_changes:
                        continue
                    raise
                entries[relative] = _fingerprint(info)
                if stat.S_ISLNK(info.st_mode):
                    unsafe.append(relative)
                elif stat.S_ISDIR(info.st_mode):
                    try:
                        child_fd = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    except OSError:
                        if tolerate_changes:
                            unsafe.append(relative)
                            continue
                        raise
                    try:
                        try:
                            visit(child_fd, relative + "/")
                        except OSError:
                            if not tolerate_changes:
                                raise
                            unsafe.append(relative)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode):
                    files[relative] = {"size": info.st_size, "fingerprint": _fingerprint(info)}
                else:
                    unsafe.append(relative)

    visit(directory_fd)
    return {"files": files, "entries": entries, "unsafe": unsafe, "directory": _fingerprint(os.fstat(directory_fd))[:3]}


def _scan_job(store, job_id: str) -> dict:
    directory_fd = _job_fd(store, job_id)
    try:
        return _scan_fd(directory_fd)
    finally:
        os.close(directory_fd)


def _protection_reason(store, meta: dict) -> str | None:
    if meta.get("status") not in _TERMINAL:
        return "任务尚未完成或取消，保留恢复与诊断文件。"
    if meta.get("desired_action") or meta.get("deletion_pending") or meta.get("maintenance_pending"):
        return "任务仍有待处理操作。"
    if live_process_identities(meta, store.job_dir(meta["id"])):
        return "任务仍关联活跃计算进程。"
    return None


def _input_paths(meta: dict) -> set[str]:
    paths = {str(item) for item in meta.get("uploaded_files") or []}

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_file") and isinstance(item, str):
                    paths.add(item)
                elif isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(meta.get("params") or {})
    return paths | {Path(item).name for item in paths}


def _disposable_paths(meta: dict) -> set[str]:
    inputs = _input_paths(meta)
    paths = set()
    for command in meta.get("commands") or []:
        if command.get("kind") == "internal" and command.get("operation") == "remove_files":
            paths.update(str(item) for item in (command.get("data") or {}).get("paths") or [])
    production = str((meta.get("params") or {}).get("production_deffnm") or "md")
    if meta.get("workflow") in {"protein_md", "protein_ligand_md"}:
        for stage in {"em", "nvt", "npt"} - {production}:
            paths.update(stage + extension for extension in _TRAJECTORIES)
    return {path for path in paths if Path(path).suffix.lower() in _TRAJECTORIES and path not in inputs}


def _candidates(meta: dict, scan: dict, request: CleanupRequest) -> dict:
    if request.mode == "delete_jobs":
        return scan["files"]
    inputs = _input_paths(meta)
    disposable = _disposable_paths(meta)
    candidates = {}
    for relative, record in scan["files"].items():
        backup = _BACKUP.fullmatch(Path(relative).name)
        original = str(Path(relative).with_name(backup.group(1))) if backup else relative
        if relative in inputs or original in inputs or Path(original).name in inputs:
            continue
        if original in disposable or (request.include_logs and Path(original).suffix.lower() == ".log"):
            candidates[relative] = record
    return candidates


def _category(relative: str, inputs: set[str]) -> str:
    if relative in inputs or Path(relative).name in inputs:
        return "inputs"
    suffix = Path(relative).suffix.lower()
    if suffix in _TRAJECTORIES:
        return "trajectories"
    if suffix == ".cpt":
        return "checkpoints"
    if suffix in {".top", ".itp", ".tpr", ".mdp", ".gro", ".ndx"}:
        return "topology_and_parameters"
    if suffix == ".log":
        return "logs"
    return "reports_and_other"


def storage_overview(store) -> dict:
    runtime = Path(store.runtime_root)
    if runtime.is_symlink() or (runtime / "jobs").is_symlink():
        raise ValueError("空间管理不支持符号链接运行目录。")
    disk = shutil.disk_usage(runtime)
    jobs, categories = [], {}
    for meta in store.list():
        try:
            scan = _scan_job(store, meta["id"])
            inputs = _input_paths(meta)
            total = sum(record["size"] for record in scan["files"].values())
            for relative, record in scan["files"].items():
                key = _category(relative, inputs)
                categories[key] = categories.get(key, 0) + record["size"]
            reason = _protection_reason(store, meta)
            if scan["unsafe"]:
                reason = "目录包含符号链接或特殊文件，禁止自动清理。"
            request = CleanupRequest(job_ids=[meta["id"]])
            reclaimable = sum(record["size"] for record in _candidates(meta, scan, request).values()) if reason is None else 0
            jobs.append(
                {
                    "id": meta["id"],
                    "name": meta.get("name") or meta["id"],
                    "status": meta["status"],
                    "bytes": total,
                    "reclaimable_bytes": reclaimable,
                    "eligible": reason is None,
                    "protected_reason": reason,
                }
            )
        except OSError:
            jobs.append(
                {
                    "id": meta["id"],
                    "name": meta.get("name") or meta["id"],
                    "status": meta["status"],
                    "bytes": 0,
                    "reclaimable_bytes": 0,
                    "eligible": False,
                    "protected_reason": "任务目录无法安全读取。",
                }
            )
    # Account for the database, exports and unregistered runtime files, without following links.
    root_fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        runtime_scan = _scan_fd(root_fd, tolerate_changes=True)
    finally:
        os.close(root_fd)
    runtime_bytes = sum(record["size"] for record in runtime_scan["files"].values())
    task_bytes = sum(job["bytes"] for job in jobs)
    return {
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
        "runtime": {"bytes": runtime_bytes, "task_bytes": task_bytes, "other_bytes": max(0, runtime_bytes - task_bytes)},
        "categories": categories,
        "jobs": jobs,
        "eligible_jobs": sum(job["eligible"] for job in jobs),
        "reclaimable_bytes": sum(job["reclaimable_bytes"] for job in jobs),
    }


def _selection(request: CleanupRequest) -> tuple:
    return (tuple(sorted(request.job_ids)), request.mode, request.include_logs)


def _meta_binding(meta: dict) -> str:
    return json.dumps(
        {key: meta.get(key) for key in ("status", "desired_action", "storage_revision", "uploaded_files", "params")},
        sort_keys=True,
        ensure_ascii=False,
    )


def preview_cleanup(store, request: CleanupRequest) -> dict:
    snapshots, jobs, files, protected = {}, [], [], []
    entry_count = 0
    for job_id in request.job_ids:
        try:
            meta = store.get(job_id)
            reason = _protection_reason(store, meta)
            if reason:
                protected.append({"id": job_id, "reason": reason})
                continue
            scan = _scan_job(store, job_id)
            if scan["unsafe"]:
                protected.append({"id": job_id, "reason": "目录包含符号链接或特殊文件，禁止自动清理。"})
                continue
            candidates = _candidates(meta, scan, request)
            if request.mode != "delete_jobs" and not candidates:
                continue
            fingerprints = (
                scan["entries"] if request.mode == "delete_jobs" else {path: record["fingerprint"] for path, record in candidates.items()}
            )
            entry_count += len(fingerprints)
            if entry_count > MAX_PREVIEW_ENTRIES:
                raise ValueError("预览文件过多，请减少所选任务数量。")
            snapshots[job_id] = {
                "meta": _meta_binding(meta),
                "files": fingerprints,
                "directory": scan["directory"],
                "bytes": sum(item["size"] for item in candidates.values()),
            }
            jobs.append(
                {"id": job_id, "name": meta.get("name") or job_id, "bytes": snapshots[job_id]["bytes"], "file_count": len(candidates)}
            )
            files.extend({"job_id": job_id, "path": path, "bytes": item["size"]} for path, item in candidates.items())
        except FileNotFoundError:
            protected.append({"id": job_id, "reason": "任务或目录已不存在。"})
        except OSError:
            protected.append({"id": job_id, "reason": "任务目录无法安全读取。"})
    now = time.time()
    token = secrets.token_urlsafe(32)
    with _PREVIEW_LOCK:
        for old in [key for key, value in _PREVIEWS.items() if value["expires"] <= now]:
            _PREVIEWS.pop(old, None)
        while len(_PREVIEWS) >= MAX_PREVIEWS:
            _PREVIEWS.pop(next(iter(_PREVIEWS)))
        _PREVIEWS[token] = {
            "expires": now + PREVIEW_TTL_SECONDS,
            "runtime": str(Path(store.runtime_root).resolve()),
            "selection": _selection(request),
            "jobs": snapshots,
        }
    return {
        "preview_token": token,
        "expires_at": datetime.fromtimestamp(now + PREVIEW_TTL_SECONDS, timezone.utc).isoformat(),
        "total_bytes": sum(job["bytes"] for job in jobs),
        "file_count": len(files),
        "jobs": jobs,
        "files": files[:500],
        "files_truncated": len(files) > 500,
        "protected_jobs": protected,
        "mode": request.mode,
    }


def _validate_snapshot(meta: dict, scan: dict, request: CleanupRequest, snapshot: dict) -> dict:
    candidates = _candidates(meta, scan, request)
    fingerprints = (
        scan["entries"] if request.mode == "delete_jobs" else {path: record["fingerprint"] for path, record in candidates.items()}
    )
    if (
        scan["unsafe"]
        or _meta_binding(meta) != snapshot["meta"]
        or scan["directory"] != snapshot["directory"]
        or fingerprints != snapshot["files"]
    ):
        raise RuntimeError("任务状态或文件已变化，请重新预览清理。")
    return candidates


def _unlink_relative(directory_fd: int, relative: str, fingerprint: tuple) -> None:
    parts = relative.split("/")
    parent_fd = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or _fingerprint(info) != fingerprint:
            raise RuntimeError("文件已变化，请重新预览清理。")
        os.unlink(parts[-1], dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def execute_cleanup(store, request: CleanupRequest) -> dict:
    with _PREVIEW_LOCK:
        saved = _PREVIEWS.get(request.preview_token or "")
        if saved is None or saved["expires"] <= time.time():
            raise ValueError("清理预览已过期或已使用，请重新预览。")
        if saved["selection"] != _selection(request) or saved["runtime"] != str(Path(store.runtime_root).resolve()):
            raise ValueError("清理选项与预览不一致，请重新预览。")
        _PREVIEWS.pop(request.preview_token, None)
    result = {"deleted_bytes": 0, "deleted_files": 0, "deleted_jobs": [], "cleaned_jobs": [], "skipped_jobs": []}
    for job_id, snapshot in saved["jobs"].items():
        try:
            if request.mode == "delete_jobs":

                def validate(meta, workdir, expected=snapshot):
                    _validate_snapshot(meta, _scan_job(store, meta["id"]), request, expected)

                store.delete(job_id, allowed_states=_TERMINAL, validate=validate)
                result["deleted_jobs"].append(job_id)
                result["deleted_files"] += sum(stat.S_ISREG(item[2]) for item in snapshot["files"].values())
                result["deleted_bytes"] += snapshot["bytes"]
            else:

                def clean(meta, workdir, expected=snapshot):
                    directory_fd = _job_fd(store, meta["id"])
                    try:
                        candidates = _validate_snapshot(meta, _scan_fd(directory_fd), request, expected)
                        deleted_bytes = 0
                        for relative, record in candidates.items():
                            _unlink_relative(directory_fd, relative, record["fingerprint"])
                            deleted_bytes += record["size"]
                        return {
                            "deleted_bytes": deleted_bytes,
                            "deleted_files": len(candidates),
                            "mode": request.mode,
                            "include_logs": request.include_logs,
                        }
                    finally:
                        os.close(directory_fd)

                cleaned = store.maintain_job(job_id, clean)
                result["cleaned_jobs"].append(job_id)
                result["deleted_bytes"] += cleaned["deleted_bytes"]
                result["deleted_files"] += cleaned["deleted_files"]
        except (OSError, RuntimeError, ValueError) as error:
            result["skipped_jobs"].append({"id": job_id, "reason": str(error)})
    return result
