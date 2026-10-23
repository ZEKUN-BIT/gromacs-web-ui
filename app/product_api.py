"""Local product features kept separate from simulation execution routes."""

from __future__ import annotations

import platform
import re
from pathlib import Path
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from .catalog import JobOrganization, ProtocolTemplate, list_templates, project_catalog, save_template
from .demo import create_example
from .diagnostics import environment_diagnostics
from .gromacs import detect_gromacs, utc_now
from .models import SimulationParams
from .result_bundle import build_result_bundle, quality_summary


def product_router(get_store: Callable, get_settings: Callable, check_origin: Callable, base_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(check_origin)])

    @router.get("/product")
    def catalog():
        settings = get_settings()
        defaults = SimulationParams(ntmpi=settings.get("default_ntmpi", 1), ntomp=settings.get("default_ntomp", 4)).model_dump()
        return {
            "templates": list_templates(get_store()),
            "projects": project_catalog(get_store()),
            "defaults": defaults,
            "schema_version": 1,
        }

    @router.post("/templates")
    def save_protocol(template: ProtocolTemplate):
        try:
            return save_template(get_store(), template)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "invalid_template", "message": str(exc)}) from exc

    @router.delete("/templates/{template_id}")
    def delete_protocol(template_id: str):
        with get_store()._connect() as connection:
            cursor = connection.execute("DELETE FROM protocol_templates WHERE id=?", (template_id,))
            if cursor.rowcount == 0:
                raise HTTPException(404, detail="模板不存在。")
        return {"deleted": template_id}

    @router.patch("/jobs/{job_id}/organization")
    def organize_job(job_id: str, organization: JobOrganization):
        try:
            return get_store().update(job_id, **organization.model_dump())
        except FileNotFoundError as exc:
            raise HTTPException(404, detail="任务不存在。") from exc
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/summary")
    def result_summary(job_id: str):
        store = get_store()
        try:
            with store.job_files_lock(job_id):
                meta = store.get(job_id)
                return {
                    "quality": quality_summary(store.job_dir(job_id)),
                    "input_count": len(meta.get("uploaded_files") or []),
                    "manifest": bool(meta.get("experiment_manifest")),
                    "is_example": bool(meta.get("is_example")),
                    "execution_status": meta["status"],
                    "analysis_source": meta.get("analysis_source"),
                    "cloned_from": meta.get("cloned_from"),
                    "quality_is_scientific_validation": False,
                }
        except FileNotFoundError as exc:
            raise HTTPException(404, detail="任务不存在。") from exc
        except (RuntimeError, OSError) as exc:
            raise HTTPException(409, detail="任务文件正在使用，请稍后重试。") from exc

    @router.get("/jobs/{job_id}/result-bundle")
    def export_results(job_id: str):
        try:
            archive = build_result_bundle(get_store(), job_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail="任务不存在。") from exc
        except (RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(409, detail=str(exc)) from exc

        def chunks():
            try:
                while chunk := archive.read(128 * 1024):
                    yield chunk
            finally:
                archive.close()

        return StreamingResponse(
            chunks(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="gromacs-results.zip"'}
        )

    @router.post("/examples/start")
    def start_example():
        settings = get_settings()
        if not detect_gromacs(settings["gmx_bin"])["available"]:
            raise HTTPException(
                409, detail={"code": "gromacs_unavailable", "message": "未检测到 GROMACS，请先完成环境配置，或打开离线示例结果。"}
            )
        try:
            return create_example(get_store(), settings)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(409, detail="无法准备教学示例，请检查运行目录权限和可用空间。") from exc

    @router.get("/support/diagnostics")
    def support_snapshot():
        diagnostics = environment_diagnostics(base_dir, get_settings())
        components = {}
        for key in ("gromacs", "acpype", "ambertools", "open_babel", "dssp"):
            item = diagnostics.get(key) or {}
            # Version numbers only: raw probe output can contain personal paths.
            match = re.search(r"\b\d{1,4}\.\d+(?:\.\d+)?\b", item.get("version") or "")
            components[key] = {"available": bool(item.get("available")), "version": match.group() if match else None}
        return {
            "schema_version": 1,
            "generated_at": utc_now(),
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "cpu_count": diagnostics.get("host", {}).get("cpu_count"),
            "gpu_verified": bool(diagnostics.get("gpu", {}).get("runtime_verified")),
            "components": components,
            "contents": "仅包含运行环境摘要，不含用户名、路径、输入结构、轨迹、任务名称或日志。",
        }

    @router.get("/support/usage")
    def local_usage():
        with get_store()._connect() as connection:
            rows = connection.execute(
                "SELECT status,count(*) AS count FROM jobs WHERE coalesce(json_extract(meta_json,'$.dry_run'),0)=0 "
                "AND coalesce(json_extract(meta_json,'$.is_example'),0)=0 GROUP BY status"
            ).fetchall()
            reused = connection.execute(
                "SELECT count(*) FROM jobs WHERE json_extract(meta_json,'$.cloned_from') IS NOT NULL "
                "AND coalesce(json_extract(meta_json,'$.dry_run'),0)=0 AND coalesce(json_extract(meta_json,'$.is_example'),0)=0"
            ).fetchone()[0]
        return {
            "scope": "本机任务记录；不是用户留存率，删除过的任务不计入。",
            "statuses": {row["status"]: row["count"] for row in rows},
            "cloned_tasks": reused,
            "uploaded": False,
        }

    @router.get("/help/document")
    def help_document(name: Annotated[str, Query(max_length=80)] = "QUICKSTART"):
        allowed = {
            "QUICKSTART",
            "USAGE",
            "WINDOWS",
            "TRAJECTORY-ANALYSIS",
            "RESEARCH-ANALYSIS",
            "PLOTTING",
            "RELEASE-CHECKLIST",
            "AI-AGENT",
        }
        if name not in allowed:
            raise HTTPException(404, detail="文档不存在。")
        path = base_dir / "docs" / f"{name}.md"
        if not path.is_file():
            raise HTTPException(404, detail="当前安装未包含此文档。")
        return FileResponse(path, media_type="text/plain; charset=utf-8")

    return router
