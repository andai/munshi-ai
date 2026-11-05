"""`GET /api/v1/org/intake/quarantine`: an org's owners and admins see its quarantined mail."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text

from andai_core.org.quarantine import PAGE_SIZE, check_summary
from conftest import Harness, role_org, sign_in

PATH = "/api/v1/org/intake/quarantine"
FIELDS = {"receivedAt", "sender", "subject", "reason", "senderAuth", "check"}
T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)

PASS_SUMMARY = {
    "verdict": "dmarc_pass",
    "reason": None,
    "from_domain": "a.example.test",
    "spf": {"result": "pass", "domain": "a.example.test", "scope": "mailfrom"},
    "dkim": [{"d": "a.example.test", "s": "s1", "result": "pass", "reason": None}],
    "dmarc": {
        "result": "pass",
        "record": "a.example.test",
        "policy": "reject",
        "spf_aligned": True,
        "dkim_aligned": ["a.example.test"],
    },
    "arc": {"cv": None, "sealer": None, "instances": 0, "trusted": False, "reason": None},
    "dns": {"queries": 3, "temperror": False, "cap_hit": False, "deadline_hit": False},
}
FAIL_SUMMARY = {
    **PASS_SUMMARY,
    "verdict": "fail",
    "reason": "not_aligned",
    "spf": {"result": "softfail", "domain": "spoof.example.test", "scope": "mailfrom"},
    "dkim": [],
    "dmarc": {**PASS_SUMMARY["dmarc"], "result": "fail", "spf_aligned": False, "dkim_aligned": []},
}


def insert_mail(
    superuser_engine: Engine,
    org_id: uuid.UUID | None,
    *,
    status: str = "quarantined",
    reason: str | None = "unknown_user",
    received_at: datetime = T0,
    sender: str | None = "someone@a.example.test",
    subject: str | None = "Weekly sync",
    sender_auth: str | None = "dmarc_pass",
    summary: dict | None = None,
    raw: bytes | None = None,
) -> uuid.UUID:
    """One `inbound_mail` row as the intake would leave it, written past row-level security."""
    raw = raw if raw is not None else b"Subject: x\r\n\r\nbody " + secrets.token_bytes(8)
    outcome = {"quarantined": "quarantined", "ignored": "ignored", "done": "scheduled"}.get(status)
    mail_id = uuid.uuid4()
    with superuser_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inbound_mail (id, received_at, raw, raw_sha256, mail_from, rcpt_to,"
                " peer_ip, helo, status, outcome, quarantine_reason, ignore_reason, org_id,"
                " sender, sender_auth, auth_summary, subject_trim, processed_at)"
                " VALUES (:id, :at, :raw, :sha, 'bounce@envelope.example.test',"
                " ARRAY['notetaker@munshi.test'], '203.0.113.77', 'helo.example.test', :status,"
                " :outcome, :reason, :ignore, :org, :sender, :auth, CAST(:summary AS jsonb),"
                " :subject, :processed)"
            ),
            {
                "id": mail_id,
                "at": received_at,
                "raw": raw,
                "sha": hashlib.sha256(raw).hexdigest(),
                "status": status,
                "outcome": outcome,
                "reason": reason if status == "quarantined" else None,
                "ignore": "auto_join_off" if status == "ignored" else None,
                "org": org_id,
                "sender": sender,
                "auth": sender_auth,
                "summary": json.dumps(PASS_SUMMARY if summary is None else summary),
                "subject": subject,
                "processed": None if outcome is None else received_at,
            },
        )
    return mail_id


def test_admins_and_the_owner_see_only_their_org_s_quarantined_mail(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    other = role_org(app_engine)
    org_id = org.owner.org_id
    marker = f"BODY-{secrets.token_hex(6)}"
    newer = insert_mail(
        superuser_engine,
        org_id,
        reason="sender_unverified",
        received_at=T0 + timedelta(minutes=5),
        sender="spoofed@a.example.test",
        subject="Board review",
        sender_auth="fail",
        summary=FAIL_SUMMARY,
        raw=f"Subject: Board review\r\n\r\n{marker}".encode(),
    )
    older = insert_mail(superuser_engine, org_id, reason="unknown_user", subject="Standup")
    # Not listed: other statuses of this org, another org's quarantine, unattributed mail.
    insert_mail(superuser_engine, org_id, status="done", subject="Scheduled one")
    insert_mail(superuser_engine, org_id, status="ignored", subject="Ignored one")
    insert_mail(superuser_engine, org_id, status="received", sender=None, subject=None)
    insert_mail(superuser_engine, other.owner.org_id, subject="Other org's secret")
    insert_mail(superuser_engine, None, subject="Nobody's secret", sender="x@nowhere.test")

    for member in (org.admin, org.owner):
        response = sign_in(harness, member).get(PATH)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"items", "next_cursor"}
        assert body["next_cursor"] is None
        assert [set(item) for item in body["items"]] == [FIELDS, FIELDS]
        assert body["items"] == [
            {
                "receivedAt": (T0 + timedelta(minutes=5)).isoformat(),
                "sender": "spoofed@a.example.test",
                "subject": "Board review",
                "reason": "sender_unverified",
                "senderAuth": "fail",
                "check": "Neither SPF nor DKIM passed in alignment with a.example.test"
                " (SPF softfail, DKIM none).",
            },
            {
                "receivedAt": T0.isoformat(),
                "sender": "someone@a.example.test",
                "subject": "Standup",
                "reason": "unknown_user",
                "senderAuth": "dmarc_pass",
                "check": "DMARC passed for a.example.test (SPF and DKIM aligned).",
            },
        ]
        for absent in (
            marker,
            "Other org's secret",
            "Nobody's secret",
            "Scheduled one",
            "Ignored one",
            "bounce@envelope.example.test",
            "203.0.113.77",
            "helo.example.test",
            "notetaker@munshi.test",
            str(newer),
            str(older),
        ):
            assert absent not in response.text, absent

    other_body = sign_in(harness, other.admin).get(PATH).json()
    assert [i["subject"] for i in other_body["items"]] == ["Other org's secret"]


@pytest.mark.parametrize("role", ["member", "lead"])
def test_members_and_leads_are_refused(
    harness: Harness, app_engine: Engine, superuser_engine: Engine, role: str
) -> None:
    org = role_org(app_engine)
    insert_mail(superuser_engine, org.owner.org_id, subject="Admins only")

    response = sign_in(harness, org.by_role(role)).get(PATH)

    assert response.status_code == 403
    assert "Admins only" not in response.text
    assert harness.client().get(PATH).status_code == 401


def test_pages_of_25_walk_every_row_once_newest_first(
    harness: Harness, app_engine: Engine, superuser_engine: Engine
) -> None:
    org = role_org(app_engine)
    org_id = org.owner.org_id
    # 60 rows; every three share a received time, so the id breaks ties across page edges.
    for n in range(60):
        insert_mail(
            superuser_engine,
            org_id,
            received_at=T0 + timedelta(seconds=n // 3),
            subject=f"Invite {n:02d}",
        )
    admin = sign_in(harness, org.admin)

    pages = []
    cursor = None
    while True:
        response = admin.get(PATH, params={} if cursor is None else {"cursor": cursor})
        assert response.status_code == 200, response.text
        pages.append(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if len(pages) == 1:
            # Mail that arrives meanwhile neither shifts nor repeats the next pages.
            insert_mail(superuser_engine, org_id, received_at=T0 + timedelta(hours=1))
        if cursor is None:
            break

    assert [len(page) for page in pages] == [PAGE_SIZE, PAGE_SIZE, 10]
    subjects = [item["subject"] for page in pages for item in page]
    assert len(subjects) == len(set(subjects)) == 60
    times = [item["receivedAt"] for page in pages for item in page]
    assert times == sorted(times, reverse=True)
    assert set(subjects) == {f"Invite {n:02d}" for n in range(60)}


def _cursor(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not-a-cursor",
        "x" * 300,
        _cursor({}),
        _cursor({"t": "2026-11-05T09:00:00", "i": str(uuid.uuid4())}),
        _cursor({"t": T0.isoformat(), "i": "not-a-uuid"}),
        _cursor(["a", "list"]),
    ],
    ids=["empty", "garbage", "too long", "empty object", "naive time", "bad id", "a list"],
)
def test_a_cursor_this_route_did_not_issue_is_422(
    harness: Harness, app_engine: Engine, cursor: str
) -> None:
    org = role_org(app_engine)

    response = sign_in(harness, org.admin).get(PATH, params={"cursor": cursor})

    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (PASS_SUMMARY, "DMARC passed for a.example.test (SPF and DKIM aligned)."),
        (
            {**PASS_SUMMARY, "dmarc": {**PASS_SUMMARY["dmarc"], "spf_aligned": False}},
            "DMARC passed for a.example.test (DKIM aligned).",
        ),
        (
            {
                **FAIL_SUMMARY,
                "verdict": "arc_trusted",
                "reason": None,
                "arc": {"sealer": "google.com", "trusted": True},
            },
            "DMARC did not pass for a.example.test; accepted through the ARC seal of google.com.",
        ),
        (
            {**FAIL_SUMMARY, "reason": "no_dmarc_record"},
            "a.example.test publishes no DMARC record, so the sender cannot be verified.",
        ),
        (
            {**FAIL_SUMMARY, "reason": "temperror"},
            "A DNS lookup failed or timed out while checking a.example.test.",
        ),
        (
            {
                **FAIL_SUMMARY,
                "dkim": [
                    {"d": "other.test", "result": "pass"},
                    {"d": "a.example.test", "result": "fail"},
                ],
            },
            "Neither SPF nor DKIM passed in alignment with a.example.test"
            " (SPF softfail, DKIM pass).",
        ),
        ({"verdict": "fail", "reason": "no_from"}, "The message has no From address."),
        ({"verdict": "fail", "reason": "several_from"}, "The message names more than one sender."),
        ({"verdict": "fail", "reason": "bad_from"}, "The From address could not be read."),
        (
            {
                **FAIL_SUMMARY,
                "from_domain": "evil.test <script>",
                "spf": {"result": "<b>pass</b>"},
            },
            "Neither SPF nor DKIM passed in alignment with the sender's domain"
            " (SPF unknown, DKIM none).",
        ),
        ({"verdict": "something new"}, "The sender could not be verified."),
        (None, None),
        ({}, None),
        ("not a summary", None),
    ],
    ids=[
        "dmarc pass, both aligned",
        "dmarc pass, dkim aligned",
        "arc trusted",
        "no dmarc record",
        "temperror",
        "not aligned, dkim of another domain",
        "no from",
        "several from",
        "bad from",
        "hostile values",
        "unknown verdict",
        "none",
        "empty",
        "not a dict",
    ],
)
def test_check_summary_is_one_readable_sentence(summary, expected) -> None:
    assert check_summary(summary) == expected
