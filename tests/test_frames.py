"""Tests for the Native Messaging frame codec.

These exercise the codec directly against :class:`io.BytesIO` — no Chrome, no
sockets.
"""

from __future__ import annotations

import io
import json
import struct

import pytest

from chrome_bridge.bridge.frames import (
    MAX_MESSAGE_BYTES,
    MAX_READ_BYTES,
    FrameError,
    encode_frame,
    read_frame,
    write_frame,
)


def test_encode_frame_is_le_length_prefixed_json():
    raw = encode_frame({"id": 1, "op": "open", "url": "https://x"})
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert json.loads(raw[4:].decode("utf-8")) == {
        "id": 1,
        "op": "open",
        "url": "https://x",
    }


def test_round_trip_through_stream():
    buf = io.BytesIO()
    msgs = [
        {"id": 1, "op": "open", "url": "https://example.com/"},
        {"id": "abc", "ok": True, "result": {"x": [1, 2, 3]}},
        {"event": True, "type": "detached", "reason": "target_closed"},
    ]
    for m in msgs:
        write_frame(buf, m)
    buf.seek(0)
    assert [read_frame(buf) for _ in msgs] == msgs
    assert read_frame(buf) is None  # clean EOF at a frame boundary


def test_round_trip_preserves_unicode():
    buf = io.BytesIO()
    msg = {"title": "Café — 90 m² · €1.200"}
    write_frame(buf, msg)
    buf.seek(0)
    assert read_frame(buf) == msg


def test_read_frame_eof_returns_none():
    assert read_frame(io.BytesIO(b"")) is None


def test_read_frame_truncated_header_raises():
    with pytest.raises(FrameError):
        read_frame(io.BytesIO(b"\x01\x02"))  # < 4 prefix bytes, but some present


def test_read_frame_truncated_body_raises():
    raw = encode_frame({"hello": "world"})
    with pytest.raises(FrameError):
        read_frame(io.BytesIO(raw[:-1]))  # body one byte short


def test_read_frame_invalid_json_raises():
    bad = b"not json at all"
    frame = struct.pack("<I", len(bad)) + bad
    with pytest.raises(FrameError):
        read_frame(io.BytesIO(frame))


def test_read_frame_allows_frame_over_chrome_cap_but_under_read_guard():
    # A frame between the 1 MB Chrome cap and the read guard is read normally;
    # the write side, not the read side, enforces the Chrome cap.
    big = {"x": "y" * (MAX_MESSAGE_BYTES + 100)}
    body = json.dumps(big, separators=(",", ":")).encode("utf-8")
    assert len(body) > MAX_MESSAGE_BYTES
    frame = struct.pack("<I", len(body)) + body
    assert read_frame(io.BytesIO(frame)) == big


def test_read_frame_rejects_length_prefix_over_read_guard():
    # A length prefix above the read guard is refused without allocating it.
    frame = struct.pack("<I", MAX_READ_BYTES + 1)
    with pytest.raises(FrameError, match="read guard"):
        read_frame(io.BytesIO(frame))


def test_encode_frame_rejects_oversize_body():
    huge = {"html": "x" * (MAX_MESSAGE_BYTES + 1)}
    with pytest.raises(FrameError, match="exceeds"):
        encode_frame(huge)


def test_write_frame_oversize_does_not_emit_partial():
    buf = io.BytesIO()
    with pytest.raises(FrameError):
        write_frame(buf, {"html": "x" * (MAX_MESSAGE_BYTES + 1)})
    assert buf.getvalue() == b""  # nothing written -> stream stays aligned


def test_encode_frame_uncapped_allows_body_over_chrome_cap():
    # The 1 MB cap is host->extension only; max_bytes=None lifts it for writers
    # whose destination is not the extension (the ext->client relay direction).
    huge = {"html": "x" * (MAX_MESSAGE_BYTES + 1)}
    raw = encode_frame(huge, max_bytes=None)
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert json.loads(raw[4:].decode("utf-8")) == huge


def test_write_frame_uncapped_round_trips_large_frame():
    buf = io.BytesIO()
    huge = {"html": "x" * (MAX_MESSAGE_BYTES + 1)}
    write_frame(buf, huge, max_bytes=None)
    buf.seek(0)
    assert read_frame(buf) == huge


def test_encode_frame_honours_custom_cap():
    with pytest.raises(FrameError, match="exceeds"):
        encode_frame({"x": "y" * 100}, max_bytes=10)


def test_encode_frame_emits_compact_utf8():
    # ensure_ascii would inflate non-ASCII into \uXXXX escapes (up to ~3x),
    # shrinking the usable 1 MB budget and letting a relayed frame outgrow the
    # peer's read guard. The codec must emit raw UTF-8 instead.
    msg = {"title": "Café — 90 m² · €1.200", "cjk": "漢字", "emoji": "😀"}
    raw = encode_frame(msg)
    assert b"\\u" not in raw
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert raw[4:] == body


def test_encode_frame_replaces_unpaired_surrogates():
    # json.loads accepts a "\ud800" escape (real-world WTF-16 page text via
    # V8's JSON.stringify), producing a str no UTF-8 encoder accepts. The codec
    # substitutes U+FFFD (as Chromium does) instead of raising a ValueError a
    # relay pump would mistake for a closed destination — which would kill the
    # whole session over one bad code point.
    decoded = json.loads('{"text": "a\\ud800b"}')
    raw = encode_frame(decoded)
    assert read_frame(io.BytesIO(raw)) == {"text": "a�b"}


def test_encode_frame_rejects_non_finite_numbers():
    # JSON has no Infinity/NaN; Chrome would kill the native port on such a
    # frame. Python's json.loads accepts the literals, so a relayed frame can
    # carry them — encode_frame must surface FrameError (dropped frame-aligned)
    # rather than emit invalid JSON or leak a bare ValueError.
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(FrameError, match="serialisable"):
            encode_frame({"v": bad})


def test_relayed_nonascii_frame_does_not_inflate():
    # The relay is read-then-re-encode, not byte forwarding: a decoded frame
    # must re-encode to its inbound size, or a frame under the 64 MB read guard
    # could be re-emitted over it and desync the client.
    big = {"html": "é" * (MAX_MESSAGE_BYTES // 2)}  # ~1 MB of 2-byte UTF-8
    wire = encode_frame(big, max_bytes=None)
    decoded = read_frame(io.BytesIO(wire))
    assert len(encode_frame(decoded, max_bytes=None)) == len(wire)
