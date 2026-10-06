from __future__ import annotations

import math
import re
from pathlib import Path
from statistics import fmean

from ..files import ensure_under_root

XVG_SERIES_UNITS = {
    "potential": "Potential energy (kJ/mol)",
    "kinetic-en.": "Kinetic energy (kJ/mol)",
    "total-energy": "Total energy (kJ/mol)",
    "temperature": "Temperature (K)",
    "pressure": "Pressure (bar)",
    "density": "Density (kg/m³)",
    "volume": "Volume (nm³)",
    "rmsd": "RMSD (nm)",
    "rmsf": "RMSF (nm)",
    "rg": "Radius of gyration (nm)",
    "contacts": "Contacts (count)",
    "hydrogen bonds": "Hydrogen bonds (count)",
}


def _xvg_series_label(legend: str, fallback: str) -> str:
    return XVG_SERIES_UNITS.get(legend.strip().lower(), fallback)


def _xvg_source_title(path: Path, title: str) -> str:
    stem = path.stem.lower()
    if "protein-ligand-mindist" in stem:
        return "Protein–ligand minimum distance"
    if "protein-ligand-contacts" in stem:
        return "Protein–ligand contacts (≤ 0.45 nm)"
    if "protein-ligand-hbonds" in stem:
        return "Protein–ligand hydrogen bonds"
    if "ligand-rmsd" in stem or ("ligand" in stem and stem.endswith("-rmsd")):
        return "Ligand RMSD relative to protein"
    if "protein-rmsf" in stem:
        return "Protein C-alpha RMSF"
    if "protein-rg" in stem:
        return "Protein radius of gyration"
    if "protein-sasa" in stem:
        return "Protein solvent-accessible surface area"
    if "backbone-rmsd" in stem:
        return "Backbone RMSD"
    if "npt-free-thermodynamics" in stem:
        return "Unrestrained NPT"
    if "observables" in stem:
        return "Production MD"
    return title


def _xvg_category(path: Path) -> str:
    stem = path.stem.lower()
    if any(token in stem for token in ("protein-ligand", "ligand-rmsd", "hbnum")) or (
        "ligand" in stem and any(stem.endswith(f"-{suffix}") for suffix in ("rmsd", "mindist", "contacts", "hbonds"))
    ):
        return "interaction"
    if any(token in stem for token in ("rmsd", "rmsf", "protein-rg", "gyrate", "sasa", "dssp")):
        return "structure"
    if any(token in stem for token in ("eigen", "pc1", "pc2", "sham")):
        return "sampling"
    return "quality"


def xvg_plots(
    workdir: Path,
    max_files: int = 12,
    max_plots: int = 24,
    max_points: int = 1200,
    max_bytes: int = 5_000_000,
    *,
    replica: int | None = None,
    metric: str | None = None,
    begin_ns: float | None = None,
    end_ns: float | None = None,
) -> dict:
    """Return bounded chart data for XVG analysis outputs in a job directory."""
    workdir = workdir.resolve()
    plots: list[dict] = []
    title_pattern = re.compile(r'^@\s+title\s+"([^"]+)"')
    axis_pattern = re.compile(r'^@\s+([xy])axis\s+label\s+"([^"]+)"')
    legend_pattern = re.compile(r'^@\s+s(\d+)\s+legend\s+"([^"]+)"')
    all_paths = sorted(workdir.rglob("*.xvg"))
    total = len(all_paths)
    replica_pattern = re.compile(r"_r(\d{2})(?=[-_.]|$)")
    catalog = []
    for path in all_paths:
        name = path.relative_to(workdir).as_posix()
        match = replica_pattern.search(name)
        catalog.append({"path": name, "replica": int(match.group(1)) if match else None, "metric": replica_pattern.sub("", name, count=1)})
    selected = {
        item["path"]: item
        for item in catalog
        if (replica is None or item["replica"] == replica) and (not metric or item["metric"] == metric)
    }
    matching_files = len(selected)
    files_seen = 0
    truncated = False
    for path in all_paths:
        metadata = selected.get(path.relative_to(workdir).as_posix())
        if metadata is None:
            continue
        if files_seen >= max_files or len(plots) >= max_plots:
            truncated = True
            break
        files_seen += 1
        try:
            if path.is_symlink() or path.stat().st_size > max_bytes:
                truncated = True
                continue
            safe_path = ensure_under_root(path, workdir)
            title = safe_path.stem.replace("_", " ").replace("-", " ")
            x_label = "X"
            y_label = "Value"
            legends: dict[int, str] = {}
            rows: list[list[float]] = []
            for raw_line in safe_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if match := title_pattern.match(line):
                    title = match.group(1)
                elif match := axis_pattern.match(line):
                    if match.group(1) == "x":
                        x_label = match.group(2)
                    else:
                        y_label = match.group(2)
                elif match := legend_pattern.match(line):
                    legends[int(match.group(1))] = match.group(2)
                elif line and line[0] not in "#@&":
                    try:
                        values = [float(value) for value in line.split()]
                    except ValueError:
                        continue
                    if len(values) >= 2 and all(math.isfinite(value) for value in values):
                        rows.append(values)
            if len(rows) < 2:
                continue
            original_count = len(rows)
            time_match = re.fullmatch(r"Time\s*\((ps|ns|us|ms|s)\)", x_label, re.IGNORECASE)
            time_unit = time_match.group(1).lower() if time_match else None
            if begin_ns is not None or end_ns is not None:
                if time_unit is None:
                    continue
                ns_factor = {"ps": 0.001, "ns": 1, "us": 1000, "ms": 1e6, "s": 1e9}[time_unit]
                rows = [
                    row
                    for row in rows
                    if (begin_ns is None or row[0] * ns_factor >= begin_ns) and (end_ns is None or row[0] * ns_factor <= end_ns)
                ]
                if len(rows) < 2:
                    continue
            width = min(min(len(row) for row in rows), 13)
            stem = safe_path.stem.lower()
            if any(token in stem for token in ("protein-rg", "protein-sasa", "protein-ligand-hbonds")):
                width = min(width, 2)
            if width < 2:
                continue
            stride = max(1, math.ceil(len(rows) / max_points))
            sampled = [row[:width] for row in rows[::stride]]
            if sampled[-1] != rows[-1][:width]:
                sampled.append(rows[-1][:width])
            path_name = safe_path.relative_to(workdir).as_posix()
            source_title = _xvg_source_title(safe_path, title)
            series_names = [legends.get(index, f"Series {index + 1}")[:120] for index in range(width - 1)]
            if width == 2 and series_names == ["Series 1"] and "rmsd" in safe_path.stem.lower():
                series_names = ["RMSD"]
            if width > 2:
                for series_index, series_name in enumerate(series_names):
                    if len(plots) >= max_plots:
                        truncated = True
                        break
                    series_points = [[row[0], row[series_index + 1]] for row in sampled]
                    values = [row[series_index + 1] for row in rows]
                    plots.append(
                        {
                            "path": path_name,
                            "category": _xvg_category(safe_path),
                            "title": f"{source_title} — {series_name}"[:160],
                            "x_label": x_label[:120],
                            "y_label": _xvg_series_label(series_name, y_label)[:120],
                            "series": [series_name],
                            "points": series_points,
                            "sample_count": len(rows),
                            "original_count": original_count,
                            "time_unit": time_unit,
                            "mean": fmean(values),
                            "last": values[-1],
                        }
                    )
            else:
                values = [row[1] for row in rows]
                plots.append(
                    {
                        "path": path_name,
                        "category": _xvg_category(safe_path),
                        "title": source_title[:160],
                        "x_label": x_label[:120],
                        "y_label": y_label[:120],
                        "series": series_names,
                        "points": sampled,
                        "sample_count": len(rows),
                        "original_count": original_count,
                        "time_unit": time_unit,
                        "mean": fmean(values),
                        "last": values[-1],
                    }
                )
        except (OSError, ValueError):
            continue
    for plot in plots:
        plot.update(selected[plot["path"]])
    return {
        "plots": plots,
        "truncated": truncated,
        "total": total,
        "matching_files": matching_files,
        "metrics": sorted({item["metric"] for item in catalog}),
        "replicas": sorted({item["replica"] for item in catalog if item["replica"] is not None}),
    }
