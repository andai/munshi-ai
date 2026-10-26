"""Teams: admins create them and set their members and leads; a lead changes who is in their own
team but not who leads it; every team member belongs to the org."""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, text

from conftest import Harness, new_member, role_org, sign_in

TEAMS = "/api/v1/teams"


def members_path(team_id: uuid.UUID | str) -> str:
    return f"{TEAMS}/{team_id}/members"


def entry(user_id: uuid.UUID, role: str) -> dict:
    return {"user_id": str(user_id), "role": role}


def team_rows(superuser_engine: Engine, team_id: uuid.UUID | str) -> set[tuple[str, str]]:
    with superuser_engine.connect() as conn:
        return {
            (str(u), r)
            for u, r in conn.execute(
                text("SELECT user_id, role FROM team_memberships WHERE team_id = :t"),
                {"t": team_id},
            )
        }


def test_an_admin_creates_a_team_and_sets_its_members(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    admin = sign_in(harness, org.admin)

    created = admin.post(TEAMS, json={"name": "  Sales  "})

    assert created.status_code == 201
    team = created.json()
    assert team["name"] == "Sales" and team["members"] == []
    body = {"members": [entry(org.lead.user_id, "lead"), entry(org.member.user_id, "member")]}
    put = admin.put(members_path(team["id"]), json=body)
    assert put.status_code == 200
    assert {(m["user_id"], m["role"]) for m in put.json()["members"]} == {
        (str(org.lead.user_id), "lead"),
        (str(org.member.user_id), "member"),
    }
    listed = {t["id"]: t for t in sign_in(harness, org.member).get(TEAMS).json()["teams"]}
    assert set(listed) == {team["id"], str(org.team), str(org.other_team)}
    assert len(listed[team["id"]]["members"]) == 2
    with superuser_engine.connect() as conn:
        actions = list(
            conn.execute(
                text("SELECT action FROM audit_events WHERE target = :t ORDER BY at"),
                {"t": f"team:{team['id']}"},
            ).scalars()
        )
    assert actions == ["team.create", "team.members_set"]


def test_team_names_are_unique_in_an_org(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    other = new_member(app_engine)
    admin = sign_in(harness, org.admin)

    assert admin.post(TEAMS, json={"name": "Ops"}).status_code == 201
    assert admin.post(TEAMS, json={"name": "Ops"}).status_code == 409
    assert admin.post(TEAMS, json={"name": "   "}).status_code == 422
    # Another org may use the same name.
    assert sign_in(harness, other).post(TEAMS, json={"name": "Ops"}).status_code == 201


def test_putting_members_replaces_the_team(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    owner = sign_in(harness, org.owner)

    response = owner.put(members_path(org.team), json={"members": [entry(org.admin.user_id, "lead")]})

    assert response.status_code == 200
    assert team_rows(superuser_engine, org.team) == {(str(org.admin.user_id), "lead")}
    assert owner.put(members_path(org.team), json={"members": []}).status_code == 200
    assert team_rows(superuser_engine, org.team) == set()


def test_a_lead_changes_who_is_in_their_team_but_not_who_leads_it(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    lead = sign_in(harness, org.lead)
    me = entry(org.lead.user_id, "lead")

    added = lead.put(
        members_path(org.team), json={"members": [me, entry(org.admin.user_id, "member")]}
    )
    assert added.status_code == 200
    assert team_rows(superuser_engine, org.team) == {
        (str(org.lead.user_id), "lead"),
        (str(org.admin.user_id), "member"),
    }

    before = team_rows(superuser_engine, org.team)
    promote = {"members": [me, entry(org.admin.user_id, "lead")]}
    step_down = {"members": [entry(org.lead.user_id, "member")]}
    leave = {"members": [entry(org.admin.user_id, "member")]}
    for body in (promote, step_down, leave):
        assert lead.put(members_path(org.team), json=body).status_code == 403
    assert team_rows(superuser_engine, org.team) == before


def test_team_members_must_belong_to_the_org(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    outsider = new_member(app_engine)
    admin = sign_in(harness, org.admin)
    before = team_rows(superuser_engine, org.team)

    for user_id in (outsider.user_id, uuid.uuid4()):
        body = {"members": [entry(org.lead.user_id, "lead"), entry(user_id, "member")]}
        response = admin.put(members_path(org.team), json=body)
        assert response.status_code == 422
        assert outsider.email not in response.text
    duplicate = {"members": [entry(org.lead.user_id, "lead"), entry(org.lead.user_id, "member")]}
    assert admin.put(members_path(org.team), json=duplicate).status_code == 422
    assert admin.put(members_path(org.team), json={"members": [{"user_id": "x"}]}).status_code == 422

    assert team_rows(superuser_engine, org.team) == before


def test_unknown_teams_are_404(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    for who in (org.admin, org.member):
        response = sign_in(harness, who).put(members_path(uuid.uuid4()), json={"members": []})
        assert response.status_code == 404


def test_members_and_other_teams_leads_cannot_change_a_team(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    before = team_rows(superuser_engine, org.other_team)
    body = {"members": [entry(org.member.user_id, "lead")]}

    assert sign_in(harness, org.member).put(members_path(org.team), json=body).status_code == 403
    assert sign_in(harness, org.lead).put(
        members_path(org.other_team), json=body
    ).status_code == 403
    assert sign_in(harness, org.member).post(TEAMS, json={"name": "X"}).status_code == 403
    assert team_rows(superuser_engine, org.other_team) == before
