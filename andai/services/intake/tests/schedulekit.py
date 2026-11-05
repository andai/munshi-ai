"""Builds the world the scheduling tests run in: an org whose sender domain signs its mail in a stub
DNS zone, signed invite mail from its members, delivery into `inbound_mail` as the SMTP receiver
stores it, and the processor run until a given message has its result. Not a test module."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

import mailkit
from andai_core.db.models import BotSetting, Org, OrgMembership, Team, TeamMembership, User
from andai_core.db.session import transaction
from andai_intake.processor import Processor, Result
from andai_intake.store import Envelope, save
from invitekit import NOTETAKER, calendar, message, vevent

MEET = "https://meet.google.com/abc-defg-hij"
PEER = "192.0.2.10"


def _ics(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class Tenant:
    """An org with an allowed domain, a member `ana` (in one team) and a stub DNS zone in which the
    domain publishes DMARC and signs with DKIM."""

    def __init__(self, engine: Engine, kit: mailkit.Kit) -> None:
        self.engine = engine
        self.kit = kit
        self.domain = f"d{uuid.uuid4().hex[:12]}.example.test"
        kit.dmarc(self.domain)
        self.id = uuid.uuid4()
        with transaction(engine, org_id=self.id) as tx:
            tx.add(Org(id=self.id, name=f"Org {self.id.hex[:8]}", slug=f"org-{self.id.hex}",
                       allowed_email_domains=[self.domain]))
        self.ana, self.ana_id = f"ana@{self.domain}", self.member("ana")
        self.team = uuid.uuid4()
        with transaction(engine, org_id=self.id) as tx:
            tx.add(Team(id=self.team, org_id=self.id, name="Design"))
            tx.flush()
            tx.add(TeamMembership(team_id=self.team, org_id=self.id, user_id=self.ana_id,
                                  role="member"))

    def member(self, local: str) -> uuid.UUID:
        user_id = uuid.uuid4()
        with transaction(self.engine) as tx:
            tx.add(User(id=user_id, email=f"{local}@{self.domain}", display_name=local.title(),
                        status="active"))
        with transaction(self.engine, org_id=self.id) as tx:
            tx.add(OrgMembership(org_id=self.id, user_id=user_id, role="member"))
        return user_id

    def auto_join(self, value: str, team: uuid.UUID | None = None) -> None:
        with transaction(self.engine, org_id=self.id) as tx:
            tx.add(BotSetting(org_id=self.id, team_id=team, auto_join=value))

    def invite(
        self,
        sender: str | None = None,
        *,
        uid: str | None = None,
        start: datetime | None = None,
        minutes: int = 60,
        method: str = "REQUEST",
        sequence: int = 0,
        link: str | None = MEET,
        summary: str = "Design review",
        headers: dict[str, str] | None = None,
        sign_as: str | None = None,
        calendar_text: str | None = None,
        dtstamp: datetime | None = None,
        extra: tuple[str, ...] = (),
    ) -> bytes:
        sender = sender or self.ana
        start = start or (datetime.now(UTC) + timedelta(days=2)).replace(second=0, microsecond=0)
        lines = [*extra, *([f"X-GOOGLE-CONFERENCE:{link}"] if link else [])]
        text_ = calendar_text or calendar(
            vevent(
                uid or f"{uuid.uuid4().hex}@{self.domain}",
                dtstart=f"DTSTART:{_ics(start)}",
                dtend=f"DTEND:{_ics(start + timedelta(minutes=minutes))}",
                sequence=sequence,
                dtstamp=f"DTSTAMP:{_ics(dtstamp or datetime.now(UTC))}",
                summary=summary,
                organizer=f"ORGANIZER:mailto:{self.ana}",
                extra=lines,
            ),
            method=method,
        )
        raw = message(inline=text_, method=method, headers=headers)
        raw = mailkit.replace_header(raw, "From", sender)
        raw = mailkit.replace_header(raw, "Message-ID", f"<{uuid.uuid4().hex}@{self.domain}>")
        return self.kit.dkim(raw, sign_as or self.domain)


def deliver(engine: Engine, raw: bytes, mail_from: str = "bounce@example.test") -> uuid.UUID:
    stored = save(engine, raw, Envelope(mail_from, (NOTETAKER,), PEER, "mx.example.test"))
    assert not stored.duplicate
    return stored.id


def drain(proc: Processor, inbound_id: uuid.UUID) -> Result:
    for _ in range(50):
        for result in proc.run_once():
            if result.inbound_id == inbound_id:
                return result
    raise AssertionError("the message was never processed")


def row(superuser_engine: Engine, inbound_id: uuid.UUID) -> dict:
    with superuser_engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM inbound_mail WHERE id = :i"), {"i": inbound_id}
            ).one()._mapping
        )


def meetings_of(superuser_engine: Engine, org_id: uuid.UUID) -> list[dict]:
    with superuser_engine.connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(
                text("SELECT * FROM meetings WHERE org_id = :o ORDER BY scheduled_start"),
                {"o": org_id},
            )
        ]
