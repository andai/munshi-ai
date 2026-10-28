"""andai-worker `/metrics`: join counts across orgs through `andai_join_stats`, D-39 success rates,
canary streaks, and labels that carry no personal or customer data.

Each test that counts rows uses a brand-new database (`fresh_app_engine`), so the golden text is
exact.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, text

from andai_core.db.models import CanaryRun, Org
from andai_core.db.session import transaction
from andai_core.mail import MemoryMailer
from andai_core.meetings.service import Meetings
from andai_worker import metrics
from andai_worker.main import WorkerServices, create_app
from andai_worker.settings import WorkerSettings
from .worker_support import canary_setup, make_harness, new_member, seed_meeting

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
"""


def _scrape(engine: Engine) -> str:
    return metrics.render(metrics.registry_for(engine, lambda: datetime.now(UTC))).decode()


def test_metrics_text_golden(fresh_app_engine: Engine) -> None:
    _world(fresh_app_engine)

    assert _scrape(fresh_app_engine) == GOLDEN


def test_metrics_carry_no_links_names_emails_or_ids(fresh_app_engine: Engine) -> None:
    world = _world(fresh_app_engine)

    body = _scrape(fresh_app_engine)

    for value in [*world["orgs"], *world["users"], *world["emails"], *world["names"],
                  *world["meetings"]]:
        assert str(value).lower() not in body.lower()
    assert "http" not in body and "@" not in body and "secret" not in body
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", body)
    labels = set(re.findall(r'(\w+)="', body))
    assert labels == {"platform", "source", "outcome", "reason"}


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
    _world(fresh_app_engine)
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
    assert response.text == GOLDEN


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
