from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.diagnostics import cuda_runtime_information, environment_diagnostics
from app.execution import WorkerEngine
from app.gromacs import Step, detect_gromacs, load_settings
from app.job_store import JobStore

SERVICE_PATH = Path(__file__).resolve().parents[1] / "packaging/windows/desktop_service.py"
spec = importlib.util.spec_from_file_location("windows_runtime_service", SERVICE_PATH)
assert spec and spec.loader
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


def test_web_and_worker_environment_finds_installed_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/existing/libs")
    for name in ("acpype", "obabel"):
        executable(tmp_path / ".venv/bin" / name, f"echo {name}\n")
    executable(tmp_path / "tools/bin/antechamber", 'test "$AMBERHOME" = "$EXPECTED_AMBERHOME"\n')
    executable(tmp_path / "gromacs/bin/gmx", "echo GROMACS\n")
    env = service.service_environment(tmp_path, 8003)
    env["EXPECTED_AMBERHOME"] = str(tmp_path / "tools")
    for binary in ("acpype", "obabel", "antechamber", "gmx"):
        assert shutil.which(binary, path=env["PATH"])
        subprocess.run([binary], env=env, check=True, capture_output=True)
    assert env["GMX_BIN"] == str(tmp_path / "gromacs/bin/gmx")
    assert env["PORT"] == "8003"
    assert env["LD_LIBRARY_PATH"] == "/existing/libs"


def test_real_worker_tool_processes_inherit_installed_tool_environment(tmp_path, monkeypatch):
    marker = tmp_path / "tool-result.txt"
    executable(tmp_path / ".venv/bin/acpype", f'antechamber && obabel && printf "%s\\n" "$AMBERHOME" > "{marker}"\n')
    executable(tmp_path / ".venv/bin/obabel", "exit 0\n")
    executable(tmp_path / "tools/bin/antechamber", 'test -n "$AMBERHOME"\n')
    env = service.service_environment(tmp_path, 8000)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("AMBERHOME", env["AMBERHOME"])
    monkeypatch.setattr("app.execution.MIN_FREE_DISK_BYTES", 0)
    store = JobStore(tmp_path / "runtime")
    step = Step("Prepare ligand", ["acpype"])
    job = store.create("runtime-tools", {"workflow": "custom"}, [], [step])
    store.start(job["id"], [step])
    assert WorkerEngine(store, "tools-test").run_once()
    assert store.get(job["id"])["status"] == "completed"
    assert marker.read_text().strip() == str(tmp_path / "tools")


def test_cpu_to_gpu_prefix_upgrade_preserves_saved_path_and_simulation_data(tmp_path):
    import json

    gpu_spec = importlib.util.spec_from_file_location("runtime_gpu_setup", SERVICE_PATH.with_name("gpu_setup.py"))
    assert gpu_spec and gpu_spec.loader
    gpu = importlib.util.module_from_spec(gpu_spec)
    gpu_spec.loader.exec_module(gpu)
    prefix = tmp_path / "gromacs"
    executable(prefix / "bin/gmx", "echo CPU-GROMACS\n")
    saved_settings = {"gmx_bin": str(prefix / "bin/gmx"), "max_parallel": 2}
    (tmp_path / "settings.json").write_text(json.dumps(saved_settings))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    checkpoint = runtime / "simulation.cpt"
    checkpoint.write_bytes(b"saved simulation checkpoint")
    staged = tmp_path / "new-gromacs"
    executable(staged / "bin/gmx", "echo GPU-GROMACS\n")
    gpu.replace_prefix(staged, prefix)
    settings = load_settings(tmp_path)
    assert settings["gmx_bin"] == saved_settings["gmx_bin"]
    assert settings["max_parallel"] == 2
    assert detect_gromacs(settings["gmx_bin"])["version"] == "GPU-GROMACS"
    assert checkpoint.read_bytes() == b"saved simulation checkpoint"


@pytest.mark.parametrize("action", ["run", "resume", "adopt", "cancel"])
def test_update_and_remove_preserve_interrupted_jobs_waiting_for_worker(tmp_path, action):
    store = JobStore(tmp_path / "runtime")
    step = Step("Resume MD", ["gmx", "mdrun", "-cpi", "simulation.cpt"])
    job = store.create("interrupted-md", {"workflow": "custom"}, [], [step])
    store.start(job["id"], [step])
    store.update(job["id"], status="interrupted", desired_action=action)
    checkpoint = store.job_dir(job["id"]) / "simulation.cpt"
    checkpoint.write_bytes(b"recoverable checkpoint")
    (tmp_path / "service.json").write_text('{"processes": []}')
    executable(tmp_path / "gromacs/bin/gmx", "echo existing GROMACS\n")
    for operation in ("stop", "remove"):
        with pytest.raises(RuntimeError, match="waiting to resume/adopt"):
            service.manage(tmp_path, operation)
    assert store.get(job["id"])["desired_action"] == action
    assert checkpoint.read_bytes() == b"recoverable checkpoint"
    assert (tmp_path / "service.json").exists()
    assert (tmp_path / "gromacs/bin/gmx").exists()


def test_historical_interrupted_job_does_not_block_stopping_service(tmp_path):
    store = JobStore(tmp_path / "runtime")
    step = Step("Resume MD", ["gmx", "mdrun", "-cpi", "simulation.cpt"])
    job = store.create("interrupted-md", {"workflow": "custom"}, [], [step])
    store.start(job["id"], [step])
    store.update(job["id"], status="interrupted", desired_action=None)
    assert service.manage(tmp_path, "stop") == {"status": "stopped"}
    assert store.get(job["id"])["status"] == "interrupted"


def test_environment_diagnostics_probe_ambertools_and_dssp(tmp_path):
    with (
        patch("app.diagnostics.discover_local_force_fields", return_value=[]),
        patch("app.diagnostics.installed_water_model_selection", return_value=None),
        patch("app.diagnostics.host_information", return_value={"cpu_count": 8, "physical_core_count": 4}),
        patch("app.diagnostics.gpu_information", return_value={"available": False, "devices": []}),
        patch("app.diagnostics._probe", return_value={"available": True, "version": ""}) as probe,
    ):
        result = environment_diagnostics(tmp_path, {})
    assert ["antechamber", "-h"] in [call.args[0] for call in probe.call_args_list]
    assert ["mkdssp", "--version"] in [call.args[0] for call in probe.call_args_list]
    assert result["ambertools"]["available"] is True
    assert result["dssp"]["available"] is True


@pytest.mark.parametrize(
    "backend,gmx_available,runtime_available,count,expected",
    [
        ("CUDA", True, True, 2, 2),
        ("CUDA", True, False, 0, 0),
        ("CUDA", False, True, 2, 0),
        ("disabled", True, True, 2, 0),
        ("SYCL", True, True, 2, 0),
        ("OpenCL", True, True, 2, 0),
        ("", True, True, 2, 0),
    ],
)
def test_gpu_recommendation_matches_build_backend_and_driver(backend, gmx_available, runtime_available, count, expected, tmp_path):
    version = f"GROMACS version: 2026.3\nPrecision: mixed\nGPU support: {backend}\n"
    with (
        patch("app.diagnostics.discover_local_force_fields", return_value=[]),
        patch("app.diagnostics.installed_water_model_selection", return_value="1"),
        patch("app.diagnostics.host_information", return_value={"cpu_count": 8, "physical_core_count": 4}),
        patch("app.diagnostics.gpu_information", return_value={"available": True, "devices": ["NVIDIA GPU"]}),
        patch("app.diagnostics._probe", return_value={"available": gmx_available, "version": version}),
        patch(
            "app.diagnostics.cuda_runtime_information",
            return_value={
                "available": runtime_available,
                "device_count": count,
                "reason": "" if runtime_available else "driver unavailable",
            },
        ) as runtime_probe,
    ):
        result = environment_diagnostics(tmp_path, {"gmx_bin": "gmx"})
    assert result["performance_recommendation"]["gpu_count"] == expected
    assert result["performance_recommendation"]["gpu"] is bool(expected)
    assert result["gpu"]["runtime_verified"] is bool(expected)
    assert result["gpu"]["gromacs_backend"] == backend
    if backend == "CUDA" and gmx_available:
        runtime_probe.assert_called_once()
    else:
        runtime_probe.assert_not_called()
    if backend in {"SYCL", "OpenCL"}:
        assert backend in result["gpu"]["reason"]


@pytest.mark.parametrize(
    "output,code,expected",
    [
        ('{"device_count": 2, "driver_version": 12090}', 0, 2),
        ('{"device_count": 0, "driver_version": 12090}', 0, 0),
        ("", 1, 0),
        ("unexpected", 0, 0),
    ],
)
def test_cuda_runtime_handles_driver_results(output, code, expected, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    with patch("app.diagnostics.subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout=output, stderr="")):
        result = cuda_runtime_information()
    assert result["device_count"] == expected
    assert result["available"] is bool(expected)


def test_cuda_runtime_does_not_use_devices_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    with patch("app.diagnostics.subprocess.run") as run:
        assert cuda_runtime_information()["available"] is False
    run.assert_not_called()


def test_cuda_driver_probe_has_timeout(monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    with patch("app.diagnostics.subprocess.run", side_effect=subprocess.TimeoutExpired("driver", 4)):
        assert cuda_runtime_information()["available"] is False


def test_cuda_runtime_requires_sufficient_driver_version(monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    output = '{"device_count": 1, "driver_version": 12020}'
    with patch("app.diagnostics.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr="")):
        result = cuda_runtime_information(minimum_driver_version=12090)
    assert result["available"] is False
    assert result["device_count"] == 0
    assert "too old" in result["reason"]


@pytest.mark.parametrize(
    "cuda_version,driver_version,expected",
    [
        ("13.10", 13000, True),
        ("13.1", 13000, True),
        ("13.10", 12090, False),
        ("12.90", 12000, True),
        ("12.90", 11080, False),
        ("12.90", 13000, True),
        ("11.80", 11000, True),
        ("11.80", 10020, False),
        ("10.20", 10020, True),
        ("10.20", 10010, False),
    ],
)
def test_gpu_recommendation_respects_cuda_minor_version_compatibility(cuda_version, driver_version, expected, tmp_path, monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    version = f"GPU support: CUDA\nCUDA runtime: {cuda_version}\n"
    output = f'{{"device_count": 1, "driver_version": {driver_version}}}'
    with (
        patch("app.diagnostics.discover_local_force_fields", return_value=[]),
        patch("app.diagnostics.installed_water_model_selection", return_value="1"),
        patch("app.diagnostics.host_information", return_value={"cpu_count": 8, "physical_core_count": 4}),
        patch("app.diagnostics.gpu_information", return_value={"available": True, "devices": ["NVIDIA GPU"]}),
        patch("app.diagnostics._probe", return_value={"available": True, "version": version}),
        patch("app.diagnostics.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr="")),
    ):
        result = environment_diagnostics(tmp_path, {})
    assert result["performance_recommendation"]["gpu"] is expected
    assert result["performance_recommendation"]["gpu_count"] == int(expected)
    assert result["performance_recommendation"]["max_parallel"] == (1 if expected else 2)
    assert result["gpu"]["runtime_verified"] is expected
    assert result["gpu"]["cuda_driver_version"] == driver_version
    if expected:
        assert result["gpu"]["reason"] == ""
    else:
        assert "too old" in result["gpu"]["reason"]


def test_gromacs_cuda_runtime_version_is_checked_against_driver(tmp_path):
    with (
        patch("app.diagnostics.discover_local_force_fields", return_value=[]),
        patch("app.diagnostics.installed_water_model_selection", return_value="1"),
        patch("app.diagnostics.host_information", return_value={"cpu_count": 8, "physical_core_count": 4}),
        patch("app.diagnostics.gpu_information", return_value={"available": True, "devices": ["NVIDIA GPU"]}),
        patch("app.diagnostics._probe", return_value={"available": True, "version": "GPU support: CUDA\nCUDA runtime: 12.90\n"}),
        patch(
            "app.diagnostics.cuda_runtime_information", return_value={"available": False, "device_count": 0, "reason": "old driver"}
        ) as probe,
    ):
        result = environment_diagnostics(tmp_path, {})
    probe.assert_called_once_with(minimum_driver_version=12000)
    assert result["performance_recommendation"]["gpu"] is False


def test_remove_unlinks_tools_without_deleting_external_target(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    outside = tmp_path / "existing-tools"
    outside.mkdir()
    marker = outside / "preserve.txt"
    marker.write_text("keep")
    (root / "tools").symlink_to(outside, target_is_directory=True)
    with patch.object(service, "stop"):
        assert service.manage(root, "remove") == {"status": "stopped"}
    assert marker.read_text() == "keep"
    assert not (root / "tools").is_symlink()
