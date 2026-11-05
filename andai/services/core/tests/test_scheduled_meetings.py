"""Meetings scheduled from invites: the skip toggle, the Upcoming section, and paste-links next to
a scheduled meeting of the same link.

The skip column of the nine-caller matrix (`conftest.CALLERS`): the requester, a lead of the
meeting's team, admins and owners may skip (`SHARERS`); everyone else, grantees included, gets 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.db.models import ConsentRecord, InviteEvent
from andai_core.db.session import transaction
from andai_core.meetings import scheduled
from andai_core.meetings.service import join_url_key
from conftest import (
    CALLERS,
    SHARERS,
    AccessOrg,
    access_org,
    give_principal,
    make_harness,
    role_org,
    sign_in,
    zoom_link,
)

LIST = "/api/v1/meetings"
JOIN = "/api/v1/meetings/join"
T0 = datetime(2031, 3, 3, 9, 0, tzinfo=UTC)


def skip_path(meeting_id: uuid.UUID) -> str:
    return f"/api/v1/meetings/{meeting_id}/skip"


@pytest.fixture(scope="module")
def world(app_engine: Engine) -> AccessOrg:
    return access_org(app_engine)


def scheduled_meeting(world: AccessOrg, start: datetime = T0, **fields) -> uuid.UUID:
    meeting_id = world.meeting(status="scheduled", **fields)
    with transaction(world.engine, org_id=world.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET scheduled_start = :s, source = 'invite_mail' WHERE id = :m"),
            {"s": start, "m": meeting_id},
        )
    return meeting_id


def skip_state(superuser_engine: Engine, meeting_id: uuid.UUID) -> tuple:
    with superuser_engine.connect() as conn:
        row = conn.execute(
            text("SELECT skipped_at, skipped_by FROM meetings WHERE id = :m"), {"m": meeting_id}
        ).one()
        actions = list(
            conn.execute(
                text("SELECT action FROM audit_events WHERE target = :t ORDER BY at, action"),
                {"t": f"meeting:{meeting_id}"},
            ).scalars()
        )
    return row.skipped_at is not None, row.skipped_by, actions


# ─── skip ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", CALLERS)
def test_who_may_skip_and_unskip(world: AccessOrg, superuser_engine: Engine, caller: str) -> None:
    meeting_id = scheduled_meeting(world)
    client = world.client(caller)

    skipped = client.post(skip_path(meeting_id))
    if caller not in SHARERS:
        assert (skipped.status_code, skipped.json()["code"]) == (404, "no_such_meeting")
        assert client.delete(skip_path(meeting_id)).status_code == 404
        assert skip_state(superuser_engine, meeting_id) == (False, None, [])
        return

    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["skipped"] is True
    assert skipped.json()["scheduled_start"] == T0.isoformat()
    user = world.people[caller].user_id
    assert skip_state(superuser_engine, meeting_id) == (True, user, ["meeting.skip"])
    assert client.post(skip_path(meeting_id)).status_code == 200  # again: nothing changes
    assert skip_state(superuser_engine, meeting_id)[2] == ["meeting.skip"]

    unskipped = client.delete(skip_path(meeting_id))
    assert unskipped.status_code == 200 and unskipped.json()["skipped"] is False
    assert skip_state(superuser_engine, meeting_id) == (
        False, None, ["meeting.skip", "meeting.unskip"]
    )


@pytest.mark.parametrize("status", ["dispatching", "recording", "done", "cancelled"])
def test_only_a_scheduled_meeting_can_be_skipped(world: AccessOrg, status: str) -> None:
    meeting_id = world.meeting(status=status)
    response = world.client("requester").post(skip_path(meeting_id))
    assert (response.status_code, response.json()["code"]) == (409, "not_scheduled")
    assert world.client("requester").delete(skip_path(meeting_id)).status_code == 409


def test_another_orgs_meeting_cannot_be_skipped(world: AccessOrg) -> None:
    meeting_id = scheduled_meeting(world)
    response = world.client("other_org").post(skip_path(meeting_id))
    assert response.status_code == 404
    assert world.client("other_org").post(skip_path(uuid.uuid4())).status_code == 404


# ─── the Upcoming section ─────────────────────────────────────────────────────────────────────────


def upcoming(client, **params) -> list[dict]:
    items, cursor = [], None
    for _ in range(50):
        query = {"cursor": cursor} if cursor else {"section": "upcoming", **params}
        page = client.get(LIST, params=query).json()["sections"]["upcoming"]
        items += page["items"]
        cursor = page["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("the pages never ended")


def test_upcoming_lists_scheduled_meetings_soonest_first(app_engine: Engine) -> None:
    w = access_org(app_engine)
    starts = [T0 + timedelta(days=d) for d in (3, 1, 2, 5, 4)]
    made = [scheduled_meeting(w, start) for start in starts]
    skipped_id = made[2]
    with transaction(app_engine, org_id=w.org_id) as tx:
        tx.execute(text("UPDATE meetings SET skipped_at = now() WHERE id = :m"), {"m": skipped_id})

    items = upcoming(w.client("requester"), limit=2)

    assert [i["id"] for i in items] == [
        str(m) for _, m in sorted(zip(starts, made, strict=True), key=lambda p: p[0])
    ]
    assert [i["scheduled_start"] for i in items] == [s.isoformat() for s in sorted(starts)]
    assert {i["id"]: i["skipped"] for i in items}[str(skipped_id)] is True
    assert sum(i["skipped"] for i in items) == 1
    assert {i["source"] for i in items} == {"invite_mail"}


@pytest.mark.parametrize("caller", sorted({"requester", "lead", "admin", "owner",
                                           "user_grantee", "team_grantee"}))
def test_each_row_says_whether_the_caller_may_share_and_skip_it(
    world: AccessOrg, caller: str
) -> None:
    meeting_id = scheduled_meeting(world, T0 - timedelta(days=3650))  # first in the list
    [item] = [i for i in upcoming(world.client(caller)) if i["id"] == str(meeting_id)]
    assert item["permissions"] == {"can_share": caller in SHARERS}


def test_past_and_live_rows_carry_the_same_fields(world: AccessOrg) -> None:
    meeting_id = world.meeting(status="done")
    body = world.client("lead").get(LIST, params={"section": "past", "limit": 50}).json()
    [item] = [i for i in body["sections"]["past"]["items"] if i["id"] == str(meeting_id)]
    assert item["permissions"] == {"can_share": True}
    assert (item["scheduled_start"], item["skipped"], item["source"]) == (
        None, False, "portal_link"
    )


# ─── paste-links next to a scheduled meeting ──────────────────────────────────────────────────────


def test_a_link_with_a_scheduled_invite_meeting_still_dispatches(
    app_engine: Engine, superuser_engine: Engine
) -> None:
    harness = make_harness(app_engine)
    org = role_org(app_engine)
    give_principal(harness, app_engine, org.owner.org_id)
    link = zoom_link()
    org_id = org.owner.org_id
    event_id, planned = uuid.uuid4(), uuid.uuid4()
    with transaction(app_engine, org_id=org_id) as tx:
        tx.add(InviteEvent(id=event_id, org_id=org_id, uid_norm=f"{event_id}@x", sequence=0,
                           state="active", requested_by=org.member.user_id))
        tx.flush()
        scheduled.insert_scheduled(
            tx, meeting_id=planned, org_id=org_id, invite_event_id=event_id,
            occurrence_key=T0, scheduled_start=T0, requested_by=org.member.user_id,
            team_id=None, join_url=link, platform="zoom", title="Planned", language="en",
            now=harness.clock(),
        )
    member = sign_in(harness, org.member)

    first = member.post(JOIN, json={"join_url": link})
    second = member.post(JOIN, json={"join_url": link})

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"] != str(planned)
    assert len(harness.gateway.dispatches) == 1

    # D-38 still applies: the bot was removed from the pasted meeting an hour ago.
    at = harness.clock() - timedelta(hours=1)
    with transaction(app_engine, org_id=org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = 'done', end_reason = 'evicted' WHERE id = :m"),
            {"m": first.json()["id"]},
        )
        tx.add(ConsentRecord(
            meeting_id=uuid.UUID(first.json()["id"]), org_id=org_id,
            bot_display_name="munshi.ai (recording)", disclosure_text="notice",
            privacy_notice_url="http://portal.test/privacy", disclosure_channel="chat",
            admitted_at=at - timedelta(minutes=5), removed_at=at,
        ))
    refused = member.post(JOIN, json={"join_url": link})
    assert (refused.status_code, refused.json()["code"]) == (409, "removed_by_participant")
    with superuser_engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, join_url_key FROM meetings WHERE id = :m"), {"m": planned}
        ).one()
    assert (row.status, row.join_url_key) == ("scheduled", join_url_key(link))


def test_invite_writes_touch_only_scheduled_meetings(app_engine: Engine) -> None:
    w = access_org(app_engine)
    live = w.meeting(status="recording")
    planned = scheduled_meeting(w)
    now = datetime.now(UTC)
    with transaction(app_engine, org_id=w.org_id) as tx:
        assert scheduled.update_scheduled(tx, live, now=now, title="moved") is False
        assert scheduled.cancel_scheduled(tx, live, now=now) is False
        assert scheduled.cancel_scheduled(tx, planned, now=now) is True
        # A meeting an invite cancelled comes back only through a restore.
        assert scheduled.update_scheduled(tx, planned, now=now, title="back") is False
        assert scheduled.update_scheduled(tx, planned, now=now, restore=True, title="back")
        with pytest.raises(ValueError):
            scheduled.update_scheduled(tx, planned, now=now, status="done")
    with transaction(app_engine, org_id=w.org_id) as tx:
        rows = dict(
            tx.execute(
                text("SELECT id, status FROM meetings WHERE id IN (:a, :b)"),
                {"a": live, "b": planned},
            ).all()
        )
    assert rows == {live: "recording", planned: "scheduled"}
