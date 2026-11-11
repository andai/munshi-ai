"""Runs the stubbed shell test of `andai/scripts/encryption_check.sh` as part of the andai suite."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TEST = Path(__file__).resolve().parents[1] / "scripts" / "tests" / "test_encryption_check.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_encryption_check_shell_suite() -> None:
    done = subprocess.run(
        ["bash", str(TEST)], capture_output=True, text=True, timeout=120, check=False
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert "all passed" in done.stdout
