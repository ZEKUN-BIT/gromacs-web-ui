"""Bounded, read-only progress from native GROMACS logs, including uploaded TPRs.

Reading progress in the web process also supports calculations started by an older
worker. It never changes the database or the running calculation.
"""

from __future__ import annotations

import math
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from .files import ensure_under_root

STEP_TIME = re.compile(r"\bStep\s+Time\s*\n\s*(\d+)\s+([\d.eE+-]+)")
NUMBER = r"([-+\d.eE]+)"
HEAD_BYTES = 96 * 1024
TAIL_BYTES = 128 * 1024
_cache: OrderedDict[tuple, dict] = OrderedDict()
_samples: OrderedDict[tuple, tuple[float, int, dict]] = OrderedDict()
_lock = threading.Lock()


def _parameter(text: str, key: str) -> float | None:
    matches = list(re.finditer(rf"^\s*{re.escape(key)}\s*=\s*{NUMBER}", text, re.MULTILINE))
    if not matches:
        return None
    try:
        value = float(matches[-1].group(1))
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def parse_native_log(header: str, tail: str) -> dict:
    """Parse actual log records; incomplete records and unknown totals stay unknown."""
    matches = list(STEP_TIME.finditer(tail))
    if not matches:
        return {}
    try:
        step = int(matches[-1].group(1))
        time_ps = float(matches[-1].group(2))
    except ValueError:
        return {}
    if not math.isfinite(time_ps):
        return {}
    parameters = header + "\n" + tail
    nsteps = _parameter(parameters, "nsteps")
    initial = _parameter(parameters, "init-step") or 0
    overrides = list(re.finditer(r"Overriding nsteps with value passed on the command line:\s*(\d+)\s+steps", parameters))
    if overrides:
        nsteps = int(overrides[-1].group(1))
    restarted = parameters.rfind("Restarting from checkpoint, appending to previous log file")
    parameter_records = list(re.finditer(r"^\s*nsteps\s*=", parameters, re.MULTILINE))
    target_position = max([record.start() for record in parameter_records + overrides], default=-1)
    if restarted > target_position or (nsteps is not None and step > initial + nsteps):
        # Appending an extended TPR does not rewrite Input Parameters. Its old
        # target is not evidence of the current target, even below 100%.
        nsteps = None
    total = int(initial + nsteps) if nsteps is not None and nsteps > 0 else None
    dt = _parameter(parameters, "dt")
    return {
        "step": step,
        "time_ps": time_ps,
        "total_steps": total,
        "dt_ps": dt,
        "percent": round(min(100, max(0, (step - initial) / nsteps * 100)), 2) if total and nsteps else None,
    }


def _current_args(meta: dict) -> list[str]:
    commands = meta.get("commands") or []
    offset = max(0, int(meta.get("step_count") or len(commands)) - len(commands))
    index = int(meta.get("step_index") or 0) - offset - 1
    if not 0 <= index < len(commands):
        return []
    return commands[index].get("args") or []


def _log_path(meta: dict, args: list[str]) -> Path:
    def option(flag: str, default: str) -> str:
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    root = Path(meta["workdir"])
    candidate = root / option("-g", option("-deffnm", "md") + ".log")
    if any(part.is_symlink() for part in [candidate, *candidate.parents] if part != root.parent):
        raise ValueError("Native log must not be a symbolic link")
    return ensure_under_root(candidate, root)


def _read_progress(path: Path, root: Path) -> tuple[dict, os.stat_result]:
    parts = path.relative_to(root).parts
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Native log must be a regular file")
        key = (str(path), info.st_ino, info.st_size, info.st_mtime_ns)
        with _lock:
            if key in _cache:
                return dict(_cache[key]), info
        header = handle.read(HEAD_BYTES).decode("utf-8", errors="replace")
        handle.seek(max(0, info.st_size - TAIL_BYTES))
        result = parse_native_log(header, handle.read(TAIL_BYTES).decode("utf-8", errors="replace"))
        with _lock:
            _cache[key] = result
            while len(_cache) > 128:
                _cache.popitem(last=False)
        return dict(result), info


def enrich_progress(meta: dict) -> dict:
    result = dict(meta)
    args = _current_args(meta)
    if meta.get("status") != "running" or "mdrun" not in args or meta.get("dry_run"):
        return result
    try:
        path = _log_path(meta, args)
        simulation, info = _read_progress(path, Path(meta["workdir"]).resolve())
        if simulation and "-cpi" in args:
            # The restart marker can fall outside the bounded head/tail window.
            # Checkpoint continuation may use a replaced/extended TPR, whose
            # total is not recoverable reliably from the appended log alone.
            simulation.update(total_steps=None, percent=None)
    except (OSError, ValueError, KeyError):
        simulation = {}
    if not simulation:
        result["progress_observation"] = {"state": "waiting", "message": "正在等待 GROMACS 写入进度；计算可能仍在初始化。"}
        if not (meta.get("simulation_progress") or {}).get("step"):
            result["progress_percent"] = None
        return result

    now = time.time()
    age = max(0, now - info.st_mtime)
    simulation["updated_at"] = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
    simulation["source"] = path.name
    sample_key = (str(path), info.st_ino, meta.get("process_started_at"), meta.get("step_index"))
    with _lock:
        previous = _samples.get(sample_key)
        elapsed = info.st_mtime - previous[0] if previous else 0
        delta = simulation["step"] - previous[1] if previous else 0
        if previous and elapsed > 0 and delta > 0 and simulation["dt_ps"] and simulation["dt_ps"] > 0:
            simulation["ns_per_day"] = round(delta / elapsed * simulation["dt_ps"] * 86.4, 3)
            if simulation["total_steps"]:
                simulation["eta_seconds"] = round(max(0, simulation["total_steps"] - simulation["step"]) / (delta / elapsed))
        elif previous and delta == 0:
            # Keep the last estimate until a new log record arrives.
            simulation.update(previous[2])
        if not previous or elapsed >= 5 or delta < 0:
            estimate = {key: simulation[key] for key in ("ns_per_day", "eta_seconds") if key in simulation}
            _samples[sample_key] = (info.st_mtime, simulation["step"], estimate)
        while len(_samples) > 128:
            _samples.popitem(last=False)
    if age > 600:
        simulation.pop("eta_seconds", None)
    result["simulation_progress"] = simulation
    fraction = simulation["percent"]
    result["progress_percent"] = (
        round((max(0, int(meta.get("step_index") or 1) - 1) + fraction / 100) / max(1, int(meta.get("step_count") or 1)) * 100, 2)
        if fraction is not None
        else None
    )
    result["progress_observation"] = {
        "state": "stale" if age > 600 else "current",
        "updated_at": simulation["updated_at"],
        "source": path.name,
        "message": (
            "日志超过 10 分钟未更新，请结合输出间隔和原生日志检查。"
            if age > 600
            else "已读取模拟步数与时间；当前日志无法确定总步数，完成比例暂未知。"
            if simulation["total_steps"] is None
            else "进度来自 GROMACS 原生日志。"
        ),
    }
    return result
