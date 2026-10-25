"""Health-check contract for the Phase 1 andai service skeletons."""

import importlib

import pytest
from fastapi.testclient import TestClient

SERVICES = [
    ("andai_core.main", "andai-core"),
    ("andai_worker.main", "andai-worker"),
    ("andai_intake.main", "andai-intake"),
]


@pytest.mark.parametrize(("module_name", "service_name"), SERVICES)
def test_healthz_reports_own_service(module_name: str, service_name: str) -> None:
    module = importlib.import_module(module_name)
    client = TestClient(module.app)

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == service_name
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


@pytest.mark.parametrize(("module_name", "service_name"), SERVICES)
def test_healthz_version_from_env(
    module_name: str, service_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANDAI_VERSION", "1.2.3")
    module = importlib.import_module(module_name)

    body = TestClient(module.app).get("/healthz").json()

    assert body == {"service": service_name, "status": "ok", "version": "1.2.3"}
