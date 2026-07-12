"""Thin raw page-CDP client over the Native Messaging bridge.

``chrome.debugger`` exposes **no browser-level target**, so Playwright's
``connect_over_cdp`` cannot attach to it. This module speaks page-level raw CDP
directly instead of carrying a second browser-automation runtime.

Topology (see ``docs/design.md``)::

    client (this module)  <-- loopback TCP -->  nmh.py  <-- NM stdio -->  sw.js

This client dials the loopback socket the host (:mod:`chrome_bridge.bridge.nmh`)
binds (``CBR_BRIDGE_HOST``/``CBR_BRIDGE_PORT``, default ``127.0.0.1:8765`` for
the default profile — each additional profile gets its own port, see
``docs/multi-profile.md``) and exchanges the same 4-byte-LE-length-prefixed
JSON frames (:mod:`.frames`) carrying the op/reply/event shapes
``extension/sw.js`` documents in its WIRE PROTOCOL block.

Responsibilities (the contract this module owns):

* Pair request ``id`` s to futures; route ``event:true`` frames (raw CDP events
  + ``onDetach``) to an event bus.
* Expose a small set of primitives that cover ordinary page scripting —
  navigation, waiting, reading text/HTML/attributes, filling inputs, and
  clicking via real synthetic input (not ``el.click()``, so anti-bot systems
  see genuine pointer input):

  - :meth:`ChromeBridge.open` / :meth:`ChromeBridge.navigate`  — open + ``Page.navigate`` +
    await ``Page.loadEventFired`` (the load wait is non-fatal — see below).
  - :meth:`ChromeBridge.attach`                          — attach to the user's
    current active tab instead of opening a fresh one; :meth:`ChromeBridge.close`
    then detaches but leaves that tab open (the one-shot CLI path).
  - :meth:`ChromeBridge.wait_selector`                   — poll ``querySelector !== null``.
  - :meth:`ChromeBridge.outer_html`                      — read a subtree's
    ``outerHTML`` (not the whole document by default, to keep replies modest;
    large replies relay fine under the bridge's 64 MB read guard). Pass a
    document-level selector (e.g. ``"html"``) when a caller needs the full
    page.
  - :meth:`ChromeBridge.click_text` / :meth:`ChromeBridge.click_selector`  — real
    ``Input.dispatchMouseEvent`` at the element centre.
  - :meth:`ChromeBridge.banner_present`                  — ``!!querySelector``.

Concurrency / failure model:

* One request at a time. ``sw.js`` requires it and this client enforces it with
  ``_op_lock`` held across each whole send->await round-trip, so even concurrent
  callers cannot pipeline ops onto the single tab.
* ``open``/``navigate`` await the full ``load`` event but treat a miss as
  non-fatal: ``wait_selector`` is the real readiness gate, and a page can finish
  loading before ``Page.enable`` subscribes.
* An ``onDetach`` fails any in-flight op with :class:`DetachedError` (it does not
  silently retry it) and marks the session so the **next** primitive re-attaches
  idempotently (re-open + re-``Page.enable``). Callers that must survive a mid-op
  detach should retry the primitive.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from .frames import FrameError, read_frame, write_frame

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Short per-step timeouts (seconds). A wedged service worker / dropped port must
# surface quickly rather than hang a long-running script.
DEFAULT_OP_TIMEOUT = 20.0          # one op/reply round-trip
DEFAULT_NAV_TIMEOUT = 30.0         # navigate + load event
DEFAULT_CONNECT_TIMEOUT = 8.0      # dial the loopback host
# Re-attach happens transparently inside another primitive (e.g. wait_selector
# polling) after an onDetach. Its load wait is non-fatal (the caller's own
# wait_selector is the real readiness gate), so bound it well below the 30 s
# nav timeout so a missed re-attach load event cannot stall the caller.
DEFAULT_REATTACH_TIMEOUT = 5.0

# Default CSS selector outer_html reads when the caller doesn't specify a
# container: the document's <body>. Chrome's 1 MB Native-Messaging cap applies
# only host->extension (the small command frames), NOT to replies: a large
# outerHTML flows extension->host->client bounded only by the relay's 64 MB
# read guard (see docs/design.md "Top risks #1"). A narrower container is
# still worth passing on heavy pages just to keep payloads small. When nothing
# matches, outer_html falls back to `documentElement` in JS as a last resort.
DEFAULT_CONTAINER_SELECTOR = "body"


class BridgeError(RuntimeError):
    """The bridge transport failed (not connected, peer gone, or op error)."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class BridgeTimeoutError(BridgeError):
    """A bridge op did not complete within its per-step timeout."""


class DetachedError(BridgeError):
    """The debugger detached from the tab (target closed, DevTools opened, SW evicted)."""


def _js_string(value: str) -> str:
    """Embed ``value`` as a safe JS string literal inside a Runtime.evaluate expr.

    ``json.dumps`` produces a valid double-quoted JS string literal with all
    quotes/backslashes/control chars escaped, which keeps selectors and search
    text from breaking out of the expression.
    """
    return json.dumps(value)


def _endpoint() -> tuple[str, int]:
    host = os.getenv("CBR_BRIDGE_HOST", DEFAULT_HOST)
    try:
        port = int(os.getenv("CBR_BRIDGE_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    return host, port


class ChromeBridge:
    """Raw page-CDP client over the bridge loopback socket.

    A single instance drives a single Chrome tab (the bridge's single-tab
    invariant). Use it as a context manager so the socket and tab are always
    torn down::

        with ChromeBridge(profile="personal") as cdp:
            cdp.navigate("https://example.com")
            cdp.wait_selector("#content")
            html = cdp.outer_html("#content")

    Thread-safety: a background reader thread fans replies/events out. ``_lock``
    guards the pending-future map and the write side of the socket (id
    correlation is race-free); ``_op_lock`` is held across each whole op/reply
    round-trip, so the one-request-at-a-time wire discipline (sw.js processes ops
    serially) holds even if two threads call public methods concurrently — they
    serialise rather than pipeline onto the single tab. A script should drive one
    ChromeBridge from one thread; the lock just makes the documented invariant
    true rather than merely assumed.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        *,
        profile: str | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        op_timeout: float = DEFAULT_OP_TIMEOUT,
    ) -> None:
        """Create a client for a bridge loopback endpoint.

        Resolution order for the endpoint: explicit ``host``/``port`` win if
        given; otherwise ``profile`` (a name registered via ``chrome-bridge
        init``) is resolved to a port through
        :mod:`chrome_bridge.profiles`; otherwise the ``CBR_BRIDGE_HOST`` /
        ``CBR_BRIDGE_PORT`` environment variables are used; otherwise the
        module defaults (``127.0.0.1:8765``).
        """
        if host is not None or port is not None:
            env_host, env_port = _endpoint()
            self._host = host if host is not None else env_host
            self._port = port if port is not None else env_port
        elif profile is not None:
            from .. import profiles as _profiles

            self._host = DEFAULT_HOST
            self._port = _profiles.resolve_port(profile)
        else:
            self._host, self._port = _endpoint()
        self._connect_timeout = connect_timeout
        self._op_timeout = op_timeout

        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None

        self._ids = itertools.count(1)
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._lock = threading.Lock()        # guards _pending + the write side
        # Serialises whole op round-trips (send -> await reply). The wire is
        # strictly one-request-at-a-time (see sw.js WIRE PROTOCOL): without this,
        # two threads could each write their op and both await, pipelining ops on
        # the single shared tab — which the single-tab model forbids. Held for the
        # full _request so a second caller blocks until the first reply lands.
        # Re-entrant so a primitive issuing several ops (held per-op, not nested)
        # and any future nested _request cannot self-deadlock.
        self._op_lock = threading.RLock()
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        # Generation guards against a stale reader from a prior (dead) socket
        # stomping a fresh reconnect. Each connect() bumps it; a reader only
        # touches shared state (_closed, pending) while it is still current, so
        # the old daemon reader's teardown after a reconnect is inert. Guarded by
        # _lock.
        self._generation = 0

        # Event bus: raw CDP events keyed by method name. Each callback gets the
        # event params dict. `_detached` flips on onDetach so primitives can
        # re-attach idempotently before their next op.
        self._event_listeners: dict[str, list] = {}
        self._detached = threading.Event()

        self._tab_id: int | None = None
        self._current_url: str | None = None
        # True when the current tab was bridge-created (open()); False when the
        # bridge merely attached to the user's active tab (attach()). Mirrors
        # sw.js's dbgTabOwned and gates detach()'s leave-the-tab-open exit.
        self._tab_owned = False

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Dial the bridge loopback socket and start the reader thread.

        Idempotent: a no-op if already connected. Safe to call again after a
        :meth:`close` or a transport loss (the reader thread that set
        ``_closed`` has its own daemon lifetime; a fresh ``connect`` allocates a
        new socket + reader and clears ``_closed``), which is what
        :meth:`reconnect` relies on to re-dial a Chrome-spawned host that came
        back after a service-worker eviction.
        """
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout)
        try:
            sock.connect((self._host, self._port))
        except OSError as exc:
            sock.close()
            raise BridgeError(
                f"could not connect to bridge at {self._host}:{self._port}: {exc} "
                "(is Chrome running with the Chrome Bridge extension loaded for "
                "this profile, and did it spawn the native host?)"
            ) from exc
        # Reads happen on the background thread with its own blocking semantics;
        # clear the connect timeout so per-op timeouts (futures) govern instead.
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        rfile = sock.makefile("rb")
        self._sock = sock
        self._rfile = rfile
        self._wfile = sock.makefile("wb")
        # A reconnect leaves stale request ids pending from the dead socket; drop
        # them so a late reply (it never comes — the host is gone) can't collide
        # with a fresh id and the pending map starts clean for the new reader.
        self._fail_all_pending(BridgeError("bridge reconnected; stale op abandoned"))
        # Bump the generation and clear _closed in ONE critical section. A stale
        # reader's teardown (_reader_teardown) reads the generation and acts on
        # _closed under the same lock, so it can never observe "current" against a
        # generation that is about to change — closing the TOCTOU that would let
        # it poison this fresh connection.
        with self._lock:
            self._generation += 1
            my_gen = self._generation
            self._closed.clear()
        # Pass this connection's rfile + generation explicitly: a stale reader
        # from a prior socket must read ITS OWN rfile and only touch shared state
        # while still current, so a reconnect's new reader is never corrupted.
        self._reader = threading.Thread(
            target=self._read_loop, args=(rfile, my_gen),
            name="chrome-bridge-reader", daemon=True,
        )
        self._reader.start()
        log.debug("chrome-bridge connected to bridge at %s:%d", self._host, self._port)

    def reconnect(self) -> None:
        """Re-dial the bridge socket after a transport loss, then re-attach.

        The MV3 service worker can be evicted (the documented #2 risk). When it
        is, the native port Chrome spawned drops, the host's relay ends, and this
        client's loopback socket dies — the reader thread sets ``_closed`` and
        every primitive then raises "bridge is not connected". Unlike an
        ``onDetach`` (tab gone, socket alive — handled by :meth:`_ensure_attached`
        re-opening the tab), a dead socket needs the **whole connection** rebuilt
        before any op can flow.

        This tears down the dead socket, dials a fresh one (the Chrome-spawned
        host is back as soon as the worker re-runs ``connect()`` in ``sw.js`` and
        re-binds the loopback port), and — if a URL was ever opened — re-opens the
        background tab on it so the session resumes idempotently. A long-lived
        owner reusing one client across jobs calls this instead of discarding the
        client, so ``_current_url`` is preserved across the heal.

        Raises :class:`BridgeError` if the socket cannot be re-dialled (host not
        back yet); the caller surfaces it and retries later.
        """
        # Drop the dead socket without trying its close-op (the peer is gone).
        self._teardown_socket()
        self.connect()
        # _detached/_tab_id were cleared on the socket death's failure path; a
        # fresh socket means no attached tab, so force a re-open on the last URL.
        self._tab_id = None
        self._detached.clear()
        if self._current_url is not None:
            log.info("chrome-bridge: reconnected; re-attaching to %s", self._current_url)
            self.open(self._current_url, timeout=DEFAULT_REATTACH_TIMEOUT)

    def close(self) -> None:
        """Best-effort end the session, then tear down the socket.

        Sends the ``close`` op: the extension detaches the debugger and
        removes the tab only if it was bridge-created (:meth:`open`) — a user
        tab joined via :meth:`attach` is detached but left open. Safe to call
        more than once and even if no tab was ever opened/attached.
        """
        if self._sock is None:
            return
        # Try to close the tab cleanly; never let teardown errors propagate.
        if self._tab_id is not None and not self._closed.is_set():
            try:
                self._request({"op": "close"}, timeout=self._op_timeout)
            except BridgeError as exc:
                log.debug("chrome-bridge close op failed (ignored): %s", exc)
        self._tab_id = None
        self._tab_owned = False
        self._current_url = None
        self._teardown_socket()

    def detach(self) -> None:
        """End the session, always leaving the tab open, then drop the socket.

        Sends the ``detach`` op: the extension detaches the debugger and
        clears its tab reference without removing the tab — even a
        bridge-created one (:meth:`open`). This matters beyond politeness: a
        stale server-side reference would make the *next* session's ``attach``
        teardown remove a tab this session promised to leave open. Use
        :meth:`close` when a bridge-created tab should be removed.
        """
        if self._sock is None:
            return
        if self._tab_id is not None and not self._closed.is_set():
            try:
                self._request({"op": "detach"}, timeout=self._op_timeout)
            except BridgeError as exc:
                log.debug("chrome-bridge detach op failed (ignored): %s", exc)
        self._tab_id = None
        self._tab_owned = False
        self._current_url = None
        self._teardown_socket()

    def _teardown_socket(self) -> None:
        """Drop the socket + reader without issuing a tab-close op.

        Shared by :meth:`close` (after its best-effort close-op) and
        :meth:`reconnect` (where the peer is already gone, so no close-op is
        possible). Sets ``_closed``, nulls the socket handles, aborts the socket
        best-effort, and fails any still-pending futures. Leaves
        ``_current_url`` untouched so a reconnect can re-attach to it.
        """
        if self._sock is None:
            return
        self._closed.set()
        sock, wfile = self._sock, self._wfile
        self._sock = self._rfile = self._wfile = None
        self._reader = None
        # Tear the connection down WITHOUT blocking on the reader thread.
        #
        # The reader is parked inside a blocking ``recv()`` (``rfile.read()``).
        # We deliberately do NOT close ``rfile`` here and do NOT join the reader:
        #  * Closing the buffered ``rfile`` while the reader holds its buffer lock
        #    inside ``read()`` deadlocks.
        #  * On Winsock a parked ``recv()`` does not wake on a local
        #    ``shutdown()``/``close()`` — only the peer closing wakes it (the same
        #    constraint nmh.py documents in _serve_session). Joining would then
        #    block until the peer disconnects.
        # Instead we shut down + close the underlying socket best-effort (which
        # frees the fd and, on platforms that honour it, aborts the read) and
        # leave the reader as a fire-and-forget daemon: it owns ``rfile`` and will
        # unwind on its own the moment its ``read()`` returns/errors. ``_closed``
        # is set, so even if it reads one more frame it dispatches nothing fatal.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if wfile is not None:
            try:
                wfile.close()
            except OSError:
                pass
        # Fail any still-pending futures so callers waiting on them wake up.
        self._fail_all_pending(BridgeError("bridge connection closed"))

    def is_alive(self) -> bool:
        """True if the loopback socket is connected and not torn down.

        A fatal transport loss (peer gone, reader thread stopped) sets
        ``_closed`` and drops the socket; after that every primitive raises
        "bridge is not connected", and this client cannot recover it on its own
        (``_ensure_attached`` only re-attaches a detached *tab*, not a dead
        *socket*). A long-lived owner that reuses one client across calls can
        poll this to discard and rebuild a dead client.
        """
        return self._sock is not None and not self._closed.is_set()

    def __enter__(self) -> ChromeBridge:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Reader thread: route replies to futures, events to the bus
    # ------------------------------------------------------------------ #

    def _read_loop(self, rfile, generation: int) -> None:
        """Pump frames off ``rfile`` until it closes/errors.

        ``rfile``/``generation`` are passed in (not read from ``self``) so a
        reader whose socket died after a :meth:`reconnect` reads its own dead
        stream and, on exit, only marks the connection ``_closed`` if it is
        STILL the current generation. Otherwise a stale daemon reader finishing
        late would wrongly tear down the fresh connection that replaced it.
        """
        try:
            while True:
                with self._lock:
                    current = generation == self._generation
                if not current or self._closed.is_set():
                    break
                try:
                    frame = read_frame(rfile)
                except (FrameError, OSError, ValueError) as exc:
                    if not self._closed.is_set():
                        log.warning("chrome-bridge reader: dropping bad frame: %s", exc)
                    break
                if frame is None:
                    break  # peer closed cleanly
                self._dispatch(frame, generation)
        finally:
            self._reader_teardown(generation)

    def _reader_teardown(self, generation: int) -> None:
        """Mark the connection dead + fail pending ops — but only if ``generation``
        is still current.

        The staleness check and the teardown it gates MUST be one atomic critical
        section. If they weren't, a stale reader could read "current", release the
        lock, be preempted while :meth:`connect` bumps the generation + clears
        ``_closed`` for a fresh socket, then resume and set ``_closed`` / fail the
        fresh connection's pending ops — the exact corruption the generation guard
        exists to prevent (a TOCTOU on the generation). ``connect`` likewise
        mutates the generation and ``_closed`` together under ``_lock``, so the two
        can never interleave. Futures are resolved outside the lock
        (``set_exception`` doesn't need it) to avoid holding ``_lock`` across
        callback work.
        """
        with self._lock:
            if generation != self._generation:
                return  # a fresh connection superseded this reader — stay inert
            self._closed.set()
            pending = list(self._pending.items())
            self._pending.clear()
        exc = BridgeError("bridge reader stopped")
        for _id, fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    def _dispatch(self, frame: dict[str, Any], generation: int) -> None:
        # A frame read by a stale reader (its socket died after a reconnect)
        # must never be routed into the live connection's pending map / event
        # bus — it belongs to the dead session. Drop it.
        with self._lock:
            if generation != self._generation:
                return
        if frame.get("event") is True:
            self._dispatch_event(frame)
            return
        # A reply: correlate by id.
        msg_id = frame.get("id")
        with self._lock:
            fut = self._pending.pop(msg_id, None) if msg_id is not None else None
        if fut is None:
            log.debug("chrome-bridge: reply for unknown id %r (dropped)", msg_id)
            return
        if not fut.done():
            fut.set_result(frame)

    def _dispatch_event(self, frame: dict[str, Any]) -> None:
        etype = frame.get("type")
        if etype == "detached":
            reason = frame.get("reason", "unknown")
            log.info("chrome-bridge: debugger detached (%s)", reason)
            # The tab is gone; mark so the next primitive re-opens. Also fail any
            # in-flight op so it doesn't hang on a reply that will never arrive.
            self._detached.set()
            self._tab_id = None
            self._tab_owned = False
            self._fail_all_pending(DetachedError(f"debugger detached: {reason}"))
            self._fire("__detached__", {"reason": reason})
            return
        if etype == "cdp":
            method = frame.get("method", "")
            self._fire(method, frame.get("params") or {})

    def _fire(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._event_listeners.get(method, ()))
        for cb in listeners:
            try:
                cb(params)
            except Exception:  # noqa: BLE001 — a bad listener must not kill the reader
                log.exception("chrome-bridge: event listener for %s raised", method)

    def _fail_all_pending(self, exc: Exception) -> None:
        """Resolve every pending future with ``exc`` so blocked callers wake."""
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for _id, fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    # ------------------------------------------------------------------ #
    # Event bus (public)
    # ------------------------------------------------------------------ #

    def on_event(self, method: str, callback) -> None:
        """Register ``callback(params)`` for a raw CDP event ``method``.

        Use ``"__detached__"`` to observe debugger detach (``params`` has
        ``reason``).
        """
        with self._lock:
            self._event_listeners.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback) -> None:
        with self._lock:
            listeners = self._event_listeners.get(method)
            if listeners and callback in listeners:
                listeners.remove(callback)

    # ------------------------------------------------------------------ #
    # Low-level request/reply
    # ------------------------------------------------------------------ #

    def _request(
        self, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one op frame, await its correlated reply, return the reply dict.

        Raises :class:`BridgeError` on an ``ok:false`` reply or transport loss,
        :class:`BridgeTimeoutError` if no reply arrives within ``timeout``.
        """
        if self._sock is None or self._closed.is_set():
            raise BridgeError("bridge is not connected")
        timeout = self._op_timeout if timeout is None else timeout
        # Serialise the whole round-trip so ops never pipeline on the single tab.
        with self._op_lock:
            msg_id = next(self._ids)
            frame = {"id": msg_id, **payload}
            fut: Future[dict[str, Any]] = Future()
            with self._lock:
                self._pending[msg_id] = fut
                try:
                    write_frame(self._wfile, frame)
                except (FrameError, OSError, ValueError) as exc:
                    self._pending.pop(msg_id, None)
                    raise BridgeError(
                        f"failed to send op {payload.get('op')}: {exc}"
                    ) from exc
            try:
                reply = fut.result(timeout=timeout)
            except FutureTimeoutError as exc:
                with self._lock:
                    self._pending.pop(msg_id, None)
                raise BridgeTimeoutError(
                    f"timed out after {timeout:.0f}s awaiting reply to "
                    f"{payload.get('op')} (id={msg_id})"
                ) from exc
        if not reply.get("ok", False):
            raise BridgeError(
                f"bridge op {payload.get('op')} failed: {reply.get('error', 'unknown')}",
                code=reply.get("errorCode"),
            )
        return reply

    def _cdp(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a raw CDP command on the attached tab; return its ``result``."""
        reply = self._request(
            {"op": "cdp", "method": method, "params": params or {}}, timeout=timeout
        )
        return reply.get("result") or {}

    def _evaluate(
        self, expression: str, *, timeout: float | None = None
    ) -> Any:
        """Runtime.evaluate ``expression`` (returnByValue) and return its value.

        Raises :class:`BridgeError` if the page threw (``exceptionDetails``).
        """
        result = self._cdp(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("text", "evaluation failed")
            exc = details.get("exception", {}).get("description", "")
            raise BridgeError(f"page evaluation error: {text} {exc}".strip())
        return result.get("result", {}).get("value")

    # ------------------------------------------------------------------ #
    # Core primitives
    # ------------------------------------------------------------------ #

    def open(self, url: str, *, timeout: float = DEFAULT_NAV_TIMEOUT) -> None:
        """Open ``url`` in a fresh attached tab, then await the load event.

        Mirrors a Playwright ``page.goto`` for a freshly opened page: it asks the
        extension to create+attach a background tab at ``url`` (so the first
        navigation is the open itself), enables ``Page`` events, and waits for
        ``Page.loadEventFired``.
        """
        # The extension's "open" both creates the tab AND navigates it to `url`,
        # so we register the load listener before enabling Page events to avoid a
        # race where the load fires before we subscribe.
        loaded = threading.Event()

        def _on_load(_params: dict[str, Any]) -> None:
            loaded.set()

        self.on_event("Page.loadEventFired", _on_load)
        try:
            # Clear the detach flag BEFORE issuing the open so that a detach the
            # reader observes for the NEW tab *during* this open (e.g. the SW is
            # evicted again mid-re-attach) survives and forces another re-attach.
            # Clearing it after the request — as an earlier draft did — could erase
            # a fresh detach the reader had just set, leaving _tab_id stale and the
            # session falsely "attached". A spuriously-set flag only costs one
            # harmless extra re-attach, which is the safe direction to err.
            self._detached.clear()
            reply = self._request({"op": "open", "url": url}, timeout=self._op_timeout)
            tab_id = reply.get("tabId")
            if tab_id is None:
                raise BridgeError(
                    "bridge 'open' reply carried no tabId; cannot attach to the tab"
                )
            self._tab_id = tab_id
            self._tab_owned = True
            self._current_url = url
            self._cdp("Page.enable")
            # The page may already have loaded between open and Page.enable; in
            # that case loadEventFired won't fire again, so a timeout here is not
            # fatal — wait_selector is the real readiness gate a script should use.
            self._wait_load(loaded, timeout)
        finally:
            self.off_event("Page.loadEventFired", _on_load)

    def attach(self, *, timeout: float | None = None) -> None:
        """Attach the debugger to the user's current active tab.

        The page the user is looking at becomes the bridge's tab, and ``Page``
        events are enabled on it. Unlike :meth:`open`, the tab is NOT
        bridge-created: :meth:`close` (and the context-manager exit) detaches
        but leaves it open, as does :meth:`detach`.

        Re-attach semantics for the attach case: no URL is recorded, so if the
        attached user tab goes away (an ``onDetach``), the next primitive
        raises :class:`DetachedError` — the bridge never silently re-opens the
        user's page in a fresh tab (re-creating a tab the user closed is never
        wanted). A subsequent explicit :meth:`navigate` records its URL and
        restores the usual re-attach behaviour for that script-requested page.

        ``timeout`` caps the ``attach`` op round-trip (default: the per-op
        timeout); the follow-up ``Page.enable`` uses the default per-op
        timeout. Raises :class:`BridgeError` if there is no active tab, on
        attach contention (DevTools open), or on a restricted page.
        """
        # Same rationale as open(): clear the detach flag BEFORE the request so
        # a detach observed for the new tab during this attach survives.
        self._detached.clear()
        reply = self._request({"op": "attach"}, timeout=timeout)
        tab_id = reply.get("tabId")
        if tab_id is None:
            raise BridgeError(
                "bridge 'attach' reply carried no tabId; cannot attach to the tab"
            )
        self._tab_id = tab_id
        self._tab_owned = False
        # No URL bookkeeping: an attached user tab must never be silently
        # re-opened after a detach (see docstring).
        self._current_url = None
        self._cdp("Page.enable")

    def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        """Evaluate a JS ``expression`` in the page and return its value.

        The public counterpart of the internal evaluate primitive: it first
        ensures the session is attached (re-attaching idempotently where
        possible), then runs ``Runtime.evaluate`` with ``returnByValue``.
        Raises :class:`BridgeError` if the page threw.
        """
        self._ensure_attached()
        return self._evaluate(expression, timeout=timeout)

    def navigate(self, url: str, *, timeout: float = DEFAULT_NAV_TIMEOUT) -> None:
        """Navigate the attached tab to ``url`` and await the load event.

        Re-attaches idempotently if the tab is gone (no tab yet, or a prior
        ``onDetach``); otherwise issues ``Page.navigate`` on the existing tab.
        This is the in-session equivalent of Playwright ``page.goto``.

        A dead loopback socket (the service worker was evicted, dropping the
        native port and thus the host's relay) is healed first: record ``url`` so
        :meth:`reconnect` re-attaches to it, then rebuild the connection and
        return — the page is now open and loaded on the fresh tab. On that heal
        path ``timeout`` does not govern the load wait (``reconnect`` uses the
        short :data:`DEFAULT_REATTACH_TIMEOUT` ceiling), which is harmless: the
        re-attach load wait is non-fatal and ``wait_selector`` is the real
        readiness gate a script should apply next.
        """
        if self._sock is None or self._closed.is_set():
            # No live socket to navigate over: rebuild the whole connection and
            # land on `url`. Set _current_url first so reconnect() re-attaches to
            # the requested page, not a stale prior one.
            self._current_url = url
            self.reconnect()
            return
        if self._tab_id is None or self._detached.is_set():
            self.open(url, timeout=timeout)
            return
        loaded = threading.Event()

        def _on_load(_params: dict[str, Any]) -> None:
            loaded.set()

        self.on_event("Page.loadEventFired", _on_load)
        try:
            self._cdp("Page.enable")
            result = self._cdp("Page.navigate", {"url": url})
            if result.get("errorText"):
                raise BridgeError(
                    f"navigate to {url} failed: {result['errorText']}"
                )
            self._current_url = url
            self._wait_load(loaded, timeout)
        finally:
            self.off_event("Page.loadEventFired", _on_load)

    def _wait_load(self, loaded: threading.Event, timeout: float) -> None:
        """Wait up to ``timeout`` for the load event; a timeout is non-fatal.

        A script should gate real readiness on :meth:`wait_selector`, so a missed
        load event (e.g. the page loaded before we subscribed) must not abort.
        """
        if not loaded.wait(timeout=timeout):
            log.debug("chrome-bridge: no Page.loadEventFired within %.0fs (continuing)", timeout)

    def wait_selector(
        self,
        selector: str,
        *,
        timeout: float = 15.0,
        poll_interval: float = 0.3,
    ) -> bool:
        """Poll until ``document.querySelector(selector)`` is non-null.

        Returns ``True`` once the element appears; raises
        :class:`BridgeTimeoutError` if it never does within ``timeout``.
        Mirrors Playwright's ``page.wait_for_selector``.
        """
        expr = f"document.querySelector({_js_string(selector)})!==null"
        deadline = time.monotonic() + timeout

        def _expired() -> bool:
            return time.monotonic() >= deadline

        while True:
            # Clamp every blocking leg to the remaining budget so the call
            # honours `timeout` as a true upper bound. Otherwise a wedged op
            # (op_timeout, 20s) or a mid-loop re-attach could each block far
            # past `timeout`, stalling the caller in exactly the SW eviction
            # conditions this bridge exists to survive.
            if _expired():
                raise BridgeTimeoutError(
                    f"selector {selector!r} not present after {timeout:.0f}s"
                )
            self._ensure_attached(timeout=deadline - time.monotonic())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeTimeoutError(
                    f"selector {selector!r} not present after {timeout:.0f}s"
                )
            try:
                present = self._evaluate(expr, timeout=min(self._op_timeout, remaining))
            except BridgeTimeoutError:
                # The evaluate was clamped to the remaining budget and timed out:
                # that means the overall wait_selector deadline lapsed mid-poll.
                # Surface it as the caller-meaningful "selector not present"
                # rather than a confusing low-level "timed out awaiting cdp".
                if _expired():
                    raise BridgeTimeoutError(
                        f"selector {selector!r} not present after {timeout:.0f}s"
                    ) from None
                raise
            if present is True:
                return True
            if _expired():
                raise BridgeTimeoutError(
                    f"selector {selector!r} not present after {timeout:.0f}s"
                )
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def outer_html(
        self, container: str = DEFAULT_CONTAINER_SELECTOR, *, timeout: float | None = None
    ) -> str:
        """Return the ``outerHTML`` of the first element matching ``container``.

        Replies are not subject to Chrome's 1 MB host->extension cap (that cap
        covers only the small command frames); a large ``outerHTML`` relays
        intact up to the bridge's 64 MB read guard. A narrower, site-specific
        ``container`` selector is still worthwhile on heavy pages to keep
        payloads small. ``container`` may be a
        comma-separated selector list (tried left to right by
        ``querySelector``); the document element is the last-resort fallback so
        a present-but-unmatched container still yields *some* HTML rather than
        an empty string. Mirrors Playwright ``page.content()`` but scoped to
        whatever subtree the caller actually needs.
        """
        sel = _js_string(container)
        expr = (
            f"(function(){{var e=document.querySelector({sel});"
            "return e?e.outerHTML:document.documentElement.outerHTML;}})()"
        )
        self._ensure_attached()
        html = self._evaluate(expr, timeout=timeout)
        return html if isinstance(html, str) else ""

    def click_text(
        self, text: str, *, tag: str | None = None, timeout: float | None = None
    ) -> bool:
        """Click the element whose visible text matches ``text`` via real input.

        Finds the element's bounding box in the page, then dispatches real
        ``Input.dispatchMouseEvent`` (mouseMoved -> mousePressed -> mouseReleased)
        at its centre — NOT a synthetic ``el.click()`` — so anti-bot systems see
        genuine pointer input. ``tag`` optionally narrows the candidate elements
        (e.g. ``"button"``). Returns ``True`` on a dispatched click; raises
        :class:`BridgeError` if no visible element matches.

        Matching is by **normalised whitespace** (runs collapsed, trimmed), not
        strict equality, and resolves to the **innermost** matching element: an
        ancestor whose only text is the target (a wrapper ``<div>`` around
        ``<button>Submit</button>``) is skipped in favour of the deepest match,
        so the click lands on the real control, not the wrapper.
        """
        self._ensure_attached()
        rect = self._element_center(text, tag=tag, timeout=timeout)
        if rect is None:
            raise BridgeError(f"no visible element with text {text!r} to click")
        x, y = rect
        self._dispatch_click(x, y, timeout=timeout)
        return True

    def click_selector(
        self, selector: str, *, timeout: float | None = None
    ) -> bool:
        """Click the first element matching ``selector`` via real mouse input.

        The selector-based counterpart of :meth:`click_text`, mirroring
        Playwright's ``locator(selector).click()``. Dispatches real
        ``Input.dispatchMouseEvent`` at the element centre. Raises
        :class:`BridgeError` if no visible element matches ``selector``.
        """
        self._ensure_attached()
        rect = self._selector_center(selector, timeout=timeout)
        if rect is None:
            raise BridgeError(f"no visible element matching {selector!r} to click")
        x, y = rect
        self._dispatch_click(x, y, timeout=timeout)
        return True

    def _element_center(
        self,
        text: str,
        *,
        tag: str | None,
        contains: bool = False,
        timeout: float | None = None,
    ) -> tuple[float, float] | None:
        """Return the viewport-centre (x, y) of the visible element whose
        normalised text matches ``text`` (and optional ``tag``), or ``None``.

        Walks every candidate and keeps the **deepest** match (one with no
        descendant that also matches), so a wrapper around the real control is
        never chosen. Text is compared with whitespace collapsed/trimmed, so
        child spans and incidental whitespace in the page's markup don't defeat
        the match. Returns CSS-pixel viewport coordinates.

        ``contains`` mirrors Playwright's ``text=`` engine: ``False`` is exact
        normalised equality (the click/button-label case); ``True`` is
        case-insensitive substring matching (the banner-fragment case, where the
        wanted phrase is only part of a longer sentence). In substring mode the
        deepest-match rule still applies, so the click lands on the innermost
        element containing the phrase, never an outer wrapper.
        """
        wanted = _js_string(text)
        tag_filter = _js_string(tag.lower()) if tag else "null"
        match_fn = (
            "function match(s){return norm(s).toLowerCase().indexOf(target)!==-1;}"
            if contains
            else "function match(s){return norm(s)===target;}"
        )
        # In substring mode target is lowercased once so each compare is cheap.
        target_expr = (
            "norm(wanted).toLowerCase()" if contains else "norm(wanted)"
        )
        expr = (
            "(function(){"
            f"var wanted={wanted};var tag={tag_filter};"
            "function norm(s){return (s||'').replace(/\\s+/g,' ').trim();}"
            f"{match_fn}"
            f"var target={target_expr};"
            "var els=document.querySelectorAll(tag?tag:'*');"
            "var best=null;"
            "for(var i=0;i<els.length;i++){var el=els[i];"
            "if(!match(el.textContent))continue;"
            "var inner=el.querySelectorAll(tag?tag:'*');var wrapper=false;"
            "for(var j=0;j<inner.length;j++){"
            "if(match(inner[j].textContent)){wrapper=true;break;}}"
            "if(wrapper)continue;"
            "best=el;break;}"
            "if(!best)return null;return __cbCenter(best);})()"
        )
        return self._eval_center(self._center_helper() + expr, timeout=timeout)

    def _selector_center(
        self, selector: str, *, timeout: float | None
    ) -> tuple[float, float] | None:
        """Return the viewport-centre (x, y) of the first visible element
        matching ``selector``, or ``None`` if absent / not visible."""
        sel = _js_string(selector)
        expr = (
            "(function(){"
            f"var el=document.querySelector({sel});"
            "if(!el)return null;return __cbCenter(el);})()"
        )
        return self._eval_center(self._center_helper() + expr, timeout=timeout)

    @staticmethod
    def _center_helper() -> str:
        """JS prelude defining ``__cbCenter(el)`` -> visible centre or null.

        Shared by text- and selector-based clicks so visibility and centring are
        computed identically. Returns ``null`` for zero-area or
        ``display:none``/``visibility:hidden`` elements.
        """
        return (
            "var __cbCenter=function(el){"
            "var r=el.getBoundingClientRect();"
            "if(r.width===0||r.height===0)return null;"
            "var s=window.getComputedStyle(el);"
            "if(s.visibility==='hidden'||s.display==='none')return null;"
            "return {x:r.left+r.width/2,y:r.top+r.height/2};};"
        )

    def _eval_center(
        self, expr: str, *, timeout: float | None
    ) -> tuple[float, float] | None:
        value = self._evaluate(expr, timeout=timeout)
        if not isinstance(value, dict):
            return None
        try:
            return float(value["x"]), float(value["y"])
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # Selector read/fill primitives
    # ------------------------------------------------------------------ #

    def query_text(
        self, selector: str, *, timeout: float | None = None
    ) -> str | None:
        """Return the trimmed ``innerText`` of the first element matching
        ``selector``, or ``None`` if no element matches.

        Mirrors Playwright ``locator(selector).inner_text()``. ``innerText``
        (with a ``textContent`` fallback) is used so it reflects the rendered
        text a user would actually see.
        """
        sel = _js_string(selector)
        expr = (
            "(function(){"
            f"var el=document.querySelector({sel});"
            "if(!el)return null;"
            "return (el.innerText||el.textContent||'').trim();})()"
        )
        self._ensure_attached()
        value = self._evaluate(expr, timeout=timeout)
        return value if isinstance(value, str) else None

    def get_attribute(
        self, selector: str, name: str, *, timeout: float | None = None
    ) -> str | None:
        """Return attribute ``name`` of the first element matching ``selector``,
        or ``None`` if the element or the attribute is absent.

        Mirrors Playwright ``locator(selector).get_attribute(name)``.
        """
        sel = _js_string(selector)
        attr = _js_string(name)
        expr = (
            "(function(){"
            f"var el=document.querySelector({sel});"
            "if(!el)return null;"
            f"return el.getAttribute({attr});}})()"
        )
        self._ensure_attached()
        value = self._evaluate(expr, timeout=timeout)
        return value if isinstance(value, str) else None

    def is_visible(
        self, selector: str, *, timeout: float | None = None
    ) -> bool:
        """Return ``True`` if the first element matching ``selector`` is present
        and visible (non-zero box, not ``display:none``/``visibility:hidden``).

        Mirrors Playwright ``locator(selector).is_visible()``.
        """
        self._ensure_attached()
        return self._selector_center(selector, timeout=timeout) is not None

    def text_visible(
        self,
        text: str,
        *,
        tag: str | None = None,
        contains: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Return ``True`` if a visible element whose normalised text matches
        ``text`` exists (optionally narrowed to ``tag``).

        The visible-text counterpart of :meth:`is_visible`, and the read-only
        sibling of :meth:`click_text`: it runs the same innermost / normalised-
        whitespace text walk but only reports presence+visibility instead of
        clicking. Useful for reading confirmation banners or button-label
        fallbacks Playwright's ``text=``/``:has-text`` engines would otherwise
        express.

        ``contains`` selects the match mode to mirror Playwright's ``text=``
        engine: the default (``False``) is exact normalised equality, used for
        button-label matching; ``contains=True`` is case-insensitive substring
        matching, for when the wanted phrase is only a fragment of a larger
        element's text.
        """
        self._ensure_attached()
        return (
            self._element_center(text, tag=tag, contains=contains, timeout=timeout)
            is not None
        )

    def input_value(
        self, selector: str, *, timeout: float | None = None
    ) -> str | None:
        """Return the ``value`` of the first input/textarea matching
        ``selector``, or ``None`` if absent.

        Mirrors Playwright ``locator(selector).input_value()``.
        """
        sel = _js_string(selector)
        expr = (
            "(function(){"
            f"var el=document.querySelector({sel});"
            "if(!el)return null;"
            "return el.value==null?'':String(el.value);})()"
        )
        self._ensure_attached()
        value = self._evaluate(expr, timeout=timeout)
        return value if isinstance(value, str) else None

    def fill(
        self, selector: str, text: str, *, timeout: float | None = None
    ) -> bool:
        """Set the ``value`` of the first input/textarea matching ``selector``
        to ``text``, firing ``input``/``change`` so the page's framework
        registers it. Returns ``True`` on success, ``False`` if no element
        matches.

        Mirrors Playwright ``locator(selector).fill(text)``: it focuses the
        element, replaces its value, and dispatches the events most forms
        listen for before accepting the value.
        """
        sel = _js_string(selector)
        val = _js_string(text)
        expr = (
            "(function(){"
            f"var el=document.querySelector({sel});"
            "if(!el)return false;"
            "el.focus();"
            f"el.value={val};"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));"
            "return true;})()"
        )
        self._ensure_attached()
        return bool(self._evaluate(expr, timeout=timeout))

    def _dispatch_click(
        self, x: float, y: float, *, timeout: float | None = None
    ) -> None:
        """Dispatch a real left-click at viewport coords (x, y)."""
        base = {"x": x, "y": y, "button": "left"}
        self._cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", **base, "buttons": 0},
            timeout=timeout,
        )
        self._cdp(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", **base, "buttons": 1, "clickCount": 1},
            timeout=timeout,
        )
        self._cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", **base, "buttons": 0, "clickCount": 1},
            timeout=timeout,
        )

    def banner_present(
        self, selector: str, *, timeout: float | None = None
    ) -> bool:
        """Return ``True`` if ``document.querySelector(selector)`` matches.

        A single, non-polling check — ``!!querySelector(...)`` — used to detect
        a confirmation/captcha banner or any other transient element. Mirrors a
        Playwright ``locator.count()>0`` style check.
        """
        expr = f"!!document.querySelector({_js_string(selector)})"
        self._ensure_attached()
        return bool(self._evaluate(expr, timeout=timeout))

    def get_cookies(
        self, urls: list[str] | None = None, *, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Return the browser cookies for ``urls`` via ``Network.getCookies``.

        This reads cookies straight out of the **real, logged-in profile** the
        bridge drives — Chrome decrypts them for us, so it sidesteps App-Bound
        Encryption (which breaks *copying* a profile's cookie store but not a
        live in-browser read). Each cookie is the raw CDP shape
        (``name``/``value``/``domain``/``path``/``expires``/…). Pass ``urls`` to
        scope the read to specific origins; omit it to read the attached tab's
        cookies.

        ``Network.getCookies`` is a one-shot command and does **not** need
        ``Network.enable`` — deliberately not enabling it keeps the bridge from
        being flooded with per-request network events on the single tab.
        """
        params: dict[str, Any] = {}
        if urls:
            params["urls"] = list(urls)
        self._ensure_attached()
        result = self._cdp("Network.getCookies", params, timeout=timeout)
        cookies = result.get("cookies")
        return cookies if isinstance(cookies, list) else []

    # ------------------------------------------------------------------ #
    # Idempotent re-attach
    # ------------------------------------------------------------------ #

    def _ensure_attached(self, *, timeout: float | None = None) -> None:
        """Resume the session if a service-worker eviction left it unusable.

        The MV3 service worker can be evicted mid-job (the documented #2 risk).
        It surfaces two ways, handled idempotently here so the **next** primitive
        transparently resumes with no human click:

        * **onDetach, socket still alive** — the tab is gone but the loopback
          host is up. Re-open the tab on the last URL and re-enable ``Page``
          events ("re-open and re-Page.enable rather than assume the prior tab
          is still attached", per the wire contract).
        * **socket dead** — eviction also dropped the native port Chrome spawned,
          so the host's relay ended and this client's loopback socket died. A
          dead socket cannot carry an ``open``; rebuild the whole connection via
          :meth:`reconnect` (which re-dials and re-attaches to the last URL)
          before any op. A long-lived owner reusing one client thus self-heals
          an evicted worker without being discarded and rebuilt.

        ``timeout`` caps the re-attach load wait; callers running on a deadline
        (e.g. :meth:`wait_selector`) pass their remaining budget so a re-attach
        cannot blow past their own bound. It is clamped to a small ceiling
        (:data:`DEFAULT_REATTACH_TIMEOUT`) since the re-attach load event is
        non-fatal anyway — the caller's own readiness check is the real gate.

        Sessions joined via :meth:`attach` record no URL, so a detach of the
        user's tab surfaces here as :class:`DetachedError` — by design the
        bridge never silently re-opens a user tab (see :meth:`attach`).
        """
        # A dead socket must be rebuilt first: reconnect() re-dials and (if a URL
        # was opened) re-attaches, so this returns ready. Without a URL there is
        # nothing to resume — surface the loss rather than silently reconnect to
        # an unattached session.
        if self._sock is None or self._closed.is_set():
            if self._current_url is None:
                raise BridgeError(
                    "bridge connection lost and no URL to re-attach to "
                    "(call open/navigate/attach first)"
                )
            self.reconnect()
            return
        if self._tab_id is not None and not self._detached.is_set():
            return
        if self._current_url is None:
            raise DetachedError(
                "debugger detached and no URL to re-attach to "
                "(call open/navigate/attach first)"
            )
        load_timeout = DEFAULT_REATTACH_TIMEOUT
        if timeout is not None:
            load_timeout = max(0.0, min(DEFAULT_REATTACH_TIMEOUT, timeout))
        log.info("chrome-bridge: re-attaching to %s after detach", self._current_url)
        self.open(self._current_url, timeout=load_timeout)

