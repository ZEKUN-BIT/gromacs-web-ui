"""Stable advice codes while preserving the existing HTTP detail contract."""

from __future__ import annotations

import re


def error_advice(message: str) -> dict:
    rules = [
        (
            r"No space left|disk space|磁盘.*(不足|阈值)",
            "storage_full",
            "运行盘空间不足",
            "打开空间管理清理旧结果；确认 checkpoint 后继续运行。",
        ),
        (
            r"ligand_charge|net charge|电子数|odd number of electrons",
            "ligand_charge",
            "配体电荷需要确认",
            "核对质子化状态与净电荷，再确认每个配体的总电荷。",
        ),
        (r"not in the current upload list|references '.+'", "missing_upload", "参数引用了未上传的文件", "重新选择输入文件，检查文件映射。"),
        (r"maxwarn|Too many warnings", "grompp_warning", "预处理发现需要处理的警告", "打开日志查看 grompp 原始警告，修正体系后重试。"),
        (
            r"LINCS WARNING|constraint.*(error|fail)",
            "constraint_failure",
            "约束计算异常",
            "检查初始结构、能量最小化结果、时间步及平衡条件。",
        ),
        (
            r"No such file|not found|not available|未检测到",
            "missing_dependency_or_file",
            "文件或计算工具不可用",
            "检查文件列表和运行环境诊断，再重试失败步骤。",
        ),
        (r"maintenance|deletion|still stopping", "task_busy", "任务仍在执行或处理文件", "等待当前操作完成后重试。"),
        (r"outside|symbolic link|symlink", "unsafe_path", "文件路径不可用", "使用任务目录中的普通文件，避免符号链接和目录外路径。"),
    ]
    for pattern, code, title, recovery in rules:
        if re.search(pattern, str(message), re.IGNORECASE):
            return {"code": code, "message": title, "recovery": recovery}
    return {"code": "operation_failed", "message": "操作未完成", "recovery": "查看原始错误和日志，修正后重试。"}
