"""Read-only helpers for mdrun progress: MDP values, native logs and /proc usage."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .files import ensure_under_root

MDRUN_STEP_RE = re.compile(r"\bstep\s+(\d+)(?:.*?\btime\s+([0-9.eE+\-]+))?", re.IGNORECASE)
MDRUN_PERFORMANCE_RE = re.compile(r"\bPerformance:\s*([0-9.eE+\-]+)", re.IGNORECASE)
GROMACS_SEED_RE = re.compile(r"\b(?:seed|seeds)\b.*?[-+]?\d+", re.IGNORECASE)


def process_resources(pid: int) -> tuple[int, float]:
    peak_rss_bytes = 0
    cpu_seconds = 0.0
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8").splitlines():
            if line.startswith(("VmHWM:", "VmRSS:")):
                peak_rss_bytes = max(peak_rss_bytes, int(line.split()[1]) * 1024)
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()
        cpu_seconds = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        pass
    return peak_rss_bytes, cpu_seconds


def mdrun_mdp_path(workdir: Path, args: list[str], commands: list[dict]) -> Path | None:
    if "mdrun" not in args:
        return None
    tpr = None
    if "-s" in args and args.index("-s") + 1 < len(args):
        tpr = args[args.index("-s") + 1]
    elif "-deffnm" in args and args.index("-deffnm") + 1 < len(args):
        tpr = f"{args[args.index('-deffnm') + 1]}.tpr"
    if not tpr:
        return None
    mdp_name = None
    for candidate in commands:
        candidate_args = [str(arg) for arg in candidate.get("args") or []]
        if all(flag in candidate_args for flag in ("grompp", "-o", "-f")) and candidate_args[candidate_args.index("-o") + 1] == tpr:
            mdp_name = candidate_args[candidate_args.index("-f") + 1]
    if not mdp_name:
        return None
    return workdir / mdp_name


def mdrun_mdp_number(workdir: Path, args: list[str], commands: list[dict], key: str) -> float | None:
    path = mdrun_mdp_path(workdir, args, commands)
    if path is None:
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            clean = line.split(";", 1)[0]
            if clean.strip().lower().startswith(key.lower()) and "=" in clean:
                return float(clean.split("=", 1)[1].strip())
    except (FileNotFoundError, OSError, ValueError, IndexError):
        pass
    return None


def mdrun_total_steps(workdir: Path, args: list[str], commands: list[dict]) -> int | None:
    value = mdrun_mdp_number(workdir, args, commands, "nsteps")
    return max(1, int(value)) if value is not None else None


def native_log_path(workdir: Path, args: list[str]) -> Path:
    def option(flag: str, default: str) -> str:
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    return ensure_under_root(workdir / option("-g", option("-deffnm", "md") + ".log"), workdir)


def native_log_snapshot(workdir: Path, args: list[str]) -> dict | None:
    if "mdrun" not in args:
        return None
    path = native_log_path(workdir, args)
    snapshot = {"path": str(path.relative_to(workdir.resolve()))}
    try:
        info = path.stat()
    except FileNotFoundError:
        return snapshot
    return {**snapshot, "inode": info.st_ino, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def mdrun_completed(meta: dict, workdir: Path) -> bool:
    """Read a bounded tail of this invocation, excluding an earlier run's marker."""
    if meta.get("process_step_index", meta.get("step_index")) != meta.get("step_index"):
        return False
    path = native_log_path(workdir, [str(arg) for arg in meta.get("process_args") or []])
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    with os.fdopen(descriptor, "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode):
            return False
        before = meta.get("process_native_log") or {}
        start = 0
        if before.get("inode") == info.st_ino:
            if before.get("mtime_ns") == info.st_mtime_ns and before.get("size") == info.st_size:
                return False
            if info.st_size >= int(before.get("size") or 0):
                start = int(before.get("size") or 0)
        reader.seek(max(start, info.st_size - 128 * 1024))
        return b"Finished mdrun" in reader.read(128 * 1024)
