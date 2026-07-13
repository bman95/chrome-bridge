"""Shared test fixtures.

Every test runs with the profile registry — and therefore the auth-token files
that live beside it — redirected to a per-test tmp directory. No test, present
or future, may read or write the real ``~/.config/chrome-bridge/``: the fake
bridge harnesses (``FakeBridge`` and friends) write token files on
construction, so an unredirected test would silently pollute the developer's
real config dir. Individual tests/fixtures that monkeypatch
``default_registry_path`` themselves simply override this net.
"""

from __future__ import annotations

import pytest

from chrome_bridge import profiles


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        profiles,
        "default_registry_path",
        lambda: tmp_path / "chrome-bridge-config" / "profiles.json",
    )
