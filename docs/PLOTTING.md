# Publication plots

`scripts/md_plot.py` turns GROMACS XVG files and occupancy JSON files into a
high-resolution PNG and a vector PDF. Its defaults use Arial typography with
18 pt axis labels, 15 pt ticks, clean left/bottom axes,
a blue/orange/green/purple series palette, white background, tight layout, and
1600-DPI raster output. Exports omit plot, panel and overall titles and legends;
axis labels, colorbars and data annotations remain. Sample names, series colors
and metric descriptions stay in filenames and manifests. The legacy `--title` argument is accepted for command
compatibility but does not draw a title; `--label` records a sample name only.

The figure manifest records these font sizes. Historical exports need to be
regenerated to adopt a changed theme; PNG resolution does not change the font
sizes in points. Compare figures at their intended physical width, since scaling
a large multi-panel page down also scales its text.

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

Time series show every original observation as a thin pale line, with a dark
1 ns centered moving-average overlay (0.55 pt raw / 1.6 pt trend). A 10 ps
sampling interval gives 101 points per window and the overlay runs from
0.5 to 99.5 ns on a 100 ns trace. Only complete windows are shown: no endpoint
padding, extrapolation or smoothing across missing time intervals. Decimal
rounding of XVG timestamps is tolerated within 0.2% of the sampling interval.
The overlay is a visual guide, not a replacement for raw analysis data.

For short equilibration traces the window is capped at 5% of the observed
duration. A window must contain at least three observations. Hydrogen-bond count
plots (`*-hbonds.xvg`, including ASP/ATP and combined counts) show only the
original curve, without a trend overlay. This exception does not apply to
continuous hydrogen-bond distances or angles. Sparse traces,
RMSF residue profiles, heatmaps, occupancy bars and other non-time data are not
smoothed. Their original scientific encodings and colorbars are preserved.
The manifest's `raw_trend` records report each series and the actual window.

Use `--x-scale 0.001` when the XVG time column is in ps but the axis should be
in ns. Optional `--smooth WINDOW` overrides the window in sample points;
`--smooth 1` disables the overlay. Raw data are always retained. The legacy
legend options remain accepted for command compatibility but exports omit legends.
Use `--figsize WIDTH HEIGHT` and `--dpi DPI` to set export dimensions.

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
