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

from .frames import FrameError, read_frame, write_frame

logger = logging.getLogger(__name__)

# Loopback endpoint the client dials into. Loopback-only so the bridge is never
# reachable off-host (it can drive a real, logged-in browser tab).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


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


def _pump(src: BinaryIO, dst: BinaryIO, *, name: str, on_close: threading.Event) -> None:
    """Relay frames from ``src`` to ``dst`` until either side closes or errors.

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
            try:
                write_frame(dst, frame)
            except FrameError as exc:
                # An engine -> extension frame over Chrome's 1 MB host->extension
                # cap: it was read fine (under the larger read guard) but cannot
                # be sent to Chrome. Drop just this frame and keep relaying; the
                # dst stream stays aligned because encode_frame checks the cap
                # before any byte is written. The client's per-id timeout (see
                # sw.js WIRE PROTOCOL) covers the missing reply; callers should
                # keep host->extension reads (e.g. outer_html) scoped to stay
                # under the cap.
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


def _serve_session(
    ext_in: BinaryIO,
    ext_out: BinaryIO,
    client_in: BinaryIO,
    client_out: BinaryIO,
    client_sock: socket.socket | None = None,
) -> None:
    """Relay frames in both directions until either side closes.

    Both directions run on their own *daemon* thread sharing a ``closed`` event;
    the caller waits on that event and returns as soon as the **first** direction
    ends — it never joins either pump. A pump spends most of its time blocked
    inside a read where it cannot observe ``closed``, and there is no portable way
    to abort the surviving pump's parked read:

    * ext->client is parked reading the extension stdio pipe, which cannot be
      ``shutdown`` at all.
    * client->ext is parked in a socket ``recv()``; ``shutdown(SHUT_RDWR)`` aborts
      that read on Linux but NOT on Winsock (where a parked recv only wakes when
      the *peer* closes). We still issue the shutdown as a best-effort nudge.

    Because neither parked read can be reliably aborted, joining would block the
    caller until the surviving peer closes on its own — on Windows, the full
    duration of every normal (extension-first) Chrome teardown, while a
    long-lived client keeps client->ext parked. So we leave both pumps as
    fire-and-forget daemons: ``closed`` is set, the host runs one session per
    process and is exiting, each pump dies when its peer closes, and the loopback
    port is freed immediately for the next host (no lingering owner).

    ``client_sock`` may be None in tests that drive the pumps over plain file
    objects; the daemon threads still let the call return promptly.
    """
    closed = threading.Event()
    t_up = threading.Thread(
        target=_pump,
        args=(client_in, ext_out),
        kwargs={"name": "client->ext", "on_close": closed},
        daemon=True,
    )
    t_down = threading.Thread(
        target=_pump,
        args=(ext_in, client_out),
        kwargs={"name": "ext->client", "on_close": closed},
        daemon=True,
    )
    t_up.start()
    t_down.start()
    # Block until the first direction ends (each _pump sets `closed` on exit).
    closed.wait()
    # Best-effort nudge for the client->ext pump if it is parked in a recv():
    # this aborts the read on platforms where shutting the local end unblocks a
    # parked recv (Linux), but NOT on Winsock, where a parked recv only wakes
    # when the *peer* closes. We therefore do NOT join either pump: both are
    # fire-and-forget daemons. `closed` is already set, the host runs one session
    # per process (main() has no loop) and is on its way out, and each pump dies
    # on its own when its peer closes. Joining here would block the full timeout
    # on every normal (extension-first) Chrome teardown while a long-lived
    # client peer keeps client->ext's recv parked — holding the loopback port the
    # next host needs. Returning immediately frees it.
    if client_sock is not None:
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed / never connected


def _run_echo(ext_in: BinaryIO, ext_out: BinaryIO) -> None:
    """Write every extension frame straight back (install smoke test).

    Unlike the relay's drop-and-continue, echo just stops on any send failure:
    it only reflects extension->host frames (already under Chrome's cap), so an
    oversize write is not expected, and the mode exists purely for a quick
    connectNative round-trip rather than resilient long-running operation.
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


def _accept_client(host: str, port: int) -> tuple[socket.socket, BinaryIO, BinaryIO]:
    """Bind the loopback endpoint and block until the client connects.

    Returns the accepted client socket plus its binary read/write file objects.
    The listening socket is closed once the single client is accepted (one
    client connection per host process; Chrome restarts the host per session).

    We deliberately do NOT set ``SO_REUSEADDR``: this is an ephemeral,
    Chrome-spawned host with no restart-with-TIME_WAIT use case, and on Windows
    ``SO_REUSEADDR`` would let a *second* concurrently-launched host bind the
    same port (the MV3 service worker reconnects on an alarm, so overlapping
    host processes are a normal occurrence). Without it, the second ``bind``
    fails with ``EADDRINUSE`` and that host exits cleanly, leaving a single
    owner of the port and preserving the single-tab invariant.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((host, port))
    listener.listen(1)
    logger.info("bridge listening for client on %s:%d", host, port)
    try:
        client, addr = listener.accept()
    finally:
        listener.close()
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
        client, client_in, client_out = _accept_client(host, port)
    except OSError as exc:
        # EADDRINUSE: another host already owns the port — exit cleanly so a
        # single host stays the owner (see _accept_client on SO_REUSEADDR).
        logger.error("could not accept client connection on %s:%d: %s", host, port, exc)
        return 1
    try:
        _serve_session(ext_in, ext_out, client_in, client_out, client)
    finally:
        for closeable in (client_in, client_out, client):
            try:
                closeable.close()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
