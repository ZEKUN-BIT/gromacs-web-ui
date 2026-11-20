"""Check task-stream recovery in the real browser with fake events and time.

All HTTP requests use the existing synthetic product fixture. No simulation,
external service or real 45-second wait is required.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

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
    emit("log", {"reset": True, "start": 0, "offset": 42, "generation": "gen-1", "text": "Synthetic live log\n"})
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
    assert query["job_id"] == "product-1" and query["log_offset"] == "42" and query["log_generation"] == "gen-1", query
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

    emit("log", {"reset": True, "start": 0, "offset": 6, "generation": "gen-2", "text": "new\n"})
    expect(page.locator("#job-log")).to_have_text("new")
    expect(page.locator("#log-notice")).to_contain_text("日志已轮转")

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


def inspect_log_viewer(page, artifact_dir=None):
    """Exercise the open desktop panel without starting a simulation or using a real clipboard."""
    page.locator('[data-workspace-view="detail"]').click()
    panel = page.locator('.task-log-panel')
    log, search, follow = (page.locator(f'#{key}') for key in ('job-log', 'log-search', 'log-follow'))
    counter = page.locator('#log-count')

    def top():
        return log.evaluate('el => el.scrollTop')
    offset = 0

    def append(text, *, reset=False):
        nonlocal offset
        start = 0 if reset else offset
        offset = start + len(text.encode())
        page.evaluate('payload => testStreams.at(-1).dispatchEvent(new MessageEvent("log", {data: JSON.stringify(payload)}))',
                      {'start': start, 'offset': offset, 'text': text, 'reset': reset})

    lines = [f'Step {n * 1000:7d}    Time {n * 2:7.2f} ps    T = 300.0 K' for n in range(90)]
    lines[20] = 'WARNING: pressure coupling interval changed'
    lines[46] = 'Warning: pressure deviates from reference'
    lines[66] = 'WARNING: energy drift above tolerance'
    lines[70] += '  [a.b] <strong>sample</strong>'
    text = '\n'.join(lines) + '\n'
    append(text)  # Opening a previously closed viewer should start at the tail.
    panel.locator('summary').click()
    expect(follow).to_be_checked()
    page.wait_for_function('() => { const el=document.querySelector("#job-log"); return el.clientHeight && el.scrollHeight-el.clientHeight-el.scrollTop < 2 }')
    search.fill('warning')
    expect(counter).to_have_text('1 / 3')
    expect(follow).not_to_be_checked()
    expect(log.locator('mark')).to_have_count(3)
    first_top = top()
    page_y = page.evaluate('scrollY')
    search.press('Enter')
    expect(counter).to_have_text('2 / 3')
    assert top() > first_top and abs(page.evaluate('scrollY') - page_y) < 2
    search.press('Shift+Enter')
    expect(counter).to_have_text('1 / 3')
    page.locator('#log-prev').click()
    expect(counter).to_have_text('3 / 3')
    page.locator('#log-next').click()
    expect(counter).to_have_text('1 / 3')
    paused_top = top()
    tail = 'WARNING: new output\n'
    append(tail)
    expect(counter).to_have_text('1 / 4')
    expect(log.locator('.log-step').first).to_be_attached()  # Includes the deferred color pass.
    assert abs(top() - paused_top) < 2
    assert log.text_content() == text + tail
    expect(log.locator('strong')).to_have_count(0)
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        theme = page.evaluate('matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"')
        panel.screenshot(path=str(artifact_dir / f'log-viewer-{theme}.png'))

    page.evaluate('Object.defineProperty(navigator,"clipboard",{configurable:true,value:{async writeText(value){window.testCopied=value}}})')
    page.locator('#copy-log').click()
    assert page.evaluate('testCopied') == text + tail
    page.route('**/download?path=run.log', lambda route: route.fulfill(headers={'Content-Disposition': 'attachment; filename=run.log'}, body='retained'))
    with page.expect_download() as download:
        page.locator('#download-log').click()
    assert download.value.suggested_filename == 'run.log'
    search.fill('[a.b]')
    expect(log.locator('.log-current')).to_have_text('[a.b]')
    search.fill('no such line')
    expect(counter).to_have_text('无匹配')
    expect(page.locator('#log-next')).to_be_disabled()
    search.press('Escape')
    expect(search).to_have_value('')
    expect(counter).to_be_empty()
    page.locator('#log-bottom').click()
    expect(follow).to_be_checked()
    assert log.evaluate('el => el.scrollHeight - el.clientHeight - el.scrollTop < 2')
    follow.uncheck()
    paused_top = top()
    append(tail * 50)
    assert abs(top() - paused_top) < 2
    follow.check()
    assert top() > paused_top
    log.evaluate('el => {el.scrollTop -= 150}')
    expect(follow).not_to_be_checked()

    # Selecting a diagnostic backwards must survive both streaming and the idle color pass.
    append('pressure reading\n', reset=True)
    follow.uncheck()
    log.evaluate('el => getSelection().setBaseAndExtent(el.firstChild, 8, el.firstChild, 0)')
    append('WARNING: sample\n')
    page.wait_for_timeout(1650)
    assert page.evaluate("getSelection().toString()") == 'pressure'
    expect(log.locator('.log-warning')).to_have_count(0)
    page.evaluate('getSelection().removeAllRanges()')
    expect(log.locator('.log-warning')).to_have_text('WARNING')

    # A retained-window trim must preserve the surviving visible lines, not just their pixel offset.
    line = 'sample ' + '.' * 42 + '\n'
    data = line * 2950 + 'REFERENCE\n' + line * 3049
    append(data, reset=True)
    search.fill('REFERENCE')
    mark = log.locator('.log-current')
    before = mark.bounding_box()['y']
    mark.evaluate('el => getSelection().setBaseAndExtent(el.firstChild, 5, el.firstChild, 0)')
    append(line * 10)
    assert page.evaluate("getSelection().toString()") == 'REFER'
    assert page.evaluate('''() => { const s=getSelection(), r=s.getRangeAt(0);
        return s.anchorNode === r.endContainer && s.anchorOffset === r.endOffset; }''')
    page.evaluate('getSelection().removeAllRanges()')
    expect(counter).to_have_text('1 / 1')
    assert abs(mark.bounding_box()['y'] - before) < 2
    expect(page.locator('#log-notice')).to_contain_text('后续裁剪')
    search.fill('.')
    assert int(counter.inner_text().split('/')[-1]) > 200_000
    expect(log.locator('mark')).to_have_count(1)
    search.press('Shift+Enter')
    current, total = counter.inner_text().split(' / ')
    assert current == total
    page.locator('#log-clear').click()
    expect(search).to_be_focused()
    search.fill('REFERENCE')
    page.locator('[data-job-id="product-2"] .job-select').click()
    expect(search).to_have_value('')
    expect(follow).to_be_checked()
    expect(log).to_be_empty()
    expect(page.locator('#copy-log')).to_be_disabled()
    append('Step 1: new task only', reset=True)
    expect(log.locator('.log-step')).to_have_text('Step 1')
    expect(log).to_have_text('Step 1: new task only')
    expect(page.locator('#download-log')).to_have_attribute('href', '/api/jobs/product-2/download?path=run.log')
    print('PASS desktop log search, keyboard navigation, scrolling, clipping, raw exports and task isolation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', type=Path)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            check_viewport(browser, 1440, 1000, "light", None, inspect=inspect_connection, configure=configure)
            for theme in ("light", "dark"):
                check_viewport(browser, 1440, 1000, theme, None, inspect=lambda page: inspect_log_viewer(page, args.artifact_dir))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
