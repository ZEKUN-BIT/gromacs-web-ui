"""Inspect research input sizes before staging multi-GB trajectories."""

from __future__ import annotations

import ctypes
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from ..models import ExistingAnalysisRequest
from ..operations import OperationContext
from .limits import research_size

_ISOLATED = False


def _bound_process(parent_pid: int, seconds: int = 600) -> None:
    """Linux/WSL workers die with their parent, even while scanning in C."""
    if sys.platform != "linux":
        raise RuntimeError("Cancellable preflight requires the Linux/WSL runtime.")
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "Cannot bind preflight process lifetime")
    if os.getppid() != parent_pid:
        os._exit(1)
    # Default signal action is enforced by the OS even if the C scanner holds
    # Python's GIL. This also bounds accidental loss of the supervising thread.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(seconds)


def inspect_research_inputs_cancellable(
    selected: dict[str, Path], request: ExistingAnalysisRequest, gmx_bin: str, operation: OperationContext
) -> dict:
    operation.checkpoint()
    payload = json.dumps(
        {
            "files": {key: str(path) for key, path in selected.items()},
            "request": request.model_dump(),
            "gmx_bin": gmx_bin,
            "parent_pid": os.getpid(),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.analysis.preflight"],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        while True:
            operation.checkpoint()
            try:
                output, _ = process.communicate(payload, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                payload = None
        operation.checkpoint()
        if process.returncode:
            raise ValueError("轨迹预检未完成，请检查输入完整性或缩小轨迹范围。")
        result = json.loads(output)
        if "error" in result:
            raise ValueError(result["error"])
        return result["result"]
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


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
                parent_pid = os.getpid()
                result = subprocess.run(
                    [gmx_bin, "editconf", "-f", str(path), "-o", str(reference)],
                    cwd=directory,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                    check=False,
                    # The isolated worker is single-threaded. Bind its GROMACS
                    # decoder too, so parent death cannot leave an orphan.
                    preexec_fn=(lambda: _bound_process(parent_pid, 60)) if _ISOLATED else None,
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


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    _bound_process(payload["parent_pid"])
    _ISOLATED = True
    try:
        result = inspect_research_inputs(
            {key: Path(value) for key, value in payload["files"].items()},
            ExistingAnalysisRequest.model_validate(payload["request"]),
            payload["gmx_bin"],
        )
        print(json.dumps({"result": result}, ensure_ascii=False, allow_nan=False))
    except Exception:
        # No source paths, environment details or arbitrary decoder output cross
        # this process boundary into the provider-facing tool response.
        print(json.dumps({"error": "无法预检输入，请检查 TPR 与轨迹是否匹配、完整且时间递增。"}, ensure_ascii=False))
