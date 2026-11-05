"""The processor end to end on a disposable Postgres and Valkey, with stub DNS: signed invite mail
is stored as the SMTP receiver stores it, then processed into scheduled meetings or a recorded
reason.

Each test uses its own sender domain, org and limiter namespace. The processor claims whatever is
due in the shared test database; `drain` runs it until the test's own message has its result.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis
from sqlalchemy import Engine, text

import mailkit
from andai_core.db.session import transaction
from andai_intake import processor as processor_mod
from andai_intake.auth.verdict import Policy
from andai_intake.limits import IntakeLimits
from andai_intake.processor import Processor
from andai_intake.purge import purge_once
from andai_intake.settings import IntakeSettings
from invitekit import NOTETAKER
from schedulekit import MEET, PEER, Tenant, deliver, drain, meetings_of, row

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


@pytest.fixture
def proc(app_engine: Engine, kit: mailkit.Kit, limits: IntakeLimits) -> Processor:
    settings = IntakeSettings(ANDAI_ENV="dev", ANDAI_INTAKE_ADDRESSES=NOTETAKER)
    return Processor(app_engine, settings, limits, policy=Policy(resolver=kit.zone), batch=50)


# ─── scheduled ────────────────────────────────────────────────────────────────────────────────────


def test_a_verified_members_invite_becomes_a_scheduled_meeting(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    start = (datetime.now(UTC) + timedelta(days=3)).replace(second=0, microsecond=0)
    inbound = deliver(app_engine, org.invite(start=start, summary="Quarterly design review"))

    result = drain(proc, inbound)

    assert (result.status, result.outcome) == ("done", "scheduled")
    [meeting] = meetings_of(superuser_engine, org.id)
    assert meeting["id"] in result.created
    assert (meeting["status"], meeting["source"]) == ("scheduled", "invite_mail")
    assert (meeting["requested_by"], meeting["team_id"]) == (org.ana_id, org.team)
    assert meeting["title"] == "Quarterly design review"
    assert meeting["scheduled_start"] == meeting["occurrence_key"] == start
    assert (meeting["platform"], meeting["join_url"]) == ("google_meet", MEET)
    assert meeting["settings_snapshot"] == {"language": "en"}
    stored = row(superuser_engine, inbound)
    assert (stored["status"], stored["outcome"], stored["org_id"]) == ("done", "scheduled", org.id)
    assert (stored["resolved_user_id"], stored["sender"]) == (org.ana_id, org.ana)
    assert stored["sender_auth"] == "dmarc_pass"
    assert stored["subject_trim"] == "Invitation: Weekly sync"
    with superuser_engine.connect() as conn:
        event = conn.execute(
            text("SELECT * FROM invite_events WHERE org_id = :o"), {"o": org.id}
        ).one()._mapping
    assert (event["requested_by"], event["team_id"], event["state"]) == (
        org.ana_id, org.team, "active"
    )
    assert event["last_inbound_id"] == inbound and event["id"] == meeting["invite_event_id"]


def test_an_update_moves_and_a_cancel_cancels(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    uid = f"series-{uuid.uuid4().hex}@{org.domain}"
    start = (datetime.now(UTC) + timedelta(days=2)).replace(second=0, microsecond=0)
    assert drain(proc, deliver(app_engine, org.invite(uid=uid, start=start))).outcome == "scheduled"
    moved = drain(proc, deliver(app_engine, org.invite(uid=uid, start=start + timedelta(hours=1),
                                                       sequence=1)))
    assert moved.outcome == "updated"
    [meeting] = meetings_of(superuser_engine, org.id)
    assert meeting["scheduled_start"] == start + timedelta(hours=1)
    cancelled = drain(proc, deliver(app_engine, org.invite(uid=uid, method="CANCEL", sequence=2,
                                                           link=None)))
    assert cancelled.outcome == "cancelled"
    [meeting] = meetings_of(superuser_engine, org.id)
    assert (meeting["status"], meeting["end_reason"]) == ("cancelled", "invite_cancelled")


def test_the_same_invite_again_creates_nothing_more(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    uid = f"{uuid.uuid4().hex}@{org.domain}"
    start = (datetime.now(UTC) + timedelta(days=2)).replace(second=0, microsecond=0)
    stamp = datetime.now(UTC) - timedelta(minutes=5)
    drain(proc, deliver(app_engine, org.invite(uid=uid, start=start, dtstamp=stamp)))
    # The same calendar in a second message (a resend): same SEQUENCE and DTSTAMP.
    result = drain(proc, deliver(app_engine, org.invite(uid=uid, start=start, dtstamp=stamp)))
    assert (result.status, result.outcome, result.ignore_reason) == (
        "ignored", "ignored", "duplicate"
    )
    assert len(meetings_of(superuser_engine, org.id)) == 1


# ─── quarantine ───────────────────────────────────────────────────────────────────────────────────


def test_a_spoofed_member_is_quarantined_for_the_members_org(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    org.kit.dmarc("attacker.test", p="none")
    inbound = deliver(app_engine, org.invite(sign_as="attacker.test"))
    result = drain(proc, inbound)
    assert (result.status, result.quarantine_reason, result.org_id) == (
        "quarantined", "sender_unverified", org.id
    )
    stored = row(superuser_engine, inbound)
    assert (stored["sender"], stored["sender_auth"]) == (org.ana, "fail")
    assert stored["auth_summary"]["verdict"] == "fail"
    assert meetings_of(superuser_engine, org.id) == []


def test_an_unknown_sender_is_quarantined_for_the_domains_org(
    proc: Processor, org: Tenant, app_engine: Engine
) -> None:
    result = drain(proc, deliver(app_engine, org.invite(f"stranger@{org.domain}")))
    assert (result.quarantine_reason, result.org_id, result.user_id) == (
        "unknown_user", org.id, None
    )


def test_the_21st_invite_in_an_hour_is_rate_limited_for_the_senders_org(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    for _ in range(20):
        assert proc.limits.admit_verified(org.ana, org.id)
    inbound = deliver(app_engine, org.invite())
    result = drain(proc, inbound)
    assert (result.quarantine_reason, result.org_id, result.user_id) == (
        "rate_limited", org.id, org.ana_id
    )
    assert row(superuser_engine, inbound)["sender_auth"] == "dmarc_pass"


def test_the_peer_limit_comes_first(proc: Processor, org: Tenant, app_engine: Engine) -> None:
    for _ in range(120):
        assert proc.limits.admit_peer(PEER)
    result = drain(proc, deliver(app_engine, org.invite()))
    assert (result.quarantine_reason, result.org_id) == ("rate_limited", org.id)


def test_no_link_and_unreadable_calendars_are_quarantined(
    proc: Processor, org: Tenant, app_engine: Engine
) -> None:
    no_link = drain(proc, deliver(app_engine, org.invite(link=None)))
    assert (no_link.quarantine_reason, no_link.org_id) == ("no_meeting_link", org.id)
    broken = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nDTSTART:garbage\r\nEND:VEVENT\r\n"
    unreadable = drain(proc, deliver(app_engine, org.invite(calendar_text=broken)))
    assert (unreadable.quarantine_reason, unreadable.org_id) == ("parse_error", org.id)


@pytest.mark.parametrize("method", ["REPLY", "COUNTER"])
def test_replies_and_counters_are_ignored(
    proc: Processor, org: Tenant, app_engine: Engine, method: str
) -> None:
    result = drain(proc, deliver(app_engine, org.invite(method=method)))
    assert (result.status, result.ignore_reason) == ("ignored", "method_not_handled")


# ─── bot.autoJoin ─────────────────────────────────────────────────────────────────────────────────

AUTO = {"Auto-Submitted": "auto-forwarded"}


def test_auto_join_off_creates_nothing(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    org.auto_join("off")
    result = drain(proc, deliver(app_engine, org.invite()))
    assert (result.status, result.ignore_reason) == ("ignored", "auto_join_off")
    assert meetings_of(superuser_engine, org.id) == []


def test_only_when_invited_ignores_an_auto_forwarded_copy(
    proc: Processor, org: Tenant, app_engine: Engine
) -> None:
    result = drain(proc, deliver(app_engine, org.invite(headers=AUTO)))
    assert (result.status, result.ignore_reason) == ("ignored", "auto_forwarded")


def test_all_with_link_schedules_an_auto_forwarded_copy(
    proc: Processor, org: Tenant, app_engine: Engine
) -> None:
    org.auto_join("all_with_link")
    assert drain(proc, deliver(app_engine, org.invite(headers=AUTO))).outcome == "scheduled"


def test_the_team_override_beats_the_org_value(
    proc: Processor, org: Tenant, app_engine: Engine
) -> None:
    org.auto_join("off")
    org.auto_join("all_with_link", team=org.team)
    assert drain(proc, deliver(app_engine, org.invite(headers=AUTO))).outcome == "scheduled"
    other = Tenant(app_engine, org.kit)
    other.auto_join("all_with_link")
    other.auto_join("off", team=other.team)
    result = drain(proc, deliver(app_engine, other.invite()))
    assert result.ignore_reason == "auto_join_off"


# ─── time ─────────────────────────────────────────────────────────────────────────────────────────


def test_a_running_meeting_is_joined_now_and_an_ended_one_ignored(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    running = drain(proc, deliver(app_engine, org.invite(start=now - timedelta(minutes=10))))
    assert running.outcome == "joined_now" and running.joined_now == running.created
    [meeting] = meetings_of(superuser_engine, org.id)
    assert meeting["scheduled_start"] <= datetime.now(UTC)
    assert meeting["occurrence_key"] == now - timedelta(minutes=10)

    ended = drain(proc, deliver(app_engine, org.invite(start=now - timedelta(hours=2))))
    assert (ended.status, ended.ignore_reason) == ("ignored", "ended")
    assert len(meetings_of(superuser_engine, org.id)) == 1


# ─── failures, retries and retention ──────────────────────────────────────────────────────────────


def test_a_crash_before_the_commit_leaves_the_message_to_be_processed_once(
    proc: Processor,
    org: Tenant,
    app_engine: Engine,
    superuser_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbound = deliver(app_engine, org.invite())
    # A processor claims it and dies: the lease holds it, then runs out.
    claimed = processor_mod.claim(app_engine, 1000)
    assert inbound in {c.id for c in claimed}
    assert all(r.inbound_id != inbound for r in proc.run_once())
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE inbound_mail SET lease_until = now() - interval '1 second'"
                 " WHERE id = ANY(:ids)"),
            {"ids": [c.id for c in claimed]},
        )

    # The next run fails after writing its meetings, before the commit: nothing stays.
    real_finish = processor_mod._finish

    def finish_then_crash(tx, inbound_id, recorded):
        if inbound_id == inbound and recorded.status == "done":
            raise RuntimeError("crash before commit")
        return real_finish(tx, inbound_id, recorded)

    monkeypatch.setattr(processor_mod, "_finish", finish_then_crash)
    assert drain(proc, inbound).status == "received"
    assert meetings_of(superuser_engine, org.id) == []
    assert row(superuser_engine, inbound)["last_error"] == "RuntimeError"

    monkeypatch.setattr(processor_mod, "_finish", real_finish)
    result = drain(proc, inbound)
    assert (result.outcome, row(superuser_engine, inbound)["attempts"]) == ("scheduled", 3)
    assert len(meetings_of(superuser_engine, org.id)) == 1


def test_a_message_that_keeps_failing_is_quarantined_after_five_attempts(
    proc: Processor,
    org: Tenant,
    app_engine: Engine,
    superuser_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(processor_mod, "parse", broken)
    inbound = deliver(app_engine, org.invite())
    assert drain(proc, inbound).status == "received"
    with superuser_engine.begin() as conn:
        conn.execute(text("UPDATE inbound_mail SET attempts = 4 WHERE id = :i"), {"i": inbound})
    result = drain(proc, inbound)
    assert (result.status, result.quarantine_reason, result.org_id) == (
        "quarantined", "parse_error", org.id
    )
    assert row(superuser_engine, inbound)["attempts"] == 5


def test_the_30_day_purge_removes_mail_an_event_points_at_and_the_event_stays(
    proc: Processor, org: Tenant, app_engine: Engine, superuser_engine: Engine
) -> None:
    inbound = deliver(app_engine, org.invite())
    assert drain(proc, inbound).outcome == "scheduled"
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE inbound_mail SET received_at = now() - interval '31 days' WHERE id = :i"),
            {"i": inbound},
        )
    purged = purge_once(app_engine, raw_days=7, row_days=30)
    assert purged.rows_deleted >= 1
    with superuser_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM inbound_mail WHERE id = :i"), {"i": inbound}
        ).scalar_one() == 0
        event = conn.execute(
            text("SELECT last_inbound_id, state FROM invite_events WHERE org_id = :o"),
            {"o": org.id},
        ).one()
    assert (event.last_inbound_id, event.state) == (inbound, "active")
    assert len(meetings_of(superuser_engine, org.id)) == 1


def test_events_and_meetings_are_written_as_the_senders_org_only(
    proc: Processor, org: Tenant, app_engine: Engine
) -> None:
    other = Tenant(app_engine, org.kit)
    drain(proc, deliver(app_engine, org.invite()))
    with transaction(app_engine, org_id=other.id) as tx:
        seen = tx.execute(text("SELECT count(*) FROM invite_events")).scalar_one()
        meetings = tx.execute(text("SELECT count(*) FROM meetings")).scalar_one()
    assert (seen, meetings) == (0, 0)
    with transaction(app_engine, org_id=org.id) as tx:
        assert tx.execute(text("SELECT count(*) FROM invite_events")).scalar_one() == 1
