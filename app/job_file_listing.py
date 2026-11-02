"""Cached task file listings and bounded XVG archives for the job file routes."""

from __future__ import annotations

import io
import threading
import time
import zipfile
from pathlib import Path

from .files import ensure_under_root, relative_file_page, relative_files

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
        for job_id in job_ids:
            FILE_LIST_CACHE.pop((id(store), job_id), None)


def _remember(cache: dict, key, value) -> None:
    if len(cache) >= MAX_FILE_CACHE_ENTRIES:
        cache.pop(next(iter(cache)))
    cache[key] = value


def list_job_files(store, job_id: str, meta: dict, *, limit: int | None, offset: int, search: str) -> dict:
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
    if limit is not None:
        page_key = (id(store), job_id, search, offset, limit)
        with FILE_LIST_CACHE_LOCK:
            cached_page = FILE_PAGE_CACHE.get(page_key)
            if cached_page and fresh(cached_page[0], cached_page[2]):
                return cached_page[1]
        page = relative_file_page(workdir, search=search, offset=offset, limit=limit)
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


def xvg_archive(workdir: Path) -> bytes | None:
    """Zip regular XVG files under ``workdir`` within count/size limits; None if none."""
    workdir = workdir.resolve()
    files: list[Path] = []
    total_bytes = 0
    for candidate in sorted(workdir.rglob("*.xvg")):
        if len(files) >= MAX_XVG_ARCHIVE_FILES:
            break
        try:
            path = ensure_under_root(candidate, workdir)
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if total_bytes + size > MAX_XVG_ARCHIVE_BYTES:
            break
        files.append(path)
        total_bytes += size
    if not files:
        return None
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(workdir).as_posix())
    return archive.getvalue()
