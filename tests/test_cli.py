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
from chrome_bridge.bridge.client import BridgeError

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
    # and released it on exit via "detach" — which never removes a tab.
    assert "attach" in ops
    assert "open" not in ops
    assert "Page.navigate" in methods
    assert "detach" in ops
    assert "close" not in ops


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
    # The fresh tab must be left open: exit goes through "detach" (never
    # removes a tab, clears the server-side reference), not "close" (which
    # would remove a bridge-created tab).
    assert "detach" in ops
    assert "close" not in ops


def test_nav_surfaces_non_no_active_tab_attach_errors(fake_bridge):
    # Only the "no active tab" failure falls back to opening a fresh tab;
    # any other attach failure (contention, restricted page) must surface.
    fake_bridge.on(
        "attach",
        lambda f: {"ok": False, "error": "Another debugger is already attached"},
    )
    fake_bridge.set_default(lambda frame: {"ok": True, "result": {}})
    result = runner.invoke(
        cli_mod.app, ["nav", "https://example.com/x", *_hostport(fake_bridge)]
    )
    assert result.exit_code != 0
    ops = [f.get("op") for f in fake_bridge.received]
    assert "open" not in ops  # no silent fresh tab on a non-empty failure


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


def test_read_one_shot_reports_attach_failure_cleanly(fake_bridge):
    # A read one-shot's attach can fail in ordinary situations (restricted
    # chrome:// page, DevTools contention). It must exit nonzero with a clean
    # message, NOT dump a raw BridgeError traceback like an uncaught exception.
    fake_bridge.on(
        "attach",
        lambda f: {"ok": False, "error": "Cannot attach to this target"},
    )
    fake_bridge.set_default(lambda frame: {"ok": True, "result": {}})
    result = runner.invoke(cli_mod.app, ["html", *_hostport(fake_bridge)])
    assert result.exit_code == 1
    # Handled cleanly: the exit came from typer.Exit (SystemExit), not an
    # uncaught BridgeError bubbling out as a traceback.
    assert not isinstance(result.exception, BridgeError)
    assert "bridge error" in result.output


class FakeSwState:
    """Stateful stand-in for sw.js's tab bookkeeping, shared across CLI runs.

    Each CLI invocation dials its own connection (its own FakeBridge), but the
    real extension's `dbgTabId`/`dbgTabOwned` and the tabs themselves persist
    between commands. This model implements the WIRE PROTOCOL's op semantics
    over that persistent state so the tests catch cross-command hazards (e.g.
    stale ownership making a later "attach" remove a tab an earlier command
    promised to leave open).
    """

    def __init__(self, active_url: str | None = None) -> None:
        self.tabs: dict[int, str] = {}
        self._next_id = 100
        self.active: int | None = None
        self.attached: int | None = None
        self.owned = False
        if active_url is not None:
            self.active = self._create(active_url)

    def _create(self, url: str) -> int:
        tab_id = self._next_id
        self._next_id += 1
        self.tabs[tab_id] = url
        return tab_id

    def _teardown(self) -> None:
        # closeTab(): detach, remove only if bridge-created.
        tab_id, owned = self.attached, self.owned
        self.attached, self.owned = None, False
        if tab_id is not None and owned:
            self.tabs.pop(tab_id, None)
            if self.active == tab_id:
                self.active = None

    def handlers_on(self, bridge: FakeBridge) -> None:
        def on_open(frame):
            self._teardown()
            tab_id = self._create(frame["url"])
            self.attached, self.owned = tab_id, True
            # A window's sole tab is active; keep the model simple: the
            # newest tab becomes active when nothing else is.
            if self.active is None:
                self.active = tab_id
            bridge.send_event(
                {"event": True, "type": "cdp", "method": "Page.loadEventFired", "params": {}}
            )
            return {"ok": True, "tabId": tab_id}

        def on_attach(frame):
            if self.active is None:
                return {"ok": False, "error": "no active tab"}
            if self.active == self.attached:
                self.owned = False
                return {"ok": True, "tabId": self.attached}
            self._teardown()
            self.attached, self.owned = self.active, False
            return {"ok": True, "tabId": self.attached}

        def on_close(frame):
            self._teardown()
            return {"ok": True}

        def on_detach(frame):
            self.attached, self.owned = None, False
            return {"ok": True}

        def on_cdp(frame):
            if self.attached is None:
                return {"ok": False, "error": "no attached tab"}
            if frame.get("method") == "Page.navigate":
                self.tabs[self.attached] = frame["params"]["url"]
                bridge.send_event(
                    {"event": True, "type": "cdp", "method": "Page.loadEventFired", "params": {}}
                )
                return {"ok": True, "result": {"frameId": "f1"}}
            if frame.get("method") == "Runtime.evaluate":
                value = f"<body>content of {self.tabs[self.attached]}</body>"
                return {"ok": True, "result": {"result": {"value": value}}}
            return {"ok": True, "result": {}}

        bridge.on("open", on_open)
        bridge.on("attach", on_attach)
        bridge.on("close", on_close)
        bridge.on("detach", on_detach)
        bridge.set_default(on_cdp)


def _run_against_state(state: FakeSwState, args: list[str]):
    b = FakeBridge()
    b.start()
    state.handlers_on(b)
    try:
        return runner.invoke(cli_mod.app, [*args, *_hostport(b)])
    finally:
        b.close()


def test_nav_then_html_two_command_workflow():
    # The end-to-end two-command flow: `nav <url>` navigates the user's active
    # tab and leaves it open; a following `html` attaches to that SAME
    # still-open tab and reads its content.
    state = FakeSwState(active_url="about:blank")
    user_tab = state.active

    result = _run_against_state(state, ["nav", "https://example.com/x"])
    assert result.exit_code == 0, result.output
    assert state.tabs.get(user_tab) == "https://example.com/x"  # navigated in place
    assert state.attached is None  # released on exit
    assert user_tab in state.tabs  # tab left open

    result = _run_against_state(state, ["html"])
    assert result.exit_code == 0, result.output
    assert "content of https://example.com/x" in result.output
    assert user_tab in state.tabs  # still open after html detached


def test_nav_fallback_then_html_leaves_fresh_tab_open():
    # No active tab at all: nav opens a fresh one, hands it to the user, and
    # a following html must attach to it WITHOUT the extension's single-tab
    # teardown removing it (stale bridge-created ownership would do exactly
    # that if nav's exit failed to clear the server-side reference).
    state = FakeSwState(active_url=None)

    result = _run_against_state(state, ["nav", "https://example.com/x"])
    assert result.exit_code == 0, result.output
    assert len(state.tabs) == 1
    fresh_tab = next(iter(state.tabs))
    assert state.tabs[fresh_tab] == "https://example.com/x"
    assert state.attached is None  # no stale attachment/ownership left behind

    result = _run_against_state(state, ["html"])
    assert result.exit_code == 0, result.output
    assert "content of https://example.com/x" in result.output
    assert fresh_tab in state.tabs  # the fresh tab survived the second command


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
