from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import windows_acceptance as acceptance


@pytest.fixture
def record(tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"synthetic installer for validation tests only")
    evidence = tmp_path / "manual-run.txt"
    evidence.write_text("Synthetic evidence: this fixture does not represent a real Windows run.")
    data = {
        "schema_version": 1,
        "case": "win10-fresh-cpu",
        "status": "passed",
        "version": "0.2.10",
        "source_commit": "a" * 40,
        "installer_sha256": acceptance.digest(installer),
        "tester": "automated fixture",
        "tested_at": "2026-10-28T00:00:00+00:00",
        "os": {"system": "Windows", "version": "10.0.19045", "machine": "AMD64"},
        "hardware": "synthetic CPU",
        "driver": "n/a: CPU",
        "gromacs_version": "synthetic",
        "steps": {
            step: {"status": "passed", "actual": "synthetic assertion", "evidence": [evidence.name]}
            for step in acceptance.CASES["win10-fresh-cpu"]
        },
        "evidence": [{"path": evidence.name, "sha256": acceptance.digest(evidence)}],
    }
    path = tmp_path / "record.json"
    path.write_text(json.dumps(data))
    return path, data, installer


def validate(record, **updates):
    path, data, installer = record
    data = {**copy.deepcopy(data), **updates}
    path.write_text(json.dumps(data))
    return acceptance.validate_record(
        path, installer_sha256=acceptance.digest(installer), version="0.2.10", source_commit="a" * 40, require_passed=True
    )


def test_complete_record_is_bound_to_installer_and_evidence(record):
    assert validate(record)["case"] == "win10-fresh-cpu"
    (record[0].parent / "manual-run.txt").write_text("changed")
    with pytest.raises(ValueError, match="hash changed"):
        validate(record)


@pytest.mark.parametrize("field,value", [("version", "old"), ("source_commit", "b" * 40), ("installer_sha256", "c" * 64)])
def test_rejects_evidence_from_another_release(record, field, value):
    with pytest.raises(ValueError, match="does not match"):
        validate(record, **{field: value})


def test_passed_label_cannot_hide_unexecuted_steps(record):
    record[1]["steps"]["reboot"]["status"] = "unverified"
    with pytest.raises(ValueError, match="every step"):
        validate(record)
    with pytest.raises(ValueError, match="remains unverified"):
        validate(record, status="unverified")


@pytest.mark.parametrize("actual", [None, 123, [], {}, "", "   "])
def test_passed_step_requires_actual_text(record, actual):
    record[1]["steps"]["reboot"]["actual"] = actual
    with pytest.raises(ValueError, match="every step"):
        validate(record)


@pytest.mark.parametrize(
    "updates", [{"os": []}, {"steps": []}, {"evidence": [None]}, {"steps": dict.fromkeys(acceptance.CASES["win10-fresh-cpu"])}]
)
def test_malformed_record_containers_are_rejected(record, updates):
    with pytest.raises(ValueError):
        validate(record, **updates)


def test_record_root_must_be_object(record):
    path, _, installer = record
    path.write_text("[]")
    with pytest.raises(ValueError, match="schema"):
        acceptance.validate_record(
            path, installer_sha256=acceptance.digest(installer), version="0.2.10", source_commit="a" * 40, require_passed=True
        )


@pytest.mark.parametrize("path", ["../outside.log", "/etc/passwd", "C:\\outside.log"])
def test_evidence_paths_stay_inside_report_directory(record, path):
    with pytest.raises(ValueError, match="inside"):
        validate(record, evidence=[{"path": path, "sha256": "a" * 64}])


def test_rejects_symlink_evidence(record):
    root = record[0].parent
    (root / "linked.txt").symlink_to(root / "manual-run.txt")
    with pytest.raises(ValueError, match="Symlink"):
        validate(record, evidence=[{"path": "linked.txt", "sha256": acceptance.digest(root / "manual-run.txt")}])


def test_rejects_junction_evidence(record, monkeypatch):
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == "manual-run.txt", raising=False)
    with pytest.raises(ValueError, match="junction"):
        validate(record)


def test_complete_matrix_requires_every_case(record):
    path, _, installer = record
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.windows_acceptance",
            "check",
            "--installer",
            str(installer),
            "--version",
            "0.2.10",
            "--source-commit",
            "a" * 40,
            "--require-complete",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "All five" in result.stderr


def test_collection_cannot_pretend_linux_is_windows(record, monkeypatch):
    path, _, installer = record
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Linux")
    with pytest.raises(ValueError, match="real Windows"):
        acceptance.collect(
            installer,
            path.with_name("new.json"),
            case="win10-fresh-cpu",
            tester="tester",
            version="0.2.10",
            source_commit="a" * 40,
            distro="Ubuntu",
            gmx="gmx",
        )


def test_read_only_collection_never_marks_case_passed(record, monkeypatch):
    path, _, installer = record
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Windows")
    calls = []
    monkeypatch.setattr(acceptance, "probe", lambda command: calls.append(command) or {"exit_code": 0, "stdout": "synthetic"})
    output = path.with_name("collected.json")
    data = acceptance.collect(
        installer, output, case="win10-fresh-cpu", tester="tester", version="0.2.10", source_commit="a" * 40, distro="Ubuntu", gmx="gmx"
    )
    assert data["status"] == "unverified"
    assert all(step["status"] == "unverified" for step in data["steps"].values())
    assert calls[0] == ["wsl.exe", "--list", "--verbose"]
    assert calls[1][-2:] == ["gmx", "--version"]
    assert len(data["evidence"]) == 3
    with pytest.raises(ValueError, match="overwrite"):
        acceptance.collect(
            installer, output, case="win10-fresh-cpu", tester="tester", version="0.2.10", source_commit="a" * 40, distro="Ubuntu", gmx="gmx"
        )
