"""Exercise the real frontend with synthetic API responses and no running server."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
ORIGIN = "http://gromacs.test"


def check_browser(artifact_dir: Path | None = None) -> None:
    jobs = [
        {
            "id": f"job-{index:02}",
            "name": f"合成测试任务 {index:02}",
            "directory_name": f"synthetic-{index:02}",
            "workflow": "run_tpr",
            "status": "completed",
            "created_at": "2026-10-06T00:00:00Z",
            "step_count": 1,
            "step_index": 1,
            "progress_percent": 100,
            "params": {},
        }
        for index in range(60)
    ]
    requests: list[tuple[str, str]] = []
    errors: list[str] = []
    main_source = (APP / "static/js/main.js").read_text()
    core_match = re.search(r'"(\./core\.js\?[^\"]+)"', main_source)
    form_match = re.search(r'"(\./form\.js\?[^\"]+)"', main_source)
    assert core_match and form_match
    core_url = "/static/js/" + core_match.group(1).removeprefix("./")
    form_url = "/static/js/" + form_match.group(1).removeprefix("./")

    def fixture(route) -> None:
        parsed = urlsplit(route.request.url)
        path = parsed.path
        query = parse_qs(parsed.query)
        requests.append((route.request.method, route.request.url))
        payload = None
        if path == "/api/health":
            payload = {
                "settings": {"gmx_bin": "gmx", "runtime_root": "synthetic-test-only", "max_parallel": 1},
                "gromacs": {"available": False, "message": "合成界面测试，不运行模拟"},
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
        elif path == "/api/jobs":
            assert route.request.method == "GET", "Browser regression must never submit a simulation"
            search = query.get("search", [""])[0]
            matches = [job for job in jobs if search.lower() in (job["name"] + " " + job["id"]).lower()]
            offset = int(query.get("offset", ["0"])[0])
            payload = {
                "jobs": matches[offset : offset + 50],
                "total": len(matches),
                "offset": offset,
                "limit": 50,
                "has_more": offset + 50 < len(matches),
            }
        elif path == "/api/research/jobs":
            search = query.get("search", [""])[0]
            matches = [job for job in jobs if search.lower() in (job["name"] + " " + job["id"]).lower()]
            offset = int(query.get("offset", ["0"])[0])
            payload = {"jobs": matches[offset : offset + 50], "total": len(matches), "has_more": offset + 50 < len(matches)}
        elif path == "/api/research/compare":
            assert route.request.post_data_json == {"reference_jobs": ["job-00"], "comparison_jobs": ["job-55"]}
            unsafe_name = '<img src="/unexpected" onerror="window.researchInjected=true">'
            reference, other = {"id": "job-00", "name": unsafe_name}, {"id": "job-55", "name": "对照来源"}
            warning = f"来源 {unsafe_name} (job-00)：取样少于 50 帧，分布和占有率估计可能不稳定。"
            payload = {"reference": [reference], "comparison": [other], "metrics": [], "residues": [], "protocol": [], "warnings": [warning],
                       "sources": [{"job": reference, "group": "reference", "frames": 25, "window_ns": [10.5, 99.5], "warnings": [warning]},
                                   {"job": other, "group": "comparison", "frames": 100, "window_ns": [10, 100], "warnings": []}]}
        elif path.startswith("/api/jobs/"):
            parts = path.split("/")
            job = next((job for job in jobs if job["id"] == parts[3]), None)
            if len(parts) == 4:
                payload = job
            elif parts[4] == "files":
                payload = {"files": [], "truncated": False}
            elif parts[4] == "summary":
                payload = {"quality": [], "input_count": 0, "manifest": False}
            elif parts[4] == "plots":
                payload = {"plots": [], "metrics": [], "replicas": []}
            elif parts[4] == "research":
                route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "合成任务无研究报告"}))
                return
        if path.startswith("/api/"):
            assert payload is not None, f"Unexpected API call: {route.request.method} {path}"
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            return
        target = APP / "templates/index.html" if path == "/" else APP / path.lstrip("/")
        target = target.resolve()
        target.relative_to(APP.resolve())
        if not target.is_file():
            route.fulfill(status=404, body="not found")
            return
        route.fulfill(status=200, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream", body=target.read_bytes())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width, height in ((1440, 1000), (768, 1024), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route(f"{ORIGIN}/**", fixture)
                page.add_init_script("""
                    window.testStreams = [];
                    window.EventSource = class extends EventTarget {
                        constructor(url) { super(); this.url = url; this.readyState = 1; window.testStreams.push(this); }
                        close() { this.readyState = 2; }
                    };
                """)
                page.goto(ORIGIN, wait_until="networkidle")
                expect(page.locator(".job-item")).to_have_count(50)
                expect(page.locator("#manual-compose")).to_be_visible()
                expect(page.locator("#agent-panel")).to_be_hidden()
                expect(page.locator("#detail-view")).to_be_hidden()
                expect(page.locator("#open-agent")).to_have_attribute("aria-pressed", "false")
                page.locator("#job-name").fill("切换视图保留模拟草稿")
                page.locator("#files").set_input_files({"name": "retained.pdb", "mimeType": "text/plain", "buffer": b"END\n"})
                page.locator("#open-agent").click()
                expect(page.locator("#agent-panel")).to_be_visible()
                expect(page.locator("#manual-compose")).to_be_hidden()
                expect(page.locator("#submit-job")).to_be_hidden()
                expect(page.locator("#open-agent")).to_have_attribute("aria-pressed", "true")
                page.locator("#agent-message").fill("切换视图保留未发送的提问")
                page.locator('[data-workspace-view="detail"]').click()
                expect(page.locator("#agent-panel")).to_be_hidden()
                expect(page.locator("#detail-view")).to_be_visible()
                page.locator('[data-workspace-view="compose"]').click()
                expect(page.locator("#job-name")).to_have_value("切换视图保留模拟草稿")
                assert page.locator("#files").evaluate("node => node.files[0].name") == "retained.pdb"
                page.locator("#open-agent").click()
                expect(page.locator("#agent-message")).to_have_value("切换视图保留未发送的提问")
                page.evaluate(
                    """payload => window.testStreams.at(-1).dispatchEvent(
                    new MessageEvent('jobs', {data: JSON.stringify(payload)}))""",
                    {"jobs": jobs[:50], "total": 60, "has_more": True},
                )
                expect(page.locator("#agent-panel")).to_be_visible()
                expect(page.locator("#manual-compose")).to_be_hidden()
                page.locator('[data-workspace-view="compose"]').click()
                if width > 1180:
                    page.locator("#toggle-sidebar").click()
                    expect(page.locator("#task-sidebar")).to_be_hidden()
                    expect(page.locator("#toggle-sidebar")).to_have_attribute("aria-expanded", "false")
                    assert not page.locator("#compose-view").evaluate("node => node.inert")
                    page.locator("#toggle-sidebar").click()
                    expect(page.locator("#task-sidebar")).to_be_visible()
                    expect(page.locator("#toggle-sidebar")).to_have_attribute("aria-expanded", "true")
                if width <= 1180:
                    page.locator("#toggle-sidebar").click()
                    expect(page.locator("#job-filter")).to_be_focused()
                    expect(page.locator("#sidebar-backdrop")).to_be_visible()
                    assert page.locator("#compose-view").evaluate("node => node.inert")
                    page.locator("#job-name").focus()
                    expect(page.locator("#job-filter")).to_be_focused()
                    for _ in range(8):
                        page.keyboard.press("Tab")
                        assert not page.evaluate("document.activeElement.closest('#compose-view, #detail-view') !== null")
                    page.locator("#sidebar-backdrop").click(position={"x": width - 12, "y": 12})
                    expect(page.locator("#toggle-sidebar")).to_be_focused()
                    expect(page.locator("#sidebar-backdrop")).to_be_hidden()
                    assert not page.locator("#compose-view").evaluate("node => node.inert")
                    page.locator("#toggle-sidebar").click()
                    page.keyboard.press("Escape")
                    expect(page.locator("#toggle-sidebar")).to_be_focused()
                    expect(page.locator("#toggle-sidebar")).to_have_attribute("aria-expanded", "false")
                    page.locator("#toggle-sidebar").click()
                page.locator("#jobs-next").click()
                expect(page.locator(".job-item")).to_have_count(10)
                page.locator('[data-job-id="job-55"] [data-action="select"]').first.click()
                expect(page.locator("#active-job-title")).to_have_text("合成测试任务 55")
                if width <= 1180:
                    page.locator("#toggle-sidebar").click()
                page.locator("#jobs-previous").click()
                expect(page.locator(".job-item")).to_have_count(50)
                expect(page.locator("#active-job-title")).to_have_text("合成测试任务 55")
                page.evaluate(
                    """payload => window.testStreams.at(-1).dispatchEvent(
                    new MessageEvent('jobs', {data: JSON.stringify(payload)}))""",
                    {"jobs": jobs[:50], "total": 60, "has_more": True},
                )
                expect(page.locator("#active-job-title")).to_have_text("合成测试任务 55")
                page.locator("#job-filter").fill("job-57")
                expect(page.locator(".job-item")).to_have_count(1)
                expect(page.locator(".job-item")).to_have_attribute("data-job-id", "job-57")
                expect(page.locator("#active-job-title")).to_have_text("合成测试任务 55")
                page.locator("#job-filter").fill("")
                expect(page.locator(".job-item")).to_have_count(50)
                if width <= 1180:
                    page.locator("#toggle-sidebar").click()
                page.locator("#analyze-job").focus()
                expect(page.locator("#analyze-job")).to_be_focused()
                duplicate_result = page.evaluate(
                    """async coreUrl => {
                    const {confirmDialog} = await import(coreUrl);
                    window.firstConfirmation = null;
                    confirmDialog({title: '确认回归测试', subtitle: '合成测试，不运行模拟', items: ['确认测试']})
                        .then(value => { window.firstConfirmation = value; });
                    return await confirmDialog({title: '重复请求', subtitle: '不覆盖之前的确认', items: ['重复测试']});
                }""",
                    core_url,
                )
                assert duplicate_result is False
                expect(page.locator("#submit-warning")).to_be_visible()
                expect(page.locator("#submit-warning-title")).to_have_text("确认回归测试")
                for _ in range(5):
                    page.keyboard.press("Tab")
                    assert page.evaluate("document.querySelector('#submit-warning').contains(document.activeElement)")
                page.evaluate("document.querySelector('#job-filter').focus()")
                assert page.evaluate("document.querySelector('#submit-warning').contains(document.activeElement)")
                page.keyboard.press("Escape")
                expect(page.locator("#submit-warning")).to_be_hidden()
                page.wait_for_function("window.firstConfirmation === false")
                expect(page.locator("#analyze-job")).to_be_focused()
                # Closing and immediately reopening must not consume the new confirmation.
                page.evaluate(
                    """async coreUrl => {
                    const {confirmDialog, closeConfirmDialog} = await import(coreUrl);
                    const options = {title: '第一次', subtitle: '测试', items: ['测试']};
                    confirmDialog(options);
                    closeConfirmDialog(false);
                    window.nextConfirmation = null;
                    confirmDialog({...options, title: '第二次'}).then(value => window.nextConfirmation = value);
                }""",
                    core_url,
                )
                expect(page.locator("#submit-warning-title")).to_have_text("第二次")
                page.locator("#submit-warning-confirm").click()
                page.wait_for_function("window.nextConfirmation === true")
                # Report selection searches the whole catalog, retaining chosen
                # reference/comparison jobs across both pages and searches.
                page.locator('[data-detail-tab="research"]').click()
                page.locator('[data-research-tab="comparison"]').click()
                expect(page.locator("#research-reference option")).to_have_count(50)
                page.locator("#research-reference").select_option("job-00")
                page.locator("#research-catalog-next").click()
                expect(page.locator("#research-other option")).to_have_count(10)
                expect(page.locator("#research-reference")).to_have_values(["job-00"])
                page.locator("#research-other").select_option("job-55")
                page.locator("#research-catalog-search").fill("job-57")
                expect(page.locator("#research-catalog-status")).to_contain_text("1 个报告")
                expect(page.locator("#research-reference")).to_have_values(["job-00"])
                expect(page.locator("#research-other")).to_have_values(["job-55"])
                expect(page.locator('#research-other option[value="job-57"]')).to_have_count(1)
                page.locator('#research-compare-form button[type="submit"]').click()
                comparison = page.locator("#research-compare-results")
                expect(comparison).to_contain_text("来源轨迹与实际取样范围")
                expect(comparison).to_contain_text("job-00")
                expect(comparison).to_contain_text("取样少于 50 帧")
                expect(comparison).to_contain_text("10.500–99.500")
                expect(comparison.locator("table").first.locator("tbody tr").first).to_contain_text("25")
                expect(comparison.locator("img")).to_have_count(0)
                assert not page.evaluate("Boolean(window.researchInjected)")
                assert not page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not errors, errors
                if artifact_dir:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(artifact_dir / f"frontend-{width}.png"), full_page=True)
                # Switching workflow defaults must not erase a custom PBC group.
                page.locator('[data-workspace-view="compose"]').click()
                page.locator("#expert-mode").click()
                center = page.locator('#job-form input[name="center_group"]')
                for workflow, group in (("postprocess", "Protein_Lig"), ("analysis_suite", "Protein"), ("analysis_rmsd", "Protein")):
                    choice = page.locator(f'input[name="workflow"][value="{workflow}"]')
                    choice.locator("..").click()
                    expect(choice).to_be_checked()
                    expect(center).to_have_value(group)
                center.fill("SelectedComplex")
                page.locator('input[name="workflow"][value="analysis_suite"]').locator("..").click()
                expect(center).to_have_value("SelectedComplex")
                # Drafts can contain arbitrary strings despite the numeric input
                # types. Restore them through the real form module and verify
                # HTML parsing cannot turn a value into attributes or elements.
                page.locator('input[name="workflow"][value="protein_ligand_md"]').locator("..").click()
                malicious_numbers = {
                    field: (
                        f'1" data-draft-injected="{field}" onfocus="window.ligandInjectionExecuted=true">'
                        f'<img data-draft-node="{field}" src="/draft-injection-{field}" '
                        'onerror="window.ligandInjectionExecuted=true"><input value="'
                    )
                    for field in ("count", "charge", "ph")
                }
                card_index = page.evaluate(
                    """async ({formUrl, values}) => {
                    const {addLigandRow, ligandCards, refreshRowMode} = await import(formUrl);
                    window.ligandInjectionExecuted = false;
                    const before = ligandCards().length;
                    const index = addLigandRow({key: 'draft-escape-test', mode: 'acpype', ...values});
                    if (ligandCards().length !== before + 1) throw new Error('Draft created extra ligand cards');
                    refreshRowMode(ligandCards().at(-1));
                    return index;
                }""",
                    {"formUrl": form_url, "values": malicious_numbers},
                )
                card = page.locator(f'#ligand-list .ligand-card[data-ligand-index="{card_index}"]')
                expect(card).to_have_count(1)
                expect(page.locator("[data-draft-injected], [data-draft-node]")).to_have_count(0)
                for field, value in malicious_numbers.items():
                    numeric = card.locator(f'[data-ligand-field="{field}"]')
                    expect(numeric).to_have_attribute("value", value)
                    expect(numeric).to_have_value("")
                assert card.evaluate(
                    "element => [...element.querySelectorAll('*')].every(node => [...node.attributes].every(attr => !/^on/i.test(attr.name)))"
                )
                for field, value in {"count": "2", "charge": "-1", "ph": "7.2"}.items():
                    numeric = card.locator(f'[data-ligand-field="{field}"]')
                    expect(numeric).to_be_editable()
                    numeric.fill(value)
                    expect(numeric).to_have_value(value)
                payload = page.evaluate(
                    """async formUrl => {
                    const {readLigandPayload} = await import(formUrl);
                    return readLigandPayload().find(ligand => ligand.key === 'draft-escape-test');
                }""",
                    form_url,
                )
                assert {field: payload[field] for field in ("count", "charge", "ph")} == {"count": 2, "charge": -1, "ph": 7.2}
                assert page.evaluate("window.ligandInjectionExecuted === false")
                expect(page.locator("[data-draft-injected], [data-draft-node]")).to_have_count(0)
                assert not errors, errors
                page.close()
            # Hold diagnostics/defaults/previews open while the real app boots.
            # History, event monitoring and controls must work before any return.
            delayed = []
            delaying = True
            artifact_revision = 0

            def delayed_fixture(route) -> None:
                path = urlsplit(route.request.url).path
                if delaying and path in {"/api/product", "/api/health", "/api/preview"}:
                    delayed.append(route)
                elif path == "/api/jobs/job-00/files":
                    files = [{"path": "md.xtc", "size": 1024 * (artifact_revision + 1)}]
                    if artifact_revision:
                        files.append({"path": "md.cpt", "size": 512})
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"files": files}))
                else:
                    fixture(route)

            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(f"{ORIGIN}/**", delayed_fixture)
            page.add_init_script("""
                localStorage.setItem('gromacs.simulation-draft.v1', JSON.stringify({
                    version: 1, params: {water_model: 'opc'}
                }));
                window.testStreams = [];
                window.EventSource = class extends EventTarget {
                    constructor(url) { super(); this.url = url; this.readyState = 1; window.testStreams.push(this); }
                    close() { this.readyState = 2; }
                };
            """)
            page.goto(ORIGIN, wait_until="domcontentloaded")
            expect(page.locator(".job-item")).to_have_count(50)
            page.wait_for_function("window.testStreams.some(stream => stream.readyState === 1)")
            page.locator("#expert-mode").click()
            expect(page.locator("#expert-mode")).to_have_attribute("aria-pressed", "true")
            initial_preview = next(route.request.post_data_json for route in delayed if urlsplit(route.request.url).path == "/api/preview")
            assert "ntmpi" not in initial_preview and "ntomp" not in initial_preview
            page.locator(".new-job-organization summary").click()
            page.locator('#job-form [name="project"]').fill("启动中的项目")
            page.locator('#job-form [name="tags"]').fill("WT, pending")
            page.locator('#job-form [name="notes"]').fill("诊断尚未返回时的备注")
            pending_form = page.evaluate("async url => (await import(url)).formObject()", form_url)
            assert pending_form["project"] == "启动中的项目"
            assert pending_form["tags"] == ["WT", "pending"]
            assert pending_form["notes"] == "诊断尚未返回时的备注"
            assert "ntmpi" not in pending_form and "ntomp" not in pending_form
            page.wait_for_function("JSON.parse(localStorage.getItem('gromacs.simulation-draft.v1')).params.notes === '诊断尚未返回时的备注'")
            pending_draft = page.evaluate("JSON.parse(localStorage.getItem('gromacs.simulation-draft.v1')).params")
            assert pending_draft["project"] == "启动中的项目" and pending_draft["tags"] == ["WT", "pending"]
            assert "ntmpi" not in pending_draft and "ntomp" not in pending_draft
            page.locator('[name="force_field"]').fill("user-custom-force-field")
            page.locator(".settings-panel summary").click()
            page.locator("#gmx-bin").fill("user-gmx")
            page.locator('[data-job-id="job-00"] [data-action="select"]').first.click()
            expect(page.locator("#active-job-title")).to_have_text("合成测试任务 00")
            assert {urlsplit(route.request.url).path for route in delayed} >= {"/api/product", "/api/health", "/api/preview"}
            delaying = False
            for route in delayed:
                if urlsplit(route.request.url).path == "/api/product":
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "templates": [], "projects": [],
                        "defaults": {"force_field": "backend-force-field", "water_model": "tip3p", "box_distance": 1.7},
                    }))
                elif urlsplit(route.request.url).path == "/api/health":
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "settings": {"gmx_bin": "backend-gmx", "default_ntmpi": 2, "default_ntomp": 12},
                        "gromacs": {"available": False}, "diagnostics": {},
                    }))
                else:
                    fixture(route)
            page.wait_for_load_state("networkidle")
            expect(page.locator("#detail-view")).to_be_visible()
            expect(page.locator("#manual-compose")).to_be_hidden()
            expect(page.locator("#agent-panel")).to_be_hidden()
            expect(page.locator('[name="force_field"]')).to_have_value("user-custom-force-field")
            expect(page.locator('[name="water_model"]')).to_have_value("opc")
            expect(page.locator('[name="box_distance"]')).to_have_value("1.7")
            expect(page.locator("#gmx-bin")).to_have_value("user-gmx")
            ready_form = page.evaluate("async url => (await import(url)).formObject()", form_url)
            assert ready_form["ntmpi"] == 2 and ready_form["ntomp"] == 12
            assert ready_form["project"] == "启动中的项目" and ready_form["notes"] == "诊断尚未返回时的备注"
            expect(page.locator("#download-result-bundle")).to_have_attribute("href", "/api/jobs/job-00/result-bundle")
            expect(page.locator("#result-summary-content")).not_to_contain_text("正在读取结果摘要服务")
            # A same-step running task must discover new files and changed sizes.
            page.evaluate("""job => window.testStreams.at(-1).dispatchEvent(
                new MessageEvent('job', {data: JSON.stringify(job)}))""", {**jobs[0], "status": "running"})
            page.locator('[data-detail-tab="files"]').click()
            expect(page.locator("#output-files")).to_contain_text("1.0 KB")
            artifact_revision = 1
            expect(page.locator("#output-files")).to_contain_text("md.cpt", timeout=12000)
            expect(page.locator("#output-files")).to_contain_text("2.0 KB")
            page.locator('[data-detail-tab="overview"]').click()
            page.evaluate("""() => window.testStreams.at(-1).dispatchEvent(
                new MessageEvent('log', {data: JSON.stringify({start: 2048, offset: 2052, text: 'tail'})}))""")
            expect(page.locator("#log-notice")).to_contain_text("仅显示末尾日志")
            expect(page.locator("#log-notice")).to_contain_text("打开时省略 2.0 KB")
            expect(page.locator("#log-notice")).to_contain_text("已保留日志")
            expect(page.locator("#log-notice")).to_contain_text("服务端轮转可能省略更早内容")
            expect(page.locator("#download-log")).to_have_attribute("href", "/api/jobs/job-00/download?path=run.log")
            # Old closed connections must not append another task's log.
            page.evaluate("""() => window.testStreams[0].dispatchEvent(
                new MessageEvent('log', {data: JSON.stringify({start: 2052, offset: 2057, text: 'WRONG'})}))""")
            expect(page.locator("#job-log")).to_have_text("tail")
            assert not errors, errors
            page.close()
            # A transient product failure must not be mistaken for an old API.
            # Draft persistence also retains metadata when compatibility requires
            # omitting those fields from requests to a confirmed legacy service.
            for product_status in (503, 404):
                def unavailable_fixture(route) -> None:
                    if urlsplit(route.request.url).path == "/api/product":
                        route.fulfill(status=product_status, content_type="application/json", body=json.dumps({"detail": "Synthetic product failure"}))
                    else:
                        fixture(route)

                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route(f"{ORIGIN}/**", unavailable_fixture)
                page.add_init_script("""
                    localStorage.setItem('gromacs.simulation-draft.v1', JSON.stringify({version: 1,
                        params: {project: '保留项目', tags: ['draft'], notes: '保留备注', ntomp: 7}
                    }));
                    window.EventSource = class extends EventTarget { close() {} };
                """)
                page.goto(ORIGIN, wait_until="networkidle")
                expect(page.locator(".job-item")).to_have_count(50)
                capability = page.evaluate("async url => (await import(url)).state.productStatus", core_url)
                assert capability == ("unsupported" if product_status == 404 else "error")
                form_payload = page.evaluate("async url => (await import(url)).formObject()", form_url)
                assert form_payload["ntomp"] == 7
                if product_status == 404:
                    assert not {"project", "tags", "notes"}.intersection(form_payload)
                    expect(page.locator(".new-job-organization")).to_be_hidden()
                else:
                    assert form_payload["project"] == "保留项目"
                    assert form_payload["tags"] == ["draft"] and form_payload["notes"] == "保留备注"
                    expect(page.locator(".new-job-organization")).to_be_visible()
                page.locator("#job-name").fill("触发保存")
                page.locator("#job-name").press("Tab")
                page.wait_for_function("JSON.parse(localStorage.getItem('gromacs.simulation-draft.v1')).params.name === '触发保存'")
                saved = page.evaluate("JSON.parse(localStorage.getItem('gromacs.simulation-draft.v1')).params")
                assert saved["project"] == "保留项目" and saved["tags"] == ["draft"] and saved["notes"] == "保留备注"
                assert saved["ntomp"] == 7
                assert not errors, errors
                page.close()
        finally:
            browser.close()
    assert any("search=job-57" in url for _, url in requests)
    print("Three workspace views preserve drafts/files; mobile sidebar focus/backdrop, history/report navigation, SSE and dialogs passed at 3 viewports; delayed boot, same-step artifacts and log notices passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    check_browser(parser.parse_args().artifact_dir)
