# Research Analysis

## Workflow

Open a completed, non-dry-run task, select Research Analysis, then Create Research Analysis. Choose the matching production TPR and full-system trajectory. Set a time window in ns and a frame stride. Protein-only analysis needs no ligand selection; binding analysis requires an NDX containing Protein and a group for one non-protein ligand residue. The dialog lists the actual index-group names and atom counts.

The server copies inputs independently and hashes them before queueing. For binding, it generates an additional index group containing protein plus the selected ligand, then asks GROMACS to cluster this solute and retain the complete system. It extracts a PDB reference from the first selected frame. MDAnalysis reads this reference and the prepared XTC; the heavy calculation runs in a worker subprocess and can be cancelled through the existing task controls. Research runs before figure generation. Derived trajectories are removed only after successful analysis; source copies and reference structure remain.

Completed research tasks provide three views:

- Conformation: protein-fit C-alpha RMSD, equal-weight C-alpha Rg, per-residue C-alpha RMSF, first/last-window displacement, and a two-component PCA scatter plot.
- Binding: ligand heavy-atom RMSD after protein fitting, ligand RMSD after ligand self-fitting, protein contact-residue count, initial-pocket retention, and per-residue contact occupancy.
- Comparison: select reference and comparison research tasks, then compare trajectory summaries and sequence-mapped residues. Reports export as JSON; single-task residue tables export as TSV. Residue tables can be filtered, sorted, and paginated.

## Definitions

Coordinates are converted from MDAnalysis Angstrom units to nm. Times are converted from ps to ns. A research fit must contain at least three non-collinear protein atoms. RMSD references the first selected frame, not an external experimental structure. C-alpha Rg uses equal atom weights and is explicitly distinguished from all-protein mass-weighted Rg.

RMSF is the root mean squared deviation of each C-alpha from its average position after fitting. Displacement is the distance between mean C-alpha coordinates in the first and last 20% of sampled frames, using at least one frame per segment. PCA is unweighted Cartesian C-alpha PCA after protein fitting and mean subtraction; the first two singular components are calculated with SciPy. Constant or numerically rigid trajectories return zero projections. Independent trajectories have independent PCA bases and are not overlaid across tasks.

A residue has a ligand contact in a frame if any of its heavy atoms is within the selected cutoff of any selected ligand heavy atom. Distances use the periodic minimum image. Multiple atom-pair contacts contribute one Boolean residue contact. Occupancy is the fraction of sampled frames containing this contact. Initial-pocket retention is the fraction of initially contacting residues that still contact the ligand in each sampled frame. If the first frame has no contacts, retention is unavailable, not zero.

Each summary includes a mean, frame-distribution P05/P95, late-quarter minus early-quarter mean, and descriptive block means. P05/P95 are not confidence intervals. Frames within a trajectory are not treated as independent experimental replicates. Uneven sampling receives a warning because frame occupancy is not time occupancy. No affinity, binding-free-energy, equilibration, or convergence verdict is inferred automatically.

## Comparisons

Group means weight each trajectory equally. SD describes the spread of trajectory means and is unavailable for a group containing one trajectory. No frame-level p value or significance claim is generated. Identical source trajectory checksums are rejected across both groups; different checksums alone cannot establish independent sampling. Overlapping trajectory extracts, continuations, and reruns with correlated initial conditions must not be designated independent repeats.

Biopython performs global sequence alignment separately for matching chain identifiers, with match +2, mismatch -1, gap-open -3, and gap-extension -0.5. Residue numbers alone are not used as an alignment. Chain-set differences, unknown residue identities, ambiguous equally scoring mappings, or identity/coverage below 70% prevent residue differences. Missing positions are not interpolated. Common protonation residue aliases are normalized for alignment.

Force field, water model, ion concentration, temperature, pressure, integration timestep, thermostats, barostats, and constraints are compared from source task/MDP records. Missing records are shown as unknown. These records are not a re-decoding of the compiled TPR and do not prove that all physical conditions match. Analyze equivalent equilibrated windows with equivalent fit groups. Different settings are displayed as warnings rather than causal evidence of a mutation effect.

Binding metrics require matching ligand residue names, heavy-atom names/count, and contact cutoffs. This conservative check is not chemical graph equivalence or symmetry-aware ligand atom mapping. Ligand pose RMSD and protein RMSD reference each trajectory's own first frame; their difference is not a structural RMSD between the two systems. Initial-pocket retention can reference different initial contact sets in different systems. Inspect residue occupancy differences alongside it.

## Limits

- One selected trajectory per research job. Multiple completed research jobs may be placed in comparison groups, up to six per group.
- At least 3 sampled frames and 3 C-alpha atoms; at most 5000 frames, 3000 C-alpha residues, and 2 million frame-residue combinations.
- Binding currently supports one ligand residue with 3–256 heavy atoms per research job. Peptide ligands selected as protein, multi-residue ligands, and symmetry-corrected pose analysis need separate methods.
- This release measures heavy-atom contacts, not donor/acceptor-specific hydrogen-bond occupancy. Existing GROMACS hydrogen-bond analyses remain separate. Shared-basis cross-system PCA, structural clustering, contact lifetimes, pocket-local fitting, free-energy calculations, and 3D trajectory linking are not implemented here.
- The comparison catalog covers the most recent 200 tasks. JSON reports are bounded to 16 MB and NDX input to 20 MB.

## Validation And Security

Validated using controlled MDAnalysis trajectories with known rigid-body motion, ligand departure, multiple atom-pair contacts, and minimum-image contacts. Tests cover mutation/insertion mapping, chain mismatch, source-trajectory deduplication, equal-trajectory weighting, incompatible ligand definitions, invalid windows/index groups, path restrictions, and report-before-figure ordering.

An isolated worker processed three real frames from a completed historical trajectory through GROMACS 2026.3 and MDAnalysis: 287 protein residues and one 15-heavy-atom ligand, report output, figure generation, and intermediate cleanup. This short smoke test verifies execution, not convergence or a biological conclusion. Desktop/tablet/mobile/dark-mode Playwright checks cover all views, nonblank plots, residue filtering, comparisons, duplicate selection errors, index-group selection, and downloads.

Security-sensitive: yes. Reviewed without subagents against all ten security-review checklist categories. New requests forbid extra fields, bound selection strings/time/size/count values, validate source status, reject path traversal and symlinks, and use fixed executable/module/output names without a shell. Report rendering escapes text; SQL uses the existing parameterized store. No authentication, credential, XML, or remote data-fetch path was added. The deployment boundary remains a trusted local workstation.

Dependencies: MDAnalysis 2.10.0 and Biopython 1.87. The initial Biopython 1.86 installation was upgraded after audit flagged PYSEC-2026-1221. A repeated pip-audit check found no known vulnerabilities; the unpublished local project package cannot be audited against PyPI. Security review status: PASS within the existing local-service boundary.

The production task T-AK1/ACT was running during final verification, so the production service was not restarted. The separate UI preview uses clearly labeled synthetic fixtures and does not run production simulations. Actual WT/mutant assignments and biological conclusions require the user's sample identities and completed research reports.
