from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_service_worker_tab_ownership_and_session_fencing():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    subprocess.run(
        [node, str(REPO_ROOT / "tests" / "sw_contract_test.js")],
        check=True,
        cwd=REPO_ROOT,
    )
