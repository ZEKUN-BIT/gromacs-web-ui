from __future__ import annotations

import copy
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "packaging/windows"
spec = importlib.util.spec_from_file_location("windows_dependency_lock", ASSETS / "lock-python-dependencies.py")
lock_generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lock_generator)


@pytest.fixture
def lock_inputs(tmp_path):
    (tmp_path / "science-bootstrap-requirements.txt").write_text("pip==26.2.1\n")
    (tmp_path / "science-requirements.txt").write_text("acpype==2026.9.4 --hash=sha256:" + "a" * 64 + "\n")

    def package(name, version, requirements=(), wheel=None):
        return {
            "metadata": {"name": name, "version": version, "requires_dist": list(requirements)},
            "download_info": {
                "url": "https://example.invalid/" + (wheel or f"{name}-{version}-py3-none-any.whl"),
                "archive_info": {"hashes": {"sha256": "a" * 64}},
            },
        }

    report = {
        "environment": {"python_version": "3.12", "sys_platform": "linux", "platform_machine": "x86_64"},
        "install": [
            package("uvicorn", "0.30.3", ("click>=8; extra == 'standard'", "colorama; sys_platform == 'win32'")),
            package("click", "8.5.0"),
            package("pip", "26.2.1"),
            package("acpype", "2026.9.4"),
        ],
    }
    return report, b"uvicorn[standard]==0.30.3\n", tmp_path


def test_generator_preserves_requested_extras_and_locks_all_transitives(lock_inputs):
    report, source, assets = lock_inputs
    rendered = lock_generator.render_lock(report, source, assets)
    assert "uvicorn[standard]==0.30.3" in rendered
    assert "click==8.5.0" in rendered
    assert "colorama" not in rendered
    assert rendered.count("--hash=sha256:") == 4
    assert f"# source-input-sha256: {lock_generator.source_digest(source, assets)}" in rendered
    assert rendered == lock_generator.render_lock(copy.deepcopy(report), source, assets)


def test_report_with_installed_packages_omitted_cannot_generate_incomplete_lock(lock_inputs):
    report, source, assets = lock_inputs
    report["install"] = [item for item in report["install"] if item["metadata"]["name"] != "click"]
    with pytest.raises(ValueError, match="missing a compatible dependency: click"):
        lock_generator.render_lock(report, source, assets)


def test_generator_rejects_hash_different_from_science_manifest(lock_inputs):
    report, source, assets = lock_inputs
    report["install"][-1]["download_info"]["archive_info"]["hashes"]["sha256"] = "b" * 64
    with pytest.raises(ValueError, match="fixed source manifest"):
        lock_generator.render_lock(report, source, assets)


@pytest.mark.parametrize(
    "wheel,diagnostic",
    [
        ("uvicorn-0.30.3.tar.gz", "HTTPS wheel"),
        ("uvicorn-0.30.3-cp312-cp312-manylinux_2_40_x86_64.whl", "newer glibc"),
        ("uvicorn-0.30.3-cp312-cp312-linux_x86_64.whl", "manylinux compatibility"),
        ("uvicorn-0.30.3-cp312-cp312-win_amd64.whl", "incompatible"),
        ("uvicorn-0.30.3-cp313-cp313-manylinux_2_28_x86_64.whl", "incompatible"),
        ("uvicorn-0.30.3-cp312-cp312-manylinux_2_28_aarch64.whl", "incompatible"),
    ],
)
def test_generator_refuses_incompatible_wheels(lock_inputs, wheel, diagnostic):
    report, source, assets = lock_inputs
    report["install"][0]["download_info"]["url"] = "https://example.invalid/" + wheel
    with pytest.raises(ValueError, match=diagnostic):
        lock_generator.render_lock(report, source, assets)


def test_generator_accepts_older_cpython_abi3_wheel(lock_inputs):
    report, source, assets = lock_inputs
    report["install"][0]["download_info"]["url"] = "https://example.invalid/uvicorn-0.30.3-cp310-abi3-manylinux_2_17_x86_64.whl"
    assert "uvicorn[standard]" in lock_generator.render_lock(report, source, assets)


@pytest.mark.parametrize("key,value", [("python_version", "3.13"), ("sys_platform", "win32"), ("platform_machine", "aarch64")])
def test_generator_requires_target_environment(lock_inputs, key, value):
    report, source, assets = lock_inputs
    report["environment"][key] = value
    with pytest.raises(ValueError, match="Linux x64 / Python 3.12"):
        lock_generator.render_lock(report, source, assets)


def test_committed_lock_is_complete_pinned_and_matches_current_sources():
    lock = (ASSETS / lock_generator.LOCK_NAME).read_text()
    expected_digest = lock_generator.source_digest((ROOT / "requirements.txt").read_bytes(), ASSETS)
    assert f"# source-input-sha256: {expected_digest}" in lock
    entries = [line for line in lock.replace("\\\n", " ").splitlines() if line and not line.startswith("#")]
    assert len(entries) == 53
    assert all(re.fullmatch(r"[a-z0-9-]+(?:\[standard\])?==[a-z0-9.]+\s+--hash=sha256:[0-9a-f]{64}", line) for line in entries)
    for name in ("fastapi", "numpy", "acpype", "openbabel-wheel", "pip", "pydantic-core", "scipy"):
        assert any(line.startswith(name + "==") for line in entries)
