"""The recap mail (`meetings/recap.py`): who gets it, what it says, and that it goes once, for the
first minutes only, through the claim it runs under (`andai_claim_recap`, migration 0010). Every
test runs on a brand-new database, so the claim sees only this test's meetings."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, select, text

from andai_core.db.models import Meeting
from andai_core.db.session import transaction
from andai_core.llm.client import LlmClient, LlmConfig
from andai_core.mail import Mail, MailError, MemoryMailer, SmtpMailer
from andai_core.meetings import recap
from andai_core.meetings.minutes_job import run_one
from andai_core.meetings.mom import Document, seed_version
from andai_core.testing import fake_llm
from conftest import (
    PORTAL_ORIGIN,
    Mailpit,
    mailpit_messages,
    make_settings,
    mom_body,
    new_member,
    new_team,
    seed_meeting,
    seed_segments,
)

CLAIM = "SELECT org_id, meeting_id, lease FROM andai_claim_recap(:n)"
STARTED = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)  # Monday 10:30 in Asia/Kolkata
LINES = [
    ("Ravi Kumar", "Budget review kal tak hoga"),
    ("Asha", "Ravi budget sheet bhejega"),
    ("Asha", "Vendor se baat karni hai"),
]


class Refusing:
    """A relay that refuses every message."""

    def __init__(self) -> None:
        self.tries = 0

    async def send(self, mail: Mail) -> None:
        self.tries += 1
        raise MailError("SMTP relay test:25 refused the message")


@dataclass
class World:
    engine: Engine
    org_id: uuid.UUID
    requester: uuid.UUID
    requester_email: str

    def user(self, *, status: str = "active", org_id: uuid.UUID | None = None):
        return new_member(self.engine, role="member", status=status, org_id=org_id or self.org_id)

    def meeting(
        self,
        team: uuid.UUID | None = None,
        *,
        title: str | None = "Budget review",
        minutes: bool = True,
        pending: bool = True,
        **body,
    ) -> uuid.UUID:
        meeting_id = seed_meeting(
            self.engine, self.org_id, self.requester, team, started_at=STARTED,
            ended_at=STARTED + timedelta(minutes=30), title=title,
        )  # fmt: skip
        ids = seed_segments(self.engine, self.org_id, meeting_id, LINES)
        if minutes:
            with transaction(self.engine, org_id=self.org_id) as tx:
                seed_version(tx, self.org_id, meeting_id, mom_body(ids, **body))
        if pending:
            self.update(meeting_id, recap_state="pending", mom_state="stored")
        return meeting_id

    def update(self, meeting_id: uuid.UUID, **values: object) -> None:
        casts = {"mom_request": "jsonb", "settings_snapshot": "jsonb"}
        assignments = ", ".join(
            f"{c} = CAST(:{c} AS {casts[c]})" if c in casts else f"{c} = :{c}" for c in values
        )
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.execute(
                text(f"UPDATE meetings SET {assignments} WHERE id = :m"),
                {"m": meeting_id, **values},
            )

    def segment_ids(self, meeting_id: uuid.UUID) -> list[str]:
        return [f"seg_{meeting_id.hex[:12]}_{seq:04d}" for seq in range(len(LINES))]

    def claim(self, n: int = 100) -> dict[uuid.UUID, uuid.UUID]:
        with transaction(self.engine) as tx:
            return {row.meeting_id: row.lease for row in tx.execute(text(CLAIM), {"n": n})}

    def row(self, meeting_id: uuid.UUID) -> dict:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return dict(
                tx.execute(
                    text(
                        "SELECT recap_state, recap_attempts, recap_next_at, recap_lease, recap_at"
                        " FROM meetings WHERE id = :m"
                    ),
                    {"m": meeting_id},
                ).mappings().one()
            )

    def send(self, meeting_id: uuid.UUID, lease: uuid.UUID, mailer) -> str:
        return asyncio.run(
            recap.send_one(
                self.engine, mailer, self.org_id, meeting_id, lease, portal_origin=PORTAL_ORIGIN
            )
        )

    def recipients(self, meeting_id: uuid.UUID) -> list[str]:
        with transaction(self.engine, org_id=self.org_id) as tx:
            meeting = tx.scalar(select(Meeting).where(Meeting.id == meeting_id))
            return recap.recipients(tx, meeting)


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    member = new_member(fresh_app_engine)
    return World(fresh_app_engine, member.org_id, member.user_id, member.email)


# ─── audience ─────────────────────────────────────────────────────────────────────────────────────


def test_the_audience_is_the_requester_and_the_active_members_of_the_meeting_team(
    world: World,
) -> None:
    lead, member, disabled, elsewhere, grantee = (world.user() for _ in range(5))
    disabled_user = world.user(status="disabled")
    team = new_team(
        world.engine,
        world.org_id,
        {
            lead.user_id: "lead",
            member.user_id: "member",
            world.requester: "member",  # the requester on the team: one mail, not two
            disabled_user.user_id: "member",
        },
    )
    new_team(world.engine, world.org_id, {elsewhere.user_id: "member", disabled.user_id: "lead"})
    meeting_id = world.meeting(team)
    with transaction(world.engine, org_id=world.org_id) as tx:  # shared with a user: still no mail
        tx.execute(
            text(
                "INSERT INTO meeting_shares (org_id, meeting_id, grantee_type, grantee_id,"
                " granted_by) VALUES (:o, :m, 'user', :g, :r)"
            ),
            {"o": world.org_id, "m": meeting_id, "g": grantee.user_id, "r": world.requester},
        )

    got = world.recipients(meeting_id)

    assert got[0] == world.requester_email
    assert sorted(got) == sorted([world.requester_email, lead.email, member.email])


def test_a_meeting_without_a_team_goes_to_the_requester_alone(world: World) -> None:
    world.user()
    assert world.recipients(world.meeting(None)) == [world.requester_email]


def test_a_disabled_requester_gets_nothing(world: World) -> None:
    member = world.user()
    team = new_team(world.engine, world.org_id, {member.user_id: "member"})
    meeting_id = world.meeting(team)
    with transaction(world.engine) as tx:
        tx.execute(text("UPDATE users SET status = 'disabled' WHERE id = :u"), {"u": world.requester})

    assert world.recipients(meeting_id) == [member.email]


# ─── the mail ─────────────────────────────────────────────────────────────────────────────────────


def _doc(**overrides) -> Document:
    body = mom_body(["seg_1", "seg_2", "seg_3"], **overrides)
    return Document.model_validate(body)


def _meeting(title: str | None = "Budget review") -> Meeting:
    return Meeting(id=uuid.UUID(int=7), title=title)


def test_the_body_has_summary_decisions_action_items_and_the_portal_link() -> None:
    doc = _doc(outputLanguage="en", summary="The budget was reviewed.")
    link = recap.meeting_link(PORTAL_ORIGIN + "/", uuid.UUID(int=7))

    out = recap.body(_meeting(), doc, link, date(2026, 11, 2))

    assert link == f"{PORTAL_ORIGIN}/meetings/{uuid.UUID(int=7)}"
    assert out.startswith("The minutes of Budget review are ready.\n")
    assert "Summary\n-------\nThe budget was reviewed." in out
    assert "Decisions\n---------\n- Deadline kal hai" in out
    assert "- Ravi budget sheet bhejega (Owner: Ravi; due 2026-11-01)" in out
    assert "- Vendor se baat karni hai (Owner: Unassigned)" in out
    assert f"\n{link}\n" in out
    assert "Topics" not in out and "Risks" not in out  # the recap is the short form
    assert "seg_" not in out  # no evidence ids in a mail


def test_empty_sections_say_none_recorded_in_the_output_language() -> None:
    empty = {"summary": "", "decisions": [], "actionItems": []}
    english = recap.body(_meeting(), _doc(outputLanguage="en", **empty), "L", date(2026, 11, 2))
    hindi = recap.body(_meeting(None), _doc(outputLanguage="hi", **empty), "L", date(2026, 11, 2))

    assert english.count("None recorded") == 3
    assert hindi.count("कुछ दर्ज नहीं हुआ") == 3
    assert "निर्णय" in hindi and "कार्य बिंदु" in hindi and "सारांश" in hindi
    assert hindi.startswith("2026-11-02 की मीटिंग का कार्यवृत्त तैयार है।")
    assert recap.subject(_meeting(None), _doc(outputLanguage="hi"), date(2026, 11, 2)) == (
        "कार्यवृत्त: 2026-11-02 की मीटिंग"
    )


def test_the_subject_keeps_a_title_on_one_line() -> None:
    doc = _doc(outputLanguage="en")
    assert recap.subject(_meeting("Q4\r\nplan"), doc, date(2026, 11, 2)) == "Minutes: Q4 plan"


def test_the_template_headings_are_used() -> None:
    doc = _doc(outputLanguage="en", templateId="client_call")
    out = recap.body(_meeting(), doc, "L", date(2026, 11, 2))
    assert "Call summary" in out and "Agreements" in out and "Follow-ups" in out


# ─── send once ────────────────────────────────────────────────────────────────────────────────────


def test_the_first_minutes_are_mailed_once_to_the_audience(world: World) -> None:
    member = world.user()
    team = new_team(world.engine, world.org_id, {member.user_id: "member"})
    meeting_id = world.meeting(team, outputLanguage="en")
    mailer = MemoryMailer()

    leases = world.claim()
    assert list(leases) == [meeting_id]
    assert world.send(meeting_id, leases[meeting_id], mailer) == "sent"

    assert len(mailer.outbox) == 1
    mail = mailer.outbox[0]
    assert mail.to.split(", ") == [world.requester_email, member.email]
    assert mail.subject == "Minutes: Budget review"
    assert f"{PORTAL_ORIGIN}/meetings/{meeting_id}" in mail.text
    row = world.row(meeting_id)
    assert row["recap_state"] == "sent" and row["recap_at"] is not None
    assert row["recap_lease"] is None and row["recap_next_at"] is None
    assert world.claim() == {}  # nothing left to send


def test_a_stale_lease_sends_nothing(world: World) -> None:
    meeting_id = world.meeting()
    world.claim()
    mailer = MemoryMailer()

    assert world.send(meeting_id, uuid.uuid4(), mailer) == "lost"

    assert mailer.outbox == []
    assert world.row(meeting_id)["recap_state"] == "pending"


def test_two_senders_with_the_same_claim_send_one_mail(world: World) -> None:
    meeting_id = world.meeting()
    lease = world.claim()[meeting_id]
    mailer = MemoryMailer()

    def one():
        return recap.send_one(
            world.engine, mailer, world.org_id, meeting_id, lease, portal_origin=PORTAL_ORIGIN
        )

    async def both() -> list[str]:
        return list(await asyncio.gather(one(), one()))

    assert sorted(asyncio.run(both())) == ["lost", "sent"]
    assert len(mailer.outbox) == 1


def test_a_relay_error_retries_with_backoff_and_fails_after_five(world: World) -> None:
    meeting_id = world.meeting()
    relay = Refusing()
    before = datetime.now(UTC)

    lease = world.claim()[meeting_id]
    assert world.send(meeting_id, lease, relay) == "pending"
    row = world.row(meeting_id)
    assert row["recap_attempts"] == 1 and row["recap_lease"] is None
    assert before + timedelta(seconds=29) < row["recap_next_at"] < before + timedelta(seconds=40)
    assert world.claim() == {}  # not due before the backoff

    states = []
    for _ in range(4):
        world.update(meeting_id, recap_next_at=datetime.now(UTC) - timedelta(seconds=1))
        lease = world.claim()[meeting_id]
        states.append(world.send(meeting_id, lease, relay))

    assert states == ["pending", "pending", "pending", "failed"]
    row = world.row(meeting_id)
    assert (row["recap_state"], row["recap_attempts"], row["recap_lease"]) == ("failed", 5, None)
    world.update(meeting_id, recap_next_at=datetime.now(UTC) - timedelta(seconds=1))
    assert world.claim() == {}
    assert relay.tries == 5


def test_a_meeting_deleted_after_the_claim_is_skipped(world: World) -> None:
    meeting_id = world.meeting()
    lease = world.claim()[meeting_id]
    world.update(meeting_id, deleted_at=datetime.now(UTC), deletion_state="pending")
    mailer = MemoryMailer()

    assert world.send(meeting_id, lease, mailer) == "skipped"

    assert mailer.outbox == []
    assert world.row(meeting_id)["recap_state"] == "skipped"


def test_a_deleted_meeting_is_never_claimed(world: World) -> None:
    meeting_id = world.meeting()
    world.update(meeting_id, deleted_at=datetime.now(UTC), deletion_state="pending")
    assert world.claim() == {}


def test_a_meeting_without_minutes_is_skipped(world: World) -> None:
    meeting_id = world.meeting(minutes=False)
    lease = world.claim()[meeting_id]
    mailer = MemoryMailer()

    assert world.send(meeting_id, lease, mailer) == "skipped"
    assert mailer.outbox == []


def _llm() -> LlmClient:
    config = LlmConfig(base_url="http://fake-llm.test", api_key=None, model="fake-model")
    return LlmClient(config, transport=httpx.ASGITransport(app=fake_llm.app))


def _mom_claim(world: World) -> dict[uuid.UUID, uuid.UUID]:
    with transaction(world.engine) as tx:
        rows = tx.execute(text("SELECT org_id, meeting_id, lease FROM andai_claim_mom(10)"))
        return {row.meeting_id: row.lease for row in rows}


def test_only_the_first_generated_version_owes_a_recap(world: World) -> None:
    meeting_id = world.meeting(minutes=False, pending=False)
    world.update(meeting_id, settings_snapshot=json.dumps({"language": "en"}))
    mailer = MemoryMailer()

    first = asyncio.run(
        run_one(world.engine, _llm(), world.org_id, meeting_id, _mom_claim(world)[meeting_id])
    )
    assert (first.state, first.version) == ("stored", 1)
    assert world.row(meeting_id)["recap_state"] == "pending"
    lease = world.claim()[meeting_id]
    assert world.send(meeting_id, lease, mailer) == "sent"

    world.update(  # a regenerate in another language
        meeting_id, mom_state="pending", mom_request=json.dumps({"language": "hi"})
    )
    second = asyncio.run(
        run_one(world.engine, _llm(), world.org_id, meeting_id, _mom_claim(world)[meeting_id])
    )
    assert (second.state, second.version) == ("stored", 2)
    with transaction(world.engine, org_id=world.org_id) as tx:  # and an edit stored as version 3
        seed_version(tx, world.org_id, meeting_id, mom_body(world.segment_ids(meeting_id)))

    assert world.row(meeting_id)["recap_state"] == "sent"
    assert world.claim() == {}
    assert len(mailer.outbox) == 1


def test_a_regenerate_before_the_recap_still_mails_the_first_version(world: World) -> None:
    meeting_id = world.meeting(summary="First summary.", outputLanguage="en")
    with transaction(world.engine, org_id=world.org_id) as tx:
        seed_version(
            tx, world.org_id, meeting_id,
            mom_body(world.segment_ids(meeting_id), summary="Second summary."),
        )  # fmt: skip
    mailer = MemoryMailer()

    assert world.send(meeting_id, world.claim()[meeting_id], mailer) == "sent"

    assert "First summary." in mailer.outbox[0].text
    assert "Second summary." not in mailer.outbox[0].text


def test_the_recap_reaches_every_recipient_through_smtp(world: World, mailpit: Mailpit) -> None:
    member = world.user()
    team = new_team(world.engine, world.org_id, {member.user_id: "lead"})
    meeting_id = world.meeting(team, outputLanguage="hi", summary="बजट की समीक्षा हुई।")
    settings = make_settings(SMTP_HOST=mailpit.smtp_host, SMTP_PORT=str(mailpit.smtp_port))

    assert world.send(meeting_id, world.claim()[meeting_id], SmtpMailer(settings)) == "sent"

    for address in (world.requester_email, member.email):
        (message,) = mailpit_messages(mailpit.api_url, address)
        assert message["Subject"] == "कार्यवृत्त: Budget review"
        assert "बजट की समीक्षा हुई।" in message["Text"]
        assert f"{PORTAL_ORIGIN}/meetings/{meeting_id}" in message["Text"]
        assert message["HTML"] == ""
