from __future__ import annotations

import asyncio
import fcntl
import io
import json
import math
import os
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .analysis.comparison import compare_research_jobs, load_research_report
from .diagnostics import environment_diagnostics, host_information
from .existing_analysis import analysis_inputs, analysis_source, create_existing_analysis, source_file
from .files import (
    DEFAULT_LOG_TAIL_BYTES,
    MAX_LOG_TAIL_BYTES,
    _read_log_tail,
    _rewrite_uploaded_file_references,
    ensure_under_root,
    read_log_chunk,
    relative_files,
    stage_uploads,
    validate_uploaded_file_references,
)
from .gromacs import (
    WORKFLOWS,
    JobStore,
    Step,
    apply_mdp_overrides,
    apply_stdin_overrides,
    build_steps,
    copy_local_force_fields,
    detect_gromacs,
    discover_local_force_fields,
    force_fields_used_by_steps,
    load_settings,
    preview_mdp_overrides,
    preview_protocol,
    save_settings,
    slugify,
    steps_use_acpype,
    unconfirmed_acpype_ligands,
    utc_now,
    write_missing_mdp_files,
    xvg_plots,
)
from .models import BenchmarkRequest, ExistingAnalysisRequest, ProtocolPreviewRequest, ResearchComparisonRequest, SimulationParams
from .reproducibility import write_experiment_manifest

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
SETTINGS = load_settings(BASE_DIR)
RUNTIME_ROOT = Path(SETTINGS["runtime_root"]).resolve()
STORE = JobStore(RUNTIME_ROOT, max_parallel=int(SETTINGS.get("max_parallel", 1)))


FILE_LIST_CACHE: dict[str, tuple[str, list[dict], bool]] = {}
WEB_PROCESS_LOCK = None
MAX_FILE_LIST_ITEMS = 1000
SSE_LOG_CHUNK_BYTES = 64 * 1024

app = FastAPI(title="GROMACS Web UI", version="0.2.6")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def enforce_single_web_process() -> None:
    global WEB_PROCESS_LOCK
    if int(os.environ.get("WEB_CONCURRENCY", "1")) != 1:
        raise RuntimeError("GROMACS Web UI requires exactly one Web worker.")
    lock_path = RUNTIME_ROOT / "web.lock"
    WEB_PROCESS_LOCK = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(WEB_PROCESS_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        WEB_PROCESS_LOCK.close()
        WEB_PROCESS_LOCK = None
        raise RuntimeError("Another Web worker already owns this runtime directory; --workers > 1 is forbidden.") from exc


def release_web_process_lock() -> None:
    global WEB_PROCESS_LOCK
    if WEB_PROCESS_LOCK is not None:
        fcntl.flock(WEB_PROCESS_LOCK, fcntl.LOCK_UN)
        WEB_PROCESS_LOCK.close()
        WEB_PROCESS_LOCK = None


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    enforce_single_web_process()
    try:
        yield
    finally:
        release_web_process_lock()


app.router.lifespan_context = app_lifespan


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class SettingsPatch(BaseModel):
    gmx_bin: str | None = None
    max_parallel: int | None = None
    default_ntmpi: int | None = None
    default_ntomp: int | None = None


class CloneRequest(BaseModel):
    name: str | None = None


PreviewRequest = SimulationParams


def current_settings() -> dict:
    return load_settings(BASE_DIR)


def worker_status() -> dict:
    lock_path = RUNTIME_ROOT / "worker.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        worker_id = handle.read().strip() or None
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"available": True, "worker_id": worker_id}
        fcntl.flock(handle, fcntl.LOCK_UN)
        return {"available": False, "worker_id": worker_id}
    finally:
        handle.close()


def _positive_int(value: object, default: object) -> int:
    raw = default if value is None or value == "" else value
    try:
        number = int(raw)
    except (TypeError, ValueError):
        number = int(default or 1)
    return max(1, number)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_DIR / "templates" / "index.html")


@app.get("/api/health")
def health() -> dict:
    settings = current_settings()
    return {
        "settings": settings,
        "gromacs": detect_gromacs(settings["gmx_bin"]),
        "diagnostics": environment_diagnostics(BASE_DIR, settings),
        "local_force_fields": discover_local_force_fields(BASE_DIR),
        "worker": worker_status(),
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return current_settings()


@app.patch("/api/settings")
def patch_settings(patch: SettingsPatch) -> dict:
    data = patch.model_dump(exclude_none=True)
    if "max_parallel" in data:
        data["max_parallel"] = max(1, min(int(data["max_parallel"]), 16))
    if "gmx_bin" in data:
        data["gmx_bin"] = data["gmx_bin"].strip() or "gmx"
    settings = save_settings(BASE_DIR, data)
    STORE.configure(max_parallel=settings.get("max_parallel"))
    return settings


@app.get("/api/workflows")
def workflows() -> dict:
    return {"workflows": WORKFLOWS}


@app.post("/api/preview")
def preview(request: PreviewRequest) -> dict:
    settings = current_settings()
    params = request.model_dump()
    params["ntmpi"] = _positive_int(params.get("ntmpi"), settings.get("default_ntmpi", 1))
    params["ntomp"] = _positive_int(params.get("ntomp"), settings.get("default_ntomp", 4))
    validate_uploaded_file_references(params, request.files)
    try:
        mdp_overrides = preview_mdp_overrides(params)
        steps = apply_stdin_overrides(build_steps(params, request.files, BASE_DIR), params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "mdp_overrides": mdp_overrides,
        "commands": [
            {
                "title": step.title,
                "args": step.args,
                "outputs": step.outputs,
                "stdin": bool(step.stdin_text),
                "stdin_text": step.stdin_text or "",
                "kind": step.kind,
            }
            for step in steps
        ],
    }


@app.post("/api/protocol-preview")
def protocol_preview(request: ProtocolPreviewRequest) -> dict:
    try:
        return {"stages": preview_protocol(request.model_dump(exclude={"uploaded_mdp"}), BASE_DIR, request.uploaded_mdp)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/jobs")
def list_jobs(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str, Query(max_length=256)] = "",
) -> dict:
    return _job_page(limit, offset, search)


def _job_page(limit: int, offset: int, search: str) -> dict:
    search = search.strip()
    if search:
        labels = {
            "preparing": "准备输入中",
            "queued": "排队中",
            "running": "运行中",
            "completed": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
            "interrupted": "已中断",
            "protein_md": "蛋白 MD",
            "protein_ligand_md": "复合物 MD",
            "em_only": "能量最小化",
            "run_tpr": "运行 TPR",
            "analysis_rmsd": "RMSD 分析",
            "postprocess": "轨迹后处理",
            "analysis_suite": "分析套件",
            "benchmark": "性能基准",
            "custom": "自定义",
        }
        aliases = tuple(key for key, label in labels.items() if search.casefold() in label.casefold())
        page = STORE.list_page(limit=limit, offset=offset, search=search, search_aliases=aliases)
        return {**page, "search": search}
    return STORE.list_page(limit=limit, offset=offset)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    try:
        return STORE.get(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.post("/api/jobs")
async def create_job(
    params: Annotated[str | None, Form()] = None,
    files: Annotated[list[UploadFile], File()] = [],
) -> dict:
    try:
        request = SimulationParams.model_validate_json(params or "{}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    settings = current_settings()
    name = request.name.strip() or "GROMACS run"
    dry_run = request.dry_run
    job_params = request.model_dump(exclude={"name", "dry_run", "files"})
    job_params["gmx_bin"] = settings["gmx_bin"]
    job_params["ntmpi"] = _positive_int(job_params.get("ntmpi"), settings.get("default_ntmpi", 1))
    job_params["ntomp"] = _positive_int(job_params.get("ntomp"), settings.get("default_ntomp", 4))
    maxwarn = int(job_params.get("maxwarn") or 0)
    temp_root = RUNTIME_ROOT / "incoming"
    temp_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"pending-{slugify(name)}-", dir=temp_root))
    try:
        uploaded_names, upload_name_map = await stage_uploads(files, temp_dir)

        _rewrite_uploaded_file_references(job_params, upload_name_map)
        validate_uploaded_file_references(job_params, uploaded_names)
        preview_mdp_overrides(job_params)
        steps = apply_stdin_overrides(build_steps(job_params, uploaded_names, BASE_DIR), job_params)
        if not dry_run and maxwarn > 0:
            raise HTTPException(
                status_code=400,
                detail="Real simulations require maxwarn=0. Resolve every grompp warning instead of bypassing it.",
            )
        if not dry_run and steps_use_acpype(steps):
            unconfirmed = unconfirmed_acpype_ligands(steps)
            if unconfirmed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "ACPYPE ligand parameterization requires confirming ligand_charge before a real run. "
                        f"Unconfirmed ligand(s): {', '.join(unconfirmed)}. "
                        "Set the ligand net charge and tick ligand_charge_confirmed for each, or run Dry run first."
                    ),
                )
    except ValueError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    meta = STORE.create(name=name, params=job_params, uploaded_files=uploaded_names, steps=steps)
    if any(raw != safe for raw, safe in upload_name_map.items()):
        STORE.update(meta["id"], upload_name_map=upload_name_map)
    workdir = Path(meta["workdir"])
    try:
        for filename in uploaded_names:
            (temp_dir / filename).replace(workdir / filename)
    except OSError as exc:
        STORE.update(meta["id"], status="failed", error=f"Failed to stage uploaded files: {exc}")
        raise HTTPException(status_code=500, detail="Failed to stage uploaded files.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    try:
        written_mdp_templates = write_missing_mdp_files(workdir, BASE_DIR, job_params.get("workflow"))
    except (OSError, ValueError) as exc:
        STORE.update(meta["id"], status="failed", error=f"Failed to prepare MDP templates: {exc}")
        raise HTTPException(status_code=500, detail="Failed to prepare MDP templates.")
    if written_mdp_templates:
        STORE.update(meta["id"], mdp_templates=written_mdp_templates)
        for template in written_mdp_templates:
            STORE.append_log(meta["id"], f"[web] mdp template {template['name']} from {template['source']}\n")
    try:
        applied_mdp_overrides = apply_mdp_overrides(workdir, job_params)
    except (OSError, ValueError) as exc:
        STORE.update(meta["id"], status="failed", error=f"Failed to apply MDP overrides: {exc}")
        raise HTTPException(status_code=400, detail=f"Failed to apply MDP overrides: {exc}")
    if applied_mdp_overrides:
        STORE.update(meta["id"], mdp_overrides=applied_mdp_overrides)
        for item in applied_mdp_overrides:
            STORE.append_log(meta["id"], f"[web] mdp override {item['file']} {item['key']} = {item['value']} ({item['label']})\n")
    try:
        copied_force_fields = copy_local_force_fields(
            BASE_DIR,
            workdir,
            force_fields_used_by_steps(steps, job_params.get("force_field", "")),
        )
    except OSError as exc:
        STORE.update(meta["id"], status="failed", error=f"Failed to copy local force field: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to copy local force field: {exc}")
    if copied_force_fields:
        STORE.update(meta["id"], local_force_fields=copied_force_fields)
        for force_field in copied_force_fields:
            STORE.append_log(meta["id"], f"[web] local force field {force_field['directory']} {force_field['status']}\n")
    STORE.start(meta["id"], steps, dry_run=dry_run)
    prepared = STORE.get(meta["id"])
    try:
        manifest = write_experiment_manifest(prepared)
        STORE.update(meta["id"], experiment_manifest="experiment-manifest.json", input_hashes=manifest["input_files"])
    except OSError as exc:
        STORE.append_log(meta["id"], f"[web] warning: failed to write experiment manifest: {exc}\n")
    return STORE.get(meta["id"])


@app.get("/api/jobs/{job_id}/research")
def research_report(job_id: str) -> dict:
    try:
        return load_research_report(STORE, job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="尚无研究分析报告，请先创建研究分析任务。")
    except (OSError, ValueError):
        raise HTTPException(status_code=409, detail="研究报告尚未就绪或文件无效。")


@app.get("/api/research/jobs")
def research_jobs() -> dict:
    page = STORE.list_page(limit=200)
    jobs = []
    for job in page["jobs"]:
        path = STORE.job_dir(job["id"]) / "research-report.json"
        if job["status"] == "completed" and path.is_file() and not path.is_symlink():
            jobs.append({"id": job["id"], "name": job["name"]})
    return {"jobs": jobs, "truncated": page["has_more"]}


@app.post("/api/research/compare")
def research_comparison(request: ResearchComparisonRequest) -> dict:
    try:
        return compare_research_jobs(STORE, request)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="所选任务尚无研究分析报告。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError:
        raise HTTPException(status_code=409, detail="无法读取研究分析报告。")


@app.get("/api/jobs/{job_id}/analysis-inputs")
def existing_analysis_inputs(job_id: str) -> dict:
    try:
        return analysis_inputs(STORE, job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except (OSError, ValueError):
        raise HTTPException(status_code=409, detail="无法读取分析输入，请确认来源任务已完成且文件可用。")


@app.get("/api/jobs/{job_id}/analysis-index-groups")
def analysis_index_groups(job_id: str, file: Annotated[str, Query(min_length=1, max_length=1024)]) -> dict:
    try:
        analysis_source(STORE, job_id)
        path = source_file(STORE.job_dir(job_id), file, {".ndx"})
        if path.stat().st_size > 20_000_000:
            raise ValueError("索引文件超过 20 MB 上限。")
        groups = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split(";", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                groups.append({"name": line[1:-1].strip(), "atoms": 0})
                if len(groups) > 1000:
                    raise ValueError("索引组数量超过 1000。")
            elif line and groups:
                groups[-1]["atoms"] += len(line.split())
        return {"groups": groups}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="来源任务或索引文件不存在。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError:
        raise HTTPException(status_code=409, detail="无法读取索引文件。")


@app.post("/api/jobs/{job_id}/analysis")
def analyze_existing_job(job_id: str, request: ExistingAnalysisRequest) -> dict:
    try:
        return create_existing_analysis(STORE, job_id, request, current_settings())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job or analysis input not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (OSError, RuntimeError):
        raise HTTPException(status_code=409, detail="无法准备分析输入，请检查文件及可用磁盘空间。")


@app.post("/api/jobs/{job_id}/clone")
def clone_job(job_id: str, request: CloneRequest | None = None) -> dict:
    try:
        cloned = STORE.clone(job_id, request.name if request else None)
        manifest = write_experiment_manifest(cloned)
        return STORE.update(cloned["id"], experiment_manifest="experiment-manifest.json", input_hashes=manifest["input_files"])
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _benchmark_tpr(meta: dict) -> Path:
    workdir = STORE.job_dir(str(meta["id"]))
    argument_sets = [list(meta.get("process_args") or [])]
    argument_sets.extend([list(command.get("args") or []) for command in reversed(meta.get("commands") or [])])
    names = []
    for args in argument_sets:
        args = [str(arg) for arg in args]
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


@app.post("/api/jobs/{job_id}/benchmark")
def benchmark_job(job_id: str, request: BenchmarkRequest | None = None) -> dict:
    request = request or BenchmarkRequest()
    try:
        source = STORE.get(job_id)
        source_tpr = _benchmark_tpr(source)
        host = host_information()
        physical = int(host["physical_core_count"])
        logical = int(host["cpu_count"])
        threads = list(dict.fromkeys(request.threads)) or sorted({physical, max(physical, (physical + logical + 1) // 2), logical})
        if any(isinstance(value, bool) or value < 1 or value > logical for value in threads):
            raise ValueError(f"Benchmark thread counts must be between 1 and {logical}.")
        profiles = []
        steps = []
        gmx_bin = str((source.get("params") or {}).get("gmx_bin") or current_settings()["gmx_bin"])
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
        created = STORE.create(f"{source.get('name') or 'GROMACS'} benchmark", params, ["benchmark.tpr"], steps)
        try:
            shutil.copy2(source_tpr, STORE.job_dir(created["id"]) / "benchmark.tpr")
            STORE.start(created["id"], steps)
            prepared = STORE.get(created["id"])
            manifest = write_experiment_manifest(prepared)
            return STORE.update(
                created["id"],
                benchmark_source_job=job_id,
                experiment_manifest="experiment-manifest.json",
                input_hashes=manifest["input_files"],
            )
        except Exception as exc:
            STORE.update(created["id"], status="failed", error=f"Failed to prepare benchmark: {exc}")
            raise
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job or TPR not found")
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _flatten_params(value: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(value, dict):
        return {prefix: value}
    flattened = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        flattened.update(_flatten_params(item, path))
    return flattened


@app.get("/api/jobs/{job_id}/diff/{other_job_id}")
def compare_jobs(job_id: str, other_job_id: str) -> dict:
    try:
        left = STORE.get(job_id)
        right = STORE.get(other_job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    left_params = _flatten_params(left.get("params") or {})
    right_params = _flatten_params(right.get("params") or {})
    keys = sorted(set(left_params) | set(right_params))
    differences = [
        {"path": key, "left": left_params.get(key), "right": right_params.get(key)}
        for key in keys
        if left_params.get(key) != right_params.get(key)
    ]
    return {
        "left": {"id": left["id"], "name": left.get("name")},
        "right": {"id": right["id"], "name": right.get("name")},
        "differences": differences,
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    try:
        return STORE.cancel(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.post("/api/jobs/{job_id}/adopt")
def adopt_job(job_id: str) -> dict:
    try:
        return STORE.adopt(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/jobs/{job_id}/resume-checkpoint")
def resume_job_from_checkpoint(job_id: str) -> dict:
    try:
        return STORE.resume_from_checkpoint(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/jobs/{job_id}/retry-step")
def retry_failed_step(job_id: str) -> dict:
    try:
        return STORE.retry_failed_step(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/api/jobs/{job_id}/skip-step")
def skip_failed_step(job_id: str) -> dict:
    try:
        return STORE.skip_failed_step(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    try:
        meta = STORE.delete(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    FILE_LIST_CACHE.pop(job_id, None)
    return {"deleted": True, "job": meta}


@app.get("/api/jobs/{job_id}/log")
def job_log(
    job_id: str,
    tail: Annotated[int, Query(ge=0, le=MAX_LOG_TAIL_BYTES)] = DEFAULT_LOG_TAIL_BYTES,
    offset: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_TAIL_BYTES)] = SSE_LOG_CHUNK_BYTES,
):
    try:
        STORE.get(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    path = STORE.log_path(job_id)
    if offset is not None:
        return read_log_chunk(path, offset, limit)
    return PlainTextResponse(_read_log_tail(path, tail))


def _sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.get("/api/events")
async def job_events(
    request: Request,
    job_id: Annotated[str | None, Query()] = None,
    log_offset: Annotated[int, Query(ge=0)] = 0,
    log_tail: Annotated[int, Query(ge=0, le=MAX_LOG_TAIL_BYTES)] = 0,
    jobs_offset: Annotated[int, Query(ge=0)] = 0,
    jobs_search: Annotated[str, Query(max_length=256)] = "",
) -> StreamingResponse:
    if job_id:
        try:
            STORE.get(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Job not found")

    async def stream():
        jobs_signature = ""
        job_signature = ""
        current_offset = log_offset
        first_log_read = True
        heartbeat_at = 0.0
        while not await request.is_disconnected():
            page = _job_page(50, jobs_offset, jobs_search)
            signature = json.dumps(page, ensure_ascii=False, sort_keys=True)
            if signature != jobs_signature:
                jobs_signature = signature
                yield _sse_event("jobs", page)
            if job_id:
                try:
                    detail = STORE.get(job_id)
                except FileNotFoundError:
                    yield _sse_event("deleted", {"id": job_id})
                    return
                signature = f"{detail.get('updated_at')}:{detail.get('status')}:{detail.get('step_index')}"
                if signature != job_signature:
                    job_signature = signature
                    yield _sse_event("job", detail)
                log_path = STORE.log_path(job_id)
                if first_log_read:
                    first_log_read = False
                    if log_tail > 0:
                        # Fresh panel open: jump to the tail instead of streaming
                        # a multi-MB log from byte 0 (which used to take minutes).
                        size = log_path.stat().st_size if log_path.exists() else 0
                        current_offset = max(0, size - int(log_tail))
                        chunk = read_log_chunk(log_path, current_offset, int(log_tail))
                    else:
                        chunk = read_log_chunk(log_path, current_offset, SSE_LOG_CHUNK_BYTES)
                else:
                    chunk = read_log_chunk(log_path, current_offset, SSE_LOG_CHUNK_BYTES)
                if chunk["reset"] or chunk["text"]:
                    current_offset = int(chunk["offset"])
                    yield _sse_event("log", chunk)
            now = asyncio.get_running_loop().time()
            if now >= heartbeat_at:
                heartbeat_at = now + 15
                yield _sse_event("ping", {"time": utc_now()})
            await asyncio.sleep(0.75)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/files")
def job_files(job_id: str) -> dict:
    try:
        meta = STORE.get(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    cache_key = f"{meta.get('status')}:{meta.get('step_index')}"
    cached = FILE_LIST_CACHE.get(job_id)
    if cached and cached[0] == cache_key:
        return {"files": cached[1], "truncated": cached[2]}
    scanned = relative_files(Path(meta["workdir"]), max_files=MAX_FILE_LIST_ITEMS + 1)
    truncated = len(scanned) > MAX_FILE_LIST_ITEMS
    files = scanned[:MAX_FILE_LIST_ITEMS]
    FILE_LIST_CACHE[job_id] = (cache_key, files, truncated)
    return {"files": files, "truncated": truncated}


@app.get("/api/jobs/{job_id}/plots")
def job_plots(
    job_id: str,
    replica: int | None = None,
    metric: str | None = None,
    begin_ns: float | None = None,
    end_ns: float | None = None,
) -> dict:
    try:
        meta = STORE.get(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    if replica is not None and not 1 <= replica <= 16:
        raise HTTPException(status_code=400, detail="Replica must be between 1 and 16")
    for value in (begin_ns, end_ns):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise HTTPException(status_code=400, detail="Time bounds must be finite and nonnegative")
    if begin_ns is not None and end_ns is not None and begin_ns >= end_ns:
        raise HTTPException(status_code=400, detail="End time must exceed start time")
    return xvg_plots(Path(meta["workdir"]), replica=replica, metric=metric, begin_ns=begin_ns, end_ns=end_ns)


@app.get("/api/jobs/{job_id}/plots/download")
def download_plot_data(job_id: str) -> Response:
    try:
        meta = STORE.get(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    workdir = Path(meta["workdir"]).resolve()
    files = []
    total_bytes = 0
    for candidate in sorted(workdir.rglob("*.xvg")):
        if len(files) >= 100:
            break
        try:
            path = ensure_under_root(candidate, workdir)
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if total_bytes + size > 100 * 1024 * 1024:
            break
        files.append(path)
        total_bytes += size
    if not files:
        raise HTTPException(status_code=404, detail="No XVG plot data found for this job")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(workdir).as_posix())
    filename = f"{meta.get('directory_name') or job_id}-xvg.zip"
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/jobs/{job_id}/download")
def download_file(job_id: str, path: str) -> FileResponse:
    try:
        meta = STORE.get(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    workdir = Path(meta["workdir"])
    try:
        target = ensure_under_root(workdir / path, workdir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name)
