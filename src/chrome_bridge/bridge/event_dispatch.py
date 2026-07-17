"""Ordered, non-blocking delivery of Chrome Bridge event callbacks."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from typing import Any, TypeAlias

log = logging.getLogger(__name__)

EventCallback: TypeAlias = Callable[[dict[str, Any]], None]
_Delivery: TypeAlias = tuple[tuple[EventCallback, ...], str, dict[str, Any]]

_STOP = object()


class EventDispatcher:
    """Run listener callbacks away from the socket reader, in wire order.

    The bounded queue prevents an unbounded memory build-up when callbacks are
    slower than the event stream. Submission never blocks the reader; once the
    queue is full, the newest delivery is dropped and logged.
    """

    def __init__(self, *, max_pending: int = 1024) -> None:
        self._queue: queue.Queue[_Delivery | object] = queue.Queue(maxsize=max_pending)
        self._closed = threading.Event()
        self._start_lock = threading.Lock()
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="chrome-bridge-events",
            daemon=True,
        )

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    def submit(
        self,
        callbacks: Iterable[EventCallback],
        method: str,
        params: dict[str, Any],
    ) -> None:
        listeners = tuple(callbacks)
        if not listeners or self._closed.is_set():
            return
        with self._start_lock:
            if self._closed.is_set():
                return
            if not self._started:
                self._thread.start()
                self._started = True
        try:
            self._queue.put_nowait((listeners, method, params))
        except queue.Full:
            log.warning(
                "chrome-bridge: event queue full; dropping newest %s event",
                method,
            )

    def close(self) -> None:
        """Stop after already queued deliveries; never block the caller."""
        with self._start_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            started = self._started
        if not started:
            return
        # Loop until the stop marker lands: a submitter that passed its closed
        # check before close() set the flag can still be mid-put, so a single
        # drop-one-then-put attempt can lose the race and raise queue.Full out
        # of a teardown path. Shutdown outranks queued deliveries, and the
        # producer population is finite once _closed is set, so this converges.
        while True:
            try:
                self._queue.put_nowait(_STOP)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    pass

    def join(self, timeout: float | None = None) -> None:
        if self._started and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                if not isinstance(item, tuple):
                    continue
                listeners, method, params = item
                for callback in listeners:
                    try:
                        callback(params)
                    except Exception:  # noqa: BLE001 - isolate user callbacks
                        log.exception(
                            "chrome-bridge: event listener for %s raised", method
                        )
            finally:
                self._queue.task_done()
