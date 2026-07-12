"""Native Messaging host — ``com.chromebridge.bridge.<profile>``.

Chrome spawns this process when ``extension/sw.js`` calls
``chrome.runtime.connectNative("com.chromebridge.bridge.<profile>")``. Its
stdin/stdout are the Native Messaging pipe to the extension
(4-byte-LE-length-prefixed JSON frames). The host is a **transparent
transport**: it relays whole frames, unmodified, between the extension and the
client library. It never parses op/reply/event shapes — that contract lives in
``extension/sw.js`` and ``chrome_bridge.bridge.client``.

Topology::

    extension (sw.js)  <-- NM stdio -->  nmh.py  <-- loopback TCP -->  client

Chrome owns the host's lifetime, so a script cannot be its parent. The host
therefore listens on a loopback TCP socket (``127.0.0.1``) that the client
dials into. Exactly one client connection is served at a time (the bridge
drives a single Chrome tab; concurrent clients would interleave ops the
single-tab model forbids — see the sw.js WIRE PROTOCOL note).

Each Chrome profile you want to script gets its **own** native host name and
**own** port (see ``docs/multi-profile.md``): Chrome passes no profile-
identifying information to the spawned host process, so the port a given
invocation of this module binds is fixed at registration time via the
``CBR_BRIDGE_PORT`` env var baked into that profile's launcher script by
``chrome-bridge init``.

Run modes
---------
``python -m chrome_bridge.bridge.nmh [native-messaging-manifest-path]``
    Default. Chrome passes the manifest path as ``argv[1]``; it is ignored.
    Relays extension <-> client.

``python -m chrome_bridge.bridge.nmh --echo``
    Self-contained loopback: every frame from the extension is written straight
    back. Lets an install be smoke-tested (``connectNative`` round-trip) with
    no client connected.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from typing import BinaryIO

from .frames import MAX_MESSAGE_BYTES, MAX_READ_BYTES, FrameError, read_frame, write_frame

logger = logging.getLogger(__name__)

# Loopback endpoint the client dials into. Loopback-only so the bridge is never
# reachable off-host (it can drive a real, logged-in browser tab).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_SESSION_ID_MARKER = "chrome-bridge-session"


def _force_binary_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Return ``(stdin, stdout)`` binary streams with newline translation off.

    On Windows the C runtime opens fds 0/1 in text (``O_TEXT``) mode. CPython's
    ``sys.std*.buffer`` does raw I/O that is binary in practice, but the
    Native-Messaging framing is byte-exact, so we defensively force the
    underlying fds to ``O_BINARY`` to guarantee no ``\\n`` -> ``\\r\\n``
    translation ever corrupts a length prefix or JSON body. No-op off Windows.
    """
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return sys.stdin.buffer, sys.stdout.buffer


def _client_endpoint() -> tuple[str, int]:
    host = os.getenv("CBR_BRIDGE_HOST", DEFAULT_HOST)
    try:
        port = int(os.getenv("CBR_BRIDGE_PORT", str(DEFAULT_PORT)))
    except ValueError:
        port = DEFAULT_PORT
    return host, port


def _pump(
    src: BinaryIO,
    dst: BinaryIO,
    *,
    name: str,
    on_close: threading.Event,
    max_write_bytes: int | None = MAX_MESSAGE_BYTES,
    session_generation: int | None = None,
) -> None:
    """Relay frames from ``src`` to ``dst`` until either side closes or errors.

    ``max_write_bytes`` is the per-direction write cap forwarded to
    ``write_frame``. Chrome's 1 MB Native-Messaging cap applies only to
    host->extension messages, so the client->ext pump keeps the default cap
    while the ext->client pump uses ``MAX_READ_BYTES`` (the loopback socket has
    no Chrome cap, but the relay must never emit a frame the peer's own codec
    rejects as over its read guard — that would desync the client, not just
    drop one reply).

    Setting ``on_close`` on exit lets the peer pump and the main thread unwind:
    the first direction to end tears the whole session down so neither side is
    left talking to a half-open pipe.
    """
    try:
        while not on_close.is_set():
            try:
                frame = read_frame(src)
            except FrameError as exc:
                logger.warning("%s: dropping bad frame: %s", name, exc)
                # A bad/oversize frame leaves the stream unaligned; we cannot
                # safely resynchronise, so end this direction.
                break
            if frame is None:
                logger.debug("%s: peer closed", name)
                break
            if session_generation is not None and "id" in frame:
                frame = {
                    **frame,
                    "id": [_SESSION_ID_MARKER, session_generation, frame["id"]],
                }
            try:
                write_frame(dst, frame, max_bytes=max_write_bytes)
            except FrameError as exc:
                # A frame over this direction's write cap: a client->ext frame
                # over Chrome's 1 MB host->extension cap (it was read fine under
                # the larger read guard but cannot be sent to Chrome), or an
                # ext->client frame whose re-encoding outgrew the client's read
                # guard. Drop just this frame and keep relaying; the dst stream
                # stays aligned because encode_frame checks the cap before any
                # byte is written. The client's per-id timeout (see sw.js WIRE
                # PROTOCOL) covers the missing reply. Extension replies (e.g. a
                # large outer_html) relay to the client up to the read guard.
                logger.warning("%s: dropping oversize frame: %s", name, exc)
                continue
            except (OSError, ValueError) as exc:
                # OSError: pipe/socket broke. ValueError: the dst file was closed
                # under us — can happen when this pump is a lingering daemon and
                # the session's teardown already closed the destination. Either
                # way the destination is gone; end this direction quietly.
                logger.debug("%s: write failed: %s", name, exc)
                break
    except (OSError, ValueError) as exc:
        logger.debug("%s: read failed: %s", name, exc)
    finally:
        on_close.set()


class _ExtensionRelay:
    """Own the single extension-input reader for the host process.

    Client sessions come and go, but the Native Messaging stdin pipe is shared
    for the process lifetime and must never have two competing readers.  This
    relay keeps one reader thread alive and routes each frame only to the
    currently attached client session.  Frames received between sessions are
    discarded, so a late reply/event can never leak into the next client.
    """

    def __init__(self, ext_in: BinaryIO) -> None:
        self._ext_in = ext_in
        self._lock = threading.Lock()
        self._client_out: BinaryIO | None = None
        self._session_closed: threading.Event | None = None
        self._generation = 0
        self._active_generation: int | None = None
        self.closed = threading.Event()
        self._started = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def attach(self, client_out: BinaryIO, session_closed: threading.Event) -> int:
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._client_out = client_out
            self._session_closed = session_closed
            self._active_generation = generation
            return generation

    def detach(self, client_out: BinaryIO) -> None:
        with self._lock:
            if self._client_out is client_out:
                self._client_out = None
                self._session_closed = None
                self._active_generation = None

    def _run(self) -> None:
        try:
            while True:
                try:
                    frame = read_frame(self._ext_in)
                except FrameError as exc:
                    logger.warning("ext->client: dropping bad frame: %s", exc)
                    break
                if frame is None:
                    logger.debug("ext->client: extension closed")
                    break

                with self._lock:
                    client_out = self._client_out
                    session_closed = self._session_closed
                    active_generation = self._active_generation
                if client_out is None or session_closed is None:
                    logger.debug("ext->client: dropping frame with no active client")
                    continue
                if "id" in frame:
                    fenced_id = frame["id"]
                    if not (
                        isinstance(fenced_id, list)
                        and len(fenced_id) == 3
                        and fenced_id[0] == _SESSION_ID_MARKER
                        and fenced_id[1] == active_generation
                    ):
                        logger.debug("ext->client: dropping stale or unfenced reply")
                        continue
                    frame = {**frame, "id": fenced_id[2]}
                try:
                    write_frame(client_out, frame, max_bytes=MAX_READ_BYTES)
                except FrameError as exc:
                    logger.warning("ext->client: dropping oversize frame: %s", exc)
                except (OSError, ValueError) as exc:
                    logger.debug("ext->client: write failed: %s", exc)
                    session_closed.set()
        except (OSError, ValueError) as exc:
            logger.debug("ext->client: read failed: %s", exc)
        finally:
            self.closed.set()
            with self._lock:
                session_closed = self._session_closed
            if session_closed is not None:
                session_closed.set()


def _serve_session(
    ext_relay: _ExtensionRelay,
    ext_out: BinaryIO,
    client_in: BinaryIO,
    client_out: BinaryIO,
    client_sock: socket.socket | None = None,
) -> None:
    """Relay one client session while the process-wide relay owns ``ext_in``.

    Only the client->extension direction gets a per-session reader thread.  A
    process-wide :class:`_ExtensionRelay` is the sole reader of extension stdin
    and is atomically attached/detached here.  This prevents a reader left from
    an earlier client from stealing a reply intended for a later session.
    """
    closed = threading.Event()
    generation = ext_relay.attach(client_out, closed)
    ext_relay.start()
    t_up = threading.Thread(
        target=_pump,
        args=(client_in, ext_out),
        kwargs={
            "name": "client->ext",
            "on_close": closed,
            # Writes go to Chrome: enforce the 1 MB host->extension cap.
            "max_write_bytes": MAX_MESSAGE_BYTES,
            "session_generation": generation,
        },
        daemon=True,
    )
    t_up.start()
    closed.wait()
    ext_relay.detach(client_out)
    if client_sock is not None:
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed / never connected
    # A per-session client reader must be gone before another client can be
    # accepted; otherwise an old operation could reach the extension during
    # the next session. Peer close/shutdown wakes this read on supported
    # platforms. If it does not, fail closed instead of overlapping sessions.
    t_up.join(timeout=1.0)
    if t_up.is_alive():
        raise RuntimeError("client reader did not stop; refusing a new session")


def _run_echo(ext_in: BinaryIO, ext_out: BinaryIO) -> None:
    """Write every extension frame straight back (install smoke test).

    Unlike the relay's drop-and-continue, echo just stops on any send failure:
    it reflects extension->host frames (which Chrome does NOT cap at 1 MB) back
    into the genuinely capped host->extension direction, so a >1 MB inbound
    frame cannot be echoed and simply ends the smoke test. The mode exists
    purely for a quick connectNative round-trip rather than resilient
    long-running operation, and real smoke-test frames are tiny.
    """
    while True:
        try:
            frame = read_frame(ext_in)
        except FrameError as exc:
            logger.warning("echo: dropping bad frame: %s", exc)
            break
        if frame is None:
            break
        try:
            write_frame(ext_out, frame)
        except (FrameError, OSError) as exc:
            logger.warning("echo: cannot send frame: %s", exc)
            break


def _bind_listener(host: str, port: int) -> socket.socket:
    """Bind once for the host lifetime, preserving the single-owner invariant."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((host, port))
    listener.listen(1)
    listener.settimeout(0.2)
    logger.info("bridge listening for clients on %s:%d", host, port)
    return listener


def _accept_client(listener: socket.socket) -> tuple[socket.socket, BinaryIO, BinaryIO]:
    """Accept one client from an already-bound process-lifetime listener.

    Returns the accepted client socket plus its binary read/write file objects.
    The listener remains open so the same host can serve the next client after
    this session ends. Sessions are still strictly sequential.

    We deliberately do NOT set ``SO_REUSEADDR``: this is an ephemeral,
    Chrome-spawned host with no restart-with-TIME_WAIT use case, and on Windows
    ``SO_REUSEADDR`` would let a *second* concurrently-launched host bind the
    same port (the MV3 service worker reconnects on an alarm, so overlapping
    host processes are a normal occurrence). Without it, the second ``bind``
    fails with ``EADDRINUSE`` and that host exits cleanly, leaving a single
    owner of the port and preserving the single-tab invariant.
    """
    client, addr = listener.accept()
    logger.info("client connected from %s", addr)
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    rfile = client.makefile("rb")
    wfile = client.makefile("wb")
    return client, rfile, wfile


def _log_level() -> int:
    """Resolve CBR_LOG_LEVEL, falling back to INFO on an unknown value.

    A typo must not crash a Chrome-spawned host (whose only failure symptom
    would be connectNative silently dropping the port).
    """
    name = os.getenv("CBR_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=_log_level(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,  # stdout is the NM pipe — never log there.
    )

    # Force binary stdio so newline translation can never corrupt frames.
    ext_in, ext_out = _force_binary_stdio()

    if "--echo" in argv:
        logger.info("running in --echo mode (no client)")
        _run_echo(ext_in, ext_out)
        return 0

    host, port = _client_endpoint()
    try:
        listener = _bind_listener(host, port)
    except OSError as exc:
        # EADDRINUSE: another host already owns the port — exit cleanly so a
        # single host stays the owner (see _bind_listener on SO_REUSEADDR).
        logger.error("could not accept client connection on %s:%d: %s", host, port, exc)
        return 1
    ext_relay = _ExtensionRelay(ext_in)
    ext_relay.start()
    try:
        while not ext_relay.closed.is_set():
            try:
                client, client_in, client_out = _accept_client(listener)
            except socket.timeout:
                continue
            except OSError as exc:
                if ext_relay.closed.is_set():
                    break
                logger.error("could not accept client connection: %s", exc)
                return 1
            try:
                _serve_session(ext_relay, ext_out, client_in, client_out, client)
            finally:
                for closeable in (client_in, client_out, client):
                    try:
                        closeable.close()
                    except OSError:
                        pass
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
