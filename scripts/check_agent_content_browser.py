"""Check AI message formatting, clipboard behavior and DOM safety without a server."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

MODULE = Path(__file__).resolve().parents[1] / "app/static/js/agent-content.js"
ORIGIN = "http://gromacs.test"
PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>AI message rendering check</title><main id="messages"></main></html>"""


def check() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors, unexpected_requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def fixture(route):
                parsed = urlsplit(route.request.url)
                if parsed.netloc == "gromacs.test" and parsed.path == "/":
                    route.fulfill(content_type="text/html", body=PAGE)
                elif parsed.netloc == "gromacs.test" and parsed.path == "/agent-content.js":
                    route.fulfill(content_type="text/javascript", body=MODULE.read_text())
                else:
                    unexpected_requests.append(route.request.url)
                    route.abort()

            page.route("**/*", fixture)
            page.goto(ORIGIN, wait_until="networkidle")
            page.evaluate("""async () => {
                window.renderMessage = (await import('/agent-content.js')).renderMessage;
                window.clipboardWrites = [];
                Object.defineProperty(navigator, 'clipboard', { configurable: true,
                    value: { writeText: async value => window.clipboardWrites.push(value) } });
            }""")

            def render(content, role="assistant", created_at=None):
                page.evaluate(
                    "message => document.querySelector('#messages').replaceChildren(window.renderMessage(message))",
                    {"content": content, "role": role, "created_at": created_at},
                )

            code = 'gmx mdrun -deffnm md\nprint("<img src=x onerror=alert(1)>")'
            content = (
                "# 模拟方案\n\n请检查 **温度** 和 `dt`。查看 [官方文档](https://manual.gromacs.org/)\n\n"
                "- 准备结构\n- 设定温度\n\n3. 平衡\n4. 生产模拟\n\n> 先确认参数。\n> 不会自动执行。\n\n"
                f"```bash\n{code}\n```\n\n"
                "| 参数 | 数值 |\n| --- | ---: |\n| 温度 | **300 K** |\n| 步长 | `0.002` |"
            )
            render(content, created_at="2026-10-22T12:34:56.123456+00:00")
            expect(page.locator("article[data-role=assistant] .agent-message-head strong")).to_have_text("助手")
            expect(page.locator(".agent-message-head time")).to_have_attribute("datetime", "2026-10-22T12:34:56.123Z")
            expect(page.locator("h3")).to_have_text("模拟方案")
            expect(page.locator("ul li")).to_have_text(["准备结构", "设定温度"])
            expect(page.locator("ol")).to_have_attribute("start", "3")
            expect(page.locator("ol li")).to_have_text(["平衡", "生产模拟"])
            expect(page.locator("blockquote")).to_contain_text("不会自动执行")
            expect(page.locator(".agent-code-language")).to_have_text("bash")
            expect(page.locator("pre code")).to_have_text(code)
            expect(page.locator("thead th")).to_have_text(["参数", "数值"])
            expect(page.locator("tbody tr")).to_have_count(2)
            expect(page.locator("tbody strong")).to_have_text("300 K")
            expect(page.locator("a")).to_have_attribute("target", "_blank")
            expect(page.locator("a")).to_have_attribute("rel", "noopener noreferrer")
            expect(page.locator("img, script, iframe, svg, style")).to_have_count(0)

            page.get_by_role("button", name="复制消息", exact=True).click()
            expect(page.locator(".agent-message-head [role=status]")).to_have_text("已复制")
            page.get_by_role("button", name="复制代码", exact=True).click()
            assert page.evaluate("window.clipboardWrites") == [content, code]
            page.evaluate("() => { navigator.clipboard.writeText = async () => { throw new Error('permission denied'); }; }")
            page.get_by_role("button", name="复制代码", exact=True).click()
            expect(page.locator(".agent-code-head [role=status]")).to_have_text("复制失败，请选中文字手动复制。")
            expect(page.get_by_role("button", name="复制代码", exact=True)).to_be_enabled()
            page.evaluate("Object.defineProperty(navigator, 'clipboard', { value: undefined })")
            page.get_by_role("button", name="复制消息", exact=True).click()
            expect(page.locator(".agent-message-head [role=status]")).to_contain_text("手动复制")

            attack = (
                '<img src="https://outside.test/pixel" onerror="window.agentXss=true">\n'
                '<script>window.agentXss=true</script>\n<svg onload="window.agentXss=true"></svg>\n'
                '<iframe srcdoc="<script>parent.agentXss=true</script>"></iframe>\n'
                "[javascript](javascript:window.agentXss=true)\n[data](data:text/html,hello)\n"
                "[relative](/secret)\n[protocol-relative](//outside.test/)\n"
                "![remote image](https://outside.test/picture.png)\n"
                "[entity](jav&#x61;script:alert(1))\n[spaced](java\tscript:alert(1))\n"
                "[HTTPS](https://example.com/) [HTTP](http://example.com/)"
            )
            render(attack, created_at="not-a-date")
            expect(page.locator("img, script, iframe, svg, style, time")).to_have_count(0)
            expect(page.locator("a")).to_have_count(2)
            assert all(
                href.startswith(("https://", "http://")) for href in page.locator("a").evaluate_all("links => links.map(a => a.href)")
            )
            assert not page.evaluate("window.agentXss")
            expect(page.locator(".agent-message-body")).to_contain_text('<img src="https://outside.test/pixel"')
            expect(page.locator(".agent-message-body")).to_contain_text("![remote image]")

            for role in ("user", "system"):
                render(content + "\n" + attack, role=role)
                expect(page.locator(".agent-message-plain")).to_have_text(content + "\n" + attack)
                expect(page.locator("a, pre, h3, table, img, script")).to_have_count(0)
            for timestamp in (None, "2026", "2026-02-30T12:00:00Z", "2026-10-22T24:00:00Z"):
                render("文本", created_at=timestamp)
                expect(page.locator("time")).to_have_count(0)

            # Long and unfinished model output stays intact; it never becomes markup.
            long_code, long_value = "x" * 20000, "long-value-" * 800
            render(f"| 参数 | 数值 |\n| --- | --- |\n| 长值 | {long_value} |\n\n```python\n{long_code}")
            expect(page.locator("tbody td").last).to_have_text(long_value)
            expect(page.locator("pre code")).to_have_text(long_code)
            render("[" * 30000 + "\n" + "`" * 30000)
            expect(page.locator(".agent-message-body p")).to_have_text("[" * 30000)
            assert not unexpected_requests, json.dumps(unexpected_requests)
            assert not errors, json.dumps(errors)
            print("AI message DOM, Markdown, XSS and clipboard checks passed.")
        finally:
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    check()
