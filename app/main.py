from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import math
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from .agent_api import agent_router
from .analysis.comparison import compare_research_jobs, load_research_report
from .analysis_stream import analysis_preparation_events
from .api_errors import error_advice
from .benchmark import create_benchmark
from .diagnostics import environment_diagnostics, host_information
from .existing_analysis import analysis_inputs, analysis_source, create_existing_analysis, preflight_existing_analysis, source_file
from .files import (
    DEFAULT_LOG_TAIL_BYTES,
    MAX_LOG_TAIL_BYTES,
    _read_log_tail,
    _rewrite_uploaded_file_references,
    ensure_under_root,
    read_log_chunk,
    stage_uploads,
    validate_uploaded_file_references,
)
from .gromacs import (
    WORKFLOWS,
    JobStore,
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
from .job_file_listing import (  # noqa: F401 - caches and limits are re-exported for tests
    ACTIVE_FILE_CACHE_SECONDS,
    FILE_LIST_CACHE,
    FILE_PAGE_CACHE,
    MAX_FILE_LIST_ITEMS,
    forget_file_lists,
    list_job_files,
    xvg_archive,
)
from .models import BenchmarkRequest, ExistingAnalysisRequest, ProtocolPreviewRequest, ResearchComparisonRequest, SimulationParams
from .product_api import product_router
from .progress import enrich_progress
from .reproducibility import write_experiment_manifest
from .storage import CleanupRequest, execute_cleanup, preview_cleanup, storage_overview
from .submissions import prepare_submission, submission_status, validate_submission_id
from .worker_health import worker_status as read_worker_status

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
SETTINGS = load_settings(BASE_DIR)
RUNTIME_ROOT = Path(SETTINGS["runtime_root"]).resolve()
STORE = JobStore(
    RUNTIME_ROOT, max_parallel=int(SETTINGS.get("max_parallel", 1)), analysis_parallel=int(SETTINGS.get("analysis_parallel", 2))
)


WEB_PROCESS_LOCK = None
SSE_LOG_CHUNK_BYTES = 64 * 1024

app = FastAPI(title="GROMACS Web UI", version="0.2.11")
# Capture the running process identity once; files may be replaced by an update.
# These public identifiers distinguish desktop instances, not authentication.
RUNTIME_IDENTITY = {
    "version": app.version,
    "instance_id": os.environ.get("GROMACS_CONSOLE_INSTANCE_ID", ""),
    "installation_sha256": os.environ.get("GROMACS_CONSOLE_INSTALLATION_SHA256", ""),
}
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


def recover_web_preparations() -> None:
    """Called only after acquiring the single Web owner lock, before serving requests."""
    STORE.recover_preparing_jobs()
    # Only Web uploads use pending-* directories. Never follow a replaced
    # incoming directory or remove unrelated user files from the runtime.
    runtime_fd = os.open(RUNTIME_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            incoming_fd = os.open("incoming", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=runtime_fd)
        except FileNotFoundError:
            return
        except OSError:
            logging.getLogger(__name__).warning("Skipped unsafe or inaccessible upload staging directory")
            return
    finally:
        os.close(runtime_fd)
    try:
        for name in os.listdir(incoming_fd):
            if not name.startswith("pending-"):
                continue
            try:
                info = os.stat(name, dir_fd=incoming_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(name, dir_fd=incoming_fd)
            except OSError:
                logging.getLogger(__name__).warning("Could not remove an interrupted upload staging directory")
    finally:
        os.close(incoming_fd)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    enforce_single_web_process()
    try:
        await run_in_threadpool(recover_web_preparations)
        yield
    finally:
        release_web_process_lock()


app.router.lifespan_context = app_lifespan


@app.exception_handler(HTTPException)
async def explained_http_error(request: Request, exc: HTTPException):
    if exc.status_code == 422 and isinstance(exc.detail, list):
        return validation_error_response(exc.detail, headers=exc.headers)
    advice = exc.detail if isinstance(exc.detail, dict) and "code" in exc.detail else error_advice(str(exc.detail))
    return JSONResponse(
        status_code=exc.status_code, content=jsonable_encoder({"detail": exc.detail, "problem": advice}), headers=exc.headers
    )


def validation_error_response(errors: Sequence[Any], *, headers: dict | None = None) -> JSONResponse:
    # Pydantic inputs/contexts can contain private data or non-finite numbers
    # which JSONResponse cannot encode. Field location and reason are enough.
    details = [{key: error[key] for key in ("type", "loc", "msg") if key in error} for error in errors]
    return JSONResponse(
        status_code=422,
        content={"detail": details, "problem": {"code": "invalid_request", "message": "请检查标出的参数。"}},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def explained_validation_error(request: Request, exc: RequestValidationError):
    return validation_error_response(exc.errors())


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = None
    if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            require_storage_origin(request)
        except HTTPException as exc:
            response = await explained_http_error(request, exc)
    limited = (
        request.url.path == "/api/templates" or request.url.path.endswith("/organization") or request.url.path.startswith("/api/agent/")
    )
    if response is None and limited and request.method in {"POST", "PUT", "PATCH"}:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": {"code": "request_too_large", "message": "请求超过 256 KiB。"}},
                )
                break
        if response is None:
            request._body = bytes(body)
    if response is None:
        response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/agent/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class SettingsPatch(BaseModel):
    gmx_bin: str | None = None
    max_parallel: int | None = None
    analysis_parallel: int | None = None
    default_ntmpi: int | None = None
    default_ntomp: int | None = None


class CloneRequest(BaseModel):
    name: str | None = None


PreviewRequest = SimulationParams


def current_settings() -> dict:
    return load_settings(BASE_DIR)


def worker_status() -> dict:
    return read_worker_status(RUNTIME_ROOT)


@app.get("/api/worker")
def get_worker_status() -> dict:
    return worker_status()


def _positive_int(value: Any, default: Any) -> int:
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


@app.get("/api/version")
def runtime_version() -> dict:
    return dict(RUNTIME_IDENTITY)


@app.get("/api/settings")
def get_settings() -> dict:
    return current_settings()


@app.patch("/api/settings")
def patch_settings(patch: SettingsPatch) -> dict:
    data = patch.model_dump(exclude_none=True)
    for key in ("max_parallel", "analysis_parallel"):
        if key in data:
            data[key] = max(1, min(int(data[key]), 16))
    if "gmx_bin" in data:
        data["gmx_bin"] = data["gmx_bin"].strip() or "gmx"
    settings = save_settings(BASE_DIR, data)
    STORE.configure(max_parallel=settings.get("max_parallel"), analysis_parallel=settings.get("analysis_parallel"))
    return settings


@app.get("/api/workflows")
def workflows() -> dict:
    return {"workflows": WORKFLOWS}


@app.get("/api/storage")
def get_storage(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    refresh: bool = False,
    search: Annotated[str, Query(max_length=255)] = "",
) -> dict:
    try:
        return storage_overview(STORE, limit=limit, offset=offset, refresh=refresh, search=search)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="无法安全扫描运行目录，请检查目录与权限。") from exc


@app.post("/api/storage/preview")
def storage_preview(request: CleanupRequest, http_request: Request) -> dict:
    require_storage_origin(http_request)
    try:
        return preview_cleanup(STORE, request)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/storage/cleanup")
def storage_cleanup(request: CleanupRequest, http_request: Request) -> dict:
    require_storage_origin(http_request)
    try:
        result = execute_cleanup(STORE, request)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    forget_file_lists(STORE, [*result["deleted_jobs"], *result["cleaned_jobs"]])
    return result


def require_storage_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="此操作仅允许从本机控制台发起。")
    if origin:
        try:
            parsed = urlsplit(origin)
            hostname = parsed.hostname
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="请求来源无效。") from exc
        if hostname not in {"localhost", "127.0.0.1", "::1"} or origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="请求来源与本机控制台不一致。")


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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _job_or_404(job_id: str) -> dict:
    try:
        return STORE.get(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.get("/api/jobs")
def list_jobs(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str, Query(max_length=256)] = "",
    project: Annotated[str, Query(max_length=80)] = "",
) -> dict:
    return _job_page(limit, offset, search, project)


def _job_page(limit: int, offset: int, search: str, project: str = "") -> dict:
    options = _job_page_options(limit, offset, search, project)
    page = STORE.list_page(**options)
    return {**page, "search": options["search"]} if options.get("search") else page


def _job_page_options(limit: int, offset: int, search: str, project: str = "") -> dict:
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
        return {"limit": limit, "offset": offset, "search": search, "search_aliases": aliases, "project": project}
    return {"limit": limit, "offset": offset, "project": project}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    try:
        detail = enrich_progress(STORE.get(job_id))
        if detail.get("error"):
            detail["failure_advice"] = error_advice(detail["error"])
        return detail
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/jobs")
async def create_job(
    params: Annotated[str | None, Form()] = None,
    files: Annotated[list[UploadFile], File()] = [],
    submission_id: Annotated[str | None, Form()] = None,
) -> dict:
    if submission_id is not None:
        validate_submission_id(submission_id)
    try:
        request = SimulationParams.model_validate_json(params or "{}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    if submission_id:
        previous = await run_in_threadpool(submission_status, STORE, submission_id)
        if previous["state"] == "cancelled":
            raise HTTPException(status_code=409, detail="本次上传已取消，请重新提交。")
    name = request.name.strip() or "GROMACS run"
    temp_root = RUNTIME_ROOT / "incoming"
    temp_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"pending-{slugify(name)}-", dir=temp_root))
    prepared_hashes: dict[str, dict] = {}
    try:
        uploaded_names, upload_name_map = await stage_uploads(files, temp_dir, hashes=prepared_hashes)
        if submission_id:
            previous = await run_in_threadpool(
                prepare_submission, STORE, submission_id, request.model_dump(), prepared_hashes, upload_name_map
            )
            if previous["state"] == "submitted":
                return previous["job"]
            if previous["state"] == "deleted":
                raise HTTPException(status_code=409, detail="本次提交的任务已被删除，不会重复创建。")
        return await run_in_threadpool(
            _prepare_uploaded_job, request, temp_dir, uploaded_names, upload_name_map, prepared_hashes, submission_id
        )
    finally:
        await run_in_threadpool(shutil.rmtree, temp_dir, True)


def _prepare_uploaded_job(
    request: SimulationParams,
    temp_dir: Path,
    uploaded_names: list[str],
    upload_name_map: dict[str, str],
    prepared_hashes: dict[str, dict],
    submission_id: str | None = None,
) -> dict:
    settings = current_settings()
    name = request.name.strip() or "GROMACS run"
    dry_run = request.dry_run
    job_params = request.model_dump(exclude={"name", "dry_run", "files"})
    job_params["gmx_bin"] = settings["gmx_bin"]
    job_params["ntmpi"] = _positive_int(job_params.get("ntmpi"), settings.get("default_ntmpi", 1))
    job_params["ntomp"] = _positive_int(job_params.get("ntomp"), settings.get("default_ntomp", 4))
    maxwarn = int(job_params.get("maxwarn") or 0)
    try:
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    try:
        meta = STORE.create(
            name=name,
            params=job_params,
            uploaded_files=uploaded_names,
            steps=steps,
            **({"submission_id": submission_id} if submission_id else {}),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        with STORE.job_files_lock(meta["id"]):
            if STORE.get(meta["id"])["status"] != "preparing":
                return STORE.get(meta["id"])
            if any(raw != safe for raw, safe in upload_name_map.items()):
                STORE.update(meta["id"], upload_name_map=upload_name_map)
            workdir = Path(meta["workdir"])
            try:
                for filename in uploaded_names:
                    (temp_dir / filename).replace(workdir / filename)
            except OSError as exc:
                STORE.update(meta["id"], status="failed", error=f"Failed to stage uploaded files: {exc}")
                raise HTTPException(status_code=500, detail="Failed to stage uploaded files.") from exc
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            try:
                written_mdp_templates = write_missing_mdp_files(workdir, BASE_DIR, job_params.get("workflow"))
            except (OSError, ValueError) as exc:
                STORE.update(meta["id"], status="failed", error=f"Failed to prepare MDP templates: {exc}")
                raise HTTPException(status_code=500, detail="Failed to prepare MDP templates.") from exc
            if written_mdp_templates:
                STORE.update(meta["id"], mdp_templates=written_mdp_templates)
                for template in written_mdp_templates:
                    STORE.append_log(meta["id"], f"[web] mdp template {template['name']} from {template['source']}\n")
            try:
                applied_mdp_overrides = apply_mdp_overrides(workdir, job_params)
            except (OSError, ValueError) as exc:
                STORE.update(meta["id"], status="failed", error=f"Failed to apply MDP overrides: {exc}")
                raise HTTPException(status_code=400, detail=f"Failed to apply MDP overrides: {exc}") from exc
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
                raise HTTPException(status_code=500, detail=f"Failed to copy local force field: {exc}") from exc
            if copied_force_fields:
                STORE.update(meta["id"], local_force_fields=copied_force_fields)
                for force_field in copied_force_fields:
                    STORE.append_log(meta["id"], f"[web] local force field {force_field['directory']} {force_field['status']}\n")
            prepared = STORE.get(meta["id"])
            try:
                manifest = write_experiment_manifest(prepared, prepared_hashes=prepared_hashes)
                STORE.update(meta["id"], experiment_manifest="experiment-manifest.json", input_hashes=manifest["input_files"])
            except OSError as exc:
                STORE.update(meta["id"], status="failed", error=f"Failed to prepare experiment manifest: {exc}")
                raise HTTPException(status_code=500, detail="Failed to prepare experiment manifest; task was not queued.") from exc
            if STORE.get(meta["id"])["status"] != "preparing":
                return STORE.get(meta["id"])
            STORE.start(meta["id"], steps, dry_run=dry_run)
            return STORE.get(meta["id"])

    except sqlite3.IntegrityError:
        # Cancellation may race the final preparing -> queued transition.
        current = STORE.get(meta["id"])
        if current["status"] == "cancelled":
            return current
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/submissions/{request_id}")
def get_submission(request_id: str) -> dict:
    return submission_status(STORE, request_id)


@app.post("/api/submissions/{request_id}/resolve")
def resolve_submission(request_id: str) -> dict:
    """Seal an interrupted submission, or return its already-created task."""
    return submission_status(STORE, request_id, cancel=True)


@app.get("/api/jobs/{job_id}/research")
def research_report(job_id: str) -> dict:
    try:
        return load_research_report(STORE, job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="尚无研究分析报告，请先创建研究分析任务。") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="研究报告尚未就绪或文件无效。") from exc


@app.get("/api/research/jobs")
def research_jobs(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str, Query(max_length=256)] = "",
) -> dict:
    page = STORE.research_page(limit, offset, search)
    return {**page, "truncated": page["has_more"]}


@app.post("/api/research/compare")
def research_comparison(request: ResearchComparisonRequest) -> dict:
    try:
        return compare_research_jobs(STORE, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="所选任务尚无研究分析报告。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=409, detail="无法读取研究分析报告。") from exc


@app.get("/api/jobs/{job_id}/analysis-inputs")
def existing_analysis_inputs(job_id: str) -> dict:
    try:
        return analysis_inputs(STORE, job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="无法读取分析输入，请确认来源任务已完成且文件可用。") from exc


@app.get("/api/jobs/{job_id}/analysis-index-groups")
def analysis_index_groups(job_id: str, file: Annotated[str, Query(min_length=1, max_length=1024)]) -> dict:
    try:
        analysis_source(STORE, job_id)
        path = source_file(STORE.job_dir(job_id), file, {".ndx"})
        if path.stat().st_size > 20_000_000:
            raise ValueError("索引文件超过 20 MB 上限。")
        groups: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split(";", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                groups.append({"name": line[1:-1].strip(), "atoms": 0})
                if len(groups) > 1000:
                    raise ValueError("索引组数量超过 1000。")
            elif line and groups:
                groups[-1]["atoms"] += len(line.split())
        return {"groups": groups}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="来源任务或索引文件不存在。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=409, detail="无法读取索引文件。") from exc


@app.post("/api/jobs/{job_id}/analysis")
def analyze_existing_job(job_id: str, request: ExistingAnalysisRequest) -> dict:
    try:
        return create_existing_analysis(STORE, job_id, request, current_settings())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job or analysis input not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail="无法准备分析输入，请检查文件及可用磁盘空间。") from exc


@app.post("/api/jobs/{job_id}/analysis-preflight")
def analysis_preflight(job_id: str, request: ExistingAnalysisRequest) -> dict:
    try:
        return preflight_existing_analysis(STORE, job_id, request, current_settings())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="来源任务或分析输入不存在。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail="无法预检分析输入，请确认来源任务已完成且文件可用。") from exc


@app.post("/api/jobs/{job_id}/analysis-stream")
def analyze_existing_job_stream(job_id: str, request: ExistingAnalysisRequest) -> StreamingResponse:
    return StreamingResponse(
        analysis_preparation_events(STORE, job_id, request, current_settings()),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jobs/{job_id}/clone")
def clone_job(job_id: str, request: CloneRequest | None = None) -> dict:
    try:
        return STORE.clone(job_id, request.name if request else None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/benchmark")
def benchmark_job(job_id: str, request: BenchmarkRequest | None = None) -> dict:
    try:
        return create_benchmark(
            STORE,
            job_id,
            request or BenchmarkRequest(),
            host=host_information(),
            default_gmx_bin=current_settings()["gmx_bin"],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job or TPR not found") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
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
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.post("/api/jobs/{job_id}/adopt")
def adopt_job(job_id: str) -> dict:
    try:
        return STORE.adopt(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/resume-checkpoint")
def resume_job_from_checkpoint(job_id: str) -> dict:
    try:
        return STORE.resume_from_checkpoint(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/retry-step")
def retry_failed_step(job_id: str) -> dict:
    try:
        return STORE.retry_failed_step(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/skip-step")
def skip_failed_step(job_id: str) -> dict:
    try:
        return STORE.skip_failed_step(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    try:
        meta = STORE.delete(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    forget_file_lists(STORE, [job_id])
    return {"deleted": True, "job": meta}


@app.get("/api/jobs/{job_id}/log")
def job_log(
    job_id: str,
    tail: Annotated[int, Query(ge=0, le=MAX_LOG_TAIL_BYTES)] = DEFAULT_LOG_TAIL_BYTES,
    offset: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_TAIL_BYTES)] = SSE_LOG_CHUNK_BYTES,
):
    _job_or_404(job_id)
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
    jobs_project: Annotated[str, Query(max_length=80)] = "",
) -> StreamingResponse:
    if job_id:
        try:
            await run_in_threadpool(STORE.get, job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    async def stream():
        jobs_signature = ""
        job_signature = ""
        current_offset = log_offset
        first_log_read = True
        heartbeat_at = 0.0
        page_options = _job_page_options(50, jobs_offset, jobs_search, jobs_project)
        while not await request.is_disconnected():
            payload, signature = await run_in_threadpool(STORE.list_page_snapshot, **page_options)
            if signature != jobs_signature:
                jobs_signature = signature
                yield f"event: jobs\ndata: {payload}\n\n"
            if job_id:
                try:
                    detail = await run_in_threadpool(get_job, job_id)
                except (FileNotFoundError, HTTPException):
                    yield _sse_event("deleted", {"id": job_id})
                    return
                signature = json.dumps(
                    [
                        detail.get("updated_at"),
                        detail.get("status"),
                        detail.get("step_index"),
                        detail.get("simulation_progress"),
                        detail.get("progress_observation"),
                    ],
                    sort_keys=True,
                )
                if signature != job_signature:
                    job_signature = signature
                    yield _sse_event("job", detail)
                log_path = STORE.log_path(job_id)
                if first_log_read:
                    first_log_read = False
                    if log_tail > 0:
                        # Fresh panel open: jump to the tail instead of streaming
                        # a multi-MB log from byte 0 (which used to take minutes).
                        size = await run_in_threadpool(lambda: log_path.stat().st_size if log_path.exists() else 0)
                        current_offset = max(0, size - int(log_tail))
                        chunk = await run_in_threadpool(read_log_chunk, log_path, current_offset, int(log_tail))
                    else:
                        chunk = await run_in_threadpool(read_log_chunk, log_path, current_offset, SSE_LOG_CHUNK_BYTES)
                else:
                    chunk = await run_in_threadpool(read_log_chunk, log_path, current_offset, SSE_LOG_CHUNK_BYTES)
                if chunk["reset"] or chunk["text"]:
                    current_offset = int(chunk["offset"])
                    yield _sse_event("log", chunk)
            now = asyncio.get_running_loop().time()
            if now >= heartbeat_at:
                heartbeat_at = now + 15
                yield _sse_event("worker", await run_in_threadpool(worker_status))
                yield _sse_event("ping", {"time": utc_now()})
            await asyncio.sleep(0.75)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/files")
def job_files(
    job_id: str,
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    search: Annotated[str, Query(max_length=256)] = "",
) -> dict:
    meta = _job_or_404(job_id)
    return list_job_files(STORE, job_id, meta, limit=limit, offset=offset, search=search)


@app.get("/api/jobs/{job_id}/plots")
def job_plots(
    job_id: str,
    replica: int | None = None,
    metric: str | None = None,
    begin_ns: float | None = None,
    end_ns: float | None = None,
) -> dict:
    meta = _job_or_404(job_id)
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
    meta = _job_or_404(job_id)
    content = xvg_archive(Path(meta["workdir"]))
    if content is None:
        raise HTTPException(status_code=404, detail="No XVG plot data found for this job")
    filename = f"{meta.get('directory_name') or job_id}-xvg.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/jobs/{job_id}/download")
def download_file(job_id: str, path: str) -> FileResponse:
    meta = _job_or_404(job_id)
    workdir = Path(meta["workdir"])
    try:
        target = ensure_under_root(workdir / path, workdir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name)


app.include_router(product_router(lambda: STORE, current_settings, require_storage_origin, BASE_DIR))
app.include_router(
    agent_router(lambda: STORE, current_settings, require_storage_origin, BASE_DIR, lambda: read_worker_status(STORE.runtime_root))
)
