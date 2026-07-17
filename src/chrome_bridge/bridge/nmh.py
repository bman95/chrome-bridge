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
dials into. Exactly one client connection is served at a time: request ids
are client-chosen and the generation fence assumes one live client session,
so a second concurrent client would interleave its frames into the one relay
stream and corrupt the id space and the session-table ownership (see the
sw.js WIRE PROTOCOL note). The rule is about there being ONE CLIENT SESSION,
not about request count: within it the client may hold several attached tabs
and keep one request in flight per tab (ops on one tab are FIFO; ops across
tabs interleave — see ``docs/multi-tab.md``); the relay itself is a
transparent frame pump either way.

Client authentication (client -> host only; the sw.js wire contract is
unchanged)
    Loopback is not a trust boundary on a shared machine — on WSL2 the Windows
    host and Linux share ``127.0.0.1``, so any local process could race to
    connect and pull cookies off the real profile. So on startup the host
    writes a random ``0600`` token file (see ``chrome_bridge.profiles``), and
    the **first frame** from every accepted client must be
    ``{"auth": "<hex>"}`` matching that token. On a mismatch, a malformed
    frame, or a read that stalls past a short timeout, the host closes that
    client and loops back to accept — the extension side is never disturbed and
    the auth frame is never relayed to the extension. On success the host sends
    **no reply**; the relay simply starts.

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

import hmac
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from .. import profiles
from .frames import (
    LENGTH_PREFIX_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_READ_BYTES,
    FrameError,
    read_frame,
    write_frame,
)

logger = logging.getLogger(__name__)

# Loopback endpoint the client dials into. Loopback-only so the bridge is never
# reachable off-host (it can drive a real, logged-in browser tab).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_SESSION_ID_MARKER = "chrome-bridge-session"

# Wall-clock budget for a client's whole auth handshake (its first frame). Short
# so an unauthenticated connect-and-close (e.g. `chrome-bridge status`, or a
# local process probing the port) can never wedge the accept loop.
AUTH_TIMEOUT = 5.0
# Hard cap on the auth frame size. A legitimate frame is ~80 bytes
# ({"auth": "<64 hex chars>"}); anything bigger is an attack or a confusion and
# is rejected on the length prefix alone, before any body byte is read.
MAX_AUTH_FRAME_BYTES = 1024


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


def _token_path() -> Path:
    """Resolve where THIS host writes its auth token.

    Resolution mirrors the client's (``client.ChromeBridge._read_token``) so
    both sides always land on the same file: the ``CBR_BRIDGE_PROFILE`` env var
    wins (``install.write_launcher`` bakes it into the launcher); otherwise a
    profile registered on this port; otherwise the port-derived filename, so
    ``--host/--port``-only setups still authenticate.

    An invalid env value is ignored (with a warning) rather than honoured: the
    profile-name grammar is what guarantees the port-derived filename can never
    collide (see ``profiles.port_token_path``). A corrupt registry likewise
    degrades to the port-derived file instead of crashing a Chrome-spawned host
    whose only failure symptom would be connectNative silently dropping.
    """
    profile = os.getenv("CBR_BRIDGE_PROFILE")
    if profile:
        try:
            profiles.validate_profile_name(profile)
        except profiles.ProfileError:
            logger.warning(
                "ignoring invalid CBR_BRIDGE_PROFILE %r; using a port-derived token file",
                profile,
            )
        else:
            return profiles.token_path(profile)
    _, port = _client_endpoint()
    try:
        name = profiles.find_profile_by_port(port)
    except (OSError, ValueError):
        name = None
    if name is not None:
        return profiles.token_path(name)
    return profiles.port_token_path(port)


def _write_token(path: Path) -> str:
    """Generate a fresh random token and write it to ``path`` with ``0600`` perms.

    The token is 32 bytes from ``os.urandom`` (hex-encoded, 64 chars). Called
    only AFTER the listener bind succeeds, so a losing host in a bind race (a
    normal MV3 respawn — see ``_accept_client`` on SO_REUSEADDR) exits before
    ever touching the live owner's token.

    The secret is written to a fresh ``O_EXCL``-created ``0600`` temp inode and
    atomically ``os.replace``d into place. Never reusing a pre-existing inode
    matters twice over: a stale token file with looser permissions must not
    receive the new secret (``O_TRUNC`` would write into the loose inode, and a
    process already holding an fd to it would read every future token), and a
    client re-reading the token mid-rotation must never observe a truncated or
    empty file. On Windows ``chmod`` only toggles the read-only bit; there the
    config directory's own ACL is the protection (see the README security
    section).
    """
    token = os.urandom(32).hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)  # leftover from a crashed prior run
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    try:
        os.chmod(tmp, 0o600)  # belt-and-braces against an odd umask
    except OSError as exc:  # best-effort; e.g. a filesystem that ignores chmod
        logger.debug("could not chmod token file %s: %s", tmp, exc)
    os.replace(tmp, path)
    return token


def _recv_exact(client: socket.socket, n: int, deadline: float) -> bytes | None:
    """Read exactly ``n`` bytes off the raw socket before a wall-clock deadline.

    Returns ``None`` if the peer closes first. Raises ``socket.timeout`` (an
    ``OSError``) once ``deadline`` passes. The per-``recv`` timeout is re-armed
    with the *remaining* budget on every iteration, so a client dribbling one
    byte per interval cannot keep resetting the clock — a plain ``settimeout``
    is an idle limit per ``recv``, not a bound on the whole read.
    """
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("auth handshake deadline expired")
        client.settimeout(remaining)
        chunk = client.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _authenticate_client(
    client: socket.socket,
    expected_token: str,
    *,
    timeout: float = AUTH_TIMEOUT,
) -> bool:
    """Consume and validate the client's first frame as the auth handshake.

    Returns ``True`` iff that frame is ``{"auth": "<hex>"}`` matching
    ``expected_token``. A mismatch, a malformed/non-object frame, a first frame
    without a string ``auth``, an oversize length prefix, an early close, or a
    handshake that outlasts ``timeout`` all return ``False`` — the caller then
    closes the client and loops back to accept, so the extension side is never
    disturbed and the auth frame is never relayed. On success the host sends NO
    reply; the relay starts.

    The whole handshake is bounded by one wall-clock deadline enforced in
    :func:`_recv_exact` — this host authenticates on its single accept thread,
    so a slow-dripping client must not be able to hold it open indefinitely.
    The frame is read raw off the socket, consuming exactly the frame's bytes:
    anything the client pipelined after it is untouched and reaches the relay's
    buffered reader (which has not read anything yet) intact.
    """
    deadline = time.monotonic() + timeout
    try:
        header = _recv_exact(client, LENGTH_PREFIX_BYTES, deadline)
        if header is None:
            logger.warning("auth: rejecting client (closed before auth frame)")
            return False
        (length,) = struct.unpack("<I", header)
        if length > MAX_AUTH_FRAME_BYTES:
            logger.warning(
                "auth: rejecting client (first frame of %d bytes exceeds the "
                "%d-byte auth cap)",
                length,
                MAX_AUTH_FRAME_BYTES,
            )
            return False
        body = _recv_exact(client, length, deadline)
        if body is None:
            logger.warning("auth: rejecting client (closed mid auth frame)")
            return False
        frame = json.loads(body.decode("utf-8"))
    except (OSError, ValueError) as exc:
        # OSError covers the handshake deadline (socket.timeout) and a broken
        # pipe; ValueError covers undecodable bytes / invalid JSON.
        logger.warning("auth: rejecting client (no valid first frame): %s", exc)
        return False
    finally:
        # Restore blocking mode for the relay's per-op reads.
        client.settimeout(None)
    if not isinstance(frame, dict):
        logger.warning("auth: rejecting client (first frame is not an object)")
        return False
    token = frame.get("auth")
    # Compare as bytes: compare_digest on str raises TypeError for non-ASCII
    # input, and a client-supplied token containing an unpaired surrogate can't
    # even encode — both must reject, never crash the accept loop.
    try:
        supplied = token.encode("utf-8") if isinstance(token, str) else None
    except UnicodeEncodeError:
        supplied = None
    if supplied is None or not hmac.compare_digest(supplied, expected_token.encode("utf-8")):
        logger.warning("auth: rejecting client (token mismatch or malformed)")
        return False
    logger.info("auth: client authenticated")
    return True


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

    def __init__(
        self, ext_in: BinaryIO, *, on_close: Callable[[], object] | None = None
    ) -> None:
        self._ext_in = ext_in
        self._on_close = on_close
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
                elif frame.get("event") is True:
                    if frame.get("sessionGeneration") != active_generation:
                        logger.debug("ext->client: dropping stale or unfenced event")
                        continue
                    frame = dict(frame)
                    frame.pop("sessionGeneration", None)
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
            if self._on_close is not None:
                try:
                    self._on_close()
                except OSError:
                    pass
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
    # accepted; otherwise an old operation could interleave bytes into the
    # extension stream during the next session. shutdown() wakes the blocked
    # read on POSIX but NOT on Windows, where a recv already blocked in
    # another thread keeps blocking — there, closing the OS handle is what
    # aborts it. detach() first so the socket object (and the makefile
    # wrappers' later close) can never double-close a reused handle. If even
    # that fails to stop the reader, fail closed instead of overlapping
    # sessions.
    t_up.join(timeout=0.5)
    if t_up.is_alive() and client_sock is not None:
        try:
            socket.close(client_sock.detach())
        except OSError:
            pass
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
    logger.info("bridge listening for clients on %s:%d", host, port)
    return listener


def _wake_listener(listener: socket.socket) -> None:
    """Wake a blocking ``accept`` after the extension input pipe closes.

    Falls back to closing the listener when the wake connection cannot be
    made (e.g. the listener is bound to an address the OS refuses to connect
    to, such as 0.0.0.0 on Windows): a closed listener makes the blocked
    ``accept`` raise ``OSError``, which the main loop treats as shutdown once
    the relay is closed — without SOME wake path the host would linger in
    ``accept`` forever after Chrome closed the pipe.
    """
    try:
        wake = socket.create_connection(listener.getsockname(), timeout=1.0)
    except OSError:
        try:
            listener.close()
        except OSError:
            pass
        return
    wake.close()


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
    owner of the port and preserving the one-relay-per-profile invariant.
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
    # Only the port owner writes the token — AFTER a successful bind — so a
    # losing host in a respawn race never clobbers the live owner's token.
    try:
        token = _write_token(_token_path())
    except OSError as exc:
        logger.error("could not write bridge auth token: %s", exc)
        listener.close()
        return 1
    # A short-lived loopback connection is the explicit wakeup for the blocking
    # accept when Chrome closes the Native Messaging input pipe. Closing a
    # listening socket from another thread does not reliably wake accept on all
    # supported OSes; this avoids periodic idle polling and remains portable.
    ext_relay = _ExtensionRelay(ext_in, on_close=lambda: _wake_listener(listener))
    ext_relay.start()
    try:
        while not ext_relay.closed.is_set():
            try:
                client, client_in, client_out = _accept_client(listener)
            except OSError as exc:
                if ext_relay.closed.is_set():
                    break
                logger.error("could not accept client connection: %s", exc)
                return 1
            try:
                # Drop any client that doesn't present the token first; the
                # extension side is never touched and we loop back to accept.
                # (timeout passed explicitly so the module constant is read at
                # call time, not frozen into the default arg at import.)
                if not _authenticate_client(client, token, timeout=AUTH_TIMEOUT):
                    continue
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
