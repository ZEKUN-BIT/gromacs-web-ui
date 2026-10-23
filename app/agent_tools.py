"""A narrow, typed bridge from model suggestions to existing application actions.

The caller owns durable user approvals. Preparing an action never creates or
queues a task; executing it revalidates both arguments and the approved source.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalog import list_templates
from .existing_analysis import INPUT_SUFFIXES, analysis_inputs, create_existing_analysis, preflight_existing_analysis, source_file
from .gromacs import WORKFLOWS
from .job_store import JobStore
from .models import ExistingAnalysisRequest
from .result_bundle import quality_summary

JobId = Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
ShortText = Annotated[str, Field(max_length=160, pattern=r"^[^\x00-\x1f\x7f]*$")]
SafeToken = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")]
SAFE_WORKFLOWS = frozenset(WORKFLOWS) - {"custom"}
SUMMARY_FIELDS = (
    "id",
    "name",
    "status",
    "workflow",
    "created_at",
    "project",
    "tags",
    "pinned",
    "step_index",
    "step_count",
    "progress_percent",
    "performance_ns_per_day",
    "dry_run",
    "is_example",
)
SCIENTIFIC_FIELDS = (
    "force_field",
    "water_model",
    "box_type",
    "box_distance",
    "ion_concentration",
    "neutral",
    "replicas",
    "pre_nvt",
    "release_restraints",
    "mdp_dt_ps",
    "mdp_md_ns",
    "mdp_nvt_ns",
    "mdp_npt_ns",
    "mdp_temperature_k",
    "mdp_pressure_bar",
    "begin_ns",
    "end_ns",
    "research_stride",
    "contact_cutoff_nm",
    "do_rmsd",
    "do_rg",
    "do_energy",
    "do_dssp",
    "do_hbond",
    "do_pca",
    "do_research",
)
RESOURCE_FIELDS = ("max_parallel", "analysis_parallel", "default_ntmpi", "default_ntomp")
WORKER_FIELDS = ("available", "state", "max_parallel", "thread_capacity", "alive_threads", "ready_threads")
INTERNAL_OPERATIONS = frozenset(
    {
        "structure_preflight",
        "clean_pdb_for_pdb2gmx",
        "split_complex_pdb",
        "write_ligand_smiles",
        "prepare_ligand_chemistry",
        "stage_prepared_ligand",
        "stage_acpype_ligand",
        "restore_ligand_pose_coordinates",
        "isolate_acpype_output",
        "merge_ligand_atomtypes",
        "merge_gro",
        "create_complex_index",
        "patch_equilibration_defines",
        "validate_stage_log",
        "prepare_npt_mdp",
        "validate_thermodynamics",
        "prepare_replica_mdp",
        "prepare_unrestrained_npt_mdp",
        "summarize_replicas",
        "patch_topology",
        "combine_pc_projection",
        "remove_files",
        "generate_analysis_figures",
    }
)
GMX_COMMANDS = frozenset(
    {
        "pdb2gmx",
        "editconf",
        "solvate",
        "grompp",
        "genion",
        "mdrun",
        "energy",
        "rms",
        "rmsf",
        "gyrate",
        "sasa",
        "mindist",
        "distance",
        "trjconv",
        "dssp",
        "hbond",
        "hbond-legacy",
        "covar",
        "anaeig",
        "sham",
        "make_ndx",
        "genrestr",
    }
)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Target(Arguments):
    job_id: JobId


class JobList(Arguments):
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=100000)
    search: str = Field(default="", max_length=256)
    project: str = Field(default="", max_length=80)


class Clone(Target):
    name: ShortText = ""


class AnalysisOptions(ExistingAnalysisRequest):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @field_validator("tpr_file", "trajectory_file", "index_file", "edr_file")
    @classmethod
    def relative_input(cls, value: str) -> str:
        if value and (Path(value).is_absolute() or ".." in Path(value).parts or "\\" in value or ":" in value):
            raise ValueError("分析输入必须使用任务内的相对文件名。")
        return value

    @model_validator(mode="after")
    def enabled_analysis(self):
        if not any(getattr(self, key) for key in ("do_rmsd", "do_rg", "do_energy", "do_dssp", "do_hbond", "do_pca", "do_research")):
            raise ValueError("请至少选择一项分析指标。")
        if self.do_energy and not self.edr_file:
            raise ValueError("能量分析需要选择 EDR 文件。")
        return self


class Analysis(Target):
    analysis: AnalysisOptions


class SimulationParameters(Arguments):
    workflow: Literal["protein_md", "protein_ligand_md", "em_only", "run_tpr"] = "protein_md"
    name: ShortText = "GROMACS run"
    project: str = Field(default="", max_length=80)
    force_field: SafeToken = "amber19sb"
    water_model: SafeToken = "opc"
    box_type: Literal["dodecahedron", "cubic", "octahedron"] = "dodecahedron"
    box_distance: float = Field(default=1.0, ge=0.2, le=10)
    ntmpi: int | None = Field(default=None, ge=1, le=64)
    ntomp: int | None = Field(default=None, ge=1, le=256)
    gpu: bool = False
    neutral: bool = True
    ion_concentration: float | None = Field(default=None, ge=0, le=5)
    pre_nvt: bool = False
    replicas: int = Field(default=3, ge=1, le=20)
    release_restraints: bool = True
    dry_run: bool = False
    mdp_dt_ps: float | None = Field(default=None, ge=0.0001, le=0.004)
    mdp_md_ns: float | None = Field(default=None, gt=0, le=100000)
    mdp_nvt_ns: float | None = Field(default=None, gt=0, le=1000)
    mdp_npt_ns: float | None = Field(default=None, gt=0, le=1000)
    mdp_temperature_k: float | None = Field(default=None, ge=1, le=1000)
    mdp_pressure_bar: float | None = Field(default=None, ge=0.01, le=10000)


class Simulation(Arguments):
    params: SimulationParameters


TOOLS = {
    "workspace_summary": (Arguments, False, "查看任务数量、执行器状态和资源限制，不含本机路径或密钥。"),
    "list_jobs": (JobList, False, "分页查询任务摘要；search 可搜索名称、状态、工作流，project 为精确项目筛选。"),
    "get_job": (Target, False, "查看任务状态、可用操作、科学参数和质量检查状态；不读取原始结构、轨迹或日志。"),
    "analysis_inputs": (Target, False, "列出已完成任务可用于分析的相对文件名与大小。"),
    "available_workflows": (Arguments, False, "查看支持的科学工作流和协议模板摘要，用于准备模拟表单。"),
    "clone_job": (Clone, True, "建议复制已有安全工作流并加入运行队列；必须由用户批准后执行。"),
    "cancel_job": (Target, True, "建议取消指定任务；必须由用户批准后执行。"),
    "resume_job": (Target, True, "建议从 checkpoint 恢复失败或中断任务；必须由用户批准后执行。"),
    "retry_job": (Target, True, "建议重试失败步骤，不跳过科学检查；必须由用户批准后执行。"),
    "create_analysis": (Analysis, True, "预检已完成任务的指定分析输入，建议创建独立分析任务；用户批准后复制并排队。"),
    "prepare_simulation": (Simulation, True, "建议填入模拟表单的安全参数。批准后仅生成草稿，用户仍需上传文件并核对化学信息、预览和提交。"),
}


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _safe_relative(value: str) -> bool:
    return bool(value) and not Path(value).is_absolute() and ".." not in Path(value).parts and "\\" not in value and ":" not in value


def _summary(meta: dict) -> dict:
    result = {key: meta[key] for key in SUMMARY_FIELDS if key in meta}
    for key in ("name", "project"):
        if key in result:
            result[key] = str(result[key])[:160]
    result["tags"] = [str(item)[:40] for item in (meta.get("tags") or [])[:12]]
    return result


def _scientific_parameters(params: dict) -> dict:
    result = {}
    for key in SCIENTIFIC_FIELDS:
        value = params.get(key)
        if isinstance(value, (bool, int, float)) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str) and len(value) <= 80:
            if key in {"force_field", "water_model", "box_type"}:
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", value):
                    result[key] = value
            elif not value or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
                result[key] = value
    return result


class AgentTools:
    def __init__(self, store: JobStore, settings: dict, base_dir: Path, worker_status: Callable[[], dict]):
        self.store, self.settings, self.base_dir, self.worker_status = store, settings, base_dir, worker_status

    def definitions(self) -> list[dict]:
        return [
            {"name": name, "description": description, "parameters": model.model_json_schema()}
            for name, (model, _, description) in TOOLS.items()
        ]

    def is_mutating(self, name: str) -> bool:
        if name not in TOOLS:
            raise ValueError("不支持的 AI 工具。")
        return TOOLS[name][1]

    def _arguments(self, name: str, args: dict, *, mutating: bool) -> dict:
        if self.is_mutating(name) != mutating:
            raise ValueError("此工具不能用于当前操作。")
        return TOOLS[name][0].model_validate(args).model_dump()

    def _resources(self, params: dict | None = None) -> dict:
        params = params or {}
        ntmpi = params.get("ntmpi") or self.settings.get("default_ntmpi", 1)
        ntomp = params.get("ntomp") or self.settings.get("default_ntomp", 4)
        return {
            "ntmpi": ntmpi,
            "ntomp": ntomp,
            "requested_cpu_threads": int(ntmpi) * int(ntomp),
            "gpu": bool(params.get("gpu")),
            "analysis_parallel": self.settings.get("analysis_parallel", 2),
        }

    def run_read(self, name: str, args: dict) -> dict:
        args = self._arguments(name, args, mutating=False)
        if name == "workspace_summary":
            with self.store._connect() as connection:
                rows = connection.execute("SELECT status,count(*) AS count FROM jobs GROUP BY status").fetchall()
            worker = self.worker_status()
            return {
                "task_counts": {row["status"]: row["count"] for row in rows},
                "worker": {key: worker[key] for key in WORKER_FIELDS if key in worker},
                "settings": {key: self.settings[key] for key in RESOURCE_FIELDS if key in self.settings},
            }
        if name == "list_jobs":
            page = self.store.list_page(**args)
            return {**page, "jobs": [_summary(item) for item in page["jobs"]]}
        if name == "available_workflows":
            templates = list_templates(self.store)
            return {
                "workflows": {key: value for key, value in WORKFLOWS.items() if key in SAFE_WORKFLOWS},
                "templates": [
                    {
                        **{key: item.get(key) for key in ("id", "name", "revision", "description", "applicability")},
                        "workflow": item["params"]["workflow"],
                        "parameters": _scientific_parameters(item["params"]),
                    }
                    for item in templates[:50]
                    if (item.get("params") or {}).get("workflow") in SAFE_WORKFLOWS
                ],
                "templates_truncated": len(templates) > 50,
            }
        if name == "analysis_inputs":
            result = analysis_inputs(self.store, args["job_id"])
            result["files"] = {
                key: [{"path": item["path"], "size": item.get("size")} for item in values[:100] if _safe_relative(item["path"])]
                for key, values in result["files"].items()
            }
            result["truncated"] = result["truncated"] or any(len(values) >= 100 for values in result["files"].values())
            return result
        meta = self.store.get(args["job_id"])
        safe = self._runnable(meta, raise_error=False)
        quality = []
        with self.store.job_files_lock(meta["id"]):
            for item in quality_summary(self.store.job_dir(meta["id"])):
                if item["status"] in {"检查通过", "检查未通过", "需要复核", "PASS", "FAIL", "WARNING", "需阅读报告", "空报告"}:
                    quality.append({"file": item["file"], "status": item["status"]})
        return {
            "job": _summary(meta),
            "parameters": _scientific_parameters(meta.get("params") or {}),
            "resources": self._resources(meta.get("params")),
            "quality": quality,
            "capabilities": {
                "clone": safe and meta["status"] not in {"preparing", "running"},
                "cancel": meta.get("workflow") in SAFE_WORKFLOWS and meta["status"] in {"preparing", "queued", "running", "interrupted"},
                "resume": safe and bool(meta.get("can_resume_checkpoint")),
                "retry": safe and meta["status"] == "failed",
                "analysis": meta["status"] == "completed" and not meta.get("dry_run"),
            },
            "scientific_note": "任务完成只表示程序执行结束；采样充分性和科学结论仍需研究者审核。",
        }

    def _runnable(self, meta: dict, *, raise_error: bool = True) -> bool:
        """Saved executable overrides and custom/unchecked workflows stay in the manual UI."""
        params = meta.get("params") or {}
        allowed_executables = {"gmx_bin": self.settings.get("gmx_bin", "gmx"), "obabel_bin": "obabel", "acpype_bin": "acpype"}
        valid = meta.get("workflow") in SAFE_WORKFLOWS and not meta.get("skipped_steps")
        valid = valid and not any(
            params.get(key) for key in ("custom_command", "custom_stdin", "stdin_overrides", "index_script", "maxwarn")
        )
        valid = valid and all(params.get(key, expected) == expected for key, expected in allowed_executables.items())
        for command in meta.get("commands") or []:
            argv = command.get("args") or []
            if command.get("kind", "command") == "internal":
                valid = valid and command.get("operation") in INTERNAL_OPERATIONS
                continue
            valid = valid and bool(argv)
            if not argv:
                continue
            if argv[0] == allowed_executables["gmx_bin"]:
                valid = valid and len(argv) > 1 and argv[1] in GMX_COMMANDS and "-maxwarn" not in argv
            elif argv[0] in {"obabel", "acpype"}:
                valid = valid and meta.get("workflow") == "protein_ligand_md"
            else:
                valid = valid and argv == [sys.executable, "-m", "app.analysis.research"]
            valid = valid and all(
                isinstance(item, str)
                and not Path(item).is_absolute()
                and ".." not in Path(item).parts
                and not re.search(r"[\x00-\x1f\x7f]", item)
                for item in argv[1:]
            )
        if not valid and raise_error:
            raise ValueError("该任务含自定义命令、可执行程序覆盖或需人工复核的设置，请在任务页面手动操作。")
        return bool(valid)

    @staticmethod
    def _snapshot(meta: dict) -> dict:
        keys = (
            "id",
            "created_at",
            "workflow",
            "status",
            "started_at",
            "resumed_at",
            "process_started_at",
            "process_pid",
            "process_started_ticks",
            "step_index",
            "storage_revision",
            "desired_action",
            "maintenance_pending",
            "deletion_pending",
        )
        return {**{key: meta.get(key) for key in keys}, "definition": _fingerprint([meta.get("params"), meta.get("commands")])}

    def _analysis_snapshot(self, job_id: str, options: dict) -> dict:
        root = self.store.job_dir(job_id)
        result = {}
        for role, suffixes in INPUT_SUFFIXES.items():
            name = options[role]
            if not name or role == "edr_file" and not options["do_energy"]:
                continue
            info = source_file(root, name, suffixes).stat()
            result[role] = [name, info.st_size, info.st_mtime_ns, info.st_ino]
        return result

    def _simulation_params(self, args: dict) -> dict:
        params = {key: value for key, value in args["params"].items() if value is not None}
        for key in list(params):
            if key.startswith("mdp_"):
                params[key] = str(params[key])
                params["mdp_override_enabled"] = True
        params.setdefault("ntmpi", self.settings.get("default_ntmpi", 1))
        params.setdefault("ntomp", self.settings.get("default_ntomp", 4))
        return params

    def prepare_action(self, name: str, args: dict) -> dict:
        args = self._arguments(name, args, mutating=True)
        if name == "prepare_simulation":
            params = self._simulation_params(args)
            # The user approves concrete thread counts, even if the settings
            # page is changed before this proposal is accepted.
            args["params"]["ntmpi"] = params["ntmpi"]
            args["params"]["ntomp"] = params["ntomp"]
            return {
                "arguments": args,
                "title": "将模拟方案填入表单",
                "precondition": {"kind": "simulation_draft"},
                "preview": {
                    "workflow": params["workflow"],
                    "parameters": params,
                    "resources": self._resources(params),
                    "effect": "仅填写参数草稿；仍需上传输入文件、核对配体化学信息并手动提交。",
                },
            }
        meta = self._action_target(name, args)
        title, effect = {
            "clone_job": ("复制并运行任务", "复制输入文件，创建新任务并加入执行队列，将使用计算和磁盘资源。"),
            "cancel_job": ("取消任务", "停止排队或请求终止正在运行的计算，未保存的计算进度可能丢失。"),
            "resume_job": ("恢复 checkpoint 计算", "从现有 checkpoint 继续当前任务并加入执行队列。"),
            "retry_job": ("重试失败步骤", "重新执行失败步骤及其后续步骤，可能重新生成输出文件。"),
            "create_analysis": ("创建结果分析任务", "独立复制选定输入文件并创建分析任务，将使用磁盘和计算资源。"),
        }[name]
        precondition = {"job": self._snapshot(meta)}
        preview = {
            "target": _summary(meta),
            "parameters": args,
            "scientific_parameters": _scientific_parameters(meta.get("params") or {}),
            "resources": self._resources(meta.get("params")),
            "effect": effect,
        }
        if name == "create_analysis":
            options = AnalysisOptions.model_validate(args["analysis"])
            preview["preflight"] = preflight_existing_analysis(self.store, meta["id"], options, self.settings)
            research = preview["preflight"].get("research")
            if research and not research.get("valid"):
                raise ValueError(research.get("message") or "分析输入未通过预检。")
            precondition["inputs"] = self._analysis_snapshot(meta["id"], args["analysis"])
            preview["resources"] = self._resources()
        return {
            "arguments": args,
            "title": f"{title}：{str(meta.get('name') or meta['id'])[:160]}",
            "preview": preview,
            "precondition": precondition,
        }

    def _action_target(self, name: str, args: dict) -> dict:
        meta = self.store.get(args["job_id"])
        if meta.get("workflow") not in SAFE_WORKFLOWS or meta.get("maintenance_pending") or meta.get("deletion_pending"):
            raise ValueError("该任务工作流不受支持，或正在维护/删除，请在任务页面处理。")
        if name in {"clone_job", "resume_job", "retry_job"}:
            self._runnable(meta)
        allowed = {
            "clone_job": {"queued", "completed", "failed", "cancelled", "interrupted"},
            "cancel_job": {"preparing", "queued", "running", "interrupted"},
            "resume_job": {"failed", "interrupted"},
            "retry_job": {"failed"},
            "create_analysis": {"completed"},
        }
        if meta["status"] not in allowed[name]:
            raise ValueError("任务当前状态不支持此操作，请刷新任务后重新规划。")
        if name == "resume_job" and not meta.get("can_resume_checkpoint"):
            raise ValueError("该任务当前步骤不支持 checkpoint 恢复。")
        return meta

    def execute_action(self, name: str, args: dict, precondition: dict) -> dict:
        args = self._arguments(name, args, mutating=True)
        if name == "prepare_simulation":
            if precondition != {"kind": "simulation_draft"}:
                raise ValueError("模拟草稿审批无效，请重新生成。")
            return {"kind": "simulation_draft", "params": self._simulation_params(args)}
        # JobStore lifecycle methods each own their transaction. Progress writes
        # may continue; a different process/step or task definition needs approval
        # again. The caller must claim an approval once and must not auto-retry.
        meta = self.store.get(args["job_id"])
        if precondition.get("job") != self._snapshot(meta):
            raise ValueError("任务状态或执行步骤已变化，请重新生成操作建议并确认。")
        current = self._action_target(name, args)
        if precondition.get("job") != self._snapshot(current):
            raise ValueError("任务状态或执行步骤已变化，请重新生成操作建议并确认。")
        if name == "create_analysis":
            if precondition.get("inputs") != self._analysis_snapshot(meta["id"], args["analysis"]):
                raise ValueError("分析输入已变化，请重新预检并确认。")
            result = create_existing_analysis(self.store, meta["id"], AnalysisOptions.model_validate(args["analysis"]), self.settings)
        elif name == "clone_job":
            result = self.store.clone(meta["id"], args["name"] or None)
        elif name == "cancel_job":
            result = self.store.cancel(meta["id"])
        elif name == "resume_job":
            result = self.store.resume_from_checkpoint(meta["id"])
        else:
            result = self.store.retry_failed_step(meta["id"])
        return {"kind": "job", "job": _summary(result)}
