"""Check storage dialogs and real frontend refresh wiring using synthetic APIs only."""

from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
ORIGIN = "http://gromacs-storage.test"
MALICIOUS_NAME = '<img src=x onerror="window.storageXss=true">合成任务'


def check_browser(artifact_dir: Path | None = None) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in ((1440, 1000), (390, 844)):
                _check_viewport(browser, width, height, artifact_dir)
        finally:
            browser.close()
    print("Storage preview, protected tasks, confirmation/cancel, deletion, refresh wiring and safe labels passed at 2 viewports.")


def _check_viewport(browser, width: int, height: int, artifact_dir: Path | None) -> None:
    state = {"cleaned": False, "deleted": False, "previews": [], "cleanups": [], "jobs_reads": 0, "files_reads": 0}
    requests, errors = [], []
    completed = {
        "id": "storage-completed",
        "name": MALICIOUS_NAME,
        "directory_name": "synthetic-storage-completed",
        "workflow": "protein_md",
        "status": "completed",
        "created_at": "2026-10-16T00:00:00Z",
        "step_count": 1,
        "step_index": 1,
        "progress_percent": 100,
        "params": {},
    }
    running = {
        **completed,
        "id": "storage-running",
        "name": "受保护的运行任务",
        "status": "running",
        "directory_name": "synthetic-storage-running",
        "progress_percent": 20,
    }

    def job_files() -> list[dict]:
        files = [{"path": "md.xtc", "size": 6000}, {"path": "md.cpt", "size": 1000}, {"path": "run.log", "size": 500}]
        if not state["cleaned"]:
            files.append({"path": "nvt.trr", "size": 3000})
        return files

    def fixture(route) -> None:
        parsed = urlsplit(route.request.url)
        assert f"{parsed.scheme}://{parsed.netloc}" == ORIGIN, "Browser checks must never contact a real service"
        path, method = parsed.path, route.request.method
        query = parse_qs(parsed.query)
        requests.append((method, path))
        jobs = [running] if state["deleted"] else [completed, running]
        payload = None
        if path == "/api/health":
            assert method == "GET"
            payload = {
                "settings": {"gmx_bin": "gmx", "runtime_root": "synthetic-test-only", "max_parallel": 1},
                "gromacs": {"available": False, "message": "合成测试，不运行模拟"},
                "diagnostics": {},
            }
        elif path == "/api/product":
            payload = {"templates": [], "projects": [], "defaults": {}}
        elif path == "/api/preview":
            assert method == "POST"
            payload = {"commands": [], "mdp_overrides": []}
        elif path == "/api/protocol-preview":
            assert method == "POST"
            payload = {"stages": []}
        elif path == "/api/research/jobs":
            assert method == "GET"
            payload = {"jobs": [], "total": 0, "has_more": False}
        elif path == "/api/jobs":
            assert method == "GET", "Storage regression must never submit or mutate a simulation"
            state["jobs_reads"] += 1
            search = query.get("search", [""])[0].lower()
            matching = [job for job in jobs if search in (job["name"] + " " + job["id"]).lower()]
            payload = {"jobs": matching, "total": len(matching), "offset": 0, "limit": 50, "has_more": False}
        elif path.startswith("/api/jobs/"):
            assert method == "GET", "Storage regression must never submit or mutate a simulation"
            parts = path.split("/")
            job = next((job for job in jobs if job["id"] == parts[3]), None)
            assert job is not None, f"Unexpected request for missing synthetic task: {path}"
            if len(parts) == 4:
                payload = {**job, "storage_revision": int(state["cleaned"]) if job["id"] == completed["id"] else 0}
            elif parts[4] == "files":
                state["files_reads"] += 1
                payload = {"files": job_files() if job["id"] == completed["id"] else [], "truncated": False}
            elif parts[4] == "summary":
                payload = {"quality": [], "input_count": 0, "manifest": False}
            elif parts[4] == "plots":
                payload = {"plots": [], "metrics": [], "replicas": []}
        elif path == "/api/storage":
            assert method == "GET"
            storage_jobs = [
                {
                    "id": job["id"],
                    "name": job["name"],
                    "status": job["status"],
                    "bytes": sum(file["size"] for file in job_files()) if job["status"] == "completed" else 100,
                    "reclaimable_bytes": 0 if state["cleaned"] or job["status"] != "completed" else 3000,
                    "eligible": job["status"] == "completed",
                    "protected_reason": "任务尚未完成或取消。" if job["status"] != "completed" else None,
                }
                for job in jobs
            ]
            total = sum(job["bytes"] for job in storage_jobs)
            payload = {
                "disk": {"free_bytes": 4000000, "total_bytes": 10000000},
                "runtime": {"bytes": total + 1000, "task_bytes": total, "other_bytes": 1000},
                "categories": {"trajectories": total - 1000},
                "jobs": storage_jobs,
            }
        elif path == "/api/storage/preview":
            assert method == "POST"
            selection = route.request.post_data_json
            assert selection["job_ids"] == [completed["id"]], "Protected tasks must never be selected"
            assert selection["include_logs"] is False
            state["previews"].append(selection)
            files = job_files() if selection["mode"] == "delete_jobs" else [{"path": "nvt.trr", "size": 3000}]
            total = sum(file["size"] for file in files)
            payload = {
                "preview_token": f"preview-{len(state['previews'])}",
                "expires_at": "2099-01-01T00:00:00Z",
                "total_bytes": total,
                "file_count": len(files),
                "jobs": [{"id": completed["id"], "name": completed["name"], "bytes": total, "file_count": len(files)}],
                "files": [{"job_id": completed["id"], "path": file["path"], "bytes": file["size"]} for file in files],
                "protected_jobs": [],
                "files_truncated": False,
                "mode": selection["mode"],
            }
        elif path == "/api/storage/cleanup":
            assert method == "POST"
            selection = route.request.post_data_json
            assert selection["preview_token"] == f"preview-{len(state['previews'])}"
            assert {key: selection[key] for key in ("job_ids", "mode", "include_logs")} == state["previews"][-1]
            state["cleanups"].append(selection)
            deleting = selection["mode"] == "delete_jobs"
            files = job_files() if deleting else [{"path": "nvt.trr", "size": 3000}]
            state["cleaned"] = True
            state["deleted"] = deleting
            payload = {
                "deleted_bytes": sum(file["size"] for file in files),
                "deleted_files": len(files),
                "deleted_jobs": [completed["id"]] if deleting else [],
                "cleaned_jobs": [] if deleting else [completed["id"]],
                "skipped_jobs": [],
            }
        if path.startswith("/api/"):
            assert payload is not None, f"Unexpected API call: {method} {path}"
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        assert method == "GET"
        target = APP / "templates/index.html" if path == "/" else APP / path.lstrip("/")
        target = target.resolve()
        target.relative_to(APP.resolve())
        assert target.is_file(), f"Unexpected static asset: {path}"
        route.fulfill(status=200, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes())

    page = browser.new_page(viewport={"width": width, "height": height})
    try:
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Intercept every request, including external URLs, so the real application
        # can only read repository assets and the synthetic responses above.
        page.route("**/*", fixture)
        page.add_init_script("""
            window.storageCleanupEvents = [];
            document.addEventListener('storage-cleaned', event => window.storageCleanupEvents.push(event.detail));
            window.testStreams = [];
            window.EventSource = class extends EventTarget {
                constructor(url) { super(); this.url = url; this.readyState = 1; window.testStreams.push(this); }
                close() { this.readyState = 2; }
            };
        """)
        page.goto(ORIGIN, wait_until="networkidle")
        expect(page.locator(".job-item")).to_have_count(2)
        expect(page.locator("#active-job-title")).to_have_text(MALICIOUS_NAME)
        expect(page.locator('#output-files a[title="下载 nvt.trr"]')).to_have_count(1)
        if width <= 1180:
            page.locator("#toggle-sidebar").click()
        page.locator("#open-storage").click()
        expect(page.locator(".storage-job")).to_have_count(2)
        expect(page.locator('[name="storage-mode"][value="intermediates"]')).to_be_checked()
        expect(page.locator('[name="storage-mode"][value="delete_jobs"]')).not_to_be_checked()
        expect(page.locator("#storage-logs")).not_to_be_checked()
        expect(page.locator('.storage-job input[value="storage-running"]')).to_be_disabled()
        page.locator("#storage-select-visible").click()
        expect(page.locator('.storage-job input[value="storage-completed"]')).to_be_checked()
        expect(page.locator('.storage-job input[value="storage-running"]')).not_to_be_checked()
        page.locator("#storage-preview-button").click()
        expect(page.locator("#storage-preview")).to_be_visible()
        assert len(state["previews"]) == 1 and not state["cleanups"], "Preview must never execute cleanup"
        assert state["previews"][0]["mode"] == "intermediates"
        expect(page.locator("#storage-preview")).to_contain_text("nvt.trr")
        expect(page.locator("#storage-preview")).not_to_contain_text("md.xtc")
        expect(page.locator("#storage-preview")).not_to_contain_text("run.log")
        assert page.locator("#storage-dialog img").count() == 0
        assert not page.evaluate("Boolean(window.storageXss)")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(artifact_dir / f"storage-preview-{width}.png"), full_page=True)

        page.locator("#storage-execute").click()
        expect(page.locator("#submit-warning")).to_be_visible()
        expect(page.locator("#submit-warning-title")).to_have_text("确认清理所选文件")
        page.locator("#submit-warning-cancel").click()
        assert not state["cleanups"]
        expect(page.locator("#storage-dialog")).to_be_visible()
        expect(page.locator("#storage-execute")).to_be_enabled()
        jobs_reads, files_reads = state["jobs_reads"], state["files_reads"]
        page.locator("#storage-execute").click()
        page.locator("#submit-warning-confirm").click()
        expect(page.locator("#storage-status")).to_contain_text("已清理 1 个文件")
        page.wait_for_function("window.storageCleanupEvents.length === 1")
        expect(page.locator('#output-files a[title="下载 nvt.trr"]')).to_have_count(0)
        expect(page.locator('#output-files a[title="下载 md.xtc"]')).to_have_count(1)
        assert state["jobs_reads"] > jobs_reads and state["files_reads"] > files_reads, "main.js must refresh history and artifacts"
        assert len(state["cleanups"]) == 1

        page.evaluate("""() => {
            for (const id of ['research-reference', 'research-other']) {
                document.getElementById(id).add(new Option('selected old result', 'storage-completed', false, true));
            }
        }""")
        page.locator('[name="storage-mode"][value="delete_jobs"]').check()
        page.locator('.storage-job input[value="storage-completed"]').check()
        page.locator("#storage-preview-button").click()
        expect(page.locator("#storage-preview")).to_contain_text("md.xtc")
        expect(page.locator("#storage-preview")).to_contain_text("md.cpt")
        page.locator("#storage-execute").click()
        expect(page.locator("#submit-warning-title")).to_have_text("删除任务及全部结果")
        expect(page.locator("#submit-warning-list")).to_contain_text("checkpoint")
        assert not page.evaluate("Boolean(window.storageXss)")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if artifact_dir:
            page.screenshot(path=str(artifact_dir / f"storage-delete-confirm-{width}.png"), full_page=True)
        page.locator("#submit-warning-confirm").click()
        expect(page.locator("#storage-status")).to_contain_text("删除 1 个任务")
        page.wait_for_function("window.storageCleanupEvents.length === 2")
        expect(page.locator('.job-item[data-job-id="storage-completed"]')).to_have_count(0)
        expect(page.locator(".job-item")).to_have_count(1)
        expect(page.locator("#active-job-title")).to_have_text(running["name"])
        expect(page.locator('#research-reference option[value="storage-completed"]')).to_have_count(0)
        expect(page.locator('#research-other option[value="storage-completed"]')).to_have_count(0)
        assert len(state["cleanups"]) == 2 and state["deleted"]
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors, errors
        assert all(method == "GET" for method, path in requests if path.startswith("/api/jobs"))
    finally:
        page.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, help="Optional directory for desktop and mobile screenshots")
    check_browser(parser.parse_args().artifact_dir)
