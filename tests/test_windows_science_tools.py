from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("science_tools", ROOT / "packaging/windows/science-tools.py")
science = importlib.util.module_from_spec(spec)
spec.loader.exec_module(science)


def make_bundle(app_root):
    bundle = app_root / ".venv/lib/python3.12/site-packages/acpype/amber_linux"
    (bundle / "bin").mkdir(parents=True)
    for name in ("antechamber", "parmchk2", "tleap", "sqm"):
        program = bundle / "bin" / name
        program.write_text("#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
    return bundle


def test_expose_bundle_reuses_owned_link(tmp_path):
    bundle = make_bundle(tmp_path)
    science.expose_amber_tools(tmp_path, bundle)
    science.expose_amber_tools(tmp_path, bundle)
    assert (tmp_path / "tools").is_symlink()
    assert (tmp_path / "tools").resolve() == bundle


@pytest.mark.parametrize("existing_link", [False, True])
def test_expose_bundle_preserves_existing_tools(tmp_path, existing_link):
    bundle = make_bundle(tmp_path)
    existing = tmp_path / "old-tools"
    existing.mkdir()
    (existing / "simulation.dat").write_text("valuable")
    if existing_link:
        (tmp_path / "tools").symlink_to(existing, target_is_directory=True)
    else:
        existing.rename(tmp_path / "tools")
    with pytest.raises(RuntimeError, match="refusing to replace"):
        science.expose_amber_tools(tmp_path, bundle)
    assert (tmp_path / "tools/simulation.dat").read_text() == "valuable"


def test_expose_rejects_bundle_outside_application_venv(tmp_path):
    app_root = tmp_path / "application"
    app_root.mkdir()
    bundle = make_bundle(tmp_path / "unrelated")
    with pytest.raises(RuntimeError, match="virtual environment"):
        science.expose_amber_tools(app_root, bundle)
    assert not (app_root / "tools").exists()


def test_topology_validation_rejects_exit_zero_without_files(tmp_path):
    with pytest.raises(RuntimeError, match="did not produce"):
        science.check_ethanol_outputs(tmp_path)


@pytest.mark.parametrize("charge", [0.0, float("nan"), 1.0])
def test_topology_validation_checks_real_molecular_shape(tmp_path, charge):
    rows = [f"{i} c3 1 ETH A{i} {i} {charge} 1.0" for i in range(1, 10)]
    bonds = [f"1 {i} 1" for i in range(2, 10)]
    (tmp_path / "smoke_GMX.itp").write_text("[ atoms ]\n" + "\n".join(rows) + "\n[ bonds ]\n" + "\n".join(bonds))
    (tmp_path / "smoke_GMX.gro").write_text("Ethanol\n9\n" + "ATOM\n" * 9 + "BOX\n")
    (tmp_path / "smoke_AC.prmtop").write_text("%VERSION  VERSION_STAMP = V0001.000")
    if charge == 0:
        science.check_ethanol_outputs(tmp_path)
        (tmp_path / "smoke_GMX.itp").write_text("[ atoms ]\n" + "\n".join(rows))
        with pytest.raises(RuntimeError, match="invalid atoms"):
            science.check_ethanol_outputs(tmp_path)
    else:
        with pytest.raises(RuntimeError, match="invalid atoms"):
            science.check_ethanol_outputs(tmp_path)


def test_ligand_smoke_uses_bcc_gaff2_and_cleans_temporary_files(tmp_path, monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "acpype" in command[0]:
            raise RuntimeError("Broken AmberTools")
        return subprocess.CompletedProcess(command, 0, "sqm [-O]", "")

    monkeypatch.setattr(science.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Broken AmberTools"):
        science.verify_science_tools(tmp_path)
    assert commands[0] == ["/usr/bin/mkdssp", "--version"]
    assert commands[1] == [str(tmp_path / "tools/bin/sqm"), "-h"]
    assert commands[2][1:4] == ["-:CCO", "--gen3d", "-h"]
    assert commands[3][5:9] == ["-c", "bcc", "-a", "gaff2"]
    assert not list(tmp_path.glob("ligand-check-*"))


def test_missing_lapack_stops_before_ligand_conversion_with_repair_hint(tmp_path, monkeypatch):
    commands = []

    def missing_library(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 127, "", "sqm: error while loading shared libraries: liblapack.so.3: cannot open shared object file"
        )

    monkeypatch.setattr(science.subprocess, "run", missing_library)
    with pytest.raises(RuntimeError, match="liblapack.so.3") as failure:
        science.verify_science_tools(tmp_path, require_dssp=False)
    assert "liblapack3, libblas3 and libgfortran5" in str(failure.value)
    assert len(commands) == 1
    assert not list(tmp_path.glob("ligand-check-*"))
