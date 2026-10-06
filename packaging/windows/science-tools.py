#!/usr/bin/env python3
"""Expose the small wheel-bundled AmberTools and test real ligand preparation."""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def expose_amber_tools(app_root: Path, bundle: Path) -> Path:
    app_root = app_root.resolve()
    bundle = bundle.resolve()
    if not bundle.is_relative_to((app_root / ".venv").resolve()):
        raise RuntimeError("AmberTools must come from the application's virtual environment.")
    for name in ("antechamber", "parmchk2", "tleap", "sqm"):
        if not os.access(bundle / "bin" / name, os.X_OK):
            raise RuntimeError(f"Bundled AmberTools executable is missing: {name}")
    tools = app_root / "tools"
    if tools.is_symlink():
        if tools.resolve() != bundle:
            raise RuntimeError("Existing tools link points elsewhere; refusing to replace it.")
    elif tools.exists():
        raise RuntimeError("Existing tools directory is preserved; refusing to replace it.")
    else:
        tools.symlink_to(bundle, target_is_directory=True)
    return tools


def check_ethanol_outputs(directory: Path) -> None:
    """Require a neutral, fully bonded nine-atom ethanol topology and coordinates."""
    topology = directory / "smoke_GMX.itp"
    coordinates = directory / "smoke_GMX.gro"
    amber_topology = directory / "smoke_AC.prmtop"
    if not all(path.is_file() and path.stat().st_size > 0 for path in (topology, coordinates, amber_topology)):
        raise RuntimeError("ACPYPE did not produce GROMACS and Amber topology files.")
    section = ""
    charges = []
    bonds = []
    for line in topology.read_text().splitlines():
        line = line.split(";", 1)[0].strip()
        if line.startswith("["):
            section = line.strip("[] ").strip()
        elif line and not line.startswith("#"):
            fields = line.split()
            if section == "atoms":
                charges.append(float(fields[6]))
            elif section == "bonds":
                bonds.append((int(fields[0]), int(fields[1])))
    gro_lines = coordinates.read_text().splitlines()
    if (
        len(charges) != 9
        or any(not math.isfinite(value) for value in charges)
        or abs(sum(charges)) > 0.001
        or len(bonds) != 8
        or {atom for bond in bonds for atom in bond} != set(range(1, 10))
        or len(gro_lines) != 12
        or int(gro_lines[1].strip()) != 9
    ):
        raise RuntimeError("ACPYPE's ethanol smoke topology has invalid atoms, charges or bonds.")


def verify_science_tools(app_root: Path, *, require_dssp: bool = True) -> None:
    app_root = app_root.resolve()
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(app_root / ".venv/bin"), str(app_root / "tools/bin"), "/usr/bin", "/bin"))
    environment["AMBERHOME"] = str(app_root / "tools")
    # Bundled tool wrappers load their own libraries. Keep them out of GROMACS.
    environment.pop("LD_LIBRARY_PATH", None)
    environment.pop("LD_PRELOAD", None)
    if require_dssp:
        subprocess.run(["/usr/bin/mkdssp", "--version"], env=environment, check=True, timeout=30)
    # Starting sqm first exposes missing system libraries before ligand preparation.
    sqm = subprocess.run([str(app_root / "tools/bin/sqm"), "-h"], env=environment, capture_output=True, text=True, timeout=30)
    if sqm.returncode != 0 or "sqm [-O]" not in sqm.stdout + sqm.stderr:
        detail = (sqm.stderr or sqm.stdout).strip()[-1500:]
        raise RuntimeError(
            f"AmberTools sqm cannot start (exit {sqm.returncode}): {detail}\n"
            "Check the Ubuntu runtime packages: liblapack3, libblas3 and libgfortran5. Then run Configure or Update again."
        )
    with tempfile.TemporaryDirectory(prefix="ligand-check-", dir=app_root) as work:
        workdir = Path(work)
        subprocess.run(
            [str(app_root / ".venv/bin/obabel"), "-:CCO", "--gen3d", "-h", "-osdf", "-O", "ethanol.sdf"],
            cwd=workdir,
            env=environment,
            check=True,
            timeout=120,
        )
        subprocess.run(
            [
                str(app_root / ".venv/bin/acpype"),
                "-i",
                "ethanol.sdf",
                "-b",
                "smoke",
                "-c",
                "bcc",
                "-a",
                "gaff2",
                "-n",
                "0",
                "-o",
                "gmx",
            ],
            cwd=workdir,
            env=environment,
            check=True,
            timeout=180,
        )
        check_ethanol_outputs(workdir / "smoke.acpype")
    print("Open Babel, AmberTools and ACPYPE verified: neutral ethanol BCC/GAFF2 topology generated.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run science-tool verification as the unprivileged application user.")
    if Path(sys.prefix).resolve() != (args.app_root / ".venv").resolve():
        parser.error("Run this helper with the application virtual environment's Python.")
    from acpype.utils import bundled_amber_dir

    bundle = bundled_amber_dir()
    if bundle is None:
        raise RuntimeError("Install the pinned ACPYPE Linux wheel containing AmberTools.")
    try:
        expose_amber_tools(args.app_root, Path(bundle))
        verify_science_tools(args.app_root)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Scientific tool setup failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
