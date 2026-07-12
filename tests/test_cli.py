"""Tests for the chrome-bridge CLI via Typer's CliRunner.

These do not touch Chrome or a real bridge; `status`/`run` are pointed at a
tiny fake bridge server, and `profiles list/rm` operate against a registry
path injected through monkeypatched defaults.
"""

from __future__ import annotations

import json
import socket
import sys
import threading

import pytest
from typer.testing import CliRunner

from chrome_bridge import cli as cli_mod
from chrome_bridge import profiles as profiles_mod

runner = CliRunner()


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    monkeypatch.setattr(profiles_mod, "default_registry_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def isolated_profile_cleanup(monkeypatch, tmp_path):
    """No profiles-rm test may touch checkout files or the real HKCU key."""
    monkeypatch.setattr(cli_mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        cli_mod.install_mod, "unregister_native_host", lambda _name: False
    )


def test_profiles_list_empty(registry_path):
    result = runner.invoke(cli_mod.app, ["profiles", "list"])
    assert result.exit_code == 0
    assert "No profiles registered" in result.output


def test_profiles_list_shows_registered(registry_path):
    profiles_mod.add_profile("personal", 8765, path=registry_path)
    result = runner.invoke(cli_mod.app, ["profiles", "list"])
    assert result.exit_code == 0
    assert "personal" in result.output
    assert "8765" in result.output


def test_profiles_rm_removes_entry(registry_path):
    profiles_mod.add_profile("personal", 8765, path=registry_path)
    result = runner.invoke(cli_mod.app, ["profiles", "rm", "personal"])
    assert result.exit_code == 0
    assert profiles_mod.list_profiles(registry_path) == {}


def test_profiles_rm_missing_exits_nonzero(registry_path):
    result = runner.invoke(cli_mod.app, ["profiles", "rm", "nope"])
    assert result.exit_code == 1


def test_profiles_rm_cleans_key_and_files(registry_path, monkeypatch, tmp_path):
    """On Windows, rm also unregisters the native-host key and deletes the
    generated launcher/manifest, reporting each removal."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cli_mod, "_PROJECT_ROOT", tmp_path)
    unregistered = []
    monkeypatch.setattr(
        cli_mod.install_mod,
        "unregister_native_host",
        lambda name: unregistered.append(name) or True,
    )
    profiles_mod.add_profile("personal", 8765, path=registry_path)
    launcher = tmp_path / "run_nmh_personal.bat"
    manifest = tmp_path / "nmh-manifest.personal.local.json"
    launcher.write_text("bat", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")

    result = runner.invoke(cli_mod.app, ["profiles", "rm", "personal"])
    assert result.exit_code == 0, result.output
    assert unregistered == ["personal"]
    assert not launcher.exists()
    assert not manifest.exists()
    assert profiles_mod.list_profiles(registry_path) == {}
    assert "Removed profile 'personal' from the registry." in result.output
    assert "Removed native-host key for 'personal'." in result.output
    assert "Deleted run_nmh_personal.bat." in result.output
    assert "Deleted nmh-manifest.personal.local.json." in result.output


def test_profiles_rm_reports_absent_artifacts(registry_path, monkeypatch, tmp_path):
    """Off Windows the registry-key step is skipped; missing launcher/manifest
    files are reported as already absent rather than silently ignored."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli_mod, "_PROJECT_ROOT", tmp_path)
    profiles_mod.add_profile("personal", 8765, path=registry_path)

    result = runner.invoke(cli_mod.app, ["profiles", "rm", "personal"])
    assert result.exit_code == 0, result.output
    assert "Skipped native-host registry key (Windows-only)." in result.output
    assert "No run_nmh_personal.bat to delete." in result.output
    assert "No nmh-manifest.personal.local.json to delete." in result.output


def test_profiles_rm_reports_absent_windows_key(registry_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    profiles_mod.add_profile("personal", 8765, path=registry_path)
    result = runner.invoke(cli_mod.app, ["profiles", "rm", "personal"])
    assert result.exit_code == 0
    assert "No native-host key for 'personal' to remove." in result.output


def test_profiles_rm_rejects_invalid_name_before_cleanup(registry_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        cli_mod.install_mod,
        "unregister_native_host",
        lambda name: calls.append(name) or True,
    )
    result = runner.invoke(cli_mod.app, ["profiles", "rm", "../personal"])
    assert result.exit_code == 1
    assert calls == []


def test_status_requires_profile_or_host_port():
    result = runner.invoke(cli_mod.app, ["status"])
    assert result.exit_code == 2


def test_status_host_without_port_errors():
    # --host alone must not silently fall back to DEFAULT_BASE_PORT; it errors.
    result = runner.invoke(cli_mod.app, ["status", "--host", "127.0.0.1"])
    assert result.exit_code == 2
    assert "--port" in result.output


@pytest.mark.parametrize("port", ["0", "65536"])
def test_status_rejects_out_of_range_port(port):
    result = runner.invoke(cli_mod.app, ["status", "--port", port])
    assert result.exit_code == 2
    assert "invalid port" in result.output


def test_status_rejects_profile_with_legacy_invalid_port(registry_path):
    # A pre-validation registry can hold an out-of-range port; resolving it
    # must reject cleanly instead of crashing in socket.connect.
    registry_path.write_text(
        json.dumps({"profiles": {"personal": {"port": 70000}}}), encoding="utf-8"
    )
    result = runner.invoke(cli_mod.app, ["status", "--profile", "personal"])
    assert result.exit_code == 2
    assert "invalid port" in result.output
    assert "--port" in result.output  # actionable remediation hint


def test_status_unreachable_port_exits_nonzero():
    # Bind + free a port so nothing is listening.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    host, port = s.getsockname()
    s.close()
    result = runner.invoke(cli_mod.app, ["status", "--host", host, "--port", str(port)])
    assert result.exit_code == 1
    assert "UNREACHABLE" in result.output


def test_status_reachable_port_succeeds():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def accept_once():
        try:
            conn, _ = listener.accept()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    try:
        result = runner.invoke(cli_mod.app, ["status", "--host", host, "--port", str(port)])
        assert result.exit_code == 0
        assert "OK" in result.output
    finally:
        listener.close()


def test_status_resolves_profile_to_port(registry_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    _, port = listener.getsockname()
    profiles_mod.add_profile("personal", port, path=registry_path)

    def accept_once():
        try:
            conn, _ = listener.accept()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    try:
        result = runner.invoke(cli_mod.app, ["status", "--profile", "personal"])
        assert result.exit_code == 0
    finally:
        listener.close()


# --------------------------------------------------------------------------- #
# init — mocked winreg + prompts, no real Chrome or registry writes.
# --------------------------------------------------------------------------- #


@pytest.fixture
def mocked_install(monkeypatch, tmp_path):
    """Redirect init's project root + registration to tmp_path, no real winreg."""
    monkeypatch.setattr(cli_mod, "_PROJECT_ROOT", tmp_path)
    registered = {}

    def fake_register(profile, manifest_file):
        registered[profile] = manifest_file

    monkeypatch.setattr(cli_mod.install_mod, "register_native_host", fake_register)
    return registered


@pytest.mark.skipif(sys.platform != "win32", reason="init only automates Windows")
def test_init_full_flow_registers_and_saves_profile(registry_path, mocked_install, tmp_path):
    result = runner.invoke(
        cli_mod.app,
        ["init", "--profile", "personal"],
        input="Default\nabc123extensionid\n",
    )
    assert result.exit_code == 0, result.output
    assert "personal" in mocked_install
    cfg = profiles_mod.get_profile("personal", path=registry_path)
    assert cfg.port == profiles_mod.DEFAULT_BASE_PORT
    assert cfg.chrome_profile_directory == "Default"
    assert cfg.extension_id == "abc123extensionid"
    assert (tmp_path / "run_nmh_personal.bat").exists()
    assert (tmp_path / "nmh-manifest.personal.local.json").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="init only automates Windows")
def test_init_rejects_invalid_profile_name(registry_path, mocked_install):
    result = runner.invoke(cli_mod.app, ["init", "--profile", "Not Valid"])
    assert result.exit_code == 1


@pytest.mark.parametrize("port", ["0", "65536"])
def test_init_rejects_out_of_range_port(registry_path, mocked_install, monkeypatch, tmp_path, port):
    # init's platform gate reads sys.platform at call time; pretend win32 so
    # the validation path (which fails before any platform-specific work)
    # runs on every dev platform.
    monkeypatch.setattr(sys, "platform", "win32")
    result = runner.invoke(cli_mod.app, ["init", "--profile", "personal", "--port", port])
    assert result.exit_code == 1
    assert "invalid port" in result.output
    # Rejected before writing any launcher, manifest, or registry entry.
    assert not registry_path.exists()
    assert not list(tmp_path.glob("*.bat"))
    assert not list(tmp_path.glob("nmh-manifest.*"))
    assert not mocked_install


def test_init_rejects_port_used_by_other_profile(registry_path, mocked_install, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    profiles_mod.add_profile("other", 8765, path=registry_path)
    result = runner.invoke(cli_mod.app, ["init", "--profile", "personal", "--port", "8765"])
    assert result.exit_code == 1
    assert "already used by profile 'other'" in result.output
    assert set(profiles_mod.list_profiles(registry_path)) == {"other"}
    assert not list(tmp_path.glob("*.bat"))
    assert not mocked_install


def test_init_rejects_port_matching_legacy_string_port(registry_path, mocked_install, monkeypatch, tmp_path):
    """A profile whose port was stored as a legacy string ("8765") still owns
    that port: `init --port 8765` on another profile must be rejected as a
    conflict. A raw ``cfg.port == chosen_port`` check would miss it (str != int),
    so this guards init's use of ``find_port_conflict``."""
    monkeypatch.setattr(sys, "platform", "win32")
    registry_path.write_text(
        json.dumps({"profiles": {"other": {"port": "8765"}}}), encoding="utf-8"
    )
    result = runner.invoke(cli_mod.app, ["init", "--profile", "personal", "--port", "8765"])
    assert result.exit_code == 1
    assert "already used by profile 'other'" in result.output
    assert set(profiles_mod.list_profiles(registry_path)) == {"other"}
    assert not list(tmp_path.glob("*.bat"))
    assert not mocked_install


def test_init_rerun_rejects_legacy_out_of_range_port(registry_path, mocked_install, monkeypatch, tmp_path):
    """A pre-validation registry can hold an invalid port; re-running init on
    that profile without --port must fail cleanly before any prompt or write."""
    monkeypatch.setattr(sys, "platform", "win32")
    registry_path.write_text(
        json.dumps({"profiles": {"personal": {"port": 70000}}}), encoding="utf-8"
    )
    result = runner.invoke(cli_mod.app, ["init", "--profile", "personal"])
    assert result.exit_code == 1
    assert "invalid port 70000" in result.output
    assert "--port" in result.output  # actionable remediation hint
    assert not list(tmp_path.glob("*.bat"))
    assert not mocked_install


def test_init_rerun_rejects_legacy_string_port(registry_path, mocked_install, monkeypatch, tmp_path):
    """A non-int port in a hand-edited/legacy registry must reject cleanly,
    not crash with a TypeError inside validate_port."""
    monkeypatch.setattr(sys, "platform", "win32")
    registry_path.write_text(
        json.dumps({"profiles": {"personal": {"port": "8765"}}}), encoding="utf-8"
    )
    result = runner.invoke(cli_mod.app, ["init", "--profile", "personal"])
    assert result.exit_code == 1
    assert "invalid port" in result.output
    assert "--port" in result.output
    assert not list(tmp_path.glob("*.bat"))
    assert not mocked_install


def test_init_rerun_overwrite_keeps_stored_port(registry_path, mocked_install, monkeypatch, tmp_path):
    """The overwrite-accept re-run path: validation now runs before the
    confirm, and the stored port is kept when --port is absent."""
    monkeypatch.setattr(sys, "platform", "win32")
    profiles_mod.add_profile("personal", 8770, path=registry_path)
    result = runner.invoke(
        cli_mod.app,
        ["init", "--profile", "personal"],
        input="y\nDefault\nabc123extensionid\n",
    )
    assert result.exit_code == 0, result.output
    assert "Using port 8770" in result.output
    cfg = profiles_mod.get_profile("personal", path=registry_path)
    assert cfg.port == 8770
    assert cfg.extension_id == "abc123extensionid"
    assert "personal" in mocked_install
    assert (tmp_path / "run_nmh_personal.bat").exists()


def test_init_rerun_rejects_legacy_duplicate_port(registry_path, mocked_install, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    registry_path.write_text(
        json.dumps({"profiles": {"a": {"port": 8765}, "b": {"port": 8765}}}),
        encoding="utf-8",
    )
    result = runner.invoke(cli_mod.app, ["init", "--profile", "a"])
    assert result.exit_code == 1
    assert "already used by profile 'b'" in result.output
    assert not list(tmp_path.glob("*.bat"))
    assert not mocked_install


@pytest.mark.skipif(sys.platform != "win32", reason="init only automates Windows")
def test_init_aborts_on_overwrite_decline(registry_path, mocked_install):
    profiles_mod.add_profile("personal", 8765, path=registry_path)
    result = runner.invoke(
        cli_mod.app, ["init", "--profile", "personal"], input="n\n"
    )
    assert result.exit_code == 0
    # Registry entry untouched.
    assert profiles_mod.get_profile("personal", path=registry_path).port == 8765
