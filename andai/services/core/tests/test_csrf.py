"""The Origin check's one exemption (`auth/csrf.py`): the platform's signed webhook path, matched
exactly. Every other state-changing request without the portal's `Origin` is still refused."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from andai_core.auth.csrf import SIGNED_PATHS
from andai_core.main import create_app
from conftest import PORTAL_ORIGIN, make_harness
from test_platform_webhook import SECRET, envelope, raw, signed

REFUSED = {"detail": "cross-site request refused"}


@pytest.fixture
def client(app_engine: Engine) -> TestClient:
    harness = make_harness(app_engine, ANDAI_PLATFORM_WEBHOOK_SECRET=SECRET)
    return TestClient(create_app(harness.services))  # no Origin header


def test_exactly_one_path_is_exempt() -> None:
    assert SIGNED_PATHS == frozenset({"/internal/platform-events"})


def test_a_signed_webhook_without_origin_reaches_the_handler(client: TestClient) -> None:
    body = raw(envelope(10**12 + 1))

    response = client.post("/internal/platform-events", content=body, headers=signed(body))

    assert (response.status_code, response.json()) == (202, {"status": "accepted"})


def test_a_foreign_origin_does_not_matter_to_the_signed_path(client: TestClient) -> None:
    body = raw(envelope(10**12 + 2))
    headers = {**signed(body), "Origin": "https://evil.example"}

    response = client.post("/internal/platform-events", content=body, headers=headers)

    assert response.status_code == 202


@pytest.mark.parametrize(
    "path",
    [
        "/internal/platform-events/x",
        "/internal/platform-events/",
        "/internal/platform-eventsx",
        "/internal/other",
        "/api/v1/auth/logout",
        "/api/v1/meetings/join",
        "/api/internal/platform-events",
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_other_path_without_origin_is_still_refused(
    client: TestClient, path: str, method: str
) -> None:
    body = raw(envelope(10**12 + 3))

    response = client.request(method, path, content=body, headers=signed(body))

    assert (response.status_code, response.json()) == (403, REFUSED)


def test_other_methods_on_the_signed_path_still_need_an_origin(client: TestClient) -> None:
    for method in ("PUT", "PATCH", "DELETE"):
        assert client.request(method, "/internal/platform-events/x").status_code == 403
    # On the exact path the Origin check stands aside; the router answers for the method.
    assert client.request("PUT", "/internal/platform-events").status_code == 405


def test_the_portal_origin_still_passes_elsewhere(app_engine: Engine) -> None:
    harness = make_harness(app_engine)
    client = TestClient(create_app(harness.services), headers={"Origin": PORTAL_ORIGIN})

    assert client.post("/api/v1/auth/logout").status_code != 403
