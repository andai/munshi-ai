"""A duplicate ``POST /bots`` answers a machine-readable 409 naming the meeting it collided with.

``detail`` stays the same string as before (clients that read only ``detail`` are unaffected); the
body adds ``code: duplicate_meeting`` and the existing meeting's ``meeting_id``, ``platform`` and
``native_meeting_id``. OFFLINE over the in-memory fakes; the SQL adapter's path is exercised by the
compose stack test (``deploy/compose/tests/mock_scenarios_test.py``).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn import DuplicateMeeting, request_bot
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.bot_spawn.router import _duplicate_response

SECRET = "test-admin-token"
USER = 7
HEADERS = {"x-user-id": str(USER)}


@pytest.fixture
def client_repo(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", SECRET)
    repo = InMemoryMeetingRepo()
    return TestClient(create_app(meeting_repo=repo, runtime=FakeRuntimeClient())), repo


def test_duplicate_active_meeting_is_a_structured_409(client_repo):
    client, repo = client_repo
    body = {"platform": "google_meet", "native_meeting_id": "abc-defg-hij"}
    first = client.post("/bots", headers=HEADERS, json=body)
    assert first.status_code == 201, first.text
    second = client.post("/bots", headers=HEADERS, json=body)
    assert second.status_code == 409, second.text
    assert second.json() == {
        "detail": "An active meeting already exists for google_meet/abc-defg-hij",
        "code": "duplicate_meeting",
        "meeting_id": first.json()["id"],
        "platform": "google_meet",
        "native_meeting_id": "abc-defg-hij",
    }
    assert len(repo._meetings) == 1


def test_other_409s_keep_detail_only(client_repo):
    """A stopped meeting reopened with continue_meeting is a different conflict: no duplicate code."""
    client, repo = client_repo
    body = {"platform": "google_meet", "native_meeting_id": "stp-stop-stp"}
    created = client.post("/bots", headers=HEADERS, json=body)
    assert created.status_code == 201, created.text
    mid = created.json()["id"]
    repo.set_status(mid, "completed")
    repo._meetings[mid]["data"]["stop_requested"] = True
    r = client.post("/bots", headers=HEADERS, json={**body, "continue_meeting": True})
    assert r.status_code == 409, r.text
    assert set(r.json()) == {"detail"}


async def test_fake_reopen_of_a_live_row_names_it(monkeypatch):
    """The terminal-reuse check (`reopen_meeting`) raises with the row's own ids."""
    repo = InMemoryMeetingRepo()
    row = await repo.create_meeting(user_id=USER, platform="zoom", native_meeting_id="8123456789", data={})
    with pytest.raises(DuplicateMeeting) as ei:
        await repo.reopen_meeting(meeting_id=row["id"])
    e = ei.value
    assert (e.meeting_id, e.platform, e.native_meeting_id) == (row["id"], "zoom", "8123456789")
    assert str(e) == "Terminal meeting is no longer reusable"


async def test_request_bot_duplicate_carries_existing_ids(monkeypatch):
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    kw = dict(user_id=USER, platform="teams", native_meeting_id="1234567890123",
              redis_url="r", token_secret=SECRET)
    first = await request_bot(repo, runtime, **kw)
    with pytest.raises(DuplicateMeeting) as ei:
        await request_bot(repo, runtime, **kw)
    assert ei.value.meeting_id == first["id"]
    assert ei.value.platform == "teams"
    assert ei.value.native_meeting_id == "1234567890123"


def test_response_omits_ids_the_backstop_could_not_read():
    r = _duplicate_response(DuplicateMeeting("An active meeting already exists for zoom/1",
                                             platform="zoom", native_meeting_id="1"))
    assert r.status_code == 409
    import json

    assert json.loads(r.body) == {
        "detail": "An active meeting already exists for zoom/1",
        "code": "duplicate_meeting",
        "platform": "zoom",
        "native_meeting_id": "1",
    }


def test_bare_duplicate_is_still_constructible():
    """Existing raisers that pass only a message keep working."""
    e = DuplicateMeeting("dup")
    assert str(e) == "dup" and e.meeting_id is None and e.platform is None
