"""Check recovery, shared drafts, paged outputs and history in isolated browsers."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

from check_product_browser import ORIGIN, check_viewport
from playwright.sync_api import expect, sync_playwright


def run():
    control = {"product_failed": True, "file_reads": 0}
    held = []

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
            if search == "slow-query":
                held.append(route)
                return
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
        page.route("**/api/jobs/product-2/files*", lambda route: route.fulfill(json={
            "files": [{"path": "md.log", "size": 10}, {"path": "run/md.xtc", "size": 20}], "truncated": True,
        }))

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
        page.locator('.file-group[data-group="trajectory"] > summary').click()
        page.locator("#output-files").evaluate("el => el.scrollTop = el.scrollHeight")
        assert page.locator("#output-files").evaluate("el => el.scrollTop > 0")
        expect(page.locator("#file-filter")).to_be_in_viewport()
        expect(page.locator("#files-next")).to_be_in_viewport()
        page.locator("#files-next").click()
        expect(page.locator("#files-page-status")).to_have_text("101–200 / 2505")
        assert page.locator("#output-files").evaluate("el => el.scrollTop === 0")
        search = page.get_by_role("searchbox", name="搜索输出文件")
        search.fill("RESULT-2504")
        expect(page.locator("#output-files .file-row")).to_have_count(1)
        expect(page.get_by_role("link", name="result-2504.xtc")).to_be_visible()
        expect(page.locator("#file-count")).to_have_text("1 个匹配")
        expect(page.locator("#file-pagination")).to_be_hidden()
        search.fill("missing.xtc")
        clear_empty = page.get_by_role("button", name="清除筛选", exact=True)
        expect(page.locator("#output-files")).to_contain_text("没有匹配的输出文件")
        expect(page.locator("#file-count")).to_have_text("0 个匹配")
        clear_empty.click()
        expect(search).to_be_focused()
        expect(page.locator("#files-page-status")).to_have_text("1–100 / 2505")

        # Both edits happen in one event turn, before the debounce timer fires.
        page.locator("#files-next").click()
        expect(page.locator("#files-page-status")).to_have_text("101–200 / 2505")
        search.evaluate("""el => { el.value = 'discard'; el.dispatchEvent(new Event('input')); document.querySelector('#clear-file-filter').click(); }""")
        expect(page.locator("#files-page-status")).to_have_text("1–100 / 2505")
        with page.expect_request(lambda request: "search=slow-query" in request.url):
            search.fill("slow-query")
        page.get_by_role("button", name="清除文件搜索").click()
        expect(page.locator("#file-count")).to_have_text("2505 个")
        held.pop().fulfill(json={"files": [], "total": 0, "offset": 0, "limit": 100, "has_more": False})
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
        search.fill("2504")
        expect(page.locator("#file-count")).to_have_text("1 个匹配")
        page.locator('[data-job-id="product-2"] .job-select').click()
        expect(search).to_have_value("")
        expect(page.locator("#file-count")).to_have_text("2+ 个")
        expect(page.locator("#file-pagination")).to_be_hidden()
        search.fill("run/")
        expect(page.get_by_role("link", name="run/md.xtc")).to_be_visible()
        search.fill("missing")
        expect(page.locator("#output-files")).to_contain_text("仅搜索已载入的文件")
        clear_empty.click()
        expect(search).to_be_focused()
        expect(page.locator("#file-count")).to_have_text("2+ 个")
        page.locator('[data-job-id="product-1"] .job-select').click()
        expect(search).to_have_value("2504")
        expect(page.get_by_role("link", name="result-2504.xtc")).to_be_visible()
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
