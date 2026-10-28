"""The alert evaluator: an incident opens once, mails once, stays quiet while it lasts, closes with
one recovery mail; the 24 h rate follows D-39 (exclusions, the 5-join floor, the 95% line); a relay
outage delays a mail without losing or repeating it; one evaluator runs per deployment; the mail
goes only to the operator address and names no meeting, link or org. Each test has its own
database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select

from andai_core.db.models import AlertState, CanaryRun
from andai_core.db.session import transaction
from andai_core.mail import Mail, MailError, MemoryMailer, SmtpMailer
from andai_core.settings import Settings
from andai_worker.alerts import AlertEvaluator
from .worker_support import Clock, mailpit_messages, new_member, seed_meeting

OPERATOR = "ops@example.test"


class FlakyMailer(MemoryMailer):
    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def send(self, mail: Mail) -> None:
        if self.down:
            raise MailError("relay down")
        await super().send(mail)


def evaluator(engine: Engine, mailer=None, clock=None) -> AlertEvaluator:
    return AlertEvaluator(engine, mailer or MemoryMailer(), OPERATOR, clock=clock or Clock())


def canary(engine: Engine, outcome: str, *, platform: str = "google_meet",
           reason: str | None = None, at: datetime | None = None) -> None:
    started = at or datetime.now(UTC)
    with transaction(engine) as tx:
        tx.execute(
            insert(CanaryRun).values(
                id=uuid.uuid4(), platform=platform, state="done", due_at=started,
                started_at=started, finished_at=started, outcome=outcome,
                reason=reason if outcome == "failure" else None,
            )
        )


def tick(ev: AlertEvaluator) -> int:
    try:
        return asyncio.run(ev.tick())
    finally:
        ev.release()


def incidents(engine: Engine) -> dict[tuple[str, str], tuple]:
    with transaction(engine) as tx:
        return {
            (r.kind, r.platform): (r.open, r.notice)
            for r in tx.scalars(select(AlertState)).all()
        }


def test_canary_incident_fires_once_stays_quiet_and_recovers_once(
    fresh_app_engine: Engine,
) -> None:
    mailer = MemoryMailer()
    ev = evaluator(fresh_app_engine, mailer)
    t = datetime.now(UTC) - timedelta(hours=3)

    canary(fresh_app_engine, "success", at=t)
    canary(fresh_app_engine, "failure", reason="admission_denied", at=t + timedelta(hours=1))
    assert tick(ev) == 0
    assert mailer.outbox == []

    canary(fresh_app_engine, "failure", reason="ui_changed", at=t + timedelta(hours=2))
    assert tick(ev) == 1
    [opened] = mailer.outbox
    assert opened.to == OPERATOR
    assert opened.subject == "[munshi.ai] Bot joins failing on Google Meet"
    assert "failure - ui_changed" in opened.text and "failure - admission_denied" in opened.text
    assert incidents(fresh_app_engine)[("canary_failures", "google_meet")] == (True, None)

    canary(fresh_app_engine, "failure", reason="ui_changed", at=t + timedelta(hours=2, minutes=30))
    assert tick(ev) == 0 and tick(ev) == 0
    assert len(mailer.outbox) == 1

    canary(fresh_app_engine, "success", at=t + timedelta(hours=3))
    assert tick(ev) == 1
    recovered = mailer.outbox[1]
    assert recovered.subject == "[munshi.ai] Recovered: bot joins on Google Meet"
    assert incidents(fresh_app_engine)[("canary_failures", "google_meet")] == (False, None)
    assert tick(ev) == 0
    assert len(mailer.outbox) == 2

    # A new incident later is a new mail.
    canary(fresh_app_engine, "failure", reason="internal_error", at=t + timedelta(hours=4))
    canary(fresh_app_engine, "failure", reason="internal_error", at=t + timedelta(hours=5))
    assert tick(ev) == 1
    assert len(mailer.outbox) == 3


def test_platforms_are_separate_incidents(fresh_app_engine: Engine) -> None:
    mailer = MemoryMailer()
    ev = evaluator(fresh_app_engine, mailer)
    t = datetime.now(UTC) - timedelta(hours=2)
    for i in range(2):
        canary(fresh_app_engine, "failure", platform="zoom", reason="ui_changed",
               at=t + timedelta(minutes=i))
        canary(fresh_app_engine, "success", platform="teams", at=t + timedelta(minutes=i))

    assert tick(ev) == 1
    assert mailer.outbox[0].subject == "[munshi.ai] Bot joins failing on Zoom"
    assert incidents(fresh_app_engine) == {("canary_failures", "zoom"): (True, None)}


@pytest.mark.parametrize(
    ("successes", "failures", "excluded", "fires"),
    [
        (18, 2, 0, True),    # 90% of 20
        (19, 1, 0, False),   # exactly 95% is not below 95%
        (20, 1, 0, False),   # 95.2%
        (2, 2, 0, False),    # 50%, but only 4 counted joins (< 5)
        (3, 2, 0, True),     # 60% of 5
        (4, 0, 10, False),   # denied, cancelled and removed meetings are not counted
    ],
)
def test_join_rate_incident_follows_d39(
    fresh_app_engine: Engine, successes: int, failures: int, excluded: int, fires: bool
) -> None:
    member = new_member(fresh_app_engine)
    for _ in range(successes):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id)
    for _ in range(failures):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id, status="failed",
                     failure_reason="blocked_by_policy")
    kinds = [
        {"status": "failed", "failure_reason": "admission_denied"},
        {"status": "cancelled"},
        {"status": "done", "end_reason": "evicted"},
    ]
    for i in range(excluded):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id, **kinds[i % 3])
    # Meetings older than 24 h do not count.
    for _ in range(10):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id, status="failed",
                     failure_reason="ui_changed",
                     created_at=datetime.now(UTC) - timedelta(hours=25))
    mailer = MemoryMailer()

    sent = tick(evaluator(fresh_app_engine, mailer))

    assert sent == (1 if fires else 0)
    if fires:
        [mail] = mailer.outbox
        assert mail.subject == "[munshi.ai] Bot joins failing on Zoom"
        counted = successes + failures
        assert f"({successes} of {counted} counted joins)" in mail.text
        assert incidents(fresh_app_engine)[("join_rate", "zoom")] == (True, None)


def test_join_rate_recovers_when_the_rate_is_back(fresh_app_engine: Engine) -> None:
    member = new_member(fresh_app_engine)
    for _ in range(3):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id)
    for _ in range(2):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id, status="failed",
                     failure_reason="ui_changed")
    mailer = MemoryMailer()
    ev = evaluator(fresh_app_engine, mailer)
    assert tick(ev) == 1

    for _ in range(40):
        seed_meeting(fresh_app_engine, member.org_id, member.user_id)
    assert tick(ev) == 1
    assert [m.subject for m in mailer.outbox] == [
        "[munshi.ai] Bot joins failing on Zoom",
        "[munshi.ai] Recovered: bot joins on Zoom",
    ]


def test_a_relay_outage_delays_the_mail_without_losing_or_repeating_it(
    fresh_app_engine: Engine,
) -> None:
    mailer = FlakyMailer()
    mailer.down = True
    ev = evaluator(fresh_app_engine, mailer)
    canary(fresh_app_engine, "failure", reason="ui_changed")
    canary(fresh_app_engine, "failure", reason="ui_changed")

    assert tick(ev) == 0
    assert incidents(fresh_app_engine)[("canary_failures", "google_meet")] == (True, "open")
    mailer.down = False
    assert tick(ev) == 1
    assert tick(ev) == 0
    assert len(mailer.outbox) == 1


def test_one_evaluator_per_deployment(fresh_app_engine: Engine) -> None:
    first, second = MemoryMailer(), MemoryMailer()
    a, b = evaluator(fresh_app_engine, first), evaluator(fresh_app_engine, second)
    canary(fresh_app_engine, "failure", reason="ui_changed")
    canary(fresh_app_engine, "failure", reason="ui_changed")
    try:
        assert asyncio.run(a.tick()) == 1
        assert asyncio.run(b.tick()) == 0  # A holds the lock
    finally:
        a.release()
        b.release()
    assert (len(first.outbox), len(second.outbox)) == (1, 0)


def test_alert_mail_names_no_meeting_link_or_org(fresh_app_engine: Engine) -> None:
    member = new_member(fresh_app_engine)
    ids = [
        seed_meeting(fresh_app_engine, member.org_id, member.user_id, status="failed",
                     failure_reason="ui_changed")
        for _ in range(5)
    ]
    mailer = MemoryMailer()

    assert tick(evaluator(fresh_app_engine, mailer)) == 1

    [mail] = mailer.outbox
    for value in (*map(str, ids), str(member.org_id), member.email, "http", "pwd", "secret"):
        assert value not in mail.text and value not in mail.subject


def test_alert_reaches_mailpit_over_smtp(fresh_app_engine: Engine, mailpit) -> None:
    to = f"ops-{uuid.uuid4().hex[:8]}@example.test"
    settings = Settings(SMTP_HOST=mailpit.smtp_host, SMTP_PORT=str(mailpit.smtp_port))
    ev = AlertEvaluator(fresh_app_engine, SmtpMailer(settings), to, clock=Clock())
    canary(fresh_app_engine, "failure", platform="teams", reason="blocked_by_policy")
    canary(fresh_app_engine, "failure", platform="teams", reason="blocked_by_policy")

    assert tick(ev) == 1

    [message] = mailpit_messages(mailpit.api_url, to)
    assert message["Subject"] == "[munshi.ai] Bot joins failing on Microsoft Teams"
    assert "blocked_by_policy" in message["Text"]
