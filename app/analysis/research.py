"""Bounded trajectory analysis, executed as a cancellable worker subprocess."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import MDAnalysis as mda
import numpy as np
from MDAnalysis.analysis import align, rms
from MDAnalysis.lib.distances import capped_distance
from scipy.sparse.linalg import svds
from threadpoolctl import threadpool_limits

from ..files import ensure_under_root
from ..models import ExistingAnalysisRequest


class MissingIndexGroup(ValueError):
    pass


def read_index_group(path: Path, name: str, atom_count: int) -> np.ndarray:
    if path.stat().st_size > 20_000_000:
        raise ValueError("索引文件超过研究分析的 20 MB 上限。")
    selected = False
    found = False
    indices = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split(";", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            selected = line[1:-1].strip() == name
            if selected and found:
                raise ValueError("索引文件包含重名原子组。")
            found |= selected
        elif selected and line:
            indices.extend(int(item) - 1 for item in line.split())
    if not found:
        raise MissingIndexGroup(f"索引文件中没有原子组 {name}。")
    if not indices or min(indices) < 0 or max(indices) >= atom_count or len(indices) != len(set(indices)):
        raise ValueError(f"原子组 {name} 不存在、为空或索引无效。")
    return np.asarray(indices, dtype=int)


def series_summary(values) -> dict:
    values = np.asarray(values, dtype=float)
    quarter = max(1, len(values) // 4)
    blocks = np.array_split(values, min(5, len(values) // 5)) if len(values) >= 10 else []
    return {
        "mean": float(values.mean()),
        "p05": float(np.quantile(values, 0.05)),
        "p95": float(np.quantile(values, 0.95)),
        "samples": len(values),
        "late_minus_early": float(values[-quarter:].mean() - values[:quarter].mean()),
        "block_means": [float(block.mean()) for block in blocks],
    }


def _atom_group(universe, name: str, index_file: Path | None):
    builtins = {"Backbone": "protein and backbone", "C-alpha": "protein and name CA", "Protein": "protein"}
    if index_file is not None:
        try:
            return universe.atoms[read_index_group(index_file, name, len(universe.atoms))]
        except MissingIndexGroup:
            if name not in builtins:
                raise
    if name not in builtins:
        raise ValueError(f"拟合组 {name} 需要有效索引文件。")
    return universe.select_atoms(builtins[name])


def analyze_universe(universe, request: ExistingAnalysisRequest, index_file: Path | None = None) -> dict:
    ca = universe.select_atoms("protein and name CA")
    protein = universe.select_atoms("protein and not (name H* or name [0-9]H*)")
    fit = _atom_group(universe, request.fit_group, index_file)
    frames = range(0, len(universe.trajectory), request.research_stride)
    if not 3 <= len(frames) <= 5000 or not 3 <= len(ca) <= 3000 or len(frames) * len(ca) > 2_000_000:
        raise ValueError("研究分析需要至少 3 帧和 3 个 C-alpha；上限为 5000 帧、3000 残基、200 万帧×残基。请调整取帧间隔或时间范围。")
    if len(fit) < 3 or len(set(ca.resindices)) != len(ca):
        raise ValueError("拟合至少需要 3 个原子，且每个蛋白残基必须只有一个 C-alpha。")
    if not np.isin(fit.indices, universe.select_atoms("protein").indices).all():
        raise ValueError("研究分析的拟合组只能包含蛋白原子。")
    universe.trajectory[0]
    reference_fit = fit.positions.astype(float).copy()
    reference_center = reference_fit.mean(axis=0)
    reference_fit -= reference_center
    if np.linalg.matrix_rank(reference_fit) < 2:
        raise ValueError("拟合原子不能共线。")
    reference_ca = ca.positions.astype(float).copy()
    ligand = None
    reference_ligand = None
    if request.research_ligand_group:
        if index_file is None:
            raise ValueError("配体分析缺少索引文件。")
        ligand = universe.atoms[read_index_group(index_file, request.research_ligand_group, len(universe.atoms))]
        ligand = ligand.select_atoms("not (name H* or name [0-9]H*)")
        if not 3 <= len(ligand) <= 256 or len(set(ligand.resindices)) != 1 or np.intersect1d(ligand.indices, protein.indices).size:
            raise ValueError("配体组必须包含单个非蛋白配体残基的 3–256 个重原子。多个配体需分别选择原子组分析。")
        reference_ligand = ligand.positions.astype(float).copy()
    coordinates = np.empty((len(frames), len(ca), 3), dtype=np.float32)
    times, ca_rmsd, ca_rg = [], [], []
    ligand_rmsd, ligand_self, retained, contact_count = [], [], [], []
    contacts = np.zeros((len(frames), len(ca)), dtype=bool) if ligand is not None else None
    residue_lookup = {int(index): position for position, index in enumerate(ca.resindices)}
    initial_contacts = None
    for sample, frame in enumerate(frames):
        ts = universe.trajectory[frame]
        if not np.isfinite(ts.positions).all() or not np.isfinite(ts.time):
            raise ValueError("轨迹包含非有限坐标或时间。")
        center = fit.positions.mean(axis=0)
        rotation, _ = align.rotation_matrix(fit.positions - center, reference_fit)
        xyz = (ca.positions - center) @ rotation.T + reference_center
        coordinates[sample] = xyz / 10
        times.append(float(ts.time) / 1000)
        ca_rmsd.append(float(np.sqrt(np.mean(np.sum((xyz - reference_ca) ** 2, axis=1)))) / 10)
        ca_rg.append(float(np.sqrt(np.mean(np.sum((xyz - xyz.mean(axis=0)) ** 2, axis=1)))) / 10)
        if ligand is not None:
            if ts.dimensions is None or not np.isfinite(ts.dimensions).all() or np.any(ts.dimensions[:3] <= 0):
                raise ValueError("配体接触分析需要有效的周期性盒子。")
            ligand_xyz = (ligand.positions - center) @ rotation.T + reference_center
            ligand_rmsd.append(float(np.sqrt(np.mean(np.sum((ligand_xyz - reference_ligand) ** 2, axis=1)))) / 10)
            ligand_self.append(float(rms.rmsd(ligand.positions, reference_ligand, center=True, superposition=True)) / 10)
            pairs = capped_distance(
                protein.positions, ligand.positions, max_cutoff=request.contact_cutoff_nm * 10, box=ts.dimensions, return_distances=False
            )
            for residue in np.unique(protein.resindices[pairs[:, 0]]) if len(pairs) else []:
                if int(residue) in residue_lookup:
                    contacts[sample, residue_lookup[int(residue)]] = True
            if initial_contacts is None:
                initial_contacts = contacts[sample].copy()
            contact_count.append(int(contacts[sample].sum()))
            retained.append(float(contacts[sample, initial_contacts].mean()) if initial_contacts.any() else None)
    if np.any(np.diff(times) <= 0):
        raise ValueError("轨迹时间必须严格递增，不能拼接重复时间段。")
    if not np.isfinite(coordinates).all() or not np.isfinite(ca_rmsd + ca_rg + ligand_rmsd + ligand_self).all():
        raise ValueError("拟合结果包含非有限数值，请检查原子组。")
    mean_coordinates = coordinates.mean(axis=0)
    rmsf = np.sqrt(np.mean(np.sum((coordinates - mean_coordinates) ** 2, axis=2), axis=0))
    segment = max(1, len(frames) // 5)
    displacement = np.linalg.norm(coordinates[-segment:].mean(axis=0) - coordinates[:segment].mean(axis=0), axis=1)
    centered = (coordinates - mean_coordinates).reshape(len(frames), -1).astype(float)
    variance = float(np.sum(centered**2))
    if variance / centered.size > 1e-12:
        u, singular, _ = svds(centered, k=2, which="LM", random_state=0)
        order = np.argsort(singular)[::-1]
        projection = u[:, order] * singular[order]
        explained = (singular[order] ** 2 / variance).tolist()
    else:
        projection = np.zeros((len(frames), 2))
        explained = [0.0, 0.0]
    residues = []
    for position, atom in enumerate(ca):
        chain = str(atom.chainID).strip() if hasattr(atom, "chainID") else ""
        residues.append(
            {
                "index": position,
                "chain": chain or "_",
                "resid": int(atom.resid),
                "icode": str(atom.icode) if hasattr(atom, "icode") else "",
                "resname": str(atom.resname),
                "rmsf_nm": float(rmsf[position]),
                "displacement_nm": float(displacement[position]),
                "contact_occupancy": float(contacts[:, position].mean()) if contacts is not None else None,
            }
        )
    summaries = {"ca_rmsd_nm": series_summary(ca_rmsd), "ca_rg_nm": series_summary(ca_rg)}
    binding = None
    if ligand is not None:
        summaries.update(
            ligand_rmsd_nm=series_summary(ligand_rmsd),
            ligand_self_rmsd_nm=series_summary(ligand_self),
            contact_residues=series_summary(contact_count),
        )
        if initial_contacts.any():
            summaries["pocket_retention"] = series_summary(retained)
        binding = {
            "group": request.research_ligand_group,
            "resname": str(ligand.resnames[0]),
            "atom_names": sorted(str(name) for name in ligand.names),
            "heavy_atoms": len(ligand),
            "cutoff_nm": request.contact_cutoff_nm,
            "initial_contact_residues": int(initial_contacts.sum()),
            "contact_frame_fraction": float(np.mean(np.asarray(contact_count) > 0)),
        }
    warnings = []
    if len(frames) < 50:
        warnings.append("取样少于 50 帧，分布和占有率估计可能不稳定。")
    if not np.allclose(np.diff(times), np.diff(times)[0], rtol=1e-3, atol=1e-8):
        warnings.append("采样时间不均匀，占有率为帧比例，不代表驻留时间比例。")
    if binding and not initial_contacts.any():
        warnings.append("参考帧没有阈值内接触，初始口袋保持率不可计算。")
    return {
        "schema_version": 1,
        "frames": len(frames),
        "available_frames": len(universe.trajectory),
        "window_ns": [times[0], times[-1]],
        "stride": request.research_stride,
        "fit_group": request.fit_group,
        "reference": "first analyzed frame; protein fit",
        "residues": residues,
        "summary": summaries,
        "binding": binding,
        "warnings": warnings,
        "timeseries": {
            "time_ns": times,
            "ca_rmsd_nm": ca_rmsd,
            "ca_rg_nm": ca_rg,
            "ligand_rmsd_nm": ligand_rmsd,
            "ligand_self_rmsd_nm": ligand_self,
            "contact_residues": contact_count,
            "pocket_retention": retained,
        },
        "pca": {
            "basis": "this trajectory only; protein-fit C-alpha Cartesian coordinates",
            "explained_variance": explained,
            "projection_nm": projection.tolist(),
        },
        "methods": {
            "occupancy": "fraction of sampled frames with any protein-residue/ligand heavy-atom pair within cutoff; minimum-image PBC",
            "rmsf": "C-alpha RMS fluctuation about mean after protein fit",
            "displacement": "distance between mean C-alpha positions in first and last 20% of sampled frames",
            "statistics": "one trajectory is one sampling unit; frame quantiles and block means are descriptive, not confidence intervals",
        },
    }


def main() -> None:
    workdir = Path.cwd()
    meta = json.loads((workdir / "metadata.json").read_text(encoding="utf-8"))
    params = meta["params"]
    allowed = ExistingAnalysisRequest.model_fields
    request = ExistingAnalysisRequest.model_validate({key: value for key, value in params.items() if key in allowed})
    topology = ensure_under_root(workdir / "analysis-reference.pdb", workdir)
    trajectory = ensure_under_root(workdir / "analysis-whole.xtc", workdir)
    index = ensure_under_root(workdir / request.index_file, workdir) if request.index_file else None
    universe = mda.Universe(str(topology), str(trajectory))
    with threadpool_limits(limits=max(1, min(int(params.get("ntomp") or 2), 16))):
        report = analyze_universe(universe, request, index)
    report.update(
        job={"id": meta["id"], "name": meta["name"]},
        source=meta.get("analysis_source"),
        input_hashes=meta.get("input_hashes", []),
        software={"MDAnalysis": mda.__version__, "numpy": np.__version__},
    )
    target = ensure_under_root(workdir / "research-report.json", workdir)
    temporary = ensure_under_root(workdir / "research-report.pending", workdir)
    temporary.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(target)
    with ensure_under_root(workdir / "research-residues.tsv", workdir).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(report["residues"][0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(report["residues"])
    with ensure_under_root(workdir / "research-timeseries.xvg", workdir).open("w", encoding="utf-8") as handle:
        handle.write('@ title "Protein-fit C-alpha RMSD"\n@ xaxis label "Time (ns)"\n@ yaxis label "RMSD (nm)"\n')
        for time, value in zip(report["timeseries"]["time_ns"], report["timeseries"]["ca_rmsd_nm"], strict=True):
            handle.write(f"{time:.8g} {value:.8g}\n")
    print(f"Research report written: {report['frames']} frames, {len(report['residues'])} residues", flush=True)


if __name__ == "__main__":
    main()
