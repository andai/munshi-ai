"""Who may read, stop, share and delete a meeting (`meetings/access.py`), over the routes that read
or stop one (detail, transcript, live, minutes, stop): nine callers against `.work/AUTH_MATRIX.md`.

The org has an owner, an admin, the meeting's requester, a lead of the meeting's team, a plain
member of that team, a user the meeting is shared with, a member of a team the meeting is shared
with, and an unrelated member; a second org's owner is the ninth caller (`conftest.access_org`).
Shares are inserted directly here; the share routes are `test_shares.py`, the list is
`test_meetings_list.py`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, delete, select, text

from andai_core.db.models import Meeting, MeetingShare, TeamMembership
from andai_core.db.session import transaction
from andai_core.meetings import access
from conftest import CALLERS, DELETERS, READERS, SHARERS, STOPPERS, AccessOrg, access_org


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    w = access_org(app_engine)
    w.meeting_id = w.meeting()
    return w


def _refused(response) -> bool:
    return response.status_code == 404 and response.json()["code"] == "no_such_meeting"


# ─── the routes ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_detail(world: AccessOrg, caller: str) -> None:
    response = world.client(caller).get(f"/api/v1/meetings/{world.meeting_id}")

    if caller in READERS:
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(world.meeting_id)
        assert body["permissions"] == {
            "can_share": caller in SHARERS,
            "can_delete": caller in DELETERS,
            "can_stop": caller in STOPPERS,
        }
        assert body["team"] == {"id": str(world.team), "name": body["team"]["name"]}
        assert body["requested_by_name"] == "Test Member"
    else:
        assert _refused(response), response.text


@pytest.mark.parametrize("caller", CALLERS)
def test_transcript(world: AccessOrg, caller: str) -> None:
    response = world.client(caller).get(f"/api/v1/meetings/{world.meeting_id}/transcript")

    if caller in READERS:
        assert response.status_code == 200, response.text
        assert response.json() == {"meeting_id": str(world.meeting_id), "segments": []}
    else:
        assert _refused(response), response.text


@pytest.mark.parametrize("caller", CALLERS)
def test_live(world: AccessOrg, caller: str) -> None:
    response = world.client(caller).get(f"/api/v1/meetings/{world.meeting_id}/live")

    if caller in READERS:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.startswith("event: status\n")  # ended: one status, then the end
    else:
        assert _refused(response), response.text


@pytest.mark.parametrize("caller", CALLERS)
def test_minutes(world: AccessOrg, caller: str) -> None:
    response = world.client(caller).get(f"/api/v1/meetings/{world.meeting_id}/mom")

    assert response.status_code == 404, response.text
    if caller in READERS:  # readable, no minutes yet; the content is test_mom_read.py
        assert response.json()["code"] == "no_minutes"
    else:
        assert _refused(response), response.text


@pytest.mark.parametrize("caller", CALLERS)
def test_stop(world: AccessOrg, caller: str, superuser_engine: Engine) -> None:
    meeting_id = world.meeting(status="recording")

    response = world.client(caller).post(f"/api/v1/meetings/{meeting_id}/stop")

    with superuser_engine.connect() as conn:
        stops = conn.execute(
            text("SELECT count(*) FROM audit_events WHERE action = 'meeting.stop' AND target = :t"),
            {"t": f"meeting:{meeting_id}"},
        ).scalar_one()
    if caller in STOPPERS:
        assert response.status_code == 200, response.text
        assert stops == 1
    else:
        assert _refused(response), response.text
        assert stops == 0


# ─── the functions ────────────────────────────────────────────────────────────────────────────────


def _loaded(world: AccessOrg, meeting_id: uuid.UUID) -> Meeting:
    with transaction(world.engine, org_id=world.org_id) as tx:
        return tx.get(Meeting, meeting_id)


@pytest.mark.parametrize("caller", CALLERS)
def test_access_functions(world: AccessOrg, caller: str) -> None:
    meeting = _loaded(world, world.meeting_id)
    actor = world.actor(caller)

    with transaction(world.engine, org_id=actor.org_id, user_id=actor.user_id) as tx:
        listed = tx.scalars(
            select(Meeting.id).where(Meeting.id == world.meeting_id, access.accessible(actor))
        ).all()
        outcome = {
            "read": access.can_read(tx, actor, meeting),
            "stop": access.can_stop(tx, actor, meeting),
            "share": access.can_share(tx, actor, meeting),
            "delete": access.can_delete(tx, actor, meeting),
        }

    assert listed == ([world.meeting_id] if caller in READERS else [])
    assert outcome == {
        "read": caller in READERS,
        "stop": caller in STOPPERS,
        "share": caller in SHARERS,
        "delete": caller in DELETERS,
    }


def test_the_other_org_owner_is_refused_even_holding_the_meeting_row(world: AccessOrg) -> None:
    """An admin of another org is not an admin of this meeting: the functions check the org
    before the role."""
    meeting = _loaded(world, world.meeting_id)
    actor = world.actor("other_org")

    with transaction(world.engine, org_id=actor.org_id) as tx:
        assert not any(
            check(tx, actor, meeting)
            for check in (access.can_read, access.can_stop, access.can_share, access.can_delete)
        )


# ─── read time, not grant time ────────────────────────────────────────────────────────────────────


def test_a_revoked_share_is_404_on_the_next_request(world: AccessOrg) -> None:
    meeting_id = world.meeting()
    grantee = world.client("user_grantee")
    path = f"/api/v1/meetings/{meeting_id}"
    assert grantee.get(path).status_code == 200

    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            delete(MeetingShare).where(
                MeetingShare.meeting_id == meeting_id, MeetingShare.grantee_type == "user"
            )
        )

    assert _refused(grantee.get(path))
    assert _refused(grantee.get(f"{path}/transcript"))
    assert _refused(grantee.get(f"{path}/live"))
    assert _refused(grantee.get(f"{path}/mom"))
    assert world.client("team_grantee").get(path).status_code == 200, "the team share stays"


def test_team_shares_follow_team_membership(world: AccessOrg) -> None:
    newcomer = world.people["unrelated"]
    meeting_id = world.meeting()
    path = f"/api/v1/meetings/{meeting_id}"
    assert _refused(world.client("unrelated").get(path))

    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.add(
            TeamMembership(
                team_id=world.shared_team, org_id=world.org_id, user_id=newcomer.user_id,
                role="member",
            )
        )
    try:
        assert world.client("unrelated").get(path).status_code == 200
    finally:
        with transaction(world.engine, org_id=world.org_id) as tx:
            tx.execute(
                delete(TeamMembership).where(
                    TeamMembership.team_id == world.shared_team,
                    TeamMembership.user_id == newcomer.user_id,
                )
            )
    assert _refused(world.client("unrelated").get(path))


def test_a_lead_of_the_shared_team_reads_but_cannot_stop(world: AccessOrg) -> None:
    """Leading a team the meeting is shared with is being a grantee, not the meeting's lead."""
    meeting_id = world.meeting(status="recording")
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text("UPDATE team_memberships SET role = 'lead' WHERE team_id = :t AND user_id = :u"),
            {"t": world.shared_team, "u": world.people["team_grantee"].user_id},
        )
    try:
        client = world.client("team_grantee")
        assert client.get(f"/api/v1/meetings/{meeting_id}").status_code == 200
        assert _refused(client.post(f"/api/v1/meetings/{meeting_id}/stop"))
    finally:
        with transaction(world.engine, org_id=world.org_id) as tx:
            tx.execute(
                text(
                    "UPDATE team_memberships SET role = 'member'"
                    " WHERE team_id = :t AND user_id = :u"
                ),
                {"t": world.shared_team, "u": world.people["team_grantee"].user_id},
            )
