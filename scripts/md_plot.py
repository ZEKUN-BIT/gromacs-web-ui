#!/usr/bin/env python3
"""Create publication-style molecular-dynamics plots from XVG/JSON data.

The default theme uses Arial typography, clean axes, colorblind-safe colors,
and 1600-DPI PNG plus vector PDF output used by runtime/_analysis/fig*.  Run
``python scripts/md_plot.py --help`` for the available plot types.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import tempfile
from pathlib import Path

COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7"]
_DARK_COLORS = dict(zip(["#67899c", "#b58b72", "#829b8c", "#a18a9e"], COLORS))
_DARK_COLORS["#35a8d1"] = COLORS[0]
_TIME_TO_NS = {"ps": 0.001, "ns": 1.0, "us": 1000.0, "µs": 1000.0, "μs": 1000.0,
               "ms": 1e6, "s": 1e9}


def full_resolution_plot_points(workdir: Path, plot: dict, y_column: int | None = None) -> list[list[float]]:
    """Read original XVG points, before the web preview's extrema subsampling."""
    path = Path(workdir) / plot["path"]
    if y_column is None:
        label = (plot.get("series") or ["Series 1"])[0]
        legends = {}
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if match := re.match(r'^@\s+s(\d+)\s+legend\s+"([^"]*)"', line.strip()):
                    legends[int(match[1]) + 1] = match[2]
        matches = [column for column, name in legends.items() if name == label]
        if len(matches) > 1:
            raise ValueError(f"{path}: ambiguous series label {label!r}; specify y_column explicitly")
        generic = re.fullmatch(r"Series (\d+)", label)
        y_column = matches[0] if matches else int(generic[1]) if generic else 1
    xs, ys = read_xvg(path, y_column=y_column)
    return [list(point) for point in zip(xs, ys)]


def centered_trend(xs, ys, width: float, window_points: int | None = None):
    """Average complete centered windows; never pad endpoints or bridge gaps."""
    _, np = plotting_modules()
    xs, ys = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    finite = np.isfinite(xs) & np.isfinite(ys)
    steps = np.diff(xs)
    positive = steps[np.isfinite(steps) & (steps > 0)]
    if len(positive) == 0:
        return [], [], {"reason": "no increasing time samples"}
    dt = float(np.median(positive))
    # XVG decimal formatting can perturb adjacent times by ~0.1% of a step.
    # Estimate the cadence across the full span when there are no actual gaps.
    if np.all(finite) and np.allclose(steps, dt, rtol=0.002, atol=1e-12):
        dt = float((xs[-1] - xs[0]) / (len(xs) - 1))
    ratio = width / (2 * dt)
    half = int(round(ratio) if np.isclose(ratio, round(ratio), rtol=0.002) else np.floor(ratio))
    window = 2 * half + 1 if window_points is None else window_points
    if window < (3 if window_points is None else 2):
        return [], [], {"reason": "fewer than three samples in the smoothing window"}
    # Missing observations, reversed axes and unequal sampling start new segments.
    cuts = [0] + (np.flatnonzero(~np.isclose(steps, dt, rtol=0.002, atol=1e-12)
                                | ~finite[:-1] | ~finite[1:]) + 1).tolist() + [len(xs)]
    tx, ty = [], []
    for start, end in zip(cuts, cuts[1:]):
        if end - start < window or not np.all(finite[start:end]):
            continue
        values = ys[start:end]
        # Subtract a local origin to reduce cancellation for large energy values.
        origin = values[0]
        total = np.concatenate(([0.0], np.cumsum(values - origin)))
        means = (total[window:] - total[:-window]) / window + origin
        if tx:
            tx.append(float("nan"))
            ty.append(float("nan"))
        if window % 2:
            trim = window // 2
            tx.extend(xs[start + trim:end - trim].tolist())
        else:
            count = end - start - window + 1
            tx.extend(((xs[start:start + count] + xs[start + window - 1:end]) / 2).tolist())
        ty.extend(means.tolist())
    details = {"method": "centered moving average", "window_points": window,
               "window_axis_units": round((window - 1) * dt, 12), "sample_interval_axis_units": dt,
               "endpoint_policy": "complete windows only; no padding, extrapolation or gap bridging"}
    if not tx:
        details["reason"] = "no complete regular window"
    return tx, ty, details


def is_hbond_count_axis(ax, line=None) -> bool:
    """Identify hydrogen-bond counts without excluding distance or occupancy metrics."""
    if getattr(ax, "_md_metric_kind", None) == "hbond_count":
        return True
    description = ax.get_ylabel() + " " + (line.get_label() if line is not None else "")
    description = re.sub(r"[‐‑–—−]", "-", description.lower())
    hbond = re.search(r"\bh\s*-?\s*bonds?\b|\bhydrogen\s*-?\s*bonds?\b|氢键", description)
    continuous = re.search(r"distance|angle|occupancy|probability|fraction|lifetime|duration|correlation|距离|角度|占有|占据|概率|寿命|%", description)
    return bool(hbond and not continuous)


def apply_raw_trend_style(fig, window_ns: float = 1.0) -> list[dict]:
    """Apply the project's raw/trend style once, preserving all original observations."""
    from matplotlib.colors import to_hex, to_rgb

    remove_titles(fig)
    for legend in list(fig.legends):
        legend.remove()
    records = []
    for axis_index, ax in enumerate(fig.axes):
        if ax.get_legend() is not None:
            ax.get_legend().remove()
        if hasattr(ax, "_md_raw_trend_records"):
            records.extend(ax._md_raw_trend_records)
            continue
        unit = getattr(ax, "_md_time_unit", None)
        if unit is None:
            for sibling in ax.get_shared_x_axes().get_siblings(ax):
                label = sibling.get_xlabel().lower()
                match = re.search(r"time\s*[\[(]\s*(ps|ns|us|µs|μs|ms|s)\s*[\])]", label)
                if match:
                    unit = match[1]
                    break
        axis_records = []
        limits = ax.get_xlim(), ax.get_ylim()
        for line in list(ax.lines):
            if line.get_transform() != ax.transData or line.get_linestyle() in ("None", "none", "", " "):
                continue
            xs, ys = line.get_xdata(), line.get_ydata()
            if len(xs) < 3:
                continue
            dark = _DARK_COLORS.get(to_hex(line.get_color()).lower(), to_hex(line.get_color()))
            record = {"axis": axis_index, "series": line.get_label(), "raw_points": len(xs),
                      "dark_color": dark, "line_style": line.get_linestyle(), "smoothed": False}
            line.set_color(dark)
            hbond_count = is_hbond_count_axis(ax, line)
            if unit in _TIME_TO_NS and not getattr(line, "_md_skip_smoothing", False) and not hbond_count:
                _, np = plotting_modules()
                finite_x = np.asarray(xs)[np.isfinite(xs)]
                span = float(np.ptp(finite_x)) if len(finite_x) else 0
                factor = _TIME_TO_NS[unit]
                width = min(window_ns / factor, span * 0.05)
                width = getattr(ax, "_md_smooth_width", width)
                tx, ty, details = centered_trend(xs, ys, width, getattr(line, "_md_smooth_points", None))
                record.update(time_unit=unit, requested_window_ns=window_ns, smoothing=details)
                if tx:
                    pale = "#A6CEE3" if dark.lower() == "#0072b2" else to_hex([0.35 * c + 0.65 for c in to_rgb(dark)])
                    line.set(color=pale, linewidth=0.55, alpha=1.0, zorder=1)
                    line._md_raw_trend_role = "raw"
                    trend, = ax.plot(tx, ty, color=dark, linewidth=1.6, alpha=1.0,
                                     linestyle=line.get_linestyle(), label="_nolegend_", zorder=2)
                    trend._md_raw_trend_role = "trend"
                    record.update(smoothed=True, raw_color=pale,
                                  trend_points=int(np.count_nonzero(np.isfinite(ty))),
                                  window_ns=round(details["window_axis_units"] * factor, 12))
            else:
                record["reason"] = ("hydrogen-bond count; original curve only" if hbond_count else
                                    "non-time data or pre-aggregated/reference series; no smoothing")
            axis_records.append(record)
        ax.set_xlim(limits[0])
        ax.set_ylim(limits[1])
        ax._md_raw_trend_records = axis_records
        records.extend(axis_records)
    fig._md_raw_trend_records = records
    return records


def read_xvg(path: Path, x_column: int = 0, y_column: int = 1) -> tuple[list[float], list[float]]:
    """Read two numeric columns from an XVG file, ignoring Grace metadata."""
    xs: list[float] = []
    ys: list[float] = []
    needed = max(x_column, y_column)
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line[0] in "#@&":
            continue
        fields = line.split()
        if len(fields) <= needed:
            continue
        try:
            x_value, y_value = float(fields[x_column]), float(fields[y_column])
        except ValueError:
            continue
        if math.isfinite(x_value) and math.isfinite(y_value):
            xs.append(x_value)
            ys.append(y_value)
    if not xs:
        raise ValueError(f"{path}: no finite numeric data found (columns {x_column}, {y_column})")
    return xs, ys


def moving_average(values: list[float], window: int) -> list[float]:
    if window <= 1:
        return values
    if window > len(values):
        raise ValueError(f"smoothing window {window} exceeds the {len(values)} available points")
    result: list[float] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        divisor = min(index + 1, window)
        result.append(total / divisor)
    return result


def parse_dataset(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("dataset must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    if not label.strip() or not raw_path.strip():
        raise argparse.ArgumentTypeError("dataset must have a non-empty label and path")
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"data file does not exist: {path}")
    return label.strip(), path


def plotting_modules():
    cache_dir = Path(tempfile.gettempdir()) / "gromacs-web-ui-matplotlib"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exc:
        raise SystemExit(
            "Plotting dependencies are missing. Install them with: python -m pip install 'matplotlib==3.10.8' 'numpy>=2.0'"
        ) from exc
    return plt, np


def apply_theme(plt) -> None:
    plt.rcParams.update(
        {
            "font.family": "Arial",
            "font.size": 15,
            "axes.titlesize": 18,
            "axes.labelsize": 18,
            "axes.linewidth": 1.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.labelsize": 15,
            "ytick.labelsize": 15,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.major.size": 4,
            "ytick.major.size": 4,
            "xtick.major.width": 1.0,
            "ytick.major.width": 1.0,
            "legend.fontsize": 15,
            "legend.frameon": False,
            "legend.handlelength": 2.4,
            "lines.linewidth": 1.0,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.transparent": False,
            "savefig.facecolor": "white",
        }
    )


def remove_titles(fig) -> None:
    """Omit plot and panel titles while preserving axis labels and legends."""
    for ax in fig.axes:
        for location in ("left", "center", "right"):
            ax.set_title("", loc=location)
    fig.suptitle("")


def save_figure(fig, output: Path, dpi: int) -> None:
    apply_raw_trend_style(fig)
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    stem = output.with_suffix("") if output.suffix.lower() in {".png", ".pdf"} else output
    fig.savefig(stem.with_suffix(".png"), dpi=dpi, bbox_inches="tight", pad_inches=0.04)
    fig.savefig(stem.with_suffix(".pdf"), bbox_inches="tight", pad_inches=0.04)
    print(stem.with_suffix(".png"))
    print(stem.with_suffix(".pdf"))


def filename_slug(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in slug.split("-") if part)[:100] or "plot"


def generate_directory(
    workdir: Path,
    output_dir: str = "figures",
    dpi: int = 1600,
    sample_label: str | None = None,
) -> dict:
    """Render every plottable XVG in a job into categorized PNG/PDF files."""
    from app.analysis.xvg import xvg_plots

    plt, _ = plotting_modules()
    apply_theme(plt)
    workdir = workdir.resolve()
    target = (workdir / output_dir).resolve()
    if target != workdir and workdir not in target.parents:
        raise ValueError(f"output directory escapes job directory: {output_dir}")
    for category in ("structure", "interaction", "sampling", "quality"):
        category_dir = target / category
        category_dir.mkdir(parents=True, exist_ok=True)
        for old_figure in (*category_dir.glob("*.png"), *category_dir.glob("*.pdf")):
            old_figure.unlink()
    result = xvg_plots(workdir, max_files=100, max_plots=200, max_points=20_000, max_bytes=50_000_000)
    name_counts: dict[str, int] = {}
    source_columns: dict[str, int] = {}
    duplicate_labels = set()
    seen_labels = set()
    for plot in result["plots"]:
        key = (plot["path"], tuple(plot.get("series") or []))
        if key in seen_labels:
            duplicate_labels.add(key)
        seen_labels.add(key)
    generated: list[dict] = []
    for plot in result["plots"]:
        category = str(plot.get("category") or "quality")
        source_stem = Path(str(plot["path"])).stem
        base_name = filename_slug(source_stem)
        name_counts[base_name] = name_counts.get(base_name, 0) + 1
        if name_counts[base_name] > 1:
            series = "-".join(str(item) for item in plot.get("series") or [])
            base_name = f"{base_name}-{filename_slug(series)}"
        stem = target / category / base_name
        fig, ax = plt.subplots(figsize=(5.15, 3.8))
        if re.search(r"(?:^|[-_])(?:hbonds?|hbnum)$", source_stem.lower()):
            ax._md_metric_kind = "hbond_count"
        source_columns[plot["path"]] = source_columns.get(plot["path"], 0) + 1
        key = (plot["path"], tuple(plot.get("series") or []))
        column = source_columns[plot["path"]] if key in duplicate_labels else None
        points = full_resolution_plot_points(workdir, plot, y_column=column)
        # Repeated residue numbering marks separate blocks, not a continuous line.
        breaks = [0]
        if str(plot.get("x_label", "")).lower() == "residue":
            breaks.extend(i for i in range(1, len(points)) if points[i][0] < points[i - 1][0])
        breaks.append(len(points))
        for series_index, label in enumerate(plot.get("series") or ["Value"]):
            for block, (start, end) in enumerate(zip(breaks, breaks[1:])):
                ax.plot(
                    [row[0] for row in points[start:end]],
                    [row[series_index + 1] for row in points[start:end]],
                    color=COLORS[(series_index + block) % len(COLORS)],
                    linestyle="-",
                    label=f"Residue block {block + 1}" if len(breaks) > 2 else label,
                )
        ax.set_xlabel(str(plot.get("x_label") or "X"))
        ax.set_ylabel(str(plot.get("y_label") or "Value"))
        ax.tick_params(top=False, right=False)
        save_figure(fig, stem, dpi)
        plt.close(fig)
        generated.append(
            {
                "source": str(plot["path"]),
                "title": str(plot["title"]),
                "category": category,
                "png": stem.with_suffix(".png").relative_to(workdir).as_posix(),
                "pdf": stem.with_suffix(".pdf").relative_to(workdir).as_posix(),
                "sample_count": int(plot["sample_count"]),
                "show_legend": False,
                "raw_trend": getattr(fig, "_md_raw_trend_records", []),
            }
        )
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "style": f"publication-Arial-18-15-{dpi}dpi",
        "typography": {
            "font_family": "Arial",
            "axes_label_pt": 18,
            "tick_label_pt": 15,
            "legend_pt": 15,
        },
        "show_titles": False,
        "show_legends": False,
        "time_series_style": "pale full-resolution raw + dark centered moving average; 1 ns or at most 5% of short traces",
        "colors": COLORS,
        "dpi": dpi,
        "sample_label": sample_label,
        "source_xvg_count": int(result["total"]),
        "figure_count": len(generated),
        "truncated": bool(result["truncated"]),
        "figures": generated,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def finish_axes(ax, args) -> None:
    ax.set_xlabel(args.x_label)
    ax.set_ylabel(args.y_label)
    if args.xlim:
        ax.set_xlim(args.xlim)
    if args.ylim:
        ax.set_ylim(args.ylim)
    ax.tick_params(top=False, right=False)


def plot_lines(args) -> None:
    if args.smooth is not None and args.smooth < 1:
        raise SystemExit("--smooth must be a positive sample count")
    plt, _ = plotting_modules()
    apply_theme(plt)
    fig, ax = plt.subplots(figsize=args.figsize)
    for index, (label, path) in enumerate(args.datasets):
        xs, ys = read_xvg(path, args.x_column, args.y_column)
        xs = [value * args.x_scale for value in xs]
        ys = [value * args.y_scale for value in ys]
        line, = ax.plot(xs, ys, color=COLORS[index % len(COLORS)], label=label)
        if args.smooth is not None:
            line._md_smooth_points = args.smooth
    finish_axes(ax, args)
    save_figure(fig, args.output, args.dpi)
    plt.close(fig)


def read_occupancy(path: Path) -> tuple[list[str], dict[tuple[str, str], float]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = payload.get("occ", payload)
    values: dict[tuple[str, str], float] = {}
    for raw_key, raw_value in source.items():
        try:
            key = ast.literal_eval(raw_key) if raw_key.startswith("(") else raw_key.split("|")
            if len(key) >= 4:
                residue, atom = f"{key[2]}{key[1]}", str(key[3])
            elif len(key) == 2:
                residue, atom = str(key[0]), str(key[1])
            else:
                continue
            values[(residue, atom)] = float(raw_value)
        except (ValueError, SyntaxError, TypeError):
            continue
    atoms = [str(atom) for atom in payload.get("lig_names", [])]
    if not atoms:
        atoms = sorted({atom for _, atom in values})
    if not values:
        raise ValueError(f"{path}: no occupancy entries found")
    return atoms, values


def plot_heatmap(args) -> None:
    plt, np = plotting_modules()
    apply_theme(plt)
    parsed = [(label, *read_occupancy(path)) for label, path in args.datasets]
    residues = sorted({residue for _, _, values in parsed for residue, _ in values})
    atoms = list(dict.fromkeys(atom for _, atoms, _ in parsed for atom in atoms))
    fig, axes = plt.subplots(1, len(parsed), figsize=args.figsize, squeeze=False, sharey=True)
    image = None
    for ax, (label, _, values) in zip(axes[0], parsed, strict=True):
        matrix = np.array([[values.get((residue, atom), 0.0) for atom in atoms] for residue in residues])
        image = ax.imshow(matrix, vmin=args.vmin, vmax=args.vmax, cmap=args.cmap, aspect="auto", interpolation="nearest")
        ax.set_xticks(range(len(atoms)), atoms, rotation=90)
        ax.set_yticks(range(len(residues)), residues)
        ax.set_xlabel(args.x_label)
        ax.tick_params(length=0)
    axes[0][0].set_ylabel(args.y_label)
    if image is not None:
        colorbar = fig.colorbar(image, ax=axes.ravel().tolist(), fraction=0.035, pad=0.03)
        colorbar.set_label(args.colorbar_label)
    save_figure(fig, args.output, args.dpi)
    plt.close(fig)


def plot_fel(args) -> None:
    plt, np = plotting_modules()
    apply_theme(plt)
    xs, ys = read_xvg(args.input, args.x_column, args.y_column)
    histogram, x_edges, y_edges = np.histogram2d(xs, ys, bins=args.bins, density=True)
    probability = histogram / histogram.max()
    with np.errstate(divide="ignore"):
        energy = -0.008314462618 * args.temperature * np.log(probability)
    energy[~np.isfinite(energy)] = np.nan
    energy = np.clip(energy, 0, args.max_energy)
    x_centers = (x_edges[:-1] + x_edges[1:]) / 2
    y_centers = (y_edges[:-1] + y_edges[1:]) / 2
    fig, ax = plt.subplots(figsize=args.figsize)
    levels = np.linspace(0, args.max_energy, args.levels)
    contour = ax.contourf(x_centers, y_centers, energy.T, levels=levels, cmap=args.cmap)
    colorbar = fig.colorbar(contour, ax=ax, pad=0.04)
    colorbar.set_label("Free energy (kJ/mol)")
    ax.set_xlabel(args.x_label)
    ax.set_ylabel(args.y_label)
    save_figure(fig, args.output, args.dpi)
    plt.close(fig)


def plot_auto(args) -> None:
    manifest = generate_directory(args.workdir, args.output_dir, args.dpi, args.label)
    print(args.workdir / args.output_dir / "manifest.json")
    print(f"Generated {manifest['figure_count']} figures from {manifest['source_xvg_count']} XVG files")


def add_output_options(parser: argparse.ArgumentParser, figsize: tuple[float, float]) -> None:
    parser.add_argument("-o", "--output", type=Path, required=True, help="output stem; both PNG and PDF are written")
    parser.add_argument("--dpi", type=int, default=1600, help="PNG resolution (default: 1600)")
    parser.add_argument("--figsize", type=float, nargs=2, default=figsize, metavar=("WIDTH", "HEIGHT"), help="figure size in inches")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    line = subparsers.add_parser("line", help="draw one or more XVG time series")
    line.add_argument("datasets", type=parse_dataset, nargs="+", metavar="LABEL=PATH")
    line.add_argument("--x-column", type=int, default=0)
    line.add_argument("--y-column", type=int, default=1)
    line.add_argument("--x-scale", type=float, default=1.0)
    line.add_argument("--y-scale", type=float, default=1.0)
    line.add_argument("--smooth", type=int, default=None, metavar="WINDOW", help="optional window in samples; raw curve is always retained")
    line.add_argument("--x-label", default="Time (ns)")
    line.add_argument("--y-label", required=True)
    line.add_argument("--title", help="accepted for compatibility; plot titles are omitted")
    line.add_argument("--xlim", type=float, nargs=2, metavar=("MIN", "MAX"))
    line.add_argument("--ylim", type=float, nargs=2, metavar=("MIN", "MAX"))
    line.add_argument("--legend", default="upper center")
    line.add_argument("--legend-columns", type=int, default=2)
    line.add_argument("--no-legend", action="store_true")
    add_output_options(line, (5.15, 3.8))
    line.set_defaults(function=plot_lines)

    heatmap = subparsers.add_parser("heatmap", help="draw occupancy JSON files as side-by-side heatmaps")
    heatmap.add_argument("datasets", type=parse_dataset, nargs="+", metavar="LABEL=PATH")
    heatmap.add_argument("--x-label", default="Ligand heavy atoms")
    heatmap.add_argument("--y-label", default="Pocket residues")
    heatmap.add_argument("--colorbar-label", default="Contact occupancy")
    heatmap.add_argument("--vmin", type=float, default=0.0)
    heatmap.add_argument("--vmax", type=float, default=1.0)
    heatmap.add_argument("--cmap", default="YlGnBu")
    add_output_options(heatmap, (6.1, 5.6))
    heatmap.set_defaults(function=plot_heatmap)

    fel = subparsers.add_parser("fel", help="draw a free-energy landscape from PC1/PC2 columns")
    fel.add_argument("input", type=Path)
    fel.add_argument("--x-column", type=int, default=0)
    fel.add_argument("--y-column", type=int, default=1)
    fel.add_argument("--x-label", default="PC1 (nm)")
    fel.add_argument("--y-label", default="PC2 (nm)")
    fel.add_argument("--title", help="accepted for compatibility; plot titles are omitted")
    fel.add_argument("--temperature", type=float, default=300.0)
    fel.add_argument("--bins", type=int, default=80)
    fel.add_argument("--levels", type=int, default=25)
    fel.add_argument("--max-energy", type=float, default=12.0)
    fel.add_argument("--cmap", default="viridis")
    add_output_options(fel, (4.6, 4.0))
    fel.set_defaults(function=plot_fel)

    auto = subparsers.add_parser("auto", help="render all XVG files in a job into categorized folders")
    auto.add_argument("workdir", type=Path)
    auto.add_argument("--output-dir", default="figures")
    auto.add_argument("--dpi", type=int, default=1600)
    auto.add_argument("--label", help="sample label recorded in the manifest, without a plot title")
    auto.set_defaults(function=plot_auto)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if getattr(args, "input", None) and not args.input.is_file():
        raise SystemExit(f"input file does not exist: {args.input}")
    args.function(args)


if __name__ == "__main__":
    main()
