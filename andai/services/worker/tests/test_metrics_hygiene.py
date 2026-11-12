"""No metric carries identity or content, and none grows with the data: all three services' `/metrics`
(andai-worker, andai-core, andai-intake test apps) scraped after seeding 50 orgs, 200 users and 500
meetings with emails, titles and links, inbound mail with senders and subjects (with and without an
org), and a mail and transcript through the process counters.

The scrape is parsed with `prometheus_client.parser`. It fails when a label name is outside
`andai_core.metrics.LABEL_ALLOWLIST`, a label value is not of the `LABEL_VALUE` shape (`le` must be a
number), a label value looks like an email, a UUID, a URL or an IP address, a metric family has more
than `MAX_SERIES` series, or any seeded email, title, link, name, id, sender or subject appears
anywhere in the text.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import Engine, create_engine, insert, text

from andai_core import metrics as core_metrics
from andai_core.db.models import Meeting
from andai_core.db.session import transaction
from andai_core.mail import Mail, MailError, MemoryMailer, SmtpMailer
from andai_core.meetings import ingest
from andai_core.meetings.service import Meetings
from andai_intake.main import IntakeServices
from andai_intake.main import create_app as intake_app
from andai_intake.settings import IntakeSettings
from andai_worker.main import WorkerServices
from andai_worker.main import create_app as worker_app
from andai_worker.settings import WorkerSettings

from .worker_support import make_harness, new_member, support

ORGS = 50
USERS_PER_ORG = 4
MEETINGS_PER_ORG = 10
MAX_SERIES = 200

EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,}", re.IGNORECASE)
UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.IGNORECASE)
URL = re.compile(r"(https?|wss?)://|www\.|\.(com|test|us|in|org|net)(/|$)", re.IGNORECASE)

STATUSES = ("done", "failed", "cancelled", "recording", "scheduled", "dispatching")
PLATFORMS = ("google_meet", "teams", "zoom")


def _looks_like_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _meeting_row(org_id, user_id, n: int, now: datetime, rng: random.Random) -> dict:
    meeting_id = uuid.uuid4()
    status = STATUSES[n % len(STATUSES)]
    title = f"Synthetic review {meeting_id.hex[:10]} with Asha Verma"
    native = f"{meeting_id.int % 10**11:011d}"
    ended = now - timedelta(minutes=rng.randint(1, 600))
    row = {
        "id": meeting_id,
        "org_id": org_id,
        "requested_by": user_id,
        "source": ("portal_link", "invite_mail")[n % 2],
        "platform": PLATFORMS[n % 3],
        "join_url": f"https://zoom.us/j/{native}?pwd=secret{meeting_id.hex[:6]}",
        "join_url_key": f"zoom.us/j/{meeting_id.hex}",
        "status": status,
        "failure_reason": "admission_timeout" if status == "failed" else None,
        "settings_snapshot": {},
        "title": title,
        "title_search": title.lower(),
    }
    if status in ("done", "failed", "cancelled"):
        row |= {
            "ended_at": ended,
            "platform_meeting_id": 10_000 + n if n % 4 else None,
            "transcript_state": ("none", "pending", "stored", "failed")[n % 4],
        }
        if row["transcript_state"] == "stored":
            row |= {
                "transcript_at": ended + timedelta(seconds=rng.randint(5, 300)),
                "mom_state": ("none", "pending", "stored", "failed")[(n // 4) % 4],
            }
            if row["mom_state"] == "stored":
                row |= {
                    "mom_at": row["transcript_at"] + timedelta(seconds=rng.randint(10, 900)),
                    "recap_state": ("pending", "sent", "failed")[n % 3],
                }
                if row["recap_state"] == "sent":
                    row["recap_at"] = row["mom_at"] + timedelta(seconds=rng.randint(1, 120))
    if status == "done":
        row["started_at"] = ended - timedelta(minutes=30)
    if status == "scheduled":
        row["scheduled_start"] = now + timedelta(minutes=rng.randint(-10, 600))
    if n % 17 == 0:
        row |= {"deleted_at": now - timedelta(minutes=rng.randint(1, 60)),
                "deletion_state": ("pending", "failed")[n % 2]}
    return row


def _seed(engine: Engine, su: Engine, now: datetime) -> dict[str, list[str]]:
    """The world, and every identifying string in it."""
    rng = random.Random(1234)
    found: dict[str, list[str]] = {
        "emails": [], "ids": [], "titles": [], "links": [], "names": [], "subjects": [],
    }
    for o in range(ORGS):
        owner = new_member(engine, password=None)
        members = [owner] + [
            new_member(engine, password=None, role="member", org_id=owner.org_id)
            for _ in range(USERS_PER_ORG - 1)
        ]
        rows = [
            _meeting_row(owner.org_id, members[n % USERS_PER_ORG].user_id, o * 10 + n, now, rng)
            for n in range(MEETINGS_PER_ORG)
        ]
        with transaction(engine, org_id=owner.org_id) as tx:
            for row in rows:  # one statement per row: the rows carry different columns
                tx.execute(insert(Meeting).values(**row))
            name = tx.scalar(text("SELECT name FROM orgs"))
        found["emails"] += [m.email for m in members]
        found["ids"] += [str(owner.org_id), *(str(m.user_id) for m in members)]
        found["ids"] += [str(r["id"]) for r in rows]
        found["titles"] += [r["title"] for r in rows]
        found["links"] += [r["join_url"] for r in rows]
        found["names"].append(name)
        for k in range(2):  # inbound mail: one org-less, one attributed
            sender = f"organiser{o}x{k}@sender{o}.test"
            subject = f"Synthetic invite {uuid.uuid4().hex[:8]}"
            status = ("received", "quarantined", "done")[(o + k) % 3]
            with su.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO inbound_mail (raw_sha256, mail_from, rcpt_to, peer_ip, helo,"
                        " status, outcome, quarantine_reason, attempts, received_at, org_id,"
                        " sender, subject_trim)"
                        " VALUES (:sha, :sender, ARRAY['notetaker@munshi.test'], '198.51.100.7',"
                        " 'mx.sender.test', :status, :outcome, :reason, 1, :at, :org, :sender,"
                        " :subject)"
                    ),
                    {
                        "sha": uuid.uuid4().hex * 2, "sender": sender, "subject": subject,
                        "status": status,
                        "outcome": {"received": None, "quarantined": "quarantined",
                                    "done": "scheduled"}[status],
                        "reason": "unknown_user" if status == "quarantined" else None,
                        "at": now - timedelta(minutes=rng.randint(1, 600)),
                        "org": owner.org_id if k else None,
                    },
                )
            found["emails"].append(sender)
            found["subjects"].append(subject)
    return found


def _through_the_process_counters(found: dict[str, list[str]]) -> None:
    """A refused mail to a seeded address and a transcript with write times."""
    refusing = SmtpMailer(support.make_settings(SMTP_HOST="127.0.0.1", SMTP_PORT="1"))
    with pytest.raises(MailError):
        asyncio.run(refusing.send(Mail(to=found["emails"][0], subject=found["titles"][0],
                                       text=found["links"][0], kind="recap")))
    ingest._segments(
        {"segments": [
            {"segment_id": "a", "text": found["titles"][1], "start": 0.0, "end": 1.0,
             "speaker": found["emails"][1], "absolute_end_time": "2026-11-01T10:00:01Z",
             "created_at": "2026-11-01T10:00:03Z"},
            {"segment_id": "b", "text": found["titles"][2], "start": 1.0, "end": 2.0},
        ]},
        uuid.uuid4(),
        datetime(2026, 11, 1, 10, tzinfo=UTC),
    )


def _scrapes(engine: Engine) -> dict[str, str]:
    harness = make_harness(engine)
    settings = WorkerSettings(ANDAI_ENV="dev")
    worker = WorkerServices(
        settings=settings,
        engine=engine,
        meetings=Meetings(engine, harness.services.keyring, harness.services.gateway, settings,
                          harness.clock),
        mailer=MemoryMailer(),
    )
    bodies = {}
    with TestClient(worker_app(worker, run_jobs=False)) as client:
        bodies["andai-worker"] = client.get("/metrics").text
    bodies["andai-core"] = harness.client(origin=None).get("/metrics").text
    intake = IntakeServices(settings=IntakeSettings(ANDAI_ENV="dev"), engine=engine)
    with TestClient(intake_app(intake, run_jobs=False)) as client:
        bodies["andai-intake"] = client.get("/metrics").text
    return bodies


def _violations(service: str, body: str) -> list[str]:
    problems = []
    for family in text_string_to_metric_families(body):
        if len(family.samples) > MAX_SERIES:
            problems.append(f"{service} {family.name}: {len(family.samples)} series")
        for sample in family.samples:
            where = f"{service} {sample.name}{sample.labels}"
            for name, value in sample.labels.items():
                if name not in core_metrics.LABEL_ALLOWLIST:
                    problems.append(f"{where}: label {name!r} not allowed")
                if name == "le":
                    float(value)  # a bucket bound, `+Inf` included
                    continue
                if not core_metrics.LABEL_VALUE.fullmatch(value):
                    problems.append(f"{where}: value {value!r} not of the label shape")
                for kind, pattern in (("email", EMAIL), ("uuid", UUID), ("url", URL)):
                    if pattern.search(value):
                        problems.append(f"{where}: value {value!r} looks like an {kind}")
                if _looks_like_ip(value):
                    problems.append(f"{where}: value {value!r} looks like an IP")
    return problems


def test_no_metric_carries_identity_or_grows_with_the_data(
    fresh_app_engine: Engine, postgres
) -> None:
    now = datetime.now(UTC)
    su = create_engine(
        postgres.url("postgres", postgres.superuser_password, fresh_app_engine.url.database)
    )
    try:
        found = _seed(fresh_app_engine, su, now)
        with su.connect() as conn:
            users = conn.execute(text("SELECT count(*) FROM users")).scalar_one()
            meetings = conn.execute(text("SELECT count(*) FROM meetings")).scalar_one()
    finally:
        su.dispose()
    assert (users, meetings) == (ORGS * USERS_PER_ORG, ORGS * MEETINGS_PER_ORG)
    _through_the_process_counters(found)

    bodies = _scrapes(fresh_app_engine)

    problems = [p for service, body in bodies.items() for p in _violations(service, body)]
    assert problems == []
    for service, body in bodies.items():
        lowered = body.lower()
        leaked = [
            value for values in found.values() for value in values if value.lower() in lowered
        ]
        assert leaked == [], (service, leaked[:5])
        assert "198.51.100.7" not in body and "secret" not in lowered
    # The seeded world shows up as counts, so a leak would have had something to leak.
    worker = bodies["andai-worker"]
    assert re.search(r'^munshi_pipeline_pending\{stage="ingest"\} [1-9]', worker, re.MULTILINE)
    assert re.search(r'^munshi_intake_messages_24h\{.*status="quarantined"\} [1-9]', worker,
                     re.MULTILINE)
    assert re.search(r"^munshi_asr_segment_lag_seconds_count [1-9]", worker, re.MULTILINE)
    assert re.search(r'^munshi_mail_sent_total\{kind="recap",outcome="failed"\} [1-9]',
                     bodies["andai-core"], re.MULTILINE)
