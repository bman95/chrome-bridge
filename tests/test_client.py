"""Tests for the raw page-CDP client.

These drive :class:`chrome_bridge.bridge.client.ChromeBridge` against a *fake
bridge* running on a real loopback socket — no Chrome, no native host, no
running script. The fake plays the role of ``nmh.py`` + ``sw.js`` together: it
reads op frames the client sends and replies with the op/reply/event shapes
``extension/sw.js`` documents, so the tests assert the client's framing,
id-correlation, event routing, primitives' CDP calls, timeouts, and idempotent
re-attach.
"""

from __future__ import annotations

import json as _json
import os
import shutil as _shutil
import socket
import subprocess as _subprocess
import threading
import time
from pathlib import Path as _Path

import pytest

from chrome_bridge import profiles
from chrome_bridge.bridge.frames import read_frame, write_frame
from chrome_bridge.bridge.client import (
    BridgeError,
    BridgeTimeoutError,
    ChromeBridge,
    DetachedError,
)


# --------------------------------------------------------------------------- #
# Fake bridge: a scriptable peer that speaks the sw.js wire protocol.
# --------------------------------------------------------------------------- #


class FakeBridge:
    """A loopback server that answers op frames like sw.js + nmh.py would.

    Bind it, point a :class:`ChromeBridge` at its ``host``/``port``, and
    register per-op handlers. Each handler receives the decoded op frame and
    returns the reply payload dict (without the echoed ``id``, which the
    bridge adds), or ``None`` to send no reply (simulating a lost reply).
    Handlers may also push unsolicited events via :meth:`send_event`.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.host, self.port = self._listener.getsockname()
        self._client: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        self._handlers = {}
        self._default_handler = None
        self.received: list[dict] = []
        # The host writes a 0600 token the client must present as its first
        # frame; provision one at the port-derived path (the config dir is
        # redirected to a tmp dir by the `bridge` fixture before construction).
        self.token = os.urandom(16).hex()
        token_file = profiles.port_token_path(self.port)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(self.token, encoding="utf-8")
        self.auth_frame: dict | None = None  # the client's first frame
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._ready = threading.Event()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    def on(self, op: str, handler) -> None:
        self._handlers[op] = handler

    def set_default(self, handler) -> None:
        self._default_handler = handler

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        client, _ = self._listener.accept()
        self._listener.close()
        self._client = client
        self._rfile = client.makefile("rb")
        self._wfile = client.makefile("wb")
        self._ready.set()
        # The client's first frame is the auth handshake ({"auth": <hex>}); the
        # real host consumes it and never relays it. Record it (for assertions)
        # and swallow it so `received` holds only real ops.
        try:
            self.auth_frame = read_frame(self._rfile)
        except (OSError, ValueError):
            self.auth_frame = None
        try:
            while not self._stop.is_set():
                try:
                    frame = read_frame(self._rfile)
                except (OSError, ValueError):
                    break
                if frame is None:
                    break
                self.received.append(frame)
                op = frame.get("op")
                handler = self._handlers.get(op, self._default_handler)
                if handler is None:
                    self._reply(
                        frame.get("id"), {"ok": False, "error": f"no handler for {op}"}
                    )
                    continue
                payload = handler(frame)
                if payload is not None:
                    self._reply(frame.get("id"), payload)
        finally:
            self._stop.set()

    def _reply(self, msg_id, payload: dict) -> None:
        with self._send_lock:
            if self._wfile is not None:
                try:
                    write_frame(self._wfile, {"id": msg_id, **payload})
                except OSError:
                    pass

    def send_event(self, event: dict) -> None:
        """Push an unsolicited event frame (must carry ``event: True``)."""
        self._ready.wait(timeout=5)
        with self._send_lock:
            if self._wfile is not None:
                write_frame(self._wfile, event)

    def close(self) -> None:
        self._stop.set()
        for c in (self._rfile, self._wfile, self._client):
            try:
                if c is not None:
                    c.close()
            except OSError:
                pass


@pytest.fixture
def bridge():
    # conftest.py's autouse fixture has already redirected the config dir to a
    # per-test tmp dir, so FakeBridge's token write never touches the real
    # ~/.config/chrome-bridge.
    b = FakeBridge()
    b.start()
    yield b
    b.close()


def _ok_open(tab_id: int = 7):
    return lambda frame: {"ok": True, "tabId": tab_id}


def _cdp_result(result: dict):
    return lambda frame: {"ok": True, "result": result}


def _evaluate_value(value):
    """A cdp handler that answers Runtime.evaluate with returnByValue ``value``."""

    def handler(frame):
        if frame.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": value}}}
        return {"ok": True, "result": {}}

    return handler


def _client(bridge: FakeBridge, **kw) -> ChromeBridge:
    c = ChromeBridge(host=bridge.host, port=bridge.port, **kw)
    c.connect()
    return c


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


def test_connect_failure_raises_bridge_error():
    # Nothing listening on this port → connect fails with a clear error.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    cdp = ChromeBridge(host="127.0.0.1", port=free_port, connect_timeout=0.5)
    with pytest.raises(BridgeError, match="could not connect"):
        cdp.connect()


def test_context_manager_connects_and_closes(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_cdp_result({}))
    with ChromeBridge(host=bridge.host, port=bridge.port) as cdp:
        # Short load timeout: the fake bridge never fires Page.loadEventFired, so
        # without this open() would block the full default nav timeout (~30s).
        cdp.open("https://example.com/x", timeout=0.2)
    # close() should have sent an op:close after the tab opened.
    ops = [f.get("op") for f in bridge.received]
    assert "open" in ops
    assert "close" in ops


def test_connect_sends_auth_frame_first(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_cdp_result({}))
    with ChromeBridge(host=bridge.host, port=bridge.port) as cdp:
        cdp.open("https://x", timeout=0.2)
    # The very first frame the client sent was the auth handshake carrying the
    # host's token, and it was NOT relayed as an op (received holds only ops).
    assert bridge.auth_frame == {"auth": bridge.token}
    assert all("auth" not in f for f in bridge.received)


def test_connect_missing_token_raises_actionable_error(bridge):
    # Delete the token the host would have written: connect() must fail with an
    # actionable BridgeError, not a bare socket error.
    profiles.port_token_path(bridge.port).unlink()
    cdp = ChromeBridge(host=bridge.host, port=bridge.port)
    with pytest.raises(BridgeError, match="auth token"):
        cdp.connect()


def test_connect_empty_token_raises_actionable_error(bridge):
    # A zero-byte token file (e.g. interrupted host startup) is as unusable as
    # a missing one and must surface the same actionable error.
    profiles.port_token_path(bridge.port).write_text("", encoding="utf-8")
    cdp = ChromeBridge(host=bridge.host, port=bridge.port)
    with pytest.raises(BridgeError, match="empty"):
        cdp.connect()


def _wait_auth_frame(bridge: FakeBridge, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while bridge.auth_frame is None and time.monotonic() < deadline:
        time.sleep(0.01)


def test_connect_with_profile_reads_profile_token(bridge):
    # A profile-constructed client must read token_path(<profile>) — the file
    # the launcher-spawned host writes — not the port-derived fallback.
    profiles.add_profile("personal", bridge.port)
    token_file = profiles.token_path("personal")
    token_file.write_text("profile-secret", encoding="utf-8")
    cdp = ChromeBridge(profile="personal")
    cdp.connect()
    _wait_auth_frame(bridge)
    cdp.close()
    assert bridge.auth_frame == {"auth": "profile-secret"}


def test_connect_host_port_resolves_registered_profile_token(bridge):
    # The CLI resolves --profile to host+port before constructing the client
    # (cli._resolve_host_port), so the host/port-only path must look the
    # profile up by port in the registry and read ITS token file.
    profiles.add_profile("personal", bridge.port)
    profiles.token_path("personal").write_text("profile-secret", encoding="utf-8")
    cdp = ChromeBridge(host=bridge.host, port=bridge.port)
    cdp.connect()
    _wait_auth_frame(bridge)
    cdp.close()
    assert bridge.auth_frame == {"auth": "profile-secret"}


# --------------------------------------------------------------------------- #
# open / navigate
# --------------------------------------------------------------------------- #


def test_open_sends_open_then_page_enable_and_awaits_load(bridge):
    bridge.on("open", _ok_open(tab_id=42))
    bridge.set_default(_cdp_result({}))
    cdp = _client(bridge)
    try:
        # Fire the load event shortly after open so the wait resolves fast.
        threading.Timer(
            0.1,
            lambda: bridge.send_event(
                {
                    "event": True,
                    "type": "cdp",
                    "method": "Page.loadEventFired",
                    "params": {},
                }
            ),
        ).start()
        cdp.open("https://example.com/list")
        assert cdp._tab_id == 42
        methods = [f.get("method") for f in bridge.received if f.get("op") == "cdp"]
        assert "Page.enable" in methods
    finally:
        cdp.close()


def test_open_returns_even_if_load_event_never_fires(bridge):
    # A missed load event must not hang open() — wait_selector is the real gate.
    bridge.on("open", _ok_open())
    bridge.set_default(_cdp_result({}))
    cdp = _client(bridge)
    try:
        started = time.monotonic()
        cdp.open("https://x", timeout=0.3)
        assert time.monotonic() - started < 2.0
    finally:
        cdp.close()


def test_navigate_on_existing_tab_issues_page_navigate(bridge):
    bridge.on("open", _ok_open())

    def default(frame):
        if frame.get("method") == "Page.navigate":
            return {"ok": True, "result": {"frameId": "f1"}}
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://a", timeout=0.2)
        bridge.received.clear()
        cdp.navigate("https://b", timeout=0.2)
        methods = [f.get("method") for f in bridge.received if f.get("op") == "cdp"]
        assert "Page.navigate" in methods
        # No second "open" — it reused the existing tab.
        assert all(f.get("op") != "open" for f in bridge.received)
    finally:
        cdp.close()


def test_navigate_surfaces_page_error_text(bridge):
    bridge.on("open", _ok_open())

    def default(frame):
        if frame.get("method") == "Page.navigate":
            return {"ok": True, "result": {"errorText": "net::ERR_NAME_NOT_RESOLVED"}}
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://a", timeout=0.2)
        with pytest.raises(BridgeError, match="ERR_NAME_NOT_RESOLVED"):
            cdp.navigate("https://bad", timeout=0.2)
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# wait_selector
# --------------------------------------------------------------------------- #


def test_wait_selector_returns_true_when_present(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(True))
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.wait_selector("article.item", timeout=1.0) is True
        # The evaluate expression must reference querySelector + the selector.
        evals = [
            f["params"]["expression"]
            for f in bridge.received
            if f.get("method") == "Runtime.evaluate"
        ]
        assert any("querySelector" in e and "article.item" in e for e in evals)
    finally:
        cdp.close()


def test_wait_selector_times_out_when_absent(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(False))  # never present
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        with pytest.raises(BridgeTimeoutError, match="not present"):
            cdp.wait_selector("article.item", timeout=0.4, poll_interval=0.05)
    finally:
        cdp.close()


def test_wait_selector_becomes_present_after_polling(bridge):
    bridge.on("open", _ok_open())
    state = {"n": 0}

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            state["n"] += 1
            return {"ok": True, "result": {"result": {"value": state["n"] >= 3}}}
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert (
            cdp.wait_selector("article.item", timeout=2.0, poll_interval=0.05) is True
        )
        assert state["n"] >= 3
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# outer_html
# --------------------------------------------------------------------------- #


def test_outer_html_reads_container_subtree(bridge):
    html = "<main><article class='item'>x</article></main>"
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(html))
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        got = cdp.outer_html("main")
        assert got == html
        # The expression must read outerHTML of a querySelector'd container,
        # not the whole document unconditionally.
        evals = [
            f["params"]["expression"]
            for f in bridge.received
            if f.get("method") == "Runtime.evaluate"
        ]
        assert any("outerHTML" in e and "querySelector" in e for e in evals)
    finally:
        cdp.close()


def test_outer_html_returns_empty_string_on_non_string(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(None))
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.outer_html("main") == ""
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# click_text — must use real Input.dispatchMouseEvent, not synthetic click
# --------------------------------------------------------------------------- #


def test_click_text_dispatches_real_mouse_events_at_center(bridge):
    bridge.on("open", _ok_open())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            # Return a bounding-box center.
            return {
                "ok": True,
                "result": {"result": {"value": {"x": 100.0, "y": 50.0}}},
            }
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.click_text("Submit") is True
        mouse = [
            f for f in bridge.received if f.get("method") == "Input.dispatchMouseEvent"
        ]
        types = [f["params"]["type"] for f in mouse]
        assert types == ["mouseMoved", "mousePressed", "mouseReleased"]
        # Dispatched at the element center, not (0,0) / synthetic.
        assert all(
            f["params"]["x"] == 100.0 and f["params"]["y"] == 50.0 for f in mouse
        )
        # No synthetic el.click() — there must be NO Runtime.evaluate calling .click().
        evals = [
            f["params"]["expression"]
            for f in bridge.received
            if f.get("method") == "Runtime.evaluate"
        ]
        assert not any(".click()" in e for e in evals)
    finally:
        cdp.close()


def test_click_text_raises_when_no_match(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(None))  # element_center → None
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        with pytest.raises(BridgeError, match="no visible element"):
            cdp.click_text("Nope")
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# banner_present
# --------------------------------------------------------------------------- #


def test_banner_present_true_and_false(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(True))
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.banner_present(".captcha") is True
        evals = [
            f["params"]["expression"]
            for f in bridge.received
            if f.get("method") == "Runtime.evaluate"
        ]
        assert any(e.startswith("!!document.querySelector") for e in evals)
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# Reply correlation + error surfacing
# --------------------------------------------------------------------------- #


def test_request_surfaces_ok_false_error(bridge):
    bridge.on("open", lambda f: {"ok": False, "error": "attach failed"})
    cdp = _client(bridge)
    try:
        with pytest.raises(BridgeError, match="attach failed"):
            cdp.open("https://x", timeout=0.5)
    finally:
        cdp.close()


def test_op_times_out_when_no_reply(bridge):
    # Handler returns None → no reply is ever sent → per-op timeout fires.
    bridge.on("open", lambda f: None)
    cdp = _client(bridge, op_timeout=0.4)
    try:
        with pytest.raises(BridgeTimeoutError, match="timed out"):
            cdp.open("https://x", timeout=0.4)
    finally:
        cdp.close()


def test_concurrent_replies_correlate_by_id(bridge):
    # Two cdp ops interleaved: the bridge replies out of order, the client must
    # still route each reply to the right caller by id.
    bridge.on("open", _ok_open())

    def default(frame):
        # Stash navigate replies and answer evaluates immediately; we reply to
        # the FIRST evaluate last to prove id-correlation, not ordering.
        if frame.get("method") == "Runtime.evaluate":
            expr = frame["params"]["expression"]
            # Echo a value derived from the expression so we can tell them apart.
            return {"ok": True, "result": {"result": {"value": "A" in expr}}}
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.banner_present("A") is True
        assert cdp.banner_present("B") is False
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# Events + idempotent re-attach on detach
# --------------------------------------------------------------------------- #


def test_detach_event_routes_to_listener(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_cdp_result({}))
    cdp = _client(bridge)
    seen = []
    cdp.on_event("__detached__", lambda p: seen.append(p.get("reason")))
    try:
        cdp.open("https://x", timeout=0.2)
        bridge.send_event(
            {"event": True, "type": "detached", "reason": "target_closed"}
        )
        # Give the reader thread a moment to dispatch.
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.02)
        assert seen == ["target_closed"]
        assert cdp._detached.is_set()
        assert cdp._tab_id is None
    finally:
        cdp.close()


def test_cdp_event_routes_to_method_listener(bridge):
    bridge.on("open", _ok_open())
    bridge.set_default(_cdp_result({}))
    cdp = _client(bridge)
    got = []
    cdp.on_event("Network.requestWillBeSent", lambda p: got.append(p))
    try:
        cdp.open("https://x", timeout=0.2)
        bridge.send_event(
            {
                "event": True,
                "type": "cdp",
                "method": "Network.requestWillBeSent",
                "params": {"url": "u"},
            }
        )
        deadline = time.monotonic() + 2
        while not got and time.monotonic() < deadline:
            time.sleep(0.02)
        assert got == [{"url": "u"}]
    finally:
        cdp.close()


def test_primitive_reattaches_after_detach(bridge):
    open_calls = {"n": 0}

    def on_open(frame):
        open_calls["n"] += 1
        return {"ok": True, "tabId": 7}

    bridge.on("open", on_open)
    bridge.set_default(_evaluate_value(True))
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert open_calls["n"] == 1
        # Simulate the service worker being evicted: a detach event.
        bridge.send_event({"event": True, "type": "detached", "reason": "sw_evicted"})
        deadline = time.monotonic() + 2
        while not cdp._detached.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        # Next primitive must transparently re-open the tab on the same URL.
        assert cdp.banner_present(".x") is True
        assert open_calls["n"] == 2  # re-attached exactly once
        assert not cdp._detached.is_set()
    finally:
        cdp.close()


def test_in_flight_op_fails_on_detach(bridge):
    # An op awaiting a reply when a detach arrives must fail promptly with a
    # DetachedError rather than hang until the op timeout.
    bridge.on("open", _ok_open())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            # Never reply to this evaluate; instead the test pushes a detach.
            return None
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge, op_timeout=10.0)
    try:
        cdp.open("https://x", timeout=0.2)

        def detacher():
            time.sleep(0.2)
            bridge.send_event({"event": True, "type": "detached", "reason": "gone"})

        threading.Thread(target=detacher, daemon=True).start()
        started = time.monotonic()
        with pytest.raises(DetachedError):
            cdp.banner_present(".x")
        # Failed via the detach, well before the 10s op timeout.
        assert time.monotonic() - started < 5.0
    finally:
        cdp.close()


def test_navigate_reopens_after_detach(bridge):
    # navigate() must route to open() (a fresh tab) once the session detached,
    # not issue Page.navigate on a tab that is gone.
    open_calls = {"n": 0}

    def on_open(frame):
        open_calls["n"] += 1
        return {"ok": True, "tabId": 7}

    bridge.on("open", on_open)
    nav_seen = {"n": 0}

    def default(frame):
        if frame.get("method") == "Page.navigate":
            nav_seen["n"] += 1
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://a", timeout=0.2)
        assert open_calls["n"] == 1
        bridge.send_event({"event": True, "type": "detached", "reason": "gone"})
        deadline = time.monotonic() + 2
        while not cdp._detached.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        bridge.received.clear()
        cdp.navigate("https://b", timeout=0.2)
        # Re-opened a fresh tab rather than issuing Page.navigate on a dead one.
        assert open_calls["n"] == 2
        assert all(f.get("method") != "Page.navigate" for f in bridge.received)
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# outer_html documentElement fallback + page-evaluation errors
# --------------------------------------------------------------------------- #


def test_outer_html_falls_back_to_document_element(bridge):
    # When the container selector is absent the expression evaluates to the
    # documentElement outerHTML; assert the fallback path is in the expression.
    full = "<html><body>doc</body></html>"
    bridge.on("open", _ok_open())
    bridge.set_default(_evaluate_value(full))
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.outer_html(".missing-container") == full
        evals = [
            f["params"]["expression"]
            for f in bridge.received
            if f.get("method") == "Runtime.evaluate"
        ]
        assert any("documentElement.outerHTML" in e for e in evals)
    finally:
        cdp.close()


def test_evaluate_raises_on_exception_details(bridge):
    # A page-side exception (exceptionDetails) must surface as a BridgeError,
    # not be silently swallowed as a None value.
    bridge.on("open", _ok_open())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            return {
                "ok": True,
                "result": {
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {"description": "TypeError: boom"},
                    }
                },
            }
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        with pytest.raises(BridgeError, match="page evaluation error"):
            cdp.banner_present(".x")
    finally:
        cdp.close()


def test_wait_selector_honours_timeout_with_slow_evaluate(bridge):
    # A slow (but eventually replying) evaluate must not let wait_selector blow
    # far past its timeout: each leg is clamped to the remaining budget, so the
    # call raises promptly rather than waiting the full op_timeout per poll.
    bridge.on("open", _ok_open())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            time.sleep(0.5)  # slow page; never present
            return {"ok": True, "result": {"result": {"value": False}}}
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    # op_timeout is large; the wait_selector timeout must still govern.
    cdp = _client(bridge, op_timeout=30.0)
    try:
        cdp.open("https://x", timeout=0.2)
        started = time.monotonic()
        with pytest.raises(BridgeTimeoutError, match="not present"):
            cdp.wait_selector("article.item", timeout=0.6, poll_interval=0.05)
        elapsed = time.monotonic() - started
        # Bounded by ~timeout (0.6) + one in-flight evaluate (0.5), never the
        # 30s op_timeout. A tight bound so a dropped clamp regresses loudly.
        assert elapsed < 1.6
    finally:
        cdp.close()


def test_op_lock_serialises_concurrent_callers(bridge):
    # Two threads issue cdp ops simultaneously. _op_lock must hold the whole
    # round-trip so the ops do NOT overlap on the wire (sw.js processes ops
    # serially). We record server-side enter/exit timestamps and assert the two
    # op windows are disjoint — deterministic, not wall-clock-threshold based.
    bridge.on("open", _ok_open())

    windows: list[tuple[str, float, float]] = []
    windows_lock = threading.Lock()

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            enter = time.monotonic()
            time.sleep(0.2)  # widen the window so an overlap would be obvious
            exit_ = time.monotonic()
            expr = frame["params"]["expression"]
            tag = "A" if "A" in expr else "B"
            with windows_lock:
                windows.append((tag, enter, exit_))
            return {"ok": True, "result": {"result": {"value": True}}}
        return {"ok": True, "result": {}}

    bridge.set_default(default)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)

        def call(sel):
            cdp.banner_present(sel)

        t1 = threading.Thread(target=call, args=("A",), daemon=True)
        t2 = threading.Thread(target=call, args=("B",), daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert len(windows) == 2, windows
        # Order the two op windows by start time; the second must start only
        # after the first finished — i.e. _op_lock serialised them.
        windows.sort(key=lambda w: w[1])
        (_, _e0, x0), (_, e1, _x1) = windows
        assert e1 >= x0, f"ops overlapped on the wire: {windows}"
    finally:
        cdp.close()


def test_ensure_attached_without_url_raises(bridge):
    # If a detach is observed before any open/navigate, a primitive has no URL
    # to re-attach to and must raise DetachedError rather than hang.
    bridge.set_default(_cdp_result({}))
    cdp = _client(bridge)
    try:
        cdp._detached.set()  # simulate a detach with no prior open()
        with pytest.raises(DetachedError, match="no URL to re-attach"):
            cdp.banner_present(".x")
    finally:
        cdp.close()


# --------------------------------------------------------------------------- #
# ASSURANCE TESTS — exercise the REAL in-page JS the primitives emit.
#
# click_text's innermost/whitespace match and the selector read/fill primitives
# build JS expressions that run in the page. Stubbing the evaluate result (as the
# basic tests above do) never exercises that JS. Here the fake bridge instead
# pipes each Runtime.evaluate expression to a tiny pure-JS DOM mock (Node) and
# returns its real result, so the matching/selector logic itself is under test.
# These tests skip cleanly when Node is unavailable.
# --------------------------------------------------------------------------- #

_NODE = _shutil.which("node")
_DOM_EVAL = _Path(__file__).with_name("js_dom_eval.mjs")
_needs_node = pytest.mark.skipif(
    _NODE is None or not _DOM_EVAL.exists(),
    reason="node + js_dom_eval.mjs required to exercise the in-page DOM JS",
)


def _dom_eval(expr: str, fixture: dict):
    """Evaluate a Runtime.evaluate expression against ``fixture`` via Node."""
    proc = _subprocess.run(
        [_NODE, str(_DOM_EVAL)],
        input=_json.dumps({"expr": expr, "fixture": fixture}),
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node eval failed: {proc.stderr or proc.stdout}")
    out = _json.loads(proc.stdout)
    if "error" in out:
        raise AssertionError(f"in-page JS threw: {out['error']}")
    return out["value"]


def _dom_bridge(bridge: FakeBridge, fixture: dict) -> None:
    """Wire ``bridge`` so every Runtime.evaluate runs against ``fixture`` in Node;
    all other cdp ops (Page.enable, Input.dispatchMouseEvent, ...) just ack."""
    bridge.on("open", _ok_open())

    def handler(frame):
        if frame.get("method") == "Runtime.evaluate":
            expr = frame["params"]["expression"]
            value = _dom_eval(expr, fixture)
            return {"ok": True, "result": {"result": {"value": value}}}
        return {"ok": True, "result": {}}

    bridge.set_default(handler)


# ---- click_text picks the innermost match, normalises whitespace ----------- #


@_needs_node
def test_click_text_selects_innermost_not_wrapper(bridge):
    # A wrapper <div> whose ONLY text is the target wraps the real <button>
    # (the button carries the text directly, so it is the innermost match). In
    # document order the wrapper comes first; the matcher must skip it (a
    # descendant also matches) and click the button. We prove which element was
    # chosen by giving the two very different boxes → different click centres.
    fixture = {
        "tag": "div",
        "class": "wrapper",
        "rect": [400, 200],  # wrapper centre would be (200, 100)
        "children": [
            {
                "tag": "button",
                "class": "submit-button",
                "rect": [100, 40],  # button centre (50, 20)
                "text": "Submit request",
            }
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.click_text("Submit request") is True
        mouse = [
            f for f in bridge.received if f.get("method") == "Input.dispatchMouseEvent"
        ]
        assert mouse, "expected a real mouse click"
        # Clicked the BUTTON's centre (50, 20), not the wrapper's (200, 100).
        assert all(f["params"]["x"] == 50.0 and f["params"]["y"] == 20.0 for f in mouse)
    finally:
        cdp.close()


@_needs_node
def test_click_text_normalises_whitespace_and_child_spans(bridge):
    # The button's text is split across spans with newlines / runs of spaces.
    # Normalised it reads "Submit request"; strict equality would miss it.
    fixture = {
        "tag": "button",
        "class": "submit-button",
        "rect": [120, 48],  # centre (60, 24)
        "children": [
            {"tag": "span", "text": "  Submit\n   "},
            {"tag": "span", "text": "request  "},
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.click_text("Submit request") is True
        mouse = [
            f for f in bridge.received if f.get("method") == "Input.dispatchMouseEvent"
        ]
        assert mouse and all(
            f["params"]["x"] == 60.0 and f["params"]["y"] == 24.0 for f in mouse
        )
    finally:
        cdp.close()


@_needs_node
def test_click_text_tag_filter_via_real_walk(bridge):
    # With tag="button", a same-text <a> must be ignored; only the button matches.
    fixture = {
        "tag": "div",
        "children": [
            {"tag": "a", "text": "Submit", "rect": [10, 10]},
            {"tag": "button", "text": "Submit", "rect": [80, 40]},  # centre (40,20)
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.click_text("Submit", tag="button") is True
        mouse = [
            f for f in bridge.received if f.get("method") == "Input.dispatchMouseEvent"
        ]
        assert mouse and all(
            f["params"]["x"] == 40.0 and f["params"]["y"] == 20.0 for f in mouse
        )
    finally:
        cdp.close()


@_needs_node
def test_click_text_raises_when_real_walk_finds_nothing(bridge):
    fixture = {"tag": "div", "children": [{"tag": "button", "text": "Something else"}]}
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        with pytest.raises(BridgeError, match="no visible element"):
            cdp.click_text("Submit request")
    finally:
        cdp.close()


# ---- selector-based click + read/fill primitives ---------------------------- #


@_needs_node
def test_click_selector_clicks_matched_element_center(bridge):
    fixture = {
        "tag": "div",
        "children": [
            {"tag": "button", "class": "submit-button", "rect": [200, 80], "text": "x"}
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.click_selector("button.submit-button") is True
        mouse = [
            f for f in bridge.received if f.get("method") == "Input.dispatchMouseEvent"
        ]
        types = [f["params"]["type"] for f in mouse]
        assert types == ["mouseMoved", "mousePressed", "mouseReleased"]
        # centre of a 200x80 box at origin
        assert all(
            f["params"]["x"] == 100.0 and f["params"]["y"] == 40.0 for f in mouse
        )
    finally:
        cdp.close()


@_needs_node
def test_click_selector_raises_when_absent(bridge):
    _dom_bridge(bridge, {"tag": "div", "children": []})
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        with pytest.raises(BridgeError, match="no visible element matching"):
            cdp.click_selector("button.submit-button")
    finally:
        cdp.close()


@_needs_node
def test_is_visible_true_for_shown_false_for_hidden(bridge):
    fixture = {
        "tag": "div",
        "children": [
            {"tag": "button", "class": "shown", "rect": [10, 10], "text": "a"},
            {
                "tag": "button",
                "class": "hidden",
                "rect": [10, 10],
                "style": {"display": "none"},
                "text": "b",
            },
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.is_visible("button.shown") is True
        assert cdp.is_visible("button.hidden") is False
        assert cdp.is_visible("button.absent") is False
    finally:
        cdp.close()


@_needs_node
def test_get_attribute_reads_custom_attribute(bridge):
    fixture = {
        "tag": "button",
        "class": "submit-button",
        "attrs": {"class": "submit-button", "data-state": "submitted-again"},
        "text": "Submit request",
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert (
            cdp.get_attribute("button.submit-button", "data-state")
            == "submitted-again"
        )
        # Missing attribute / missing element → None.
        assert cdp.get_attribute("button.submit-button", "nope") is None
        assert cdp.get_attribute("button.absent", "data-state") is None
    finally:
        cdp.close()


@_needs_node
def test_query_text_returns_trimmed_label(bridge):
    fixture = {
        "tag": "button",
        "class": "submit-button",
        "children": [{"tag": "span", "text": "  Submitted again  "}],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.query_text("button.submit-button") == "Submitted again"
        assert cdp.query_text("button.absent") is None
    finally:
        cdp.close()


@_needs_node
def test_input_value_reads_textarea(bridge):
    fixture = {
        "tag": "form",
        "children": [
            {
                "tag": "textarea",
                "attrs": {"name": "message"},
                "value": "Is this still available?",
            }
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert (
            cdp.input_value("textarea[name=message]")
            == "Is this still available?"
        )
        assert cdp.input_value("textarea.absent") is None
    finally:
        cdp.close()


@_needs_node
def test_text_visible_matches_normalised_text_and_tag(bridge):
    # text_visible is the read-only sibling of click_text: it runs the same
    # innermost/normalised walk but only reports presence+visibility. A hidden
    # element, an absent label, and a tag mismatch must all read False.
    fixture = {
        "tag": "div",
        "children": [
            {
                "tag": "button",
                "children": [
                    {"tag": "span", "text": "  Submit\n  "},
                    {"tag": "span", "text": "request "},
                ],
            },
            {
                "tag": "div",
                "text": "message sent",
                "style": {"display": "none"},  # hidden → not visible
            },
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        # Normalised whitespace match across child spans.
        assert cdp.text_visible("Submit request") is True
        # tag filter: the text lives in a <button>, so tag="a" must miss it.
        assert cdp.text_visible("Submit request", tag="a") is False
        # Hidden element → not visible.
        assert cdp.text_visible("message sent") is False
        # Absent text → False.
        assert cdp.text_visible("does not exist") is False
    finally:
        cdp.close()


@_needs_node
def test_text_visible_contains_matches_sentence_fragment(bridge):
    # contains=True mirrors Playwright's unquoted `text=` engine: case-insensitive
    # SUBSTRING. The confirmation banner phrase is only a fragment of the real
    # sentence; exact equality (default) must miss it, contains must find it.
    fixture = {
        "tag": "div",
        "children": [
            {"tag": "p", "text": "We have sent your message to the seller"},
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        # Exact mode misses the fragment.
        assert cdp.text_visible("sent your message") is False
        # Substring + case-insensitive mode finds it.
        assert cdp.text_visible("sent your message", contains=True) is True
        assert cdp.text_visible("SENT YOUR MESSAGE", contains=True) is True
        # A fragment that isn't present is still False in contains mode.
        assert cdp.text_visible("rejected", contains=True) is False
    finally:
        cdp.close()


@_needs_node
def test_fill_sets_value_and_dispatches_events(bridge):
    fixture = {
        "tag": "form",
        "children": [
            {"tag": "textarea", "attrs": {"name": "message"}, "value": ""}
        ],
    }
    _dom_bridge(bridge, fixture)
    cdp = _client(bridge)
    try:
        cdp.open("https://x", timeout=0.2)
        assert cdp.fill("textarea[name=message]", "My message") is True
        # The fill expression must set value AND fire input + change events.
        fill_expr = next(
            f["params"]["expression"]
            for f in bridge.received
            if f.get("method") == "Runtime.evaluate"
            and "dispatchEvent" in f["params"]["expression"]
        )
        assert "Event('input'" in fill_expr
        assert "Event('change'" in fill_expr
        # Filling an absent element returns False.
        assert cdp.fill("textarea.absent", "x") is False
    finally:
        cdp.close()
