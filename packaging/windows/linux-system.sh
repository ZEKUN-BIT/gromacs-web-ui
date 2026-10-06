#!/usr/bin/env bash
# Runs as WSL root for system dependencies. The application never runs as root.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
    echo 'This installer supports Ubuntu 24.04 x86_64 on WSL2.' >&2
    exit 1
fi
app_user=gromacs-console
if id "$app_user" >/dev/null 2>&1; then
    [[ "$(id -u "$app_user")" != 0 && "$(getent passwd "$app_user" | cut -d: -f6)" == /home/gromacs-console ]] || exit 1
else
    useradd --create-home --shell /bin/bash "$app_user"
fi
# Open Babel and the ligand-only AmberTools bundle are installed as hashed Python
# wheels by the unprivileged installer; no duplicate apt Open Babel or Conda.
# AmberTools sqm links to system LAPACK/BLAS/Fortran; the wheel does not bundle them.
runtime_packages=(python3 python3-venv ca-certificates curl libfftw3-single3 libgomp1 dssp zstd ocl-icd-libopencl1 liblapack3 libblas3 libgfortran5)
echo '[1/4] Checking required Ubuntu system packages...'
missing_packages=()
for package in "${runtime_packages[@]}"; do
    if status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null) && [[ "$status" == 'install ok installed' ]]; then
        continue
    fi
    missing_packages+=("$package")
done
if (( ${#missing_packages[@]} )); then
    echo "[1/4] Installing missing system packages: ${missing_packages[*]}"
    apt-get update
    apt-get install -y --no-install-recommends "${missing_packages[@]}"
else
    echo '[1/4] Required system packages are already installed; skipping apt downloads.'
fi
