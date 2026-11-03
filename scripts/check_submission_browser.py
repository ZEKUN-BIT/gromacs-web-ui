"""Verify submission validation and preview races with isolated browser fixtures.

Every request is intercepted. No service, simulation, or user's runtime is used.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
ORIGIN = "http://gromacs-submission.test"


def check_submission(artifact_dir: Path | None = None) -> None:
    pending = {"preview": [], "protocol-preview": []}
    held = set()
    failures = {}
    submissions = []
    upload_mode = {"value": "validation", "resolutions": 0}
    pending_uploads = []
    recovered_job = {"id": "recovered-upload", "name": "恢复的上传任务", "status": "queued", "workflow": "run_tpr", "step_count": 1}
    errors = []
    source = (APP / "static/js/main.js").read_text()
    version = re.search(r"core\.js\?([^\"]+)", source).group(1)
    urls = {name: f"/static/js/{name}.js?{version}" for name in ("form", "jobs", "core", "submission-validation")}

    def fulfill(route, payload, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    def preview_payload(route):
        params = route.request.post_data_json
        return {"commands": [{"title": params.get("name", ""), "args": ["gmx", "mdrun"]}], "mdp_overrides": []}

    def fixture(route):
        parsed = urlsplit(route.request.url)
        assert parsed.hostname == "gromacs-submission.test", f"Unexpected external request: {route.request.url}"
        path, method = parsed.path, route.request.method
        if path in ("/api/preview", "/api/protocol-preview"):
            key = path.rsplit("/", 1)[1]
            if key in held:
                pending[key].append(route)
                return
            if key in failures:
                fulfill(route, failures[key], 422)
            else:
                fulfill(route, preview_payload(route) if key == "preview" else {"stages": []})
            return
        if path == "/api/jobs" and method == "POST":
            submissions.append(route.request.post_data_buffer)
            if upload_mode["value"] == "hold":
                pending_uploads.append(route)
                return
            if upload_mode["value"] == "lost-response":
                route.abort("failed")
                return
            fulfill(
                route, {"detail": [{"loc": ["body", "params", "name"], "msg": "<img src=x onerror=window.xss=true> 服务端字段错误"}]}, 422
            )
            return
        if path.startswith("/api/submissions/") and path.endswith("/resolve"):
            upload_mode["resolutions"] += 1
            if upload_mode["value"] == "lost-response":
                fulfill(route, {"state": "submitted", "job": recovered_job})
            else:
                fulfill(route, {"state": "cancelled"})
            return
        if path == "/api/jobs/recovered-upload":
            fulfill(route, recovered_job)
            return
        if path in {"/api/jobs/recovered-upload/files", "/api/jobs/synthetic-progress/files"}:
            assert method == "GET", "The structure viewer only reads the fixture's file list"
            fulfill(route, {"files": [], "total": 0, "has_more": False, "truncated": False})
            return
        payloads = {
            "/api/health": {"settings": {"gmx_bin": "gmx", "max_parallel": 1}, "gromacs": {"available": False}, "diagnostics": {}},
            "/api/product": {"templates": [], "projects": [], "defaults": {}},
            "/api/jobs": {"jobs": [], "total": 0, "has_more": False},
            "/api/research/jobs": {"jobs": [], "total": 0, "has_more": False},
            "/api/agent/config": {
                "provider": "openai",
                "model": "test",
                "has_api_key": False,
                "key_source": "none",
                "providers": [{"id": "openai", "label": "OpenAI", "default_model": "test", "endpoint": "https://api.openai.com/v1"}],
                "profiles": {"openai": {"model": "test", "has_api_key": False}},
            },
            "/api/agent/sessions": {"sessions": []},
        }
        if path in payloads:
            fulfill(route, payloads[path])
            return
        if path.startswith("/api/"):
            raise AssertionError(f"Unexpected API call: {method} {path}")
        target = (APP / "templates/index.html" if path == "/" else APP / path.lstrip("/")).resolve()
        target.relative_to(APP.resolve())
        route.fulfill(status=200, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", fixture)
        page.add_init_script("""
            window.EventSource = class extends EventTarget {
                constructor(url) { super(); this.url = url; this.readyState = 1; }
                close() { this.readyState = 2; }
            };
        """)
        try:
            page.goto(ORIGIN, wait_until="networkidle")
            page.locator("#expert-mode").click()
            page.evaluate(
                "async urls => { window.modules = {}; for (const [name, url] of Object.entries(urls)) window.modules[name] = await import(url); }",
                urls,
            )

            def configure(workflow="run_tpr", *, dry_run=True):
                page.evaluate(
                    """({workflow, dryRun}) => {
                    const form = document.querySelector('#job-form');
                    form.querySelector(`[name='workflow'][value='${workflow}']`).checked = true;
                    form.elements.name.value = '隔离提交测试';
                    form.elements.dry_run.checked = dryRun;
                    form.elements.maxwarn.value = '0';
                    for (const field of form.querySelectorAll('[name$="_file"]')) field.value = '';
                    const transfer = new DataTransfer();
                    transfer.items.add(new File(['END\\n'], workflow === 'run_tpr' ? 'run.tpr' : 'complex.pdb', {lastModified: 1}));
                    document.querySelector('#files').files = transfer.files;
                    form.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                    {"workflow": workflow, "dryRun": dry_run},
                )
                expect(page.locator("#submit-job")).to_be_enabled()

            configure()
            # Edits invalidate readiness immediately, including the debounce gap.
            held.add("preview")
            page.locator("#job-name").fill("延迟预览 A")
            expect(page.locator("#submit-job")).to_be_disabled()
            page.wait_for_timeout(340)
            assert pending["preview"]
            page.evaluate("() => window.modules.jobs.submitJob()")
            assert not submissions
            page.locator("#job-name").fill("最新预览 B")
            page.wait_for_timeout(340)
            newest = pending["preview"].pop()
            fulfill(newest, preview_payload(newest))
            expect(page.locator("#submit-job")).to_be_enabled()
            for old in pending["preview"]:
                fulfill(old, {"detail": "旧预览失败"}, 422)
            pending["preview"].clear()
            held.clear()
            expect(page.locator("#command-preview")).to_contain_text("最新预览 B")
            expect(page.locator("#submit-job")).to_be_enabled()

            # The File object identity matters even with identical name/metadata.
            page.evaluate("""() => {
                const input = document.querySelector('#files'), old = input.files[0];
                const transfer = new DataTransfer();
                transfer.items.add(new File(['NEW\\n'], old.name, {lastModified: old.lastModified}));
                input.files = transfer.files;
                return window.modules.jobs.submitJob();
            }""")
            assert not submissions
            expect(page.locator("#submission-errors")).to_contain_text("参数或文件已变化")
            expect(page.locator("#submit-job")).to_be_enabled()

            # Both command and protocol requests must finish for this revision.
            held.add("protocol-preview")
            page.evaluate("""() => {
                const form = document.querySelector('#job-form');
                form.querySelector('[name="workflow"][value="protein_md"]').checked = true;
                form.dispatchEvent(new Event('change', {bubbles:true}));
            }""")
            page.wait_for_timeout(100)
            expect(page.locator("#submit-job")).to_be_disabled()
            assert pending["protocol-preview"]
            for route in pending["protocol-preview"]:
                fulfill(route, {"stages": []})
            pending["protocol-preview"].clear()
            held.clear()
            expect(page.locator("#submit-job")).to_be_enabled()
            failures["protocol-preview"] = {"detail": "协议预览失败"}
            page.locator("#job-name").fill("失败协议")
            expect(page.locator("#submission-errors")).to_contain_text("协议预览失败")
            expect(page.locator("#submit-job")).to_be_disabled()
            failures.clear()
            page.locator("#job-name").fill("恢复协议")
            expect(page.locator("#submit-job")).to_be_enabled()

            # Multi-ligand errors link to the exact row and reveal advanced fields.
            configure("protein_ligand_md")
            page.evaluate("""() => {
                const {form, core} = window.modules;
                form.addLigandRow({key:'lig', residue:'ABC'});
                form.addLigandRow({key:'third', residue:''});
                const fields = core.els.jobForm.elements;
                fields.dry_run.checked = false; fields.maxwarn.value = '1';
                core.els.jobForm.dispatchEvent(new Event('change', {bubbles:true}));
            }""")
            expect(page.locator("#submission-errors")).to_contain_text("配体 2 的标识 lig 重复")
            page.locator("#submission-errors button").filter(has_text="配体 2 的标识").click()
            expect(page.locator(".ligand-card").nth(1).locator('[data-ligand-field="key"]')).to_be_focused()
            page.locator("#submission-errors button").filter(has_text="配体 3（third）缺少").click()
            expect(page.locator(".ligand-card").nth(2).locator('[data-ligand-field="residue"]')).to_be_focused()
            page.locator("#submission-errors button").filter(has_text="maxwarn = 0").click()
            expect(page.locator('[name="maxwarn"]')).to_be_focused()
            expect(page.locator("body")).to_have_attribute("data-form-mode", "expert")
            expect(page.locator('[name="maxwarn"]')).to_have_attribute("aria-invalid", "true")
            if artifact_dir:
                artifact_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(artifact_dir / "field-errors.png"), full_page=True)

            # A 422 preserves server locations and safely renders arbitrary text.
            configure()
            page.locator("#submit-job").click()
            expect(page.locator("#submission-errors")).to_contain_text("服务端字段错误")
            assert len(submissions) == 1
            expect(page.locator("#submission-errors img")).to_have_count(0)
            assert page.evaluate("window.xss") is None
            page.locator("#submission-errors button").click()
            expect(page.locator("#job-name")).to_be_focused()
            expect(page.locator("#job-name")).to_have_attribute("aria-invalid", "true")
            page.wait_for_timeout(3300)
            expect(page.locator("#submission-errors")).to_be_visible()
            page.locator("#job-name").fill("修正服务端错误")
            expect(page.locator("#job-name")).not_to_have_attribute("aria-invalid", "true")
            expect(page.locator("#submit-job")).to_be_enabled()

            # A late programmatic edit during the modal cannot submit the snapshot.
            configure(dry_run=False)
            page.locator("#submit-job").click()
            expect(page.locator("#submit-warning")).to_be_visible()
            page.evaluate("document.querySelector('#job-name').value = '确认时自动填充新名称'")
            page.locator("#submit-warning-confirm").click()
            expect(page.locator("#submission-errors")).to_contain_text("确认期间参数或文件已变化")
            assert len(submissions) == 1
            expect(page.locator("#submit-job")).to_be_enabled()

            failures["preview"] = {"detail": [{"loc": ["body", "params", "name"], "msg": "字段格式错误"}]}
            page.locator("#job-name").fill("错误预览")
            expect(page.locator("#submission-errors")).to_contain_text("字段格式错误")
            expect(page.locator("#job-name")).to_have_attribute("aria-invalid", "true")
            expect(page.locator("#submit-job")).to_be_disabled()
            failures.clear()
            page.locator("#job-name").fill("恢复预览")
            expect(page.locator("#submit-job")).to_be_enabled()
            expect(page.locator("#submission-errors")).to_be_hidden()

            # Server ligand locations retain the row index, including a field
            # hidden by the selected input mode; MDP errors open their section.
            page.evaluate("""() => document.querySelectorAll('.ligand-card').forEach((card, index) => {
                card.querySelector('[data-ligand-field="key"]').value = 'unique' + index;
                card.querySelector('[data-ligand-field="residue"]').value = 'L' + index;
                card.querySelector('[data-ligand-field="mode"]').value = 'prepared';
                window.modules.form.refreshRowMode(card);
            })""")
            configure("protein_ligand_md")
            page.evaluate("""() => window.modules['submission-validation'].showSubmissionIssues(
                window.modules['submission-validation'].apiSubmissionIssues({detail:[
                    {loc:['body','params','ligands',2,'charge'],msg:'净电荷无效'},
                    {loc:['body','params','mdp_tau_t_ps'],msg:'温控时间无效'}
                ]}))""")
            page.locator("#submission-errors button").filter(has_text="配体 3 · charge").click()
            expect(page.locator(".ligand-card").nth(2).locator('[data-ligand-field="charge"]')).to_be_focused()
            page.locator("#submission-errors button").filter(has_text="mdp_tau_t_ps").click()
            expect(page.locator('[name="mdp_tau_t_ps"]')).to_be_focused()
            expect(page.locator("#mdp-panel-toggle")).to_have_attribute("aria-expanded", "true")
            page.locator("#submission-errors button").filter(has_text="配体 3 · charge").click()
            charge = page.locator(".ligand-card").nth(2).locator('[data-ligand-field="charge"]')
            charge.fill("12")
            expect(charge).to_be_focused()
            expect(charge).to_be_visible()
            expect(page.locator("#submit-job")).to_be_enabled()

            # Asynchronous file-based autofill holds submission until complete.
            page.evaluate("""() => {
                const file = document.querySelector('#files').files[0];
                // Hold the detector's read once. The structure viewer can read
                // the same file independently when the workspace is revealed.
                const originalText = file.text.bind(file);
                file.text = () => {
                    file.text = originalText;
                    return new Promise(resolve => { window.finishPdbRead = resolve; });
                };
                void window.modules.form.detectLigandsFromPdb();
            }""")
            expect(page.locator("#submit-job")).to_be_disabled()
            page.evaluate("() => window.modules.jobs.submitJob()")
            assert len(submissions) == 1
            page.evaluate("window.finishPdbRead('END\\n')")
            expect(page.locator("#submit-job")).to_be_enabled()

            # Cancellation seals the upload identity, preserves form/files, and
            # allows retry only after the server confirms no task was created.
            configure()
            upload_mode["value"] = "hold"
            page.locator("#submit-job").click()
            expect(page.locator("#cancel-upload")).to_be_visible()
            page.wait_for_timeout(100)
            assert pending_uploads
            assert b'name="submission_id"' in submissions[-1]
            page.locator("#cancel-upload").click()
            expect(page.locator("#upload-message")).to_contain_text("参数和已选文件已保留")
            expect(page.locator("#submit-job")).to_be_enabled()
            assert upload_mode["resolutions"] == 1
            assert page.locator("#files").evaluate("input => input.files.length") == 1
            expect(page.locator("#job-name")).to_have_value("隔离提交测试")
            for route in pending_uploads:
                route.abort("aborted")
            pending_uploads.clear()

            # A lost success response opens the original task instead of
            # uploading again, with a durable request ID carried in FormData.
            upload_mode["value"] = "lost-response"
            count = len(submissions)
            page.locator("#submit-job").click()
            expect(page.locator("#active-job-title")).to_have_text(recovered_job["name"])
            expect(page.locator("#detail-view")).to_be_visible()
            assert len(submissions) == count + 1
            assert upload_mode["resolutions"] == 2
            assert page.evaluate("sessionStorage.getItem('gromacs.pending-submission')") is None

            # Progress announces known, out-of-range, unknown and reset states.
            for value, expected in ((42.5, "42.5"), (130, "100"), (-8, "0"), (None, None), ("bad", None)):
                page.evaluate(
                    """async value => {
                    const {core, jobs} = window.modules;
                    core.state.activeJobId = 'synthetic-progress';
                    core.state.activeJob = {id:'synthetic-progress',name:'进度测试',status:'running',workflow:'run_tpr',step_index:1,step_count:3,progress_percent:value};
                    await jobs.renderActiveJob();
                }""",
                    value,
                )
                progress = page.locator("#job-progress")
                if expected is None:
                    expect(progress).not_to_have_attribute("aria-valuenow", re.compile(".*"))
                    expect(progress).to_have_attribute("aria-valuetext", "运行中，暂无法估算进度")
                else:
                    expect(progress).to_have_attribute("aria-valuenow", expected)
            page.evaluate("window.modules.jobs.forgetDeletedJob('synthetic-progress')")
            expect(page.locator("#job-progress")).to_have_attribute("aria-valuenow", "0")
            expect(page.locator("#job-progress")).to_have_attribute("aria-valuetext", "未选择任务")
            expect(page.locator("#job-progress")).to_have_attribute("data-unknown", "false")
            # Background status changes preserve the keyboard target, falling
            # back to this task's selection button if its action disappears.
            page.evaluate("""() => {
                const {core, jobs} = window.modules;
                core.state.jobs = [{id:'focus-job',name:'焦点测试',status:'running',workflow:'run_tpr'}];
                jobs.renderJobs();
                document.querySelector('[data-job-id="focus-job"] [data-action="cancel"]').focus();
                core.state.jobsBusy = true;
                jobs.renderJobs();
            }""")
            expect(page.locator('[data-job-id="focus-job"] [data-action="cancel"]')).to_be_focused()
            page.evaluate("window.modules.core.state.jobs[0].status = 'completed'; window.modules.jobs.renderJobs()")
            expect(page.locator('[data-job-id="focus-job"] .job-select')).to_be_focused()
            assert not errors, errors
            assert not any(pending.values()), "Held requests must all be released"
            print("PASS field navigation, preview races, safe errors, upload cancellation/recovery, keyboard focus and progress ARIA")
        except Exception:
            print(
                page.evaluate("""() => ({active:document.activeElement?.tagName + '#' + document.activeElement?.id,
                errors:document.querySelector('#submission-errors')?.textContent})""")
            )
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    check_submission(parser.parse_args().artifact_dir)
