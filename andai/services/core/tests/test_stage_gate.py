"""The two-factor stage gate, proven over every registered route.

The parametrised test enumerates `app.routes` of the real application (flattened through
`fastapi.routing.iter_route_contexts`: an included router appears in `app.routes` as one entry),
so a route added later is covered without editing this file: for each route and method, an `mfa_pending` and an
`enroll_required` session must be stopped by the gate unless the route is on that stage's
allow-list, and must get through when it is.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from sqlalchemy import Engine

from andai_core.auth.sessions import COOKIE_NAME
from andai_core.auth.stage_gate import DETAIL, NEXT_STEP
from andai_core.main import create_app
from conftest import (
    LOGIN_PATH,
    MFA_VERIFY_PATH,
    Harness,
    enroll,
    new_member,
    staged_session,
    totp_code,
)

STAGES = ("mfa_pending", "enroll_required")
ALLOWED = {
    "mfa_pending": {
        ("POST", "/api/v1/auth/mfa/verify"),
        ("POST", "/api/v1/auth/logout"),
    },
    "enroll_required": {
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/me/mfa"),
        ("POST", "/api/v1/me/mfa/confirm"),
        ("DELETE", "/api/v1/me/mfa"),
        ("POST", "/api/v1/me/mfa/recovery-codes"),
    },
}
# Routes this phase must have; guards against an enumeration that silently finds nothing.
KNOWN = {
    ("GET", "/healthz"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/me"),
    ("POST", "/api/v1/auth/mfa/verify"),
    ("POST", "/api/v1/me/mfa"),
    ("POST", "/api/v1/bootstrap"),
}


def served(app: FastAPI) -> set[tuple[str, str]]:
    """Every (method, path template) `app` serves, included routers flattened."""
    return {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        for method in context.methods or ()
    }


def registered_routes() -> list[tuple[str, str]]:
    return sorted(served(create_app()))


ROUTES = registered_routes()


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), path)


def test_the_enumeration_covers_the_live_application(harness: Harness) -> None:
    live = served(harness.client().app)
    assert set(ROUTES) == live
    assert KNOWN <= live
    for stage in STAGES:
        assert ALLOWED[stage] <= live, f"an allow-listed route of {stage} does not exist"


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_is_gated_for_two_factor_stages(
    harness: Harness, app_engine: Engine, stage: str, method: str, path: str
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    client.cookies.set(COOKIE_NAME, staged_session(app_engine, member, stage, harness.clock))

    response = client.request(method, concrete(path), json={})

    gate = {"detail": DETAIL[stage], "next_step": NEXT_STEP[stage]}
    body = response.json() if response.headers.get("content-type") == "application/json" else {}
    if (method, path) in ALLOWED[stage]:
        assert body.get("detail") not in DETAIL.values(), (stage, method, path, response.text)
    else:
        assert (response.status_code, body) == (401, gate), (stage, method, path)


@pytest.mark.parametrize(
    ("stage", "path"),
    [
        ("mfa_pending", "/no/such/path"),
        ("enroll_required", "/no/such/path"),
        ("enroll_required", "/api/v1/me/mfaX"),
        ("enroll_required", "/api/v1/me"),
        ("mfa_pending", "/api/v1/me/mfa"),
        ("mfa_pending", "/api/v1/auth/mfa/verify/"),
    ],
)
def test_paths_outside_the_allow_list_are_gated_even_without_a_route(
    harness: Harness, app_engine: Engine, stage: str, path: str
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    client.cookies.set(COOKIE_NAME, staged_session(app_engine, member, stage, harness.clock))

    response = client.post(path, json={})

    assert response.status_code == 401
    assert response.json()["next_step"] == NEXT_STEP[stage]


def test_full_sessions_and_anonymous_requests_pass(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    full = harness.client()
    full.cookies.set(COOKIE_NAME, staged_session(app_engine, member, "full", harness.clock))
    anonymous = harness.client()
    dead = harness.client()
    dead.cookies.set(COOKIE_NAME, "not-a-session")

    assert full.get("/api/v1/me").status_code == 200
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/api/v1/me").json() == {"detail": "not authenticated"}
    assert dead.get("/api/v1/me").json() == {"detail": "not authenticated"}


def test_an_expired_pending_session_is_not_treated_as_pending(
    harness: Harness, app_engine: Engine
) -> None:
    member = new_member(app_engine)
    client = harness.client()
    client.cookies.set(COOKIE_NAME, staged_session(app_engine, member, "mfa_pending", harness.clock))
    harness.clock.advance(minutes=5, seconds=1)

    response = client.get("/api/v1/me")

    assert response.status_code == 401 and response.json() == {"detail": "not authenticated"}


def test_the_gate_lifts_once_the_code_is_entered(harness: Harness, app_engine: Engine) -> None:
    member = new_member(app_engine)
    setup = harness.client()
    setup.post(LOGIN_PATH, json={"email": member.email, "password": member.password})
    totp, _ = enroll(setup, harness.clock)
    client = harness.client()

    assert client.post(
        LOGIN_PATH, json={"email": member.email, "password": member.password}
    ).json() == {"stage": "mfa_pending"}
    gated = client.get("/api/v1/me")
    assert gated.status_code == 401 and gated.json()["next_step"] == "mfa_verify"
    assert client.post(MFA_VERIFY_PATH, json={"code": totp_code(totp, harness.clock)}).json() == {
        "stage": "full"
    }

    assert client.get("/api/v1/me").status_code == 200
