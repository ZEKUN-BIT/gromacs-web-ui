"""Check bounded canvas rendering with synthetic data and intercepted local modules.

Use --baseline-module with an older plots.js to collect comparable counts, without
requiring the older implementation to satisfy the new rendering assertions.
Timing covers canvas setup/painting, not data preparation or compositor work.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "app/static/js/plots.js"
CORE = ROOT / "app/static/js/core.js"
ORIGIN = "http://gromacs.test"
PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Synthetic plot rendering check</title><style>
:root { --plot-text: #333333; --plot-grid: #aaaaaa; }
@media (prefers-color-scheme: dark) { :root { --plot-text: #dddddd; --plot-grid: #555555; } }
body { margin: 0; } [hidden] { display: none !important; }
#plot-panel { width: 900px; max-width: 95vw; }
.plot-card { height: 430px; overflow: hidden; }
.plot-card-head { height: 40px; } canvas { display: block; width: 100%; height: 250px; }
output, small { display: block; } h3, p { margin: 0; }
</style><form id="plot-controls"></form><span id="plot-count"></span>
<div id="plot-filter-error" hidden></div><section id="plot-panel"><div id="analysis-plots"></div></section>
</html>"""
INSTRUMENT = """() => {
    window.drawMetrics = [];
    let frame = 0;
    const tick = time => { frame = time; requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    const active = new WeakMap();
    let nextId = 0;
    const ids = new WeakMap();
    const width = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, 'width');
    Object.defineProperty(HTMLCanvasElement.prototype, 'width', {
        ...width, set(value) {
            if (!ids.has(this)) ids.set(this, ++nextId);
            const box = this.getBoundingClientRect();
            const record = { id: ids.get(this), index: Number(this.dataset.plotIndex), frame,
                connected: this.isConnected, start: performance.now(), canvas_ms: 0,
                onscreen: box.width > 0 && box.height > 0 && box.bottom > 0 && box.right > 0 &&
                    box.top < innerHeight && box.left < innerWidth };
            active.set(this, record);
            window.drawMetrics.push(record);
            width.set.call(this, value);
        }
    });
    const dash = CanvasRenderingContext2D.prototype.setLineDash;
    CanvasRenderingContext2D.prototype.setLineDash = function(segments) {
        const result = dash.call(this, segments);
        const record = active.get(this.canvas);
        if (record) record.canvas_ms = performance.now() - record.start;
        return result;
    };
    window.settlePlots = async () => {
        for (let i = 0; i < 8; i++) await new Promise(requestAnimationFrame);
    };
    window.visibleCanvases = () => [...document.querySelectorAll('canvas')].filter(canvas => {
        const box = canvas.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && box.bottom > 0 && box.right > 0 &&
            box.top < innerHeight && box.left < innerWidth;
    });
} """


def run(browser, module: Path, *, strict: bool, no_intersection: bool) -> dict:
    context = browser.new_context(viewport={"width": 1024, "height": 768}, color_scheme="light")
    if no_intersection:
        context.add_init_script("delete window.IntersectionObserver")
    page = context.new_page()
    errors, unexpected_requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    module_source = re.sub(r'"\./core\.js\?v=[^\"]+"', '"/core.js"', module.read_text())

    def route_request(route):
        url = urlsplit(route.request.url)
        if url.netloc != "gromacs.test":
            unexpected_requests.append(route.request.url)
            route.abort()
        elif url.path == "/":
            route.fulfill(content_type="text/html", body=PAGE)
        elif url.path == "/plots.js":
            route.fulfill(content_type="text/javascript", body=module_source)
        elif url.path == "/core.js":
            route.fulfill(content_type="text/javascript", body=CORE.read_text())
        else:
            unexpected_requests.append(route.request.url)
            route.abort()

    page.route("**/*", route_request)
    metrics = {}
    try:
        page.goto(ORIGIN, wait_until="networkidle")
        page.evaluate(INSTRUMENT)
        page.evaluate("""async () => {
            const core = await import('/core.js');
            Object.assign(core.els, {
                analysisPlots: document.querySelector('#analysis-plots'),
                plotCount: document.querySelector('#plot-count'),
                plotControls: document.querySelector('#plot-controls'),
                plotFilterError: document.querySelector('#plot-filter-error'),
            });
            window.plotsModule = await import('/plots.js');
            window.syntheticPlots = Array.from({ length: 24 }, (_, index) => ({
                path: `plot-${index}.xvg`, title: `Synthetic ${index}`, category: 'quality',
                series: ['signal'], sample_count: 1200, mean: 0, last: 1,
                x_label: 'Time', y_label: 'Signal',
                points: Array.from({ length: 1200 }, (_, point) => [point, Math.sin(point / 20 + index)]),
            }));
        }""")

        def phase(name, action, *, empty=False, all_visible=False):
            page.evaluate("window.drawMetrics = []")
            action()
            page.evaluate("window.settlePlots()")
            records = page.evaluate("window.drawMetrics")
            counts = {}
            for record in records:
                key = (record["id"], record["frame"])
                counts[key] = counts.get(key, 0) + 1
            elapsed = sorted(record["canvas_ms"] for record in records)
            metrics[name] = {
                "draws": len(records),
                "offscreen_draws": sum(not record["onscreen"] for record in records),
                "detached_draws": sum(not record["connected"] for record in records),
                "max_draws_per_canvas_per_frame": max(counts.values(), default=0),
                "canvas_ms_total": round(sum(elapsed), 3),
                "canvas_ms_p95": round(elapsed[min(len(elapsed) - 1, int(len(elapsed) * 0.95))], 3) if elapsed else 0,
            }
            if strict:
                assert all(record["onscreen"] and record["connected"] for record in records), (name, records)
                assert max(counts.values(), default=0) <= 1, (name, records)
                if empty:
                    assert not records, (name, records)
                if all_visible:
                    visible = page.evaluate("window.visibleCanvases().map(canvas => Number(canvas.dataset.plotIndex))")
                    assert visible, name
                    assert sorted(record["index"] for record in records) == sorted(visible), (name, records, visible)
            return records

        phase("initial", lambda: page.evaluate("window.plotsModule.renderPlots('synthetic', window.syntheticPlots)"), all_visible=True)
        phase("resize", lambda: page.set_viewport_size({"width": 850, "height": 768}), all_visible=True)
        phase(
            "resize_burst",
            lambda: page.evaluate("""() => {
                for (const width of [650, 680, 700]) {
                    document.querySelector('#plot-panel').style.width = `${width}px`;
                    window.dispatchEvent(new Event('resize'));
                }
            }"""),
            all_visible=True,
        )
        phase("unchanged_resize", lambda: page.evaluate("window.dispatchEvent(new Event('resize'))"), empty=True)
        phase(
            "offscreen_resize",
            lambda: page.evaluate("document.querySelector('canvas[data-plot-index=\"20\"]').style.height = '220px'"),
            empty=True,
        )
        phase(
            "viewport_edge",
            lambda: page.evaluate("""() => {
                const canvas = document.querySelector('canvas[data-plot-index="10"]');
                window.scrollTo(0, canvas.getBoundingClientRect().top + scrollY - innerHeight);
            }"""),
            all_visible=True,
        )
        entered = phase("viewport_edge_enter", lambda: page.evaluate("window.scrollBy(0, 1)"))
        if strict:
            assert [record["index"] for record in entered] == [10], entered
        phase(
            "scroll",
            lambda: page.locator('canvas[data-plot-index="20"]').evaluate("canvas => canvas.scrollIntoView({ block: 'center' })"),
            all_visible=True,
        )
        phase("theme", lambda: page.emulate_media(color_scheme="dark"), all_visible=True)
        if strict:
            assert page.evaluate("window.visibleCanvases().every(canvas => canvas.getContext('2d').fillStyle === '#dddddd')")
        phase("scroll_back_after_theme", lambda: page.evaluate("window.scrollTo(0, 0)"), all_visible=True)
        phase("hide", lambda: page.evaluate("document.querySelector('#plot-panel').hidden = true"), empty=True)
        phase("hidden_theme", lambda: page.emulate_media(color_scheme="light"), empty=True)
        phase("show_after_theme", lambda: page.evaluate("document.querySelector('#plot-panel').hidden = false"), all_visible=True)
        phase("hide_again", lambda: page.evaluate("document.querySelector('#plot-panel').hidden = true"), empty=True)
        phase("show_unchanged", lambda: page.evaluate("document.querySelector('#plot-panel').hidden = false"), empty=True)
        session = context.new_cdp_session(page)

        def change_pixel_ratio():
            session.send(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": 850,
                    "height": 768,
                    "deviceScaleFactor": 2,
                    "mobile": False,
                },
            )
            # CDP changes DPR without invalidating media queries; trigger that
            # evaluation without changing either the viewport or color scheme.
            page.emulate_media(color_scheme="light")

        phase("dpr", change_pixel_ratio, all_visible=True)
        if strict:
            assert page.evaluate(
                "devicePixelRatio === 2 && window.visibleCanvases().every(canvas => canvas.width === Math.round(canvas.clientWidth * 2))"
            )
        phase(
            "replace_before_frame",
            lambda: page.evaluate("""() => {
            window.plotsModule.renderPlots('replacement-a', window.syntheticPlots);
            window.plotsModule.renderPlots('replacement-b', window.syntheticPlots);
        }"""),
            all_visible=True,
        )
        phase(
            "reset_before_frame",
            lambda: page.evaluate("""() => {
            document.querySelector('#plot-panel').style.width = '680px';
            window.dispatchEvent(new Event('resize'));
            window.plotsModule.resetPlotFilters();
            document.querySelector('#analysis-plots').replaceChildren();
        }"""),
            empty=True,
        )
        phase(
            "empty_before_frame",
            lambda: page.evaluate("""() => {
            window.plotsModule.renderPlots('discarded', window.syntheticPlots);
            window.plotsModule.renderPlots('empty', []);
        }"""),
            empty=True,
        )
        phase(
            "render_after_reset",
            lambda: page.evaluate("window.plotsModule.renderPlots('synthetic', window.syntheticPlots)"),
            all_visible=True,
        )
        canvas = page.locator('canvas[data-plot-index="0"]')
        canvas.hover()
        if strict:
            assert page.locator(".plot-readout").first.text_content().startswith("signal:"), "Hover readout must remain available"
        assert not errors, errors
        assert not unexpected_requests, unexpected_requests
        return metrics
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-module", type=Path)
    parser.add_argument("--output", type=Path, help="Write counts and canvas painting timings as JSON")
    args = parser.parse_args()
    result = {"fixture": {"plots": 24, "points_per_plot": 1200}, "timing_scope": "canvas setup/paint only; synthetic Chromium fixture"}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result["current"] = run(browser, MODULE, strict=True, no_intersection=False)
            result["without_intersection_observer"] = run(browser, MODULE, strict=True, no_intersection=True)
            if args.baseline_module:
                result["baseline"] = run(browser, args.baseline_module, strict=False, no_intersection=False)
        finally:
            browser.close()
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized)
    print(serialized)


if __name__ == "__main__":
    main()
