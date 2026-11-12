"""andai-core `GET /metrics`: served outside `/api` without a session, passed by the two-factor stage
gate by exact path, never reachable through the portal; mail delivery counted by kind and outcome
against a real relay (Mailpit) and a refusing one; the ASR lag observed at transcript ingest."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.routing import iter_route_contexts
from sqlalchemy import Engine

from andai_core import metrics
from andai_core.auth.sessions import COOKIE_NAME
from andai_core.mail import Mail, MailError, SmtpMailer
from andai_core.meetings import ingest
from conftest import (
    Mailpit,
    make_harness,
    make_settings,
    mailpit_messages,
    new_member,
    sign_in,
    staged_session,
)

PORTAL_PROXY = Path(__file__).resolve().parents[3] / "portal" / "app" / "api" / "[...path]" / "route.ts"
USERS = "/api/v1/org/users"


def _sample(body: str, series: str) -> float:
    match = re.search(rf"^{re.escape(series)} (\S+)$", body, re.MULTILINE)
    assert match, series
    return float(match.group(1))


def _mail(body: str, kind: str, outcome: str) -> float:
    return _sample(body, f'munshi_mail_sent_total{{kind="{kind}",outcome="{outcome}"}}')


# ── endpoint ──


def test_metrics_endpoint_answers_without_a_session(app_engine: Engine) -> None:
    client = make_harness(app_engine).client(origin=None)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=")
    for kind in metrics.MAIL_KINDS:
        for outcome in metrics.MAIL_OUTCOMES:
            assert _mail(response.text, kind, outcome) >= 0
    assert "_created" not in response.text
    assert "set-cookie" not in response.headers


def test_metrics_endpoint_is_outside_api_and_only_at_its_exact_path(app_engine: Engine) -> None:
    client = make_harness(app_engine).client(origin=None)
    paths = {
        (method, context.path)
        for context in iter_route_contexts(client.app.routes)
        for method in context.methods or ()
    }

    assert ("GET", "/metrics") in paths
    assert not any(path.endswith("/metrics") for _, path in paths if path != "/metrics")
    for other in ("/api/metrics", "/api/v1/metrics", "/metricsx"):
        assert client.get(other).status_code == 404, other


@pytest.mark.parametrize("stage", ["mfa_pending", "enroll_required"])
def test_metrics_endpoint_passes_the_stage_gate_by_exact_path(
    app_engine: Engine, stage: str
) -> None:
    harness = make_harness(app_engine)
    member = new_member(app_engine)
    client = harness.client()
    client.cookies.set(COOKIE_NAME, staged_session(app_engine, member, stage, harness.clock))

    assert client.get("/metrics").status_code == 200
    gated = client.get("/api/v1/me")  # every other route still answers the gate
    assert gated.status_code == 401
    assert gated.json()["next_step"] in ("mfa_verify", "mfa_enroll")


def test_metrics_endpoint_leaves_the_origin_check_alone(app_engine: Engine) -> None:
    harness = make_harness(app_engine)
    foreign = harness.client(origin="https://evil.test")

    assert foreign.get("/metrics").status_code == 200  # a safe method
    assert foreign.post("/metrics").status_code == 403  # a write is refused before routing
    login = foreign.post("/api/v1/auth/login", json={"email": "a@example.test", "password": "x"})
    assert login.status_code == 403


def test_metrics_endpoint_is_never_forwarded_by_the_portal() -> None:
    proxy = PORTAL_PROXY.read_text()

    # The portal forwards /api/* only, to andai-core's /api/..., and refuses dot segments that
    # could climb out of /api once the URL is normalised.
    assert "const target = `${coreUrl()}/api/${path.map(encodeURIComponent).join(\"/\")}" in proxy
    assert 'segment === "." || segment === ".."' in proxy


# ── mail ──


def _smtp_settings(mailpit: Mailpit, **extra: str):
    return make_settings(
        SMTP_HOST=mailpit.smtp_host,
        SMTP_PORT=str(mailpit.smtp_port),
        SMTP_FROM="munshi.ai <no-reply@munshi.invalid>",
        **extra,
    )


def test_mail_sent_counts_an_invite_delivered_to_mailpit(
    mailpit: Mailpit, app_engine: Engine
) -> None:
    harness = make_harness(
        app_engine, SMTP_HOST=mailpit.smtp_host, SMTP_PORT=str(mailpit.smtp_port)
    )
    harness.services.mailer = SmtpMailer(harness.services.settings)
    owner = sign_in(harness, new_member(app_engine))
    invitee = f"{uuid.uuid4().hex}@example.test"
    before = owner.get("/metrics").text

    answer = owner.post(USERS, json={"email": invitee, "role": "member"})

    assert answer.status_code == 201, answer.text
    assert len(mailpit_messages(mailpit.api_url, invitee)) == 1
    after = owner.get("/metrics").text
    assert _mail(after, "invite", "sent") == _mail(before, "invite", "sent") + 1
    assert _mail(after, "invite", "failed") == _mail(before, "invite", "failed")
    assert invitee not in after and "example.test" not in after


def test_mail_sent_counts_a_refused_invite_as_failed(mailpit: Mailpit, app_engine: Engine) -> None:
    harness = make_harness(app_engine, SMTP_HOST="127.0.0.1", SMTP_PORT="1")
    harness.services.mailer = SmtpMailer(harness.services.settings)
    owner = sign_in(harness, new_member(app_engine))
    before = owner.get("/metrics").text

    answer = owner.post(USERS, json={"email": f"{uuid.uuid4().hex}@example.test", "role": "member"})

    assert answer.status_code == 503
    after = owner.get("/metrics").text
    assert _mail(after, "invite", "failed") == _mail(before, "invite", "failed") + 1
    assert _mail(after, "invite", "sent") == _mail(before, "invite", "sent")


def test_mail_sent_counts_each_kind_and_refuses_unknown_kinds(mailpit: Mailpit) -> None:
    registry = metrics.service_registry()
    mailer = SmtpMailer(_smtp_settings(mailpit))
    to = f"{uuid.uuid4().hex}@example.test"

    def count(kind: str, outcome: str) -> float:
        value = registry.get_sample_value(
            "munshi_mail_sent_total", {"kind": kind, "outcome": outcome}
        )
        assert value is not None
        return value

    before = {kind: count(kind, "sent") for kind in metrics.MAIL_KINDS}
    for kind in metrics.MAIL_KINDS:
        asyncio.run(mailer.send(Mail(to=to, subject=f"kind {kind}", text="body\n", kind=kind)))
    refusing = SmtpMailer(make_settings(SMTP_HOST="127.0.0.1", SMTP_PORT="1"))
    failed = count("recap", "failed")
    with pytest.raises(MailError):
        asyncio.run(refusing.send(Mail(to=to, subject="s", text="body\n", kind="recap")))

    assert {kind: count(kind, "sent") - before[kind] for kind in metrics.MAIL_KINDS} == {
        kind: 1.0 for kind in metrics.MAIL_KINDS
    }
    assert count("recap", "failed") == failed + 1
    assert Mail(to=to, subject="s", text="b").kind == "other"
    with pytest.raises(ValueError):
        Mail(to=to, subject="s", text="b", kind=to)  # never a free-text label value


def test_mail_senders_name_their_kind() -> None:
    from andai_core.auth.invites import invite_mail
    from andai_core.auth.resets import reset_mail

    settings = make_settings()
    invite = invite_mail(settings, email="a@example.test", org_name="Org", token="t", owner=False)
    reset = reset_mail(settings, email="a@example.test", token="t")

    assert (invite.kind, reset.kind) == ("invite", "password_reset")


# ── ASR lag ──


def _asr(registry) -> tuple[float, float, float]:
    """(observed segments, sum of lags, segments without both times)."""
    return (
        registry.get_sample_value("munshi_asr_segment_lag_seconds_count") or 0.0,
        registry.get_sample_value("munshi_asr_segment_lag_seconds_sum") or 0.0,
        registry.get_sample_value("munshi_asr_segment_lag_missing_total") or 0.0,
    )


def test_asr_lag_is_observed_from_segments_with_both_times() -> None:
    registry = metrics.service_registry(metrics.ASR_LAG, metrics.ASR_LAG_MISSING)
    before = _asr(registry)
    document = {
        "start_time": "2026-11-01T10:00:00Z",
        "segments": [
            {"segment_id": "a", "text": "namaste", "start": 0.0, "end": 2.0,
             "absolute_end_time": "2026-11-01T10:00:02Z", "created_at": "2026-11-01T10:00:04.5Z"},
            {"segment_id": "b", "text": "theek hai", "start": 2.0, "end": 4.0,
             "absolute_end_time": "2026-11-01T10:00:04Z", "created_at": "2026-11-01T10:00:05Z"},
            # the same platform segment again: counted once (the last copy)
            {"segment_id": "b", "text": "theek hai", "start": 2.0, "end": 4.0,
             "absolute_end_time": "2026-11-01T10:00:04Z", "created_at": "2026-11-01T10:00:06Z"},
            # a write time before the spoken end (clock skew) counts as 0
            {"segment_id": "c", "text": "aage", "start": 4.0, "end": 5.0,
             "absolute_end_time": "2026-11-01T10:00:05Z", "created_at": "2026-11-01T10:00:04Z"},
            {"segment_id": "d", "text": "no write time", "start": 5.0, "end": 6.0,
             "absolute_end_time": "2026-11-01T10:00:06Z"},
            {"segment_id": "e", "text": "no spoken end", "start": 6.0, "end": 7.0,
             "created_at": "2026-11-01T10:00:09Z"},
            {"segment_id": "f", "text": "", "created_at": "2026-11-01T10:00:09Z"},  # no text
        ],
    }

    segments = ingest._segments(document, uuid.uuid4(), datetime(2026, 11, 1, 10, tzinfo=UTC))

    assert len(segments) == 5
    count, total, missing = (a - b for a, b in zip(_asr(registry), before, strict=True))
    assert (count, round(total, 3), missing) == (3, 2.5 + 2.0 + 0.0, 2)
