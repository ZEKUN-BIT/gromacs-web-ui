from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .files import ensure_under_root, relative_files
from .gromacs import Step, _read_mdp_values, build_steps
from .job_store import JobStore
from .models import ExistingAnalysisRequest
from .reproducibility import write_experiment_manifest
from .workflows.analysis import _analysis_window_args

ANALYSIS_DISK_HEADROOM = 2 * 1024**3
ANALYSIS_OUTPUT_RESERVE = 256 * 1024**2

INPUT_SUFFIXES = {
    "tpr_file": {".tpr"},
    "trajectory_file": {".xtc", ".trr"},
    "index_file": {".ndx"},
    "edr_file": {".edr"},
}


def analysis_source(store: JobStore, job_id: str) -> dict:
    source = store.get(job_id)
    if source["status"] != "completed" or source.get("dry_run"):
        raise ValueError("仅可使用已完成的正式任务结果创建分析。")
    return source


def source_file(root: Path, name: str, suffixes: set[str]) -> Path:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or path.suffix.lower() not in suffixes:
        raise ValueError("分析输入必须是任务目录内的相应类型文件。")
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("分析输入不能是符号链接。")
    candidate = ensure_under_root(candidate, root)
    if not candidate.is_file() or candidate.stat().st_size == 0:
        raise ValueError(f"分析输入不存在或为空：{name}")
    return candidate


def analysis_inputs(store: JobStore, job_id: str) -> dict:
    source = analysis_source(store, job_id)
    root = store.job_dir(job_id)
    listed = relative_files(root, max_files=1001)
    result = {key: [] for key in INPUT_SUFFIXES}
    for item in listed[:1000]:
        for key, suffixes in INPUT_SUFFIXES.items():
            if Path(item["path"]).suffix.lower() not in suffixes:
                continue
            try:
                source_file(root, item["path"], suffixes)
            except (OSError, ValueError):
                continue
            result[key].append(item)
    return {"source_job_id": job_id, "source_name": source["name"], "files": result, "truncated": len(listed) > 1000}


def analysis_disk_budget(selected: dict[str, Path], request: ExistingAnalysisRequest, extra_input_bytes: int = 0) -> dict:
    """Estimate staging and peak intermediates without allocating a disk cache."""
    copied = sum(path.stat().st_size for path in selected.values()) + extra_input_bytes
    # RMSD temporarily holds the existing prepared trajectory, nojump history,
    # and clustered RMSD output together; nojump is removed before fitting.
    # Reserve twice the source size for each peak concurrent intermediate
    # because unwrapped coordinates can compress less efficiently.
    trajectory = selected["trajectory_file"].stat().st_size
    derived = 0 if request.dry_run else 2 * trajectory * (3 if request.do_rmsd else 1)
    output = 0 if request.dry_run else ANALYSIS_OUTPUT_RESERVE * (4 if request.do_pca else 1)
    return {
        "copied_input_bytes": copied,
        "derived_trajectory_bytes": derived,
        "output_reserve_bytes": output,
        "headroom_bytes": ANALYSIS_DISK_HEADROOM,
        "required_free_bytes": copied + derived + output + ANALYSIS_DISK_HEADROOM,
        "method": "independent input copies plus estimated intermediate/output reserve; not a fixed-size allocation",
    }


def require_analysis_disk_space(destination: Path, budget: dict) -> None:
    free = shutil.disk_usage(destination).free
    if free < budget["required_free_bytes"]:
        raise ValueError(
            f"分析磁盘空间不足：当前可用 {free / 1024**3:.2f} GiB，预计需预留 {budget['required_free_bytes'] / 1024**3:.2f} GiB"
            "（独立输入副本、派生轨迹、分析输出和 2 GiB 余量）。请释放空间或选择更小的轨迹。"
        )


def create_existing_analysis(store: JobStore, job_id: str, request: ExistingAnalysisRequest, settings: dict) -> dict:
    source = analysis_source(store, job_id)
    if not any(getattr(request, key) for key in ("do_rmsd", "do_rg", "do_energy", "do_dssp", "do_hbond", "do_pca", "do_research")):
        raise ValueError("请至少选择一项分析指标。")
    if request.do_energy and not request.edr_file:
        raise ValueError("能量分析需要选择 EDR 文件。")
    root = store.job_dir(job_id)
    selected = {}
    for key, suffixes in INPUT_SUFFIXES.items():
        name = getattr(request, key)
        if key == "edr_file" and not request.do_energy:
            continue
        if name or key in {"tpr_file", "trajectory_file"}:
            selected[key] = source_file(root, name, suffixes)

    params = request.model_dump(exclude={"name", "dry_run"})
    params.update(workflow="analysis_suite", gmx_bin=settings["gmx_bin"], time_unit="ns", ntomp=settings.get("default_ntomp", 4))
    params["edr_file"] = ""
    mapping = []
    for key, path in selected.items():
        local_name = f"input{path.suffix.lower()}"
        params[key] = local_name
        mapping.append({"source": getattr(request, key), "path": local_name, "role": key})
    uploaded = [item["path"] for item in mapping]
    research_index = None
    if request.do_research and request.research_ligand_group:
        from .analysis.research import read_index_group

        index_source = selected["index_file"]
        protein_atoms = read_index_group(index_source, "Protein", sys.maxsize)
        ligand_atoms = read_index_group(index_source, request.research_ligand_group, sys.maxsize)
        if set(protein_atoms) & set(ligand_atoms):
            raise ValueError("配体索引组不能包含蛋白原子。")
        index_text = index_source.read_text(encoding="utf-8")
        group = "ResearchSolute"
        names = {line.split(";", 1)[0].strip()[1:-1].strip() for line in index_text.splitlines() if line.strip().startswith("[")}
        while group in names:
            group += "_"
        combined = sorted(set(protein_atoms) | set(ligand_atoms))
        rows = [" ".join(str(int(atom) + 1) for atom in combined[start : start + 15]) for start in range(0, len(combined), 15)]
        research_index = index_text + f"\n[ {group} ]\n" + "\n".join(rows) + "\n"
        params["index_file"] = "research-index.ndx"
        params["center_group"] = group
        uploaded.append("research-index.ndx")
    trajectory = params["trajectory_file"]
    params["trajectory_file"] = "analysis-whole.xtc"
    params["rmsd_trajectory_file"] = trajectory
    index_args = ["-n", params["index_file"]] if params["index_file"] else []
    steps = [
        Step(
            "Prepare whole-system trajectory for analysis",
            [
                params["gmx_bin"],
                "trjconv",
                "-s",
                params["tpr_file"],
                "-f",
                trajectory,
                "-o",
                "analysis-whole.xtc",
                "-pbc",
                "cluster",
                "-center",
                *index_args,
            ],
            stdin_text=f"{params['center_group']}\n{params['center_group']}\nSystem\n",
            outputs=["analysis-whole.xtc"],
        ),
        *build_steps(params, [*uploaded, "analysis-whole.xtc"], analysis_trajectory_prepared=True),
    ]
    window_args = _analysis_window_args(params, "ps")
    steps[0].args.extend(window_args)
    for step in steps[1:]:
        if len(step.args) > 1 and step.args[1] == "energy":
            step.args.extend(window_args)
    if request.do_research:
        figure_steps = [step for step in steps if step.operation == "generate_analysis_figures"]
        steps = [step for step in steps if step.operation != "generate_analysis_figures"]
        steps.extend(
            [
                Step(
                    "Extract research reference structure",
                    [
                        params["gmx_bin"],
                        "trjconv",
                        "-s",
                        params["tpr_file"],
                        "-f",
                        "analysis-whole.xtc",
                        "-o",
                        "analysis-reference.pdb",
                        "-dump",
                        "0",
                    ],
                    stdin_text="System\n",
                    outputs=["analysis-reference.pdb"],
                ),
                Step(
                    "Analyze conformation and residue contacts",
                    [sys.executable, "-m", "app.analysis.research"],
                    outputs=["research-report.json", "research-residues.tsv", "research-timeseries.xvg"],
                ),
            ]
        )
        steps.extend(figure_steps)
    intermediates = ["analysis-whole.xtc"]
    steps.append(
        Step(
            "Remove intermediate analysis trajectories",
            ["internal", "remove_files"],
            kind="internal",
            operation="remove_files",
            data={"paths": intermediates},
        )
    )
    name = request.name.strip() or f"{source['name']} analysis"
    disk_budget = analysis_disk_budget(selected, request, len(research_index.encode("utf-8")) if research_index else 0)
    require_analysis_disk_space(store.jobs_root, disk_budget)
    created = store.create(name, params, uploaded, steps)
    target = store.job_dir(created["id"])
    try:
        # Another task may consume space between preflight and staging.
        require_analysis_disk_space(target, disk_budget)
        for item in mapping:
            shutil.copy2(selected[item["role"]], target / item["path"])
        if research_index is not None:
            (target / "research-index.ndx").write_text(research_index, encoding="utf-8")
        protocol = dict((source.get("analysis_source") or {}).get("protocol") or {})
        if not protocol:
            protocol = {key: (source.get("params") or {}).get(key) for key in ("force_field", "water_model", "ion_concentration")}
            for mdp_name in (Path(request.tpr_file).with_suffix(".mdp").as_posix(), "md.mdp"):
                try:
                    mdp = source_file(root, mdp_name, {".mdp"})
                    if mdp.stat().st_size > 262144:
                        continue
                    values = _read_mdp_values(mdp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                protocol.update({key: values.get(key) for key in ("ref_t", "ref_p", "dt", "tcoupl", "pcoupl", "constraints")})
                break
        provenance = {
            "job_id": job_id,
            "name": source["name"],
            "files": mapping,
            "storage": "independent_copy",
            "protocol": protocol,
            "window_ns": [request.begin_ns, request.end_ns],
            "storage_estimate": disk_budget,
        }
        if request.do_rmsd:
            provenance["rmsd_reference"] = {
                "source": "selected TPR coordinates",
                "fit_group": request.fit_group,
                "rmsd_group": request.rmsd_group,
                "pbc": "nojump from source trajectory start, then cluster+center; time window applied after nojump",
                "center_group": params["center_group"],
                "fit": "rot+trans within gmx rms",
            }
        prepared = store.update(created["id"], analysis_source=provenance)
        manifest = write_experiment_manifest(prepared)
        store.update(created["id"], experiment_manifest="experiment-manifest.json", input_hashes=manifest["input_files"])
        store.append_log(created["id"], f"[web] analysis inputs copied from {job_id}\n")
        store.start(created["id"], steps, dry_run=request.dry_run)
    except Exception:
        # A partial copy must never become runnable through the retry action.
        # This preparing task has no process to cancel; leave no claimable action.
        store.update(created["id"], status="cancelled", error="分析输入准备失败，未加入运行队列。", desired_action=None)
        for filename in uploaded:
            try:
                (target / filename).unlink(missing_ok=True)
            except OSError:
                store.append_log(created["id"], "[web] could not remove partial analysis input\n")
        raise
    return store.get(created["id"])
