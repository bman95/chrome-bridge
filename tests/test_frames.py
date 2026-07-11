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
