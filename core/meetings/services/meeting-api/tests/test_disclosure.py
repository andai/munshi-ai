"""The recording notice (BOT-03): ``POST /bots`` accepts an optional ``disclosure``, hands it to the
bot through invocation.v1, and the bot's reported outcome lands at ``meeting.data.disclosure``.

OFFLINE over the in-memory fakes and the shipped ``create_app`` (the same callback handler prod runs).
"""
from __future__ import annotations

import asyncio
import json

import jsonschema
import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn import build_invocation
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.lifecycle import disclosure_outcome
from meeting_api.lifecycle.receiver import conforms

SECRET = "test-admin-token"
USER = 7
HEADERS = {"x-user-id": str(USER)}
CALLBACK = "/bots/internal/callback/lifecycle"
DISCLOSURE = {
    "text": "munshi.ai is transcribing this meeting for its organiser. Remove the bot to stop it.",
    "privacy_notice_url": "https://example.com/privacy",
}


def _spawn(client: TestClient, native: str, **extra):
    return client.post(
        "/bots", headers=HEADERS,
        json={"platform": "google_meet", "native_meeting_id": native, **extra},
    )


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    client = TestClient(create_app(meeting_repo=repo, runtime=runtime))
    return repo, runtime, client


def _invocation(runtime: FakeRuntimeClient, i: int = -1) -> dict:
    return json.loads(runtime.specs[i]["env"]["BOT_CONFIG"])


# ── POST /bots → invocation.v1 ────────────────────────────────────────────────────────────────────

def test_disclosure_rides_the_invocation(stack):
    repo, runtime, client = stack
    r = _spawn(client, "abc-defg-hij", disclosure=DISCLOSURE)
    assert r.status_code == 201, r.text
    assert _invocation(runtime)["disclosure"] == DISCLOSURE


def test_without_disclosure_the_invocation_is_unchanged(stack):
    """Absent and null both mean no notice: the invocation carries no `disclosure` key at all."""
    repo, runtime, client = stack
    assert _spawn(client, "aaa-aaaa-aaa").status_code == 201
    assert _spawn(client, "bbb-bbbb-bbb", disclosure=None).status_code == 201
    assert "disclosure" not in _invocation(runtime, 0)
    assert "disclosure" not in _invocation(runtime, 1)


def test_build_invocation_validates_disclosure_against_the_contract():
    base = dict(
        meeting_id=1, platform="google_meet", meeting_url="https://meet.google.com/abc-defg-hij",
        bot_name="munshi.ai (recording)", token="t", native_meeting_id="abc-defg-hij",
        connection_id="c", redis_url="redis://r:6379",
    )
    assert build_invocation(**base, disclosure=DISCLOSURE)["disclosure"] == DISCLOSURE
    with pytest.raises(jsonschema.ValidationError):
        build_invocation(**base, disclosure={**DISCLOSURE, "text": "x" * 501})
    with pytest.raises(jsonschema.ValidationError):
        build_invocation(**base, disclosure={**DISCLOSURE, "privacy_notice_url": "ftp://x/y"})


@pytest.mark.parametrize(
    "disclosure, needle",
    [
        ({**DISCLOSURE, "text": "x" * 501}, "maximum is 500"),
        ({**DISCLOSURE, "text": ""}, "non-empty"),
        ({**DISCLOSURE, "text": "bell\x07"}, "control characters"),
        ({"text": "notice"}, "privacy_notice_url"),
        ({**DISCLOSURE, "privacy_notice_url": "javascript:alert(1)"}, "http"),
        ({**DISCLOSURE, "privacy_notice_url": "/privacy"}, "http"),
        ({**DISCLOSURE, "privacy_notice_url": "https://"}, "http"),
        ({**DISCLOSURE, "lang": "en"}, "unknown field"),
        ("notice", "object"),
    ],
)
def test_bad_disclosure_is_422_before_any_write(stack, disclosure, needle):
    repo, runtime, client = stack
    r = _spawn(client, "bad-disc-lsr", disclosure=disclosure)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]
    assert repo._meetings == {} and runtime.specs == []


def test_text_of_exactly_500_chars_and_http_url_are_accepted(stack):
    repo, runtime, client = stack
    d = {"text": "y" * 500, "privacy_notice_url": "http://intranet.example/privacy"}
    assert _spawn(client, "max-text-ok", disclosure=d).status_code == 201
    assert _invocation(runtime)["disclosure"] == d


# ── lifecycle.v1 outcome → meeting.data.disclosure ───────────────────────────────────────────────

def _spawned_session(repo: InMemoryMeetingRepo, runtime: FakeRuntimeClient) -> tuple[int, str]:
    inv = _invocation(runtime)
    return inv["meeting_id"], inv["connectionId"]


def _post(client: TestClient, body: dict):
    return client.post(CALLBACK, json=body)


def _admit(client: TestClient, sid: str) -> None:
    for status in ("joining", "active"):
        r = _post(client, {"connection_id": sid, "status": status,
                           "timestamp": "2026-10-27T10:00:00.000Z"})
        assert r.status_code == 200, r.text


def test_chat_outcome_is_stored_on_meeting_data(stack):
    repo, runtime, client = stack
    assert _spawn(client, "chat-ok-mtg", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    outcome = {"channel": "chat", "posted_at": "2026-10-27T10:00:07.250Z"}
    r = _post(client, {"connection_id": sid, "status": "active",
                       "timestamp": "2026-10-27T10:00:07.300Z", "disclosure": outcome})
    assert r.status_code == 200, r.text
    row = repo._meetings[mid]
    assert row["data"]["disclosure"] == outcome
    assert row["status"] == "active"
    # The repeated `active` is a replay for the state machine: no second admission on the trail.
    assert [t["to"] for t in row["data"]["status_transition"]] == ["joining", "active"]


def test_name_only_outcome_keeps_its_detail(stack):
    repo, runtime, client = stack
    assert _spawn(client, "name-only-m", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    outcome = {"channel": "bot_name_only", "detail": "composer_disabled"}
    assert _post(client, {"connection_id": sid, "status": "active",
                          "disclosure": outcome}).status_code == 200
    assert repo._meetings[mid]["data"]["disclosure"] == outcome


def test_first_report_wins(stack):
    repo, runtime, client = stack
    assert _spawn(client, "first-wins-", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    first = {"channel": "chat", "posted_at": "2026-10-27T10:00:07.250Z"}
    later = {"channel": "bot_name_only", "detail": "send_failed"}
    assert _post(client, {"connection_id": sid, "status": "active", "disclosure": first}).status_code == 200
    assert _post(client, {"connection_id": sid, "status": "active", "disclosure": later}).status_code == 200
    assert repo._meetings[mid]["data"]["disclosure"] == first


def test_outcome_survives_the_terminal_advance(stack):
    """The completed advance merges the FSM's own data and must not erase the stored outcome."""
    repo, runtime, client = stack
    assert _spawn(client, "terminal-ok", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    outcome = {"channel": "chat", "posted_at": "2026-10-27T10:00:07.250Z"}
    _post(client, {"connection_id": sid, "status": "active", "disclosure": outcome})
    r = _post(client, {"connection_id": sid, "status": "completed", "completion_reason": "stopped"})
    assert r.status_code == 200, r.text
    assert repo._meetings[mid]["status"] == "completed"
    assert repo._meetings[mid]["data"]["disclosure"] == outcome


def test_report_while_stopping_does_not_reopen_the_row(stack):
    """A disclosure report that lands after a user stop leaves the row `stopping`."""
    repo, runtime, client = stack
    assert _spawn(client, "stopping-mt", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    repo.set_status(mid, "stopping")
    outcome = {"channel": "chat", "posted_at": "2026-10-27T10:00:07.250Z"}
    assert _post(client, {"connection_id": sid, "status": "active", "disclosure": outcome}).status_code == 200
    assert repo._meetings[mid]["status"] == "stopping"
    assert repo._meetings[mid]["data"]["disclosure"] == outcome


def test_report_that_is_also_the_admission_is_stored(stack):
    """If the bot's own `active` was lost, the disclosure report is the admission: both land."""
    repo, runtime, client = stack
    assert _spawn(client, "late-admit-", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    assert _post(client, {"connection_id": sid, "status": "joining"}).status_code == 200
    outcome = {"channel": "bot_name_only", "detail": "chat_unavailable"}
    assert _post(client, {"connection_id": sid, "status": "active", "disclosure": outcome}).status_code == 200
    assert repo._meetings[mid]["status"] == "active"
    assert repo._meetings[mid]["data"]["disclosure"] == outcome


def test_events_without_disclosure_never_write_one(stack):
    repo, runtime, client = stack
    assert _spawn(client, "no-disclose", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    _post(client, {"connection_id": sid, "status": "completed", "completion_reason": "stopped"})
    assert "disclosure" not in repo._meetings[mid]["data"]


def test_off_contract_outcome_is_rejected_at_the_seam(stack):
    repo, runtime, client = stack
    assert _spawn(client, "bad-outcome", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)
    for bad in ({"channel": "tts"}, {"channel": "chat"}):  # unknown channel; chat without posted_at
        r = _post(client, {"connection_id": sid, "status": "active", "disclosure": bad})
        assert r.status_code == 422, r.text
    assert "disclosure" not in repo._meetings[mid]["data"]


def test_store_fault_never_fails_the_callback(stack):
    repo, runtime, client = stack
    assert _spawn(client, "store-fault", disclosure=DISCLOSURE).status_code == 201
    mid, sid = _spawned_session(repo, runtime)
    _admit(client, sid)

    async def boom(**_kw):
        raise RuntimeError("db down")

    repo.record_disclosure = boom  # type: ignore[method-assign]
    r = _post(client, {"connection_id": sid, "status": "active",
                       "disclosure": {"channel": "chat", "posted_at": "2026-10-27T10:00:07Z"}})
    assert r.status_code == 200, r.text


# ── the normaliser + the contract ────────────────────────────────────────────────────────────────

def test_disclosure_outcome_normalises_and_caps():
    assert disclosure_outcome({}) is None
    assert disclosure_outcome({"disclosure": "chat"}) is None
    assert disclosure_outcome({"disclosure": {"channel": "tts"}}) is None
    out = disclosure_outcome({"disclosure": {
        "channel": "bot_name_only", "detail": "d" * 900, "extra": 1, "posted_at": "  ",
    }})
    assert out == {"channel": "bot_name_only", "detail": "d" * 500}


def test_lifecycle_contract_requires_posted_at_for_chat():
    conforms({"connection_id": "c", "status": "active",
              "disclosure": {"channel": "chat", "posted_at": "2026-10-27T10:00:07Z"}}, "LifecycleEvent")
    conforms({"connection_id": "c", "status": "active",
              "disclosure": {"channel": "bot_name_only"}}, "LifecycleEvent")
    with pytest.raises(jsonschema.ValidationError):
        conforms({"connection_id": "c", "status": "active",
                  "disclosure": {"channel": "chat"}}, "LifecycleEvent")


async def test_fake_record_disclosure_unknown_session_is_false():
    assert await InMemoryMeetingRepo().record_disclosure(
        session_uid="nope", disclosure={"channel": "chat"}
    ) is False
