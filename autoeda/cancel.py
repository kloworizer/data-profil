"""Cooperative job cancellation.

A job thread installs a CancelToken with `use(token)`. Long loops call `check()`;
blocking work registers an abort hook with `on_cancel()` (DuckDB interrupt, Spark
job group cancel), so a stop request takes effect within a second or two.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading


class Cancelled(BaseException):
    """Raised inside a job when the user stopped it. A BaseException (like
    asyncio.CancelledError) so the many `except Exception` fallbacks don't swallow it."""


class CancelToken:
    def __init__(self):
        self._ev = threading.Event()
        self._hooks: list = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def cancel(self):
        with self._lock:
            self._ev.set()
            hooks, self._hooks = self._hooks, []
        for fn in hooks:
            try:
                fn()
            except Exception:
                pass

    def on_cancel(self, fn):
        """Run fn when the job is cancelled (immediately if it already is)."""
        with self._lock:
            if not self._ev.is_set():
                self._hooks.append(fn)
                return
        try:
            fn()
        except Exception:
            pass

    def check(self):
        if self._ev.is_set():
            raise Cancelled("Stopped by the user.")


_current: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar("autoeda_cancel", default=None)


def current() -> CancelToken | None:
    return _current.get()


def check():
    t = _current.get()
    if t is not None:
        t.check()


def on_cancel(fn):
    t = _current.get()
    if t is not None:
        t.on_cancel(fn)


def cancelled() -> bool:
    t = _current.get()
    return t is not None and t.cancelled


@contextlib.contextmanager
def use(token: CancelToken | None):
    reset = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(reset)
