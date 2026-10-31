"""The authorization matrix for every role-checked route: each allowed and denied cell.

Roles: anonymous (no session), member, lead (org role member, lead of the team in the path), admin
and owner. An allowed cell is any answer other than 401/403 (the route's own validation may still
refuse the body); a denied cell is exactly 403 for a signed-in role and 401 for anonymous.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import Engine

from conftest import Harness, RoleOrg, new_member, new_team, role_org, served, sign_in

ALLOW, DENY = "allow", "deny"
ROLE_COLUMNS = ("anonymous", "member", "lead", "admin", "owner")


def _invite(org: RoleOrg) -> dict:
    return {"email": f"new-{uuid.uuid4().hex}@example.test", "role": "member"}


def _team_members(org: RoleOrg) -> dict:
    return {
        "members": [
            {"user_id": str(org.lead.user_id), "role": "lead"},
            {"user_id": str(org.member.user_id), "role": "member"},
            {"user_id": str(org.admin.user_id), "role": "member"},
        ]
    }


# (method, path template, body builder, cells for ROLE_COLUMNS)
Case = tuple[str, Callable[[RoleOrg], str], Callable[[RoleOrg], dict] | None, tuple[str, ...]]
MATRIX: dict[str, Case] = {
    "GET /me": ("GET", lambda o: "/api/v1/me", None, (DENY, ALLOW, ALLOW, ALLOW, ALLOW)),
    "GET /org/security": (
        "GET", lambda o: "/api/v1/org/security", None, (DENY, ALLOW, ALLOW, ALLOW, ALLOW)
    ),
    "PUT /org/security": (
        "PUT",
        lambda o: "/api/v1/org/security",
        lambda o: {"require2fa": False},
        (DENY, DENY, DENY, ALLOW, ALLOW),
    ),
    "DELETE /org/users/{user_id}/mfa": (
        "DELETE",
        lambda o: f"/api/v1/org/users/{o.member.user_id}/mfa",
        None,
        (DENY, DENY, DENY, ALLOW, ALLOW),
    ),
    "GET /org/users": (
        "GET", lambda o: "/api/v1/org/users", None, (DENY, DENY, DENY, ALLOW, ALLOW)
    ),
    "POST /org/users": (
        "POST", lambda o: "/api/v1/org/users", _invite, (DENY, DENY, DENY, ALLOW, ALLOW)
    ),
    "PATCH /org/users/{user_id}": (
        "PATCH",
        lambda o: f"/api/v1/org/users/{o.member.user_id}",
        lambda o: {"role": "admin"},
        (DENY, DENY, DENY, ALLOW, ALLOW),
    ),
    "GET /org/directory": (
        "GET", lambda o: "/api/v1/org/directory", None, (DENY, ALLOW, ALLOW, ALLOW, ALLOW)
    ),
    "GET /teams": ("GET", lambda o: "/api/v1/teams", None, (DENY, ALLOW, ALLOW, ALLOW, ALLOW)),
    "POST /teams": (
        "POST",
        lambda o: "/api/v1/teams",
        lambda o: {"name": f"T {uuid.uuid4().hex[:8]}"},
        (DENY, DENY, DENY, ALLOW, ALLOW),
    ),
    "PUT /teams/{team_id}/members (own team)": (
        "PUT",
        lambda o: f"/api/v1/teams/{o.team}/members",
        _team_members,
        (DENY, DENY, ALLOW, ALLOW, ALLOW),
    ),
    "PUT /teams/{team_id}/members (another team)": (
        "PUT",
        lambda o: f"/api/v1/teams/{o.other_team}/members",
        lambda o: {"members": [{"user_id": str(o.member.user_id), "role": "member"}]},
        (DENY, DENY, DENY, ALLOW, ALLOW),
    ),
}

CELLS = [
    pytest.param(name, column, id=f"{name} as {column}")
    for name in MATRIX
    for column in ROLE_COLUMNS
]


@pytest.mark.parametrize(("name", "column"), CELLS)
def test_matrix_cell(harness: Harness, app_engine: Engine, name: str, column: str) -> None:
    method, path, body, cells = MATRIX[name]
    expected = cells[ROLE_COLUMNS.index(column)]
    org = role_org(app_engine)
    if column == "anonymous":
        client = harness.client()
    else:
        client = sign_in(harness, org.by_role(column))

    kwargs = {} if body is None else {"json": body(org)}
    response = client.request(method, path(org), **kwargs)

    if expected == ALLOW:
        assert response.status_code not in (401, 403), (name, column, response.text)
        assert response.status_code < 500, (name, column, response.text)
    elif column == "anonymous":
        assert response.status_code == 401, (name, column, response.text)
    else:
        assert response.status_code == 403, (name, column, response.text)


def test_every_role_checked_route_has_matrix_cells() -> None:
    """A route added under /org or /teams must get rows here."""
    from andai_core.main import create_app

    prefixes = ("/api/v1/org", "/api/v1/teams")
    checked = {(m, p) for m, p in served(create_app()) if p.startswith(prefixes)}
    covered = {(case[0], "/api/v1" + name.split(" ")[1]) for name, case in MATRIX.items()}
    assert checked == {(m, p) for m, p in covered if p.startswith(prefixes)}
    assert len(checked) == 10


def test_a_role_change_applies_on_the_next_request(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    owner = sign_in(harness, org.owner)
    admin = sign_in(harness, org.admin)
    member = sign_in(harness, org.member)
    assert admin.get("/api/v1/org/users").status_code == 200
    assert member.get("/api/v1/org/users").status_code == 403

    assert owner.patch(
        f"/api/v1/org/users/{org.admin.user_id}", json={"role": "member"}
    ).status_code == 200
    assert owner.patch(
        f"/api/v1/org/users/{org.member.user_id}", json={"role": "admin"}
    ).status_code == 200

    # Same sessions, next request.
    assert admin.get("/api/v1/org/users").status_code == 403
    assert member.get("/api/v1/org/users").status_code == 200
    assert admin.get("/api/v1/me").json()["role"] == "member"


def test_a_team_lead_role_change_applies_on_the_next_request(
    harness: Harness, app_engine: Engine
) -> None:
    org = role_org(app_engine)
    lead = sign_in(harness, org.lead)
    body = {"members": [{"user_id": str(org.lead.user_id), "role": "lead"}]}
    assert lead.put(f"/api/v1/teams/{org.team}/members", json=body).status_code == 200

    demote = {"members": [{"user_id": str(org.lead.user_id), "role": "member"}]}
    assert sign_in(harness, org.admin).put(
        f"/api/v1/teams/{org.team}/members", json=demote
    ).status_code == 200

    assert lead.put(f"/api/v1/teams/{org.team}/members", json=demote).status_code == 403


def test_a_lead_of_one_team_is_a_plain_member_elsewhere(
    harness: Harness, app_engine: Engine
) -> None:
    org = role_org(app_engine)
    led_by_member = new_team(app_engine, org.owner.org_id, {org.member.user_id: "lead"})
    lead = sign_in(harness, org.lead)

    for team in (org.other_team, led_by_member):
        body = {"members": [{"user_id": str(org.lead.user_id), "role": "member"}]}
        assert lead.put(f"/api/v1/teams/{team}/members", json=body).status_code == 403
    assert lead.get("/api/v1/org/users").status_code == 403
    assert lead.post("/api/v1/teams", json={"name": "Mine"}).status_code == 403


def test_require_role_rejects_unknown_roles() -> None:
    from andai_core.auth.roles import require_role

    with pytest.raises(ValueError):
        require_role("lead")
    with pytest.raises(ValueError):
        require_role()


def test_a_member_of_another_org_is_not_a_member_here(
    harness: Harness, app_engine: Engine
) -> None:
    org = role_org(app_engine)
    stranger = new_member(app_engine)  # owner of a different org
    client = sign_in(harness, stranger)

    body = {"members": [{"user_id": str(stranger.user_id), "role": "lead"}]}
    assert client.put(f"/api/v1/teams/{org.team}/members", json=body).status_code == 404
    assert client.patch(
        f"/api/v1/org/users/{org.member.user_id}", json={"role": "admin"}
    ).status_code == 404
