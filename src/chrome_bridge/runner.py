"""Run a user-authored script with a connected :class:`ChromeBridge`.

``chrome-bridge run script.py --profile X`` is the primary way scripts drive
the bridge. The script itself never receives injected globals; instead it
imports :func:`get_bridge` to reach the live connection this module set up,
mirroring pytest fixture-style ergonomics rather than an ``exec()`` with a
magic namespace::

    # my_script.py
    from chrome_bridge.runner import get_bridge

    cdp = get_bridge()
    cdp.navigate("https://example.com")
    print(cdp.outer_html("body"))
"""

from __future__ import annotations

import contextvars
import runpy
import sys
from pathlib import Path

from .bridge.client import ChromeBridge

_current_bridge: contextvars.ContextVar[ChromeBridge] = contextvars.ContextVar(
    "chrome_bridge_current"
)


def get_bridge() -> ChromeBridge:
    """Return the :class:`ChromeBridge` the current ``chrome-bridge run`` set up.

    Raises :class:`RuntimeError` if called outside of :func:`run_script` (e.g.
    a script imported and run directly rather than via the CLI).
    """
    try:
        return _current_bridge.get()
    except LookupError as exc:
        raise RuntimeError(
            "get_bridge() called outside `chrome-bridge run` — no bridge is "
            "connected. Run this script via `chrome-bridge run <script> "
            "--profile <name>`."
        ) from exc


def run_script(
    script_path: str | Path,
    *,
    bridge: ChromeBridge,
    script_args: list[str] | None = None,
) -> None:
    """Execute ``script_path`` as ``__main__`` with ``bridge`` reachable via
    :func:`get_bridge`, then close ``bridge`` regardless of outcome.

    ``script_args`` becomes ``sys.argv[1:]`` for the duration of the script, as
    if it had been invoked directly.
    """
    token = _current_bridge.set(bridge)
    script_args = script_args or []
    original_argv = sys.argv
    sys.argv = [str(script_path), *script_args]
    try:
        bridge.connect()
        runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = original_argv
        _current_bridge.reset(token)
        bridge.close()
