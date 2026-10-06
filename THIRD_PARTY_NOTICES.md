# Third-party software and assets

GROMACS Console's original code is licensed under the MIT License, copyright
(c) 2026 yzk. That license does not replace the licenses of the components below.
GROMACS Console is an independent project and is not endorsed by these projects.

## Assets included in the application

| Component | License | Included license |
| --- | --- | --- |
| IBM Plex Sans | SIL Open Font License 1.1 | `app/static/fonts/IBM-Plex-OFL.txt` |
| JetBrains Mono | SIL Open Font License 1.1 | `app/static/fonts/JetBrains-Mono-OFL.txt` |
| Lucide icons | ISC; original Feather icons MIT | `app/static/icons/lucide/LICENSE` |

Font licenses permit redistribution subject to their conditions, including
retention of notices and any reserved font names. Fonts are not relicensed under MIT.

## Components downloaded or supplied by the target system

The lightweight Windows installer downloads scientific tools and their
dependencies separately. Their copyright and license files remain in the
installed packages. Ubuntu and WSL are separate system components, not part of
the application's MIT grant.

| Component | License / source of terms |
| --- | --- |
| GROMACS | LGPL 2.1 or later; GROMACS package license/source notices |
| CUDA cuFFT, NVRTC and nvJitLink | NVIDIA proprietary redistribution terms in the downloaded NVIDIA wheels; installed under `gromacs/share/licenses/` |
| ACPYPE | GPL 3.0 or later; wheel metadata and license files |
| Bundled AmberTools programs | Individual AmberTools component licenses distributed in the ACPYPE wheel; no claim that all components share one license |
| Open Babel | GPL 2.0; Open Babel / openbabel-wheel distribution notices |
| DSSP / mkdssp | BSD 2-Clause; Ubuntu package copyright file |
| MDAnalysis 2.10.0 | LGPL 3.0 or later; installed distribution license files |
| NumPy, SciPy, Biopython, Matplotlib and web/Python dependencies | Their respective upstream license files and installed distribution metadata |
| Ubuntu packages | `/usr/share/doc/<package>/copyright` inside the distribution |
| Microsoft WSL | Microsoft WSL license and Windows component terms |

Package versions and wheel hashes for the Windows Python environment are
recorded in `windows-python-requirements.lock`. GROMACS/CUDA package URLs and
hashes are fixed in `gpu_setup.py`. When redistributing third-party binaries,
retain their notices and comply with any source-offer, attribution and
redistribution requirements that apply to those specific components. The MIT
license for this application's code does not waive those requirements.
