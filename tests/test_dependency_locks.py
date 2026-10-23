from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.lock_dependencies import PROFILES, check_lock, source_inputs

ROOT = Path(__file__).resolve().parents[1]


def locked_requirements(profile: str, python: str) -> dict[str, Requirement]:
    environment = {**default_environment(), "python_version": python, "python_full_version": python + ".1", "sys_platform": "linux"}
    content = (ROOT / "requirements" / f"{profile}.lock").read_text().replace("\\\n", " ")
    result = {}
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(re.sub(r"\s+--hash=sha256:[0-9a-f]{64}", "", line))
        if requirement.marker is None or requirement.marker.evaluate(environment):
            name = canonicalize_name(requirement.name)
            assert name not in result, f"Overlapping markers for {name} on Python {python}"
            result[name] = requirement
    return result


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("python", ["3.11", "3.12"])
def test_locks_match_sources_and_satisfy_direct_requirements(profile, python):
    check_lock(ROOT, profile)
    locked = locked_requirements(profile, python)
    sources, _ = source_inputs(ROOT, profile)
    for line in sources.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        requirement = Requirement(line)
        actual = locked[canonicalize_name(requirement.name)]
        version = next(iter(actual.specifier)).version
        assert version in requirement.specifier
        assert requirement.extras <= actual.extras


@pytest.mark.parametrize("python", ["3.11", "3.12"])
def test_dev_lock_preserves_runtime_versions_and_runtime_excludes_dev_tools(python):
    runtime = locked_requirements("runtime", python)
    dev = locked_requirements("dev", python)
    assert all(runtime[name].specifier == dev[name].specifier for name in runtime)
    assert not {"pytest", "pytest-cov", "ruff", "playwright"}.intersection(runtime)
    assert {"pytest", "pytest-cov", "ruff", "playwright"} <= dev.keys()


@pytest.fixture
def lock_project(tmp_path):
    for name in ("requirements.txt", "pyproject.toml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "requirements", tmp_path / "requirements")
    return tmp_path


def test_changed_direct_requirement_rejects_stale_locks(lock_project):
    with (lock_project / "requirements.txt").open("a") as output:
        output.write("httpx==0.28.1\n")
    for profile in PROFILES:
        with pytest.raises(ValueError, match="stale"):
            check_lock(lock_project, profile)


def test_hashless_or_unpinned_dependency_is_rejected(lock_project):
    path = lock_project / "requirements/runtime.lock"
    with path.open("a") as output:
        output.write("unexpected-package>=1\n")
    with pytest.raises(ValueError, match="exact versions and SHA-256 hashes"):
        check_lock(lock_project, "runtime")


def test_changed_test_extra_rejects_stale_dev_lock_only(lock_project):
    path = lock_project / "pyproject.toml"
    path.write_text(path.read_text().replace("test = [", 'test = ["hypothesis>=6",'))
    check_lock(lock_project, "runtime")
    with pytest.raises(ValueError, match="stale"):
        check_lock(lock_project, "dev")


def test_package_metadata_has_one_application_dependency_source():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "dependencies" not in project["project"]
    assert "dependencies" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["dependencies"] == {"file": ["requirements.txt"]}
