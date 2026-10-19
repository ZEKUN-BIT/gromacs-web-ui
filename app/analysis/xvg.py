from __future__ import annotations

import re
from pathlib import Path

from ..files import ensure_under_root
from .xvg_stream import MAX_XVG_BYTES, XvgUnavailable, load_xvg

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
    max_bytes: int = MAX_XVG_BYTES,
    *,
    replica: int | None = None,
    metric: str | None = None,
    begin_ns: float | None = None,
    end_ns: float | None = None,
) -> dict:
    """Return bounded, peak-preserving charts with statistics from all matching rows."""
    workdir = workdir.resolve()
    max_points = max(2, min(max_points, 10_000))
    max_bytes = min(max_bytes, MAX_XVG_BYTES)
    plots: list[dict] = []
    skipped_files: list[dict] = []
    all_paths = sorted(workdir.rglob("*.xvg"))
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
    files_seen = 0
    truncated = False
    for path in all_paths:
        metadata = selected.get(path.relative_to(workdir).as_posix())
        if metadata is None:
            continue
        if files_seen >= max_files or len(plots) >= max_plots:
            code = "file_limit" if files_seen >= max_files else "plot_limit"
            skipped_files.append({**metadata, "code": code, "reason": "达到本次展示数量上限；请选择具体指标或副本。"})
            truncated = True
            continue
        files_seen += 1
        try:
            if path.is_symlink():
                raise XvgUnavailable("symlink", "为保护任务文件，符号链接不参与图表读取。")
            safe_path = ensure_under_root(path, workdir)
            data = load_xvg(safe_path, max_points, max_bytes, begin_ns, end_ns)
            source_title = _xvg_source_title(safe_path, data["title"])
            series_names = [data["legends"].get(index, f"Series {index + 1}") for index in range(len(data["points"]))]
            if series_names == ["Series 1"] and "rmsd" in safe_path.stem.lower():
                series_names = ["RMSD"]
            for index, (series_name, points) in enumerate(zip(series_names, data["points"])):
                if len(plots) >= max_plots:
                    skipped_files.append({**metadata, "code": "plot_limit", "reason": "此文件的部分序列达到展示数量上限；请下载完整 XVG。"})
                    truncated = True
                    break
                split = len(series_names) > 1
                plots.append(
                    {
                        **metadata,
                        "category": _xvg_category(safe_path),
                        "title": (f"{source_title} — {series_name}" if split else source_title)[:160],
                        "x_label": data["x_label"],
                        "y_label": (_xvg_series_label(series_name, data["y_label"]) if split else data["y_label"])[:120],
                        "series": [series_name],
                        "points": points,
                        "sample_count": data["sample_count"],
                        "original_count": data["original_count"],
                        "time_unit": data["time_unit"],
                        "mean": data["means"][index],
                        "last": data["last"][index],
                        "sampled_points": len(points),
                        "sampling": ("minmax" if max_points >= 4 else "endpoints") if len(points) < data["sample_count"] else "all",
                    }
                )
        except XvgUnavailable as exc:
            skipped_files.append({**metadata, "code": exc.code, "reason": exc.reason})
            if exc.code not in {"insufficient_points", "non_time_axis"}:
                truncated = True
        except (OSError, ValueError):
            skipped_files.append({**metadata, "code": "unreadable", "reason": "无法安全读取此文件；请检查文件权限和格式。"})
            truncated = True
    return {
        "plots": plots,
        "truncated": truncated,
        "total": len(all_paths),
        "matching_files": len(selected),
        "metrics": sorted({item["metric"] for item in catalog}),
        "replicas": sorted({item["replica"] for item in catalog if item["replica"] is not None}),
        "skipped_files": skipped_files,
    }
