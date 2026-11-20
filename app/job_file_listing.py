"""Cached task file listings and bounded XVG archives for the job file routes."""

from __future__ import annotations

import io
import json
import os
import stat
import threading
import time
import zipfile
from pathlib import Path

from .files import relative_file_page, relative_files

FILE_LIST_CACHE: dict[tuple[int, str], tuple[str, list[dict], bool, float]] = {}
FILE_LIST_CACHE_LOCK = threading.Lock()
FILE_PAGE_CACHE: dict[tuple, tuple[str, dict, float]] = {}
ACTIVE_FILE_CACHE_SECONDS = 5.0
MAX_FILE_CACHE_ENTRIES = 256
MAX_FILE_LIST_ITEMS = 1000
MAX_XVG_ARCHIVE_FILES = 100
MAX_XVG_ARCHIVE_BYTES = 100 * 1024 * 1024
ACTIVE_STATUSES = frozenset({"preparing", "queued", "running", "interrupted"})


def forget_file_lists(store, job_ids) -> None:
    """Drop cached full listings after tasks are deleted or cleaned."""
    with FILE_LIST_CACHE_LOCK:
        entries = {(id(store), job_id) for job_id in job_ids}
        for key in entries:
            FILE_LIST_CACHE.pop(key, None)
        for page_key in list(FILE_PAGE_CACHE):
            if page_key[:2] in entries:
                del FILE_PAGE_CACHE[page_key]


def _remember(cache: dict, key, value) -> None:
    if len(cache) >= MAX_FILE_CACHE_ENTRIES:
        cache.pop(next(iter(cache)))
    cache[key] = value


def list_job_files(store, job_id: str, meta: dict, *, limit: int | None, offset: int, search: str, structures_only: bool = False) -> dict:
    """Return a full (truncated) listing, or one page when ``limit`` is given.

    Results are reused until the task's status, step or storage revision
    changes; active tasks are rescanned at most every few seconds.
    """
    cache_key = f"{meta['workdir']}:{meta.get('status')}:{meta.get('step_index')}:{meta.get('storage_revision', 0)}"
    active = meta.get("status") in ACTIVE_STATUSES
    now = time.monotonic()

    def fresh(stored_key: str, stored_at: float) -> bool:
        return stored_key == cache_key and (not active or now - stored_at < ACTIVE_FILE_CACHE_SECONDS)

    workdir = Path(meta["workdir"])
    if structures_only and limit is None:
        limit = 200
    if limit is not None:
        page_key = (id(store), job_id, search, offset, limit, structures_only)
        with FILE_LIST_CACHE_LOCK:
            cached_page = FILE_PAGE_CACHE.get(page_key)
            if cached_page and fresh(cached_page[0], cached_page[2]):
                return cached_page[1]
        page = relative_file_page(workdir, search=search, offset=offset, limit=limit, structures_only=structures_only)
        with FILE_LIST_CACHE_LOCK:
            _remember(FILE_PAGE_CACHE, page_key, (cache_key, page, now))
        return page

    entry_key = (id(store), job_id)
    with FILE_LIST_CACHE_LOCK:
        cached = FILE_LIST_CACHE.get(entry_key)
        if cached and fresh(cached[0], cached[3]):
            return {"files": cached[1], "truncated": cached[2]}
    scanned = relative_files(workdir, max_files=MAX_FILE_LIST_ITEMS + 1)
    truncated = len(scanned) > MAX_FILE_LIST_ITEMS
    files = scanned[:MAX_FILE_LIST_ITEMS]
    with FILE_LIST_CACHE_LOCK:
        _remember(FILE_LIST_CACHE, entry_key, (cache_key, files, truncated, now))
    return {"files": files, "truncated": truncated}


class XvgArchiveLimitError(ValueError):
    """XVG data exists, but no individual file fits the archive."""


def xvg_archive(workdir: Path) -> bytes | None:
    """Archive bounded snapshots; skip oversized files without losing later small ones."""
    if not workdir.is_dir():
        return None
    archive = io.BytesIO()
    count = total_bytes = skipped = 0
    examples = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for directory, folders, names, dir_fd in os.fwalk(workdir, follow_symlinks=False):
            folders.sort()
            for name in sorted(names):
                if not name.lower().endswith(".xvg"):
                    continue
                rel = (Path(directory) / name).relative_to(workdir).as_posix()
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
                except OSError:
                    continue
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if count >= MAX_XVG_ARCHIVE_FILES or total_bytes + info.st_size > MAX_XVG_ARCHIVE_BYTES:
                        skipped += 1
                        if len(examples) < 100:
                            examples.append({"path": rel, "reason": "file_count_limit" if count >= MAX_XVG_ARCHIVE_FILES else "byte_limit"})
                        continue
                    with bundle.open(rel, "w") as dest:
                        remaining = info.st_size
                        while remaining and (chunk := source.read(min(1024 * 1024, remaining))):
                            dest.write(chunk)
                            remaining -= len(chunk)
                    total_bytes += info.st_size - remaining
                    count += 1
        if not count:
            if skipped:
                raise XvgArchiveLimitError()
            return None
        if skipped:
            bundle.writestr(
                "manifest.json", json.dumps({"included": count, "skipped": skipped, "skipped_examples": examples}, ensure_ascii=False)
            )
    return archive.getvalue()
