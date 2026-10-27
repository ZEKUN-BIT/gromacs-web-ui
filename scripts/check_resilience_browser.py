"""Check recovery, shared drafts, paged outputs and history in isolated browsers."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

from check_product_browser import ORIGIN, check_viewport
from playwright.sync_api import expect, sync_playwright


def run():
    control = {"product_failed": True, "file_reads": 0}

    def configure(page):
        def product(route):
            if control["product_failed"]:
                route.fulfill(status=503, content_type="application/json", body='{"detail":"temporary outage"}')
            else:
                route.fallback()

        def files(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if "limit" not in query:
                route.fallback()
                return
            control["file_reads"] += 1
            limit, offset = int(query["limit"][0]), int(query.get("offset", [0])[0])
            search = query.get("search", [""])[0].casefold()
            items = [{"path": f"result-{number:04}.xtc", "size": control["file_reads"]} for number in range(2505)]
            items = [item for item in items if search in item["path"].casefold()]
            route.fulfill(
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": items[offset : offset + limit],
                        "total": len(items),
                        "offset": offset,
                        "limit": limit,
                        "has_more": offset + limit < len(items),
                        "truncated": False,
                    }
                ),
            )

        page.route("**/api/product", product)
        page.route("**/api/jobs/product-1/files*", files)

    def inspect(page):
        expect(page.locator("#retry-product-initialization")).to_be_visible()
        page.locator("#job-name").fill("保留已选文件")
        page.locator("#files").set_input_files({"name": "keep.pdb", "mimeType": "text/plain", "buffer": b"END\n"})
        control["product_failed"] = False
        page.locator("#retry-product-initialization").click()
        expect(page.locator("#product-availability")).to_have_count(0)
        expect(page.locator("#open-library")).to_be_enabled()
        expect(page.locator("#job-name")).to_have_value("保留已选文件")
        assert page.locator("#files").evaluate("el => el.files[0].name") == "keep.pdb"

        expect(page.locator("#draft-status")).to_have_text("参数草稿已保存")
        other = page.context.new_page()
        other_errors = []
        other.on("pageerror", lambda error: other_errors.append(str(error)))
        other.add_init_script("""window.testStreams = [];
            window.EventSource = class extends EventTarget {
                constructor(url) { super(); this.url = url; this.readyState = 1; window.testStreams.push(this); }
                close() { this.readyState = 2; }
            };""")
        try:
            other.goto(ORIGIN, wait_until="networkidle")
            expect(other.locator("#job-name")).to_have_value("保留已选文件")
            other.locator("#job-name").fill("另一标签页的体系")
            expect(other.locator("#draft-status")).to_have_text("参数草稿已保存")
            other.wait_for_function("JSON.parse(localStorage.getItem('gromacs.simulation-draft.v1')).params.name === '另一标签页的体系'")
            expect(page.locator("#draft-conflict")).to_be_visible()
            page.locator("#job-name").fill("本页保留的体系")
            expect(page.locator("#job-name")).to_have_value("本页保留的体系")
            assert page.evaluate("JSON.parse(localStorage.getItem('gromacs.simulation-draft.v1')).params.name") == "另一标签页的体系"
            page.get_by_role("button", name="保存当前参数", exact=True).click()
            expect(other.locator("#draft-conflict")).to_be_visible()
            other.get_by_role("button", name="载入另一标签页草稿", exact=True).click()
            expect(other.locator("#job-name")).to_have_value("本页保留的体系")
            assert not other_errors, other_errors
        finally:
            other.close()

        page.locator('[data-workspace-view="detail"]').click()
        page.locator('[data-detail-tab="files"]').click()
        expect(page.locator("#file-count")).to_have_text("2505 个")
        expect(page.locator("#output-files .file-row")).to_have_count(100)
        page.locator("#files-next").click()
        expect(page.locator("#files-page-status")).to_have_text("101–200 / 2505")
        page.locator("#file-filter").fill("RESULT-2504")
        expect(page.locator("#output-files .file-row")).to_have_count(1)
        expect(page.locator("#output-files")).to_contain_text("result-2504.xtc")
        page.locator("#file-filter").fill("")
        expect(page.locator("#output-files .file-row")).to_have_count(100)
        group = page.locator('.file-group[data-group="trajectory"]')
        group.evaluate("el => el.open = true")
        version = page.evaluate("document.querySelector('script[type=module]').src.split('?')[1]")
        page.evaluate(
            """async version => {
            const {state} = await import('/static/js/core.js?' + version);
            const {renderActiveJob} = await import('/static/js/jobs.js?' + version);
            state.artifactKey = ''; await renderActiveJob();
        }""",
            version,
        )
        expect(group).to_have_attribute("open", "")
        expect(page).to_have_url(re.compile(r"[?&]tab=files"))
        page.locator('[data-detail-tab="plots"]').click()
        expect(page).to_have_url(re.compile(r"[?&]tab=plots"))
        page.go_back(wait_until="domcontentloaded")
        expect(page.locator('[data-detail-panel="files"]')).to_be_visible()
        page.reload(wait_until="networkidle")
        expect(page.locator('[data-detail-panel="files"]')).to_be_visible()
        expect(page.locator("#active-job-title")).to_have_text("产品流程测试 1")
        print("PASS product recovery, cross-tab conflict decisions, 2505-file search/paging, group state and URL back/reload")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            check_viewport(browser, 1440, 1000, "light", None, configure=configure, inspect=inspect)
        finally:
            browser.close()


if __name__ == "__main__":
    run()
