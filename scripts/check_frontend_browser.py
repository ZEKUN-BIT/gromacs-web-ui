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
                if width <= 1180:
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
        finally:
            browser.close()
    assert any("search=job-57" in url for _, url in requests)
    print("History/report navigation, SSE, confirmation, workflow fields and numeric draft HTML escaping passed at 3 viewports.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path)
    check_browser(parser.parse_args().artifact_dir)
