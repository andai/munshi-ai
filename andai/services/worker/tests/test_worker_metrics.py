"""andai-worker `/metrics`: join counts across orgs through `andai_join_stats`, D-39 success rates,
canary streaks, the deletion backlog through `andai_deletion_backlog`, the minutes pipeline through
`andai_mom_stats`, sealed secrets per KEK id, pipeline backlogs, failures and stage durations through
`andai_pipeline_stats` / `andai_pipeline_durations`, intake counts through `andai_intake_stats`, the
database size, and labels that carry no personal or customer data.

Each test that counts rows uses a brand-new database (`fresh_app_engine`), so the golden text is
exact.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, insert, text

from andai_core import metrics as core_metrics
from andai_core.db.models import CanaryRun, Org
from andai_core.db.session import transaction
from andai_core.mail import MemoryMailer
from andai_core.meetings.service import Meetings
from andai_worker import metrics
from andai_worker.main import WorkerServices, create_app
from andai_worker.settings import WorkerSettings
from .worker_support import (
    canary_setup,
    give_principal,
    make_harness,
    new_member,
    seed_meeting,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _run(engine: Engine, platform: str, started: datetime, outcome: str, reason=None) -> None:
    with transaction(engine) as tx:
        tx.execute(
            insert(CanaryRun).values(
                id=uuid.uuid4(), platform=platform, state="done", due_at=started,
                started_at=started, finished_at=started + timedelta(minutes=1),
                outcome=outcome, reason=reason,
            )
        )


def _world(engine: Engine) -> dict:
    """Two customer orgs, the canary org, meetings of every kind, and canary runs."""
    harness = make_harness(engine)
    a = new_member(engine)
    b = new_member(engine)
    canary_org = canary_setup(harness, engine)
    with transaction(engine, org_id=canary_org) as tx:
        canary_owner = tx.scalar(text("SELECT user_id FROM org_memberships LIMIT 1"))
        canary_name = tx.scalar(text("SELECT name FROM orgs"))

    def m(member, **kw):
        return seed_meeting(engine, member.org_id, member.user_id, **kw)

    ids = []
    # org A
    ids += [m(a) for _ in range(3)]                                                   # zoom success
    ids.append(m(a, status="failed", failure_reason="admission_timeout"))             # failure
    ids.append(m(a, status="cancelled"))                                             # excluded
    ids.append(m(a, status="failed", failure_reason="admission_denied"))             # excluded
    ids.append(m(a, platform="google_meet", status="recording"))                     # success
    ids.append(m(a, platform="google_meet", status="dispatching"))                   # pending
    ids.append(m(a, created_at=datetime.now(UTC) - timedelta(hours=25)))             # out of window
    # org B
    ids += [m(b) for _ in range(2)]                                                   # zoom success
    ids.append(m(b, end_reason="evicted"))                                           # excluded
    ids.append(m(b, platform="teams", status="failed", failure_reason="blocked_by_policy"))
    # failed after admission: reached recording, so a success (D-39)
    ids.append(m(b, platform="teams", status="failed", failure_reason="internal_error", started=True))
    # canary org
    ids.append(seed_meeting(engine, canary_org, canary_owner, platform="google_meet",
                            source="canary"))
    ids.append(seed_meeting(engine, canary_org, canary_owner, platform="google_meet",
                            source="canary", status="failed", failure_reason="meeting_not_found"))

    _run(engine, "google_meet", T0, "success")
    _run(engine, "google_meet", T0 + timedelta(hours=1), "failure", "admission_denied")
    _run(engine, "google_meet", T0 + timedelta(hours=2), "failure", "meeting_not_found")
    _run(engine, "zoom", T0 + timedelta(hours=1), "failure", "ui_changed")
    _run(engine, "zoom", T0 + timedelta(hours=3), "success")
    return {
        "orgs": [a.org_id, b.org_id, canary_org],
        "users": [a.user_id, b.user_id, canary_owner],
        "emails": [a.email, b.email],
        "names": [canary_name],
        "meetings": ids,
        "key_id": harness.services.keyring.key_id,
    }


GOLDEN = """\
# HELP munshi_bot_joins_total Bot joins started in the last 24 hours by platform, source, outcome (success, failure, excluded, pending) and reason.
# TYPE munshi_bot_joins_total gauge
munshi_bot_joins_total{outcome="failure",platform="google_meet",reason="meeting_not_found",source="canary"} 1.0
munshi_bot_joins_total{outcome="success",platform="google_meet",reason="",source="canary"} 1.0
munshi_bot_joins_total{outcome="pending",platform="google_meet",reason="",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="success",platform="google_meet",reason="",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="failure",platform="teams",reason="blocked_by_policy",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="success",platform="teams",reason="",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="excluded",platform="zoom",reason="admission_denied",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="excluded",platform="zoom",reason="cancelled",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="excluded",platform="zoom",reason="removed_by_participant",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="failure",platform="zoom",reason="admission_timeout",source="portal_link"} 1.0
munshi_bot_joins_total{outcome="success",platform="zoom",reason="",source="portal_link"} 5.0
# HELP munshi_join_success_ratio_24h Share of counted bot joins (success or failure, D-39) that reached recording in the last 24 hours; source="all" is every source together.
# TYPE munshi_join_success_ratio_24h gauge
munshi_join_success_ratio_24h{platform="google_meet",source="all"} 0.6666666666666666
munshi_join_success_ratio_24h{platform="google_meet",source="canary"} 0.5
munshi_join_success_ratio_24h{platform="google_meet",source="portal_link"} 1.0
munshi_join_success_ratio_24h{platform="teams",source="all"} 0.5
munshi_join_success_ratio_24h{platform="teams",source="portal_link"} 0.5
munshi_join_success_ratio_24h{platform="zoom",source="all"} 0.8333333333333334
munshi_join_success_ratio_24h{platform="zoom",source="portal_link"} 0.8333333333333334
# HELP munshi_canary_consecutive_failures Canary joins that failed in a row up to the latest canary run.
# TYPE munshi_canary_consecutive_failures gauge
munshi_canary_consecutive_failures{platform="google_meet"} 2.0
munshi_canary_consecutive_failures{platform="zoom"} 0.0
# HELP munshi_canary_last_run_timestamp_seconds Start time of the latest canary run with this outcome (Unix seconds).
# TYPE munshi_canary_last_run_timestamp_seconds gauge
munshi_canary_last_run_timestamp_seconds{outcome="failure",platform="google_meet"} 1.7908524e+09
munshi_canary_last_run_timestamp_seconds{outcome="success",platform="google_meet"} 1.7908452e+09
munshi_canary_last_run_timestamp_seconds{outcome="failure",platform="zoom"} 1.7908488e+09
munshi_canary_last_run_timestamp_seconds{outcome="success",platform="zoom"} 1.790856e+09
# HELP munshi_deletions_pending Deleted meetings not yet purged from the platform and andai (pending or failed).
# TYPE munshi_deletions_pending gauge
munshi_deletions_pending 0.0
# HELP munshi_deletion_oldest_pending_seconds Seconds since the oldest deleted, not yet purged meeting was deleted (0 when none).
# TYPE munshi_deletion_oldest_pending_seconds gauge
munshi_deletion_oldest_pending_seconds 0.0
# HELP munshi_mom_pending Finished meetings with a stored transcript whose minutes are still owed.
# TYPE munshi_mom_pending gauge
munshi_mom_pending 0.0
# HELP munshi_mom_failed_total Meetings whose minutes failed after every attempt.
# TYPE munshi_mom_failed_total gauge
munshi_mom_failed_total 0.0
# HELP munshi_mom_latency_seconds Seconds from meeting end to its first minutes stored, for minutes stored in the last 24 hours.
# TYPE munshi_mom_latency_seconds histogram
munshi_mom_latency_seconds_bucket{le="30.0"} 0.0
munshi_mom_latency_seconds_bucket{le="60.0"} 0.0
munshi_mom_latency_seconds_bucket{le="120.0"} 0.0
munshi_mom_latency_seconds_bucket{le="300.0"} 0.0
munshi_mom_latency_seconds_bucket{le="600.0"} 0.0
munshi_mom_latency_seconds_bucket{le="900.0"} 0.0
munshi_mom_latency_seconds_bucket{le="1800.0"} 0.0
munshi_mom_latency_seconds_bucket{le="3600.0"} 0.0
munshi_mom_latency_seconds_bucket{le="+Inf"} 0.0
munshi_mom_latency_seconds_count 0.0
munshi_mom_latency_seconds_sum 0.0
# HELP munshi_sealed_values Sealed values per table still wrapped by each key-encryption key (by key id).
# TYPE munshi_sealed_values gauge
munshi_sealed_values{key_id="WORLD_KEY_ID",table="platform_principals"} 1.0
"""


# The families 0018 added, after the existing ones (which stay byte for byte as in GOLDEN). The
# world has no scheduled, ingested or deleted meeting and no inbound mail, so every stage is empty.
PIPELINE_GOLDEN = """\
# HELP munshi_pipeline_pending Work still owed per pipeline stage (scheduled dispatch, transcript ingest, minutes, recap mail, deletion, intake).
# TYPE munshi_pipeline_pending gauge
munshi_pipeline_pending{stage="dispatch"} 0.0
munshi_pipeline_pending{stage="ingest"} 0.0
munshi_pipeline_pending{stage="minutes"} 0.0
munshi_pipeline_pending{stage="recap"} 0.0
munshi_pipeline_pending{stage="deletion"} 0.0
munshi_pipeline_pending{stage="intake"} 0.0
# HELP munshi_pipeline_oldest_pending_seconds Seconds since the oldest pending item of the stage became due (0 when none).
# TYPE munshi_pipeline_oldest_pending_seconds gauge
munshi_pipeline_oldest_pending_seconds{stage="dispatch"} 0.0
munshi_pipeline_oldest_pending_seconds{stage="ingest"} 0.0
munshi_pipeline_oldest_pending_seconds{stage="minutes"} 0.0
munshi_pipeline_oldest_pending_seconds{stage="recap"} 0.0
munshi_pipeline_oldest_pending_seconds{stage="deletion"} 0.0
munshi_pipeline_oldest_pending_seconds{stage="intake"} 0.0
# HELP munshi_pipeline_failed_24h Items of the stage that failed for good in the last 24 hours.
# TYPE munshi_pipeline_failed_24h gauge
munshi_pipeline_failed_24h{stage="dispatch"} 0.0
munshi_pipeline_failed_24h{stage="ingest"} 0.0
munshi_pipeline_failed_24h{stage="minutes"} 0.0
munshi_pipeline_failed_24h{stage="recap"} 0.0
munshi_pipeline_failed_24h{stage="deletion"} 0.0
munshi_pipeline_failed_24h{stage="intake"} 0.0
# HELP munshi_pipeline_stage_seconds Seconds each stage took, for the items it finished in the last 24 hours (dispatch: due to recording; ingest: end to transcript; minutes: transcript to first minutes; recap: first minutes to recap sent).
# TYPE munshi_pipeline_stage_seconds histogram
munshi_pipeline_stage_seconds_bucket{le="1.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="5.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="15.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="30.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="60.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="120.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="300.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="600.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="900.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1800.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="3600.0",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="+Inf",stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_count{stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_sum{stage="dispatch"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="5.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="15.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="30.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="60.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="120.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="300.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="600.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="900.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1800.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="3600.0",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="+Inf",stage="ingest"} 0.0
munshi_pipeline_stage_seconds_count{stage="ingest"} 0.0
munshi_pipeline_stage_seconds_sum{stage="ingest"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="5.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="15.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="30.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="60.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="120.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="300.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="600.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="900.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1800.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="3600.0",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="+Inf",stage="minutes"} 0.0
munshi_pipeline_stage_seconds_count{stage="minutes"} 0.0
munshi_pipeline_stage_seconds_sum{stage="minutes"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="5.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="15.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="30.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="60.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="120.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="300.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="600.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="900.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="1800.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="3600.0",stage="recap"} 0.0
munshi_pipeline_stage_seconds_bucket{le="+Inf",stage="recap"} 0.0
munshi_pipeline_stage_seconds_count{stage="recap"} 0.0
munshi_pipeline_stage_seconds_sum{stage="recap"} 0.0
# HELP munshi_intake_messages_24h Messages the notetaker mailbox received in the last 24 hours by status, outcome and quarantine reason.
# TYPE munshi_intake_messages_24h gauge
# HELP munshi_db_size_bytes Size of the andai database on disk in bytes.
# TYPE munshi_db_size_bytes gauge
munshi_db_size_bytes DB_SIZE
"""


# The families 0019 added, after the database size. The world has no erasure request.
ERASURE_GOLDEN = """\
# HELP munshi_erasures_pending User erasure requests not yet done (pending or running).
# TYPE munshi_erasures_pending gauge
munshi_erasures_pending 0.0
# HELP munshi_erasure_oldest_pending_seconds Seconds since the oldest open user erasure request was made (0 when none).
# TYPE munshi_erasure_oldest_pending_seconds gauge
munshi_erasure_oldest_pending_seconds 0.0
# HELP munshi_erasures_failed User erasure requests that failed and need the operator.
# TYPE munshi_erasures_failed gauge
munshi_erasures_failed 0.0
"""


def _golden(world: dict) -> str:
    """The canary org's principal is the world's only sealed value, under the harness's KEK."""
    return GOLDEN.replace("WORLD_KEY_ID", world["key_id"]) + PIPELINE_GOLDEN + ERASURE_GOLDEN


def _masked(body: str) -> str:
    """`body` with the database size (it changes with every write) replaced by DB_SIZE."""
    size = _value(body, "munshi_db_size_bytes")
    assert size > 0
    return re.sub(r"^munshi_db_size_bytes \S+$", "munshi_db_size_bytes DB_SIZE", body,
                  flags=re.MULTILINE)


def _scrape(engine: Engine) -> str:
    return metrics.render(metrics.registry_for(engine, lambda: datetime.now(UTC))).decode()


def test_metrics_text_golden(fresh_app_engine: Engine) -> None:
    world = _world(fresh_app_engine)

    scraped = _scrape(fresh_app_engine)

    assert _masked(scraped) == _golden(world)
    assert scraped.startswith(GOLDEN.replace("WORLD_KEY_ID", world["key_id"]))  # unchanged


def test_metrics_carry_no_links_names_emails_or_ids(fresh_app_engine: Engine) -> None:
    world = _world(fresh_app_engine)

    body = _scrape(fresh_app_engine)

    for value in [*world["orgs"], *world["users"], *world["emails"], *world["names"],
                  *world["meetings"]]:
        assert str(value).lower() not in body.lower()
    assert "http" not in body and "@" not in body and "secret" not in body
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", body)
    labels = set(re.findall(r'(\w+)="', body))
    # le: histogram bounds; table and key_id: sealed values per KEK; stage: the pipelines
    assert labels == {"platform", "source", "outcome", "reason", "le", "table", "key_id", "stage"}
    assert labels <= core_metrics.LABEL_ALLOWLIST


def test_collector_sees_every_org_while_the_app_role_sees_no_meeting(
    fresh_app_engine: Engine,
) -> None:
    _world(fresh_app_engine)

    counts = metrics.join_counts(fresh_app_engine, datetime.now(UTC) - metrics.WINDOW)
    with transaction(fresh_app_engine) as tx:  # andai_app, no app.org_id
        direct = tx.execute(text("SELECT count(*) FROM meetings")).scalar_one()
        orgs = tx.execute(text("SELECT count(*) FROM orgs")).scalar_one()

    assert sum(c.count for c in counts) == 15  # every meeting in the window, from three orgs
    assert {c.source for c in counts} == {"portal_link", "canary"}
    assert (direct, orgs) == (0, 0)


def test_rates_follow_d39(fresh_app_engine: Engine) -> None:
    _world(fresh_app_engine)

    r = metrics.rates(metrics.join_counts(fresh_app_engine, datetime.now(UTC) - metrics.WINDOW))

    # zoom: 5 successes, 1 failure; cancelled, denied and removed are not counted.
    assert (r[("zoom", "all")].successes, r[("zoom", "all")].failures) == (5, 1)
    assert r[("google_meet", "canary")].counted == 2
    assert ("google_meet", "pending") not in r


def _delete(engine: Engine, member, meeting_id: uuid.UUID, at: datetime, state: str) -> None:
    with transaction(engine, org_id=member.org_id) as tx:
        tx.execute(
            text("UPDATE meetings SET deleted_at = :at, deletion_state = :s WHERE id = :m"),
            {"at": at, "s": state, "m": meeting_id},
        )


def test_the_deletion_backlog_counts_every_org_and_ages_from_the_oldest(
    fresh_app_engine: Engine,
) -> None:
    a, b = new_member(fresh_app_engine), new_member(fresh_app_engine)
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    m = [seed_meeting(fresh_app_engine, o.org_id, o.user_id) for o in (a, a, b, b)]
    _delete(fresh_app_engine, a, m[0], now - timedelta(minutes=3), "pending")
    _delete(fresh_app_engine, b, m[2], now - timedelta(minutes=20), "failed")
    _delete(fresh_app_engine, b, m[3], now - timedelta(seconds=30), "pending")

    body = metrics.render(metrics.registry_for(fresh_app_engine, lambda: now)).decode()

    assert "munshi_deletions_pending 3.0" in body
    assert "munshi_deletion_oldest_pending_seconds 1200.0" in body
    with transaction(fresh_app_engine) as tx:  # andai_app, no app.org_id
        assert tx.execute(text("SELECT count(*) FROM meetings")).scalar_one() == 0


def _erasure(engine: Engine, org_id, created_at: datetime, state: str) -> None:
    with transaction(engine, org_id=org_id) as tx:
        tx.execute(
            text(
                "INSERT INTO erasure_requests (org_id, audit_org_id, subject_user_id, source,"
                " state, error, created_at, finished_at)"
                " VALUES (:o, :o, :s, 'org_admin', :st, :e, :c, :f)"
            ),
            {"o": org_id, "s": uuid.uuid4(), "st": state, "c": created_at,
             "e": "purge_failed" if state == "failed" else None,
             "f": created_at if state in ("done", "failed") else None},
        )


def test_the_erasure_backlog_counts_every_org_and_ages_from_the_oldest(
    fresh_app_engine: Engine,
) -> None:
    a, b = new_member(fresh_app_engine), new_member(fresh_app_engine)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    _erasure(fresh_app_engine, a.org_id, now - timedelta(hours=2), "pending")
    _erasure(fresh_app_engine, b.org_id, now - timedelta(minutes=5), "running")
    _erasure(fresh_app_engine, b.org_id, now - timedelta(days=3), "done")
    _erasure(fresh_app_engine, a.org_id, now - timedelta(days=2), "failed")

    body = metrics.render(metrics.registry_for(fresh_app_engine, lambda: now)).decode()

    assert "munshi_erasures_pending 2.0" in body
    assert "munshi_erasure_oldest_pending_seconds 7200.0" in body
    assert "munshi_erasures_failed 1.0" in body
    for line in body.splitlines():
        if line.startswith("munshi_erasure"):
            assert "{" not in line  # no labels
    with transaction(fresh_app_engine) as tx:  # andai_app, no app.org_id
        assert tx.execute(text("SELECT count(*) FROM erasure_requests")).scalar_one() == 0


def test_join_stats_returns_aggregates_only(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        fn = conn.execute(
            text(
                "SELECT p.prosecdef, r.rolname, p.proconfig, pg_get_function_result(p.oid),"
                " has_function_privilege('public', p.oid, 'EXECUTE'),"
                " has_function_privilege('andai_app', p.oid, 'EXECUTE')"
                " FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner"
                " WHERE p.proname = 'andai_join_stats'"
            )
        ).one()
    assert fn[0] is True and fn[1] == "andai_scan"
    assert fn[2] == ["search_path=public, pg_temp"]
    assert fn[3] == "TABLE(platform text, source text, outcome text, reason text, count bigint)"
    assert (fn[4], fn[5]) == (False, True)


def test_metrics_endpoint(fresh_app_engine: Engine) -> None:
    world = _world(fresh_app_engine)
    harness = make_harness(fresh_app_engine)
    settings = WorkerSettings(ANDAI_ENV="dev")
    services = WorkerServices(
        settings=settings,
        engine=fresh_app_engine,
        meetings=Meetings(
            fresh_app_engine, harness.services.keyring, harness.services.gateway, settings,
            harness.clock,
        ),
        mailer=MemoryMailer(),
    )

    with TestClient(create_app(services, run_jobs=False)) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=")
    database, process = response.text.split("# HELP munshi_mail_sent_total ", 1)
    assert _masked(database) == _golden(world)
    # then the process's own counters: mail delivery and ASR lag
    for family in ("munshi_mail_sent_total", "munshi_asr_segment_lag_seconds",
                   "munshi_asr_segment_lag_missing_total"):
        assert f"# TYPE {family} " in "# HELP munshi_mail_sent_total " + process
    assert '_created' not in process


def test_metrics_without_a_database_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from andai_worker.settings import get_worker_settings

    monkeypatch.delenv("ANDAI_DB_URL_APP", raising=False)
    get_worker_settings.cache_clear()
    try:
        with TestClient(create_app(None, run_jobs=False)) as client:
            assert client.get("/metrics").status_code == 503
            assert client.get("/healthz").status_code == 200
    finally:
        get_worker_settings.cache_clear()


def test_canary_setup_flags_one_org_and_customer_orgs_stay_unflagged(
    fresh_app_engine: Engine, superuser_engine: Engine
) -> None:
    member = new_member(fresh_app_engine)
    harness = make_harness(fresh_app_engine)
    canary_org = canary_setup(harness, fresh_app_engine)
    canary_setup(harness, fresh_app_engine)  # a second run changes nothing

    with transaction(fresh_app_engine, org_id=member.org_id) as tx:
        assert tx.get(Org, member.org_id).is_canary is False
    with transaction(fresh_app_engine, org_id=canary_org) as tx:
        assert tx.get(Org, canary_org).is_canary is True
    with transaction(fresh_app_engine) as tx:
        flagged = tx.execute(text("SELECT andai_canary_org_id()")).scalar_one()
    assert flagged == canary_org


def _set(engine: Engine, org_id: uuid.UUID, meeting_id: uuid.UUID, **values) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with transaction(engine, org_id=org_id) as tx:
        tx.execute(
            text(f"UPDATE meetings SET {assignments} WHERE id = :m"), {"m": meeting_id, **values}
        )


def _value(text_body: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)} (\S+)$", text_body, re.MULTILINE)
    assert match, series
    return float(match.group(1))


def test_the_minutes_metrics_count_every_org_and_measure_end_to_stored(
    fresh_app_engine: Engine,
) -> None:
    engine = fresh_app_engine
    a, b = new_member(engine), new_member(engine)
    now = datetime.now(UTC)
    ended = now - timedelta(minutes=20)

    def m(member, **values):
        meeting_id = seed_meeting(engine, member.org_id, member.user_id)
        _set(engine, member.org_id, meeting_id, **values)
        return meeting_id

    m(a, transcript_state="stored")                                        # owed, never claimed
    m(b, transcript_state="stored", mom_state="pending", mom_attempts=2)   # owed, retrying
    m(a, transcript_state="pending")                                       # not owed yet
    m(b, transcript_state="stored", mom_state="failed", mom_error="llm_unavailable")
    for seconds in (20, 45, 400):                                          # stored, 3 latencies
        m(a, transcript_state="stored", mom_state="stored", ended_at=ended,
          mom_at=ended + timedelta(seconds=seconds))
    m(b, transcript_state="stored", mom_state="stored", ended_at=now - timedelta(days=3),
      mom_at=now - timedelta(days=2))                                      # out of the window
    m(a, transcript_state="stored", deleted_at=now, deletion_state="pending")  # hidden

    scraped = _scrape(engine)

    assert _value(scraped, "munshi_mom_pending") == 2
    assert _value(scraped, "munshi_mom_failed_total") == 1
    assert _value(scraped, 'munshi_mom_latency_seconds_bucket{le="30.0"}') == 1
    assert _value(scraped, 'munshi_mom_latency_seconds_bucket{le="60.0"}') == 2
    assert _value(scraped, 'munshi_mom_latency_seconds_bucket{le="300.0"}') == 2
    assert _value(scraped, 'munshi_mom_latency_seconds_bucket{le="600.0"}') == 3
    assert _value(scraped, 'munshi_mom_latency_seconds_bucket{le="+Inf"}') == 3
    assert _value(scraped, "munshi_mom_latency_seconds_count") == 3
    assert abs(_value(scraped, "munshi_mom_latency_seconds_sum") - 465) < 0.01
    for leaked in (a.org_id, b.org_id, a.email, b.email):
        assert str(leaked) not in scraped


def _sealed_series(body: str) -> dict[tuple[str, str], float]:
    found = re.findall(
        r'^munshi_sealed_values\{key_id="([^"]+)",table="([^"]+)"\} (\S+)$', body, re.MULTILINE
    )
    return {(table, key_id): float(value) for key_id, table, value in found}


def test_sealed_values_count_each_table_by_key_id_and_are_cached_for_300_s(
    fresh_app_engine: Engine,
) -> None:
    from andai_core.auth import mfa
    from andai_core.crypto import KEY_BYTES, Keyring

    engine = fresh_app_engine
    harness = make_harness(engine)
    old, new = os.urandom(KEY_BYTES), os.urandom(KEY_BYTES)
    members = [new_member(engine) for _ in range(3)]
    for i, member in enumerate(members):
        harness.services.keyring = Keyring(old if i < 2 else new)
        give_principal(harness, engine, member.org_id)
        with transaction(engine) as tx:
            mfa.start(tx, harness.services.keyring, user_id=member.user_id,
                      email=member.email, now=T0)
    now = [T0]
    registry = metrics.registry_for(engine, lambda: now[0])
    old_id, new_id = Keyring(old).key_id, Keyring(new).key_id

    first = _sealed_series(metrics.render(registry).decode())
    with transaction(engine) as tx:  # the third user's TOTP enrolment goes away
        tx.execute(text("DELETE FROM user_totp WHERE user_id = :u"), {"u": members[2].user_id})
    now[0] = T0 + timedelta(seconds=299)
    cached = _sealed_series(metrics.render(registry).decode())
    now[0] = T0 + timedelta(seconds=300)
    fresh = _sealed_series(metrics.render(registry).decode())

    expected = {
        ("platform_principals", old_id): 2.0, ("platform_principals", new_id): 1.0,
        ("user_totp", old_id): 2.0, ("user_totp", new_id): 1.0,
    }
    assert first == expected and cached == expected
    # a key that wraps nothing in a table has no series there
    assert fresh == {k: v for k, v in expected.items() if k != ("user_totp", new_id)}


# ── pipelines (0018) ──


def _superuser(postgres, engine: Engine) -> Engine:
    """A superuser engine on `engine`'s database (inbound mail is written by andai-intake's
    functions; tests put rows of every state straight in)."""
    return create_engine(postgres.url("postgres", postgres.superuser_password, engine.url.database))


def _inbound(su: Engine, *, status: str, received_at: datetime, org_id=None, outcome=None,
             reason=None, attempts: int = 1) -> None:
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inbound_mail (raw_sha256, mail_from, rcpt_to, status, outcome,"
                " quarantine_reason, attempts, received_at, lease_until, org_id, sender,"
                " subject_trim)"
                " VALUES (:sha, :sender, ARRAY['notetaker@munshi.test'], :status, :outcome,"
                " :reason, :attempts, :at, :lease, :org, :sender, :subject)"
            ),
            {
                "sha": uuid.uuid4().hex * 2, "sender": f"{uuid.uuid4().hex[:8]}@sender.test",
                "status": status, "outcome": outcome, "reason": reason, "attempts": attempts,
                "at": received_at, "org": org_id, "subject": "Synthetic standup invite",
                "lease": received_at + timedelta(minutes=5) if status == "processing" else None,
            },
        )


def _pipeline_world(engine: Engine, su: Engine, now: datetime) -> None:
    """Three orgs with work in every stage, and inbound mail with and without an org."""
    a, b, c = new_member(engine), new_member(engine), new_member(engine)

    def m(member, status="done", failure_reason=None, **values):
        meeting_id = seed_meeting(
            engine, member.org_id, member.user_id, status=status, failure_reason=failure_reason,
            started=status in ("recording", "done"),
        )
        if values:
            _set(engine, member.org_id, meeting_id, **values)
        return meeting_id

    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731
    # dispatch: lead 120 s, so due 120 s before the start
    m(a, "scheduled", scheduled_start=now + timedelta(seconds=60))         # due 60 s ago
    m(b, "scheduled", scheduled_start=now + timedelta(seconds=30))         # due 90 s ago
    m(b, "scheduled", scheduled_start=now + timedelta(minutes=10))         # not due yet
    m(c, "scheduled", scheduled_start=ago(minutes=5), skipped_at=ago(hours=1))   # skipped
    m(a, "failed", scheduled_start=ago(hours=2), ended_at=ago(hours=1),
      failure_reason="internal_error")                                     # failed, no bot
    m(c, "failed", scheduled_start=ago(hours=31), ended_at=ago(hours=30),
      failure_reason="internal_error")                                     # failed, too long ago
    m(b, "failed", scheduled_start=ago(hours=2), ended_at=ago(hours=1),
      failure_reason="admission_timeout", platform_meeting_id=7,
      transcript_state="stored", mom_state="skipped")                      # a bot was sent: a join
    # dispatch durations: due → recording
    m(c, "done", scheduled_start=ago(minutes=30), started_at=ago(minutes=30) + timedelta(seconds=40))
    # ingest
    m(a, platform_meeting_id=11, ended_at=ago(minutes=10))                 # owed, never claimed
    m(b, platform_meeting_id=12, ended_at=ago(minutes=40), transcript_state="pending")
    m(c, platform_meeting_id=13, ended_at=ago(hours=2), transcript_state="failed")
    m(c, platform_meeting_id=14, ended_at=ago(minutes=50), transcript_state="stored",
      transcript_at=ago(minutes=50) + timedelta(seconds=20),
      mom_state="skipped")                                                 # ingest took 20 s
    # minutes
    m(a, platform_meeting_id=21, transcript_state="stored", transcript_at=ago(minutes=5))
    m(b, platform_meeting_id=22, transcript_state="stored", transcript_at=ago(minutes=15),
      mom_state="pending", mom_attempts=1)
    m(c, platform_meeting_id=23, transcript_state="stored", ended_at=ago(hours=3),
      mom_state="failed", mom_error="llm_unavailable")
    m(a, platform_meeting_id=24, transcript_state="stored", ended_at=ago(hours=1),
      transcript_at=ago(minutes=59), mom_state="stored", mom_at=ago(minutes=57),
      recap_state="sent", recap_at=ago(minutes=56))                        # 120 s, then 60 s
    # recap
    m(a, platform_meeting_id=31, transcript_state="stored", transcript_at=ago(minutes=4),
      mom_state="stored", mom_at=ago(minutes=2), recap_state="pending")
    m(b, platform_meeting_id=32, transcript_state="stored", ended_at=ago(hours=1),
      mom_state="stored", mom_at=ago(minutes=50), recap_state="failed")
    # deletion
    m(c, deleted_at=ago(minutes=7), deletion_state="pending")
    m(a, deleted_at=ago(hours=3), deletion_state="failed")

    # intake: two open (one org-less), one given up, one quarantined at once, one done
    _inbound(su, status="received", received_at=ago(seconds=50))
    _inbound(su, status="processing", received_at=ago(seconds=20), attempts=1)
    _inbound(su, status="quarantined", outcome="quarantined", reason="parse_error",
             attempts=5, received_at=ago(hours=1))
    _inbound(su, status="quarantined", outcome="quarantined", reason="unknown_user",
             received_at=ago(hours=2))
    _inbound(su, status="done", outcome="scheduled", org_id=a.org_id, received_at=ago(hours=3))
    _inbound(su, status="done", outcome="scheduled", org_id=b.org_id, received_at=ago(hours=30))


def test_pipeline_stats_count_every_org_and_age_from_the_oldest(
    fresh_app_engine: Engine, postgres
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    su = _superuser(postgres, fresh_app_engine)
    try:
        _pipeline_world(fresh_app_engine, su, now)
    finally:
        su.dispose()

    stats = {s.stage: s for s in metrics.pipeline_stats(fresh_app_engine, now, 120)}

    assert list(stats) == list(metrics.PIPELINE_STAGES)
    got = {stage: (s.pending, round(s.oldest_s), s.failed_24h) for stage, s in stats.items()}
    assert got == {
        "dispatch": (2, 90, 1),
        "ingest": (2, 2400, 1),
        "minutes": (2, 900, 1),
        "recap": (1, 120, 1),
        "deletion": (1, 420, 1),
        "intake": (2, 50, 1),
    }
    with transaction(fresh_app_engine) as tx:  # andai_app, no app.org_id
        assert tx.execute(text("SELECT count(*) FROM meetings")).scalar_one() == 0


def test_pipeline_durations_and_intake_counts(fresh_app_engine: Engine, postgres) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    su = _superuser(postgres, fresh_app_engine)
    try:
        _pipeline_world(fresh_app_engine, su, now)
    finally:
        su.dispose()

    durations = metrics.pipeline_durations(fresh_app_engine, now - metrics.WINDOW, 120)
    intake = metrics.intake_stats(fresh_app_engine, now - metrics.WINDOW)

    assert {stage: sorted(round(v) for v in values) for stage, values in durations.items()} == {
        "dispatch": [160],  # due 120 s before the start, recording 40 s after it
        "ingest": [20, 60],
        "minutes": [120, 120],
        "recap": [60],
    }
    assert {(c.status, c.outcome, c.reason): c.count for c in intake} == {
        ("received", "", ""): 1,
        ("processing", "", ""): 1,
        ("quarantined", "quarantined", "parse_error"): 1,
        ("quarantined", "quarantined", "unknown_user"): 1,
        ("done", "scheduled", ""): 1,  # the one received 30 h ago is out of the window
    }


def test_pipeline_metrics_text(fresh_app_engine: Engine, postgres) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    su = _superuser(postgres, fresh_app_engine)
    try:
        _pipeline_world(fresh_app_engine, su, now)
    finally:
        su.dispose()

    body = metrics.render(metrics.registry_for(fresh_app_engine, lambda: now, 120)).decode()

    assert _value(body, 'munshi_pipeline_pending{stage="dispatch"}') == 2
    assert _value(body, 'munshi_pipeline_oldest_pending_seconds{stage="ingest"}') == 2400
    assert _value(body, 'munshi_pipeline_failed_24h{stage="intake"}') == 1
    assert _value(body, 'munshi_pipeline_stage_seconds_bucket{le="30.0",stage="ingest"}') == 1
    assert _value(body, 'munshi_pipeline_stage_seconds_bucket{le="60.0",stage="ingest"}') == 2
    assert _value(body, 'munshi_pipeline_stage_seconds_count{stage="minutes"}') == 2
    assert _value(body, 'munshi_pipeline_stage_seconds_sum{stage="dispatch"}') == 160
    assert _value(
        body,
        'munshi_intake_messages_24h{outcome="quarantined",reason="parse_error",'
        'status="quarantined"}',
    ) == 1
    assert _value(body, 'munshi_intake_messages_24h{outcome="",reason="",status="received"}') == 1
    assert _value(body, "munshi_db_size_bytes") > 1_000_000
    assert "@" not in body and "Synthetic" not in body and "sender.test" not in body


def test_pipeline_functions_return_aggregates_only(superuser_engine: Engine) -> None:
    with superuser_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.proname, pg_get_function_result(p.oid) FROM pg_proc p"
                " WHERE p.proname IN ('andai_pipeline_stats', 'andai_pipeline_durations',"
                " 'andai_intake_stats')"
            )
        ).all()
    assert dict(rows) == {
        "andai_pipeline_stats": (
            "TABLE(stage text, pending bigint, oldest_s double precision, failed_24h bigint)"
        ),
        "andai_pipeline_durations": "TABLE(stage text, seconds double precision)",
        "andai_intake_stats": "TABLE(status text, outcome text, reason text, n bigint)",
    }


def test_intake_failure_threshold_matches_the_processor() -> None:
    import importlib

    from andai_intake import processor

    migration = importlib.import_module("migrations.versions.0018_pipeline_stats")
    assert migration.INTAKE_MAX_ATTEMPTS == processor.MAX_ATTEMPTS
