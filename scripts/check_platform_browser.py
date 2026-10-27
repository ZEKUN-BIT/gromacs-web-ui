"""Native-font Chromium/Firefox checks, also runnable on Windows CI.

Half-width layout and reduced viewport height are stress simulations; this does
not claim physical mobile keyboard, operating-system zoom or hardware coverage.
"""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from check_product_browser import check_viewport
from playwright.sync_api import expect, sync_playwright


def check(artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    reports = []
    with sync_playwright() as playwright:
        for engine in ("chromium", "firefox"):
            browser = getattr(playwright, engine).launch(headless=True)
            try:
                for width, height, forced in ((1440, 1000, False), (768, 1000, True), (390, 844, False)):

                    def inspect(page):
                        def fits():
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Overflow: {engine}/{width}"

                        expect(page.locator("#job-name")).to_be_visible()
                        page.evaluate("document.fonts.ready")
                        fonts = page.locator("body").evaluate("el => getComputedStyle(el).fontFamily")
                        assert "GromacsSnapshotCJK" not in fonts, "Compatibility tests must use native fonts"
                        fits()
                        page.screenshot(path=str(artifact_dir / f"{engine}-{width}-native.png"))
                        page.locator("#job-name").focus()
                        page.locator("#job-name").fill("中文输入可读性 · Molecular dynamics 123")
                        expect(page.locator("#job-name")).to_be_focused()
                        if width == 390:
                            page.set_viewport_size({"width": width, "height": 440})
                            page.locator("#job-name").scroll_into_view_if_needed()
                            expect(page.locator("#job-name")).to_be_visible()
                            expect(page.locator("#job-name")).to_be_focused()
                            fits()
                            page.screenshot(path=str(artifact_dir / f"{engine}-keyboard-height.png"))
                            page.set_viewport_size({"width": width, "height": height})
                        if width == 1440:
                            # Desktop 200% browser zoom halves the CSS viewport.
                            # Exercise that layout without claiming native zoom.
                            page.set_viewport_size({"width": width // 2, "height": height // 2})
                            page.locator("#job-name").scroll_into_view_if_needed()
                            fits()
                            expect(page.locator("#job-name")).to_be_visible()
                            page.screenshot(path=str(artifact_dir / f"{engine}-half-viewport.png"))
                            page.set_viewport_size({"width": width, "height": height})
                        page.locator('[data-workspace-view="detail"]').click()
                        page.locator('[data-detail-tab="files"]').click()
                        expect(page.locator('[data-detail-panel="files"]')).to_be_visible()
                        fits()
                        page.screenshot(path=str(artifact_dir / f"{engine}-{width}-detail.png"))
                        reports.append(
                            {
                                "engine": engine,
                                "version": browser.version,
                                "system": platform.system(),
                                "viewport": [width, height],
                                "forced_colors": forced,
                                "reduced_motion": True,
                                "body_font_stack": fonts,
                                "status": "passed",
                            }
                        )

                    check_viewport(
                        browser,
                        width,
                        height,
                        "light",
                        None,
                        inspect=inspect,
                        context_options={
                            "reduced_motion": "reduce",
                            "forced_colors": "active" if forced else "none",
                            "has_touch": width == 390,
                        },
                    )
                    print(f"PASS {platform.system()} {engine} native fonts, {width}px, reduced motion, forced-colors={forced}")
            finally:
                browser.close()
    (artifact_dir / "platform-browser.json").write_text(json.dumps(reports, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=Path("runtime/frontend-quality/platform"))
    check(parser.parse_args().artifact_dir)
