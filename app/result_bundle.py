"""Bounded offline result packages, built under the existing job file lock."""

from __future__ import annotations

import html
import json
import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote

from .execution import live_process_identities
from .gromacs import utc_now

MAX_BUNDLE_BYTES = 64 * 1024**2
MAX_FILE_BYTES = 16 * 1024**2
MAX_FILES = 500
REPORT_NAMES = {
    "experiment-manifest.json",
    "research-report.json",
    "sampling-summary.json",
    "structural-convergence.json",
    "structure-preflight.txt",
}
SUMMARY_CSS = """
body{max-width:1040px;margin:40px auto;padding:0 24px;font:16px/1.65 system-ui,sans-serif;color:#242a30;background:#f8fafb}
h1{line-height:1.3;overflow-wrap:anywhere}h2{margin-top:32px}a{color:#16786f}dt{font-weight:600}dd{margin:0 0 12px;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse}td{border-bottom:1px solid #d9dfe4;padding:8px;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:16px}
img{max-width:100%;height:auto}figure{margin:20px 0}small,figcaption{color:#626d77}
.notice{border-left:4px solid #b86f1c;padding:8px 16px;background:#fff4dc}
"""


def read_regular(path: Path, limit: int, *, root: Path | None = None) -> bytes:
    """Open every untrusted path component relative to an anchored directory."""
    root = root or path.parent
    parts = path.relative_to(root).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("文件路径不可用。")
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
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("文件不是普通文件或超过导出大小限制。")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError("文件在读取期间增长，超过导出大小限制。")
        return data


def quality_summary(workdir: Path) -> list[dict]:
    summaries = []
    # Bound work even for a directory containing many generated report files.
    with os.scandir(workdir) as entries:
        paths = []
        for index, entry in enumerate(entries):
            if index >= 10000:
                break
            if entry.name in REPORT_NAMES or entry.name.startswith("quality-"):
                paths.append(Path(entry.path))
            if len(paths) >= 100:
                break
    for path in sorted(paths):
        if path.name not in REPORT_NAMES and not path.name.startswith("quality-"):
            continue
        if path.name == "experiment-manifest.json":
            continue
        try:
            if path.is_symlink():
                continue
            raw = read_regular(path, 256 * 1024, root=workdir).decode("utf-8", errors="replace")
            try:
                report = json.loads(raw)
            except ValueError:
                # Stage gates use key=value TXT; thermodynamic TXT is JSON.
                status_match = re.search(r"^status=(PASS|FAIL|WARNING)\s*$", raw, re.MULTILINE)
                if not status_match:
                    continue
                report = {"status": status_match.group(1), "failures": re.findall(r"^failure=(.*)$", raw, re.MULTILINE)}
            if not isinstance(report, dict):
                continue
        except (OSError, ValueError):
            continue
        raw_status = report.get("status") or ("需阅读报告" if report else "空报告")
        status = {"passed": "检查通过", "pass": "检查通过", "failed": "检查未通过", "fail": "检查未通过", "warning": "需要复核"}.get(
            str(raw_status).lower(), str(raw_status)
        )
        failures = report.get("failures") or report.get("warnings") or []
        summaries.append(
            {
                "file": path.name,
                "status": status[:200],
                "notes": [str(item)[:500] for item in failures[:12]] if isinstance(failures, list) else [],
            }
        )
    return summaries


def build_result_bundle(store, job_id: str):
    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024**2, mode="w+b")
    try:
        with store.job_files_lock(job_id):
            meta = store.get(job_id)
            if (
                meta["status"] not in {"completed", "failed", "cancelled", "interrupted"}
                or meta.get("maintenance_pending")
                or meta.get("deletion_pending")
                or meta.get("desired_action")
            ):
                raise ValueError("请等任务停止或完成后导出结果摘要包。")
            workdir = store.job_dir(job_id)
            if live_process_identities(meta, workdir):
                raise ValueError("任务仍关联活跃计算进程，请等计算停止后导出。")
            included, skipped = [], []
            used = 0
            summary = {
                "schema_version": 1,
                "exported_at": utc_now(),
                "job": {
                    key: meta.get(key)
                    for key in (
                        "id",
                        "name",
                        "workflow",
                        "status",
                        "created_at",
                        "project",
                        "tags",
                        "notes",
                        "cloned_from",
                        "analysis_source",
                        "dry_run",
                        "is_example",
                    )
                },
                "parameters": meta.get("params") or {},
                "input_hashes": meta.get("input_hashes") or [],
                "quality": quality_summary(workdir),
                "limitations": [
                    "任务完成表示程序执行结束，不代表所有质量检查通过或采样充分。",
                    "原始结构、轨迹、checkpoint 和运行日志未包含；请在输出文件中另行下载。",
                ],
            }
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                seen = 0
                stop = False
                for directory, dirs, names in os.walk(workdir, followlinks=False):
                    dirs[:] = sorted(name for name in dirs if not (Path(directory) / name).is_symlink())
                    seen += len(dirs)
                    if seen > 10000:
                        skipped.append({"path": "其他目录", "reason": "达到目录扫描上限，请单独下载所需文件。"})
                        break
                    if len(Path(directory).relative_to(workdir).parts) >= 16:
                        dirs.clear()
                        skipped.append({"path": Path(directory).relative_to(workdir).as_posix(), "reason": "达到目录深度上限。"})
                    for name in sorted(names):
                        seen += 1
                        if seen > 10000:
                            skipped.append({"path": "其他文件", "reason": "达到目录扫描上限，请单独下载所需文件。"})
                            stop = True
                            break
                        path = Path(directory) / name
                        relative = path.relative_to(workdir).as_posix()
                        allowed = name in REPORT_NAMES or name.startswith("quality-") and path.suffix in {".json", ".txt"}
                        allowed = allowed or path.suffix.lower() in {".xvg", ".png", ".pdf", ".tsv", ".mdp"}
                        if not allowed:
                            continue
                        if len(included) >= MAX_FILES:
                            skipped.append({"path": relative, "reason": "达到文件数量上限。"})
                            continue
                        try:
                            if path.is_symlink():
                                raise ValueError("符号链接不参与导出。")
                            data = read_regular(path, min(MAX_FILE_BYTES, MAX_BUNDLE_BYTES - used), root=workdir)
                        except (OSError, ValueError) as error:
                            skipped.append(
                                {
                                    "path": relative,
                                    "reason": "文件不可读取、为符号链接或超过大小限制。" if isinstance(error, OSError) else str(error),
                                }
                            )
                            continue
                        bundle.writestr("files/" + relative, data)
                        included.append({"path": relative, "size": len(data)})
                        used += len(data)
                    if stop:
                        break
                summary.update(files=included, skipped_files=skipped)
                bundle.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
                bundle.writestr("index.html", render_summary(summary))
                bundle.writestr("report.css", SUMMARY_CSS)
                bundle.writestr(
                    "README.txt",
                    "离线打开 index.html 查看摘要。summary.json 提供结构化记录。files/ 保留报告、参数及图表原文件。\n"
                    + "\n".join(summary["limitations"]),
                )
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise


def render_summary(summary: dict) -> str:
    def escape(value):
        return html.escape(str(value if value is not None else "未记录"), quote=True)

    job = summary["job"]
    title = escape(job.get("name"))
    facts = "".join(
        f"<dt>{label}</dt><dd>{escape(job.get(key))}</dd>"
        for key, label in (("id", "任务 ID"), ("workflow", "工作流"), ("status", "执行状态"), ("project", "项目"), ("notes", "备注"))
    )
    checks = "".join(
        f"<li><strong>{escape(item['file'])}</strong>：{escape(item['status'])}<p>{escape('；'.join(item['notes']))}</p></li>"
        for item in summary["quality"]
    )
    files = "".join(
        f'<tr><td><a href="files/{quote(item["path"], safe="/")}">{escape(item["path"])}</a></td><td>{item["size"]:,} B</td></tr>'
        for item in summary["files"]
    )
    images = "".join(
        f'<figure><img loading="lazy" src="files/{quote(item["path"], safe="/")}" alt="{escape(item["path"])}"><figcaption>{escape(item["path"])}</figcaption></figure>'
        for item in summary["files"]
        if item["path"].lower().endswith(".png")
    )
    skipped = "".join(f"<li>{escape(item['path'])}：{escape(item['reason'])}</li>" for item in summary["skipped_files"])
    limitations = "".join(f"<li>{escape(item)}</li>" for item in summary["limitations"])
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'">
<title>{title} · GROMACS 结果摘要</title><link rel="stylesheet" href="report.css">
<body><small>GROMACS Console · 可离线查看 · {escape(summary["exported_at"])}</small><h1>{title}</h1>
<div class="notice"><ul>{limitations}</ul></div><h2>实验记录</h2><dl>{facts}</dl><h2>质量检查</h2><ul>{checks or "<li>未找到可读取的质量报告；请先完成相应分析。</li>"}</ul>
<h2>图表</h2>{images or "<p>尚无 PNG 图表；可下载 XVG 原始数据继续绘图。</p>"}
<h2>协议参数</h2><pre>{escape(json.dumps(summary["parameters"], ensure_ascii=False, indent=2))}</pre>
<h2>输入校验记录</h2><pre>{escape(json.dumps(summary["input_hashes"], ensure_ascii=False, indent=2))}</pre>
<h2>包含的文件</h2><table>{files}</table><h2>未包含的候选文件</h2><ul>{skipped or "<li>无</li>"}</ul></body></html>"""
