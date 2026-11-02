"""Create mdrun thread-count benchmark tasks from an existing task's TPR."""

from __future__ import annotations

import shutil
from contextlib import ExitStack
from pathlib import Path

from .files import ensure_under_root
from .models import BenchmarkRequest
from .reproducibility import write_experiment_manifest
from .steps import Step


def benchmark_tpr(store, meta: dict) -> Path:
    """Locate the TPR the source task ran, falling back to its newest TPR."""
    workdir = store.job_dir(str(meta["id"]))
    argument_sets = [list(meta.get("process_args") or [])]
    argument_sets.extend([list(command.get("args") or []) for command in reversed(meta.get("commands") or [])])
    names = []
    for raw_args in argument_sets:
        args = [str(arg) for arg in raw_args]
        if "-s" in args and args.index("-s") + 1 < len(args):
            names.append(args[args.index("-s") + 1])
        if "-deffnm" in args and args.index("-deffnm") + 1 < len(args):
            names.append(f"{args[args.index('-deffnm') + 1]}.tpr")
    for name in names:
        candidate = ensure_under_root(workdir / name, workdir)
        if candidate.suffix.lower() == ".tpr" and candidate.is_file():
            return candidate
    candidates = sorted(workdir.glob("*.tpr"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise RuntimeError("No generated TPR is available in this task yet.")
    return candidates[0]


def benchmark_threads(request: BenchmarkRequest, host: dict) -> list[int]:
    physical = int(host["physical_core_count"])
    logical = int(host["cpu_count"])
    threads = list(dict.fromkeys(request.threads)) or sorted({physical, max(physical, (physical + logical + 1) // 2), logical})
    if any(isinstance(value, bool) or value < 1 or value > logical for value in threads):
        raise ValueError(f"Benchmark thread counts must be between 1 and {logical}.")
    return threads


def benchmark_steps(gmx_bin: str, threads: list[int], request: BenchmarkRequest) -> list[Step]:
    profiles = []
    steps = []
    for ntomp in threads:
        stem = f"benchmark-1x{ntomp}"
        args = [
            gmx_bin,
            "mdrun",
            "-s",
            "benchmark.tpr",
            "-deffnm",
            stem,
            "-ntmpi",
            "1",
            "-ntomp",
            str(ntomp),
            "-nsteps",
            str(request.nsteps),
            "-noconfout",
            "-pin",
            request.pin,
        ]
        if request.gpu:
            args.extend(["-nb", "gpu"])
        profiles.append({"ntmpi": 1, "ntomp": ntomp, "log": f"{stem}.log"})
        steps.append(Step(f"Benchmark 1×{ntomp}", args, outputs=[f"{stem}.log"]))
    steps.append(
        Step(
            "Summarize benchmark",
            ["internal", "summarize_benchmarks"],
            outputs=["benchmark-results.json", "benchmark-report.txt"],
            kind="internal",
            operation="summarize_benchmarks",
            data={
                "profiles": profiles,
                "nsteps": request.nsteps,
                "json_output": "benchmark-results.json",
                "text_output": "benchmark-report.txt",
            },
        )
    )
    return steps


def create_benchmark(store, job_id: str, request: BenchmarkRequest, *, host: dict, default_gmx_bin: str) -> dict:
    """Queue a benchmark task that reruns the source TPR at several thread counts.

    Raises FileNotFoundError for a missing task or TPR, and OSError,
    RuntimeError or ValueError when the benchmark cannot be prepared.
    """
    with ExitStack() as locks:
        locks.enter_context(store.job_files_lock(job_id))
        source = store.get(job_id)
        if source.get("deletion_pending") or source.get("maintenance_pending"):
            raise RuntimeError("Source task files are under maintenance.")
        source_tpr = benchmark_tpr(store, source)
        threads = benchmark_threads(request, host)
        gmx_bin = str((source.get("params") or {}).get("gmx_bin") or default_gmx_bin)
        steps = benchmark_steps(gmx_bin, threads, request)
        params = {
            "workflow": "benchmark",
            "source_job_id": job_id,
            "ntmpi": 1,
            "ntomp": max(threads),
            "benchmark_threads": threads,
            "benchmark_nsteps": request.nsteps,
            "gpu": request.gpu,
            "pin": request.pin,
        }
        created = store.create(f"{source.get('name') or 'GROMACS'} benchmark", params, ["benchmark.tpr"], steps)
        try:
            locks.enter_context(store.job_files_lock(created["id"]))
            if store.get(created["id"])["status"] != "preparing":
                return store.get(created["id"])
            shutil.copy2(source_tpr, store.job_dir(created["id"]) / "benchmark.tpr")
            prepared = store.get(created["id"])
            manifest = write_experiment_manifest(prepared)
            store.update(
                created["id"],
                benchmark_source_job=job_id,
                experiment_manifest="experiment-manifest.json",
                input_hashes=manifest["input_files"],
            )
            if store.get(created["id"])["status"] != "preparing":
                return store.get(created["id"])
            store.start(created["id"], steps)
            return store.get(created["id"])
        except Exception as exc:
            if store.get(created["id"])["status"] == "cancelled":
                return store.get(created["id"])
            store.update(created["id"], status="failed", error=f"Failed to prepare benchmark: {exc}")
            raise
