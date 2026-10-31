"""`GET /api/v1/org/directory`: the org's people and teams for the share picker — names and ids
only, active members only, this org only."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from conftest import make_harness, new_member, new_team, sign_in

PATH = "/api/v1/org/directory"


def _rename(superuser_engine: Engine, user_id, name: str) -> None:
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET display_name = :n WHERE id = :u"), {"n": name, "u": user_id}
        )


@pytest.mark.parametrize("role", ["member", "admin", "owner"])
def test_a_member_lists_the_orgs_active_people_and_teams(
    app_engine: Engine, superuser_engine: Engine, role: str
) -> None:
    owner = new_member(app_engine)
    org = owner.org_id
    asha = new_member(app_engine, role="member", org_id=org)
    vikram = new_member(app_engine, role="admin", org_id=org)
    gone = new_member(app_engine, role="member", org_id=org, status="disabled")
    pending = new_member(app_engine, role="member", org_id=org, status="invited")
    caller = {"member": asha, "admin": vikram, "owner": owner}[role]
    for person, name in ((owner, "Owner Name"), (asha, "Asha Rao"), (vikram, "Vikram S"),
                         (gone, "Gone Person"), (pending, "Pending Person")):
        _rename(superuser_engine, person.user_id, name)
    sales = new_team(app_engine, org, {asha.user_id: "lead", vikram.user_id: "member"}, "Sales")
    design = new_team(app_engine, org, {asha.user_id: "member", gone.user_id: "member"}, "Design")
    elsewhere = new_member(app_engine)
    _rename(superuser_engine, elsewhere.user_id, "Elsewhere Person")
    new_team(app_engine, elsewhere.org_id, {elsewhere.user_id: "lead"}, "Elsewhere Team")

    response = sign_in(make_harness(app_engine), caller).get(PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"users", "teams"}
    assert body["users"] == [
        {"id": str(asha.user_id), "name": "Asha Rao",
         "team_ids": sorted([str(sales), str(design)])},
        {"id": str(owner.user_id), "name": "Owner Name", "team_ids": []},
        {"id": str(vikram.user_id), "name": "Vikram S", "team_ids": [str(sales)]},
    ]
    assert body["teams"] == [
        {"id": str(design), "name": "Design"},
        {"id": str(sales), "name": "Sales"},
    ]
    for absent in ("example.test", "@", "role", "Gone Person", "Pending Person", "Elsewhere",
                   str(elsewhere.org_id), "lead"):
        assert absent not in response.text, absent


def test_anonymous_is_401(app_engine: Engine) -> None:
    assert make_harness(app_engine).client().get(PATH).status_code == 401
