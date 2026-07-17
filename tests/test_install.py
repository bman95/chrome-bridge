"""Tests for chrome_bridge.install: per-profile manifest/launcher generation
and registry registration (mocked winreg, no real registry writes)."""

from __future__ import annotations

import json
import sys

import pytest

from chrome_bridge import install


EXTENSION_ID_A = "abcdefghijklmnopabcdefghijklmnop"
EXTENSION_ID_B = "ponmlkjihgfedcbaponmlkjihgfedcba"


def test_host_name_includes_profile():
    assert install.host_name("personal") == "com.chromebridge.bridge.personal"


def test_install_extension_copies_loadable_bundle_to_stable_location(tmp_path):
    destination = install.install_extension(tmp_path / "installed-extension")
    assert destination == tmp_path / "installed-extension"
    assert json.loads((destination / "manifest.json").read_text(encoding="utf-8"))["name"]
    assert (destination / "sw.js").is_file()
    assert (destination / "options.html").is_file()


def test_install_profile_validates_then_writes_all_profile_artifacts(
    tmp_path, monkeypatch
):
    registered = []
    monkeypatch.setattr(
        install,
        "register_native_host",
        lambda profile, manifest: registered.append((profile, manifest)),
    )
    result = install.install_profile(
        "personal",
        8765,
        chrome_profile_directory="Default",
        extension_id=f" {EXTENSION_ID_A}\n",
        artifacts_root=tmp_path / "artifacts",
        registry_path=tmp_path / "profiles.json",
        python_executable=r"C:\Python\python.exe",
    )

    assert result.extension_id == EXTENSION_ID_A
    assert result.launcher.is_file()
    assert result.manifest.is_file()
    assert registered == [("personal", result.manifest)]
    assert json.loads(result.manifest.read_text(encoding="utf-8"))["allowed_origins"] == [
        f"chrome-extension://{EXTENSION_ID_A}/"
    ]


def test_install_extension_replaces_stale_files_from_older_bundle(tmp_path):
    destination = tmp_path / "installed-extension"
    install.install_extension(destination)
    stale = destination / "old-module.js"
    stale.write_text("// leftover from an older bundle\n", encoding="utf-8")

    install.install_extension(destination)

    assert not stale.exists()
    assert (destination / "manifest.json").is_file()


def test_install_profile_port_conflict_fails_before_writing_artifacts(
    tmp_path, monkeypatch
):
    from chrome_bridge import profiles as profiles_mod

    registered = []
    monkeypatch.setattr(
        install,
        "register_native_host",
        lambda *args: registered.append(args),
    )
    registry_path = tmp_path / "profiles.json"
    profiles_mod.add_profile("existing", 8765, path=registry_path)

    with pytest.raises(profiles_mod.ProfileError, match="already used"):
        install.install_profile(
            "personal",
            8765,
            chrome_profile_directory="Default",
            extension_id=EXTENSION_ID_A,
            artifacts_root=tmp_path / "artifacts",
            registry_path=registry_path,
        )
    assert not (tmp_path / "artifacts").exists()
    assert not registered
    assert set(profiles_mod.list_profiles(registry_path)) == {"existing"}


def test_install_profile_rejects_invalid_input_before_writing(tmp_path, monkeypatch):
    registered = []
    monkeypatch.setattr(
        install,
        "register_native_host",
        lambda *args: registered.append(args),
    )
    with pytest.raises(ValueError, match="invalid Chrome extension ID"):
        install.install_profile(
            "personal",
            8765,
            chrome_profile_directory="Default",
            extension_id="not-an-id",
            artifacts_root=tmp_path / "artifacts",
            registry_path=tmp_path / "profiles.json",
        )
    assert not (tmp_path / "artifacts").exists()
    assert not (tmp_path / "profiles.json").exists()
    assert not registered


def test_write_launcher_bakes_in_port_and_uses_crlf(tmp_path):
    path = install.write_launcher(tmp_path, "personal", 8765)
    raw = path.read_bytes()
    assert b"\r\n" in raw
    text = raw.decode("utf-8")
    assert "set CBR_BRIDGE_PORT=8765" in text
    assert "chrome_bridge.bridge.nmh" in text


def test_write_launcher_bakes_in_profile_env(tmp_path):
    # The host learns its profile name from CBR_BRIDGE_PROFILE (issue #7) to
    # resolve which per-profile token file to write.
    path = install.write_launcher(tmp_path, "personal", 8765)
    text = path.read_bytes().decode("utf-8")
    assert "set CBR_BRIDGE_PROFILE=personal" in text
    assert "set CBR_BRIDGE_PORT=8765" in text


def test_write_launcher_uses_installed_python_without_uv(tmp_path):
    path = install.write_launcher(
        tmp_path,
        "personal",
        8765,
        python_executable=r"C:\Program Files\Chrome Bridge\python.exe",
    )
    text = path.read_bytes().decode("utf-8")
    assert '"C:\\Program Files\\Chrome Bridge\\python.exe" -m chrome_bridge.bridge.nmh' in text
    assert "uv run" not in text
    assert "where uv" not in text


def test_write_manifest_points_at_launcher_and_lists_origins(tmp_path):
    install.write_launcher(tmp_path, "personal", 8765)
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=[EXTENSION_ID_A])
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["name"] == "com.chromebridge.bridge.personal"
    assert data["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID_A}/"]
    assert data["path"] == str(install.launcher_path(tmp_path, "personal"))


def test_write_manifest_allows_multiple_origins(tmp_path):
    manifest = install.write_manifest(
        tmp_path, "personal", extension_ids=[EXTENSION_ID_A, EXTENSION_ID_B]
    )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == [
        f"chrome-extension://{EXTENSION_ID_A}/",
        f"chrome-extension://{EXTENSION_ID_B}/",
    ]


def test_patch_allowed_origins_appends_new_id(tmp_path):
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=[EXTENSION_ID_A])
    install.patch_allowed_origins(manifest, EXTENSION_ID_B)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == [
        f"chrome-extension://{EXTENSION_ID_A}/",
        f"chrome-extension://{EXTENSION_ID_B}/",
    ]


def test_patch_allowed_origins_is_idempotent(tmp_path):
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=[EXTENSION_ID_A])
    install.patch_allowed_origins(manifest, EXTENSION_ID_A)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID_A}/"]


def test_patch_allowed_origins_trims_pasted_extension_id(tmp_path):
    manifest = install.write_manifest(tmp_path, "personal", extension_ids=[])
    install.patch_allowed_origins(manifest, f"  {EXTENSION_ID_A}\n")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID_A}/"]


@pytest.mark.parametrize(
    "extension_id",
    ["abc123", "a" * 31, "a" * 33, "A" * 32, "q" * 32, "a" * 31 + "-"],
)
def test_write_manifest_rejects_invalid_extension_id_without_writing(
    tmp_path, extension_id
):
    with pytest.raises(ValueError, match="invalid Chrome extension ID"):
        install.write_manifest(tmp_path, "personal", extension_ids=[extension_id])
    assert not install.manifest_path(tmp_path, "personal").exists()


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
