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

Split of responsibilities (Playwright-style):

* :class:`ChromeBridge` owns the **connection**: the loopback socket, the
  background reader thread, request-``id`` correlation, and a connection-wide
  event bus. Over the multi-tab wire protocol it can hold several attached
  tabs at once: :meth:`ChromeBridge.new_tab` opens an ADDITIONAL tab (``open``
  with ``replace:false``) and returns a :class:`Tab` handle.
* :class:`Tab` owns **page state and the page primitives** for one attached
  tab — ``tab_id``, ``current_url``, a per-tab detached flag, a per-tab event
  bus — covering ordinary page scripting: navigation, waiting, reading
  text/HTML/attributes, filling inputs, clicking via real synthetic input
  (not ``el.click()``, so anti-bot systems see genuine pointer input), and
  cookies. Every op stamps this tab's ``tabId`` into its wire frames.
* Backward-compatible facade: :class:`ChromeBridge` keeps the historical
  single-tab surface — :meth:`ChromeBridge.open` / :meth:`ChromeBridge.navigate`
  / :meth:`ChromeBridge.wait_selector` / ... — by delegating to an internal
  **default tab**. The facade ``open`` keeps the wire-default ``replace:true``
  (single-tab mode: the extension tears down every other attached tab first),
  so existing callers and the CLI run unchanged.

  - :meth:`Tab.navigate` (and the facade ``open``/``navigate``) — open /
    ``Page.navigate`` + await ``Page.loadEventFired`` (the load wait is
    non-fatal — see below).
  - :meth:`ChromeBridge.attach`                 — attach to the user's current
    active tab instead of opening a fresh one; :meth:`ChromeBridge.close`
    then detaches but leaves that tab open (the one-shot CLI path).
  - :meth:`Tab.wait_selector`                   — poll ``querySelector !== null``.
  - :meth:`Tab.outer_html`                      — read a subtree's
    ``outerHTML`` (not the whole document by default, to keep replies modest;
    large replies relay fine under the bridge's 64 MB read guard). Pass a
    document-level selector (e.g. ``"html"``) when a caller needs the full
    page.
  - :meth:`Tab.click_text` / :meth:`Tab.click_selector`  — real
    ``Input.dispatchMouseEvent`` at the element centre.
  - :meth:`Tab.banner_present`                  — ``!!querySelector``.

Concurrency / failure model:

* Per-tab pipelining — ops on ONE tab are strictly FIFO (each :class:`Tab`
  holds its own op lock across the whole send->await round-trip); ops on
  DIFFERENT tabs may interleave and run in parallel, which ``sw.js`` supports
  with per-tab request chains. Tab-lifecycle ops — :meth:`ChromeBridge.new_tab`,
  :meth:`Tab.close`, :meth:`ChromeBridge.reconnect` / :meth:`ChromeBridge.close`
  / :meth:`ChromeBridge.detach`, and every ``open``/``attach`` round-trip —
  serialize against EVERYTHING (the exclusive side of the connection lock),
  because they mutate the session table / the tab registry. Thread-safety:
  ONE THREAD PER TAB is the supported pattern; the connection internals
  (reader thread, pending map, event buses, the lock split above) are safe
  under it. Two threads sharing one tab serialize on that tab's lock in no
  guaranteed order — and their RECOVERY is not coordinated: both may re-open
  the same detached tab, leaking a duplicate server-side tab until the next
  ``replace:true`` open reclaims it. Stick to one thread per tab.
* ``open``/``navigate`` await the full ``load`` event but treat a miss as
  non-fatal: ``wait_selector`` is the real readiness gate, and a page can finish
  loading before ``Page.enable`` subscribes.
* An ``onDetach`` is per tab: it fails any in-flight op TARGETING that tab
  with :class:`DetachedError` (it does not silently retry it) and marks that
  tab's session so the **next** primitive on it re-attaches idempotently
  (re-open + re-``Page.enable``). Other attached tabs are untouched. Callers
  that must survive a mid-op detach should retry the primitive.
* A DEAD SOCKET (the MV3 service worker was evicted, dropping the native
  port and with it every attachment at once) is recovered whole-connection:
  :meth:`ChromeBridge.reconnect` re-dials the host and re-opens EVERY tab
  that had a ``current_url``, in creation order, remapping each handle's
  ``tab_id`` onto its fresh Chrome tab so callers' :class:`Tab` references
  keep working. A handle that never opened a URL has nothing to restore and
  is marked dead: its next primitive raises an actionable
  :class:`BridgeError` (an explicit open/navigate/attach starts a fresh
  session on it and revives the handle). Primitives trigger this heal
  transparently via ``_ensure_attached``.
* Stale events: an event tagged with a ``tabId`` no live handle has
  published — e.g. a late event from a tab that already detached, or from a
  pre-eviction session — is dropped (debug-logged) rather than routed, so it
  can never fire a listener on the wrong handle. (A ``detached`` event in
  that position is parked instead of dropped, to catch a detach racing its
  own open/attach reply — see ``_undispatched_detaches``.)
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
from contextlib import contextmanager
from typing import Any, BinaryIO

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
# Minimum share of the reconnect heal budget worth STARTING a heal with
# (seconds). An "open" sent with a near-zero timeout is worse than none: the
# client gives up immediately, but a slow-but-live worker still creates and
# attaches the tab — whose late reply is then dropped as an unknown id — so
# the tab is orphaned server-side until the next key-less reaper open. Not
# starting hopeless heals keeps that window small.
_MIN_HEAL_BUDGET = 0.5

# Sentinel for Tab._open's `replace` param: "compute it adaptively via
# Tab._reopen_replace()" — distinct from None, which is a meaningful value
# there (omit the key -> the wire default replace:true).
_ADAPTIVE_REPLACE = object()

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


def _fire_listeners(
    lock: threading.Lock,
    listeners_by_method: dict[str, list],
    method: str,
    params: dict[str, Any],
) -> None:
    """Invoke every listener registered for ``method`` with ``params``.

    Shared by the connection-wide bus (:meth:`ChromeBridge.on_event`) and the
    per-tab buses (:meth:`Tab.on_event`). Listeners are snapshotted under
    ``lock`` and invoked outside it; a raising listener is logged, never
    allowed to kill the reader thread.
    """
    with lock:
        listeners = list(listeners_by_method.get(method, ()))
    for cb in listeners:
        try:
            cb(params)
        except Exception:  # noqa: BLE001 — a bad listener must not kill the reader
            log.exception("chrome-bridge: event listener for %s raised", method)


class _RWLock:
    """Reentrant reader-writer lock backing the connection's op discipline.

    Ordinary per-tab op round-trips hold the SHARED side (many at once — this
    is what lets independent tabs pipeline); tab-lifecycle ops (``new_tab``,
    ``Tab.close``, ``reconnect``/``close``/``detach``, every ``open``/
    ``attach`` round-trip) hold the EXCLUSIVE side, serializing against every
    in-flight op because they mutate the extension's session table or the
    client's tab registry.

    Reentrancy: a thread may nest shared grips, nest exclusive grips, and
    take shared grips while holding the exclusive one (lifecycle ops issue
    ordinary op legs, e.g. ``reconnect`` -> ``_open`` -> ``Page.enable``).
    Upgrading — taking the exclusive side while holding only the shared one —
    would deadlock against another upgrader and raises instead; no code path
    does it. Writers get preference: a first-time shared grip queues behind a
    waiting exclusive one, so lifecycle ops cannot starve under steady
    per-tab traffic. Lock order elsewhere in this module: the shared grip is
    taken BEFORE a tab's own op lock, never after — an exclusive holder may
    then take any tab lock without deadlock (all tab locks taken under a
    shared grip are released before the grip is).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0            # live shared grips, all threads, incl. nested
        self._writer: int | None = None   # ident of the exclusive-side owner
        self._writer_depth = 0
        self._writers_waiting = 0
        self._local = threading.local()   # per-thread shared-grip depth

    def _shared_depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @contextmanager
    def shared(self):
        me = threading.get_ident()
        with self._cond:
            # Bypass the queue when this thread already holds a grip: a nested
            # shared grip must never block behind a WAITING writer (that would
            # deadlock — the writer waits for this thread's outer grip), and
            # the exclusive holder's own op legs must run.
            if self._writer != me and self._shared_depth() == 0:
                while self._writer is not None or self._writers_waiting > 0:
                    self._cond.wait()
            self._local.depth = self._shared_depth() + 1
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._local.depth -= 1
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        me = threading.get_ident()
        with self._cond:
            if self._writer == me:
                self._writer_depth += 1
            else:
                if self._shared_depth() > 0:
                    raise RuntimeError(
                        "cannot take the exclusive connection lock while "
                        "holding the shared side (lock upgrade would deadlock)"
                    )
                self._writers_waiting += 1
                try:
                    while self._writer is not None or self._readers > 0:
                        self._cond.wait()
                    self._writer = me
                    self._writer_depth = 1
                finally:
                    self._writers_waiting -= 1
                    if self._writers_waiting == 0 and self._writer is not me:
                        # Interrupted while queued: wake the shared grips this
                        # waiter's writer preference was holding back.
                        self._cond.notify_all()
        try:
            yield
        finally:
            with self._cond:
                self._writer_depth -= 1
                if self._writer_depth == 0:
                    self._writer = None
                    self._cond.notify_all()


class ChromeBridge:
    """Raw page-CDP client over the bridge loopback socket.

    Owns the CONNECTION: the loopback socket, the background reader thread,
    request-id correlation, and the connection-wide event bus. Page state and
    the page primitives live on :class:`Tab` handles. One instance can hold
    several attached tabs at once (:meth:`new_tab`), while the historical
    single-tab surface keeps working through an internal default tab::

        with ChromeBridge(profile="personal") as cdp:
            cdp.navigate("https://example.com")        # the default tab
            cdp.wait_selector("#content")
            html = cdp.outer_html("#content")

            other = cdp.new_tab("https://example.org")  # an ADDITIONAL tab
            other.wait_selector("#main")
            other.close()

    Thread-safety: a background reader thread fans replies/events out. ``_lock``
    guards the pending-future map, the tab registry, and the write side of the
    socket (id correlation is race-free, and frames never interleave on the
    wire). Op ordering is PER TAB: each :class:`Tab` holds its own op lock
    across each whole op/reply round-trip, so ops on one tab are FIFO while
    ops on different tabs pipeline in parallel (sw.js processes them on
    per-tab chains). Tab-lifecycle ops — :meth:`new_tab`, :meth:`Tab.close`,
    :meth:`reconnect`, :meth:`close`, :meth:`detach`, and every
    ``open``/``attach`` round-trip — take the exclusive side of ``_conn_lock``
    and serialize against everything. The supported pattern is ONE THREAD PER
    TAB; two threads sharing a tab serialize on its lock but do not
    coordinate recovery (see the module docstring).
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
        # Remembered for the auth handshake: a named profile maps straight to its
        # token file; a host/port-only client resolves the token by port instead.
        self._profile = profile
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
        # msg id -> the tabId its op targets (None for tab-less ops such as
        # "open"/"attach"/"tabs"). Kept BESIDE _pending — not merged into it —
        # so a per-tab detach can fail only that tab's in-flight ops while
        # everything that iterates _pending keeps seeing bare futures. Guarded
        # by _lock; entries live and die with their _pending twin.
        self._pending_tab: dict[int, int | None] = {}
        self._lock = threading.Lock()        # guards _pending + the write side
        # Connection-level ordering lock. Ordinary per-tab op round-trips hold
        # its SHARED side (plus their own Tab._op_lock — see Tab), so ops on
        # one tab are FIFO while independent tabs pipeline in parallel (the
        # wire supports it: sw.js runs per-tab request chains). Tab-lifecycle
        # ops — new_tab, Tab.close, reconnect/close/detach, every open/attach
        # round-trip — hold the EXCLUSIVE side, serializing against every
        # in-flight op, because they mutate the extension's session table or
        # this client's tab registry.
        self._conn_lock = _RWLock()
        self._reader: threading.Thread | None = None
        self._closed = threading.Event()
        # Generation guards against a stale reader from a prior (dead) socket
        # stomping a fresh reconnect. Each connect() bumps it; a reader only
        # touches shared state (_closed, pending) while it is still current, so
        # the old daemon reader's teardown after a reconnect is inert. Guarded by
        # _lock.
        self._generation = 0

        # Connection-wide event bus: raw CDP events keyed by method name. Each
        # callback gets the event params dict, for EVERY tab's events (per-tab
        # routing lives on Tab.on_event).
        self._event_listeners: dict[str, list] = {}

        # tabId -> reason for targeted "detached" events that matched no handle
        # when the reader processed them. The one legitimate producer is a
        # detach racing its own open/attach reply: the op thread has not
        # published the new tab_id yet, so routing by id finds nothing — and
        # without this map the handle would believe itself attached to a
        # session the extension already discarded, forever. _open/_attach
        # consume entries atomically when publishing the id. Guarded by _lock;
        # cleared on every (re)connect (a new browser session may reuse ids).
        self._undispatched_detaches: dict[int, str] = {}

        # Tab registry, used to route tabId-tagged events to their handles.
        # The FIRST entry is the default tab backing the legacy single-tab
        # facade; new_tab() appends. Guarded by _lock.
        self._default_tab = Tab(self, is_default=True)
        self._tabs: list[Tab] = [self._default_tab]

    # ------------------------------------------------------------------ #
    # Legacy single-tab introspection (delegates to the default tab).
    # Kept as properties so callers and tests that reached into the old
    # single-tab attributes keep working unchanged.
    # ------------------------------------------------------------------ #

    @property
    def _tab_id(self) -> int | None:
        return self._default_tab.tab_id

    @property
    def _current_url(self) -> str | None:
        return self._default_tab.current_url

    @_current_url.setter
    def _current_url(self, url: str | None) -> None:
        self._default_tab.current_url = url

    @property
    def _tab_owned(self) -> bool:
        return self._default_tab._owned

    @property
    def _detached(self) -> threading.Event:
        return self._default_tab._detached

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Dial the bridge loopback socket, authenticate, and start the reader.

        Idempotent: a no-op if already connected. Safe to call again after a
        :meth:`close` or a transport loss (the reader thread that set
        ``_closed`` has its own daemon lifetime; a fresh ``connect`` allocates a
        new socket + reader and clears ``_closed``), which is what
        :meth:`reconnect` relies on to re-dial a Chrome-spawned host that came
        back after a service-worker eviction.

        The host drops any client whose FIRST frame is not the shared-secret
        token, so this sends ``{"auth": "<hex>"}`` before anything else. The
        token file is only meaningful once the host is up, so it is read AFTER
        the dial succeeds — a missing/stale token then surfaces as an actionable
        :class:`BridgeError` distinct from a plain "could not connect". The token
        is re-read on every (re)connect, so a host that rotated it on restart is
        picked up transparently. One caveat: the host answers a WRONG token with
        silence (by design it sends no auth reply either way), so a stale token
        that still exists on disk surfaces not here but as the connection dying
        on the first op ("bridge reader stopped") — reconnecting re-reads the
        fresh token and heals it.
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
        wfile = sock.makefile("wb")
        # First frame MUST be the auth token or the host drops us.
        try:
            token = self._read_token()
            write_frame(wfile, {"auth": token})
        except BridgeError:
            self._abort_connect(sock, rfile, wfile)
            raise
        except (FrameError, OSError, ValueError) as exc:
            self._abort_connect(sock, rfile, wfile)
            raise BridgeError(
                f"failed to send auth handshake to bridge at "
                f"{self._host}:{self._port}: {exc}"
            ) from exc
        self._sock = sock
        self._rfile = rfile
        self._wfile = wfile
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
            # Parked detaches belong to the dead connection; a fresh browser
            # session may even reuse its tab ids, so never let them leak.
            self._undispatched_detaches.clear()
        # Pass this connection's rfile + generation explicitly: a stale reader
        # from a prior socket must read ITS OWN rfile and only touch shared state
        # while still current, so a reconnect's new reader is never corrupted.
        self._reader = threading.Thread(
            target=self._read_loop, args=(rfile, my_gen),
            name="chrome-bridge-reader", daemon=True,
        )
        self._reader.start()
        log.debug("chrome-bridge connected to bridge at %s:%d", self._host, self._port)

    @staticmethod
    def _abort_connect(sock: socket.socket, *files: BinaryIO) -> None:
        """Close a half-open socket + its file objects after a failed handshake."""
        for f in files:
            try:
                f.close()
            except OSError:
                pass
        try:
            sock.close()
        except OSError:
            pass

    def _read_token(self) -> str:
        """Read this endpoint's shared-secret token for the auth handshake.

        Resolution mirrors the host: a named ``profile`` maps straight to
        :func:`chrome_bridge.profiles.token_path`; a host/port-only client looks
        up the registry for a profile bound to this port (the ``chrome-bridge``
        CLI passes host+port even for ``--profile`` usage) and falls back to the
        port-derived filename when none is registered. A missing/unreadable/empty
        token surfaces as an actionable :class:`BridgeError`.
        """
        from .. import profiles as _profiles

        if self._profile is not None:
            path = _profiles.token_path(self._profile)
        else:
            try:
                name = _profiles.find_profile_by_port(self._port)
            except (OSError, ValueError):
                # A corrupt/unreadable registry must degrade to the port-derived
                # file (mirroring the host's resolution), not mask itself as an
                # auth-handshake failure.
                name = None
            path = (
                _profiles.token_path(name)
                if name is not None
                else _profiles.port_token_path(self._port)
            )
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BridgeError(
                f"could not read the bridge auth token at {path}: {exc}. The "
                "bridge may not have started yet, or the token is stale — reload "
                "the Chrome Bridge extension for this profile (chrome://extensions) "
                "so the host regenerates it, then retry."
            ) from exc
        if not token:
            raise BridgeError(
                f"the bridge auth token at {path} is empty; reload the Chrome "
                "Bridge extension for this profile so the host regenerates it, "
                "then retry."
            )
        return token

    def reconnect(self) -> None:
        """Re-dial the bridge socket after a transport loss, then re-attach
        ALL tabs.

        The MV3 service worker can be evicted (the documented #2 risk). When it
        is, the native port Chrome spawned drops, the host's relay ends, and this
        client's loopback socket dies — the reader thread sets ``_closed`` and
        every primitive then raises "bridge is not connected". Unlike an
        ``onDetach`` (one tab gone, socket alive — handled per tab by the
        idempotent re-attach), a dead socket kills every attachment at once and
        needs the **whole connection** rebuilt before any op can flow.

        This tears down the dead socket and dials a fresh one (the
        Chrome-spawned host is back as soon as the worker re-runs ``connect()``
        in ``sw.js`` and re-binds the loopback port). A fresh socket means no
        attached tabs — whatever the old socket held is at best a
        generation-fenced leftover server-side — so every handle (the default
        tab and each :meth:`new_tab` handle) that had a ``current_url`` is
        re-opened eagerly, in creation order, remapping its ``tab_id`` onto
        the fresh Chrome tab so callers' :class:`Tab` references keep
        working. Each re-open's load wait is capped at the short
        :data:`DEFAULT_REATTACH_TIMEOUT` (non-fatal — the caller's own
        readiness check is the real gate), and ALL the heals share one
        budget — a single connection op allowance plus that load ceiling per
        restorable tab, with every heal leg re-clamped to what remains of it
        as the leg starts — so a wedged worker cannot stack full per-op
        timeouts, within one tab's heal or across N tabs; a heal that misses
        the budget is skipped or cut short and runs lazily on that tab's
        next primitive. Heals are never STARTED with less than a minimal
        budget share (:data:`_MIN_HEAL_BUDGET`): an ``open`` the worker
        answers only after the client's clamp expired still creates a tab
        server-side that no handle owns (its late reply is dropped) — the
        next key-less reaper open reclaims such leftovers, and the floor
        keeps that window small. The FIRST re-open goes out in the
        key-less reaper shape (``replace:true`` on the wire — nothing else is
        attached yet, and it reclaims the dead session's fenced leftover tabs
        server-side); every later one is ``replace:false`` so it never tears
        down an already-healed sibling (:meth:`Tab._reopen_replace`).

        Re-opens are best-effort PER TAB: one tab's failed heal is logged and
        must not poison its siblings — the failed handle stays unattached and
        retries transparently on its next primitive. A handle that never
        opened a URL has nothing to restore and is marked dead: its next
        primitive raises an actionable :class:`BridgeError`, and an explicit
        open/navigate/attach starts a fresh session on it. A long-lived owner
        reusing one client across jobs calls this instead of discarding the
        client, so ``current_url`` is preserved across the heal.

        Raises :class:`BridgeError` if the socket cannot be re-dialled (host not
        back yet); the caller surfaces it and retries later.
        """
        # A whole-connection rebuild is a tab-lifecycle op: hold the exclusive
        # side of the connection lock so the teardown + redial + heals can
        # never interleave with in-flight per-tab ops (the heals' own _open /
        # Page.enable legs nest reentrantly).
        with self._conn_lock.exclusive():
            self._reconnect_locked()

    def _reconnect_if_dead(self) -> bool:
        """:meth:`reconnect`, unless another thread already rebuilt the
        connection. Returns ``True`` iff THIS call ran the rebuild.

        The transparent-heal paths (:meth:`Tab.navigate`'s dead-socket branch,
        ``_ensure_attached``) call this instead of :meth:`reconnect`: with one
        thread per tab, N threads observe the same dead socket and would each
        run a full teardown + rebuild in turn — every loser destroying the
        fresh socket (and the freshly healed Chrome tabs, via its reaper
        re-open) the winner just built. Checking liveness under the exclusive
        grip makes the decision race-free: by the time a loser gets the grip,
        the winner's heal is complete, so a live connection means the rebuild
        this caller wanted has already happened. The return value tells the
        caller WHO rebuilt — a loser must not infer from shared state what
        the winner's heal did (see :meth:`Tab.navigate`'s fall-through). The
        public :meth:`reconnect` keeps its unconditional force-rebuild
        semantics for direct callers.
        """
        with self._conn_lock.exclusive():
            if self.is_alive():
                return False
            self._reconnect_locked()
            return True

    def _reconnect_locked(self) -> None:
        # Drop the dead socket without trying its close-op (the peer is gone).
        self._teardown_socket()
        self.connect()
        with self._lock:
            tabs = list(self._tabs)  # creation order: default first, then new_tab
        # A fresh socket means no attached tabs. Null every stale id BEFORE the
        # first re-open so its adaptive replace shape sees no phantom "attached"
        # sibling and goes out as the key-less reaper (each _open below clears
        # the detach flag it sets here).
        for tab in tabs:
            tab._mark_detached()
        # One shared heal budget across ALL re-opens, mirroring close()/
        # detach(): with a wedged worker, per-tab op timeouts would stack (N
        # tabs -> up to 2N full op timeouts), stalling the primitive that
        # triggered this heal for minutes. The budget is one connection op
        # allowance for all the op round-trips plus the spec'd short load
        # ceiling per restorable tab; a heal that cannot run within it is
        # skipped and that tab re-attaches lazily on its next primitive.
        n_restorable = sum(1 for t in tabs if t.current_url is not None)
        deadline = (
            time.monotonic()
            + self._op_timeout
            + n_restorable * DEFAULT_REATTACH_TIMEOUT
        )
        for tab in tabs:
            if tab.tab_id is not None:
                continue  # a concurrent primitive already healed this handle
            if tab.current_url is None:
                # Nothing to restore: dead until an explicit open/navigate/
                # attach. The next primitive raises this as an actionable
                # BridgeError instead of silently running against no tab.
                # navigate() is the safe revival (adaptive replace:false);
                # attach() and the facade open() also revive but are
                # single-tab and tear down the siblings this reconnect just
                # restored.
                tab._dead_reason = (
                    "bridge reconnected after a connection loss, but this tab "
                    "had no URL to restore (it was never opened, or was "
                    "attach()-joined — attach records no URL by design); "
                    "navigate() it to open a fresh page, or attach() to join "
                    "the user's active tab — attach() and the facade open() "
                    "are single-tab and tear down sibling tabs"
                )
                continue
            if deadline - time.monotonic() < _MIN_HEAL_BUDGET:
                # Starting a heal with a near-zero grant would still WRITE the
                # open frame; a slow worker then creates a tab whose late
                # reply is dropped — an orphan. Skip instead; the handle
                # re-attaches lazily with a full op timeout on its next use.
                log.warning(
                    "chrome-bridge: reconnect heal budget too low to start "
                    "a re-open; %s re-attaches on its next use",
                    tab.current_url,
                )
                continue
            log.info(
                "chrome-bridge: reconnected; re-attaching to %s", tab.current_url
            )
            try:
                tab._open(
                    tab.current_url,
                    timeout=DEFAULT_REATTACH_TIMEOUT,
                    deadline=deadline,
                )
            except BridgeError as exc:
                # Best-effort per tab: one tab's failed heal must not poison
                # the siblings.
                if tab.tab_id is not None:
                    # The open leg succeeded (id published); a LATER leg —
                    # the Page.enable ack — was cut short by the budget. The
                    # handle IS attached; only Page events may be missing
                    # until Page.enable next goes out on it (an in-place
                    # navigate re-issues it; the early-return heal paths,
                    # which see an attached handle, do not).
                    log.warning(
                        "chrome-bridge: re-attach of %s cut short after "
                        "attach (Page events may be missing): %s",
                        tab.current_url,
                        exc,
                    )
                else:
                    # The handle stays unattached; its next primitive
                    # retries via _ensure_attached.
                    log.warning(
                        "chrome-bridge: re-attach of %s failed (will retry "
                        "on next use): %s",
                        tab.current_url,
                        exc,
                    )

    def close(self) -> None:
        """Best-effort end the session — closing all open tabs — then tear down
        the socket.

        Sends one ``close`` op per attached tab (the default tab and every
        :meth:`new_tab` handle): the extension detaches the debugger and
        removes each tab only if it was bridge-created (:meth:`open` /
        :meth:`new_tab`) — a user tab joined via :meth:`attach` is detached
        but left open. Safe to call more than once and even if no tab was
        ever opened/attached.
        """
        if self._sock is None:
            return
        # Session teardown is a tab-lifecycle op: the exclusive grip waits out
        # (and then excludes) every in-flight per-tab op, so the close ops and
        # the registry reset below cannot interleave with concurrent drivers.
        with self._conn_lock.exclusive():
            with self._lock:
                tabs = list(self._tabs)
            # One shared op-timeout budget across ALL tab teardowns: with a
            # wedged worker, per-tab timeouts would stack (N tabs -> N x
            # op_timeout inside __exit__); the shared deadline keeps the worst
            # case at the legacy single-tab bound.
            deadline = time.monotonic() + self._op_timeout
            for tab in tabs:
                # Snapshot the id: the reader thread (a detach event) can null
                # it mid-loop, and a close that then went out WITHOUT tabId
                # would be resolved by the extension onto whatever single tab
                # remains — closing the wrong tab.
                tab_id = tab.tab_id
                # Close each tab cleanly; never let teardown errors propagate.
                if tab_id is not None and not self._closed.is_set():
                    try:
                        self._request(
                            {"op": "close"},
                            timeout=max(0.0, deadline - time.monotonic()),
                            tab_id=tab_id,
                        )
                    except BridgeError as exc:
                        log.debug("chrome-bridge close op failed (ignored): %s", exc)
                tab.tab_id = None
                tab._owned = False
                tab.current_url = None
                tab._dead_reason = None  # explicit teardown supersedes a dead mark
            self._retire_extra_tabs()
            self._teardown_socket()

    def detach(self) -> None:
        """End the session, always leaving the tabs open, then drop the socket.

        Sends one ``detach`` op per attached tab: the extension detaches the
        debugger and clears its per-tab reference without removing the tab —
        even a bridge-created one (:meth:`open` / :meth:`new_tab`). This
        matters beyond politeness: a stale server-side reference would make
        the *next* session's ``attach`` teardown remove a tab this session
        promised to leave open. Use :meth:`close` when bridge-created tabs
        should be removed.
        """
        if self._sock is None:
            return
        # Same lifecycle-op exclusion, shared teardown budget, and id-snapshot
        # rationale as close().
        with self._conn_lock.exclusive():
            with self._lock:
                tabs = list(self._tabs)
            deadline = time.monotonic() + self._op_timeout
            for tab in tabs:
                tab_id = tab.tab_id
                if tab_id is not None and not self._closed.is_set():
                    try:
                        self._request(
                            {"op": "detach"},
                            timeout=max(0.0, deadline - time.monotonic()),
                            tab_id=tab_id,
                        )
                    except BridgeError as exc:
                        log.debug("chrome-bridge detach op failed (ignored): %s", exc)
                tab.tab_id = None
                tab._owned = False
                tab.current_url = None
                tab._dead_reason = None  # explicit teardown supersedes a dead mark
            self._retire_extra_tabs()
            self._teardown_socket()

    def _teardown_socket(self) -> None:
        """Drop the socket + reader without issuing a tab-close op.

        Shared by :meth:`close` (after its best-effort close-ops) and
        :meth:`reconnect` (where the peer is already gone, so no close-op is
        possible). Sets ``_closed``, nulls the socket handles, aborts the socket
        best-effort, and fails any still-pending futures. Leaves each tab's
        ``current_url`` untouched so a reconnect can re-attach to it.
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
        ``_closed`` and drops the socket. Tabs with a ``current_url`` heal
        transparently — their next primitive rebuilds the connection via
        :meth:`reconnect` — but a session that never opened a URL cannot, so a
        long-lived owner can poll this to decide when to re-open explicitly.
        """
        return self._sock is not None and not self._closed.is_set()

    def __enter__(self) -> ChromeBridge:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Reader thread: route replies to futures, events to the buses
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
            self._pending_tab.clear()
        exc = BridgeError("bridge reader stopped")
        for _id, fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    def _dispatch(self, frame: dict[str, Any], generation: int) -> None:
        # A frame read by a stale reader (its socket died after a reconnect)
        # must never be routed into the live connection's pending map / event
        # buses — it belongs to the dead session. Drop it.
        with self._lock:
            if generation != self._generation:
                return
        if frame.get("event") is True:
            self._dispatch_event(frame, generation)
            return
        # A reply: correlate by id.
        msg_id = frame.get("id")
        with self._lock:
            fut = self._pending.pop(msg_id, None) if msg_id is not None else None
            if msg_id is not None:
                self._pending_tab.pop(msg_id, None)
        if fut is None:
            log.debug("chrome-bridge: reply for unknown id %r (dropped)", msg_id)
            return
        if not fut.done():
            fut.set_result(frame)

    def _dispatch_event(self, frame: dict[str, Any], generation: int) -> None:
        """Route an unsolicited event frame to the connection bus + tab buses.

        Events from the extension carry the source ``tabId`` (see the sw.js
        WIRE PROTOCOL block; ``sessionGeneration`` is consumed and stripped by
        nmh.py before frames reach this client), which routes them to the
        matching :class:`Tab` handle. An event WITHOUT ``tabId`` — a
        single-tab-era peer — is broadcast to every handle, preserving the
        historical behavior. The connection-wide bus always fires.

        ``generation`` is re-checked inside the routing critical sections:
        :meth:`_dispatch`'s up-front check releases ``_lock`` before this
        runs, so a stale reader could otherwise be preempted across a
        reconnect and then park/route a dead session's event into the fresh
        connection's state.
        """
        etype = frame.get("type")
        if etype == "detached":
            reason = frame.get("reason", "unknown")
            tab_id = frame.get("tabId")
            log.info("chrome-bridge: debugger detached (%s)", reason)
            with self._lock:
                if generation != self._generation:
                    return  # stale reader: never touch the fresh connection
                if tab_id is None:
                    targets = list(self._tabs)
                else:
                    targets = [t for t in self._tabs if t.tab_id == tab_id]
                    if not targets:
                        # No handle has published this tabId (yet): an
                        # open/attach reply may still be crossing the op
                        # thread, whose handle would otherwise never learn of
                        # this detach and believe itself attached forever.
                        # Park it; _open/_attach consume it atomically (under
                        # this same lock) when publishing the id. An entry
                        # whose reply was lost (op timeout) lingers until the
                        # next (re)connect clears the map — inert, as Chrome
                        # never reuses tab ids within one browser session.
                        self._undispatched_detaches[tab_id] = reason
            if tab_id is not None and not targets:
                log.debug(
                    "chrome-bridge: parked detach for unpublished tab %r", tab_id
                )
            # The tab is gone; mark so the next primitive on it re-opens.
            for tab in targets:
                tab._mark_detached()
            # Fail in-flight ops so they don't hang on replies that will never
            # arrive: only the detached tab's ops for a tabId-tagged event —
            # other tabs' in-flight ops are unaffected — but ALL of them for a
            # legacy untagged one (a single-tab wire has only one session).
            exc = DetachedError(f"debugger detached: {reason}")
            if tab_id is None:
                self._fail_all_pending(exc)
            else:
                self._fail_pending_for_tab(tab_id, exc)
            params: dict[str, Any] = {"reason": reason}
            if tab_id is not None:
                params["tabId"] = tab_id
            self._fire("__detached__", params)
            for tab in targets:
                tab._fire("__detached__", params)
            return
        if etype == "cdp":
            method = frame.get("method", "")
            params = frame.get("params") or {}
            tab_id = frame.get("tabId")
            with self._lock:
                if generation != self._generation:
                    return  # stale reader: never route into the fresh connection
                targets = [
                    t for t in self._tabs if tab_id is None or t.tab_id == tab_id
                ]
            if tab_id is not None and not targets:
                # A tabId no live handle has published: a late event from a
                # tab that already detached (or a pre-eviction session whose
                # ids were since remapped). Routing it anywhere — even the
                # connection-wide bus — could hand it to a listener observing
                # a different tab, so drop it. A freshly OPENED tab cannot
                # lose events here (no CDP domain is enabled until the
                # post-publish Page.enable). The one real window is the
                # attach handover: an already-attached tab keeps its enabled
                # domains while _attach unpublishes the prior owner's id
                # before publishing its own, so an event landing in that
                # instant is dropped — acceptable, as no handle owns the id
                # at that moment (the same rationale as parking detaches).
                log.debug(
                    "chrome-bridge: dropping event %s for unknown tab %r",
                    method,
                    tab_id,
                )
                return
            # Connection-wide bus first (the legacy surface sees every tab's
            # events), then per-tab routing.
            self._fire(method, params)
            for tab in targets:
                tab._fire(method, params)

    def _fire(self, method: str, params: dict[str, Any]) -> None:
        _fire_listeners(self._lock, self._event_listeners, method, params)

    def _fail_all_pending(self, exc: Exception) -> None:
        """Resolve every pending future with ``exc`` so blocked callers wake."""
        with self._lock:
            pending = list(self._pending.items())
            self._pending.clear()
            self._pending_tab.clear()
        for _id, fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    def _fail_pending_for_tab(self, tab_id: int, exc: Exception) -> None:
        """Resolve the pending futures whose ops target ``tab_id`` with ``exc``.

        A per-tab detach must wake only THAT tab's in-flight ops; ops on other
        tabs (or tab-less ops) keep awaiting their replies.
        """
        with self._lock:
            ids = [i for i, t in self._pending_tab.items() if t == tab_id]
            pending = [
                (i, self._pending.pop(i)) for i in ids if i in self._pending
            ]
            for i in ids:
                self._pending_tab.pop(i, None)
        for _id, fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    # ------------------------------------------------------------------ #
    # Event bus (public, connection-wide)
    # ------------------------------------------------------------------ #

    def on_event(self, method: str, callback) -> None:
        """Register ``callback(params)`` for a raw CDP event ``method``.

        Connection-wide: fires for the matching events of EVERY attached tab
        (use :meth:`Tab.on_event` to observe a single tab). An event tagged
        with a ``tabId`` that no live handle has published is dropped
        entirely and does NOT reach this bus (see the module failure-model
        notes on stale events). Use ``"__detached__"`` to observe debugger
        detach (``params`` has ``reason``, plus ``tabId`` when the event was
        tagged with one).
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
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        tab_id: int | None = None,
    ) -> dict[str, Any]:
        """Send one op frame, await its correlated reply, return the reply dict.

        ``tab_id`` — when given — is stamped into the frame as ``"tabId"`` so
        ``cdp``/``close``/``detach`` ops address one specific attached tab
        (see the sw.js WIRE PROTOCOL block), and is recorded beside the
        pending future so a detach of THAT tab fails this op promptly.

        Raises :class:`BridgeError` on an ``ok:false`` reply or transport loss,
        :class:`BridgeTimeoutError` if no reply arrives within ``timeout``.
        """
        if self._sock is None or self._closed.is_set():
            raise BridgeError("bridge is not connected")
        timeout = self._op_timeout if timeout is None else timeout
        # Hold the shared side of the connection lock across the round-trip:
        # ops pipeline freely across tabs, but never fly while a lifecycle op
        # (exclusive holder) is mutating the session table. Reentrant, so
        # callers already holding a grip (Tab._cdp's shared, a lifecycle op's
        # exclusive) nest harmlessly. Same-tab FIFO is Tab._op_lock's job.
        with self._conn_lock.shared():
            msg_id = next(self._ids)
            frame = {"id": msg_id, **payload}
            if tab_id is not None:
                frame["tabId"] = tab_id
            fut: Future[dict[str, Any]] = Future()
            with self._lock:
                # Re-check under the lock: a lifecycle op (close/detach/
                # reconnect) can tear the socket down between the entry check
                # above and this point — writer preference deliberately queues
                # this thread behind it at the shared grip — leaving _wfile
                # None. Surface the documented BridgeError, not an
                # AttributeError from write_frame(None, ...).
                if self._wfile is None or self._closed.is_set():
                    raise BridgeError("bridge is not connected")
                self._pending[msg_id] = fut
                self._pending_tab[msg_id] = tab_id
                try:
                    write_frame(self._wfile, frame)
                except (FrameError, OSError, ValueError) as exc:
                    self._pending.pop(msg_id, None)
                    self._pending_tab.pop(msg_id, None)
                    raise BridgeError(
                        f"failed to send op {payload.get('op')}: {exc}"
                    ) from exc
            try:
                reply = fut.result(timeout=timeout)
            except FutureTimeoutError as exc:
                with self._lock:
                    self._pending.pop(msg_id, None)
                    self._pending_tab.pop(msg_id, None)
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

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #

    def new_tab(self, url: str, *, timeout: float = DEFAULT_NAV_TIMEOUT) -> Tab:
        """Open ``url`` in an ADDITIONAL attached tab and return its handle.

        Sends ``open`` with ``replace:false`` — existing attached tabs,
        including the default tab, are left untouched — then enables ``Page``
        events on the new tab and awaits its load event (non-fatal, mirroring
        the facade :meth:`open` semantics per tab; ``timeout`` caps that load
        wait). Ops on the returned :class:`Tab` are FIFO among themselves and
        pipeline in parallel with other tabs' ops — one thread per tab is the
        supported driving pattern (see the module docstring).

        The lifecycle-op exclusion lives in :meth:`Tab._open`: its open
        round-trip + publish run under the exclusive side of the connection
        lock, so it cannot interleave with in-flight ops or a concurrent
        (re)open computing its ``replace`` shape. The registry append below is
        ``_lock``-guarded and safe on its own — an unpublished handle (no
        ``tab_id`` yet) is invisible to sibling-liveness checks and event
        routing.

        Raises :class:`BridgeError` if the open fails; the handle is not
        registered in that case.
        """
        tab = Tab(self, is_default=False)
        with self._lock:
            self._tabs.append(tab)
        try:
            tab._open(url, timeout=timeout, replace=False)
        except BaseException as exc:
            # A failed open must not leak a half-alive handle: if the tab was
            # created server-side before the failure (e.g. Page.enable died),
            # best-effort close it, then unregister the handle. The network
            # cleanup runs only for ordinary Exceptions — a KeyboardInterrupt/
            # SystemExit must not block on another op round-trip first.
            tab_id = tab.tab_id  # snapshot: the reader may null it concurrently
            if isinstance(exc, Exception) and tab_id is not None and self.is_alive():
                try:
                    self._request(
                        {"op": "close"}, timeout=self._op_timeout, tab_id=tab_id
                    )
                except BridgeError as cleanup_exc:
                    log.debug(
                        "chrome-bridge new_tab cleanup close failed (ignored): %s",
                        cleanup_exc,
                    )
            with self._lock:
                if tab in self._tabs:
                    self._tabs.remove(tab)
            raise
        return tab

    def _other_tabs_attached(self, tab: Tab) -> bool:
        """True when any OTHER registered handle currently holds an attached tab."""
        with self._lock:
            return any(t is not tab and t.tab_id is not None for t in self._tabs)

    def _retire_extra_tabs(self) -> None:
        """Permanently retire every non-default handle and reset the registry.

        A connection teardown (:meth:`close` / :meth:`detach`) invalidates
        additional tabs for good — unlike the default tab, which backs the
        facade and stays reusable across close()/connect() cycles, a stale
        :meth:`new_tab` handle must not linger in the registry (leaking into
        event routing and the sibling-liveness check) or silently re-open a
        tab on a future connection. Touching one afterwards raises the same
        "tab is closed" error as :meth:`Tab.close`.
        """
        with self._lock:
            for tab in self._tabs:
                if tab is not self._default_tab:
                    tab._handle_closed = True
            self._tabs = [self._default_tab]

    def _mark_other_tabs_detached(self, keep: Tab) -> None:
        """Mark every handle except ``keep`` detached.

        Mirrors the extension's single-tab teardown (a ``replace:true`` open,
        or a successful ``attach``, tears down every other attached tab) —
        the extension emits NO ``detached`` events for its own teardown, so
        the client records it itself. A marked handle re-opens
        (``replace:false``) on its next use.
        """
        with self._lock:
            tabs = [t for t in self._tabs if t is not keep]
        for tab in tabs:
            tab._mark_detached()

    # ------------------------------------------------------------------ #
    # Single-tab facade (delegates to the internal default tab)
    # ------------------------------------------------------------------ #

    def open(self, url: str, *, timeout: float = DEFAULT_NAV_TIMEOUT) -> None:
        """Open ``url`` in a fresh attached tab (single-tab mode), await load.

        The historical facade: drives the internal default :class:`Tab` with
        the wire-default ``replace:true`` open — the extension tears down
        every currently attached tab FIRST (bridge-created ones are removed),
        which also makes this the reaper for leftover tabs from a dead
        session. Any :meth:`new_tab` handles are therefore marked detached
        client-side (the extension emits no events for its own teardown);
        touching one afterwards re-opens it in a fresh tab. The mark happens
        inside ``_open``'s exclusive section (``single_tab=True``), atomically
        with the ``replace`` computation, so a concurrent ``new_tab`` can
        never be silently spared or orphaned. The teardown mirror still
        applies even when the open then fails (server-side the replace:true
        teardown runs BEFORE the tab is created); a spurious mark only costs
        a handle one harmless extra re-attach, the safe direction to err.
        """
        self._default_tab._open(url, timeout=timeout, single_tab=True)

    def attach(self, *, timeout: float | None = None) -> None:
        """Attach the debugger to the user's current active tab.

        The page the user is looking at becomes the default tab, and ``Page``
        events are enabled on it. "Active tab" means the active tab of the
        profile's *last-focused* Chrome window (the extension's service worker
        has no window of its own) — with several windows open, that is the one
        most recently in the foreground. Unlike :meth:`open`, the tab is NOT
        bridge-created: :meth:`close` (and the context-manager exit) detaches
        but leaves it open, as does :meth:`detach`.

        Always single-tab, like the facade :meth:`open`: once the new
        attachment is secured the extension tears down every OTHER attached
        tab, so any :meth:`new_tab` handles are marked detached client-side
        (a FAILED attach leaves them fully intact).

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
        self._default_tab._attach(timeout=timeout)

    def navigate(self, url: str, *, timeout: float = DEFAULT_NAV_TIMEOUT) -> None:
        """Navigate the default tab to ``url``; see :meth:`Tab.navigate`."""
        self._default_tab.navigate(url, timeout=timeout)

    def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        """Evaluate JS in the default tab; see :meth:`Tab.evaluate`."""
        return self._default_tab.evaluate(expression, timeout=timeout)

    def wait_selector(
        self,
        selector: str,
        *,
        timeout: float = 15.0,
        poll_interval: float = 0.3,
    ) -> bool:
        """Poll the default tab for ``selector``; see :meth:`Tab.wait_selector`."""
        return self._default_tab.wait_selector(
            selector, timeout=timeout, poll_interval=poll_interval
        )

    def outer_html(
        self, container: str = DEFAULT_CONTAINER_SELECTOR, *, timeout: float | None = None
    ) -> str:
        """Read ``container``'s outerHTML in the default tab; see :meth:`Tab.outer_html`."""
        return self._default_tab.outer_html(container, timeout=timeout)

    def click_text(
        self, text: str, *, tag: str | None = None, timeout: float | None = None
    ) -> bool:
        """Click by visible text in the default tab; see :meth:`Tab.click_text`."""
        return self._default_tab.click_text(text, tag=tag, timeout=timeout)

    def click_selector(
        self, selector: str, *, timeout: float | None = None
    ) -> bool:
        """Click by selector in the default tab; see :meth:`Tab.click_selector`."""
        return self._default_tab.click_selector(selector, timeout=timeout)

    def query_text(
        self, selector: str, *, timeout: float | None = None
    ) -> str | None:
        """Read an element's text in the default tab; see :meth:`Tab.query_text`."""
        return self._default_tab.query_text(selector, timeout=timeout)

    def get_attribute(
        self, selector: str, name: str, *, timeout: float | None = None
    ) -> str | None:
        """Read an attribute in the default tab; see :meth:`Tab.get_attribute`."""
        return self._default_tab.get_attribute(selector, name, timeout=timeout)

    def is_visible(
        self, selector: str, *, timeout: float | None = None
    ) -> bool:
        """Element visibility in the default tab; see :meth:`Tab.is_visible`."""
        return self._default_tab.is_visible(selector, timeout=timeout)

    def text_visible(
        self,
        text: str,
        *,
        tag: str | None = None,
        contains: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Text visibility in the default tab; see :meth:`Tab.text_visible`."""
        return self._default_tab.text_visible(
            text, tag=tag, contains=contains, timeout=timeout
        )

    def input_value(
        self, selector: str, *, timeout: float | None = None
    ) -> str | None:
        """Read an input's value in the default tab; see :meth:`Tab.input_value`."""
        return self._default_tab.input_value(selector, timeout=timeout)

    def fill(
        self, selector: str, text: str, *, timeout: float | None = None
    ) -> bool:
        """Fill an input in the default tab; see :meth:`Tab.fill`."""
        return self._default_tab.fill(selector, text, timeout=timeout)

    def banner_present(
        self, selector: str, *, timeout: float | None = None
    ) -> bool:
        """One-shot selector check in the default tab; see :meth:`Tab.banner_present`."""
        return self._default_tab.banner_present(selector, timeout=timeout)

    def get_cookies(
        self, urls: list[str] | None = None, *, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        """Read cookies via the default tab; see :meth:`Tab.get_cookies`."""
        return self._default_tab.get_cookies(urls, timeout=timeout)


class Tab:
    """Handle for ONE attached tab: page state + the page primitives.

    Obtained from :meth:`ChromeBridge.new_tab` (additional tabs); the legacy
    single-tab facade drives an internal default Tab through the same
    machinery. Every op stamps this tab's ``tabId`` into its wire frames.
    Ops on this tab are FIFO (its own ``_op_lock`` is held across each whole
    round-trip) and pipeline in parallel with other tabs' ops; one thread per
    tab is the supported driving pattern (see the module docstring).

    Public state: ``tab_id`` (``None`` while unattached) and ``current_url``
    (the last script-requested URL — the idempotent re-attach target).

    Lifecycle: a ``detached`` event for this tab marks the handle detached;
    the next primitive re-attaches idempotently by re-opening ``current_url``
    (with ``replace:false`` whenever other tabs are live, so a re-attach never
    tears siblings down). :meth:`close` ends the tab and permanently retires
    the handle — subsequent primitives raise :class:`BridgeError`.
    """

    def __init__(self, bridge: ChromeBridge, *, is_default: bool = False) -> None:
        self._bridge = bridge
        self._is_default = is_default
        self.tab_id: int | None = None
        self.current_url: str | None = None
        # True when this tab was bridge-created (open); False when the bridge
        # merely attached to the user's active tab (attach). Mirrors sw.js's
        # per-entry bridgeCreated — which is what actually gates tab removal
        # server-side — and is kept client-side for introspection and tests;
        # the close-vs-detach choice is the caller's, via which method it
        # invokes.
        self._owned = False
        # Flips on this tab's onDetach so primitives can re-attach
        # idempotently before their next op.
        self._detached = threading.Event()
        # Set by ChromeBridge.reconnect() when it found nothing to restore for
        # this handle (no URL was ever opened on it): the next primitive
        # raises it as an actionable BridgeError. An explicit open/navigate/
        # attach starts a fresh session and clears it.
        self._dead_reason: str | None = None
        # Flipped by close(): the handle is permanently unusable.
        self._handle_closed = False
        # Per-tab op lock: held (with the connection lock's shared side, taken
        # FIRST — see _RWLock's lock-order note) across each whole op/reply
        # round-trip, so this tab's ops are FIFO while other tabs' ops
        # pipeline in parallel. Reentrant for the same reason the old
        # connection-wide op lock was: primitives issue several ops back to
        # back, and a nested _request must never self-deadlock.
        self._op_lock = threading.RLock()
        # Per-tab event bus: raw CDP events routed to this tab by tabId, plus
        # "__detached__". Guarded by the bridge's _lock.
        self._event_listeners: dict[str, list] = {}

    # ------------------------------------------------------------------ #
    # Handle lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Close this tab and permanently retire the handle.

        Sends ``close`` with this tab's ``tabId`` — the extension detaches
        the debugger and, for a bridge-created tab, removes it — best-effort,
        then marks the handle unusable: every subsequent primitive raises
        :class:`BridgeError`. Idempotent. The default tab backing the legacy
        facade cannot be closed this way (use :meth:`ChromeBridge.close`).
        """
        if self._is_default:
            raise BridgeError(
                "the default tab cannot be closed; use ChromeBridge.close()"
            )
        bridge = self._bridge
        # Retiring a tab is a lifecycle op: the exclusive grip waits out this
        # tab's own in-flight op (and everyone else's), so the close op and
        # the registry removal cannot interleave with concurrent drivers.
        with bridge._conn_lock.exclusive():
            # Check-and-flip under the lock so concurrent close() calls stay
            # idempotent (exactly one sends the close op).
            with bridge._lock:
                if self._handle_closed:
                    return
                self._handle_closed = True
            # Snapshot the id: the reader can null it concurrently, and a
            # close that went out WITHOUT tabId would be resolved onto
            # whatever single tab remains attached — closing the wrong tab.
            tab_id = self.tab_id
            if tab_id is not None and bridge.is_alive():
                try:
                    bridge._request(
                        {"op": "close"}, timeout=bridge._op_timeout, tab_id=tab_id
                    )
                except BridgeError as exc:
                    log.debug("chrome-bridge tab close op failed (ignored): %s", exc)
            self.tab_id = None
            self._owned = False
            self.current_url = None
            # Unregister so events stop routing here and the handle can be GC'd.
            with bridge._lock:
                if self in bridge._tabs:
                    bridge._tabs.remove(self)

    def _check_usable(self) -> None:
        if self._handle_closed:
            raise BridgeError("tab is closed; this Tab handle is no longer usable")

    def _mark_detached(self) -> None:
        """Record that this tab's attachment is gone (its tab may be too)."""
        self._detached.set()
        self.tab_id = None
        self._owned = False

    def _reopen_replace(self) -> bool | None:
        """The ``replace`` flag for re-opens issued through this handle.

        ``None`` means omit the key (the wire default, ``replace:true`` — the
        single-tab reaper). A (re)open issued while OTHER tabs are attached
        must be ``replace:false`` so it never tears siblings down; with
        nothing else attached the reaper shape is kept, so the first re-open
        after a detach or reconnect also reclaims generation-fenced leftover
        tabs server-side (the wire designates a ``replace:true`` open as the
        only reaper for a dead session's tabs). :meth:`ChromeBridge.new_tab`'s
        INITIAL open bypasses this and is always explicit ``replace:false`` —
        opening an additional tab is never a reaping event.
        """
        return False if self._bridge._other_tabs_attached(self) else None

    def _leg_timeout(self, deadline: float | None) -> float:
        """One heal leg's op timeout: the connection default, re-clamped to
        whatever remains of ``deadline`` at the moment the leg starts.

        Re-clamping PER LEG (rather than granting each leg a clamp computed
        once up front) is what makes :meth:`ChromeBridge.reconnect`'s shared
        heal budget a real bound: an earlier leg that consumed most of the
        budget leaves the later legs only the remainder, so two op timeouts
        can never stack past the deadline within one tab's heal.
        """
        op_timeout = self._bridge._op_timeout
        if deadline is None:
            return op_timeout
        return min(op_timeout, max(0.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------ #
    # Per-tab event bus
    # ------------------------------------------------------------------ #

    def on_event(self, method: str, callback) -> None:
        """Register ``callback(params)`` for THIS tab's raw CDP event ``method``.

        Fires for events the extension tagged with this tab's ``tabId`` (and,
        for compatibility with single-tab-era peers, untagged events, which
        are broadcast to every handle). Use ``"__detached__"`` to observe this
        tab's debugger detach (``params`` has ``reason``).
        """
        with self._bridge._lock:
            self._event_listeners.setdefault(method, []).append(callback)

    def off_event(self, method: str, callback) -> None:
        with self._bridge._lock:
            listeners = self._event_listeners.get(method)
            if listeners and callback in listeners:
                listeners.remove(callback)

    def _fire(self, method: str, params: dict[str, Any]) -> None:
        _fire_listeners(self._bridge._lock, self._event_listeners, method, params)

    # ------------------------------------------------------------------ #
    # Low-level per-tab ops
    # ------------------------------------------------------------------ #

    def _cdp(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a raw CDP command on this tab; return its ``result``.

        ``tab_id`` is snapshotted ONCE: the reader thread (a detach event) or
        a sibling facade ``open``/``attach`` can null it between a primitive's
        ``_ensure_attached`` and this send, and a frame that then went out
        WITHOUT ``tabId`` would be resolved by the extension onto whatever
        single tab remains attached — silently executing on the wrong tab.
        Raising :class:`DetachedError` instead lands the race on the
        documented retry path (the next primitive re-attaches).
        """
        tab_id = self.tab_id
        if tab_id is None:
            raise DetachedError(
                "debugger detached: this tab is no longer attached"
            )
        bridge = self._bridge
        # Shared connection grip FIRST, then this tab's own op lock (the
        # module-wide lock order — see _RWLock): held across the whole
        # round-trip, so this tab's ops are FIFO while other tabs' ops
        # pipeline in parallel and lifecycle ops (exclusive holders) wait
        # for the reply to land.
        with bridge._conn_lock.shared():
            with self._op_lock:
                reply = bridge._request(
                    {"op": "cdp", "method": method, "params": params or {}},
                    timeout=timeout,
                    tab_id=tab_id,
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
    # Open / attach / navigate
    # ------------------------------------------------------------------ #

    def _open(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_NAV_TIMEOUT,
        replace: Any = _ADAPTIVE_REPLACE,
        deadline: float | None = None,
        single_tab: bool = False,
    ) -> None:
        """Open ``url`` in a fresh attached tab for this handle, await load.

        Mirrors a Playwright ``page.goto`` for a freshly opened page: it asks
        the extension to create+attach a background tab at ``url`` (so the
        first navigation is the open itself), enables ``Page`` events, and
        waits for ``Page.loadEventFired``. ``replace`` defaults to the
        adaptive :meth:`_reopen_replace` shape (``replace:false`` while
        siblings are attached, the key-less reaper otherwise);
        :meth:`ChromeBridge.new_tab` pins its initial open to ``False``.

        ``timeout`` caps only the load wait. ``deadline`` — when given — is a
        ``time.monotonic`` deadline that re-clamps EACH leg (the ``open`` op,
        ``Page.enable``, and the load wait) to whatever remains of it as that
        leg starts, an op leg never exceeding the connection default;
        :meth:`ChromeBridge.reconnect` passes its shared heal budget so a
        wedged worker cannot stack full per-op timeouts, within one tab's
        heal or across N tabs. ``single_tab`` is the facade
        :meth:`ChromeBridge.open` contract: mark every sibling handle
        detached INSIDE the exclusive section, atomically with the ``replace``
        computation, so the open goes out as the reaper and no interleaving
        with a concurrent ``new_tab`` can downgrade it to ``replace:false``
        or leave a marked-but-still-attached orphan tab behind.
        """
        self._check_usable()
        # The extension's "open" both creates the tab AND navigates it to `url`,
        # so we register the load listener before enabling Page events to avoid a
        # race where the load fires before we subscribe.
        loaded = threading.Event()

        def _on_load(_params: dict[str, Any]) -> None:
            loaded.set()

        self.on_event("Page.loadEventFired", _on_load)
        try:
            payload: dict[str, Any] = {"op": "open", "url": url}
            bridge = self._bridge
            # An "open" mutates the session table (its replace:true shape is
            # the tear-everything-down reaper), so the round-trip is a
            # lifecycle op: the exclusive grip waits out every in-flight op
            # and excludes concurrent opens/attaches. Computing the adaptive
            # replace shape UNDER it means no sibling can BECOME attached
            # before this frame is sent (attaching requires its own exclusive
            # round-trip), so a key-less reaper open can never race a
            # concurrent new_tab and silently destroy its fresh tab. Drift
            # the other way (a sibling detaching after the snapshot) errs to
            # replace:false, which is harmless. Reentrant: reconnect's heals
            # and new_tab arrive here holding no grip or the exclusive one,
            # and _request's shared grip nests under it. The exclusive
            # section deliberately ends BEFORE the Page.enable leg and the
            # load wait below, so a slow page never stalls other tabs.
            with bridge._conn_lock.exclusive():
                # Clear the detach flag BEFORE issuing the open so that a
                # detach the reader observes for the NEW tab *during* this
                # open (e.g. the SW is evicted again mid-re-attach) survives
                # and forces another re-attach. Clearing it after the request
                # could erase a fresh detach the reader had just set, leaving
                # tab_id stale and the session falsely "attached"; a
                # spuriously-set flag only costs one harmless extra
                # re-attach. Cleared UNDER the grip so a sibling's single-tab
                # mark can never land between this clear and the publish
                # below — that interleaving would leave a healthy handle
                # flagged detached while its live tab stays attached
                # server-side, an orphan no handle (and no close()) reclaims.
                self._detached.clear()
                if single_tab:
                    # The facade open's single-tab contract, applied
                    # atomically with the replace computation: mark every
                    # sibling detached (the server-side replace:true teardown
                    # emits no events for its own teardown), which makes the
                    # adaptive shape below the reaper. A concurrent new_tab
                    # either published first — and is marked and reaped here,
                    # consistently — or opens after this reaper as an
                    # additional replace:false tab; it can never be silently
                    # spared (downgrading this open to replace:false was the
                    # old unlocked-mark race) or orphaned.
                    bridge._mark_other_tabs_detached(self)
                if replace is _ADAPTIVE_REPLACE:
                    replace = self._reopen_replace()
                if replace is not None:
                    payload["replace"] = replace
                reply = bridge._request(
                    payload, timeout=self._leg_timeout(deadline)
                )
                tab_id = reply.get("tabId")
                if tab_id is None:
                    raise BridgeError(
                        "bridge 'open' reply carried no tabId; cannot attach to the tab"
                    )
                # Publish while STILL holding the exclusive grip: a sibling's
                # adaptive re-open computes its replace shape under this same
                # lock, so it can never read this handle as unattached in the
                # gap between our reply and our publish and go out as a reaper.
                self._publish_tab_id(tab_id, during="open")
            # Revival of a dead handle is complete only once the new id is
            # published: a FAILED open must leave the dead mark in place so
            # the next primitive keeps raising the actionable dead error, not
            # a vaguer DetachedError.
            self._dead_reason = None
            self._owned = True
            self.current_url = url
            self._cdp("Page.enable", timeout=self._leg_timeout(deadline))
            # The page may already have loaded between open and Page.enable; in
            # that case loadEventFired won't fire again, so a timeout here is not
            # fatal — wait_selector is the real readiness gate a script should use.
            load_timeout = timeout
            if deadline is not None:
                load_timeout = min(
                    load_timeout, max(0.0, deadline - time.monotonic())
                )
            self._wait_load(loaded, load_timeout)
        finally:
            self.off_event("Page.loadEventFired", _on_load)

    def _publish_tab_id(self, tab_id: int, *, during: str) -> None:
        """Set ``self.tab_id`` from an open/attach reply — unless its detach
        already arrived.

        The reader routes ``detached`` events by matching handles' published
        ``tab_id``; an event that raced this reply (dispatched before the op
        thread resumed) matched nothing and was parked in the bridge's
        ``_undispatched_detaches``. Publish-or-consume must be atomic under
        the same lock as the reader's route-or-park decision, or a detach
        landing between the two would be lost and the handle would believe
        itself attached to a session the extension already discarded —
        permanently, since nothing would ever set ``_detached``. Consuming
        raises :class:`DetachedError`; the handle stays detached so the next
        primitive re-attaches (or the caller retries).
        """
        bridge = self._bridge
        with bridge._lock:
            reason = bridge._undispatched_detaches.pop(tab_id, None)
            if reason is None:
                self.tab_id = tab_id
        if reason is not None:
            self._detached.set()
            # The connection-wide bus already fired at event time; this tab's
            # own bus could not (the id was unpublished), so honour the
            # Tab.on_event contract now.
            self._fire("__detached__", {"reason": reason, "tabId": tab_id})
            raise DetachedError(f"debugger detached during {during}: {reason}")

    def _attach(self, *, timeout: float | None = None) -> None:
        """Attach this handle to the user's current active tab.

        The wire ``attach`` op is inherently single-tab (the extension tears
        down every OTHER attached tab once the new attachment is secured), so
        only the default tab uses it — via the :meth:`ChromeBridge.attach`
        facade, whose docstring carries the caller-facing contract.
        """
        self._check_usable()
        # An "attach" mutates the session table (its success tears down every
        # other attached tab), so its round-trip + publish are a lifecycle op
        # under the exclusive grip, like _open's. The Page.enable leg below
        # runs outside it, as an ordinary per-tab op.
        with self._bridge._conn_lock.exclusive():
            # Same rationale (and same under-the-grip placement) as _open():
            # clear the detach flag BEFORE the request so a detach observed
            # for the new tab during this attach survives, and so a sibling's
            # single-tab mark can never land between this clear and the
            # publish, falsely flagging the fresh attachment detached.
            self._detached.clear()
            reply = self._bridge._request({"op": "attach"}, timeout=timeout)
            tab_id = reply.get("tabId")
            if tab_id is None:
                raise BridgeError(
                    "bridge 'attach' reply carried no tabId; cannot attach to the tab"
                )
            self._owned = False
            # No URL bookkeeping: an attached user tab must never be silently
            # re-opened after a detach (see ChromeBridge.attach).
            self.current_url = None
            # attach is single-tab: on SUCCESS the extension tears down every
            # other attached tab (acquire-before-release — a failed attach
            # leaves them intact, hence marking only now, and BEFORE the
            # publish below, which may raise if the new tab's own detach raced
            # the reply). It emits no detached events for its own teardown, so
            # mirror it client-side.
            self._bridge._mark_other_tabs_detached(self)
            self._publish_tab_id(tab_id, during="attach")
            # Revival of a dead handle is complete only once the id is
            # published (see _open): a failed attach keeps the actionable
            # dead error.
            self._dead_reason = None
        try:
            self._cdp("Page.enable")
        except BridgeError:
            # The attach reply's tabId can ALIAS a tab another handle already
            # held (the extension's attach handover): a detach racing the
            # reply is then routed to that handle rather than parked, so the
            # id published above is already dead and Page.enable is the first
            # op to notice. Mark this handle detached so it lands on the
            # documented detached path (next primitive raises DetachedError /
            # re-attaches after a navigate) instead of holding a stale tab_id
            # forever. A spurious mark only costs one extra re-attach.
            self._mark_detached()
            raise

    def navigate(self, url: str, *, timeout: float = DEFAULT_NAV_TIMEOUT) -> None:
        """Navigate this tab to ``url`` and await the load event.

        Re-attaches idempotently if the tab is gone (no tab yet, or a prior
        ``onDetach``); otherwise issues ``Page.navigate`` on the existing tab.
        This is the in-session equivalent of Playwright ``page.goto``.

        A dead loopback socket (the service worker was evicted, dropping the
        native port and thus the host's relay) is healed first: record ``url``
        so the rebuilt session lands on it, reconnect (which re-opens EVERY
        tab that had a URL, this one included), and the page is then open and
        loaded on a fresh tab. On that heal path ``timeout`` does not govern
        ``reconnect``'s eager re-attach load waits (those use the short
        :data:`DEFAULT_REATTACH_TIMEOUT` ceiling per tab; only the in-call
        fallback retry below honours ``timeout``), which is harmless:
        the re-attach load wait is non-fatal and ``wait_selector`` is the real
        readiness gate a script should apply next.
        """
        self._check_usable()
        bridge = self._bridge
        if bridge._sock is None or bridge._closed.is_set():
            # No live socket to navigate over: rebuild the whole connection and
            # land on `url`. Set current_url first so the reconnect's eager
            # re-open of this tab targets the requested page, not a stale
            # prior one. _reconnect_if_dead coalesces with a concurrent
            # thread's reconnect rather than tearing down what it just built.
            self.current_url = url
            healed_by_us = bridge._reconnect_if_dead()
            if self.tab_id is None:
                # The heal is best-effort per tab; if this tab's eager
                # re-open failed (or a coalesced reconnect ran before our
                # current_url write and skipped it), retry here so the error
                # (if any) surfaces on this call.
                self._open(url, timeout=timeout)
                return
            if healed_by_us:
                # OUR rebuild's heal loop read the current_url written above
                # (no other thread writes this tab's current_url under the
                # one-thread-per-tab pattern), so the tab is freshly open on
                # `url`.
                return
            # A concurrent thread's reconnect rebuilt the connection. Whether
            # its heal read this tab's PRIOR url or the one written above is
            # a race we must not guess from current_url (this thread just
            # wrote it, so equality proves nothing). Fall through to an
            # ordinary in-place navigate — worst case one redundant same-URL
            # load; correct in every interleaving.
        elif self.tab_id is None or self._detached.is_set():
            self._open(url, timeout=timeout)
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
            self.current_url = url
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

    # ------------------------------------------------------------------ #
    # Core primitives
    # ------------------------------------------------------------------ #

    def evaluate(self, expression: str, *, timeout: float | None = None) -> Any:
        """Evaluate a JS ``expression`` in this tab's page and return its value.

        The public counterpart of the internal evaluate primitive: it first
        ensures the session is attached (re-attaching idempotently where
        possible), then runs ``Runtime.evaluate`` with ``returnByValue``.
        Raises :class:`BridgeError` if the page threw.
        """
        self._ensure_attached()
        return self._evaluate(expression, timeout=timeout)

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
            # conditions this bridge exists to survive. (A re-attach's op
            # legs are the documented exception — see _ensure_attached.)
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
                present = self._evaluate(
                    expr, timeout=min(self._bridge._op_timeout, remaining)
                )
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
        scope the read to specific origins; omit it to read this tab's
        cookies.

        ``Network.getCookies`` is a one-shot command and does **not** need
        ``Network.enable`` — deliberately not enabling it keeps the bridge from
        being flooded with per-request network events on the tab.
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
        """Resume this tab's session if an eviction/detach left it unusable.

        The MV3 service worker can be evicted mid-job (the documented #2 risk).
        It surfaces two ways, handled idempotently here so the **next** primitive
        transparently resumes with no human click:

        * **onDetach, socket still alive** — this tab is gone but the loopback
          host is up. Re-open the tab on its last URL and re-enable ``Page``
          events ("re-open and re-Page.enable rather than assume the prior
          tabs are still attached", per the wire contract). Other attached
          tabs are untouched: the re-open goes out ``replace:false`` whenever
          siblings are live (:meth:`_reopen_replace`).
        * **socket dead** — eviction also dropped the native port Chrome spawned,
          so the host's relay ended and this client's loopback socket died. A
          dead socket cannot carry an ``open``; rebuild the whole connection via
          the coalescing ``_reconnect_if_dead`` (one rebuild even when N tab
          threads observe the same dead socket; the winner re-dials and eagerly
          re-opens EVERY tab that had a URL, in creation order, remapping tab
          ids — including this one, so it normally returns ready) before any
          op. If
          this tab's eager re-open failed (reconnect heals best-effort per
          tab), it falls through to the re-open below as the retry. A
          long-lived owner reusing one client thus self-heals an evicted
          worker without being discarded and rebuilt.

        ``timeout`` caps the re-attach LOAD wait; callers running on a
        deadline (e.g. :meth:`wait_selector`) pass their remaining budget so
        the load wait cannot blow past their bound. It is clamped to a small
        ceiling (:data:`DEFAULT_REATTACH_TIMEOUT`) since the re-attach load
        event is non-fatal anyway — the caller's own readiness check is the
        real gate. The re-open's op round-trips still use the connection op
        timeout, so against a wedged worker the heal can overshoot a small
        caller ``timeout`` (bounded by two op timeouts — not caller-tight).
        The dead-socket leg likewise runs under ``reconnect``'s own shared
        heal budget (one op allowance plus the short load ceiling per
        restorable tab), which bounds it but can still exceed a small caller
        ``timeout``.

        Sessions joined via :meth:`ChromeBridge.attach` record no URL, so a
        detach of the user's tab (socket still alive) surfaces here as
        :class:`DetachedError` — by design the bridge never silently re-opens
        a user tab. After a socket-death recovery the same no-URL situation
        surfaces instead as the reconnect dead mark: a handle marked dead
        raises its stored actionable :class:`BridgeError` here, and an
        explicit open/navigate/attach revives it.
        """
        self._check_usable()
        if self._dead_reason is not None:
            # Set by reconnect() when there was nothing to restore. Effective
            # only while the handle still HAS nothing: a concurrent revival
            # (open/navigate/attach) can race the mark, in which case it is
            # stale — discard it rather than fail a healthy handle.
            if self.tab_id is None and self.current_url is None:
                raise BridgeError(self._dead_reason)
            self._dead_reason = None
        bridge = self._bridge
        # A dead socket must be rebuilt first: reconnect() re-dials and
        # re-attaches every tab that had a URL — including this one, which
        # then returns ready below. Without a URL there is nothing to resume —
        # surface the loss rather than silently reconnect to an unattached
        # session.
        if bridge._sock is None or bridge._closed.is_set():
            if self.current_url is None:
                raise BridgeError(
                    "bridge connection lost and no URL to re-attach to "
                    "(call open/navigate/attach first)"
                )
            # Coalesce with a concurrent thread's reconnect (one thread per
            # tab means N threads can observe the same dead socket): only
            # one rebuild runs; losers find the fresh connection healed.
            bridge._reconnect_if_dead()
            if self.tab_id is not None:
                return  # the reconnect re-opened this tab
        if self.tab_id is not None and not self._detached.is_set():
            return
        if self.current_url is None:
            raise DetachedError(
                "debugger detached and no URL to re-attach to "
                "(call open/navigate/attach first)"
            )
        load_timeout = DEFAULT_REATTACH_TIMEOUT
        if timeout is not None:
            load_timeout = max(0.0, min(DEFAULT_REATTACH_TIMEOUT, timeout))
        log.info("chrome-bridge: re-attaching to %s after detach", self.current_url)
        self._open(self.current_url, timeout=load_timeout)
