"""Chrome Native Messaging frame codec.

A frame is one JSON object serialised to UTF-8 and prefixed with its byte
length as a **4-byte little-endian unsigned int** (``struct "<I"``). This is the
exact wire format Chrome uses on the host's stdin/stdout, and the same format
``extension/sw.js`` documents in its WIRE PROTOCOL block.

The functions here operate on arbitrary binary streams (``read``/``readinto``
and ``write``/``flush``) so they work equally against ``sys.stdin.buffer`` /
``sys.stdout.buffer`` (the extension side) and a socket file object (the client
side), and can be unit-tested against :class:`io.BytesIO`.
"""

from __future__ import annotations

import json
import re
import struct
from typing import Any, BinaryIO

# 4-byte little-endian unsigned int length prefix.
_LEN = struct.Struct("<I")
LENGTH_PREFIX_BYTES = _LEN.size

# Chrome's Native Messaging caps a single *host -> extension* message at 1 MB.
# The cap is direction-specific: it does NOT apply to extension -> host messages
# (Chrome allows those to be far larger), nor to the host <-> client loopback
# socket. It is enforced on the WRITE side (encode_frame / write_frame) via the
# ``max_bytes`` parameter, which defaults to this cap: an oversize frame is
# rejected before any byte is written, so the relay can drop it and keep going
# without ever sending Chrome a frame it would reject (which would desync).
# Writers whose destination is not the extension lift the cap: the ext->client
# pump in nmh.py passes ``max_bytes=MAX_READ_BYTES`` (the peer's read guard,
# so the relay never emits a frame the client's own codec rejects). See
# docs/design.md "Top risks #1".
MAX_MESSAGE_BYTES = 1024 * 1024

# A generous upper bound on the size of a frame we are willing to READ into
# memory from either pipe (the extension->host direction Chrome allows far above
# 1 MB; the client socket is local and unbounded). This is a denial-of-service /
# runaway-allocation guard, NOT the Chrome 1 MB cap: a frame between
# MAX_MESSAGE_BYTES and MAX_READ_BYTES is read fine and relayed intact when it
# is bound for the client (e.g. a large outer_html reply); only a frame bound
# for the *extension* is dropped on write. A length prefix above MAX_READ_BYTES
# is treated as a stream corruption (we refuse to allocate it) and ends that
# direction.
MAX_READ_BYTES = 64 * 1024 * 1024


class FrameError(Exception):
    """A frame could not be read or written (oversize, truncated, or invalid)."""


# Unpaired UTF-16 surrogates. json.loads happily decodes a ``\udXXX`` escape
# into a lone surrogate (JS page text is WTF-16, so V8's JSON.stringify emits
# such escapes for real-world DOM strings), but a str containing one cannot be
# encoded as UTF-8. See encode_frame.
_SURROGATES = re.compile(r"[\ud800-\udfff]")


def _read_exactly(stream: BinaryIO, n: int) -> bytes | None:
    """Read exactly ``n`` bytes, or ``None`` on a clean EOF before any byte.

    A partial read (EOF after some but not all of ``n`` bytes) is a truncated
    frame and raises :class:`FrameError`; a clean EOF at a frame boundary
    (0 bytes available) returns ``None`` so callers can stop looping.
    """
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            raise FrameError(
                f"truncated frame: wanted {n} bytes, got {n - remaining}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one frame from ``stream``; return its decoded object, or ``None`` at EOF.

    EOF *between* frames (the peer closed the connection cleanly) returns
    ``None``. EOF *within* a frame, a length prefix above the read guard, or
    invalid JSON raises :class:`FrameError`. A frame that is merely above the
    1 MB Chrome cap is read normally here; it is the *write* side that rejects
    it for the host->extension direction.
    """
    header = _read_exactly(stream, LENGTH_PREFIX_BYTES)
    if header is None:
        return None
    (length,) = _LEN.unpack(header)
    if length > MAX_READ_BYTES:
        # We have already consumed the prefix; the stream is now unaligned and
        # such a size is a runaway/corruption, so surface a hard error.
        raise FrameError(
            f"frame length {length} exceeds {MAX_READ_BYTES}-byte read guard"
        )
    # _read_exactly returns exactly `length` bytes or raises on a short read;
    # for length>0 it can only return None at a clean boundary, which here means
    # the prefix promised a body that never arrived — a truncated frame.
    body = _read_exactly(stream, length)
    if body is None:
        raise FrameError("truncated frame body")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(f"invalid frame JSON: {exc}") from exc


def encode_frame(obj: Any, *, max_bytes: int | None = MAX_MESSAGE_BYTES) -> bytes:
    """Serialise ``obj`` to a length-prefixed frame; raise if it exceeds ``max_bytes``.

    ``max_bytes`` defaults to Chrome's 1 MB host->extension cap; pass ``None``
    when the destination is not the extension (the cap is direction-specific).
    The cap is checked *before* anything is written, so an oversize frame never
    half-emits and desynchronises the stream.

    Serialisation is compact UTF-8 (``ensure_ascii=False``): a relayed frame
    re-encodes to roughly its inbound size instead of inflating non-ASCII text
    into ``\\uXXXX`` escapes (up to ~3x), which would both shrink the usable
    1 MB budget and let a re-encoded frame outgrow the peer's read guard.

    Two serialisation edge cases are handled so a relay never emits a frame the
    peer would reject, and never dies on a `ValueError` its pump mistakes for a
    closed destination: non-finite floats raise :class:`FrameError` (JSON has
    no ``Infinity``/``NaN``; Chrome would kill the port on such a frame), and
    unpaired surrogates — legitimate in scraped page text — are replaced with
    U+FFFD (as Chromium itself does) rather than failing the whole frame.
    """
    try:
        text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        raise FrameError(f"frame not JSON-serialisable: {exc}") from exc
    try:
        body = text.encode("utf-8")
    except UnicodeEncodeError:
        body = _SURROGATES.sub("\ufffd", text).encode("utf-8")
    if max_bytes is not None and len(body) > max_bytes:
        raise FrameError(
            f"frame body {len(body)} bytes exceeds {max_bytes}-byte cap"
        )
    return _LEN.pack(len(body)) + body


def write_frame(
    stream: BinaryIO, obj: Any, *, max_bytes: int | None = MAX_MESSAGE_BYTES
) -> None:
    """Encode ``obj`` and write it to ``stream`` as a single frame, then flush.

    ``max_bytes`` is forwarded to :func:`encode_frame`; pass ``None`` to lift
    the 1 MB cap when the destination is not the extension.
    """
    stream.write(encode_frame(obj, max_bytes=max_bytes))
    stream.flush()
