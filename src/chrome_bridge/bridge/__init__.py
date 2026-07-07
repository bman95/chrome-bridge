"""Native Messaging bridge between the user's real Chrome and a Python script.

Chrome spawns :mod:`chrome_bridge.bridge.nmh` (the *native messaging host*)
when the MV3 extension calls
``chrome.runtime.connectNative("com.chromebridge.bridge.<profile>")``. The
host is a **transparent transport**: it relays length-prefixed JSON frames
between the extension (over Native Messaging stdio) and a script (over a
loopback TCP socket).

The wire protocol — the op/reply/event message shapes — is defined by and
documented in ``extension/sw.js`` (see the "WIRE PROTOCOL" contract block).
The host (:mod:`.nmh`) does not interpret those shapes; it only moves whole
frames. :class:`~chrome_bridge.bridge.client.ChromeBridge` is the one
component that speaks the protocol, pairing replies to requests and exposing
the scripting primitives.
"""

from __future__ import annotations

from .client import (
    BridgeError,
    BridgeTimeoutError,
    ChromeBridge,
    DetachedError,
)

# Native Messaging host name prefix. Each profile gets its own full name
# "<HOST_NAME_PREFIX>.<profile>" — see docs/multi-profile.md. MUST match what
# extension/sw.js builds and what nmh-manifest.<profile>.local.json's "name"
# field contains.
HOST_NAME_PREFIX = "com.chromebridge.bridge"

__all__ = [
    "HOST_NAME_PREFIX",
    "ChromeBridge",
    "BridgeError",
    "BridgeTimeoutError",
    "DetachedError",
]
