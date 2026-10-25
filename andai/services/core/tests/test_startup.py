"""Service startup: outside dev, andai-core refuses to start without a key-encryption key."""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient

from andai_core.crypto import KeyUnavailable
from andai_core.main import create_app
from andai_core.settings import get_settings
from conftest import Postgres


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANDAI_", "SMTP_"))}
    env.update(extra)
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(env: dict[str, str]) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "andai_core.main:app", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, port


@pytest.fixture
def no_kek_production(monkeypatch: pytest.MonkeyPatch):
    for name in ("ANDAI_KEK", "ANDAI_KEK_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANDAI_ENV", "production")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_lifespan_refuses_without_a_kek_outside_dev(no_kek_production: None) -> None:
    app = create_app()
    with pytest.raises(KeyUnavailable), TestClient(app):
        pass


def test_uvicorn_exits_without_a_kek_outside_dev() -> None:
    proc, _ = _serve(_clean_env(ANDAI_ENV="production"))
    try:
        output, _ = proc.communicate(timeout=60)
    finally:
        proc.kill()
    assert proc.returncode != 0
    assert "KeyUnavailable" in output and "ANDAI_KEK" in output


def test_uvicorn_starts_with_a_kek_outside_dev(migrated: Postgres) -> None:
    kek = base64.b64encode(os.urandom(32)).decode()
    proc, port = _serve(
        _clean_env(ANDAI_ENV="production", ANDAI_KEK=kek, ANDAI_DB_URL_APP=migrated.app_url)
    )
    try:
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                    status = r.status
                    break
            except OSError:
                time.sleep(0.3)
        assert status == 200, proc.stdout.read() if proc.poll() is not None else "no answer"
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    assert kek not in (proc.stdout.read() or "")
