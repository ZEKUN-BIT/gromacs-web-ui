"""Check task-stream recovery in the real browser with fake events and time.

All HTTP requests use the existing synthetic product fixture. No simulation,
external service or real 45-second wait is required.
"""

from __future__ import annotations

from datetime import UTC, datetime

from check_product_browser import check_viewport
from playwright.sync_api import expect, sync_playwright


def configure(page):
    now = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    page.clock.install(time=now)
    page.clock.pause_at(now)


def inspect_connection(page):
    notice = page.locator("#task-sync-notice")
    status = page.locator("#task-sync-state")
    synced = page.locator("#task-sync-time")

    def streams(*, total=None, active=1):
        snapshot = page.evaluate("({ total: testStreams.length, active: testStreams.filter(source => source.readyState !== 2).length })")
        assert snapshot["active"] == active, snapshot
        if total is not None:
            assert snapshot["total"] == total, snapshot
        return snapshot["total"]

    def emit(event_type, payload, *, old=False):
        page.evaluate(
            """({type, payload, old}) => {
                const source = old ? window.previousStream : window.testStreams.at(-1);
                source.dispatchEvent(new MessageEvent(type, {data: JSON.stringify(payload)}));
            }""",
            {"type": event_type, "payload": payload, "old": old},
        )

    def fail():
        page.evaluate("window.testStreams.at(-1).onerror(new Event('error'))")

    def retry():
        expect(notice).to_be_visible()
        page.locator("#retry-task-sync").click(force=True)

    total = streams()
    expect(status).to_have_text("正在连接实时更新…")
    expect(synced).to_have_attribute("hidden", "")
    expect(notice).to_be_hidden()
    page.evaluate("window.testStreams.at(-1).dispatchEvent(new Event('open'))")
    expect(status).to_have_text("正在连接实时更新…")

    job = {
        "id": "product-1",
        "name": "合成实时任务",
        "workflow": "run_tpr",
        "status": "running",
        "created_at": "2026-10-19T00:00:00Z",
        "updated_at": "2026-10-26T00:00:00Z",
        "step_count": 1,
        "step_index": 0,
        "progress_percent": 25,
        "params": {},
        "project": "原项目",
        "tags": [],
        "notes": "",
        "pinned": False,
    }
    emit("jobs", {"jobs": [job], "total": 1, "offset": 0, "has_more": False})
    expect(page.locator(".job-item")).to_have_count(1)
    expect(status).to_have_text("实时更新已连接")
    emit("job", job)
    expect(page.locator("#job-status")).to_have_attribute("data-state", "running")
    expect(page.locator("#job-percent")).to_have_text("25.0%")
    emit("log", {"reset": True, "start": 0, "offset": 42, "text": "Synthetic live log\n"})
    expect(page.locator("#job-log")).to_contain_text("Synthetic live log")
    first_sync = synced.get_attribute("datetime")
    page.clock.run_for(1000)
    emit("ping", {})
    assert synced.get_attribute("datetime") != first_sync
    last_sync = synced.get_attribute("datetime")
    streams(total=total)

    page.evaluate("window.previousStream = window.testStreams.at(-1)")
    fail()
    streams(total=total, active=0)
    expect(notice).to_have_attribute("data-state", "reconnecting")
    expect(notice).to_contain_text("后台任务不会因此停止")
    expect(page.locator("#job-percent")).to_have_text("25.0%")
    expect(page.locator("#job-log")).to_contain_text("Synthetic live log")
    assert synced.get_attribute("datetime") == last_sync
    fail()  # A second error from the closed source cannot schedule another retry.
    retry()
    total += 1
    streams(total=total)
    query = page.evaluate("Object.fromEntries(new URL(testStreams.at(-1).url, location.href).searchParams)")
    assert query["job_id"] == "product-1" and query["log_offset"] == "42", query
    page.clock.run_for(1500)
    streams(total=total)  # Manual recovery cancelled the old backoff timer.

    emit("job", {**job, "status": "failed", "progress_percent": 99}, old=True)
    emit("jobs", {"jobs": [], "total": 0}, old=True)
    emit("log", {"reset": True, "start": 0, "offset": 100, "text": "STALE LOG"}, old=True)
    emit("ping", {}, old=True)
    page.evaluate("window.previousStream.onerror(new Event('error'))")
    expect(page.locator(".job-item")).to_have_count(1)
    expect(page.locator("#job-status")).to_have_attribute("data-state", "running")
    expect(page.locator("#job-log")).not_to_contain_text("STALE LOG")
    assert synced.get_attribute("datetime") == last_sync
    streams(total=total)
    emit("ping", {})
    expect(notice).to_be_hidden()

    # Missing heartbeats keep the last data and close exactly at 45 seconds.
    page.clock.run_for(44_999)
    streams(total=total)
    page.clock.run_for(1)
    streams(total=total, active=0)
    expect(notice).to_have_attribute("data-state", "reconnecting")
    page.clock.run_for(1499)
    streams(total=total, active=0)
    page.clock.run_for(1)
    total += 1
    streams(total=total)

    # Consecutive errors back off, reaching the cap without overlapping streams.
    for delay in (3000, 6000, 12_000, 15_000):
        fail()
        streams(total=total, active=0)
        page.clock.run_for(delay - 1)
        streams(total=total, active=0)
        page.clock.run_for(1)
        total += 1
        streams(total=total)
    emit("ping", {})
    expect(status).to_have_text("实时更新已连接")
    fail()
    page.clock.run_for(1499)
    streams(total=total, active=0)
    page.clock.run_for(1)
    total += 1
    streams(total=total)

    # Going offline cancels a pending retry and prevents manual retry requests.
    fail()
    page.evaluate("""() => {
        Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => false });
        window.dispatchEvent(new Event('offline'));
    }""")
    expect(notice).to_have_attribute("data-state", "offline")
    retry()
    page.clock.run_for(60_000)
    streams(total=total, active=0)
    page.evaluate("""() => {
        Object.defineProperty(navigator, 'onLine', { configurable: true, get: () => true });
        window.dispatchEvent(new Event('online'));
    }""")
    total += 1
    streams(total=total)
    emit("ping", {})
    expect(notice).to_be_hidden()
    expect(status).to_have_text("实时更新已连接")

    # A valid heartbeat extends the deadline; malformed event data fails closed.
    page.clock.run_for(30_000)
    emit("ping", {})
    page.clock.run_for(30_000)
    streams(total=total)
    page.evaluate("testStreams.at(-1).dispatchEvent(new MessageEvent('job', {data: '{malformed'}))")
    streams(total=total, active=0)
    expect(notice).to_have_attribute("data-state", "reconnecting")
    retry()
    total += 1
    streams(total=total)
    emit("ping", {})

    # Background pages pause and resume through the same single-connection path.
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    streams(total=total, active=0)
    expect(status).to_have_text("实时更新已暂停")
    page.clock.run_for(60_000)
    streams(total=total, active=0)
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    expect(page.locator(".job-item")).to_have_count(2)  # Visible-page history refresh completed.
    page.wait_for_function("testStreams.at(-1).readyState === 1", polling=10)
    streams(total=total + 1)
    print("PASS task stream events, stale-source isolation, heartbeat deadline, backoff, manual recovery and offline/visibility lifecycle")


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            check_viewport(browser, 1440, 1000, "light", None, inspect=inspect_connection, configure=configure)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
