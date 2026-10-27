"""Check WCAG rules, pinned visual snapshots and frontend resource/startup budgets.

Uses synthetic, intercepted API fixtures only. --update-snapshots explicitly
accepts a reviewed visual change; CI never regenerates missing baselines.
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from check_product_browser import check_viewport
from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tests/browser_assets"
BASELINES = ROOT / "tests/browser_baselines"
BUDGETS = ROOT / "tests/browser_budgets.json"

FONT_CSS = (
    "\n".join(
        f'@font-face {{ font-family: "Gromacs Snapshot CJK"; font-weight: {weight}; src: url("/__qa__/cjk-{weight}.otf"); }}'
        for weight in (400, 500, 600)
    )
    + '\n:root { --sans: "Inter", "Gromacs Snapshot CJK", sans-serif; --display: var(--sans); }'
)


def compare_image(actual: Path, expected: Path, diff_path: Path, threshold: float) -> float:
    assert expected.is_file(), f"Missing visual baseline: {expected}; review and use --update-snapshots explicitly"
    first, second = Image.open(actual).convert("RGB"), Image.open(expected).convert("RGB")
    assert first.size == second.size, f"Snapshot dimensions changed: {actual.name}"
    delta = np.abs(np.asarray(first, dtype=np.int16) - np.asarray(second, dtype=np.int16))
    changed = np.max(delta, axis=2) > 24  # Ignore small antialiasing differences.
    ratio = float(changed.mean())
    if ratio > threshold:
        pixels = np.asarray(first).copy()
        pixels[changed] = [255, 0, 110]
        Image.fromarray(pixels).save(diff_path)
        raise AssertionError(f"{actual.name}: {ratio:.2%} pixels differ (limit {threshold:.2%}); inspect {diff_path}")
    return ratio


def run(artifact_dir: Path, update: bool, visual: bool = True) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    budgets = json.loads(BUDGETS.read_text())
    resources = {
        kind: sum(p.stat().st_size for p in (ROOT / "app/static").rglob(pattern))
        for kind, pattern in (("javascript_bytes", "js/*.js"), ("css_bytes", "*.css"), ("font_bytes", "fonts/*.woff2"))
    }
    for name, size in resources.items():
        assert size <= budgets[name], f"{name}: {size} exceeds {budgets[name]}"
    report = {
        "scope": "Isolated Chromium fixtures; startup timings are lab measurements, not field Core Web Vitals.",
        "resources": resources,
        "views": [],
        "startup_ms": [],
    }
    violations = []
    pending_baselines = []

    def configure(page):
        page.clock.set_fixed_time(datetime(2026, 10, 26, tzinfo=timezone.utc))
        page.add_init_script("""window.qaLongTasks=[];
          window.qaLongTaskObserver = new PerformanceObserver(list => window.qaLongTasks.push(...list.getEntries().map(e => e.duration)));
          window.qaLongTaskObserver.observe({type:'longtask', buffered:true});""")

    def inspect(page, width, scheme):
        expect(page.locator(".job-item")).to_have_count(2)
        expect(page.locator("#job-name")).to_be_editable()
        startup = page.evaluate("performance.now()")
        report["startup_ms"].append(startup)
        report.setdefault("long_tasks_ms", []).extend(
            page.evaluate("""() => {
          const pending = window.qaLongTaskObserver.takeRecords().map(e => e.duration);
          window.qaLongTaskObserver.disconnect();
          return [...window.qaLongTasks, ...pending];
        }""")
        )
        assert startup < budgets["fixture_ready_ms"], f"Fixture startup exceeded budget: {startup:.0f}ms"
        # Pin CJK glyphs only in the visual harness, independent of host fonts.
        page.route(
            "**/__qa__/*",
            lambda route: route.fulfill(content_type="font/otf", body=(ASSETS / route.request.url.rsplit("/", 1)[1]).read_bytes()),
        )
        page.add_style_tag(content=FONT_CSS)
        page.add_script_tag(path=str(ASSETS / "axe.min.js"))
        page.evaluate("document.fonts.ready")
        page.locator("#start-guide").evaluate("el => el.open = false")
        # The task stream in this fixture is synthetic, so publish a heartbeat.
        page.evaluate("window.testStreams.at(-1).dispatchEvent(new MessageEvent('ping',{data:'{}'}))")

        for view in ("compose", "agent", "detail"):
            if view == "agent":
                page.locator("#open-agent").click()
                expect(page.locator("#agent-connection-status")).to_have_text("尚未配置 API")
                expect(page.locator("#agent-message")).to_be_editable()
                page.locator("#agent-message").focus()
            else:
                page.locator(f'[data-workspace-view="{view}"]').click()
            if view == "detail":
                expect(page.locator("#job-status")).to_have_text("已完成")
            page.evaluate("document.fonts.ready")
            page.evaluate("window.scrollTo(0,0)")
            # Finish native transitions before contrast/screenshot sampling.
            page.evaluate(
                "async () => { await Promise.all(document.getAnimations().filter(a => a.effect.getTiming().iterations !== Infinity).map(a => a.finished.catch(()=>{}))); }"
            )
            results = page.evaluate("""async () => {
              const result = await axe.run(document, {runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa','wcag22aa']}});
              return result.violations.map(v => ({id:v.id,impact:v.impact,help:v.help,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}));
            }""")
            name = f"{view}-{width}-{scheme}"
            violations.extend({"view": name, **item} for item in results)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Horizontal clipping in {name}"
            item = {"name": name, "wcag_violations": len(results)}
            if visual:
                path = artifact_dir / f"{name}.png"
                # Native smooth scrolling is not a Web Animation. Cancel it at
                # sampling time so slower CI hosts capture the same location.
                page.evaluate("window.scrollTo({top:0, left:0, behavior:'instant'})")
                # Last-update timestamps are tested separately; exclude locale formatting.
                page.screenshot(path=str(path), animations="disabled", caret="hide", mask=[page.locator("#task-sync-time")])
                baseline = BASELINES / path.name
                if update:
                    pending_baselines.append((path, baseline))
                else:
                    item["changed_pixel_ratio"] = compare_image(
                        path, baseline, artifact_dir / f"{name}-diff.png", budgets["visual_changed_ratio"]
                    )
            report["views"].append(item)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for width, height, scheme in ((1440, 1000, "light"), (390, 844, "light"), (1440, 1000, "dark"), (390, 844, "dark")):
                    check_viewport(
                        browser,
                        width,
                        height,
                        scheme,
                        None,
                        configure=configure,
                        context_options={"locale": "zh-CN", "timezone_id": "UTC", "device_scale_factor": 1},
                        inspect=lambda page, w=width, s=scheme: inspect(page, w, s),
                    )
            finally:
                browser.close()
        report["startup_median_ms"] = statistics.median(report["startup_ms"])
        report["max_long_task_ms"] = max(report.get("long_tasks_ms", [0]), default=0)
        assert report["max_long_task_ms"] <= budgets["max_long_task_ms"], "Long task budget exceeded"
        assert not violations, f"{len(violations)} WCAG violations; inspect {artifact_dir / 'accessibility.json'}"
        # A failed run must never replace previously accepted screenshots.
        if update:
            BASELINES.mkdir(exist_ok=True)
            for actual, baseline in pending_baselines:
                baseline.write_bytes(actual.read_bytes())
    finally:
        (artifact_dir / "accessibility.json").write_text(json.dumps(violations, ensure_ascii=False, indent=2))
        (artifact_dir / "performance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"PASS {len(report['views'])} views: WCAG rules, resource/startup budgets" + (", visual snapshots" if visual else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=Path("runtime/frontend-quality"))
    parser.add_argument("--update-snapshots", action="store_true", help="Explicitly replace reviewed visual baselines")
    parser.add_argument("--skip-visual", action="store_true", help="Run accessibility/performance while preparing new snapshots")
    args = parser.parse_args()
    run(args.artifact_dir, args.update_snapshots, not args.skip_visual)
