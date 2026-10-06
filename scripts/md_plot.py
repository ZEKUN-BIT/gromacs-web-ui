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
import tempfile
from pathlib import Path

COLORS = ["#67899C", "#B58B72", "#829B8C", "#A18A9E"]


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
            "font.size": 12,
            "axes.titlesize": 14,
            "axes.labelsize": 14,
            "axes.linewidth": 1.0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.major.size": 4,
            "ytick.major.size": 4,
            "xtick.major.width": 1.0,
            "ytick.major.width": 1.0,
            "legend.fontsize": 13,
            "legend.frameon": False,
            "legend.handlelength": 2.4,
            "lines.linewidth": 1.0,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.transparent": False,
            "savefig.facecolor": "white",
        }
    )


def save_figure(fig, output: Path, dpi: int) -> None:
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
        points = plot["points"]
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
        show_legend = len(plot.get("series") or []) > 1 or len(breaks) > 2
        if sample_label:
            ax.set_title(sample_label, loc="left", pad=34 if show_legend else 8)
        ax.tick_params(top=False, right=False)
        if show_legend:
            ax.legend(
                loc="lower right",
                bbox_to_anchor=(1, 1.02),
                borderaxespad=0,
                ncol=2,
            )
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
            }
        )
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "style": f"publication-Arial-muted-large-{dpi}dpi",
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
    if args.title:
        ax.set_title(args.title, fontsize=11, pad=8)
    if args.xlim:
        ax.set_xlim(args.xlim)
    if args.ylim:
        ax.set_ylim(args.ylim)
    ax.tick_params(top=False, right=False)


def plot_lines(args) -> None:
    plt, _ = plotting_modules()
    apply_theme(plt)
    fig, ax = plt.subplots(figsize=args.figsize)
    for index, (label, path) in enumerate(args.datasets):
        xs, ys = read_xvg(path, args.x_column, args.y_column)
        xs = [value * args.x_scale for value in xs]
        ys = [value * args.y_scale for value in ys]
        ax.plot(xs, moving_average(ys, args.smooth), color=COLORS[index % len(COLORS)], label=label)
    finish_axes(ax, args)
    if not args.no_legend:
        ax.legend(loc=args.legend, ncol=min(len(args.datasets), args.legend_columns), handlelength=2.0)
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
        ax.set_title(label, fontsize=11, pad=8)
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
    if args.title:
        ax.set_title(args.title, fontsize=11, pad=8)
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
    line.add_argument("--smooth", type=int, default=1, metavar="WINDOW")
    line.add_argument("--x-label", default="Time (ns)")
    line.add_argument("--y-label", required=True)
    line.add_argument("--title")
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
    fel.add_argument("--title")
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
    auto.add_argument("--label", help="sample label shown above every generated plot")
    auto.set_defaults(function=plot_auto)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if getattr(args, "input", None) and not args.input.is_file():
        raise SystemExit(f"input file does not exist: {args.input}")
    args.function(args)


if __name__ == "__main__":
    main()
