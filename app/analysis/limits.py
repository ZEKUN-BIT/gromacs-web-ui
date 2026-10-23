"""Shared research size limits for preflight and the final worker check."""

from __future__ import annotations

import math

MAX_RESEARCH_FRAMES = 5000
MAX_RESEARCH_RESIDUES = 3000
MAX_RESEARCH_FRAME_RESIDUES = 2_000_000


def research_size(frames: int, residues: int, stride: int = 1) -> dict:
    sampled = math.ceil(frames / stride)
    limit = min(MAX_RESEARCH_FRAMES, MAX_RESEARCH_FRAME_RESIDUES // max(1, residues))
    suggested = max(1, math.ceil(frames / max(1, limit)))
    message = ""
    if not 3 <= residues <= MAX_RESEARCH_RESIDUES:
        message = f"研究分析需要 3–{MAX_RESEARCH_RESIDUES} 个 C-alpha，当前为 {residues}；请选择适用的蛋白体系。"
    elif sampled < 3:
        message = "所选时间范围及取帧间隔不足 3 帧，请扩大时间范围或减小取帧间隔。"
    elif sampled > limit:
        message = (
            f"当前预计取样 {sampled} 帧 × {residues} 个残基，超过研究分析上限。"
            f"建议每 {suggested} 帧取一帧，或缩短时间范围；调整采样前请确认研究需要。"
        )
    return {
        "valid": not message,
        "message": message,
        "selected_frames": frames,
        "sampled_frames": sampled,
        "residues": residues,
        "frame_limit": limit,
        "suggested_stride": suggested,
    }
