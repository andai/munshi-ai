"""`bot.autoJoin` per org and team: the store (migration 0014), the routes and the resolver."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from andai_core.db.models import BotSetting
from andai_core.db.session import transaction
from andai_core.org.bot_settings import effective_auto_join
from conftest import Harness, new_team, role_org, sign_in

PATH = "/api/v1/org/bot-settings"
ACTION = "bot.auto_join.update"


def audit_events(superuser_engine: Engine, org_id: uuid.UUID) -> list[tuple[str, uuid.UUID]]:
    with superuser_engine.connect() as conn:
        return sorted(
            (target, actor)
            for target, actor in conn.execute(
                text(
                    "SELECT target, actor_user_id FROM audit_events"
                    " WHERE org_id = :o AND action = :a"
                ),
                {"o": org_id, "a": ACTION},
            ).all()
        )


def stored(superuser_engine: Engine, org_id: uuid.UUID) -> dict:
    with superuser_engine.connect() as conn:
        return {
            team: (value, by)
            for team, value, by in conn.execute(
                text(
                    "SELECT team_id, auto_join, updated_by FROM bot_settings WHERE org_id = :o"
                ),
                {"o": org_id},
            ).all()
        }


def team_value(body: dict, team_id: uuid.UUID) -> str | None:
    [value] = [t["autoJoin"] for t in body["teams"] if t["teamId"] == str(team_id)]
    return value


def test_an_org_starts_at_only_when_invited_with_no_team_overrides(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)

    body = sign_in(harness, org.admin).get(PATH).json()

    assert body["autoJoin"] == "only_when_invited"
    assert {t["teamId"] for t in body["teams"]} == {str(org.team), str(org.other_team)}
    assert all(t["autoJoin"] is None for t in body["teams"])
    assert set(body) == {"autoJoin", "teams"}
    assert stored(superuser_engine, org.owner.org_id) == {}


@pytest.mark.parametrize("role", ["member", "lead"])
def test_members_and_leads_neither_read_nor_change_it(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, role: str
) -> None:
    org = role_org(app_engine)
    client = sign_in(harness, org.by_role(role))

    assert client.get(PATH).status_code == 403
    put = client.put(
        PATH, json={"autoJoin": "off", "teams": [{"teamId": str(org.team), "autoJoin": "off"}]}
    )
    assert put.status_code == 403
    assert harness.client().get(PATH).status_code == 401
    assert stored(superuser_engine, org.owner.org_id) == {}
    assert audit_events(superuser_engine, org.owner.org_id) == []


def test_admins_and_the_owner_set_the_org_value_and_team_overrides_and_each_change_is_audited(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    org_id = org.owner.org_id
    admin = sign_in(harness, org.admin)
    owner = sign_in(harness, org.owner)

    first = admin.put(
        PATH,
        json={
            "autoJoin": "all_with_link",
            "teams": [{"teamId": str(org.team), "autoJoin": "off"}],
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["autoJoin"] == "all_with_link"
    assert team_value(first.json(), org.team) == "off"
    assert team_value(first.json(), org.other_team) is None
    assert owner.get(PATH).json() == first.json()
    assert stored(superuser_engine, org_id) == {
        None: ("all_with_link", org.admin.user_id),
        org.team: ("off", org.admin.user_id),
    }

    # The same values again change nothing and audit nothing; `teams` may be left out.
    assert admin.put(PATH, json={"autoJoin": "all_with_link"}).json() == first.json()
    assert len(audit_events(superuser_engine, org_id)) == 2

    # The owner changes the override, adds another and leaves the org value as it is.
    second = owner.put(
        PATH,
        json={
            "autoJoin": "all_with_link",
            "teams": [
                {"teamId": str(org.team), "autoJoin": "only_when_invited"},
                {"teamId": str(org.other_team), "autoJoin": "off"},
            ],
        },
    ).json()
    assert team_value(second, org.team) == "only_when_invited"
    assert team_value(second, org.other_team) == "off"

    # null removes an override; an unlisted team keeps its own.
    third = admin.put(
        PATH, json={"autoJoin": "off", "teams": [{"teamId": str(org.team), "autoJoin": None}]}
    ).json()
    assert third["autoJoin"] == "off"
    assert team_value(third, org.team) is None
    assert team_value(third, org.other_team) == "off"
    assert stored(superuser_engine, org_id) == {
        None: ("off", org.admin.user_id),
        org.other_team: ("off", org.owner.user_id),
    }

    assert audit_events(superuser_engine, org_id) == sorted(
        [
            (f"org:{org_id}", org.admin.user_id),
            (f"team:{org.team}", org.admin.user_id),
            (f"team:{org.team}", org.owner.user_id),
            (f"team:{org.other_team}", org.owner.user_id),
            (f"org:{org_id}", org.admin.user_id),
            (f"team:{org.team}", org.admin.user_id),
        ]
    )


@pytest.mark.parametrize(
    "body",
    [
        {"autoJoin": "always"},
        {"autoJoin": None},
        {},
        {"autoJoin": "off", "teams": [{"teamId": "TEAM", "autoJoin": "sometimes"}]},
        {"autoJoin": "off", "teams": [{"teamId": "TEAM"}]},
        {"autoJoin": "off", "teams": [{"teamId": "not-a-uuid", "autoJoin": "off"}]},
        {"autoJoin": "off", "extra": True},
        {"autoJoin": "off", "teams": [{"teamId": "TEAM", "autoJoin": "off", "extra": 1}]},
        {
            "autoJoin": "off",
            "teams": [
                {"teamId": "TEAM", "autoJoin": "off"},
                {"teamId": "TEAM", "autoJoin": None},
            ],
        },
    ],
    ids=[
        "unknown org value",
        "null org value",
        "no org value",
        "unknown team value",
        "team value missing",
        "bad team id",
        "unknown field",
        "unknown team field",
        "team listed twice",
    ],
)
def test_an_invalid_body_is_422_and_changes_nothing(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, body: dict
) -> None:
    org = role_org(app_engine)
    for team in body.get("teams", []):
        if team["teamId"] == "TEAM":
            team["teamId"] = str(org.team)

    response = sign_in(harness, org.admin).put(PATH, json=body)

    assert response.status_code == 422, response.text
    assert stored(superuser_engine, org.owner.org_id) == {}
    assert audit_events(superuser_engine, org.owner.org_id) == []


def test_a_team_of_another_org_is_404_and_nothing_changes_in_either_org(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    other = role_org(app_engine)
    admin = sign_in(harness, org.admin)

    response = admin.put(
        PATH,
        json={
            "autoJoin": "off",
            "teams": [
                {"teamId": str(org.team), "autoJoin": "off"},
                {"teamId": str(other.team), "autoJoin": "off"},
            ],
        },
    )
    missing = admin.put(
        PATH, json={"autoJoin": "off", "teams": [{"teamId": str(uuid.uuid4()), "autoJoin": "off"}]}
    )

    assert response.status_code == 404, response.text
    assert missing.status_code == 404, missing.text
    assert str(other.team) not in response.text
    for org_id in (org.owner.org_id, other.owner.org_id):
        assert stored(superuser_engine, org_id) == {}
        assert audit_events(superuser_engine, org_id) == []
    # The other org's teams are not in this org's answer either.
    assert str(other.team) not in admin.get(PATH).text


def test_effective_auto_join_takes_the_team_override_else_the_org_value_else_the_default(
    harness: Harness, app_engine: Engine
) -> None:
    org = role_org(app_engine)
    other = role_org(app_engine)
    org_id = org.owner.org_id

    def effective(team: uuid.UUID | None, in_org: uuid.UUID = org_id) -> str:
        with transaction(app_engine, org_id=in_org) as tx:
            return effective_auto_join(tx, in_org, team)

    assert effective(None) == effective(org.team) == "only_when_invited"

    admin = sign_in(harness, org.admin)
    admin.put(PATH, json={"autoJoin": "off"})
    assert effective(None) == effective(org.team) == effective(org.other_team) == "off"

    override = {"teamId": str(org.team), "autoJoin": "all_with_link"}
    admin.put(PATH, json={"autoJoin": "off", "teams": [override]})
    assert effective(org.team) == "all_with_link"
    assert effective(org.other_team) == effective(None) == "off"
    # A team of another org has no override here; its own org is untouched.
    assert effective(other.team) == "off"
    assert effective(other.team, other.owner.org_id) == "only_when_invited"

    removed = {"teamId": str(org.team), "autoJoin": None}
    admin.put(PATH, json={"autoJoin": "only_when_invited", "teams": [removed]})
    assert effective(org.team) == effective(None) == "only_when_invited"


def test_rows_stay_in_their_org(app_engine: Engine, superuser_engine: Engine) -> None:
    org = role_org(app_engine)
    other = role_org(app_engine)
    org_id, other_id = org.owner.org_id, other.owner.org_id
    with transaction(app_engine, org_id=org_id) as tx:
        tx.add(BotSetting(org_id=org_id, team_id=None, auto_join="off"))
        tx.add(BotSetting(org_id=org_id, team_id=org.team, auto_join="all_with_link"))

    with transaction(app_engine, org_id=other_id) as tx:
        assert tx.execute(text("SELECT count(*) FROM bot_settings")).scalar_one() == 0
        assert tx.execute(text("UPDATE bot_settings SET auto_join = 'off'")).rowcount == 0
        assert tx.execute(text("DELETE FROM bot_settings")).rowcount == 0
        assert effective_auto_join(tx, org_id, org.team) == "only_when_invited"  # sees nothing
    with pytest.raises(ProgrammingError, match="row-level security"):
        with transaction(app_engine, org_id=other_id) as tx:
            tx.add(BotSetting(org_id=org_id, team_id=None, auto_join="all_with_link"))
    # A team override names a team of its own org.
    with pytest.raises(IntegrityError, match="fk_bot_settings_team_id_org_id_teams"):
        with transaction(app_engine, org_id=org_id) as tx:
            tx.add(BotSetting(org_id=org_id, team_id=other.team, auto_join="off"))
    # One row per scope, the org's own included (NULLS NOT DISTINCT).
    with pytest.raises(IntegrityError, match="uq_bot_settings_org_id_team_id"):
        with transaction(app_engine, org_id=org_id) as tx:
            tx.add(BotSetting(org_id=org_id, team_id=None, auto_join="all_with_link"))
    with pytest.raises(IntegrityError, match="ck_bot_settings_auto_join"):
        with transaction(app_engine, org_id=org_id) as tx:
            tx.add(BotSetting(org_id=org_id, team_id=org.other_team, auto_join="sometimes"))

    assert stored(superuser_engine, org_id) == {
        None: ("off", None),
        org.team: ("all_with_link", None),
    }
    # An override goes with its team.
    with superuser_engine.begin() as conn:
        conn.execute(text("DELETE FROM teams WHERE id = :t"), {"t": org.team})
    assert stored(superuser_engine, org_id) == {None: ("off", None)}


def test_a_new_team_has_no_override(harness: Harness, app_engine: Engine) -> None:
    org = role_org(app_engine)
    admin = sign_in(harness, org.admin)
    admin.put(PATH, json={"autoJoin": "all_with_link"})

    team = new_team(app_engine, org.owner.org_id, {org.member.user_id: "member"})

    assert team_value(admin.get(PATH).json(), team) is None
    with transaction(app_engine, org_id=org.owner.org_id) as tx:
        assert effective_auto_join(tx, org.owner.org_id, team) == "all_with_link"
