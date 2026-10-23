"""Inspect research input sizes before staging multi-GB trajectories."""

from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path

from ..models import ExistingAnalysisRequest
from .limits import research_size


def _topology(path: Path, gmx_bin: str):
    import MDAnalysis as mda

    try:
        return mda.Universe(str(path))
    except (ValueError, OSError, NotImplementedError):
        # New GROMACS TPR versions can precede support in MDAnalysis. Let the
        # installed GROMACS decode its own topology without touching the source.
        with tempfile.TemporaryDirectory(prefix="gromacs-preflight-") as directory:
            reference = Path(directory) / "reference.pdb"
            try:
                result = subprocess.run(
                    [gmx_bin, "editconf", "-f", str(path), "-o", str(reference)],
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("无法预检 TPR，请检查 GROMACS 路径和拓扑文件。") from exc
            if result.returncode or not reference.is_file():
                raise ValueError("无法读取 TPR，请确认拓扑有效且与当前 GROMACS 兼容。")
            return mda.Universe(str(reference))


def inspect_research_inputs(selected: dict[str, Path], request: ExistingAnalysisRequest, gmx_bin: str) -> dict:
    from MDAnalysis.lib.formats.libmdaxdr import TRRFile, XTCFile

    topology = _topology(selected["tpr_file"], gmx_bin)
    residues = len(topology.select_atoms("protein and name CA"))
    path = selected["trajectory_file"]
    reader_type = XTCFile if path.suffix.lower() == ".xtc" else TRRFile
    try:
        with reader_type(str(path)) as reader:
            # The XDR offset scan reads frame headers in C, without loading all
            # coordinates or creating MDAnalysis offset caches in the source.
            frames = len(reader.offsets)
            if not frames:
                raise ValueError("轨迹不包含可读取帧。")
            if reader.n_atoms != len(topology.atoms):
                raise ValueError("轨迹原子数与 TPR 不一致，请选择完整体系且相互匹配的文件。")

            def frame_time(index: int) -> float:
                reader.seek(index)
                value = float(reader.read().time) / 1000
                if not math.isfinite(value):
                    raise ValueError("轨迹时间不是有限数值。")
                return value

            first, last = frame_time(0), frame_time(frames - 1)
            if last < first:
                raise ValueError("轨迹时间倒序，请先检查轨迹。")

            def boundary(value: float, *, upper: bool = False) -> int:
                left, right = 0, frames
                while left < right:
                    middle = (left + right) // 2
                    time = frame_time(middle)
                    if time < value - 1e-8 or upper and time <= value + 1e-8:
                        left = middle + 1
                    else:
                        right = middle
                return left

            start = boundary(request.begin_ns)
            stop = frames if request.end_ns is None else boundary(request.end_ns, upper=True)
    except (OSError, EOFError) as exc:
        raise ValueError("无法读取轨迹，请确认 XTC/TRR 文件完整。") from exc
    return {
        **research_size(max(0, stop - start), residues, request.research_stride),
        "available_frames": frames,
        "trajectory_window_ns": [first, last],
        "atoms": len(topology.atoms),
        "note": "按递增轨迹时间预估；实际取帧及坐标有效性由分析步骤再次核验。",
    }
