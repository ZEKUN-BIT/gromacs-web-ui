from __future__ import annotations

import json
from collections import defaultdict
from statistics import fmean, stdev

from Bio.Align import PairwiseAligner
from Bio.SeqUtils import seq1

from ..files import ensure_under_root
from ..job_store import JobStore
from ..models import ResearchComparisonRequest


def load_research_report(store: JobStore, job_id: str) -> dict:
    job = store.get(job_id)
    if job["status"] != "completed" or job.get("dry_run"):
        raise ValueError("研究结果尚未完成。")
    root = store.job_dir(job_id)
    path = root / "research-report.json"
    if path.is_symlink():
        raise ValueError("研究报告不能是符号链接。")
    path = ensure_under_root(path, root)
    if path.stat().st_size > 16_000_000:
        raise ValueError("研究报告超过 16 MB 读取上限。")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1 or not 1 <= len(report.get("residues", [])) <= 3000 or not report.get("summary"):
        raise ValueError("研究报告格式不受支持。")
    report["job"] = {"id": job_id, "name": job["name"]}
    return report


def align_residues(reference: list[dict], other: list[dict]) -> dict[int, int]:
    left, right = defaultdict(list), defaultdict(list)
    for i, residue in enumerate(reference):
        left[residue["chain"]].append((i, residue))
    for i, residue in enumerate(other):
        right[residue["chain"]].append((i, residue))
    if set(left) != set(right):
        raise ValueError("蛋白链标识不一致，未生成残基差值。")
    aligner = PairwiseAligner(mode="global", match_score=2, mismatch_score=-1, open_gap_score=-3, extend_gap_score=-0.5)
    custom = {
        "HID": "H",
        "HIE": "H",
        "HIP": "H",
        "HSD": "H",
        "HSE": "H",
        "HSP": "H",
        "CYX": "C",
        "CYM": "C",
        "LYN": "K",
        "ASH": "D",
        "GLH": "E",
    }
    mapping = {}
    for chain in left:
        a = "".join(seq1(item["resname"], custom_map=custom) for _, item in left[chain])
        b = "".join(seq1(item["resname"], custom_map=custom) for _, item in right[chain])
        if "X" in a or "X" in b:
            raise ValueError("存在无法识别的蛋白残基，未自动映射。")
        alignments = iter(aligner.align(a, b))
        alignment = next(alignments)
        alternative = next(alignments, None)
        if alternative is not None and alternative.indices.tolist() != alignment.indices.tolist():
            raise ValueError("序列存在多个同分映射，需核对残基对应关系。")
        pairs = [(int(i), int(j)) for i, j in alignment.indices.T if i >= 0 and j >= 0]
        identity = sum(a[i] == b[j] for i, j in pairs) / max(1, len(pairs))
        coverage = len(pairs) / max(len(a), len(b))
        if identity < 0.7 or coverage < 0.7:
            raise ValueError("链序列一致性或覆盖率低于 70%，未进行残基差值计算。")
        mapping.update({left[chain][i][0]: right[chain][j][0] for i, j in pairs})
    return mapping


def _replica_stats(values: list[float]) -> dict:
    return {"mean": fmean(values), "sd": stdev(values) if len(values) > 1 else None, "n": len(values), "values": values}


def compare_reports(reference: list[dict], comparison: list[dict]) -> dict:
    all_reports = reference + comparison
    identities = []
    for report in all_reports:
        hashes = {item["path"]: item["sha256"] for item in report.get("input_hashes", [])}
        identity = hashes.get("input.xtc") or hashes.get("input.trr")
        if not identity:
            raise ValueError("报告缺少轨迹校验值，无法排除重复输入。")
        identities.append(identity)
    if len(set(identities)) != len(identities):
        raise ValueError("选中的分析复用了同一轨迹，不能作为独立重复或放入两组比较。")
    warnings = [
        "单条轨迹是一个取样单位；组内多条轨迹时提供轨迹均值的 SD，不进行逐帧显著性检验。文件去重不等于独立性验证。",
        "RMSD 相对于各自的起始构象，均值差不代表两种蛋白之间的结构 RMSD；独立 PCA 的坐标不可直接比较。",
    ]
    if min(len(reference), len(comparison)) < 2:
        warnings.append("至少一组只有一个轨迹，本次为描述性对照，无法估计该组的副本间变异。")
    protocol_rows = []
    protocol_keys = ("force_field", "water_model", "ion_concentration", "ref_t", "ref_p", "dt", "tcoupl", "pcoupl", "constraints")
    for key in protocol_keys:
        values = [(item.get("source") or {}).get("protocol", {}).get(key) for item in all_reports]
        complete = all(value is not None and value != "" for value in values)
        same = complete and len({str(value) for value in values}) == 1
        protocol_rows.append({"key": key, "values": values, "status": "same" if same else "different" if complete else "unknown"})
    if any(row["status"] != "same" for row in protocol_rows):
        warnings.append("模拟条件存在差异或缺失，观察到的差异不能单独归因于突变；条件来自来源任务及 MDP 记录。")
    for key in ("window_ns", "stride", "fit_group"):
        if any(item.get(key) != all_reports[0].get(key) for item in all_reports[1:]):
            warnings.append(f"分析设置 {key} 不一致，请结合设置差异解释结果。")
    bindings = [item.get("binding") for item in all_reports]

    def binding_signature(item):
        return item.get("resname"), item.get("heavy_atoms"), tuple(item.get("atom_names", [])), item.get("cutoff_nm")

    binding_comparable = all(bindings) and len({binding_signature(item) for item in bindings if item}) == 1
    if not binding_comparable:
        warnings.append("配体缺失或配体标识、重原子、接触阈值不同，未计算配体指标与占有率差值。")
    shared_metrics = set.intersection(*(set(item["summary"]) for item in all_reports))
    if not binding_comparable:
        shared_metrics &= {"ca_rmsd_nm", "ca_rg_nm"}
    metrics = []
    for key in sorted(shared_metrics):
        a = _replica_stats([item["summary"][key]["mean"] for item in reference])
        b = _replica_stats([item["summary"][key]["mean"] for item in comparison])
        metrics.append({"metric": key, "reference": a, "comparison": b, "difference": b["mean"] - a["mean"]})
    canonical = reference[0]["residues"]
    residues = []
    try:
        maps = [align_residues(canonical, report["residues"]) for report in all_reports]
        common = set.intersection(*(set(mapping) for mapping in maps))
        for i in sorted(common):
            mapped = [report["residues"][mapping[i]] for report, mapping in zip(all_reports, maps, strict=True)]
            entry = {
                "chain": canonical[i]["chain"],
                "resid": canonical[i]["resid"],
                "icode": canonical[i].get("icode", ""),
                "reference_names": sorted({item["resname"] for item in mapped[: len(reference)]}),
                "comparison_names": sorted({item["resname"] for item in mapped[len(reference) :]}),
            }
            for key in ("rmsf_nm", "displacement_nm", "contact_occupancy"):
                if key == "contact_occupancy" and not binding_comparable:
                    continue
                if any(item.get(key) is None for item in mapped):
                    continue
                a = fmean(item[key] for item in mapped[: len(reference)])
                b = fmean(item[key] for item in mapped[len(reference) :])
                entry[key] = {"reference": a, "comparison": b, "difference": b - a}
            residues.append(entry)
        if len(common) < len(canonical):
            warnings.append(f"残基差值覆盖参考结构的 {len(common)}/{len(canonical)} 个 C-alpha，缺口未插值。")
    except ValueError as exc:
        warnings.append(str(exc))
    return {
        "reference": [item["job"] for item in reference],
        "comparison": [item["job"] for item in comparison],
        "metrics": metrics,
        "residues": residues,
        "protocol": protocol_rows,
        "warnings": warnings,
        "difference_direction": "comparison minus reference",
        "pca_comparison": "not comparable: separate bases",
    }


def compare_research_jobs(store: JobStore, request: ResearchComparisonRequest) -> dict:
    ids = request.reference_jobs + request.comparison_jobs
    if len(set(ids)) != len(ids):
        raise ValueError("同一个任务不能重复选择或同时分到两组。")
    return compare_reports(
        [load_research_report(store, job) for job in request.reference_jobs],
        [load_research_report(store, job) for job in request.comparison_jobs],
    )
