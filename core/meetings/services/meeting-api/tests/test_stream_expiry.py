"""Every transcript stream ``tc:meeting:{id}`` gets the ``TC_STREAM_TTL_S`` expiry when its meeting
ends, on every end path — the bot's own ``session_end``, a lifecycle completion, a user stop, a
failure (the bot's or one the runtime confirms) and the db-writer's finalize — and a later end event
never extends it: the expiry is set only on a key with none (TTL -1) or a longer one."""
from __future__ import annotations

import asyncio
import importlib
import json

import fakeredis
import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo
from meeting_api.collector.adapters import set_stream_expiry
from meeting_api.collector.db_writer import finalize_meeting
from meeting_api.collector.fakes import FakeRedisBus, InMemoryTranscriptStore
from meeting_api.collector.ingest import ingest
from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher

DAY = 86400
ENDPOINT = "/bots/internal/callback/lifecycle"


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
def check(server):
    """A synchronous view of the same Valkey, for the assertions."""
    return fakeredis.FakeRedis(server=server, decode_responses=True)


def _app(server, repo, **kw) -> TestClient:
    redis = fakeredis.aioredis.FakeRedis(server=server)
    return TestClient(create_app(meeting_repo=repo, redis=redis, **kw))


def _seed(repo: InMemoryMeetingRepo, status: str) -> dict:
    m = asyncio.run(repo.create_meeting(user_id=1, platform="google_meet", native_meeting_id="m1",
                                        data={}))
    asyncio.run(repo.create_session(meeting_id=m["id"], session_uid="sess-uid"))
    repo.set_status(m["id"], status)
    return m


def _post(client: TestClient, **event) -> None:
    r = client.post(ENDPOINT, json={"connection_id": "sess-uid", **event})
    assert r.status_code == 200, r.text


def _assert_day(check, key: str) -> None:
    ttl = check.ttl(key)
    assert DAY - 5 <= ttl <= DAY, f"{key} TTL {ttl}"


# ── the helper ───────────────────────────────────────────────────────────────────────────────────


async def test_the_helper_sets_the_expiry_on_a_stream_without_one(server, check):
    client = fakeredis.aioredis.FakeRedis(server=server)
    check.xadd("tc:meeting:4", {"type": "transcript"})
    assert check.ttl("tc:meeting:4") == -1

    assert await set_stream_expiry(client, 4) is True
    _assert_day(check, "tc:meeting:4")


async def test_the_helper_never_extends_a_shorter_expiry(server, check):
    client = fakeredis.aioredis.FakeRedis(server=server)
    check.xadd("tc:meeting:4", {"type": "transcript"})
    check.expire("tc:meeting:4", 100)

    assert await set_stream_expiry(client, 4) is False
    assert 95 <= check.ttl("tc:meeting:4") <= 100


async def test_the_helper_shortens_a_longer_expiry(server, check):
    client = fakeredis.aioredis.FakeRedis(server=server)
    check.xadd("tc:meeting:4", {"type": "transcript"})
    check.expire("tc:meeting:4", 3 * DAY)

    assert await set_stream_expiry(client, 4) is True
    _assert_day(check, "tc:meeting:4")


async def test_the_helper_leaves_a_missing_stream_missing(server, check):
    client = fakeredis.aioredis.FakeRedis(server=server)
    assert await set_stream_expiry(client, 4) is False
    assert check.exists("tc:meeting:4") == 0


async def test_a_zero_tc_stream_ttl_s_sets_nothing(server, check, monkeypatch):
    monkeypatch.setattr(importlib.import_module("meeting_api.collector.ingest"), "TC_STREAM_TTL_S", 0)
    client = fakeredis.aioredis.FakeRedis(server=server)
    check.xadd("tc:meeting:4", {"type": "transcript"})
    assert await set_stream_expiry(client, 4) is False
    assert check.ttl("tc:meeting:4") == -1


# ── each end path ───────────────────────────────────────────────────────────────────────────────


def _session_end(meeting_id: int) -> dict:
    return {"payload": json.dumps({"type": "session_end", "meeting_id": str(meeting_id),
                                   "native_meeting_id": "m1"})}


async def test_bot_session_end(server, check):
    store = InMemoryTranscriptStore()
    bus = FakeRedisBus(fakeredis.aioredis.FakeRedis(server=server))
    await ingest(store, bus, _session_end(3))
    _assert_day(check, "tc:meeting:3")


async def test_a_second_bot_session_end_keeps_the_shorter_ttl(server, check):
    store = InMemoryTranscriptStore()
    bus = FakeRedisBus(fakeredis.aioredis.FakeRedis(server=server))
    await ingest(store, bus, _session_end(3))
    check.expire("tc:meeting:3", 100)                       # time has passed since the first end

    await ingest(store, bus, _session_end(3))
    assert 95 <= check.ttl("tc:meeting:3") <= 100


def test_lifecycle_completion(server, check):
    repo = InMemoryMeetingRepo()
    m = _seed(repo, "requested")
    client = _app(server, repo)
    for status in ("joining", "active"):
        _post(client, status=status)
    assert check.exists(f"tc:meeting:{m['id']}") == 0

    _post(client, status="completed", completion_reason="stopped", exit_code=0)
    _assert_day(check, f"tc:meeting:{m['id']}")


def test_bot_reported_failure(server, check):
    repo = InMemoryMeetingRepo()
    m = _seed(repo, "requested")
    client = _app(server, repo)
    _post(client, status="joining")
    _post(client, status="failed", completion_reason="join_failure", exit_code=1)
    _assert_day(check, f"tc:meeting:{m['id']}")


def test_user_stop_of_a_live_bot(server, check):
    """DELETE /bots asks the bot to leave; its terminal event (as ``lifecycle.stop`` builds it) ends
    the meeting and sets the expiry."""
    from meeting_api.lifecycle.machine import BotStatus, MeetingRecord
    from meeting_api.lifecycle.stop import stop_event_for

    repo = InMemoryMeetingRepo()
    m = _seed(repo, "requested")
    pub = InMemoryCommandPublisher()
    client = _app(server, repo, command_publisher=pub)
    for status in ("joining", "active"):
        _post(client, status=status)
    check.xadd(f"tc:meeting:{m['id']}", {"type": "transcript"})          # the live feed
    r = client.delete("/bots/google_meet/m1", headers={"x-user-id": "1"})
    assert r.status_code == 200, r.text
    assert check.ttl(f"tc:meeting:{m['id']}") == -1                     # still in the meeting

    event = stop_event_for(MeetingRecord(connection_id="sess-uid", status=BotStatus.ACTIVE))
    _post(client, **{k: v for k, v in event.items() if k != "connection_id"})
    _assert_day(check, f"tc:meeting:{m['id']}")


def test_failure_confirmed_by_the_runtime(server, check):
    """A bot stopped in the waiting room whose workload the runtime reports destroyed: the meeting
    fails without a callback of the bot's own, and its stream still gets the expiry."""
    repo = InMemoryMeetingRepo()
    m = _seed(repo, "awaiting_admission")
    repo._meetings[m["id"]]["bot_container_id"] = "wl-1"
    client = _app(server, repo, command_publisher=InMemoryCommandPublisher())
    assert client.delete("/bots/google_meet/m1", headers={"x-user-id": "1"}).status_code == 200

    rc = client.post("/runtime/callback", json={"workloadId": "wl-1", "state": "destroyed"})
    assert rc.status_code == 200, rc.text
    assert repo._meetings[m["id"]]["status"] == "failed"
    _assert_day(check, f"tc:meeting:{m['id']}")


def test_a_later_end_event_does_not_extend_the_expiry(server, check):
    """The bot's session_end set the expiry an hour ago; the lifecycle completion that follows keeps
    the remaining hour instead of restarting the day."""
    repo = InMemoryMeetingRepo()
    m = _seed(repo, "requested")
    client = _app(server, repo)
    for status in ("joining", "active"):
        _post(client, status=status)
    key = f"tc:meeting:{m['id']}"
    check.xadd(key, {"type": "session_end"})
    check.expire(key, 3600)

    _post(client, status="completed", completion_reason="stopped", exit_code=0)
    assert 3595 <= check.ttl(key) <= 3600


async def test_db_writer_finalize(server, check):
    redis_c = fakeredis.aioredis.FakeRedis(server=server)
    store = InMemoryTranscriptStore(redis_client=redis_c)
    store.seed_meeting(user_id=1, platform="google_meet", native_meeting_id="m1", meeting_id=5)
    check.xadd("tc:meeting:5", {"type": "transcript"})

    await finalize_meeting(redis_c, store, 5)
    _assert_day(check, "tc:meeting:5")
