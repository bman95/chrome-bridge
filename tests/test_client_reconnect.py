"""Reconnect + idempotent-resume tests for the bridge client.

A script that reuses one module-level :class:`ChromeBridge` across calls must
survive an MV3 service-worker eviction: when the worker is evicted, the native
port Chrome spawned drops, the host's relay ends, and the script's loopback
socket dies. A long-lived owner that reuses one client must then transparently
re-dial the host (which Chrome respawns as soon as ``sw.js`` re-runs
``connect()`` and re-binds the port) and re-attach to the last URL — with zero
human interaction.

These drive :class:`ChromeBridge` against a *reconnectable* fake bridge on a
real loopback socket. The fake plays ``nmh.py`` + ``sw.js``: it can drop the
current client connection (the worker eviction) and accept a fresh one on the
SAME port (the respawned host), so the tests assert the client re-dials and
resumes idempotently — re-``open`` on the last URL, fresh ``Page.enable`` —
without the caller discarding it. No Chrome, no native host, no running script.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from chrome_bridge import profiles
from chrome_bridge.bridge.frames import read_frame, write_frame
from chrome_bridge.bridge.client import BridgeError, ChromeBridge


class ReconnectableBridge:
    """A loopback server whose client connection can be dropped and re-accepted.

    Unlike the one-shot ``FakeBridge`` in ``test_client.py``, this keeps its
    listener bound across reconnects so a :class:`ChromeBridge` can re-dial the
    SAME host:port after the current connection is severed — exactly the
    Chrome-respawns-the-native-host shape after a service-worker eviction.

    ``drop()`` severs the current client connection (the worker eviction). The
    accept loop then blocks for the next client (the respawned host binding the
    port again). ``opens`` counts ``op:open`` frames so a test can assert how
    many times the client re-attached.
    """

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.host, self.port = self._listener.getsockname()
        # Token the client presents as its first frame on every (re)connect; the
        # `rbridge` fixture redirects the config dir to a tmp dir first.
        self.token = os.urandom(16).hex()
        token_file = profiles.port_token_path(self.port)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(self.token, encoding="utf-8")
        self._client: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.received: list[dict] = []
        self.opens = 0
        self._connections = 0
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    # ---- lifecycle ------------------------------------------------------ #

    def start(self) -> None:
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                break
            with self._lock:
                self._client = client
                self._rfile = client.makefile("rb")
                self._wfile = client.makefile("wb")
                self._connections += 1
            self._connected.set()
            self._serve(client)

    def _serve(self, client: socket.socket) -> None:
        rfile = client.makefile("rb")
        # Consume the auth handshake the client sends first on this connection
        # (the real host validates + swallows it and never relays it); the
        # client re-sends it on every reconnect, so swallow it per connection.
        try:
            read_frame(rfile)
        except (OSError, ValueError):
            return
        try:
            while not self._stop.is_set():
                try:
                    frame = read_frame(rfile)
                except (OSError, ValueError):
                    break
                if frame is None:
                    break
                self.received.append(frame)
                op = frame.get("op")
                if op == "open":
                    with self._lock:
                        self.opens += 1
                    self._reply(frame.get("id"), {"ok": True, "tabId": 7})
                elif op == "cdp":
                    # Default: ack with an empty result; Runtime.evaluate gets a
                    # truthy value so banner_present / wait_selector resolve.
                    if frame.get("method") == "Runtime.evaluate":
                        self._reply(
                            frame.get("id"),
                            {"ok": True, "result": {"result": {"value": True}}},
                        )
                    else:
                        self._reply(frame.get("id"), {"ok": True, "result": {}})
                elif op == "close":
                    self._reply(frame.get("id"), {"ok": True})
                else:
                    self._reply(
                        frame.get("id"), {"ok": False, "error": f"bad op {op}"}
                    )
        finally:
            with self._lock:
                if self._client is client:
                    self._connected.clear()

    def _reply(self, msg_id, payload: dict) -> None:
        with self._lock:
            wfile = self._wfile
        if wfile is not None:
            try:
                write_frame(wfile, {"id": msg_id, **payload})
            except OSError:
                pass

    # ---- test controls -------------------------------------------------- #

    def wait_connected(self, timeout: float = 5.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def connections(self) -> int:
        with self._lock:
            return self._connections

    def drop(self) -> None:
        """Sever the current client connection (simulate the worker eviction)."""
        with self._lock:
            client = self._client
            self._client = None
            self._rfile = self._wfile = None
        self._connected.clear()
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        self.drop()
        try:
            self._listener.close()
        except OSError:
            pass


@pytest.fixture
def rbridge(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "default_registry_path", lambda: tmp_path / "profiles.json")
    b = ReconnectableBridge()
    b.start()
    yield b
    b.close()


def _client(bridge: ReconnectableBridge, **kw) -> ChromeBridge:
    c = ChromeBridge(host=bridge.host, port=bridge.port, **kw)
    c.connect()
    return c


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)


# --------------------------------------------------------------------------- #
# Socket death (worker eviction) -> reconnect + idempotent re-attach
# --------------------------------------------------------------------------- #


def test_reconnect_redials_and_reattaches_on_last_url(rbridge):
    cdp = _client(rbridge)
    try:
        cdp.open("https://example.com/list", timeout=0.3)
        assert rbridge.wait_connected()
        assert rbridge.opens == 1
        first_url = cdp._current_url

        # Worker evicted: the native port drops, our loopback socket dies.
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())
        assert not cdp.is_alive()

        # reconnect() re-dials the respawned host and re-opens the same URL.
        cdp.reconnect()
        assert rbridge.wait_connected()
        assert cdp.is_alive()
        assert cdp._current_url == first_url
        assert cdp._tab_id is not None
        assert not cdp._detached.is_set()
        assert rbridge.opens == 2  # re-attached exactly once
        assert rbridge.connections() == 2
    finally:
        cdp.close()


def test_primitive_auto_reconnects_after_socket_death(rbridge):
    # The core acceptance path: after a worker eviction (socket death), the NEXT
    # primitive transparently rebuilds the connection and resumes — no human
    # click, and the caller never has to discard the client.
    cdp = _client(rbridge)
    try:
        cdp.open("https://example.com/list", timeout=0.3)
        assert rbridge.opens == 1
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        # banner_present() -> _ensure_attached() must reconnect first, then run.
        assert cdp.banner_present("article.item") is True
        assert cdp.is_alive()
        assert rbridge.opens == 2  # re-attached on the same URL
    finally:
        cdp.close()


def test_navigate_reconnects_after_socket_death(rbridge):
    # navigate() over a dead socket must rebuild the connection and land on the
    # requested URL, not raise "bridge is not connected".
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        assert rbridge.opens == 1
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        cdp.navigate("https://b", timeout=0.3)
        assert cdp.is_alive()
        assert cdp._current_url == "https://b"
        # Re-attach goes through op:open (not Page.navigate on a dead tab).
        assert rbridge.opens == 2
    finally:
        cdp.close()


def test_reconnect_without_url_raises(rbridge):
    # A socket death before any open()/navigate() has no URL to resume, so a
    # primitive must surface the loss rather than silently reconnect unattached.
    cdp = _client(rbridge)
    try:
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())
        with pytest.raises(BridgeError, match="no URL to re-attach"):
            cdp.banner_present(".x")
    finally:
        cdp.close()


def test_reconnect_raises_when_host_stays_down():
    # If the respawned host never comes back (nothing listening), reconnect must
    # raise a clear BridgeError the caller surfaces for this job — the script
    # then retries on the next job.
    # Bind+free a port so nothing is listening on it.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    host, port = s.getsockname()
    s.close()

    cdp = ChromeBridge(host=host, port=port, connect_timeout=0.3)
    # Simulate a previously-open session whose socket then died.
    cdp._current_url = "https://example.com/x"
    with pytest.raises(BridgeError, match="could not connect"):
        cdp.reconnect()


def test_stale_reader_does_not_poison_fresh_connection(rbridge):
    # After a reconnect, the OLD socket's reader daemon may still be unwinding.
    # When it finishes it must NOT mark the fresh connection closed or fail its
    # pending ops (generation guard). We reconnect, then drive an op that relies
    # on the fresh connection staying alive while the stale reader exits.
    cdp = _client(rbridge)
    try:
        cdp.open("https://example.com/list", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())
        cdp.reconnect()
        assert cdp.is_alive()
        # Give any stale daemon reader time to unwind and (wrongly) tear down.
        time.sleep(0.3)
        assert cdp.is_alive(), "stale reader wrongly closed the fresh connection"
        # The fresh connection still serves ops.
        assert cdp.banner_present("article.item") is True
    finally:
        cdp.close()


def test_reconnect_preserves_one_connection_across_jobs(rbridge):
    # Models a script reusing ONE client across a read job and a follow-up job
    # with a worker eviction in between: the client heals itself, so the second
    # job runs over the same ChromeBridge rather than a fresh one.
    cdp = _client(rbridge)
    try:
        # Job 1: open + read.
        cdp.open("https://example.com/list", timeout=0.3)
        assert cdp.wait_selector("article.item", timeout=1.0) is True

        # Worker evicted between jobs.
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        # Job 2: navigate to a detail page on the SAME client.
        cdp.navigate("https://example.com/item/1/", timeout=0.3)
        assert cdp.banner_present("button.submit-button") is True
        assert cdp.is_alive()
        assert cdp._current_url == "https://example.com/item/1/"
    finally:
        cdp.close()


def test_reattach_targets_the_last_url_not_an_earlier_one(rbridge):
    # Re-attach must resume on the MOST RECENT URL, not a stale earlier one: a
    # list page followed by a navigate to a detail page, then an eviction, must
    # re-open the DETAIL page. Guards against a regression where reconnect
    # re-attaches to whatever URL was opened first.
    cdp = _client(rbridge)
    try:
        cdp.open("https://example.com/list", timeout=0.3)
        cdp.navigate("https://example.com/item/42/", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        cdp.banner_present(".x")  # triggers the transparent re-attach
        opens = [f for f in rbridge.received if f.get("op") == "open"]
        assert opens, "expected a re-attach op:open after the eviction"
        # The re-attach op:open carried the LAST url, not the first.
        assert opens[-1]["url"] == "https://example.com/item/42/"
    finally:
        cdp.close()


def test_stale_reader_teardown_is_inert_against_fresh_generation():
    # DETERMINISTIC generation-guard test (no timing). Directly drive the reader
    # teardown with a STALE generation while the connection is "fresh" (alive,
    # with a pending op): it must NOT mark the connection closed and must NOT fail
    # the pending future. This pins the exact invariant the guard protects — a
    # regression that drops the generation check in _reader_teardown fails here
    # without relying on a race window being hit.
    from concurrent.futures import Future

    cdp = ChromeBridge(host="127.0.0.1", port=1)  # never connected; we poke internals
    # Simulate a fresh connection at generation 5 with one op in flight.
    cdp._generation = 5
    cdp._closed.clear()
    fut: Future = Future()
    cdp._pending[1] = fut

    # A stale reader (generation 4) finishing late must be a no-op.
    cdp._reader_teardown(generation=4)
    assert not cdp._closed.is_set(), "stale teardown wrongly closed the connection"
    assert not fut.done(), "stale teardown wrongly failed a fresh pending op"

    # The CURRENT generation's reader teardown DOES tear down + fail pending.
    cdp._reader_teardown(generation=5)
    assert cdp._closed.is_set()
    assert fut.done()
    with pytest.raises(BridgeError, match="reader stopped"):
        fut.result()
