from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .gromacs import discover_local_force_fields, installed_water_model_selection


def _probe(command: list[str], timeout: float = 4.0) -> dict:
    binary = command[0]
    resolved = shutil.which(binary) if not Path(binary).is_file() else str(Path(binary).resolve())
    result = {"available": False, "binary": binary, "resolved": resolved, "version": ""}
    if not resolved:
        return result
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        result.update(available=completed.returncode == 0, version=output[:2000], returncode=completed.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["error"] = str(exc)
    return result


def host_information() -> dict:
    logical_cpus = os.cpu_count() or 1
    physical_cores: set[tuple[str, str]] = set()
    for topology in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/topology"):
        try:
            package = (topology / "physical_package_id").read_text(encoding="utf-8").strip()
            core = (topology / "core_id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        physical_cores.add((package, core))
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": logical_cpus,
        "physical_core_count": len(physical_cores) or logical_cpus,
    }


def gpu_information() -> dict:
    probe = _probe(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"])
    devices = [line.strip() for line in probe.get("version", "").splitlines() if line.strip()]
    probe["devices"] = devices
    return probe


def cuda_runtime_information(minimum_driver_version: int = 0) -> dict:
    # Probe in an isolated process: a broken driver must not hang the Web API.
    script = """
import ctypes
import ctypes.util
import json
from pathlib import Path
library = ctypes.util.find_library('cuda')
if not library:
    library = '/usr/lib/wsl/lib/libcuda.so.1' if Path('/usr/lib/wsl/lib/libcuda.so.1').is_file() else 'libcuda.so.1'
driver = ctypes.CDLL(library)
if driver.cuInit(0) != 0:
    raise SystemExit(1)
count = ctypes.c_int()
if driver.cuDeviceGetCount(ctypes.byref(count)) != 0:
    raise SystemExit(1)
version = ctypes.c_int()
if driver.cuDriverGetVersion(ctypes.byref(version)) != 0:
    raise SystemExit(1)
print(json.dumps({'device_count': count.value, 'driver_version': version.value}))
"""
    if os.environ.get("CUDA_VISIBLE_DEVICES") in {"", "-1"}:
        return {"available": False, "device_count": 0, "reason": "CUDA devices are disabled by CUDA_VISIBLE_DEVICES."}
    try:
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=4, check=False)
        result = json.loads(completed.stdout) if completed.returncode == 0 else {}
        count = int(result.get("device_count", 0))
        driver_version = int(result.get("driver_version", 0))
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        count, driver_version = 0, 0
    if count > 0 and driver_version < minimum_driver_version:
        return {
            "available": False,
            "device_count": 0,
            "driver_version": driver_version,
            "reason": "The NVIDIA driver is too old for this GROMACS CUDA runtime; update the Windows driver when using WSL2.",
        }
    return {
        "available": count > 0,
        "device_count": max(0, count),
        "driver_version": driver_version,
        "reason": "" if count > 0 else "No usable NVIDIA CUDA device was found; check the Windows NVIDIA driver when using WSL2.",
    }


def environment_diagnostics(base_dir: Path, settings: dict) -> dict:
    gmx_bin = str(settings.get("gmx_bin") or "gmx")
    force_fields = discover_local_force_fields(base_dir)
    force_field_names = sorted({str(item.get("name") or "") for item in force_fields if item.get("name")})
    configured_force_field = str(settings.get("force_field") or "amber19sb")
    water_models = {}
    for model in ("opc", "opc3", "tip3p", "spce", "tip4p"):
        selection = installed_water_model_selection(gmx_bin, configured_force_field, model)
        water_models[model] = {"available": selection is not None, "selection": selection}
    configured_force_field_available = any(item["available"] for item in water_models.values())
    if configured_force_field_available and configured_force_field not in force_field_names:
        force_field_names.append(configured_force_field)
    host = host_information()
    gpu = gpu_information()
    gromacs = _probe([gmx_bin, "--version"])
    gpu_support = re.search(r"GPU support:[ \t]*([^\r\n]*)", gromacs.get("version", ""), re.IGNORECASE)
    backend = gpu_support.group(1).strip() if gpu_support else "unknown"
    gpu["gromacs_backend"] = backend
    gpu["gromacs_supported"] = bool(gromacs.get("available") and backend.lower() not in {"disabled", "none", "off", "unknown", ""})
    if gromacs.get("available") and backend.lower() == "cuda":
        cuda_version = re.search(r"CUDA runtime:[ \t]*(\d+)\.(\d+)", gromacs.get("version", ""), re.IGNORECASE)
        minimum_driver_version = 0
        if cuda_version:
            major = int(cuda_version.group(1))
            # CUDA 11+ supports minor-version compatibility within a major release.
            minimum_driver_version = 1000 * major
            if major < 11:
                # GROMACS prints CUDA's encoded minor component (10.20 for CUDA 10.2).
                minor = int(cuda_version.group(2))
                minimum_driver_version += minor if minor >= 10 else 10 * minor
        runtime = cuda_runtime_information(minimum_driver_version=minimum_driver_version)
        gpu_count = runtime["device_count"] if runtime["available"] else 0
        gpu["cuda_driver_version"] = runtime.get("driver_version")
        gpu["reason"] = runtime["reason"]
    else:
        gpu_count = 0
        gpu["reason"] = (
            "GROMACS GPU acceleration is disabled in this build."
            if not gpu["gromacs_supported"]
            else f"This GROMACS build uses {backend}; CUDA device detection cannot verify that backend."
        )
    gpu["runtime_verified"] = gpu_count > 0
    recommendation = {
        "ntmpi": 1,
        "ntomp": int(host["physical_core_count"]),
        "pin": "on",
        "gpu": gpu_count > 0,
        "gpu_count": gpu_count,
        "max_parallel": 1 if gpu_count else max(1, int(host["cpu_count"]) // int(host["physical_core_count"])),
    }
    return {
        "gromacs": gromacs,
        "acpype": _probe([str(settings.get("acpype_bin") or "acpype"), "--help"]),
        "ambertools": _probe(["antechamber", "-h"]),
        "open_babel": _probe([str(settings.get("obabel_bin") or "obabel"), "-V"]),
        "dssp": _probe(["mkdssp", "--version"]),
        "gpu": gpu,
        "force_fields": {
            "available": bool(force_fields) or configured_force_field_available,
            "configured": configured_force_field,
            "names": force_field_names,
            "items": force_fields,
        },
        "water_models": water_models,
        "host": host,
        "performance_recommendation": recommendation,
    }
