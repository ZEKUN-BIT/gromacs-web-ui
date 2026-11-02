"""Allowlisted explanations for AI tools, without exporting raw errors or logs."""

from __future__ import annotations

import math
import re

QUALITY_STATUSES = frozenset({"检查通过", "检查未通过", "需要复核", "PASS", "FAIL", "WARNING", "需阅读报告", "空报告"})
MAX_QUALITY_REPORTS = 12
_NUMBER = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
_EXACT_NOTES = {
    "constraint/LINCS warning detected": ("constraint_warning", "检测到约束或 LINCS 警告；应先核查结构和模拟参数。"),
    "NaN detected": ("nonfinite_values", "检测到非有限数值，需核查数值稳定性。"),
    "energy minimization did not converge to Fmax": ("minimization_not_converged", "能量最小化未达到 Fmax 收敛条件。"),
    "取样少于 50 帧，分布和占有率估计可能不稳定。": ("few_frames", "取样少于 50 帧，分布和占有率估计可能不稳定。"),
    "采样时间不均匀，占有率为帧比例，不代表驻留时间比例。": ("uneven_sampling", "采样时间不均匀，占有率不代表驻留时间比例。"),
    "参考帧没有阈值内接触，初始口袋保持率不可计算。": ("missing_initial_contacts", "参考帧没有阈值内接触，初始口袋保持率不可计算。"),
}
_NUMERIC_NOTES = (
    (
        rf"mean temperature {_NUMBER} K differs from target {_NUMBER} K",
        "temperature_outside_target",
        "平均温度偏离设定温度超过 5 K。",
        ("mean_k", "target_k"),
        {"allowed_deviation_k": 5.0},
    ),
    (
        rf"aqueous density {_NUMBER} kg/m\^3 is implausible",
        "density_outside_range",
        "水溶液平均密度超出质量检查范围。",
        ("mean_kg_m3",),
        {"minimum_kg_m3": 850.0, "maximum_kg_m3": 1200.0},
    ),
    (
        rf"density drift {_NUMBER} kg/m\^3 has not stabilized",
        "density_drift",
        "密度前后段漂移超过质量检查阈值。",
        ("drift_kg_m3",),
        {},
    ),
    (
        rf"volume drift {_NUMBER} exceeds 3%",
        "volume_drift",
        "体积前后段漂移超过均值的 3%。",
        ("drift_nm3",),
        {"maximum_fraction": 0.03},
    ),
)
_STEP_LABELS = {
    "validate_stage_log": "模拟阶段质量检查",
    "validate_thermodynamics": "温度、密度与体积质量检查",
    "structure_preflight": "输入结构预检",
    "generate_analysis_figures": "分析图表生成",
    "summarize_replicas": "副本统计汇总",
    "prepare_ligand_chemistry": "配体化学检查",
}
_COMMAND_LABELS = {
    "mdrun": "分子动力学或能量最小化计算",
    "grompp": "模拟输入预处理",
    "pdb2gmx": "蛋白拓扑生成",
    "trjconv": "轨迹预处理",
    "energy": "能量与热力学分析",
    "rms": "RMSD 分析",
    "rmsf": "RMSF 分析",
    "gyrate": "回转半径分析",
}


def _quality_note(value: object) -> dict:
    # Exact matches are intentional: a valid prefix followed by a path, secret,
    # command or injected instruction must never be echoed to the provider.
    if isinstance(value, str) and len(value) <= 500:
        if value in _EXACT_NOTES:
            code, message = _EXACT_NOTES[value]
            return {"code": code, "message": message}
        for pattern, code, message, fields, limits in _NUMERIC_NOTES:
            match = re.fullmatch(pattern, value)
            if match:
                values = [float(item) for item in match.groups()]
                if all(math.isfinite(item) and abs(item) <= 1e12 for item in values):
                    return {"code": code, "message": message, "metrics": {**dict(zip(fields, values, strict=True)), **limits}}
        match = re.fullmatch(r"required observable (Temperature|Density|Volume) is missing", value)
        if match:
            return {"code": "missing_observable", "message": "缺少质量检查所需观测量。", "observable": match.group(1)}
    return {"code": "manual_review", "message": "报告含需人工复核的说明，请在本机查看该报告。"}


def quality_diagnostics(reports: list[dict]) -> list[dict]:
    result = []
    for report in reports:
        name, status = report.get("file"), report.get("status")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\.(?:json|txt)", name):
            continue
        if status not in QUALITY_STATUSES:
            continue
        notes = []
        for value in (report.get("notes") or [])[:4]:
            note = _quality_note(value)
            if note not in notes:
                notes.append(note)
        result.append({"file": name, "status": status, "notes": notes, "notes_truncated": len(report.get("notes") or []) > 4})
        if len(result) >= MAX_QUALITY_REPORTS:
            break
    return result


def failure_diagnostics(meta: dict) -> dict | None:
    if meta.get("status") not in {"failed", "interrupted"}:
        return None
    runs = meta.get("step_runs") or []
    failed = next((run for run in reversed(runs) if isinstance(run, dict) and run.get("status") in {"failed", "interrupted"}), {})
    index = failed.get("index", meta.get("step_index"))
    index = index if type(index) is int and 1 <= index <= 100_000 else None
    operation = failed.get("operation")
    operation = operation if isinstance(operation, str) else None
    args = failed.get("args")
    command = args[1] if isinstance(args, list) and len(args) > 1 and isinstance(args[1], str) else None
    label = _STEP_LABELS.get(operation, _COMMAND_LABELS.get(command, "任务执行步骤"))
    exit_code = failed.get("exit_code", meta.get("exit_code"))
    exit_code = exit_code if type(exit_code) is int and -255 <= exit_code <= 255 else None
    error = meta.get("error")
    error = error[:2048] if isinstance(error, str) else ""
    if meta.get("disk_stop") or error.startswith(("Low disk space:", "磁盘空间不足，已请求受控停止。")):
        code, message = "storage_insufficient", "磁盘空间保护触发；释放空间后检查 checkpoint 和任务状态。"
    elif operation in {"validate_stage_log", "validate_thermodynamics", "structure_preflight"}:
        code, message = "quality_check_failed", "质量检查未通过；请结合对应报告处理原因，再决定是否重试。"
    elif meta["status"] == "interrupted":
        code, message = "execution_interrupted", "执行已中断；需核查进程状态和 checkpoint。"
    elif exit_code not in {None, 0}:
        code, message = "process_failed", "计算进程以非零退出码结束；退出码本身不能确定科学原因。"
    else:
        code, message = "step_failed", "步骤未完成；现有结构化记录不足以确定原因，请在本机核查。"
    return {"code": code, "message": message, "step": {"index": index, "label": label}, "exit_code": exit_code}
