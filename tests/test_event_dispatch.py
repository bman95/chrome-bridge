"""EventDispatcher edge cases not reachable through the client tests."""

from __future__ import annotations

import threading

from chrome_bridge.bridge.event_dispatch import EventDispatcher


def test_close_with_full_queue_never_raises_and_stops_worker():
    # Wedge the worker inside a callback, then fill the queue to its cap:
    # close() must drop queued deliveries to land its stop marker instead of
    # raising queue.Full out of a teardown path.
    dispatcher = EventDispatcher(max_pending=4)
    entered = threading.Event()
    release = threading.Event()

    def blocking_callback(_params):
        entered.set()
        release.wait(5.0)

    dispatcher.submit([blocking_callback], "Wedge.evt", {})
    assert entered.wait(2.0)
    for _ in range(8):  # over-fill: put_nowait drops (and warns) past the cap
        dispatcher.submit([lambda _params: None], "Fill.evt", {})

    dispatcher.close()  # must not raise
    release.set()
    dispatcher.join(timeout=5.0)
    assert not dispatcher._thread.is_alive()
    assert dispatcher.is_closed


def test_submit_after_close_is_dropped():
    dispatcher = EventDispatcher(max_pending=4)
    fired = threading.Event()
    dispatcher.close()
    dispatcher.submit([lambda _params: fired.set()], "Late.evt", {})
    assert not fired.wait(0.2)
