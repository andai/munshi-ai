"""Paste a link, get one bot: dispatch, dedup, typed failures, the D-38 refusal, reading and
stopping a meeting, all through andai-core's routes against the in-memory platform gateway."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select, text

from andai_core.db.models import ConsentRecord, Meeting
from andai_core.db.session import transaction
from conftest import (
    Harness,
    RoleOrg,
    give_principal,
    make_harness,
    new_member,
    role_org,
    sign_in,
    zoom_link,
)

JOIN = "/api/v1/meetings/join"
PASSCODE = "s3cretPwd"


@dataclass
class World:
    harness: Harness
    engine: Engine
    org: RoleOrg

    def client(self, role: str):
        return sign_in(self.harness, self.org.by_role(role))


@pytest.fixture
def world(app_engine: Engine) -> World:
    harness = make_harness(app_engine)
    org = role_org(app_engine)
    give_principal(harness, app_engine, org.owner.org_id)
    return World(harness, app_engine, org)


def meet_link() -> str:
    letters = "".join(chr(ord("a") + int(c, 16) % 26) for c in uuid.uuid4().hex[:10])
    return f"https://meet.google.com/{letters[:3]}-{letters[3:7]}-{letters[7:10]}"


def teams_classic_link() -> str:
    thread = uuid.uuid4().hex
    return (
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting_"
        f"{thread}%40thread.v2/0?context=%7b%22Tid%22%3a%22t%22%7d"
    )


def teams_meet_link() -> str:
    return f"https://teams.microsoft.com/meet/{uuid.uuid4().int % 10**14:014d}?p={PASSCODE}"


def stored(engine: Engine, org_id: uuid.UUID, meeting_id: str) -> Meeting:
    with transaction(engine, org_id=org_id) as tx:
        return tx.scalar(select(Meeting).where(Meeting.id == uuid.UUID(meeting_id)))


def set_status(engine: Engine, org_id: uuid.UUID, meeting_id: str, status: str) -> None:
    with transaction(engine, org_id=org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = :s WHERE id = :m"), {"s": status, "m": meeting_id}
        )


# ─── the five link shapes ─────────────────────────────────────────────────────────────────────────

LINKS = {
    "meet": (meet_link, "google_meet"),
    "teams-classic": (teams_classic_link, "teams"),
    "teams-meet": (teams_meet_link, "teams"),
    "zoom": (lambda: zoom_link(passcode=PASSCODE), "zoom"),
    "zoom-vanity": (lambda: zoom_link(host="acme.zoom.us", passcode=PASSCODE), "zoom"),
}


@pytest.mark.parametrize("kind", sorted(LINKS))
def test_each_link_shape_dispatches_one_bot_with_the_exact_body(
    world: World, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    make, family = LINKS[kind]
    link = make()
    member = world.client("member")

    response = member.post(JOIN, json={"join_url": link, "language": "hi"})

    assert response.status_code == 201, response.text
    meeting = response.json()
    [body] = world.harness.gateway.dispatches
    assert body == {
        "meeting_url": link,
        "language": "hi",
        "bot_name": "munshi.ai (recording)",
        "automatic_leave": {"max_wait_for_admission": 600_000, "max_time_left_alone": 300_000},
        "disclosure": {
            "text": world.harness.services.settings.disclosure_text,
            "privacy_notice_url": "http://portal.test/privacy",
        },
    }
    platform_meeting = next(iter(world.harness.gateway.meetings.values()))
    row = stored(world.engine, world.org.owner.org_id, meeting["id"])
    # andai stores what the platform answered, never its own parse.
    assert (row.platform, row.native_meeting_id, row.platform_meeting_id) == (
        family, platform_meeting["native_meeting_id"], platform_meeting["id"]
    )
    assert (meeting["status"], meeting["failure_reason"]) == ("dispatching", None)
    assert meeting["platform"] == family
    assert meeting["language"] == "hi"
    assert meeting["join_url"] == link  # the requester sees their own link in full
    assert row.settings_snapshot == {
        "bot_name": "munshi.ai (recording)",
        "language": "hi",
        "stt_language": "hi",
        "lobby_timeout_s": 600,
        "alone_timeout_s": 300,
        "disclosure_text": world.harness.services.settings.disclosure_text,
        "privacy_notice_url": "http://portal.test/privacy",
    }
    assert all(PASSCODE not in record.getMessage() for record in caplog.records)


def test_the_same_link_twice_is_one_bot(world: World) -> None:
    link = zoom_link()
    member = world.client("member")

    first = member.post(JOIN, json={"join_url": link})
    second = member.post(JOIN, json={"join_url": link + "&extra=1"})  # query ignored

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(world.harness.gateway.dispatches) == 1


def test_a_colleague_without_access_gets_409_and_no_id(world: World) -> None:
    link = zoom_link()
    meeting_id = world.client("member").post(JOIN, json={"join_url": link}).json()["id"]
    colleague = new_member(world.engine, role="member", org_id=world.org.owner.org_id)

    response = sign_in(world.harness, colleague).post(JOIN, json={"join_url": link})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "the bot is already in this meeting for your organisation",
        "code": "already_in_meeting",
    }
    assert meeting_id not in response.text
    assert len(world.harness.gateway.dispatches) == 1


def test_an_admin_pasting_a_live_link_gets_the_meeting(world: World) -> None:
    link = zoom_link()
    meeting_id = world.client("member").post(JOIN, json={"join_url": link}).json()["id"]

    response = world.client("admin").post(JOIN, json={"join_url": link})

    assert (response.status_code, response.json()["id"]) == (200, meeting_id)


@pytest.mark.parametrize(
    "link",
    [
        "http://meet.google.com/abc-defg-hij",
        "https://example.com/j/12345678901",
        "https://zoom.us.example.com/j/12345678901",
        "https://notzoom.us/j/12345678901",
        "https://evil-teams.microsoft.com.example/meet/1",
        "https://user:pw@zoom.us/j/12345678901",
        "javascript:alert(1)",
        "not a link",
        "https://[::1/j/1",
    ],
)
def test_non_meeting_links_are_refused_before_any_call(world: World, link: str) -> None:
    response = world.client("member").post(JOIN, json={"join_url": link})

    assert response.status_code == 422
    assert response.json()["code"] == "not_a_meeting_link"
    assert world.harness.gateway.calls == []


def test_a_link_the_platform_rejects_leaves_no_meeting(world: World, superuser_engine: Engine) -> None:
    response = world.client("member").post(
        JOIN, json={"join_url": "https://meet.google.com/not-a-meet-code"}
    )

    assert (response.status_code, response.json()["code"]) == (422, "not_a_meeting_link")
    with superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM meetings WHERE org_id = :o"), {"o": world.org.owner.org_id}
        ).scalar_one() == 0


def test_the_platform_409_for_the_same_meeting_under_another_host_returns_ours(
    world: World,
) -> None:
    link = zoom_link()
    vanity = link.replace("https://zoom.us/", "https://acme.zoom.us/")
    member = world.client("member")
    first = member.post(JOIN, json={"join_url": link}).json()

    second = member.post(JOIN, json={"join_url": vanity})

    assert (second.status_code, second.json()["id"]) == (200, first["id"])
    assert len(world.harness.gateway.meetings) == 1
    with transaction(world.engine, org_id=world.org.owner.org_id) as tx:
        count = tx.scalar(text("SELECT count(*) FROM meetings"))
    assert count == 1  # the provisional row is gone


def test_the_platform_409_for_a_meeting_andai_does_not_hold_attaches_to_it(world: World) -> None:
    """A bot the org's principal runs that no andai row tracks (sent before this row existed)."""
    gateway = world.harness.gateway
    link = zoom_link()
    key = next(t["token"] for t in world.harness.admin_api.tokens.values())
    import asyncio

    platform_meeting = asyncio.run(gateway.client().dispatch(key, {"meeting_url": link}))

    response = world.client("member").post(JOIN, json={"join_url": link})

    assert response.status_code == 201
    row = stored(world.engine, world.org.owner.org_id, response.json()["id"])
    assert (row.platform_meeting_id, row.status) == (platform_meeting["id"], "dispatching")


def test_the_org_cap_fails_as_capacity_exhausted_and_frees_the_link(world: World) -> None:
    gateway = world.harness.gateway
    gateway.fail["dispatch"] = 429
    link = zoom_link()
    member = world.client("member")

    response = member.post(JOIN, json={"join_url": link})

    assert response.status_code == 201
    assert (response.json()["status"], response.json()["failure_reason"]) == (
        "failed", "capacity_exhausted"
    )
    del gateway.fail["dispatch"]
    again = member.post(JOIN, json={"join_url": link})
    assert again.status_code == 201 and again.json()["id"] != response.json()["id"]


@pytest.mark.parametrize("status", [500, 502, 503, 401])
def test_other_platform_errors_fail_as_internal_error_without_the_passcode_in_logs(
    world: World, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world.harness.gateway.fail["dispatch"] = status

    response = world.client("member").post(JOIN, json={"join_url": zoom_link(passcode=PASSCODE)})

    assert (response.json()["status"], response.json()["failure_reason"]) == (
        "failed", "internal_error"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("pwd=REDACTED" in m for m in messages), messages
    assert all(PASSCODE not in m for m in messages)


def test_hinglish_is_recorded_and_sent_as_the_agreed_stt_language(world: World) -> None:
    response = world.client("member").post(
        JOIN, json={"join_url": zoom_link(), "language": "hinglish"}
    )

    assert response.json()["language"] == "hinglish"
    assert world.harness.gateway.dispatches[0]["language"] == "hi"
    row = stored(world.engine, world.org.owner.org_id, response.json()["id"])
    assert row.settings_snapshot["stt_language"] == "hi"


def test_an_unsupported_language_is_422(world: World) -> None:
    response = world.client("member").post(JOIN, json={"join_url": zoom_link(), "language": "fr"})
    assert response.status_code == 422
    assert world.harness.gateway.calls == []


def test_dispatch_is_audited(world: World, superuser_engine: Engine) -> None:
    meeting_id = world.client("member").post(JOIN, json={"join_url": zoom_link()}).json()["id"]
    with superuser_engine.connect() as conn:
        actions = conn.execute(
            text("SELECT action, actor_user_id FROM audit_events WHERE target = :t"),
            {"t": f"meeting:{meeting_id}"},
        ).all()
    assert actions == [("meeting.dispatch", world.org.member.user_id)]


def test_an_org_without_a_principal_cannot_dispatch(app_engine: Engine) -> None:
    harness = make_harness(app_engine)
    owner = new_member(app_engine)

    response = sign_in(harness, owner).post(JOIN, json={"join_url": zoom_link()})

    assert (response.status_code, response.json()["code"]) == (503, "no_platform_principal")
    assert harness.gateway.calls == []


# ─── teams ────────────────────────────────────────────────────────────────────────────────────────


def test_a_member_dispatches_only_for_their_own_teams(world: World) -> None:
    member = world.client("member")
    own = member.post(JOIN, json={"join_url": zoom_link(), "team_id": str(world.org.team)})
    other_team_of_member = member.post(
        JOIN, json={"join_url": zoom_link(), "team_id": str(world.org.other_team)}
    )
    lead = world.client("lead").post(
        JOIN, json={"join_url": zoom_link(), "team_id": str(world.org.other_team)}
    )
    missing = member.post(JOIN, json={"join_url": zoom_link(), "team_id": str(uuid.uuid4())})
    admin = world.client("admin").post(
        JOIN, json={"join_url": zoom_link(), "team_id": str(world.org.other_team)}
    )

    assert own.status_code == 201 and own.json()["team_id"] == str(world.org.team)
    assert other_team_of_member.status_code == 201  # the member belongs to both teams
    assert (lead.status_code, lead.json()["code"]) == (403, "not_in_team")
    assert (missing.status_code, missing.json()["code"]) == (404, "no_such_team")
    assert admin.status_code == 201
    assert len(world.harness.gateway.dispatches) == 3


# ─── D-38: no rejoin after removal ────────────────────────────────────────────────────────────────


def remove_bot(world: World, meeting_id: str, hours_ago: float) -> None:
    """The reconciler's record of a removal, `hours_ago` before the harness clock."""
    at = world.harness.clock() - timedelta(hours=hours_ago)
    with transaction(world.engine, org_id=world.org.owner.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET status = 'done', end_reason = 'evicted' WHERE id = :m"),
            {"m": meeting_id},
        )
        tx.add(
            ConsentRecord(
                meeting_id=uuid.UUID(meeting_id),
                org_id=world.org.owner.org_id,
                bot_display_name="munshi.ai (recording)",
                disclosure_text="notice",
                privacy_notice_url="http://portal.test/privacy",
                disclosure_channel="chat",
                admitted_at=at - timedelta(minutes=5),
                removed_at=at,
            )
        )


def test_a_removed_bot_is_refused_for_12_hours(world: World) -> None:
    link = zoom_link()
    member = world.client("member")
    meeting_id = member.post(JOIN, json={"join_url": link}).json()["id"]
    remove_bot(world, meeting_id, hours_ago=1)

    refused = member.post(JOIN, json={"join_url": link})
    by_admin = world.client("admin").post(JOIN, json={"join_url": link})

    assert refused.status_code == 409
    assert refused.json()["code"] == "removed_by_participant"
    assert by_admin.json()["code"] == "removed_by_participant"
    assert len(world.harness.gateway.dispatches) == 1

    world.harness.clock.advance(hours=11, minutes=1)  # 12 h 1 min after the removal
    allowed = member.post(JOIN, json={"join_url": link})
    assert allowed.status_code == 201


def test_canary_meetings_are_exempt_from_the_removal_window(world: World) -> None:
    import asyncio

    from andai_core.meetings.service import Actor, Meetings

    link = zoom_link()
    meeting_id = world.client("member").post(JOIN, json={"join_url": link}).json()["id"]
    remove_bot(world, meeting_id, hours_ago=1)
    services = world.harness.services
    meetings = Meetings(
        services.engine, services.keyring, services.gateway, services.settings, services.clock
    )
    actor = Actor(world.org.owner.org_id, world.org.owner.user_id, "owner")

    result = asyncio.run(meetings.join(actor, join_url=link, language=None, team_id=None,
                                       source="canary"))

    assert result.created and result.meeting["source"] == "canary"


# ─── reading ──────────────────────────────────────────────────────────────────────────────────────


def test_who_reads_a_meeting_and_who_sees_the_passcode(world: World) -> None:
    link = zoom_link(passcode=PASSCODE)
    meeting_id = world.client("member").post(
        JOIN, json={"join_url": link, "team_id": str(world.org.team)}
    ).json()["id"]
    path = f"/api/v1/meetings/{meeting_id}"
    outsider = new_member(world.engine, role="member", org_id=world.org.owner.org_id)

    seen = {role: world.client(role).get(path) for role in ("member", "lead", "admin", "owner")}
    hidden = sign_in(world.harness, outsider).get(path)

    assert {role: r.status_code for role, r in seen.items()} == dict.fromkeys(seen, 200)
    assert seen["member"].json()["join_url"] == link
    assert seen["admin"].json()["join_url"] == link
    assert seen["owner"].json()["join_url"] == link
    assert PASSCODE not in seen["lead"].text
    assert "pwd=REDACTED" in seen["lead"].json()["join_url"]
    assert (hidden.status_code, hidden.json()["code"]) == (404, "no_such_meeting")


def test_a_lead_of_another_team_cannot_read(world: World, app_engine: Engine) -> None:
    from conftest import new_team

    meeting_id = world.client("member").post(
        JOIN, json={"join_url": zoom_link(), "team_id": str(world.org.other_team)}
    ).json()["id"]

    response = world.client("lead").get(f"/api/v1/meetings/{meeting_id}")

    assert response.status_code == 404
    # ... and a lead of that team can.
    other_lead = new_member(app_engine, role="member", org_id=world.org.owner.org_id)
    new_team(app_engine, world.org.owner.org_id, {other_lead.user_id: "lead"})
    with transaction(app_engine, org_id=world.org.owner.org_id) as tx:
        tx.execute(
            text(
                "INSERT INTO team_memberships (team_id, org_id, user_id, role)"
                " VALUES (:t, :o, :u, 'lead')"
            ),
            {"t": world.org.other_team, "o": world.org.owner.org_id, "u": other_lead.user_id},
        )
    assert sign_in(world.harness, other_lead).get(f"/api/v1/meetings/{meeting_id}").status_code == 200


def test_admit_instructions_appear_only_while_waiting(world: World) -> None:
    member = world.client("member")
    meeting_id = member.post(JOIN, json={"join_url": meet_link()}).json()["id"]
    path = f"/api/v1/meetings/{meeting_id}"
    assert member.get(path).json()["admit_instructions"] is None

    set_status(world.engine, world.org.owner.org_id, meeting_id, "awaiting_admission")
    waiting = member.get(path).json()

    assert "With potential risks" in waiting["admit_instructions"]
    assert '"munshi.ai (recording)"' in waiting["admit_instructions"]


def test_an_unknown_meeting_id_is_404(world: World) -> None:
    response = world.client("owner").get(f"/api/v1/meetings/{uuid.uuid4()}")
    assert response.status_code == 404


# ─── stopping ─────────────────────────────────────────────────────────────────────────────────────


def test_stop_before_admission_cancels(world: World, superuser_engine: Engine) -> None:
    member = world.client("member")
    meeting = member.post(JOIN, json={"join_url": zoom_link()}).json()

    response = member.post(f"/api/v1/meetings/{meeting['id']}/stop")

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["failure_reason"]) == ("cancelled", None)
    assert response.json()["ended_at"] is not None
    assert world.harness.gateway.stops == [("zoom", meeting["native_meeting_id"])]
    with superuser_engine.connect() as conn:
        actions = conn.execute(
            text("SELECT action FROM audit_events WHERE target = :t ORDER BY at, action"),
            {"t": f"meeting:{meeting['id']}"},
        ).scalars().all()
    assert sorted(actions) == ["meeting.dispatch", "meeting.stop"]
    # A cancelled meeting no longer holds its link.
    assert member.post(JOIN, json={"join_url": meeting["join_url"]}).status_code == 201


def test_stop_after_admission_asks_the_platform_and_lets_it_end(world: World) -> None:
    member = world.client("member")
    meeting = member.post(JOIN, json={"join_url": zoom_link()}).json()
    gateway = world.harness.gateway
    platform_meeting = gateway.by_native(meeting["native_meeting_id"])
    gateway.set(platform_meeting["id"], status="active", start_time="2026-10-28T10:00:00Z")
    set_status(world.engine, world.org.owner.org_id, meeting["id"], "recording")

    response = member.post(f"/api/v1/meetings/{meeting['id']}/stop")

    assert (response.status_code, response.json()["status"]) == (200, "recording")
    assert gateway.meetings[platform_meeting["id"]]["status"] == "stopping"


def test_stop_while_waiting_but_admitted_meanwhile_is_not_cancelled(world: World) -> None:
    member = world.client("member")
    meeting = member.post(JOIN, json={"join_url": zoom_link()}).json()
    gateway = world.harness.gateway
    platform_meeting = gateway.by_native(meeting["native_meeting_id"])
    gateway.set(platform_meeting["id"], status="active", start_time="2026-10-28T10:00:00Z")
    set_status(world.engine, world.org.owner.org_id, meeting["id"], "awaiting_admission")

    response = member.post(f"/api/v1/meetings/{meeting['id']}/stop")

    assert response.json()["status"] == "awaiting_admission"  # the reconciler takes it from here


def test_stop_of_an_ended_meeting_is_409(world: World) -> None:
    member = world.client("member")
    meeting_id = member.post(JOIN, json={"join_url": zoom_link()}).json()["id"]
    set_status(world.engine, world.org.owner.org_id, meeting_id, "done")

    response = member.post(f"/api/v1/meetings/{meeting_id}/stop")

    assert (response.status_code, response.json()["code"]) == (409, "not_running")


def test_stop_by_someone_without_access_is_404_and_changes_nothing(world: World) -> None:
    meeting_id = world.client("member").post(JOIN, json={"join_url": zoom_link()}).json()["id"]

    response = world.client("lead").post(f"/api/v1/meetings/{meeting_id}/stop")

    assert response.status_code == 404
    assert world.harness.gateway.stops == []
    assert stored(world.engine, world.org.owner.org_id, meeting_id).status == "dispatching"


def test_stop_when_the_platform_is_unreachable_is_502_and_changes_nothing(world: World) -> None:
    member = world.client("member")
    meeting_id = member.post(JOIN, json={"join_url": zoom_link()}).json()["id"]
    world.harness.gateway.fail["stop"] = 503

    response = member.post(f"/api/v1/meetings/{meeting_id}/stop")

    assert (response.status_code, response.json()["code"]) == (502, "platform_unavailable")
    assert stored(world.engine, world.org.owner.org_id, meeting_id).status == "dispatching"


def test_stop_of_a_bot_the_platform_already_let_go_still_cancels(world: World) -> None:
    member = world.client("member")
    meeting = member.post(JOIN, json={"join_url": zoom_link()}).json()
    gateway = world.harness.gateway
    gateway.set(gateway.by_native(meeting["native_meeting_id"])["id"], status="failed")

    response = member.post(f"/api/v1/meetings/{meeting['id']}/stop")

    assert (response.status_code, response.json()["status"]) == (200, "cancelled")


def test_a_database_error_never_repeats_the_passcode(world: World) -> None:
    """The app engine hides statement parameters, so an error about a meeting row (logged by
    whatever catches it) does not carry the pasted link."""
    from sqlalchemy.exc import IntegrityError

    link = zoom_link(passcode=PASSCODE)
    with pytest.raises(IntegrityError) as caught:
        with transaction(world.engine, org_id=world.org.owner.org_id) as tx:
            tx.add(
                Meeting(
                    org_id=world.org.owner.org_id, requested_by=uuid.uuid4(), platform="zoom",
                    join_url=link, join_url_key="zoom.us/j/x", status="dispatching",
                    settings_snapshot={},
                )
            )
    assert PASSCODE not in str(caught.value)


def test_a_platform_409_for_a_colleagues_meeting_is_409_and_leaves_no_row(
    world: World, superuser_engine: Engine
) -> None:
    link = zoom_link()
    vanity = link.replace("https://zoom.us/", "https://acme.zoom.us/")
    world.client("member").post(JOIN, json={"join_url": link})
    colleague = new_member(world.engine, role="member", org_id=world.org.owner.org_id)

    response = sign_in(world.harness, colleague).post(JOIN, json={"join_url": vanity})

    assert (response.status_code, response.json()["code"]) == (409, "already_in_meeting")
    with superuser_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM meetings WHERE org_id = :o"), {"o": world.org.owner.org_id}
        ).scalar_one()
    assert rows == 1


def test_a_row_whose_platform_meeting_ended_unseen_is_superseded(world: World) -> None:
    """The platform accepted a new bot for a native meeting an andai row still shows as live (its
    end not yet reconciled): that row is closed so the new one can hold the meeting."""
    link = zoom_link()
    vanity = link.replace("https://zoom.us/", "https://acme.zoom.us/")
    member = world.client("member")
    old = member.post(JOIN, json={"join_url": link}).json()
    gateway = world.harness.gateway
    gateway.set(gateway.by_native(old["native_meeting_id"])["id"], status="completed")

    new = member.post(JOIN, json={"join_url": vanity})

    assert new.status_code == 201 and new.json()["status"] == "dispatching"
    assert new.json()["native_meeting_id"] == old["native_meeting_id"]
    closed = stored(world.engine, world.org.owner.org_id, old["id"])
    assert (closed.status, closed.failure_reason) == ("failed", "internal_error")
