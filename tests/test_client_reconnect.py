"""Reconnect + idempotent-resume tests for the bridge client.

A script that reuses one module-level :class:`ChromeBridge` across calls must
survive an MV3 service-worker eviction: when the worker is evicted, the native
port Chrome spawned drops, the host's relay ends, and the script's loopback
socket dies. A long-lived owner that reuses one client must then transparently
re-dial the host (which Chrome respawns as soon as ``sw.js`` re-runs
``connect()`` and re-binds the port) and re-attach EVERY tab on its last URL,
in creation order, remapping tab ids onto the existing handles — with zero
human interaction.

These drive :class:`ChromeBridge` against a *reconnectable* fake bridge on a
real loopback socket. The fake plays ``nmh.py`` + ``sw.js``: it can drop the
current client connection (the worker eviction) and accept a fresh one on the
SAME port (the respawned host), so the tests assert the client re-dials and
resumes idempotently — re-``open`` per tab on its last URL, fresh
``Page.enable`` — without the caller discarding it. No Chrome, no native
host, no running script.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from chrome_bridge import profiles
from chrome_bridge.bridge.frames import read_frame, write_frame
from chrome_bridge.bridge.client import (
    DEFAULT_REATTACH_TIMEOUT,
    BridgeError,
    ChromeBridge,
    DetachedError,
)


# Sentinel an ``open_hook`` returns to send NO reply at all — simulating a
# wedged worker whose ops hang until the client's own timeout fires.
DROP = object()


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
        # Optional failure injection: a callable(frame) -> reply payload dict
        # to send INSTEAD of the default ok:true open reply, DROP to send no
        # reply at all (wedged worker), or None to fall through to the
        # default. Lets tests fail or hang one specific re-open.
        self.open_hook = None
        # Same shape for "cdp" ops (e.g. DROP a Page.enable to wedge the
        # second heal leg).
        self.cdp_hook = None
        # Push Page.loadEventFired after acking each tagged Page.enable so
        # opens/heals don't sleep out their full load wait. Tests that
        # measure the silent-load ceiling set this to False.
        self.emit_load_events = True
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
        # ENFORCE the auth handshake like the real host: the first frame on
        # every connection must be this bridge's token, or the client gets no
        # service (its ops would time out and the test fails loudly). This
        # makes the reconnect tests prove the token is re-sent on every
        # re-dial, not merely that some first frame was swallowed.
        try:
            first = read_frame(rfile)
        except (OSError, ValueError):
            return
        if first != {"auth": self.token}:
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
                    override = self.open_hook(frame) if self.open_hook else None
                    if override is DROP:
                        continue  # wedged worker: the op is never answered
                    if override is not None:
                        self._reply(frame.get("id"), override)
                        continue
                    # Sequential tab ids so multi-tab tests can tell handles
                    # apart (no test depends on a specific id value).
                    with self._lock:
                        self.opens += 1
                        tab_id = 100 + self.opens
                    self._reply(frame.get("id"), {"ok": True, "tabId": tab_id})
                elif op == "cdp":
                    override = self.cdp_hook(frame) if self.cdp_hook else None
                    if override is DROP:
                        continue  # wedged worker: the op is never answered
                    if override is not None:
                        self._reply(frame.get("id"), override)
                        continue
                    # Default: ack with an empty result; Runtime.evaluate gets a
                    # truthy value so banner_present / wait_selector resolve.
                    if frame.get("method") == "Runtime.evaluate":
                        self._reply(
                            frame.get("id"),
                            {"ok": True, "result": {"result": {"value": True}}},
                        )
                    else:
                        self._reply(frame.get("id"), {"ok": True, "result": {}})
                        if (
                            self.emit_load_events
                            and frame.get("method") == "Page.enable"
                            and frame.get("tabId") is not None
                        ):
                            # By the time the client sends Page.enable its
                            # tab_id is published, so the tagged load event
                            # routes to the right handle deterministically.
                            self.send_event(
                                {"event": True, "type": "cdp",
                                 "method": "Page.loadEventFired",
                                 "params": {}, "tabId": frame["tabId"]}
                            )
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
        # Write UNDER the lock so serve-thread replies and test-thread
        # send_event calls can never interleave frame bytes.
        with self._lock:
            if self._wfile is not None:
                try:
                    write_frame(self._wfile, {"id": msg_id, **payload})
                except OSError:
                    pass

    # ---- test controls -------------------------------------------------- #

    def send_event(self, event: dict) -> None:
        """Push an unsolicited event frame to the CURRENT client connection."""
        self._connected.wait(timeout=5)
        with self._lock:
            if self._wfile is not None:
                try:
                    write_frame(self._wfile, event)
                except OSError:
                    pass

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
def rbridge():
    # conftest.py's autouse fixture isolates the config dir before construction.
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


def test_connect_redials_after_peer_eof_without_explicit_reconnect(rbridge):
    cdp = _client(rbridge)
    try:
        cdp.open("https://example.com/before-eof", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        cdp.connect()

        assert cdp.is_alive()
        assert rbridge.connections() == 2
        assert rbridge.opens == 2
        assert cdp.banner_present("body") is True
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


def test_reconnect_heals_default_and_additional_tabs(rbridge):
    # A socket death with BOTH the default tab and a new_tab handle open:
    # touching the additional tab must rebuild the connection, which re-opens
    # BOTH tabs in creation order — the default on its last URL (key-less
    # reaper shape), then the additional tab with replace:false so the healed
    # default is not torn down.
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        assert tab_b.banner_present(".x") is True
        opens = [f for f in rbridge.received if f.get("op") == "open"]
        assert [f["url"] for f in opens] == ["https://a", "https://b"]
        assert "replace" not in opens[0]      # default tab: legacy reaper shape
        assert opens[1]["replace"] is False   # additional tab: never reaps
        assert cdp._tab_id is not None and tab_b.tab_id is not None
        assert cdp._tab_id != tab_b.tab_id
        # The default facade resumed too.
        assert cdp.banner_present(".y") is True
    finally:
        cdp.close()


def test_concurrent_dead_socket_recovery_reconnects_once(rbridge):
    # One thread per tab is the supported pattern, so N threads can observe
    # the same dead socket at once. The transparent heal must coalesce: one
    # thread rebuilds the connection (re-opening every tab); the losers must
    # find the winner's live connection and SKIP — not tear down the fresh
    # socket and heal every tab again (destroying the winner's fresh tabs
    # via the reaper re-open). Pin: exactly one re-dial, one re-open per tab.
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        connections_before = rbridge.connections()
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        opens_before = rbridge.opens
        start = threading.Barrier(2)
        results: dict = {}

        def drive(key, fn):
            start.wait(timeout=5)
            results[key] = fn()

        t1 = threading.Thread(
            target=drive, args=("a", lambda: cdp.banner_present(".x")), daemon=True
        )
        t2 = threading.Thread(
            target=drive, args=("b", lambda: tab_b.banner_present(".x")), daemon=True
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive()
        assert results.get("a") is True and results.get("b") is True
        # Exactly ONE rebuild: one fresh connection, one re-open per tab.
        assert rbridge.connections() - connections_before == 1
        assert rbridge.opens - opens_before == 2
        assert cdp._tab_id is not None and tab_b.tab_id is not None
    finally:
        cdp.close()


def test_reconnect_if_dead_reports_who_rebuilt(rbridge):
    # navigate()'s dead-socket branch keys its "already on the right page"
    # early return off WHO rebuilt: True only when this call ran the rebuild
    # (its heal then read the current_url the caller just wrote); a False
    # (coalesced loser, or the connection was never dead) must fall through
    # to an in-place navigate. Pin the return contract.
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        # Alive: no rebuild happened, and none may be reported.
        connections_before = rbridge.connections()
        assert cdp._reconnect_if_dead() is False
        assert rbridge.connections() == connections_before

        rbridge.drop()
        _wait(lambda: not cdp.is_alive())
        # Dead: this call must run (and report) the rebuild.
        assert cdp._reconnect_if_dead() is True
        assert rbridge.connections() == connections_before + 1
    finally:
        cdp.close()


def test_navigate_on_additional_tab_reconnects_after_socket_death(rbridge):
    # navigate() on a new_tab handle over a dead socket must rebuild the
    # connection, which re-opens the default tab on ITS last URL and then
    # THIS tab on the requested URL (recorded before the reconnect) with
    # replace:false.
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        tab_b.navigate("https://b2", timeout=0.3)
        assert cdp.is_alive()
        opens = [f for f in rbridge.received if f.get("op") == "open"]
        assert [f["url"] for f in opens] == ["https://a", "https://b2"]
        assert "replace" not in opens[0]      # default tab: legacy reaper shape
        assert opens[1]["replace"] is False   # additional tab: never reaps
        assert tab_b.current_url == "https://b2"
        assert tab_b.tab_id is not None and tab_b.tab_id != cdp._tab_id
    finally:
        cdp.close()


def test_reconnect_new_tab_only_session_reaps_on_first_reopen(rbridge):
    # A session that only ever used new_tab (default tab never opened) must
    # still reap the dead session's leftover tabs after a socket death: with
    # nothing else attached, the first re-open keeps the key-less wire-default
    # replace:true — the designated reaper.
    cdp = _client(rbridge)
    try:
        tab = cdp.new_tab("https://b", timeout=0.3)
        first_id = tab.tab_id
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        assert tab.banner_present(".x") is True
        opens = [f for f in rbridge.received if f.get("op") == "open"]
        assert len(opens) == 1                # no default-tab URL to resume
        assert opens[0]["url"] == "https://b"
        assert "replace" not in opens[0]      # the reaper shape
        assert tab.tab_id is not None and tab.tab_id != first_id
    finally:
        cdp.close()


def test_reconnect_restores_all_tabs_in_creation_order(rbridge):
    # An explicit reconnect() after a socket death must EAGERLY re-open every
    # tab that had a URL — the default tab first, then new_tab handles in
    # creation order — remapping each handle's tab_id onto its fresh Chrome
    # tab so existing Tab references keep working.
    # Silent-load mode: no Page.loadEventFired, so each heal waits out its
    # full load ceiling — this test MEASURES that ceiling below.
    rbridge.emit_load_events = False
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        old_default_id, old_b_id = cdp._tab_id, tab_b.tab_id

        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        started = time.monotonic()
        cdp.reconnect()
        elapsed = time.monotonic() - started
        # Criterion: the eager heal keeps the SHORT per-tab load-wait ceiling.
        # The fake never sends Page.loadEventFired, so each re-open waits its
        # full load timeout — two tabs must cost ~2x DEFAULT_REATTACH_TIMEOUT,
        # never the 30s nav timeout per tab.
        assert elapsed < 2 * DEFAULT_REATTACH_TIMEOUT + 5.0

        # Both tabs re-opened inside reconnect() itself, oldest first.
        opens = [f for f in rbridge.received if f.get("op") == "open"]
        assert [f["url"] for f in opens] == ["https://a", "https://b"]
        assert "replace" not in opens[0]     # first re-open: the reaper shape
        assert opens[1]["replace"] is False  # later ones never reap siblings
        # Fresh tab ids were remapped onto the existing handles.
        assert cdp._tab_id is not None and cdp._tab_id != old_default_id
        assert tab_b.tab_id is not None and tab_b.tab_id != old_b_id
        assert cdp._tab_id != tab_b.tab_id
        # Both handles remain usable without any further re-open.
        assert cdp.banner_present(".x") is True
        assert tab_b.banner_present(".y") is True
        assert sum(1 for f in rbridge.received if f.get("op") == "open") == 2
    finally:
        cdp.close()


def test_reconnect_marks_urlless_default_tab_dead_until_reopened(rbridge):
    # A tab that never opened a URL has nothing to restore: after a socket
    # death + reconnect() it is marked dead — its next primitive raises an
    # actionable BridgeError (not a bare DetachedError) — while its siblings
    # are restored and usable. An explicit open() starts a fresh session on
    # the dead handle and revives it.
    cdp = _client(rbridge)
    try:
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        cdp.reconnect()
        # The new_tab handle was restored; the URL-less default tab is dead.
        assert tab_b.tab_id is not None
        with pytest.raises(BridgeError, match="no URL to restore") as ei:
            cdp.banner_present(".x")
        assert not isinstance(ei.value, DetachedError)
        # The restored sibling is untouched by the dead handle.
        assert tab_b.banner_present(".y") is True

        # A FAILED revival keeps the actionable dead error in place...
        armed = {"on": True}

        def open_hook(frame):
            if armed["on"]:
                armed["on"] = False
                return {"ok": False, "error": "tab creation failed"}
            return None

        rbridge.open_hook = open_hook
        with pytest.raises(BridgeError, match="open failed"):
            cdp.open("https://a", timeout=0.3)
        with pytest.raises(BridgeError, match="no URL to restore"):
            cdp.banner_present(".x")

        # ...and a successful open revives the handle with a fresh session.
        cdp.open("https://a", timeout=0.3)
        assert cdp.banner_present(".x") is True
    finally:
        cdp.close()


def test_stale_dead_mark_on_revived_handle_is_discarded(rbridge):
    # White-box guard: a dead mark that raced a concurrent revival (the
    # handle has a tab/URL again) must be discarded by the next primitive,
    # never fail a healthy handle.
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        cdp._default_tab._dead_reason = "stale mark from a racing reconnect"
        assert cdp.banner_present(".x") is True  # discarded, not raised
        assert cdp._default_tab._dead_reason is None
    finally:
        cdp.close()


def test_reconnect_wedged_worker_is_bounded_by_shared_budget(rbridge, monkeypatch):
    # The shared heal budget: against a wedged worker (open ops never
    # answered), reconnect() must return within roughly one op allowance plus
    # the small per-tab load ceiling — never N stacked op timeouts — and the
    # unhealed tabs must re-attach lazily once the worker recovers.
    from chrome_bridge.bridge import client as client_mod

    monkeypatch.setattr(client_mod, "DEFAULT_REATTACH_TIMEOUT", 0.1)
    cdp = _client(rbridge, op_timeout=1.0)
    try:
        cdp.open("https://a", timeout=0.3)
        cdp.new_tab("https://b", timeout=0.3)
        cdp.new_tab("https://c", timeout=0.3)
        tab_d = cdp.new_tab("https://d", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.open_hook = lambda frame: DROP  # wedged: opens never answered
        rbridge.received.clear()
        started = time.monotonic()
        cdp.reconnect()  # must not raise; heals time out / skip per tab
        elapsed = time.monotonic() - started
        # Budget = op_timeout (1.0) + 4 tabs x 0.1 = 1.4s (heals below the
        # minimal-share floor are skipped outright). Unbudgeted stacking
        # would burn >= one full op timeout PER tab (>= 4.0s).
        assert elapsed < 3.0, f"reconnect took {elapsed:.2f}s; budget not enforced"
        assert cdp._tab_id is None  # nothing healed against the wedged worker
        # The _MIN_HEAL_BUDGET floor: only the FIRST (doomed) open ever hits
        # the wire — after it burns the op allowance, the second tab has only
        # ~0.4s of budget left (< the 0.5s floor) and is skipped BEFORE
        # writing a frame, as are the rest. Without the floor, a second
        # near-zero-grant open frame would also be written and abandoned.
        opens_sent = sum(1 for f in rbridge.received if f.get("op") == "open")
        assert opens_sent == 1, f"expected 1 wedged open on the wire, got {opens_sent}"

        # Worker recovers: an unhealed tab re-attaches on its next primitive.
        rbridge.open_hook = None
        assert tab_d.banner_present(".x") is True
        assert tab_d.tab_id is not None
    finally:
        cdp.close()


def test_reconnect_budget_reclamps_each_heal_leg(rbridge, monkeypatch):
    # The per-leg re-clamp: a slow (but answered) open leg must SHRINK the
    # grant of the following Page.enable leg, so two op timeouts can never
    # stack within one tab's heal. Here open is answered only after 1.8s and
    # Page.enable is never answered: with per-leg re-clamping the enable leg
    # gets only the ~0.4s that remains of the 2.2s budget; a once-per-heal
    # clamp would grant it a fresh 2.0s (1.8 + 2.0 = 3.8s total).
    from chrome_bridge.bridge import client as client_mod

    monkeypatch.setattr(client_mod, "DEFAULT_REATTACH_TIMEOUT", 0.1)
    cdp = _client(rbridge, op_timeout=2.0)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        def slow_open(frame):
            time.sleep(1.8)
            return None  # then answer normally

        rbridge.open_hook = slow_open
        rbridge.cdp_hook = (
            lambda frame: DROP if frame.get("method") == "Page.enable" else None
        )
        started = time.monotonic()
        cdp.reconnect()  # must not raise
        elapsed = time.monotonic() - started
        # Budget = op_timeout (2.0) + 2 tabs x 0.1 = 2.2s; the stacked-clamp
        # regression costs >= 3.8s.
        assert elapsed < 3.0, f"reconnect took {elapsed:.2f}s; leg re-clamp broken"

        # Worker recovers: tab_b (skipped, still unattached) heals on its
        # next primitive.
        rbridge.open_hook = None
        rbridge.cdp_hook = None
        assert tab_b.tab_id is None
        assert tab_b.banner_present(".x") is True
        assert tab_b.tab_id is not None
    finally:
        cdp.close()


def test_reconnect_heal_failure_does_not_poison_siblings(rbridge):
    # Best-effort per tab: if one tab's eager re-open fails during
    # reconnect(), reconnect must not raise, the SIBLINGS must still be
    # restored, and the failed handle must heal transparently on its next
    # primitive.
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        armed = {"on": True}

        def open_hook(frame):
            if armed["on"] and frame.get("url") == "https://a":
                armed["on"] = False  # fail the default tab's heal exactly once
                return {"ok": False, "error": "tab creation failed"}
            return None

        rbridge.open_hook = open_hook
        cdp.reconnect()  # must NOT raise: the failure stays per-tab

        # The sibling healed despite the default tab's failed re-open...
        assert tab_b.tab_id is not None
        assert tab_b.banner_present(".y") is True
        # ...and the failed handle retries transparently on its next use.
        assert cdp._tab_id is None
        assert cdp.banner_present(".x") is True
        assert cdp._tab_id is not None
    finally:
        cdp.close()


def test_reconnect_order_skips_closed_tabs(rbridge):
    # Creation order must hold across a Tab.close(): default, B, C with B
    # closed before the socket death -> reconnect restores [default, C].
    cdp = _client(rbridge)
    try:
        cdp.open("https://a", timeout=0.3)
        tab_b = cdp.new_tab("https://b", timeout=0.3)
        tab_c = cdp.new_tab("https://c", timeout=0.3)
        tab_b.close()
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        rbridge.received.clear()
        cdp.reconnect()
        opens = [f for f in rbridge.received if f.get("op") == "open"]
        assert [f["url"] for f in opens] == ["https://a", "https://c"]
        assert cdp._tab_id is not None and tab_c.tab_id is not None
        assert tab_c.banner_present(".x") is True
    finally:
        cdp.close()


def test_stale_pre_eviction_event_is_dropped_after_remap(rbridge):
    # Criterion 4's literal scenario: after an eviction + reconnect remaps
    # tab ids, a late event tagged with a PRE-eviction id must be dropped,
    # while an event tagged with the remapped id reaches the same handle.
    cdp = _client(rbridge)
    got: list[dict] = []
    try:
        tab = cdp.new_tab("https://b", timeout=0.3)
        old_id = tab.tab_id
        tab.on_event("Custom.evt", lambda p: got.append(p))
        rbridge.drop()
        _wait(lambda: not cdp.is_alive())

        cdp.reconnect()
        new_id = tab.tab_id
        assert new_id is not None and new_id != old_id

        # Stale first, then the live one as an ordering fence: the reader
        # dispatches in order, so once the live event has fired we know the
        # stale one was processed (and dropped) before it.
        rbridge.send_event(
            {"event": True, "type": "cdp", "method": "Custom.evt",
             "params": {"stale": True}, "tabId": old_id}
        )
        rbridge.send_event(
            {"event": True, "type": "cdp", "method": "Custom.evt",
             "params": {"live": True}, "tabId": new_id}
        )
        _wait(lambda: bool(got))
        assert got == [{"live": True}]
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
