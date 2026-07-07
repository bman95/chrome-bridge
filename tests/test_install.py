"""Tests for chrome_bridge.install: per-profile manifest/launcher generation
and registry registration (mocked winreg, no real registry writes)."""

from __future__ import annotations

import json
import sys

import pytest

from chrome_bridge import install


def test_host_name_includes_profile():
    assert install.host_name("personal") == "com.chromebridge.bridge.personal"


def test_write_launcher_bakes_in_port_and_uses_crlf(tmp_path):
    path = install.write_launcher(tmp_path, "personal", 8765)
    raw = path.read_bytes()
    assert b"\r\n" in raw
    text = raw.decode("utf-8")
    assert "set CBR_BRIDGE_PORT=8765" in text
    assert "chrome_bridge.bridge.nmh" in text


def test_write_manifest_points_at_launcher_and_lists_origins(tmp_path):
    install.write_launcher(tmp_path, "personal", 8765)
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=["abc123"])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["name"] == "com.chromebridge.bridge.personal"
    assert data["allowed_origins"] == ["chrome-extension://abc123/"]
    assert data["path"] == str(install.launcher_path(tmp_path, "personal"))


def test_write_manifest_allows_multiple_origins(tmp_path):
    manifest = install.write_manifest(
        tmp_path, "personal", extension_ids=["abc123", "def456"]
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == [
        "chrome-extension://abc123/",
        "chrome-extension://def456/",
    ]


def test_patch_allowed_origins_appends_new_id(tmp_path):
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=["abc123"])
    install.patch_allowed_origins(manifest, "def456")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == [
        "chrome-extension://abc123/",
        "chrome-extension://def456/",
    ]


def test_patch_allowed_origins_is_idempotent(tmp_path):
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=["abc123"])
    install.patch_allowed_origins(manifest, "abc123")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == ["chrome-extension://abc123/"]


@pytest.mark.skipif(sys.platform != "win32", reason="winreg is Windows-only")
def test_register_native_host_writes_registry_key(tmp_path, monkeypatch):
    import winreg

    written = {}

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_create_key(root, path):
        written["path"] = path
        return FakeKey()

    def fake_set_value(key, name, reserved, type_, value):
        written["value"] = value

    monkeypatch.setattr(winreg, "CreateKey", fake_create_key)
    monkeypatch.setattr(winreg, "SetValueEx", fake_set_value)

    manifest = tmp_path / "nmh-manifest.personal.local.json"
    manifest.write_text("{}", encoding="utf-8")
    install.register_native_host("personal", manifest)

    assert written["path"].endswith("com.chromebridge.bridge.personal")
    assert written["value"] == str(manifest)


def test_register_native_host_raises_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows"):
        install.register_native_host("personal", tmp_path / "m.json")
