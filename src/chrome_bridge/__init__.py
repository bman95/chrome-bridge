"""chrome-bridge: drive your real, everyday Chrome from Python scripts.

Scripts talk to :class:`ChromeBridge`, which speaks raw page-CDP over a
Native Messaging bridge to a `chrome.debugger`-based MV3 extension loaded
into whichever Chrome profile you want to script — see ``docs/design.md``
and ``docs/multi-profile.md``.
"""

from __future__ import annotations

from .bridge.client import (
    BridgeError,
    BridgeTimeoutError,
    ChromeBridge,
    DetachedError,
    Tab,
)

__all__ = [
    "ChromeBridge",
    "Tab",
    "BridgeError",
    "BridgeTimeoutError",
    "DetachedError",
]
