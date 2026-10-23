"""Check preflight and preparation UI using only intercepted synthetic APIs."""

from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

APP = Path(__file__).resolve().parents[1] / "app"
ORIGIN = "http://gromacs.test"


def check(artifact_dir: Path | None = None):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in ((1440, 1000), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                errors = []
                calls = []
                job = {"id": "source", "name": "合成来源", "workflow": "protein_md", "status": "completed", "params": {}, "tags": []}
                created = {**job, "id": "analysis", "name": "新分析", "status": "queued"}
                jobs = [job]

                def fixture(route):
                    path = urlsplit(route.request.url).path
                    calls.append((route.request.method, path))
                    if path == "/api/health":
                        payload = {
                            "settings": {"gmx_bin": "gmx", "max_parallel": 1, "analysis_parallel": 2},
                            "gromacs": {"available": False},
                            "diagnostics": {},
                        }
                    elif path == "/api/agent/config":
                        assert route.request.method == "GET", "Automatic AI initialization must be read-only"
                        payload = {
                            "provider": "openai", "model": "gpt-6.1-sol", "has_api_key": False, "key_source": "none",
                            "providers": [{"id": "openai", "label": "OpenAI", "default_model": "gpt-6.1-sol", "endpoint": "https://api.openai.com/v1"}],
                            "profiles": {"openai": {"model": "gpt-6.1-sol", "has_api_key": False, "key_source": "none"}},
                        }
                    elif path == "/api/agent/sessions":
                        assert route.request.method == "GET", "Opening the application must not create a conversation"
                        payload = {"sessions": [], "limit": 200}
                    elif path == "/api/product":
                        payload = {"templates": [], "projects": [], "defaults": {}}
                    elif path == "/api/preview":
                        payload = {"commands": [], "mdp_overrides": []}
                    elif path == "/api/protocol-preview":
                        payload = {"stages": []}
                    elif path in {"/api/jobs", "/api/research/jobs"}:
                        assert route.request.method == "GET"
                        payload = {"jobs": jobs, "total": len(jobs), "has_more": False}
                    elif path.endswith("/analysis-inputs"):
                        payload = {
                            "source_name": job["name"],
                            "files": {
                                "tpr_file": [{"path": "md.tpr", "size": 100}],
                                "trajectory_file": [{"path": "md.xtc", "size": 1024}],
                                "index_file": [],
                                "edr_file": [],
                            },
                        }
                    elif path.endswith("/analysis-preflight"):
                        assert route.request.post_data_json["research_stride"] == 10
                        payload = {
                            "storage": {"required_free_bytes": 3 * 1024**3},
                            "research": {
                                "valid": False,
                                "selected_frames": 100000,
                                "sampled_frames": 10000,
                                "residues": 1000,
                                "suggested_stride": 50,
                                "message": "建议每 50 帧取一帧，或缩短时间范围。",
                            },
                        }
                    elif path.endswith("/analysis-stream"):
                        assert route.request.post_data_json["research_stride"] == 50
                        jobs.append(created)
                        events = [
                            {"type": "progress", "phase": "copy", "copied_bytes": 512, "total_bytes": 1024, "job_id": "analysis"},
                            {"type": "result", "job": created},
                        ]
                        route.fulfill(content_type="application/x-ndjson", body="".join(json.dumps(event) + "\n" for event in events))
                        return
                    elif path.endswith("/cancel"):
                        created["status"] = "cancelled"
                        payload = created
                    elif path.endswith("/files"):
                        payload = {"files": [], "truncated": False}
                    elif path.endswith("/summary"):
                        payload = {"quality": [], "input_count": 2, "manifest": True}
                    elif path.endswith("/plots"):
                        payload = {"plots": [], "metrics": [], "replicas": []}
                    elif path.startswith("/api/jobs/"):
                        payload = next(item for item in jobs if item["id"] == path.rsplit("/", 1)[1])
                    elif path.startswith("/api/"):
                        raise AssertionError(f"Unexpected API {path}")
                    else:
                        target = APP / "templates/index.html" if path == "/" else APP / path.lstrip("/")
                        target.resolve().relative_to(APP.resolve())
                        route.fulfill(
                            content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes()
                        )
                        return
                    route.fulfill(content_type="application/json", body=json.dumps(payload))

                page.route("**/*", fixture)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("window.EventSource = class extends EventTarget { close() {} };")
                page.goto(ORIGIN, wait_until="networkidle")
                if width <= 1180:
                    page.locator("#toggle-sidebar").click()
                page.locator('[data-job-id="source"] [data-action="select"]').first.click()
                page.locator("#analyze-job").click()
                dialog = page.locator("#existing-analysis-dialog")
                expect(dialog).to_be_visible()
                dialog.locator('[name="do_research"]').check()
                page.locator("#analysis-preflight").click()
                expect(page.locator("#analysis-preflight-result")).to_contain_text("建议每 50 帧")
                expect(dialog.locator('[name="research_stride"]')).to_have_value("10")
                dialog.locator('[name="research_stride"]').fill("50")
                assert dialog.locator("form").evaluate("form => form.checkValidity()"), dialog.locator("form").evaluate(
                    "form => [...form.elements].filter(field => field.validity && !field.validity.valid).map(field => [field.name, field.value, field.validationMessage])"
                )
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if artifact_dir:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(artifact_dir / f"analysis-preflight-{width}.png"))
                page.locator("#existing-analysis-submit").click()
                try:
                    expect(dialog).not_to_be_visible()
                except AssertionError:
                    print({"errors": errors, "calls": calls[-12:], "dialog": dialog.inner_text()})
                    raise
                expect(page.locator("#active-job-title")).to_have_text("新分析")
                assert ("POST", "/api/jobs/source/analysis-stream") in calls
                if width <= 1180:
                    page.locator("#toggle-sidebar").click()
                page.locator('[data-job-id="source"] [data-action="select"]').first.click()
                page.locator("#analyze-job").click()
                page.evaluate("""() => {
                    const originalFetch = window.fetch;
                    window.fetch = (url, options) => String(url).endsWith('/analysis-stream')
                        ? Promise.resolve(new Response(new ReadableStream({start(controller) {
                            window.analysisController = controller;
                            controller.enqueue(new TextEncoder().encode(JSON.stringify({type:'progress',phase:'copy',job_id:'analysis',copied_bytes:512,total_bytes:1024})+'\\n'));
                        }}), {headers:{'Content-Type':'application/x-ndjson'}}))
                        : originalFetch(url, options);
                }""")
                page.locator("#existing-analysis-submit").click()
                expect(page.locator("#existing-analysis-status")).to_contain_text("50.0%")
                expect(page.locator("#existing-analysis-cancel")).to_have_text("停止准备")
                page.locator("#existing-analysis-cancel").click()
                expect(page.locator("#existing-analysis-status")).to_contain_text("已请求停止准备")
                page.evaluate("""() => {
                    window.analysisController.enqueue(new TextEncoder().encode(JSON.stringify({type:'error',message:'分析准备已取消。'})+'\\n'));
                    window.analysisController.close();
                }""")
                expect(page.locator("#existing-analysis-error")).to_have_text("分析准备已取消。")
                assert ("POST", "/api/jobs/analysis/cancel") in calls
                page.locator("#existing-analysis-cancel").click()
                expect(dialog).not_to_be_visible()
                assert not errors, errors
                page.close()
        finally:
            browser.close()
    print("Research preflight advice, explicit stride selection, streamed progress and cancellation passed on desktop/mobile.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    check(parser.parse_args().artifact_dir)
