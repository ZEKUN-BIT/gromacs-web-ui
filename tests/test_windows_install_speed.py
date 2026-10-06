from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SYSTEM_SCRIPT = Path(__file__).resolve().parents[1] / "packaging/windows/linux-system.sh"
RUNTIME_PACKAGES = {
    "python3",
    "python3-venv",
    "ca-certificates",
    "curl",
    "libfftw3-single3",
    "libgomp1",
    "dssp",
    "zstd",
    "ocl-icd-libopencl1",
    "liblapack3",
    "libblas3",
    "libgfortran5",
}


def run_system_setup(tmp_path, package_statuses, *, apt_failure=False, app_user_home="/home/gromacs-console"):
    programs = tmp_path / "programs"
    programs.mkdir()
    log = tmp_path / "commands.jsonl"
    statuses = tmp_path / "package-status.json"
    statuses.write_text(json.dumps(package_statuses))
    stub = (
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['SETUP_COMMAND_LOG'], 'a') as stream:
    stream.write(json.dumps([name, *args]) + '\\n')
if name == 'uname':
    print('x86_64')
elif name == 'id':
    print('1001' if '-u' in args else 'uid=1001(gromacs-console)')
elif name == 'getent':
    print('gromacs-console:x:1001:1001::' + os.environ['SETUP_APP_HOME'] + ':/bin/bash')
elif name == 'dpkg-query':
    packages = json.loads(Path(os.environ['SETUP_PACKAGE_STATUS']).read_text())
    if args[-1] not in packages:
        raise SystemExit(1)
    print(packages[args[-1]], end='')
elif name == 'apt-get' and os.environ['SETUP_APT_FAILURE'] == '1':
    raise SystemExit(100)
"""
    )
    for name in ("id", "getent", "uname", "dpkg-query", "apt-get"):
        path = programs / name
        path.write_text(stub)
        path.chmod(0o755)
    # The production script has no environment-controlled OS or command override.
    script = tmp_path / "linux-system.sh"
    script.write_text(SYSTEM_SCRIPT.read_text().replace("source /etc/os-release", "ID=ubuntu\nVERSION_ID=24.04"))
    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{programs}:/usr/bin:/bin",
            "SETUP_COMMAND_LOG": str(log),
            "SETUP_PACKAGE_STATUS": str(statuses),
            "SETUP_APT_FAILURE": str(int(apt_failure)),
            "SETUP_APP_HOME": app_user_home,
        },
    )
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    return result, commands


def installed_packages():
    return dict.fromkeys(RUNTIME_PACKAGES, "install ok installed")


def test_existing_runtime_skips_apt_entirely_even_when_network_is_unavailable(tmp_path):
    result, commands = run_system_setup(tmp_path, installed_packages(), apt_failure=True)
    assert result.returncode == 0, result.stderr
    assert not any(command[0] == "apt-get" for command in commands)
    queried_packages = {command[-1] for command in commands if command[0] == "dpkg-query"}
    assert queried_packages == RUNTIME_PACKAGES


@pytest.mark.parametrize("broken_status", [None, "deinstall ok config-files", "install ok unpacked"])
def test_missing_or_incomplete_dependency_triggers_only_required_packages(tmp_path, broken_status):
    statuses = installed_packages()
    statuses.pop("dssp")
    if broken_status is not None:
        statuses["dssp"] = broken_status
    result, commands = run_system_setup(tmp_path, statuses)
    assert result.returncode == 0, result.stderr
    apt = [command for command in commands if command[0] == "apt-get"]
    assert apt == [["apt-get", "update"], ["apt-get", "install", "-y", "--no-install-recommends", "dssp"]]


def test_dependency_repair_reports_network_failure_instead_of_skipping_missing_tool(tmp_path):
    statuses = installed_packages()
    statuses.pop("dssp")
    result, commands = run_system_setup(tmp_path, statuses, apt_failure=True)
    assert result.returncode == 100
    assert [command for command in commands if command[0] == "apt-get"] == [["apt-get", "update"]]


def test_clean_science_environment_installs_the_missing_native_runtime_without_compilers(tmp_path):
    native_packages = ["liblapack3", "libblas3", "libgfortran5"]
    statuses = installed_packages()
    for name in native_packages:
        statuses.pop(name)
    result, commands = run_system_setup(tmp_path, statuses)
    assert result.returncode == 0, result.stderr
    assert [command for command in commands if command[0] == "apt-get"] == [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "--no-install-recommends", *native_packages],
    ]


def test_existing_application_user_with_wrong_home_is_rejected_before_apt(tmp_path):
    result, commands = run_system_setup(tmp_path, installed_packages(), app_user_home="/root")
    assert result.returncode != 0
    assert not any(command[0] == "apt-get" for command in commands)
