"""The first-use tutorial uses the production queue, worker and offline export."""

import json
import os
import shutil
from pathlib import Path

import pytest

from scripts.generate_offline_example import generate

pytestmark = pytest.mark.gromacs_integration


def test_real_tutorial_generates_a_portable_offline_report(request, tmp_path, monkeypatch):
    if os.environ.get("GROMACS_INTEGRATION") != "1" and "gromacs_integration" not in request.config.option.markexpr:
        pytest.skip("Enable real GROMACS checks with GROMACS_INTEGRATION=1 or -m gromacs_integration")
    gmx = os.environ.get("GROMACS_TEST_GMX") or shutil.which("gmx") or "/usr/local/gromacs/bin/gmx"
    if not Path(gmx).is_file() and not shutil.which(gmx):
        pytest.fail("Real GROMACS was requested but is unavailable")
    # generate changes only this test process's environment; restore it afterwards.
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "GMX_DISABLE_GPU_DETECTION", "CUDA_VISIBLE_DEVICES"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    output = tmp_path / "offline"
    result = generate(gmx, output)
    assert result["energy_frames"] == 101 and result["end_ps"] == 1.0
    summary = json.loads((output / "summary.json").read_text())
    assert summary["job"]["status"] == "completed"
    assert summary["job"]["is_example"]
    assert summary["quality"][0]["status"] == "检查通过"
    assert summary["parameters"]["ntomp"] == 1 and summary["parameters"]["gpu"] is False
    manifest = json.loads((output / "files/experiment-manifest.json").read_text())
    assert {item["path"] for item in manifest["input_files"]} == {"initial.gro", "example.mdp", "example.top"}
    assert all(len(item["sha256"]) == 64 for item in manifest["input_files"])
    assert (output / "files/example-energy.png").read_bytes().startswith(b"\x89PNG")
    assert (output / "report.css").is_file()
    for path in output.rglob("*"):
        if path.is_file() and path.suffix != ".png":
            contents = path.read_text()
            assert "gromacs-offline-example-" not in contents
            assert str(Path.home()) not in contents
