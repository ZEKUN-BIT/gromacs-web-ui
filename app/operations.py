"""Cooperative cancellation for bounded local preparation work."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

_CURRENT: ContextVar[OperationContext | None] = ContextVar("local_operation", default=None)


class OperationCancelled(Exception):
    pass


class OperationTimeout(Exception):
    pass


class OperationContext:
    def __init__(self, seconds: float, progress: Callable[[dict], None]):
        self._remaining = seconds
        self._progress = progress
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._committed = False
        self._deadline: float | None = None

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    @contextmanager
    def local(self):
        started = time.monotonic()
        self._deadline = started + self._remaining
        try:
            self.checkpoint()
            yield self
        finally:
            self._remaining -= time.monotonic() - started
            self._deadline = None

    def checkpoint(self):
        if not self._committed:
            if self._cancelled.is_set():
                raise OperationCancelled("操作已取消，尚未提交的任务不会入队。")
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise OperationTimeout("本地准备操作已达到时间上限，未完成的任务不会入队，请缩小输入后重新规划。")

    def cancel(self) -> bool:
        with self._lock:
            if self._committed:
                return False
            self._cancelled.set()
            return True

    def report(self, value: dict):
        self.checkpoint()
        self._progress({**value, "cancellable": not self._committed})

    @contextmanager
    def commit(self):
        # Claim the short irreversible phase atomically against cancellation.
        # A completed queue/cancel/resume result must never become 'cancelled'.
        with self._lock:
            self.checkpoint()
            self._committed = True
        self._progress({"phase": "commit", "message": "正在提交已确认操作…", "cancellable": False})
        yield


def current_operation() -> OperationContext | None:
    return _CURRENT.get()
