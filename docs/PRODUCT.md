# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary users are researchers running GROMACS locally on Linux or Intel/AMD x64 Windows 10/11 with WSL2. They understand molecular dynamics concepts and want guided preparation, execution, monitoring, and analysis without hand-copying long command chains. Windows users can install the desktop launcher and configure the Linux calculation environment through the supplied installer.

## Product Purpose

The product is a local web control console for GROMACS. It turns common molecular dynamics workflows into guided forms, command previews, task execution, logs, and downloadable outputs.

Success means a user can choose a workflow, identify the required input files, inspect the exact generated commands, launch the job, monitor progress, and retrieve outputs without losing the ability to verify the underlying GROMACS operations.

## Positioning

The product keeps calculations on the user's machine, exposes generated commands, and organizes inputs, execution records, logs, and outputs around individual GROMACS jobs. The browser interface provides the same workflow on Linux and Windows/WSL2.

## Operating Context

The interface runs as a local FastAPI application with static HTML, CSS, and JavaScript. A separate worker executes queued calculations and writes task state to SQLite. Users upload structures, topologies, MDP files, TPR files, and trajectories, then run protein MD, protein-ligand MD, energy minimization, direct TPR execution, RMSD analysis, trajectory post-processing, analysis suites, or supported custom GROMACS commands.

On Linux, users configure the Python environment and scientific executables. On Windows, the installer configures or reuses Ubuntu 24.04 WSL2 and installs GROMACS, Open Babel, ACPYPE with ligand AmberTools, and DSSP. Compatible NVIDIA hardware uses a verified CUDA build; other detected configurations use a CPU build. The desktop shortcut opens the local browser interface, and closing that interface leaves background calculations running. Installation requirements and verification limits are documented in [WINDOWS.md](WINDOWS.md).

## Capabilities and Constraints

Confirmed capabilities from code:

- Environment diagnostics, configurable `gmx` path, and CPU/GPU parameter recommendations based on the available build and runtime.
- Guided workflows for protein MD, protein-ligand MD with multiple ligand definitions, EM, TPR execution, RMSD, post-processing, analysis suites, and supported custom commands.
- Command previews, MDP protocol summaries and overrides, input checks, and browser-local parameter drafts.
- Multipart file upload into per-job working directories, with input hashes and experiment manifests.
- Task summaries paginated in groups of 50, with server-side search across task history by name, ID, status, or workflow. Search accepts Chinese status/workflow labels; selecting a historical task remains independent of the visible list page.
- Task details, streamed progress and logs, cancellation, retry, checkpoint continuation, output listings, and downloads. Registered process identities protect live calculations during recovery and Windows environment updates/removal.
- Existing-result analysis jobs that copy and hash selected source inputs before running additional metrics.
- Interactive XVG plots with metric/replica filters, compatible replica overlays, hover values, and time-window summaries; full source data remains downloadable.
- Research analysis of protein conformation and single-ligand binding contacts, with sequence-mapped comparisons between completed research tasks and JSON/TSV exports. Definitions and limits are documented in [RESEARCH-ANALYSIS.md](RESEARCH-ANALYSIS.md).
- PNG/PDF figure generation and MDP templates under `mdp/protein/` and `mdp/complex/`.

Constraints:

- Calculations depend on Linux executables such as `gmx`, ACPYPE, Open Babel, and DSSP. The Windows installer prepares these in WSL2; Linux installations require their local configuration.
- Windows packaging targets x64. Automatic GPU setup requires a compatible NVIDIA CUDA driver/device; AMD and Intel GPUs use the automatic CPU route.
- The deployment boundary is a trusted local workstation. Task files and trajectories consume local disk space, and copied analysis inputs require additional storage.
- Each research analysis uses one trajectory; group comparisons weight trajectories equally and require compatible selections and residue mappings. Scientific interpretation must respect the documented sampling and comparison limits.
- It should preserve field names and workflow names unless the backend changes with them.
- It should avoid fabricating simulation validity claims. The UI can guide setup, but scientific correctness remains the user's responsibility.

## Brand Commitments

The product is named "GROMACS Console" ("GROMACS 控制台" in the Chinese interface). Its voice is practical, technical, and Chinese-first, with English scientific terms retained where they match GROMACS usage.

The interface and Windows installer share a deep-green/light-green molecular symbol. Dedicated SVG, Windows ICO, and high-resolution installer BMP assets are included. The project also bundles IBM Plex Sans, JetBrains Mono, and Lucide icons with their license files.

The project's own code is attributed to `yzk`, with `Copyright (c) 2026 yzk` and the MIT license. The page links to the project license; Windows installation files include the license and third-party notices. License scope and attribution are documented in [LICENSING.md](LICENSING.md).

## Evidence on Hand

Real source files:

- `app/main.py`
- `app/job_store.py`
- `app/execution.py`
- `app/gromacs.py`
- `app/templates/index.html`
- `app/static/styles.css`
- `app/static/js/main.js` and its sibling modules
- `app/static/gromacs-console.svg`
- `app/static/fonts/` and `app/static/icons/lucide/`
- `mdp/protein/` and `mdp/complex/`
- `packaging/windows/installer.nsi`
- `packaging/windows/GromacsConsole.ico`, `wizard-header.bmp`, and `wizard-sidebar.bmp`
- `LICENSE` and `THIRD_PARTY_NOTICES.md`
- `README.md`

Use the included visual assets and supported capabilities when describing the product. Customer testimonials, external endorsements, and hardware performance claims require separately verified evidence.

## Product Principles

- Keep command transparency: users should always be able to inspect generated GROMACS commands.
- Make readiness explicit: required files, missing parameters, and blocked launch states must be visible before submission.
- Optimize for repeated work: dense but organized controls matter more than marketing-style presentation.
- Preserve local control: local paths, local binaries, logs, and output files are first-class interface objects.
- Design for correction: errors should name the missing input and the recovery path.

## Accessibility & Inclusion

The design target is WCAG AA for contrast, visible focus, keyboard access, and mobile touch targets; this is not a compliance certification. The interface uses responsive layouts, a collapsible task sidebar on narrow screens, and a native confirmation dialog with keyboard focus containment and focus restoration. Windows installer graphics support high-DPI scaling. Browser regression checks cover desktop, tablet, and mobile viewports.
