from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.job_store import JobStore


@pytest.fixture
def source(tmp_path):
    store = JobStore(tmp_path / "runtime")
    job = store.create("metadata export", {"workflow": "custom"}, [], [])
    return store, job, store.job_dir(job["id"])


@pytest.mark.parametrize("symlink", [False, True])
def test_metadata_export_does_not_touch_former_fixed_temporary_path(source, tmp_path, symlink):
    store, job, root = source
    old_temporary = root / "metadata.json.tmp"
    original = b"this file belongs to the user"
    if symlink:
        external = tmp_path / "external-data"
        external.write_bytes(original)
        old_temporary.symlink_to(external)
    else:
        old_temporary.write_bytes(original)
    exported = store.export_metadata(job["id"])
    assert json.loads(exported.read_text())["id"] == job["id"]
    assert old_temporary.read_bytes() == original
    assert old_temporary.is_symlink() is symlink
    assert list(root.glob(".metadata-export-*.tmp")) == []


def test_metadata_export_rejects_a_symlinked_target(source, tmp_path):
    store, job, root = source
    external = tmp_path / "external-data"
    external.write_bytes(b"important data")
    target = root / "metadata.json"
    target.symlink_to(external)
    with pytest.raises(RuntimeError, match="symbolic link"):
        store.export_metadata(job["id"])
    assert target.is_symlink()
    assert external.read_bytes() == b"important data"
    assert list(root.glob(".metadata-export-*.tmp")) == []


def test_failed_metadata_export_preserves_previous_export_and_cleans_temporary_file(source):
    store, job, root = source
    target = root / "metadata.json"
    target.write_bytes(b"previous export")
    with patch.object(Path, "replace", side_effect=OSError("injected rename failure")):
        with pytest.raises(OSError, match="injected rename failure"):
            store.export_metadata(job["id"])
    assert target.read_bytes() == b"previous export"
    assert list(root.glob(".metadata-export-*.tmp")) == []
    assert store.get(job["id"])["status"] == "preparing"


def test_metadata_export_respects_the_shared_task_file_lock(source):
    store, job, root = source
    with store.job_files_lock(job["id"]):
        with pytest.raises(RuntimeError, match="busy"):
            store.export_metadata(job["id"])
    assert not (root / "metadata.json").exists()
    assert list(root.glob(".metadata-export-*.tmp")) == []
