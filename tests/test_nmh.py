"""Tests for the Native Messaging host relay/echo logic.

These exercise the relay/echo loops directly against a real loopback socket —
no Chrome, no extension.
"""

from __future__ import annotations

import io
import logging
import socket
import struct
import threading
import time

import pytest

from chrome_bridge.bridge import HOST_NAME_PREFIX
from chrome_bridge.bridge import nmh
from chrome_bridge.bridge.frames import read_frame, write_frame, MAX_MESSAGE_BYTES


def test_host_name_prefix_matches_extension_contract():
    assert HOST_NAME_PREFIX == "com.chromebridge.bridge"


# --------------------------------------------------------------------------- #
# Echo mode
# --------------------------------------------------------------------------- #


def test_echo_relays_every_frame_back():
    ext_in = io.BytesIO()
    msgs = [{"id": 1, "op": "cdp"}, {"id": 2, "op": "close"}]
    for m in msgs:
        write_frame(ext_in, m)
    ext_in.seek(0)
    ext_out = io.BytesIO()

    nmh._run_echo(ext_in, ext_out)

    ext_out.seek(0)
    assert [read_frame(ext_out) for _ in msgs] == msgs


def test_echo_stops_on_bad_inbound_frame():
    # A malformed frame leaves the stream unaligned; echo relays what came
    # before it and then stops rather than corrupt the stream.
    ext_in = io.BytesIO()
    write_frame(ext_in, {"ok": True})
    bad = b"not json"
    ext_in.write(struct.pack("<I", len(bad)) + bad)
    ext_in.seek(0)
    ext_out = io.BytesIO()

    nmh._run_echo(ext_in, ext_out)

    ext_out.seek(0)
    assert read_frame(ext_out) == {"ok": True}
    assert read_frame(ext_out) is None  # nothing after the good frame


# --------------------------------------------------------------------------- #
# Bidirectional relay over a real loopback socket
# --------------------------------------------------------------------------- #


def _make_socketpair() -> tuple[socket.socket, socket.socket]:
    """A connected TCP pair on loopback (socket.socketpair is POSIX-only)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    addr = listener.getsockname()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(addr)
    server, _ = listener.accept()
    listener.close()
    return client, server


def _serve_session(
    ext_in, ext_out, client_in, client_out, client_sock=None
):
    """Run one session with the same process-wide extension relay as main()."""
    relay = nmh._ExtensionRelay(ext_in)
    nmh._serve_session(relay, ext_out, client_in, client_out, client_sock)


def test_serve_session_relays_both_directions():
    # Real sockets for BOTH sides so neither stream hits a premature EOF (as a
    # BytesIO would), mirroring production where the extension pipe stays open
    # for the whole session. The extension side is driven by `ext_peer`.
    ext_peer, ext_host = _make_socketpair()
    client_peer, client_host = _make_socketpair()

    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    client_in = client_host.makefile("rb")
    client_out = client_host.makefile("wb")

    ext_peer_w = ext_peer.makefile("wb")
    ext_peer_r = ext_peer.makefile("rb")
    client_peer_r = client_peer.makefile("rb")
    client_peer_w = client_peer.makefile("wb")

    relayed: list[dict] = []

    def extension_behaviour():
        # Read the two client requests. Their ids are fenced on the wire; echo
        # those ids back and the relay restores the caller-visible ids.
        first = read_frame(ext_peer_r)
        second = read_frame(ext_peer_r)
        write_frame(ext_peer_w, {"id": first["id"], "ok": True, "tabId": 7})
        write_frame(
            ext_peer_w,
            {"event": True, "type": "cdp", "method": "X", "params": {}},
        )
        relayed.extend([first, second])
        # Half-close the write side so the host's ext_in sees a clean EOF and the
        # relay unwinds. (A plain socket.close() leaves the open makefile handles
        # holding the fd, so EOF can lag.)
        ext_peer.shutdown(socket.SHUT_WR)

    def client_behaviour():
        write_frame(client_peer_w, {"id": 1, "op": "open", "url": "u"})
        write_frame(client_peer_w, {"id": 2, "op": "close"})
        assert read_frame(client_peer_r) == {"id": 1, "ok": True, "tabId": 7}
        assert read_frame(client_peer_r) == {
            "event": True,
            "type": "cdp",
            "method": "X",
            "params": {},
        }

    te = threading.Thread(target=extension_behaviour, daemon=True)
    tc = threading.Thread(target=client_behaviour, daemon=True)
    tc.start()
    te.start()

    started = time.monotonic()
    _serve_session(ext_in, ext_out, client_in, client_out, client_host)
    elapsed = time.monotonic() - started
    te.join(timeout=5.0)
    tc.join(timeout=5.0)

    # Extension-first teardown must return promptly — it must NOT block on a join
    # of the client->ext pump whose recv() the live client peer keeps parked.
    assert elapsed < 2.0, f"_serve_session lingered {elapsed:.2f}s on ext-first close"
    assert [frame["id"][2] for frame in relayed] == [1, 2]

    for s in (ext_host, client_host, client_peer):
        s.close()


def test_serve_session_returns_when_client_closes_first():
    # Client-first teardown must NOT hang on the still-open extension stdin pipe:
    # _serve_session returns once `closed` is set, because both pumps are daemons
    # and the ext->client reader is not joined. We keep the extension side fully
    # open (never shut down) to prove the call still returns.
    ext_peer, ext_host = _make_socketpair()
    client_peer, client_host = _make_socketpair()

    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    client_in = client_host.makefile("rb")
    client_out = client_host.makefile("wb")

    done = threading.Event()

    def run():
        _serve_session(ext_in, ext_out, client_in, client_out, client_host)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()

    # Client closes its side immediately; the extension side stays fully open.
    client_peer.shutdown(socket.SHUT_RDWR)
    client_peer.close()

    assert done.wait(timeout=5.0), "_serve_session hung on client-first close"

    for s in (ext_peer, ext_host, client_host):
        s.close()


def test_serve_session_drops_oversize_client_frame_and_keeps_relaying():
    # The documented #1 production risk: a client->extension frame over the
    # 1 MB cap must be dropped (it cannot be sent to Chrome) WITHOUT tearing the
    # session down — a following valid frame still reaches the extension.
    ext_peer, ext_host = _make_socketpair()
    client_peer, client_host = _make_socketpair()

    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    client_in = client_host.makefile("rb")
    client_out = client_host.makefile("wb")
    ext_peer_r = ext_peer.makefile("rb")
    client_peer_w = client_peer.makefile("wb")

    relayed: list[dict] = []

    def extension_behaviour():
        # Expect exactly one frame (the small one); the oversize one is dropped.
        relayed.append(read_frame(ext_peer_r))
        ext_peer.shutdown(socket.SHUT_WR)

    def client_behaviour():
        # First an oversize frame (raw, bypassing write_frame's own cap check),
        # then a valid small frame.
        oversize = b"x" * (MAX_MESSAGE_BYTES + 10)
        body = b'{"id":1,"big":"' + oversize + b'"}'
        client_peer.sendall(struct.pack("<I", len(body)) + body)
        write_frame(client_peer_w, {"id": 2, "ok": True})

    te = threading.Thread(target=extension_behaviour, daemon=True)
    tc = threading.Thread(target=client_behaviour, daemon=True)
    tc.start()
    te.start()

    started = time.monotonic()
    _serve_session(ext_in, ext_out, client_in, client_out, client_host)
    elapsed = time.monotonic() - started
    te.join(timeout=5.0)
    tc.join(timeout=5.0)

    # Only the small frame made it through; the oversize one was dropped, and
    # the relay stayed up long enough to deliver it.
    assert elapsed < 2.0, f"_serve_session lingered {elapsed:.2f}s on ext-first close"
    assert relayed[0]["id"][:2] == [nmh._SESSION_ID_MARKER, 1]
    assert relayed[0]["id"][2] == 2
    assert relayed[0]["ok"] is True

    for s in (ext_host, client_host, client_peer):
        s.close()


def test_serve_session_sanitises_surrogate_frame_and_keeps_relaying():
    # A frame carrying an unpaired-surrogate escape is valid JSON (WTF-16 page
    # text is a legitimate source), but its decoded str cannot round-trip
    # through UTF-8. Re-encoding must sanitise to U+FFFD and keep the session
    # alive — a bare UnicodeEncodeError (a ValueError) in the pump would be
    # mistaken for a closed destination and tear the whole session down.
    ext_peer, ext_host = _make_socketpair()
    client_peer, client_host = _make_socketpair()

    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    client_in = client_host.makefile("rb")
    client_out = client_host.makefile("wb")
    ext_peer_r = ext_peer.makefile("rb")
    client_peer_w = client_peer.makefile("wb")

    relayed: list[dict] = []

    def extension_behaviour():
        relayed.append(read_frame(ext_peer_r))
        relayed.append(read_frame(ext_peer_r))
        ext_peer.shutdown(socket.SHUT_WR)

    def client_behaviour():
        # Raw bytes so the surrogate escape reaches the relay unmodified.
        body = b'{"id":1,"text":"a\\ud800b"}'
        client_peer.sendall(struct.pack("<I", len(body)) + body)
        write_frame(client_peer_w, {"id": 2, "ok": True})

    te = threading.Thread(target=extension_behaviour, daemon=True)
    tc = threading.Thread(target=client_behaviour, daemon=True)
    tc.start()
    te.start()

    _serve_session(ext_in, ext_out, client_in, client_out, client_host)
    te.join(timeout=5.0)
    tc.join(timeout=5.0)

    assert [frame["id"][2] for frame in relayed] == [1, 2]
    assert relayed[0]["text"] == "a�b"
    assert relayed[1]["ok"] is True

    for s in (ext_host, client_host, client_peer):
        s.close()


def test_serve_session_relays_large_extension_reply_to_client():
    # Chrome's 1 MB cap is host->extension only: a reply over 1 MB read from the
    # extension (e.g. outer_html of a heavy page) must relay INTACT to the
    # client instead of being silently dropped (which used to surface as a
    # confusing 20 s BridgeTimeoutError).
    ext_peer, ext_host = _make_socketpair()
    client_peer, client_host = _make_socketpair()

    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    client_in = client_host.makefile("rb")
    client_out = client_host.makefile("wb")
    ext_peer_w = ext_peer.makefile("wb")
    client_peer_r = client_peer.makefile("rb")

    # Non-ASCII payload on purpose: the relay re-encodes (read_frame ->
    # write_frame), and an ensure_ascii serialisation would inflate this ~3x,
    # so the test also guards against re-encode amplification.
    big_reply = {
        "id": 1,
        "ok": True,
        "result": "é" * (MAX_MESSAGE_BYTES // 2) + "x" * 100,  # body > 1 MB
    }
    received: list[dict] = []

    def extension_behaviour():
        # The extension->host direction has no 1 MB cap, so the peer writes the
        # oversize reply uncapped, then closes to end the session.
        wire_reply = {
            **big_reply,
            "id": [nmh._SESSION_ID_MARKER, 1, big_reply["id"]],
        }
        write_frame(ext_peer_w, wire_reply, max_bytes=None)
        ext_peer.shutdown(socket.SHUT_WR)

    def client_behaviour():
        received.append(read_frame(client_peer_r))

    te = threading.Thread(target=extension_behaviour, daemon=True)
    tc = threading.Thread(target=client_behaviour, daemon=True)
    tc.start()
    te.start()

    _serve_session(ext_in, ext_out, client_in, client_out, client_host)
    te.join(timeout=5.0)
    tc.join(timeout=5.0)

    assert received == [big_reply]

    for s in (ext_host, client_host, client_peer):
        s.close()


def test_accept_client_binds_loopback_and_accepts():
    # Bind on an ephemeral port to avoid colliding with a real bridge.
    host, port = "127.0.0.1", _free_port()

    result: dict[str, object] = {}

    listener = nmh._bind_listener(host, port)

    def server_thread():
        client, rfile, wfile = nmh._accept_client(listener)
        result["client"] = client
        result["rfile"] = rfile
        result["wfile"] = wfile

    t = threading.Thread(target=server_thread, daemon=True)
    t.start()

    # Connect as the client.
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _connect_with_retry(client, (host, port))
    t.join(timeout=5.0)

    assert "client" in result
    client.close()
    result["client"].close()  # type: ignore[union-attr]
    listener.close()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _connect_with_retry(sock: socket.socket, addr, attempts: int = 50):
    last = None
    for _ in range(attempts):
        try:
            sock.connect(addr)
            return
        except OSError as exc:  # listener not bound yet
            last = exc
            time.sleep(0.02)
    raise last  # type: ignore[misc]


def test_accept_client_bind_collision_raises():
    # A second host on the same port must fail to bind (no SO_REUSEADDR), so the
    # single-owner invariant holds. We hold the port with a plain listener.
    port = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    try:
        with pytest.raises(OSError):
            nmh._bind_listener("127.0.0.1", port)
    finally:
        holder.close()


def test_main_returns_1_on_bind_collision(monkeypatch):
    # The bind-collision OSError surfaces as main() -> 1 (clean exit), not a crash.
    port = _free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", port))
    holder.listen(1)
    monkeypatch.setenv("CBR_BRIDGE_PORT", str(port))
    # Drive stdio off harmless empty buffers so --echo path isn't taken.
    monkeypatch.setattr(nmh, "_force_binary_stdio", lambda: (io.BytesIO(), io.BytesIO()))
    try:
        assert nmh.main([]) == 1
    finally:
        holder.close()


def test_main_serves_two_sequential_clients_and_exits_on_extension_eof(monkeypatch):
    host, port = "127.0.0.1", _free_port()
    ext_peer, ext_host = _make_socketpair()
    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    ext_peer_r = ext_peer.makefile("rb")
    ext_peer_w = ext_peer.makefile("wb")
    monkeypatch.setenv("CBR_BRIDGE_HOST", host)
    monkeypatch.setenv("CBR_BRIDGE_PORT", str(port))
    monkeypatch.setattr(nmh, "_force_binary_stdio", lambda: (ext_in, ext_out))

    result: list[int] = []
    server = threading.Thread(target=lambda: result.append(nmh.main([])), daemon=True)
    server.start()

    for request_id in (1, 2):
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _connect_with_retry(client, (host, port))
        client_r = client.makefile("rb")
        client_w = client.makefile("wb")
        write_frame(client_w, {"id": request_id, "op": "cdp"})
        wire_request = read_frame(ext_peer_r)
        assert wire_request["id"][:2] == [nmh._SESSION_ID_MARKER, request_id]
        assert wire_request["id"][2] == request_id
        assert wire_request["op"] == "cdp"
        write_frame(ext_peer_w, {"id": wire_request["id"], "ok": True})
        assert read_frame(client_r) == {"id": request_id, "ok": True}
        client.shutdown(socket.SHUT_RDWR)
        client_r.close()
        client_w.close()
        client.close()

    ext_peer.shutdown(socket.SHUT_WR)
    server.join(timeout=5.0)
    assert result == [0]

    ext_peer_r.close()
    ext_peer_w.close()
    ext_peer.close()
    ext_host.close()


def test_main_exits_promptly_on_extension_eof_without_client(monkeypatch):
    host, port = "127.0.0.1", _free_port()
    ext_peer, ext_host = _make_socketpair()
    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    monkeypatch.setenv("CBR_BRIDGE_HOST", host)
    monkeypatch.setenv("CBR_BRIDGE_PORT", str(port))
    monkeypatch.setattr(nmh, "_force_binary_stdio", lambda: (ext_in, ext_out))

    result: list[int] = []
    server = threading.Thread(target=lambda: result.append(nmh.main([])), daemon=True)
    server.start()
    ext_peer.shutdown(socket.SHUT_WR)
    server.join(timeout=2.0)

    assert result == [0]
    ext_peer.close()
    ext_host.close()


def test_late_reply_from_previous_session_is_not_delivered_to_next_client(monkeypatch):
    host, port = "127.0.0.1", _free_port()
    ext_peer, ext_host = _make_socketpair()
    ext_in = ext_host.makefile("rb")
    ext_out = ext_host.makefile("wb")
    ext_peer_r = ext_peer.makefile("rb")
    ext_peer_w = ext_peer.makefile("wb")
    monkeypatch.setenv("CBR_BRIDGE_HOST", host)
    monkeypatch.setenv("CBR_BRIDGE_PORT", str(port))
    monkeypatch.setattr(nmh, "_force_binary_stdio", lambda: (ext_in, ext_out))

    server = threading.Thread(target=lambda: nmh.main([]), daemon=True)
    server.start()

    first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _connect_with_retry(first, (host, port))
    first_w = first.makefile("wb")
    write_frame(first_w, {"id": 1, "op": "cdp", "session": "old"})
    old_wire_request = read_frame(ext_peer_r)
    first.shutdown(socket.SHUT_RDWR)
    first_w.close()
    first.close()

    second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _connect_with_retry(second, (host, port))
    second_r = second.makefile("rb")
    second_w = second.makefile("wb")
    write_frame(second_w, {"id": 1, "op": "cdp", "session": "new"})
    new_wire_request = read_frame(ext_peer_r)
    assert old_wire_request["id"] != new_wire_request["id"]

    # The stale reply arrives first and reuses the caller-visible id. Fencing
    # must discard it; only the reply for session B reaches B.
    write_frame(ext_peer_w, {"id": old_wire_request["id"], "ok": True, "value": "old"})
    write_frame(ext_peer_w, {"id": new_wire_request["id"], "ok": True, "value": "new"})
    assert read_frame(second_r) == {"id": 1, "ok": True, "value": "new"}

    second.shutdown(socket.SHUT_RDWR)
    second_r.close()
    second_w.close()
    second.close()
    ext_peer.shutdown(socket.SHUT_WR)
    server.join(timeout=5.0)
    assert not server.is_alive()


def test_log_level_falls_back_to_info_on_garbage(monkeypatch):
    monkeypatch.setenv("CBR_LOG_LEVEL", "not-a-level")
    assert nmh._log_level() == logging.INFO


def test_log_level_honours_valid_value(monkeypatch):
    monkeypatch.setenv("CBR_LOG_LEVEL", "debug")
    assert nmh._log_level() == logging.DEBUG
