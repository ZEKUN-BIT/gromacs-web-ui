"""Browser -> isolated real API -> worker/GROMACS -> SSE -> result download.

Requires gmx and Chromium. No API mocks, user's settings or existing jobs are used.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def serve(runtime: Path, port: int, gmx: str) -> None:
    # Patch before importing main: its module-level store must never open the
    # repository's settings.json/runtime, even when those override environment.
    import app.gromacs as gromacs

    settings = {
        **gromacs.default_settings(ROOT),
        "runtime_root": str(runtime),
        "gmx_bin": gmx,
        "max_parallel": 1,
        "default_ntmpi": 1,
        "default_ntomp": 1,
    }
    gromacs.load_settings = lambda _base: dict(settings)
    import uvicorn

    import app.main as main
    from app.execution import WorkerEngine
    from app.worker import WorkerService

    service = WorkerService(WorkerEngine(main.STORE, "browser-integration"), lambda: dict(settings), poll_interval=0.1)
    thread = threading.Thread(target=service.run, daemon=True)

    @asynccontextmanager
    async def lifespan(app):
        async with main.app_lifespan(app):
            thread.start()
            try:
                yield
            finally:
                service.stop.set()
                thread.join(timeout=5)
                for slot in service.threads:
                    slot.join(timeout=5)

    main.app.router.lifespan_context = lifespan
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="warning")


OBSERVERS = """(() => {
    window.liveEvents = [];
    const Native = window.EventSource;
    window.EventSource = class extends Native {
        constructor(...args) { super(...args);
            this.addEventListener('job', e => window.liveEvents.push(JSON.parse(e.data)));
        }
    };
    window.lab = {lcp: null, cls: 0, longTasks: []};
    const observe = (type, handle) => {
        if (PerformanceObserver.supportedEntryTypes.includes(type))
            new PerformanceObserver(list => list.getEntries().forEach(handle)).observe({type, buffered:true});
    };
    observe('largest-contentful-paint', e => window.lab.lcp = e.startTime);
    observe('layout-shift', e => {if (!e.hadRecentInput) window.lab.cls += e.value;});
    observe('longtask', e => window.lab.longTasks.push(e.duration));
})();"""


def measure(page) -> dict:
    return page.evaluate("""() => ({...window.lab,
        navigationMs: performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd,
        resources: performance.getEntriesByType('resource').map(e => ({
            path: new URL(e.name).pathname, transferSize:e.transferSize, encodedBodySize:e.encodedBodySize}))})""")


def check(artifact_dir: Path, gmx: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    from app.demo import example_inputs

    artifact_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gromacs-live-browser-") as temporary:
        root = Path(temporary)
        inputs = root / "inputs"
        inputs.mkdir()
        for name, content in example_inputs().items():
            (inputs / name).write_text(content, encoding="utf-8")
        environment = {
            **os.environ,
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "GMX_DISABLE_GPU_DETECTION": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "GMX_MAXBACKUP": "-1",
        }
        result = subprocess.run(
            [gmx, "grompp", "-f", "example.mdp", "-c", "initial.gro", "-p", "example.top", "-o", "browser.tpr"],
            cwd=inputs,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        (artifact_dir / "grompp.log").write_text(result.stdout)
        assert result.returncode == 0, result.stdout
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with (artifact_dir / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--runtime",
                    str(root / "runtime"),
                    "--port",
                    str(port),
                    "--gmx",
                    gmx,
                ],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(300):
                    if server.poll() is not None:
                        raise AssertionError((artifact_dir / "server.log").read_text())
                    try:
                        with urllib.request.urlopen(origin + "/api/health", timeout=1) as response:
                            health = json.load(response)
                        if health.get("worker", {}).get("state") == "ready":
                            break
                        # The frontend is usable while heartbeat starts; wait for
                        # readiness independently below if health schema differs.
                        if health.get("gromacs", {}).get("available"):
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError("Isolated API did not become ready")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="zh-CN", accept_downloads=True)
                    page = context.new_page()
                    errors, external, requests = [], [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "request",
                        lambda request: (
                            requests.append(request.url),
                            external.append(request.url) if not request.url.startswith(origin + "/") else None,
                        ),
                    )
                    page.add_init_script(OBSERVERS)
                    try:
                        page.goto(origin, wait_until="networkidle")
                        cold = measure(page)
                        page.reload(wait_until="networkidle")
                        warm = measure(page)
                        assert not any("/api/agent/" in url for url in requests), "AI work must stay deferred until opened"
                        page.locator("#workflow-intent").select_option("tpr")
                        page.locator('#job-form [name="workflow"][value="run_tpr"]').check()
                        page.locator("#job-name").fill("真实浏览器 · 氩体系")
                        page.locator("#files").set_input_files(inputs / "browser.tpr")
                        page.get_by_role("button", name="专业视图", exact=True).click()
                        page.locator('#job-form [name="ntmpi"]').fill("1")
                        page.locator('#job-form [name="ntomp"]').fill("1")
                        page.locator('#job-form [name="gpu"]').uncheck()
                        expect(page.locator('#job-form [name="tpr_file"]')).to_have_value("browser.tpr")
                        expect(page.locator("#submit-job")).to_be_enabled()
                        page.locator("#submit-job").click()
                        expect(page.locator("#submit-warning")).to_be_visible()
                        with page.expect_response(
                            lambda response: response.url == origin + "/api/jobs" and response.request.method == "POST"
                        ) as submitted:
                            page.locator("#submit-warning-confirm").click()
                        response = submitted.value
                        assert response.ok, response.text()
                        job = response.json()
                        expect(page.locator("#job-status")).to_have_attribute("data-state", "completed", timeout=120_000)
                        page.wait_for_function(
                            "id => window.liveEvents.some(job => job.id === id && job.status === 'completed')",
                            arg=job["id"],
                            timeout=15_000,
                        )
                        expect(page.locator("#job-log")).to_contain_text("[worker] completed", timeout=15_000)
                        expect(page.locator("#download-result-bundle")).to_be_visible()
                        with page.expect_download() as downloaded:
                            page.locator("#download-result-bundle").click()
                        bundle = artifact_dir / "live-result.zip"
                        downloaded.value.save_as(bundle)
                        with zipfile.ZipFile(bundle) as archive:
                            names = archive.namelist()
                            assert any(name.endswith("execution-manifest.json") for name in names), names
                        page.locator('[data-detail-tab="files"]').click()
                        expect(page.locator("#output-files")).to_contain_text("browser.xtc")
                        with page.expect_download() as coordinates:
                            page.locator("#output-files a", has_text="browser.gro").click()
                        gro = artifact_dir / "browser.gro"
                        coordinates.value.save_as(gro)
                        assert int(gro.read_text().splitlines()[1]) == 64
                        page.screenshot(path=str(artifact_dir / "completed.png"))
                        assert not errors, errors
                        assert not external, external
                        report = {
                            "scope": "isolated localhost, Chromium, real CPU GROMACS; laboratory navigation, not field INP",
                            "job_id": job["id"],
                            "status": "completed",
                            "cold": cold,
                            "warm": warm,
                            "sse_statuses": sorted(
                                {event["status"] for event in page.evaluate("window.liveEvents") if event["id"] == job["id"]}
                            ),
                            "bundle_files": names,
                        }
                        (artifact_dir / "live-browser.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
                        print(
                            f"PASS real upload, preview, confirmation, worker, GROMACS, SSE, log and result ZIP; cold/warm DOM ready {cold['navigationMs']:.0f}/{warm['navigationMs']:.0f} ms"
                        )
                    finally:
                        context.close()
                        browser.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=ROOT / "runtime/frontend-quality/live")
    parser.add_argument("--gmx", default=shutil.which("gmx") or "/usr/local/gromacs/bin/gmx")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--runtime", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.runtime, args.port, args.gmx)
    else:
        check(args.artifact_dir.resolve(), str(Path(args.gmx).resolve()))


if __name__ == "__main__":
    main()
