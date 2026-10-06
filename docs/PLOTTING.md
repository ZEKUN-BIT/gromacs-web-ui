# Publication plots

`scripts/md_plot.py` turns GROMACS XVG files and occupancy JSON files into a
high-resolution PNG and a vector PDF. Its defaults match the existing
`runtime/_analysis` figures: Arial typography, clean left/bottom axes,
the fixed blue/coral/mint/pink series palette, white background, tight layout, and
1600-DPI raster output.

Install the project dependencies once:

```bash
.venv/bin/python -m pip install -e .
```

New Protein MD and Protein–Ligand MD jobs run this automatically after their
analysis steps. Outputs are organized under the job directory as:

```text
figures/
├── structure/
├── interaction/
├── sampling/
├── quality/
└── manifest.json
```

To organize plots for an existing job manually, run:

```bash
.venv/bin/python scripts/md_plot.py auto runtime/jobs/JOB_ID
```

## Compare XVG series

```bash
.venv/bin/python scripts/md_plot.py line \
  'T-AK1=runtime/jobs/JOB_A/md_0_100-protein-ligand-hbonds.xvg' \
  'T-AK1/ACT=runtime/jobs/JOB_B/md_0_100-protein-ligand-hbonds.xvg' \
  --y-label 'Protein–ligand H-bonds' \
  --xlim 0 100 --ylim 0 11 \
  --output figures/hbonds
```

Use `--x-scale 0.001` when the XVG time column is in ps but the axis should be
in ns. `--smooth 50` adds a moving average; the default `--smooth 1` preserves
the raw data. Use `--figsize WIDTH HEIGHT` and `--dpi DPI` only when a journal
requires different dimensions.

## Occupancy heatmap

```bash
.venv/bin/python scripts/md_plot.py heatmap \
  'T-AK1=runtime/_analysis/occ_system_a.json' \
  'T-AK1/ACT=runtime/_analysis/occ_system_b.json' \
  --output figures/occupancy
```

The JSON may contain an `occ` mapping with tuple-like keys such as
`("A", 426, "LYS", "O2")`, or a flat mapping with `residue|atom` keys.

## Free-energy landscape

For a two-column PC1/PC2 file:

```bash
.venv/bin/python scripts/md_plot.py fel projection.xvg \
  --temperature 300 --bins 80 --max-energy 12 \
  --output figures/fel
```

For a projection containing `time PC1 PC2`, add `--x-column 1 --y-column 2`.
Run the script or any subcommand with `--help` for the complete option list.
