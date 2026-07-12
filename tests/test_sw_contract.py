from __future__ import annotations

import shutil
import subprocess

import pytest


def test_service_worker_tab_ownership_and_session_fencing():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    subprocess.run([node, "tests/sw_contract_test.js"], check=True)
