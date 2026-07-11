"""Tests for the chrome-bridge CLI via Typer's CliRunner.

These do not touch Chrome or a real bridge; `status`/`run` are pointed at a
tiny fake bridge server, the one-shot commands (`nav`/`html`/`click`/`fill`/
`eval`) at the scriptable :class:`FakeBridge` from ``test_client``, and
`profiles list/rm` operate against a registry path injected through
monkeypatched defaults.
"""

from __future__ import annotations

import socket
import sys
import threading

import pytest
from typer.testing import CliRunner

from chrome_bridge import cli as cli_mod
from chrome_bridge import profiles as profiles_mod

from test_client import FakeBridge

runner = CliRunner()


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    monkeypatch.setattr(profiles_mod, "default_registry_path", lambda: path)
    return path


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


def test_status_requires_profile_or_host_port():
    result = runner.invoke(cli_mod.app, ["status"])
    assert result.exit_code == 2


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
# One-shot commands — act on the ACTIVE tab via the "attach" op and leave the
# tab open on exit (the "close" op only detaches an attached tab).
# --------------------------------------------------------------------------- #


def _attach_ok(tab_id: int = 3):
    return lambda frame: {"ok": True, "tabId": tab_id}


@pytest.fixture
def fake_bridge():
    b = FakeBridge()
    b.start()
    yield b
    b.close()


def _hostport(b: FakeBridge) -> list[str]:
    return ["--host", b.host, "--port", str(b.port)]


def test_nav_attaches_and_navigates_active_tab(fake_bridge):
    fake_bridge.on("attach", _attach_ok())

    def default(frame):
        if frame.get("method") == "Page.navigate":
            # Fire the load event so navigate()'s load wait resolves fast.
            fake_bridge.send_event(
                {"event": True, "type": "cdp", "method": "Page.loadEventFired", "params": {}}
            )
            return {"ok": True, "result": {"frameId": "f1"}}
        return {"ok": True, "result": {}}

    fake_bridge.set_default(default)
    result = runner.invoke(
        cli_mod.app, ["nav", "https://example.com/x", *_hostport(fake_bridge)]
    )
    assert result.exit_code == 0, result.output
    assert "navigated to https://example.com/x" in result.output
    ops = [f.get("op") for f in fake_bridge.received]
    methods = [f.get("method") for f in fake_bridge.received if f.get("op") == "cdp"]
    # Attached to the active tab (no fresh bridge tab), navigated it in place,
    # and detached on exit — "close" on an attached tab leaves it open.
    assert "attach" in ops
    assert "open" not in ops
    assert "Page.navigate" in methods
    assert "close" in ops


def test_nav_opens_fresh_tab_when_no_active_tab_and_leaves_it_open(fake_bridge):
    fake_bridge.on("attach", lambda f: {"ok": False, "error": "no active tab"})

    def on_open(frame):
        # Resolve open()'s load wait promptly.
        threading.Timer(
            0.05,
            lambda: fake_bridge.send_event(
                {"event": True, "type": "cdp", "method": "Page.loadEventFired", "params": {}}
            ),
        ).start()
        return {"ok": True, "tabId": 4}

    fake_bridge.on("open", on_open)
    fake_bridge.set_default(lambda frame: {"ok": True, "result": {}})
    result = runner.invoke(
        cli_mod.app, ["nav", "https://example.com/x", *_hostport(fake_bridge)]
    )
    assert result.exit_code == 0, result.output
    ops = [f.get("op") for f in fake_bridge.received]
    assert "open" in ops
    # The fresh tab must be left open: the "close" op would remove a
    # bridge-created tab, so nav's exit must not send it.
    assert "close" not in ops


def test_html_prints_active_tab_content_and_detaches(fake_bridge):
    fake_bridge.on("attach", _attach_ok())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": "<body>hi</body>"}}}
        return {"ok": True, "result": {}}

    fake_bridge.set_default(default)
    result = runner.invoke(cli_mod.app, ["html", *_hostport(fake_bridge)])
    assert result.exit_code == 0, result.output
    assert "<body>hi</body>" in result.output
    ops = [f.get("op") for f in fake_bridge.received]
    assert "attach" in ops
    assert "open" not in ops
    assert "close" in ops  # detaches; the attached tab is left open server-side


def test_click_acts_on_active_tab(fake_bridge):
    fake_bridge.on("attach", _attach_ok())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": {"x": 10.0, "y": 5.0}}}}
        return {"ok": True, "result": {}}

    fake_bridge.set_default(default)
    result = runner.invoke(cli_mod.app, ["click", "button.go", *_hostport(fake_bridge)])
    assert result.exit_code == 0, result.output
    assert "clicked button.go" in result.output
    ops = [f.get("op") for f in fake_bridge.received]
    assert "attach" in ops and "open" not in ops and "close" in ops
    mouse = [
        f for f in fake_bridge.received if f.get("method") == "Input.dispatchMouseEvent"
    ]
    assert [f["params"]["type"] for f in mouse] == [
        "mouseMoved",
        "mousePressed",
        "mouseReleased",
    ]


def test_fill_acts_on_active_tab(fake_bridge):
    fake_bridge.on("attach", _attach_ok())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": True}}}
        return {"ok": True, "result": {}}

    fake_bridge.set_default(default)
    result = runner.invoke(
        cli_mod.app, ["fill", "input.q", "hello", *_hostport(fake_bridge)]
    )
    assert result.exit_code == 0, result.output
    assert "filled" in result.output
    ops = [f.get("op") for f in fake_bridge.received]
    assert "attach" in ops and "open" not in ops and "close" in ops


def test_eval_goes_through_public_attached_path(fake_bridge):
    fake_bridge.on("attach", _attach_ok())

    def default(frame):
        if frame.get("method") == "Runtime.evaluate":
            return {"ok": True, "result": {"result": {"value": 42}}}
        return {"ok": True, "result": {}}

    fake_bridge.set_default(default)
    result = runner.invoke(cli_mod.app, ["eval", "6*7", *_hostport(fake_bridge)])
    assert result.exit_code == 0, result.output
    assert "42" in result.output
    ops = [f.get("op") for f in fake_bridge.received]
    # Attach FIRST — eval must no longer bypass the attached path.
    assert ops[0] == "attach"
    assert "close" in ops


def test_nav_then_html_two_command_workflow():
    # The end-to-end two-command flow: `nav <url>` navigates the active tab and
    # leaves it open; a following `html` attaches to that same still-open tab
    # and reads its content. Each CLI invocation dials its own connection, so
    # each gets its own FakeBridge; the shared `page` dict stands in for the
    # tab that persists between the two commands.
    page: dict[str, str | None] = {"url": None}

    b1 = FakeBridge()
    b1.start()
    try:
        b1.on("attach", _attach_ok())

        def nav_default(frame):
            if frame.get("method") == "Page.navigate":
                page["url"] = frame["params"]["url"]
                b1.send_event(
                    {"event": True, "type": "cdp", "method": "Page.loadEventFired", "params": {}}
                )
            return {"ok": True, "result": {}}

        b1.set_default(nav_default)
        result = runner.invoke(
            cli_mod.app, ["nav", "https://example.com/x", *_hostport(b1)]
        )
        assert result.exit_code == 0, result.output
        assert page["url"] == "https://example.com/x"
    finally:
        b1.close()

    b2 = FakeBridge()
    b2.start()
    try:
        b2.on("attach", _attach_ok())

        def html_default(frame):
            if frame.get("method") == "Runtime.evaluate":
                value = f"<body>content of {page['url']}</body>"
                return {"ok": True, "result": {"result": {"value": value}}}
            return {"ok": True, "result": {}}

        b2.set_default(html_default)
        result = runner.invoke(cli_mod.app, ["html", *_hostport(b2)])
        assert result.exit_code == 0, result.output
        assert "content of https://example.com/x" in result.output
    finally:
        b2.close()


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


@pytest.mark.skipif(sys.platform != "win32", reason="init only automates Windows")
def test_init_aborts_on_overwrite_decline(registry_path, mocked_install):
    profiles_mod.add_profile("personal", 8765, path=registry_path)
    result = runner.invoke(
        cli_mod.app, ["init", "--profile", "personal"], input="n\n"
    )
    assert result.exit_code == 0
    # Registry entry untouched.
    assert profiles_mod.get_profile("personal", path=registry_path).port == 8765
