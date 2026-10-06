from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dependency_setup", ROOT / "packaging/windows/dependency_setup.py")
dependencies = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dependencies)
REAL_RUN = subprocess.run


def payload_at(path, application_requirement=b"numpy>=2,<3\nuvicorn[standard]==0.30.3\n"):
    path.mkdir(exist_ok=True)
    with tarfile.open(path / "application.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("requirements.txt")
        member.size = len(application_requirement)
        archive.addfile(member, io.BytesIO(application_requirement))
    for name in dependencies.SCIENCE_FILES:
        (path / name).write_text("stable content")
    refresh_lock(path, application_requirement)
    return path


def refresh_lock(payload, application_requirement=None):
    if application_requirement is None:
        with tarfile.open(payload / "application.tar.gz") as archive:
            application_requirement = archive.extractfile("requirements.txt").read()
    digest = hashlib.sha256()
    for name, content in [("requirements.txt", application_requirement)] + [
        (name, (payload / name).read_bytes()) for name in dependencies.SCIENCE_FILES[:2]
    ]:
        digest.update(name.encode() + b"\0" + content + b"\0")
    (payload / dependencies.LOCK_NAME).write_text(
        f"# source-input-sha256: {digest.hexdigest()}\nnumpy==2.5.3 --hash=sha256:" + "0" * 64 + "\n"
    )


@pytest.fixture
def fake_setup(tmp_path, monkeypatch):
    app_root = tmp_path / "application"
    app_root.mkdir()
    payload = payload_at(tmp_path / "payload")
    current_environment = ["installed-package-versions"]
    calls = []
    installed = {}

    def run(command, **kwargs):
        calls.append(command)
        if "pip" in command:
            requirement = Path(command[command.index("-r") + 1])
            content = requirement.read_bytes()
            if "--dry-run" in command:
                if installed.get(requirement.name) != content:
                    return subprocess.CompletedProcess(command, 1, "", "No offline candidate")
                Path(command[command.index("--report") + 1]).write_text('{"install": []}')
            else:
                installed[requirement.name] = content
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dependencies, "environment_fingerprint", lambda: current_environment[0])
    monkeypatch.setattr(dependencies, "offline_checks", lambda _: None)
    monkeypatch.setattr(dependencies, "python_checks", lambda _: None)
    monkeypatch.setattr(dependencies, "native_checks", lambda _: None)

    def stage(command, **kwargs):
        result = dependencies.subprocess.run(command, check=True, timeout=kwargs["timeout"])
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command)

    monkeypatch.setattr(dependencies, "run_stage", stage)
    monkeypatch.setattr(dependencies.subprocess, "run", run)
    return app_root, payload, current_environment, calls


def online_installs(calls):
    return [command for command in calls if "pip" in command and "--dry-run" not in command]


def test_verified_environment_is_reused_without_install_or_ligand_smoke(fake_setup):
    app_root, payload, _, calls = fake_setup
    assert dependencies.setup(app_root, payload) is False
    assert len(calls) == 3  # One unified offline plan, installation, then real ligand verification.
    installs = online_installs(calls)
    assert len(installs) == 1
    assert "--require-hashes" in installs[0]
    assert installs[0][-1] == str(payload / dependencies.LOCK_NAME)
    assert all("--only-binary=:all:" in command and "--no-cache-dir" in command for command in installs)
    assert calls[-1][1] == str(payload / "science-tools.py")
    assert not list(app_root.glob("dependency-setup-*"))
    calls.clear()
    assert dependencies.setup(app_root, payload) is True
    assert calls == []


@pytest.mark.parametrize("change", ["requirements", "science-requirements", "science-helper", "package-version", "missing-marker"])
def test_invalid_cache_installs_only_unsatisfied_stages_and_runs_full_verifier(fake_setup, change):
    app_root, payload, current_environment, calls = fake_setup
    dependencies.setup(app_root, payload)
    if change == "requirements":
        payload_at(payload, b"numpy>=2.5,<3\n")
    elif change == "science-requirements":
        (payload / "science-requirements.txt").write_text("updated hashed requirements")
        refresh_lock(payload)
    elif change == "science-helper":
        (payload / "science-tools.py").write_text("new verification code")
    elif change == "package-version":
        current_environment[0] = "an-unrelated-package-changed"
    else:
        (app_root / dependencies.MARKER).unlink()
    calls.clear()
    assert dependencies.setup(app_root, payload) is False
    installs = online_installs(calls)
    assert len(installs) == (1 if change in {"requirements", "science-requirements"} else 0)
    assert len([command for command in calls if "--dry-run" in command]) == 1
    assert calls[-1][1] == str(payload / "science-tools.py")
    if change == "science-requirements":
        assert "--require-hashes" in installs[0]


@pytest.mark.parametrize(
    "report", [None, "not json", "{}", '{"install": null}', '{"install": {}}', '{"install": [{"metadata": {"name": "numpy"}}]}']
)
def test_offline_plan_must_be_a_valid_empty_install_list(tmp_path, monkeypatch, report):
    requirement = tmp_path / "requirements.txt"
    output = tmp_path / "report.json"

    def run(command, **kwargs):
        assert "--no-index" in command and "--dry-run" in command
        if report is not None:
            output.write_text(report)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    assert dependencies.stage_is_satisfied(["python", "-m", "pip", "install"], requirement, output) is False


def test_failed_offline_resolver_does_not_skip_install_even_with_empty_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text('{"install": []}')
    monkeypatch.setattr(dependencies.subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 1, "", ""))
    assert dependencies.stage_is_satisfied(["python", "-m", "pip", "install"], tmp_path / "requirements.txt", output) is False


def test_offline_probe_removes_remote_pip_overrides_without_changing_parent_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PIP_FIND_LINKS", "https://example.invalid/wheels")
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PIP_REQUIRE_VIRTUALENV", "1")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(tmp_path / "user-pip.conf"))
    monkeypatch.setenv("GROMACS_OFFLINE_TEST", "preserved")
    original_environment = dict(os.environ)
    output = tmp_path / "report.json"

    def run(command, **kwargs):
        environment = kwargs["env"]
        assert {name: value for name, value in environment.items() if name.startswith("PIP_")} == {"PIP_CONFIG_FILE": os.devnull}
        assert environment["GROMACS_OFFLINE_TEST"] == "preserved"
        assert "--no-index" in command
        output.write_text('{"install": []}')
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    assert dependencies.stage_is_satisfied(["python", "-m", "pip", "install"], tmp_path / "requirements.txt", output) is True
    assert dict(os.environ) == original_environment


def test_real_pip_configuration_is_disabled_for_offline_probe(tmp_path, monkeypatch):
    config = tmp_path / "pip.conf"
    config.write_text("[global]\nfind-links = https://example.invalid/config-wheels\nindex-url = https://example.invalid/config-index\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(config))
    monkeypatch.setenv("PIP_FIND_LINKS", "https://example.invalid/environment-wheels")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://example.invalid/environment-index")
    output = tmp_path / "report.json"
    original_run = dependencies.subprocess.run

    def run(command, **kwargs):
        # The real pip configuration reader does no downloading and sees the same isolated child environment.
        configuration = original_run(
            [sys.executable, "-m", "pip", "config", "list"],
            env=kwargs["env"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert "example.invalid" not in configuration.stdout
        assert all(line.startswith(":env:.config-file=") for line in configuration.stdout.splitlines())
        output.write_text('{"install": []}')
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    assert dependencies.stage_is_satisfied([sys.executable, "-m", "pip", "install"], tmp_path / "requirements.txt", output) is True
    assert os.environ["PIP_FIND_LINKS"] == "https://example.invalid/environment-wheels"


def test_online_installs_continue_to_use_parent_pip_configuration(fake_setup, monkeypatch):
    app_root, payload, _, _ = fake_setup
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/user-mirror")
    original_run = dependencies.subprocess.run
    online_stages = []

    def run(command, **kwargs):
        if "pip" in command and "--dry-run" not in command:
            assert "env" not in kwargs
            assert os.environ["PIP_INDEX_URL"] == "https://example.invalid/user-mirror"
            online_stages.append(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    dependencies.setup(app_root, payload)
    assert len(online_stages) == 1


def test_forced_verification_skips_package_install_but_rechecks_ligand(fake_setup):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    calls.clear()
    assert dependencies.setup(app_root, payload, force_verify=True) is False
    assert len(calls) == 1
    assert calls[0][1] == str(payload / "science-tools.py")


def test_broken_python_cache_is_repaired_not_reused(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    checks = iter([False, True])

    def offline(_):
        if not next(checks):
            raise dependencies.PythonDependencyError("A package import is broken")

    monkeypatch.setattr(dependencies, "offline_checks", offline)
    calls.clear()
    assert dependencies.setup(app_root, payload) is False
    assert len(calls) == 3  # Offline satisfaction plus one controlled repair and smoke.
    assert all("--force-reinstall" in command for command in online_installs(calls))


def test_cached_native_failure_never_reinstalls_python_and_removes_marker(fake_setup, monkeypatch, capsys):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    monkeypatch.setattr(
        dependencies, "offline_checks", lambda _: (_ for _ in ()).throw(dependencies.NativeToolError("sqm: liblapack.so.3 missing"))
    )
    monkeypatch.setattr(
        dependencies, "native_checks", lambda _: (_ for _ in ()).throw(dependencies.NativeToolError("sqm: liblapack.so.3 missing"))
    )
    calls.clear()
    with pytest.raises(dependencies.NativeToolError, match="liblapack"):
        dependencies.setup(app_root, payload)
    assert len(calls) == 1  # Full scientific verifier; no pip repair for a native runtime error.
    assert online_installs(calls) == []
    assert "liblapack.so.3" in capsys.readouterr().out
    assert not (app_root / dependencies.MARKER).exists()
    calls.clear()
    with pytest.raises(dependencies.NativeToolError, match="liblapack"):
        dependencies.setup(app_root, payload)
    assert online_installs(calls) == []  # A missing marker also reuses satisfied packages offline.
    assert not (app_root / dependencies.MARKER).exists()


def test_missing_native_link_can_be_restored_by_full_verifier_without_pip(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    checks = iter([False, True])

    def offline(_):
        if not next(checks):
            raise dependencies.NativeToolError("Bundled AmberTools link is missing")

    monkeypatch.setattr(dependencies, "offline_checks", offline)
    calls.clear()
    assert dependencies.setup(app_root, payload) is False
    assert len(calls) == 1
    assert (app_root / dependencies.MARKER).exists()


@pytest.mark.parametrize("failure_stage", ["install", "smoke", "offline"])
def test_failure_never_leaves_success_marker(fake_setup, monkeypatch, failure_stage):
    app_root, payload, _, _ = fake_setup
    (app_root / dependencies.MARKER).write_text('{"obsolete":"environment"}')

    def fail_run(command, **kwargs):
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 1, "", "")
        is_install = "pip" in command
        if failure_stage == "install" and is_install or failure_stage == "smoke" and not is_install:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(dependencies.subprocess, "run", fail_run)
    if failure_stage == "offline":
        monkeypatch.setattr(
            dependencies, "python_checks", lambda _: (_ for _ in ()).throw(dependencies.PythonDependencyError("Missing extra dependency"))
        )
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        dependencies.setup(app_root, payload)
    assert not (app_root / dependencies.MARKER).exists()
    assert not list(app_root.glob("dependency-setup-*"))


def test_marker_records_environment_after_package_install(fake_setup, monkeypatch):
    app_root, payload, current_environment, _ = fake_setup

    def install(command, **kwargs):
        current_environment[0] = "the-newly-installed-environment"
        return subprocess.CompletedProcess(command, 1 if "--dry-run" in command else 0, "", "")

    monkeypatch.setattr(dependencies.subprocess, "run", install)
    dependencies.setup(app_root, payload)
    saved = json.loads((app_root / dependencies.MARKER).read_text())
    assert saved["environment"] == "the-newly-installed-environment"


@pytest.fixture
def native_checks(tmp_path, monkeypatch):
    (tmp_path / ".venv/amber/bin").mkdir(parents=True)
    (tmp_path / "tools").symlink_to(tmp_path / ".venv/amber", target_is_directory=True)
    for name in ("antechamber", "parmchk2", "tleap", "sqm"):
        binary = tmp_path / "tools/bin" / name
        binary.write_text("executable")
        binary.chmod(0o755)
    calls = []
    replies = {
        "obabel": (0, "Open Babel 3.1.0"),
        "antechamber": (0, "Usage: antechamber -i INPUT"),
        "parmchk2": (1, "Usage: parmchk2 -i INPUT"),
        "tleap": (0, "Usage: teLeap [options]"),
        "sqm": (0, "sqm [-O] -i <input> -o <output>"),
        "mkdssp": (0, "mkdssp version 4.4.0"),
    }

    def run(command, **kwargs):
        calls.append(command)
        if "-c" in command and "from openbabel" in command[2]:
            code, output = 0, "Open Babel 3.1.0"
        else:
            code, output = replies.get(Path(command[0]).name, (0, ""))
        return subprocess.CompletedProcess(command, code, output, "")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    monkeypatch.setattr(
        dependencies.importlib.metadata,
        "distribution",
        lambda _: SimpleNamespace(locate_file=lambda _: tmp_path / ".venv/amber"),
    )
    return tmp_path, calls, replies


def test_offline_checks_validate_uvicorn_extras_and_all_scientific_binaries(native_checks):
    tmp_path, calls, _ = native_checks
    dependencies.offline_checks(tmp_path)
    assert calls[0][1:] == ["-m", "pip", "check"]
    assert "httptools,uvloop,yaml,watchfiles,websockets" in calls[1][2]
    assert calls[-1] == ["/usr/bin/mkdssp", "--version"]
    (tmp_path / "tools/bin/sqm").unlink()
    with pytest.raises(RuntimeError, match="sqm"):
        dependencies.offline_checks(tmp_path)


@pytest.mark.parametrize("binary", ["parmchk2", "tleap", "sqm"])
def test_offline_checks_detect_missing_wrapped_native_executable(native_checks, binary):
    tmp_path, _, replies = native_checks
    replies[binary] = (127, "No such file or directory")
    with pytest.raises(RuntimeError, match=binary):
        dependencies.offline_checks(tmp_path)


def test_offline_checks_reject_unrelated_error_with_normal_parmchk_exit_code(native_checks):
    tmp_path, _, replies = native_checks
    replies["parmchk2"] = (1, "Could not load a required library")
    with pytest.raises(RuntimeError, match="parmchk2"):
        dependencies.offline_checks(tmp_path)


def test_offline_checks_reject_wrong_bundle_inside_application_venv(native_checks):
    tmp_path, _, _ = native_checks
    (tmp_path / ".venv/unrelated").mkdir()
    (tmp_path / "tools").unlink()
    (tmp_path / "tools").symlink_to(tmp_path / ".venv/unrelated", target_is_directory=True)
    with pytest.raises(RuntimeError, match="ACPYPE's bundled"):
        dependencies.offline_checks(tmp_path)


@pytest.mark.parametrize(
    "message,expected_error",
    [
        ("ModuleNotFoundError: No module named 'numpy'", dependencies.PythonDependencyError),
        ("ImportError: liblapack.so.3: cannot open shared object file", dependencies.NativeToolError),
        ("ImportError: numpy_extension.so: undefined symbol: runtime_symbol", dependencies.NativeToolError),
    ],
)
def test_python_import_failure_distinguishes_package_corruption_from_native_runtime(native_checks, monkeypatch, message, expected_error):
    tmp_path, _, _ = native_checks
    original_run = dependencies.subprocess.run

    def run(command, **kwargs):
        if "-c" in command and "import fastapi" in command[2]:
            return subprocess.CompletedProcess(command, 1, "", message)
        return original_run(command, **kwargs)

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    with pytest.raises(expected_error, match=message.split(":")[0]):
        dependencies.offline_checks(tmp_path)


def test_missing_dssp_is_a_native_failure_with_original_diagnostic(native_checks, monkeypatch):
    tmp_path, _, _ = native_checks
    original_run = dependencies.subprocess.run

    def run(command, **kwargs):
        if command[0] == "/usr/bin/mkdssp":
            raise FileNotFoundError("/usr/bin/mkdssp does not exist")
        return original_run(command, **kwargs)

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    with pytest.raises(dependencies.NativeToolError, match="mkdssp does not exist"):
        dependencies.offline_checks(tmp_path)


def test_missing_marker_with_broken_import_repairs_satisfied_package(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    (app_root / dependencies.MARKER).unlink()
    source = app_root / "auditbroken.py"
    source.write_text("this is invalid python !!!\n")

    def python_health(_):
        result = REAL_RUN(
            [sys.executable, "-c", "import auditbroken"],
            env={**os.environ, "PYTHONPATH": str(app_root)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise dependencies.PythonDependencyError(result.stderr)

    original_run = dependencies.subprocess.run

    def repairing_run(command, **kwargs):
        if "--force-reinstall" in command:
            source.write_text("healthy = True\n")
        return original_run(command, **kwargs)

    monkeypatch.setattr(dependencies, "offline_checks", python_health)
    monkeypatch.setattr(dependencies, "python_checks", python_health)
    monkeypatch.setattr(dependencies.subprocess, "run", repairing_run)
    calls.clear()
    assert dependencies.setup(app_root, payload) is False
    assert len(online_installs(calls)) == 1
    assert "--force-reinstall" in online_installs(calls)[0]
    assert (app_root / dependencies.MARKER).exists()
    python_health(app_root)


def test_verification_timeout_is_not_package_corruption_and_keeps_tail(native_checks, monkeypatch):
    app_root, _, _ = native_checks

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 60, output=b"x" * 6000 + b"\nlast diagnostic", stderr=b"stderr diagnostic")

    monkeypatch.setattr(dependencies.subprocess, "run", timeout)
    with pytest.raises(dependencies.VerificationIncompleteError) as caught:
        dependencies.python_checks(app_root)
    assert "last diagnostic" in str(caught.value)
    assert "stderr diagnostic" in str(caught.value)
    assert len(str(caught.value)) < 3200
    assert not isinstance(caught.value, dependencies.PythonDependencyError)


def test_incomplete_verification_never_reinstalls_packages(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    monkeypatch.setattr(
        dependencies, "offline_checks", lambda _: (_ for _ in ()).throw(dependencies.VerificationIncompleteError("slow import"))
    )
    calls.clear()
    with pytest.raises(dependencies.VerificationIncompleteError, match="slow import"):
        dependencies.setup(app_root, payload)
    assert calls == []
    assert not (app_root / dependencies.MARKER).exists()


def test_offline_plan_timeout_preserves_diagnostic_and_prevents_network_install(tmp_path, monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 60, output=b"pip resolver still waiting")

    monkeypatch.setattr(dependencies.subprocess, "run", timeout)
    with pytest.raises(dependencies.VerificationIncompleteError, match="pip resolver still waiting"):
        dependencies.stage_is_satisfied(["python", "-m", "pip", "install"], tmp_path / "lock", tmp_path / "report")


def test_healthy_force_verification_runs_offline_checks_once(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    probes = []
    monkeypatch.setattr(dependencies, "offline_checks", lambda _: probes.append("offline"))
    monkeypatch.setattr(dependencies, "python_checks", lambda _: probes.append("python"))
    monkeypatch.setattr(dependencies, "native_checks", lambda _: probes.append("native"))
    calls.clear()
    dependencies.setup(app_root, payload, force_verify=True)
    assert probes == ["offline"]
    assert len(calls) == 1


def test_readiness_check_is_read_only_and_requires_real_health(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    marker = app_root / dependencies.MARKER
    content = marker.read_bytes()
    calls.clear()
    assert dependencies.check_ready(app_root, payload) is True
    monkeypatch.setattr(dependencies, "offline_checks", lambda _: (_ for _ in ()).throw(dependencies.NativeToolError("liblapack missing")))
    assert dependencies.check_ready(app_root, payload) is False
    assert marker.read_bytes() == content
    assert calls == []


def test_native_python_import_failure_cannot_create_success_marker(fake_setup, monkeypatch):
    app_root, payload, _, calls = fake_setup
    dependencies.setup(app_root, payload)
    (app_root / dependencies.MARKER).unlink()

    def failed_native_import(_):
        raise dependencies.NativeToolError("Python extension: liblapack.so.3 missing")

    monkeypatch.setattr(dependencies, "offline_checks", failed_native_import)
    monkeypatch.setattr(dependencies, "python_checks", failed_native_import)
    calls.clear()
    with pytest.raises(dependencies.NativeToolError, match="liblapack"):
        dependencies.setup(app_root, payload)
    assert online_installs(calls) == []
    assert not (app_root / dependencies.MARKER).exists()


def test_stale_lock_is_rejected_before_package_operations(fake_setup):
    app_root, payload, _, calls = fake_setup
    (payload / "science-requirements.txt").write_text("changed source, stale lock")
    with pytest.raises(RuntimeError, match="lock does not match"):
        dependencies.setup(app_root, payload)
    assert calls == []


def test_pip_stage_timeout_kills_descendants_and_releases_their_lock(tmp_path):
    # This is a real process group: the child ignores TERM and owns a file lock.
    # An orphaned installer child would keep this lock held after setup returns.
    import fcntl

    lock = tmp_path / "child.lock"
    script = tmp_path / "stage.py"
    child = (
        "import fcntl,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"stream = open({str(lock)!r}, 'w'); "
        "fcntl.flock(stream, fcntl.LOCK_EX); stream.write('ready'); stream.flush(); time.sleep(60)"
    )
    script.write_text(f"import subprocess,sys,time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)\n")
    with pytest.raises(dependencies.VerificationIncompleteError, match="total time limit"):
        dependencies.run_stage([sys.executable, str(script)], timeout=1, stage="Test pip stage")
    assert lock.read_text() == "ready"
    with lock.open("a") as stream:
        # Delivery of SIGKILL to an orphan descendant is asynchronous.
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)


def test_launcher_termination_unwinds_stage_and_stops_child(tmp_path):
    import fcntl

    app = tmp_path / "app"
    app.mkdir()
    (app / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    lock = tmp_path / "cancelled-child.lock"
    child = (
        "import fcntl,signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"stream = open({str(lock)!r}, 'w'); "
        "fcntl.flock(stream, fcntl.LOCK_EX); stream.write('ready'); stream.flush(); time.sleep(60)"
    )
    harness = tmp_path / "cancellation.py"
    harness.write_text(
        "import importlib.util,sys\n"
        f"spec = importlib.util.spec_from_file_location('dependencies', {str(ROOT / 'packaging/windows/dependency_setup.py')!r})\n"
        "dependencies = importlib.util.module_from_spec(spec); spec.loader.exec_module(dependencies)\n"
        f"dependencies.setup = lambda *a, **kw: dependencies.run_stage([sys.executable, '-c', {child!r}], timeout=60, stage='cancel test')\n"
        f"sys.argv = ['dependencies', '--app-root', {str(app)!r}, '--payload-dir', {str(tmp_path)!r}]\n"
        "raise SystemExit(dependencies.main())\n"
    )
    process = subprocess.Popen([sys.executable, str(harness)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 3
        while not lock.exists() or lock.read_text() != "ready":
            assert process.poll() is None, process.communicate()
            if time.monotonic() >= deadline:
                pytest.fail("Installer child did not become ready")
            time.sleep(0.01)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=10)  # Includes the stage's five-second TERM grace period.
        assert process.returncode == 143
        with lock.open("a") as stream:
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
