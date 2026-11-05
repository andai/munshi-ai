"""Confirmations: one plain-text mail per processed invite to its verified sender, sent through a
real SMTP relay (Mailpit, from andai-core's test fixtures) after the result is committed, and none
for anyone else (RFC 3834). Signed invite mail on stub DNS, as in test_processor.py.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
import zoneinfo
from datetime import UTC, datetime, timedelta

import pytest
import redis
from sqlalchemy import Engine

import mailkit
from andai_core.auth.ratelimit import MemoryLimiter
from andai_core.mail import MemoryMailer, SmtpMailer
from andai_intake import confirm
from andai_intake import processor as processor_mod
from andai_intake.auth.verdict import Policy
from andai_intake.confirm import Confirmer, Notice, Occurrence, may_confirm
from andai_intake.limits import IntakeLimits
from andai_intake.processor import Processor, Result
from andai_intake.settings import IntakeSettings
from conftest import Mailpit, mailpit_messages
from invitekit import NOTETAKER, calendar, vevent
from schedulekit import Tenant, deliver, drain, meetings_of

NEW_YORK = zoneinfo.ZoneInfo("America/New_York")


@pytest.fixture
def kit() -> mailkit.Kit:
    return mailkit.Kit()


@pytest.fixture
def limits(valkey_url: str) -> IntakeLimits:
    lim = IntakeLimits.valkey(redis.Redis.from_url(valkey_url))
    for limiter in (lim.sender, lim.org, lim.peer):
        limiter.prefix = f"test:{uuid.uuid4().hex}:"
    return lim


@pytest.fixture
def org(app_engine: Engine, kit: mailkit.Kit) -> Tenant:
    return Tenant(app_engine, kit)


def intake_settings(mailpit: Mailpit) -> IntakeSettings:
    return IntakeSettings(
        ANDAI_ENV="dev",
        ANDAI_INTAKE_ADDRESSES=NOTETAKER,
        SMTP_HOST=mailpit.smtp_host,
        SMTP_PORT=str(mailpit.smtp_port),
        SMTP_FROM="munshi.ai <no-reply@munshi.invalid>",
    )


@pytest.fixture
def proc(app_engine: Engine, kit: mailkit.Kit, limits: IntakeLimits, mailpit: Mailpit) -> Processor:
    settings = intake_settings(mailpit)
    confirmer = Confirmer(
        SmtpMailer(settings),
        MemoryLimiter(confirm.PER_HOUR, confirm.WINDOW_SECONDS),
        lead_s=120,
        horizon_days=14,
        notetakers=settings.intake_addresses,
    )
    return Processor(
        app_engine, settings, limits, policy=Policy(resolver=kit.zone), batch=50,
        confirmer=confirmer,
    )


def run(proc: Processor, engine: Engine, raw: bytes, mail_from: str = "bounce@example.test"):
    """Process one message and send its confirmation, as the loop does after the commit."""
    result = drain(proc, deliver(engine, raw, mail_from))
    sent = asyncio.run(proc.confirm([result]))
    return result, sent


def inbox(mailpit: Mailpit, to: str, expect: int = 1, timeout: float = 15.0) -> list[dict]:
    return mailpit_messages(mailpit.api_url, to, expect=expect, timeout=timeout)


def headers_of(mailpit: Mailpit, message: dict) -> dict:
    with urllib.request.urlopen(
        f"{mailpit.api_url}/api/v1/message/{message['ID']}/headers", timeout=5
    ) as r:
        return json.load(r)


def soon(days: int = 3) -> datetime:
    return (datetime.now(UTC) + timedelta(days=days)).replace(second=0, microsecond=0)


def ics(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def new_york_invite(org: Tenant, local: datetime, *, summary: str, attendees=None,
                    rrule: str | None = None) -> bytes:
    """An invite whose time is given in America/New_York (TZID, no VTIMEZONE)."""
    end = local + timedelta(hours=1)
    extra = ["X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij"]
    if rrule:
        extra.append(f"RRULE:{rrule}")
    text = calendar(
        vevent(
            f"{uuid.uuid4().hex}@{org.domain}",
            dtstart=f"DTSTART;TZID=America/New_York:{local:%Y%m%dT%H%M%S}",
            dtend=f"DTEND;TZID=America/New_York:{end:%Y%m%dT%H%M%S}",
            summary=summary,
            organizer=f"ORGANIZER:mailto:{org.ana}",
            dtstamp=f"DTSTAMP:{ics(datetime.now(UTC))}",
            **({} if attendees is None else {"attendees": attendees}),
            extra=extra,
        )
    )
    return org.invite(calendar_text=text)


# ─── one mail per outcome ─────────────────────────────────────────────────────────────────────────


def test_scheduled_names_title_time_in_the_invites_zone_and_utc_and_platform(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    local = (datetime.now(NEW_YORK) + timedelta(days=3)).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    result, sent = run(proc, app_engine, new_york_invite(org, local, summary="Quarterly review"))

    assert (result.outcome, sent) == ("scheduled", 1)
    [mail] = inbox(mailpit, org.ana)
    utc = local.astimezone(UTC)
    assert mail["Subject"] == "munshi.ai: scheduled — Quarterly review"
    assert [a["Address"] for a in mail["To"]] == [org.ana]
    assert mail["From"]["Address"] == "no-reply@munshi.invalid"
    text = mail["Text"]
    assert "Meeting: Quarterly review" in text
    assert "Platform: Google Meet" in text
    assert f"Time: {local:%a %d %b %Y, %H:%M} (America/New_York), {utc:%H:%M} UTC" in text
    assert "will join on Google Meet two minutes before it starts" in text
    assert "recorder" in text
    assert "forwarded a copy" not in text
    assert headers_of(mailpit, mail)["Auto-Submitted"] == ["auto-replied"]


def test_updated_cancelled_and_each_say_so(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    uid = f"{uuid.uuid4().hex}@{org.domain}"
    start = soon()
    run(proc, app_engine, org.invite(uid=uid, start=start, summary="Roadmap"))
    moved, _ = run(proc, app_engine, org.invite(uid=uid, start=start + timedelta(hours=2),
                                                sequence=1, summary="Roadmap"))
    cancelled, _ = run(proc, app_engine, org.invite(uid=uid, start=start + timedelta(hours=2),
                                                    sequence=2, method="CANCEL",
                                                    summary="Roadmap"))

    assert (moved.outcome, cancelled.outcome) == ("updated", "cancelled")
    mails = {m["Subject"]: m for m in inbox(mailpit, org.ana, expect=3)}
    assert set(mails) == {
        "munshi.ai: scheduled — Roadmap",
        "munshi.ai: updated — Roadmap",
        "munshi.ai: cancelled — Roadmap",
    }
    later = start + timedelta(hours=2)
    updated = mails["munshi.ai: updated — Roadmap"]["Text"]
    assert f"Time: {later:%a %d %b %Y, %H:%M} (UTC)" in updated
    assert "was updated" in updated
    assert "will not join" in mails["munshi.ai: cancelled — Roadmap"]["Text"]


def test_a_meeting_that_started_says_joining_now(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    start = (datetime.now(UTC) - timedelta(minutes=10)).replace(second=0, microsecond=0)
    result, _ = run(proc, app_engine, org.invite(start=start, summary="Standup"))

    assert result.outcome == "joined_now"
    [mail] = inbox(mailpit, org.ana)
    assert mail["Subject"] == "munshi.ai: joining now — Standup"
    assert f"Time: {start:%a %d %b %Y, %H:%M} (UTC)" in mail["Text"]
    assert "is joining on Google Meet now" in mail["Text"]


def test_an_ended_meeting_is_ignored_with_why(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    start = (datetime.now(UTC) - timedelta(hours=3)).replace(second=0, microsecond=0)
    result, _ = run(proc, app_engine, org.invite(start=start, summary="Retro"))

    assert (result.outcome, result.ignore_reason) == ("ignored", "ended")
    [mail] = inbox(mailpit, org.ana)
    assert mail["Subject"] == "munshi.ai: nothing changed — Retro"
    assert "did not change anything: the meeting has already ended." in mail["Text"]


def test_an_invite_without_a_link_could_not_be_scheduled_with_why(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    result, _ = run(proc, app_engine, org.invite(link=None, summary="Offsite"))

    assert (result.outcome, result.quarantine_reason) == ("quarantined", "no_meeting_link")
    [mail] = inbox(mailpit, org.ana)
    assert mail["Subject"] == "munshi.ai: could not schedule — Offsite"
    assert "no Google Meet, Microsoft Teams or Zoom link" in mail["Text"]


def test_a_series_says_how_many_occurrences_are_scheduled(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    local = (datetime.now(NEW_YORK) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    result, _ = run(proc, app_engine, new_york_invite(org, local, summary="Weekly",
                                                      rrule="FREQ=WEEKLY;COUNT=10"))

    assert result.outcome == "scheduled"
    assert len(result.created) == 2  # within 14 days
    [mail] = inbox(mailpit, org.ana)
    assert "This meeting repeats: 2 occurrence(s) in the next 14 days are scheduled" in mail["Text"]
    second = (local + timedelta(days=7)).astimezone(UTC)
    assert f"- {local + timedelta(days=7):%a %d %b %Y, %H:%M} (America/New_York), " \
        f"{second:%H:%M} UTC" in mail["Text"]


# ─── forwarded copies ─────────────────────────────────────────────────────────────────────────────


def test_a_forwarded_copy_says_later_changes_are_not_tracked(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    local = (datetime.now(NEW_YORK) + timedelta(days=2)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    # The notetaker is not an attendee: someone forwarded their copy.
    raw = new_york_invite(org, local, summary="Board", attendees=("mailto:bo@acme.test",))
    result, _ = run(proc, app_engine, raw)

    assert result.outcome == "scheduled"
    [mail] = inbox(mailpit, org.ana)
    assert (
        "You forwarded a copy of this invitation. Later changes or a cancellation by the "
        f"organiser will not reach munshi.ai unless the notetaker ({NOTETAKER}) is added to the "
        "meeting as an attendee."
    ) in " ".join(mail["Text"].split())


# ─── nobody else ──────────────────────────────────────────────────────────────────────────────────


def nothing_for(mailpit: Mailpit, address: str) -> bool:
    return inbox(mailpit, address, timeout=1.5) == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-generated"},
        {"Auto-Submitted": "auto-forwarded"},
        {"List-Id": "<team.lists.example.test>"},
        {"Precedence": "bulk"},
        {"Precedence": "list"},
        {"X-MS-Exchange-Inbox-Rules-Loop": "ana@example.test"},
    ],
    ids=lambda h: next(iter(h.items()))[0] + ":" + next(iter(h.values())),
)
def test_no_confirmation_for_automatic_list_or_bulk_mail(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit, headers: dict
) -> None:
    org.auto_join("all_with_link")  # an auto-forward is scheduled too: only the mail is withheld
    result, sent = run(proc, app_engine, org.invite(headers=headers))
    assert result.outcome == "scheduled"
    assert (result.notice, sent) == (None, 0)
    assert nothing_for(mailpit, org.ana)


def test_no_confirmation_for_a_null_reverse_path(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    result, sent = run(proc, app_engine, org.invite(), mail_from="")
    assert result.outcome == "scheduled"
    assert (result.notice, sent) == (None, 0)
    assert nothing_for(mailpit, org.ana)


def test_no_confirmation_for_an_unverified_or_unknown_sender(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    org.kit.dmarc("attacker.test", p="none")
    spoofed, _ = run(proc, app_engine, org.invite(sign_as="attacker.test"))
    stranger = f"stranger@{org.domain}"
    unknown, _ = run(proc, app_engine, org.invite(stranger))

    assert spoofed.quarantine_reason == "sender_unverified"
    assert unknown.quarantine_reason == "unknown_user"
    assert spoofed.notice is None and unknown.notice is None
    assert nothing_for(mailpit, org.ana)
    assert nothing_for(mailpit, stranger)


def test_the_rfc_3834_guard() -> None:
    raw = b"From: ana@acme.test\r\nSubject: hi\r\n\r\nbody"
    user = uuid.uuid4()
    ok = {"mail_from": "ana@acme.test", "sender": "ana@acme.test", "sender_auth": "dmarc_pass",
          "user_id": user, "notetakers": frozenset({NOTETAKER})}
    assert may_confirm(raw, **ok)
    assert may_confirm(raw, **{**ok, "sender_auth": "arc_trusted"})
    assert may_confirm(b"Auto-Submitted: no\r\n" + raw, **ok)
    assert not may_confirm(raw, **{**ok, "sender_auth": "fail"})
    assert not may_confirm(raw, **{**ok, "sender_auth": None})
    assert not may_confirm(raw, **{**ok, "user_id": None})
    assert not may_confirm(raw, **{**ok, "sender": NOTETAKER.upper()})  # the notetaker itself
    assert not may_confirm(raw, **{**ok, "mail_from": "<>"})
    assert not may_confirm(raw, **{**ok, "mail_from": ""})
    assert not may_confirm(b"Auto-Submitted: auto-replied\r\n" + raw, **ok)
    assert not may_confirm(b"Precedence: junk\r\n" + raw, **ok)
    assert not may_confirm(b"X-Forwarded-To: bo@acme.test\r\n" + raw, **ok)


# ─── limits, injection, failures ──────────────────────────────────────────────────────────────────


def notice(to: str, title: str = "Sync", outcome: str = "scheduled") -> Notice:
    return Notice(
        to=to, outcome=outcome, reason=None, title=title, platform="zoom", zone=UTC,
        zone_name="UTC", occurrences=(Occurrence(datetime(2031, 3, 3, 9, 0, tzinfo=UTC)),),
    )


def test_the_31st_confirmation_in_an_hour_is_suppressed() -> None:
    mailer = MemoryMailer()
    confirmer = Confirmer(mailer, MemoryLimiter(confirm.PER_HOUR, confirm.WINDOW_SECONDS))

    async def send_all() -> list[bool]:
        sent = [await confirmer.send(notice("ana@acme.test")) for _ in range(31)]
        sent.append(await confirmer.send(notice("bo@acme.test")))
        return sent

    sent = asyncio.run(send_all())

    assert sent == [True] * 30 + [False, True]
    assert [m.to for m in mailer.outbox].count("ana@acme.test") == 30


def test_a_header_injection_in_the_summary_is_neutralised(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit
) -> None:
    evil = f"evil-{uuid.uuid4().hex[:8]}@example.test"
    result, sent = run(proc, app_engine, org.invite(summary=f"Review\\r\\nBcc: {evil}"))

    assert sent == 1
    [mail] = inbox(mailpit, org.ana)
    assert "\n" not in mail["Subject"] and "\r" not in mail["Subject"]
    assert mail["Subject"].startswith("munshi.ai: scheduled — Review")
    assert not mail.get("Bcc") and [a["Address"] for a in mail["To"]] == [org.ana]
    assert nothing_for(mailpit, evil)
    built = confirm.build(notice("ana@acme.test", title="A\r\nBcc: x@y\x00\x1b" + "z" * 300),
                          lead_s=120, horizon_days=14, notetaker=None)
    assert "\n" not in built.subject and "\x00" not in built.subject
    assert len(built.subject) <= len("munshi.ai: scheduled — ") + confirm.TITLE_MAX


def test_a_relay_failure_is_logged_and_the_decision_stands(
    org: Tenant, app_engine: Engine, kit: mailkit.Kit, limits: IntakeLimits,
    superuser_engine: Engine, mailpit: Mailpit, caplog: pytest.LogCaptureFixture,
) -> None:
    settings = IntakeSettings(ANDAI_ENV="dev", ANDAI_INTAKE_ADDRESSES=NOTETAKER,
                              SMTP_HOST="127.0.0.1", SMTP_PORT="1")
    confirmer = Confirmer(SmtpMailer(settings), MemoryLimiter(confirm.PER_HOUR, 3600))
    down = Processor(app_engine, settings, limits, policy=Policy(resolver=kit.zone), batch=50,
                     confirmer=confirmer)

    result, sent = run(down, app_engine, org.invite(summary="Planning"))

    assert (result.outcome, sent) == ("scheduled", 0)
    assert len(meetings_of(superuser_engine, org.id)) == 1
    assert any("confirmation not sent: MailError" in r.getMessage() for r in caplog.records)
    assert all(org.ana not in r.getMessage() for r in caplog.records)


def test_nothing_is_confirmed_for_a_result_that_was_not_committed(
    proc: Processor, org: Tenant, app_engine: Engine, mailpit: Mailpit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(processor_mod, "_finish", crash)
    inbound = deliver(app_engine, org.invite())
    result = None
    for _ in range(50):
        result = next((r for r in proc.run_once() if r.inbound_id == inbound), None)
        if result is not None:
            break
    assert result is not None

    assert (result.status, result.notice) == ("received", None)
    assert asyncio.run(proc.confirm([result])) == 0
    assert nothing_for(mailpit, org.ana)


def test_results_without_a_confirmer_send_nothing(app_engine: Engine, limits: IntakeLimits) -> None:
    bare = Processor(app_engine, IntakeSettings(ANDAI_ENV="dev"), limits)
    assert asyncio.run(bare.confirm([Result(uuid.uuid4(), "done", notice=notice("a@b.test"))])) == 0
