"""Exercise product workflows in the real browser without touching a live service.

All requests are intercepted, including external URLs. The fixture implements the
public API contract; it never launches GROMACS or reads a user's runtime directory.
"""

from __future__ import annotations

import argparse
import copy
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
ORIGIN = "http://gromacs-product.test"


def check_job_comparison(page, screenshot):
    """Check the read-only comparison with realistic values and a delayed old response."""
    url = f"{ORIGIN}/api/jobs/product-1/diff/product-2"
    old = []
    values = [
        ("force_field", "amber99sb-ildn", "amber14sb"),
        ("md.dt", 0.002, 0.001), ("md.nsteps", 5000000, 10000000),
        ("md.ref_t", [300, 300], [310, 310]), ("md.tcoupl", "v-rescale", "nose-hoover"),
        ("ntomp", 0, 8), ("replicas", 3, 5), ("run.seed", None, 712),
        ("verbose", False, True), ("water_model", "tip3p", "opc"),
        *[(f"protocol.stage_{n:02}.duration_ps", 100, 200) for n in range(24)],
        ("output.note", "", '<b>not markup</b>'),
        ("selection.groups", ["Protein_" + "A" * 300], ["Solvent"]),
    ]
    differences = [{"path": k, "left": a, "right": b} for k, a, b in values]
    mode = "data"

    def respond(route):
        assert route.request.method == "GET"
        if mode == "hold":
            old.append(route)
        elif mode == "fail":
            route.fulfill(status=404, json={"detail": "Job not found"})
        else:
            route.fulfill(json={
                "left": {"id": "product-1", "name": "蛋白平衡 · 300 K"},
                "right": {"id": "product-2", "name": "蛋白平衡 · 310 K"},
                "differences": differences if mode == "data" else [],
            })

    page.route(url, respond)
    launcher = page.locator("#compare-job")
    menu = page.locator(".job-more-actions > summary")
    sidebar = page.locator("#job-filter")
    sidebar.fill("产品流程测试 1")
    expect(page.locator(".job-item")).to_have_count(1)
    menu.click()
    launcher.click()
    dialog = page.get_by_role("dialog", name="参数比较")
    visible = dialog.locator("tbody tr:visible")
    query = dialog.get_by_role("searchbox", name="搜索参数名或值")
    target_search = dialog.get_by_role("searchbox", name="搜索全部历史对照任务")
    status = dialog.get_by_role("status")
    expect(visible).to_have_count(len(values))
    expect(dialog.get_by_role("columnheader", name="当前任务 蛋白平衡 · 300 K")).to_be_visible()
    expect(dialog.get_by_role("columnheader", name="对照任务 蛋白平衡 · 310 K")).to_be_visible()
    target_search.fill("没有这个任务")
    expect(status).to_have_text("无对照任务")
    expect(visible).to_have_count(0)
    target_search.fill("产品流程测试 2")
    expect(visible).to_have_count(len(values))
    expect(sidebar).to_have_value("产品流程测试 1")
    screenshot("comparison")
    for term, expected in [("AMBER14SB", '"amber14sb"'), ("FALSE", "false"), ("ntomp", "0"), ("null", "未设置"), ("output.note", '""'), ("Solvent", '"Protein_')]:
        query.fill(term)
        expect(visible).to_have_count(1)
        expect(visible).to_contain_text(expected)
        expect(query).to_be_focused()
    assert dialog.locator(".diff-scroll").evaluate("el => el.scrollWidth <= el.clientWidth"), "Long values should wrap"
    query.fill("not markup")
    expect(visible.locator("td").last).to_have_text('"<b>not markup</b>"')
    expect(visible.locator("b")).to_have_count(0)
    query.fill("no-such-parameter")
    expect(status).to_have_text(f"匹配 0 / {len(values)} 项")
    dialog.get_by_role("button", name="清除筛选").click()
    expect(query).to_be_focused()
    expect(visible).to_have_count(len(values))
    scroll = dialog.locator(".diff-scroll")
    assert scroll.evaluate("el => { el.scrollTop = el.scrollHeight; return el.scrollTop > 0 && Math.abs(el.querySelector('thead th').getBoundingClientRect().top - el.getBoundingClientRect().top) < 2; }")
    query.fill("md.")
    dialog.get_by_role("button", name="清除参数搜索").click()
    expect(query).to_be_focused()
    page.keyboard.press("Escape")
    expect(launcher).to_be_focused()

    mode = "hold"
    with page.expect_request(url):
        launcher.click()
    expect(status).to_have_text("正在比较…")
    dialog.get_by_role("button", name="关闭参数比较").click()
    mode = "fail"
    launcher.click()
    expect(status).to_have_text("比较失败")
    expect(dialog).to_contain_text("任务不存在")
    mode = "same"
    dialog.get_by_role("button", name="重新比较").click()
    expect(dialog).to_contain_text("两个任务没有参数差异")
    assert len(old) == 1
    old[0].fulfill(json={"left": {"id": "old"}, "right": {"id": "stale"}, "differences": differences})
    expect(status).to_have_text("共 0 项差异")
    expect(query).to_be_disabled()
    expect(visible).to_have_count(0)
    page.keyboard.press("Escape")
    expect(launcher).to_be_focused()
    menu.click()
    sidebar.fill("")
    expect(page.locator(".job-item")).to_have_count(2)
    page.unroute(url, respond)


def check_clone_guard(page):
    button = page.locator("#clone-job")
    pending = []
    url = f"{ORIGIN}/api/jobs/product-1/clone"
    page.route(url, lambda route: pending.append(route))
    button.click()
    page.locator("#submit-warning-cancel").click()
    assert not pending
    button.click()
    with page.expect_request(url):
        page.locator("#submit-warning-confirm").click()
    expect(button).to_be_disabled()
    expect(button).to_have_text("正在复制…")
    # Even a queued event while the first write is pending cannot start another one.
    button.dispatch_event("click")
    expect(page.locator("#submit-warning")).to_be_hidden()
    assert len(pending) == 1 and pending[0].request.method == "POST"
    pending[0].fulfill(status=503, json={"detail": "测试：未创建任务"})
    expect(button).to_be_enabled()
    expect(button).to_have_text("复制并运行")
    page.unroute(url)


def check_viewport(browser, width: int, height: int, scheme: str, artifact_dir: Path | None, *, legacy: bool = False, first_use: bool = False, inspect=None, configure=None, context_options=None) -> None:
    jobs = [{
        "id": f"product-{index}", "name": f"产品流程测试 {index}", "workflow": "run_tpr",
        "status": "completed", "created_at": "2026-10-19T00:00:00Z", "updated_at": "2026-10-19T01:00:00Z",
        "step_count": 1, "step_index": 1, "progress_percent": 100, "params": {},
        "project": "原项目", "tags": ["WT"], "notes": "原始备注", "pinned": False,
    } for index in (1, 2)]
    if first_use:
        jobs.clear()
    templates: list[dict] = []
    errors: list[str] = []
    pending: dict = {}
    preview_payloads: list[dict] = []
    control = {"hold_product": False, "hold_patch": False, "hold_help": False, "hold_diagnostics": False, "hold_summary": False, "fail_files": False}

    def catalog():
        projects = sorted({job["project"] for job in jobs if job["project"]})
        return {"templates": copy.deepcopy(templates), "projects": [{"name": name, "count": sum(job["project"] == name for job in jobs)} for name in projects], "defaults": {"replicas": 3, "force_field": "amber19sb", "water_model": "opc", "verbose": False, "ignh": False}}

    def fulfill(route, payload):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def fixture(route):
        parsed = urlsplit(route.request.url)
        assert parsed.hostname == "gromacs-product.test", f"Unexpected external request: {route.request.url}"
        path, query, method = parsed.path, parse_qs(parsed.query), route.request.method
        payload = None
        if path == "/api/health":
            payload = {"settings": {"gmx_bin": "gmx", "runtime_root": "isolated-browser-fixture", "max_parallel": 1}, "gromacs": {"available": False, "message": "浏览器回归不运行模拟"}, "diagnostics": {}}
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
            if legacy:
                route.fulfill(status=404, content_type="application/json", body='{"detail":"Not Found"}')
                return
            payload = catalog()
            if control["hold_product"]:
                control["hold_product"] = False
                pending["product"] = (route, payload)
                return
        elif path == "/api/examples/start":
            assert first_use and method == "POST", "Example creation is only allowed in the isolated first-use fixture"
            payload = {"id": "first-example", "name": "首次教学示例", "workflow": "run_tpr", "status": "queued", "is_example": True,
                       "created_at": "2026-10-19T00:00:00Z", "step_count": 1, "step_index": 0, "params": {}, "project": "", "tags": [], "notes": ""}
            jobs.append(copy.deepcopy(payload))
        elif path == "/api/templates" and method == "POST":
            incoming = route.request.post_data_json
            payload = {**incoming, "id": f"template-{len(templates) + 1}", "revision": 1 + sum(item["name"] == incoming["name"] for item in templates), "created_at": "2026-10-19T00:00:00Z", "schema_version": 1}
            # The service stores portable protocol parameters without job identity.
            payload["params"] = {key: value for key, value in incoming["params"].items() if key not in {"name", "project", "tags", "notes", "files", "dry_run"} and not key.endswith(("_file", "_bin"))}
            templates.insert(0, payload)
        elif path.startswith("/api/templates/") and method == "DELETE":
            template_id = path.rsplit("/", 1)[1]
            templates[:] = [item for item in templates if item["id"] != template_id]
            payload = {"deleted": template_id}
        elif path == "/api/preview":
            preview_payloads.append(route.request.post_data_json)
            if legacy and {"project", "tags", "notes"} & preview_payloads[-1].keys():
                # The first preview may precede capability discovery. Emulate
                # the old API's validation error, then require a compatible retry.
                route.fulfill(status=422, content_type="application/json", body='{"detail":"Extra inputs are not permitted"}')
                return
            payload = {"commands": [], "mdp_overrides": []}
        elif path == "/api/protocol-preview":
            payload = {"stages": []}
        elif path in {"/api/jobs", "/api/research/jobs"}:
            assert method == "GET", "Product regression must never submit a simulation"
            project = query.get("project", [""])[0]
            search = query.get("search", [""])[0]
            selected = [job for job in jobs if (not project or job["project"] == project) and search in job["name"]]
            payload = {"jobs": selected, "total": len(selected), "offset": 0, "limit": 50, "has_more": False}
        elif path.startswith("/api/jobs/"):
            parts = path.split("/")
            job = next(item for item in jobs if item["id"] == parts[3])
            if len(parts) == 4:
                payload = job
            elif parts[4] == "organization":
                assert method == "PATCH"
                job.update(route.request.post_data_json)
                payload = copy.deepcopy(job)
                if control["hold_patch"]:
                    control["hold_patch"] = False
                    pending["patch"] = (route, payload)
                    return
            elif parts[4] == "files":
                if control["fail_files"]:
                    control["fail_files"] = False
                    route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "文件服务暂时不可用"}))
                    return
                payload = {"files": [], "truncated": False}
            elif parts[4] == "summary":
                payload = {"quality": [{"file": "quality_report.json", "status": "passed", "notes": ["核对采样充分性"]}], "input_count": 2 if job["id"] == "product-1" else 99, "manifest": True}
                if control["hold_summary"]:
                    control["hold_summary"] = False
                    pending["summary"] = (route, payload)
                    return
            elif parts[4] == "plots":
                payload = {"plots": [], "metrics": [], "replicas": []}
            elif parts[4] == "research":
                route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "暂无研究报告"}))
                return
        elif path == "/api/help/document":
            name = query["name"][0]
            if control["hold_help"]:
                control["hold_help"] = False
                pending["help"] = route
                return
            route.fulfill(content_type="text/plain", body=f"{name} 使用说明\n这里是所选文档。")
            return
        elif path == "/api/support/diagnostics":
            payload = {"system": "Linux", "contents": "仅环境摘要，不含输入文件"}
            if control["hold_diagnostics"]:
                control["hold_diagnostics"] = False
                pending["diagnostics"] = (route, payload)
                return
        elif path == "/api/support/usage":
            payload = {"scope": "本机任务记录", "uploaded": False, "statuses": {"completed": 2}}
        if path.startswith("/api/"):
            assert payload is not None, f"Unexpected API call: {method} {path}"
            fulfill(route, payload)
            return
        assert method == "GET"
        target = APP / "templates/index.html" if path == "/" else APP / path.lstrip("/")
        target = target.resolve()
        target.relative_to(APP.resolve())
        assert target.is_file(), f"Unknown asset: {path}"
        route.fulfill(content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes())

    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, **(context_options or {}))
    context.route("**/*", fixture)
    page = context.new_page()
    if configure:
        configure(page)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("""
        window.testStreams = [];
        window.EventSource = class extends EventTarget {
            constructor(url) { super(); this.url = url; this.readyState = 1; window.testStreams.push(this); }
            close() { this.readyState = 2; }
        };
        window.composeEvents = [];
        for (const type of ['pointerdown', 'change', 'mouseup', 'click', 'compose-reveal-field']) {
            document.addEventListener(type, event => {
                const next = document.querySelector('#compose-next');
                const rect = next?.getBoundingClientRect();
                window.composeEvents.push({type, target: event.target.id || event.target.className,
                    step: document.querySelector('#manual-compose')?.dataset.composeStep,
                    next: rect && {x: rect.x, y: rect.y}, at: performance.now()});
                if (window.composeEvents.length > 50) window.composeEvents.shift();
            }, true);
        }
    """)

    def screenshot(name):
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Horizontal overflow at {width}"
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            if name == "compose":
                page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(artifact_dir / f"product-{name}-{width}-{scheme}.png"))

    try:
        page.goto(ORIGIN, wait_until="networkidle")
        if inspect:
            inspect(page)
            assert not errors, errors
            return
        if first_use:
            expect(page.locator(".job-item")).to_have_count(0)
            assert page.evaluate("!new URL(window.testStreams.at(-1).url, location.href).searchParams.has('job_id')")
            page.locator('[data-workspace-view="detail"]').click()
            page.locator('[data-detail-tab="files"]').click()
            expect(page.locator("#output-files")).to_contain_text("未选择任务")
            expect(page.locator("#file-filter")).to_be_disabled()
            page.locator('[data-detail-tab="overview"]').click()
            page.locator('[data-workspace-view="compose"]').click()
            page.locator("#start-guide > summary").click()
            page.locator("#start-example").click()
            page.locator("#submit-warning-confirm").click()
            expect(page.locator("#active-job-title")).to_have_text("首次教学示例")
            page.wait_for_function("new URL(window.testStreams.at(-1).url, location.href).searchParams.get('job_id') === 'first-example'", timeout=5000)
            assert page.evaluate("window.testStreams[0].readyState === 2"), "The old unscoped event stream must be closed"
            for status in ("running", "completed"):
                jobs[0].update(status=status, updated_at=f"2026-10-19T00:00:0{1 if status == 'running' else 2}Z")
                page.evaluate("job => window.testStreams.at(-1).dispatchEvent(new MessageEvent('job', {data: JSON.stringify(job)}))", jobs[0])
                expect(page.locator("#job-status")).to_have_attribute("data-state", status)
            expect(page.locator("#result-summary-content")).to_contain_text("教学示例结果")
            expect(page.locator("#download-result-bundle")).to_have_attribute("href", "/api/jobs/first-example/result-bundle")
            assert not errors, errors
            print("PASS empty-history example subscribes to task progress and completion")
            return
        expect(page.locator(".job-item")).to_have_count(2)
        if legacy:
            expect(page.locator("#product-availability")).to_contain_text("现有任务结束")
            expect(page.locator("#preview-count")).to_have_text("0 步")
            assert preview_payloads and not ({"project", "tags", "notes"} & preview_payloads[-1].keys()), "Confirmed legacy services must receive a compatible preview retry"
            for selector in ("#open-library", "#project-filter", "#open-help", "#start-example"):
                expect(page.locator(selector)).to_be_disabled()
            expect(page.locator("#download-result-bundle")).not_to_be_visible()
            expect(page.locator(".organization-panel")).not_to_be_visible()
            page.locator('[data-workspace-view="compose"]').click()
            page.locator("#job-name").fill("旧服务仍可编辑任务")
            expect(page.locator("#job-name")).to_have_value("旧服务仍可编辑任务")
            assert not errors, errors
            print("PASS older service capability fallback")
            return
        page.locator('[data-workspace-view="compose"]').click()
        expect(page.locator('[name="verbose"]')).not_to_be_checked()
        expect(page.locator('[name="ignh"]')).not_to_be_checked()
        expect(page.locator("#guided-mode")).to_have_attribute("aria-pressed", "true")
        # Steps preserve the same form and expose one final submission action.
        expect(page.locator("#submit-job")).to_be_hidden()
        page.locator('[data-compose-anchor="resources"]').click()
        page.locator("#submit-job").click()
        page.locator(".submission-error-link").filter(has_text="请先填写任务名").click()
        expect(page.locator("#job-name")).to_be_focused()
        expect(page.locator('[data-compose-anchor="inputs"]')).to_have_attribute("aria-current", "step")
        page.locator("#job-name").fill("步骤切换保留的任务")
        page.locator("#compose-next").click()
        expect(page.locator(".form-column")).to_be_hidden()
        expect(page.locator(".mdp-section")).to_be_visible()
        page.locator("#compose-next").click()
        expect(page.locator("#compose-review")).to_contain_text("步骤切换保留的任务")
        expect(page.locator("#submit-job")).to_be_visible()
        expect(page.locator("#compose-next")).to_be_hidden()
        page.locator('[data-compose-anchor="inputs"]').click()
        expect(page.locator("#job-name")).to_have_value("步骤切换保留的任务")
        page.locator("#expert-mode").click()
        expect(page.locator('[name="ntomp"]')).to_be_visible()
        page.locator("#guided-mode").click()
        expect(page.locator('[name="ntmpi"]')).not_to_be_visible()
        page.locator("#workflow-intent").select_option("tpr")
        expect(page.locator('[name="workflow"][value="run_tpr"]')).to_be_checked()
        page.locator("#workflow-intent").select_option("simulation")
        page.locator("#job-name").fill("保留的任务名称")
        page.locator(".new-job-organization > summary").click()
        page.locator('[name="project"]').fill("新研究项目")
        page.locator('[name="tags"]').fill("WT, 300K")
        page.locator('[name="notes"]').fill("模板应用不应删除此备注")
        page.locator('[data-compose-anchor="resources"]').click()
        page.locator('[name="replicas"]').fill("2")
        page.locator('[data-compose-anchor="inputs"]').click()
        page.locator("#files").set_input_files({"name": "protein.pdb", "mimeType": "text/plain", "buffer": b"END\n"})
        expect(page.locator("#input-mapping")).to_contain_text("protein.pdb")
        page.locator('[data-compose-anchor="resources"]').click()
        expect(page.locator("#compose-review")).to_contain_text("protein.pdb")
        page.locator('[data-compose-anchor="inputs"]').click()
        assert page.locator("#files").evaluate("field => field.files[0].name") == "protein.pdb"
        screenshot("compose")

        # A modal must trap focus and return to the invoking control on Escape.
        page.locator(".job-name-field .field-help").click()
        expect(page.locator("#field-help-dialog")).to_be_visible()
        assert page.locator("#field-help-dialog").evaluate("dialog => dialog.matches(':modal')")
        expect(page.locator('[data-close-dialog="field-help-dialog"]')).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator(".job-name-field .field-help")).to_be_focused()
        page.locator('.workflow-switch label:has([value="protein_ligand_md"])').click()
        page.locator("#add-ligand").click()
        new_ligand = page.locator("#ligand-list .ligand-card").last
        new_ligand.locator('[data-ligand-field="mode"]').select_option("acpype")
        charge_help = new_ligand.locator('label:has([data-ligand-field="charge"]) .field-help')
        charge_help.click()
        expect(page.locator("#field-help-content")).to_contain_text("净电荷")
        page.keyboard.press("Escape")
        expect(charge_help).to_be_focused()
        page.locator('.workflow-switch label:has([value="protein_md"])').click()

        page.locator("#open-library").click()
        expect(page.locator("#apply-template")).to_be_disabled()
        expect(page.locator("#template-empty")).to_be_visible()
        expect(page.locator("#template-details")).to_be_hidden()
        expect(page.get_by_label("导入模板")).to_be_enabled()
        page.locator("#library-dialog details > summary").click()
        page.locator("#template-name").fill("两副本协议")
        page.locator("#template-applicability").fill("水溶性蛋白；需要重新核对输入")
        page.locator("#template-source").fill("实验室协议 2026")
        page.locator('#template-form button[type="submit"]').click()
        expect(page.locator("#template-select")).to_have_value("template-1")
        expect(page.get_by_role("article", name="两副本协议")).to_be_visible()
        expect(page.locator("#template-workflow")).to_contain_text("蛋白 MD")
        expect(page.locator("#template-origin")).to_have_text("实验室协议 2026")
        expect(page.locator("#template-note")).to_be_hidden()
        page.locator("#template-description").fill("每个副本分别记录输出。")
        page.locator('#template-form button[type="submit"]').click()
        expect(page.locator("#template-select")).to_have_value("template-2")
        expect(page.locator("#template-select option:checked")).to_contain_text("v2")
        page.locator("#library-dialog details > summary").click()
        page.locator("#template-select").select_option("template-1")
        expect(page.locator("#template-revision")).to_have_text("v1")
        expect(page.locator("#template-note-label")).to_be_hidden()
        page.locator("#template-select").select_option("template-2")
        expect(page.locator("#template-revision")).to_have_text("v2")
        expect(page.locator("#template-note")).to_have_text("每个副本分别记录输出。")
        expect(page.locator("#template-empty")).to_be_hidden()
        screenshot("library")
        with page.expect_download() as download_info:
            page.locator("#export-template").click()
        exported = json.loads(Path(download_info.value.path()).read_text())
        assert exported["schema_version"] == 1 and exported["params"]["replicas"] == 2
        assert "id" not in exported and "name" not in exported["params"]
        page.locator("#import-template").set_input_files({"name": "invalid.json", "mimeType": "application/json", "buffer": b"null"})
        expect(page.locator("#template-error")).to_contain_text("JSON 对象")
        assert len(templates) == 2

        # An older catalogue request must not undo the result of a later import.
        page.locator('[data-close-dialog="library-dialog"]').click()
        control["hold_product"] = True
        page.locator("#open-library").click()
        exported["name"] = '<img src=x onerror="window.productXss=true">'
        exported["params"]["water_model"] = "laboratory-water"
        exported["applicability"] = exported["source"] = ""
        page.locator("#import-template").set_input_files({"name": "protocol.json", "mimeType": "application/json", "buffer": json.dumps(exported).encode()})
        expect(page.locator("#template-select")).to_have_value("template-3")
        fulfill(*pending.pop("product"))
        expect(page.locator("#template-select")).to_have_value("template-3")
        expect(page.locator("#template-card-title")).to_have_text(exported["name"])
        expect(page.locator("#template-scope")).to_contain_text("未填写")
        expect(page.locator("#template-origin")).to_have_text("未注明")
        assert not page.evaluate("Boolean(window.productXss)")
        page.locator("#apply-template").click()
        expect(page.locator("#submit-warning")).to_be_visible()
        page.locator("#submit-warning-cancel").click()
        expect(page.locator("#library-dialog")).to_be_visible()
        expect(page.locator("#apply-template")).to_be_focused()
        page.locator("#apply-template").click()
        page.locator("#submit-warning-confirm").click()
        expect(page.locator("#job-name")).to_have_value("保留的任务名称")
        expect(page.locator('[name="project"]')).to_have_value("新研究项目")
        expect(page.locator('[name="notes"]')).to_have_value("模板应用不应删除此备注")
        expect(page.locator('[name="water_model"]')).to_have_value("laboratory-water")
        expect(page.locator('[name="replicas"]')).to_have_value("2")
        assert page.locator("#files").evaluate("input => input.files.length") == 0
        expect(page.locator("#input-mapping")).to_be_empty()
        page.locator("#open-library").click()
        page.locator("#delete-template").click()
        page.locator("#submit-warning-cancel").click()
        expect(page.locator("#delete-template")).to_be_focused()
        assert len(templates) == 3
        page.locator("#delete-template").click()
        page.locator("#submit-warning-confirm").click()
        expect(page.locator("#template-select option")).to_have_count(2)
        page.locator('[data-close-dialog="library-dialog"]').click()

        # Saving while typing must not replace the user's newer edits.
        page.locator('[data-workspace-view="detail"]').click()
        page.locator(".organization-panel > summary").click()
        page.locator("#job-project").fill("新研究项目")
        page.locator("#job-tags").fill("WT，300K")
        page.locator("#job-notes").fill("第一次提交")
        page.locator("#job-pinned").check()
        expect(page.locator("#organization-summary")).to_contain_text("未保存")
        expect(page.locator("#organization-draft-status")).to_contain_text("本机草稿已保留")
        page.locator('[data-job-id="product-2"] .job-select').click()
        expect(page.locator("#job-notes")).to_have_value("原始备注")
        page.locator('[data-job-id="product-1"] .job-select').click()
        expect(page.locator("#job-project")).to_have_value("新研究项目")
        expect(page.locator("#job-tags")).to_have_value("WT，300K")
        expect(page.locator("#job-notes")).to_have_value("第一次提交")
        expect(page.locator("#job-pinned")).to_be_checked()
        page.once("dialog", lambda dialog: dialog.accept())
        page.reload(wait_until="networkidle")
        page.locator('[data-workspace-view="detail"]').click()
        page.locator(".organization-panel > summary").click()
        expect(page.locator("#job-project")).to_have_value("新研究项目")
        expect(page.locator("#job-tags")).to_have_value("WT，300K")
        expect(page.locator("#job-notes")).to_have_value("第一次提交")
        expect(page.locator("#job-pinned")).to_be_checked()
        expect(page.locator("#organization-draft-status")).to_contain_text("未保存")
        screenshot("draft")
        control["hold_patch"] = True
        page.locator('#organization-form button[type="submit"]').click()
        page.locator("#job-notes").fill("请求期间继续编辑")
        fulfill(*pending.pop("patch"))
        expect(page.locator('#organization-form button[type="submit"]')).to_be_enabled()
        expect(page.locator("#job-notes")).to_have_value("请求期间继续编辑")
        page.locator('#organization-form button[type="submit"]').click()
        expect(page.locator("#organization-summary")).to_contain_text("新研究项目")
        expect(page.locator('#organization-form button[type="submit"]')).to_be_enabled()
        assert jobs[0]["notes"] == "请求期间继续编辑" and jobs[0]["tags"] == ["WT", "300K"] and jobs[0]["pinned"]
        expect(page.locator("#organization-draft-status")).not_to_contain_text("未保存")
        assert page.evaluate("localStorage.getItem('gromacs-organization-draft-v1:product-1')") is None
        expect(page.locator("#result-summary-content")).to_contain_text("科学结论分别判断")
        # Chromium's native HTTP downloads bypass request interception. Verify the
        # selected-job link here; backend bundle tests validate its ZIP contents.
        expect(page.locator("#download-result-bundle")).to_have_attribute("href", "/api/jobs/product-1/result-bundle")
        expect(page.locator("#download-result-bundle")).to_be_visible()
        screenshot("result")
        check_job_comparison(page, screenshot)
        check_clone_guard(page)

        # A transient file failure is actionable and retrying the same task works.
        control["fail_files"] = True
        page.locator('[data-detail-tab="files"]').click()
        expect(page.locator("#output-files")).to_contain_text("文件列表加载失败")
        expect(page.locator("#file-count")).to_have_text("读取失败")
        screenshot("files-error")
        page.locator("#output-files").get_by_role("button", name="重试加载文件").click()
        expect(page.locator("#output-files")).to_contain_text("暂无输出文件")
        expect(page.locator("#file-count")).to_have_text("0 个")
        expect(page.locator("#file-pagination")).to_be_hidden()
        expect(page.locator("#output-files button")).to_have_count(0)
        page.locator('[data-detail-tab="overview"]').click()

        # Late metadata and summary responses from another job cannot replace the
        # selected job's form, quality summary or export target.
        control["hold_summary"] = True
        page.locator('[data-job-id="product-2"] .job-select').click()
        expect(page.locator("#active-job-title")).to_have_text("产品流程测试 2")
        page.locator("#job-notes").fill("另一个任务的记录")
        control["hold_patch"] = True
        page.locator('#organization-form button[type="submit"]').click()
        page.locator('[data-job-id="product-1"] .job-select').click()
        expect(page.locator("#active-job-title")).to_have_text("产品流程测试 1")
        fulfill(*pending.pop("summary"))
        fulfill(*pending.pop("patch"))
        expect(page.locator('#organization-form button[type="submit"]')).to_be_enabled()
        expect(page.locator("#job-notes")).to_have_value("请求期间继续编辑")
        expect(page.locator("#result-summary-content")).to_contain_text("2 个输入文件")
        expect(page.locator("#result-summary-content")).not_to_contain_text("99 个输入文件")
        expect(page.locator("#download-result-bundle")).to_have_attribute("href", "/api/jobs/product-1/result-bundle")

        page.locator("#project-filter").select_option("新研究项目")
        expect(page.locator(".job-item")).to_have_count(1)
        page.locator("#open-help").click()
        expect(page.locator("#help-content")).to_contain_text("QUICKSTART")
        control["hold_help"] = True
        page.locator("#help-document").select_option("USAGE")
        page.locator("#help-document").select_option("WINDOWS")
        expect(page.locator("#help-content")).to_contain_text("WINDOWS")
        pending.pop("help").fulfill(content_type="text/plain", body="旧 USAGE 文档")
        expect(page.locator("#help-content")).to_contain_text("WINDOWS")
        page.locator("#help-dialog details > summary").click()
        control["hold_diagnostics"] = True
        page.locator("#preview-diagnostics").click()
        page.locator("#show-local-usage").click()
        expect(page.locator("#diagnostic-preview")).to_contain_text("本机任务记录")
        fulfill(*pending.pop("diagnostics"))
        expect(page.locator("#diagnostic-preview")).to_contain_text("本机任务记录")
        expect(page.locator("#download-diagnostics")).to_be_disabled()
        page.locator("#preview-diagnostics").click()
        expect(page.locator("#download-diagnostics")).to_be_enabled()
        screenshot("help")
        page.keyboard.press("Escape")
        expect(page.locator("#open-help")).to_be_focused()
        page.locator("#project-filter").select_option("")
        expect(page.locator(".job-item")).to_have_count(2)
        jobs[1].update(status="running", progress_percent=None, progress_observation={"state": "waiting", "message": "等待原生日志更新"})
        page.locator('[data-job-id="product-2"] .job-select').click()
        expect(page.locator("#job-percent")).to_have_text("暂无法估算")
        expect(page.locator("#job-progress")).to_have_attribute("data-unknown", "true")
        expect(page.locator("#job-performance")).to_have_text("-")
        page.locator('[data-job-id="product-1"] .job-select').click()
        expect(page.locator("#active-job-title")).to_have_text("产品流程测试 1")
        jobs[1].update(progress_percent=51.83, simulation_progress={"time_ps": 51830, "ns_per_day": 20, "eta_seconds": 3600}, progress_observation={"state": "stale", "message": "原生日志一段时间没有更新，请查看运行状态", "updated_at": "2026-10-19T01:00:00Z"})
        page.locator('[data-job-id="product-2"] .job-select').click()
        expect(page.locator("#job-percent")).to_have_text("51.8% · 51.830 ns")
        expect(page.locator("#progress-observation")).to_have_attribute("data-state", "stale")
        expect(page.locator("#progress-observation")).to_contain_text("最后更新")
        expect(page.locator("#job-performance")).to_have_text("20.00 ns/day")
        screenshot("progress")
        assert not errors, errors
        assert not pending, f"Unresolved responses: {list(pending)}"
        print(f"PASS product workflows {width}x{height} {scheme}")
    except Exception:
        diagnostics = artifact_dir or ROOT / "runtime/frontend-quality/product"
        diagnostics.mkdir(parents=True, exist_ok=True)
        name = f"failure-{width}-{scheme}"
        page.screenshot(path=str(diagnostics / f"{name}.png"))
        (diagnostics / f"{name}.json").write_text(json.dumps(page.evaluate("window.composeEvents"), ensure_ascii=False, indent=2))
        print(f"Product interaction diagnostics: {diagnostics / name}")
        raise
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, help="Optional screenshots for visual QA")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height, scheme in ((1440, 1000, "light"), (1280, 800, "dark")):
                check_viewport(browser, width, height, scheme, args.artifact_dir)
            check_viewport(browser, 1440, 1000, "light", args.artifact_dir, legacy=True)
            check_viewport(browser, 1440, 1000, "light", args.artifact_dir, first_use=True)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
