"""Bounded progress events for analysis preparation; the existing sync API remains available."""

from __future__ import annotations

import asyncio
import json

from starlette.concurrency import run_in_threadpool

from .existing_analysis import create_existing_analysis


async def analysis_preparation_events(store, job_id, request, settings):
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue(maxsize=8)
    closed = False

    def deliver(event: dict) -> None:
        if closed:
            return
        if events.full():
            events.get_nowait()
        events.put_nowait(event)

    def progress(event: dict) -> None:
        loop.call_soon_threadsafe(deliver, {"type": "progress", **event})

    async def prepare() -> None:
        try:
            job = await run_in_threadpool(create_existing_analysis, store, job_id, request, settings, progress=progress)
        except FileNotFoundError:
            deliver({"type": "error", "message": "来源任务或分析输入不存在。"})
        except ValueError as exc:
            deliver({"type": "error", "message": str(exc)})
        except Exception:
            deliver({"type": "error", "message": "分析准备已取消或失败，请检查任务状态、文件及可用磁盘空间。"})
        else:
            deliver({"type": "result", "job": job})

    task = asyncio.create_task(prepare())
    # Keep preparation alive on browser disconnect, just like the synchronous
    # endpoint. The task owns file reservations and records/cleans up failures.
    try:
        while True:
            try:
                event = await asyncio.wait_for(events.get(), timeout=10)
            except TimeoutError:
                event = {"type": "heartbeat"}
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event["type"] in {"result", "error"}:
                break
    finally:
        closed = True
        # asyncio holds only weak task references. Retain this task until its
        # final cleanup even when the response consumer has disconnected.
        _preparations.add(task)
        task.add_done_callback(_preparations.discard)


_preparations: set[asyncio.Task] = set()
