"""Fault-inject real promotions and interrupted updates in disposable directories."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "packaging/windows/application_update.py"
spec = importlib.util.spec_from_file_location("application_updater", SOURCE)
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def populate(path: Path, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name in updater.ITEMS:
        item = path / name
        if name in {"app", "mdp", "scripts"}:
            item.mkdir()
            (item / "sample").write_text(version)
        else:
            item.write_text(version)


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "application"
    populate(root, "old")
    for name in ("runtime", ".venv", "gromacs"):
        (root / name).mkdir()
        (root / name / "keep").write_text("retain")
    (root / "settings.json").write_text("retain")
    stage = root / "build.test"
    populate(stage, "new")
    return root, stage


def assert_version(root: Path, version: str) -> None:
    for name in updater.ITEMS:
        path = root / name
        assert (path / "sample" if path.is_dir() else path).read_text() == version
    for name in ("runtime", ".venv", "gromacs"):
        assert (root / name / "keep").read_text() == "retain"
    assert (root / "settings.json").read_text() == "retain"


@pytest.mark.parametrize("failed_item", updater.ITEMS)
def test_each_promotion_failure_restores_every_old_item(installation, monkeypatch, failed_item):
    root, stage = installation
    original = Path.replace
    failed = False

    def inject(path, target):
        nonlocal failed
        if path == stage / failed_item and not failed:
            failed = True
            raise OSError("injected promotion failure")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", inject)
    with pytest.raises(OSError, match="injected"):
        updater.promote(root, stage)
    assert_version(root, "old")
    assert not (root / updater.JOURNAL).exists()
    populate(stage, "new")
    updater.promote(root, stage)
    assert_version(root, "new")
    assert not stage.exists()


@pytest.mark.parametrize("kill_after", ("backup", "promotion"))
def test_abrupt_process_exit_is_recovered_on_next_run(installation, kill_after):
    root, stage = installation
    script = r"""
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('updater', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
root, stage = Path(sys.argv[2]), Path(sys.argv[3])
replace = Path.replace
def interrupt(path, target):
    result = replace(path, target)
    if (sys.argv[4] == 'backup' and path == root / 'mdp') or (sys.argv[4] == 'promotion' and path == stage / 'mdp'):
        os._exit(97)
    return result
Path.replace = interrupt
module.promote(root, stage)
"""
    process = subprocess.run([sys.executable, "-c", script, str(SOURCE), str(root), str(stage), kill_after], timeout=10)
    assert process.returncode == 97
    assert (root / updater.JOURNAL).exists()
    updater.recover(root)
    assert_version(root, "old")
    assert not stage.exists()


def test_failed_rollback_keeps_backup_and_journal_for_retry(installation, monkeypatch):
    root, stage = installation
    original = Path.replace

    def inject(path, target):
        if path == stage / "mdp" or path == stage / "previous/mdp":
            raise OSError("injected filesystem failure")
        return original(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", inject)
        with pytest.raises(OSError):
            updater.promote(root, stage)
    assert (root / updater.JOURNAL).exists()
    assert (stage / "previous/mdp/sample").read_text() == "old"
    updater.recover(root)
    assert_version(root, "old")


def test_fresh_install_failure_removes_only_new_application_files(installation, monkeypatch):
    root, stage = installation
    for name in updater.ITEMS:
        updater.remove(root / name)
    original = Path.replace

    def inject(path, target):
        if path == stage / "mdp":
            raise OSError("fresh install failed")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", inject)
    with pytest.raises(OSError):
        updater.promote(root, stage)
    assert not any((root / name).exists() for name in updater.ITEMS)
    assert (root / "runtime/keep").read_text() == "retain"


def test_recovery_rejects_an_external_backup_path(installation, tmp_path):
    root, _ = installation
    outside = tmp_path / "build.outside"
    outside.mkdir()
    (outside / "keep").write_text("private")
    (root / updater.JOURNAL).write_text(json.dumps({"phase": "committed", "stage": str(outside), "started": [], "previous": []}))
    with pytest.raises(ValueError, match="directly inside"):
        updater.recover(root)
    assert (outside / "keep").read_text() == "private"
    assert (root / updater.JOURNAL).exists()


def test_committed_recovery_keeps_new_application(installation):
    root, stage = installation
    (stage / "previous").mkdir()
    (root / updater.JOURNAL).write_text(
        json.dumps({"phase": "committed", "stage": str(stage), "started": list(updater.ITEMS), "previous": list(updater.ITEMS)})
    )
    updater.recover(root)
    assert_version(root, "old")  # Recovery must not replace committed files from staging.
    assert not stage.exists()


def test_disk_preflight_uses_branch_budget_and_reports_linux_location(installation, tmp_path, monkeypatch):
    import tarfile

    root, _ = installation
    payload = tmp_path / "payload"
    payload.mkdir()
    with tarfile.open(payload / "application.tar.gz", "w:gz"):
        pass
    usage = shutil.disk_usage(root)
    monkeypatch.setattr(updater.shutil, "disk_usage", lambda _: usage._replace(free=512 * 1024**2))
    assert updater.check_space(root, payload, app_only=True) == 128 * 1024**2
    with pytest.raises(RuntimeError, match="Insufficient Linux disk space.*application"):
        updater.check_space(root, payload, app_only=False, backend="CPU")
    monkeypatch.setattr(updater.shutil, "disk_usage", lambda _: usage._replace(free=5 * 1024**3))
    assert updater.check_space(root, payload, app_only=False, backend="CPU") < updater.check_space(
        root, payload, app_only=False, backend="CUDA"
    )


def test_application_only_runtime_gate_detects_missing_executable_and_native_libraries(tmp_path):
    root = tmp_path / "application"
    prefix = root / "gromacs"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "share/gromacs/top/amber19sb.ff").mkdir(parents=True)
    (prefix / ".ready").touch()
    with pytest.raises(RuntimeError, match="incomplete"):
        updater.check_runtime(root)
    gmx = prefix / "bin/gmx"
    gmx.write_text('#!/bin/sh\necho "GROMACS version: 2026.3"\necho "Precision: mixed"\n')
    gmx.chmod(0o755)
    updater.check_runtime(root)
    gmx.write_text('#!/bin/sh\necho "libgromacs.so: cannot open shared object file" >&2\nexit 127\n')
    with pytest.raises(RuntimeError, match="libgromacs.so"):
        updater.check_runtime(root)
