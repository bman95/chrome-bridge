"""Tests for chrome_bridge.runner: script execution against a fake bridge."""

from __future__ import annotations

import socket
import threading

import pytest

from chrome_bridge.bridge.frames import read_frame, write_frame
from chrome_bridge.bridge.client import ChromeBridge
from chrome_bridge.runner import get_bridge, run_script


class _EchoBridgeServer:
    """Minimal fake bridge: acks open/close, no-ops any cdp op."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.host, self.port = self._listener.getsockname()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._stop = threading.Event()

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        client, _ = self._listener.accept()
        rfile = client.makefile("rb")
        wfile = client.makefile("wb")
        try:
            while not self._stop.is_set():
                try:
                    frame = read_frame(rfile)
                except (OSError, ValueError):
                    break
                if frame is None:
                    break
                op = frame.get("op")
                if op == "open":
                    write_frame(wfile, {"id": frame["id"], "ok": True, "tabId": 1})
                elif op == "close":
                    write_frame(wfile, {"id": frame["id"], "ok": True})
                else:
                    write_frame(wfile, {"id": frame["id"], "ok": True, "result": {}})
        except OSError:
            pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass


@pytest.fixture
def echo_server():
    s = _EchoBridgeServer()
    s.start()
    yield s
    s.close()


def test_get_bridge_outside_run_script_raises():
    with pytest.raises(RuntimeError, match="outside"):
        get_bridge()


def test_run_script_exposes_bridge_via_get_bridge(tmp_path, echo_server):
    script = tmp_path / "script.py"
    script.write_text(
        "from chrome_bridge.runner import get_bridge\n"
        "cdp = get_bridge()\n"
        "print('got', type(cdp).__name__)\n"
    )
    bridge = ChromeBridge(host=echo_server.host, port=echo_server.port)
    run_script(script, bridge=bridge)
    assert not bridge.is_alive()  # closed in the finally block


def test_run_script_passes_argv(tmp_path, echo_server, capsys):
    script = tmp_path / "script.py"
    script.write_text(
        "import sys\n"
        "print(sys.argv[1:])\n"
    )
    bridge = ChromeBridge(host=echo_server.host, port=echo_server.port)
    run_script(script, bridge=bridge, script_args=["a", "b"])
    out = capsys.readouterr().out
    assert "['a', 'b']" in out


def test_run_script_closes_bridge_even_on_script_exception(tmp_path, echo_server):
    script = tmp_path / "script.py"
    script.write_text("raise ValueError('boom')\n")
    bridge = ChromeBridge(host=echo_server.host, port=echo_server.port)
    with pytest.raises(ValueError, match="boom"):
        run_script(script, bridge=bridge)
    assert not bridge.is_alive()


def test_get_bridge_not_visible_after_run_script_returns(tmp_path, echo_server):
    script = tmp_path / "script.py"
    script.write_text("pass\n")
    bridge = ChromeBridge(host=echo_server.host, port=echo_server.port)
    run_script(script, bridge=bridge)
    with pytest.raises(RuntimeError, match="outside"):
        get_bridge()
