"""The minutes job (`meetings/minutes_job.py`) with the claim it runs under (`andai_claim_mom`,
migration 0010) and the fake LLM (`andai_core.testing.fake_llm`) in-process. Every test runs on a
brand-new database, so the claim sees only this test's meetings."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, text

from andai_core.db.session import transaction
from andai_core.llm.client import LlmClient, LlmConfig
from andai_core.meetings.minutes_job import MAX_ATTEMPTS, run_one
from andai_core.minutes.owners import UNASSIGNED
from andai_core.testing import fake_llm
from conftest import new_member, seed_meeting, seed_segments

LINES = [
    ("Ravi Kumar", "कल की मीटिंग में deadline तय हुई"),
    ("Asha", "क्या हम Friday तक ship कर सकते हैं"),
    ("Ravi Kumar", "ठीक है, मैं report कल भेज दूँगा"),
    ("Asha", "बजट अभी pending है"),
]
ATTENDEES = [{"name": "Ravi Kumar", "source": "invite"}, {"name": "Asha", "source": "speaker"}]
STARTED = datetime(2026, 11, 2, 5, 0, tzinfo=UTC)  # Monday 10:30 in Asia/Kolkata
ENDED = STARTED + timedelta(minutes=30)
CLAIM = "SELECT org_id, meeting_id, lease FROM andai_claim_mom(:n)"


class Gate(httpx.AsyncBaseTransport):
    """The fake LLM behind a gate the test opens, counting calls."""

    def __init__(self, inner: httpx.AsyncBaseTransport, gate: asyncio.Event | None = None):
        self.inner = inner
        self.gate = gate
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return await self.inner.handle_async_request(request)


def fake_route() -> httpx.AsyncBaseTransport:
    return httpx.ASGITransport(app=fake_llm.app)


def client(transport: httpx.AsyncBaseTransport | None = None) -> LlmClient:
    config = LlmConfig(base_url="http://fake-llm.test", api_key=None, model="fake-model")
    return LlmClient(config, transport=transport or fake_route())


def unavailable() -> LlmClient:
    return client(httpx.MockTransport(lambda request: httpx.Response(503)))


@dataclass
class World:
    engine: Engine
    org_id: uuid.UUID
    user_id: uuid.UUID

    def meeting(
        self, *, language: str = "hinglish", lines=LINES, status: str = "done", **values
    ) -> uuid.UUID:
        meeting_id = seed_meeting(
            self.engine, self.org_id, self.user_id, None, status=status,
            started_at=STARTED, ended_at=ENDED,
        )  # fmt: skip
        self.update(
            meeting_id,
            attendees=json.dumps(ATTENDEES),
            settings_snapshot=json.dumps({"language": language}),
        )
        if lines:
            seed_segments(self.engine, self.org_id, meeting_id, list(lines))
        else:
            self.update(meeting_id, transcript_state="stored")
        if values:
            self.update(meeting_id, **values)
        return meeting_id

    def update(self, meeting_id: uuid.UUID, **values: object) -> None:
        casts = {"attendees": "jsonb", "settings_snapshot": "jsonb", "mom_request": "jsonb"}
        assignments = ", ".join(
            f"{c} = CAST(:{c} AS {casts[c]})" if c in casts else f"{c} = :{c}" for c in values
        )
        with transaction(self.engine, org_id=self.org_id) as tx:
            tx.execute(
                text(f"UPDATE meetings SET {assignments} WHERE id = :m"),
                {"m": meeting_id, **values},
            )

    def claim(self, n: int = 100) -> dict[uuid.UUID, uuid.UUID]:
        with transaction(self.engine) as tx:
            return {row.meeting_id: row.lease for row in tx.execute(text(CLAIM), {"n": n})}

    def row(self, meeting_id: uuid.UUID) -> dict:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return dict(
                tx.execute(
                    text(
                        "SELECT mom_state, mom_attempts, mom_next_at, mom_at, mom_error,"
                        " mom_request, mom_lease, mom_lease_until, recap_state FROM meetings"
                        " WHERE id = :m"
                    ),
                    {"m": meeting_id},
                ).mappings().one()
            )

    def versions(self, meeting_id: uuid.UUID) -> list[dict]:
        with transaction(self.engine, org_id=self.org_id) as tx:
            return [
                {"version": r.version, "language": r.output_language, "body": r.body}
                for r in tx.execute(
                    text(
                        "SELECT version, output_language, body FROM mom_documents"
                        " WHERE meeting_id = :m ORDER BY version"
                    ),
                    {"m": meeting_id},
                )
            ]

    def run(self, meeting_id: uuid.UUID, lease: uuid.UUID, llm: LlmClient | None = None, **kw):
        return asyncio.run(
            run_one(self.engine, llm or client(), self.org_id, meeting_id, lease, **kw)
        )


@pytest.fixture
def world(fresh_app_engine: Engine) -> World:
    member = new_member(fresh_app_engine)
    return World(fresh_app_engine, member.org_id, member.user_id)


# ─── the fake LLM's draft passes the writer ───────────────────────────────────────────────────────


@pytest.mark.parametrize("language", ["en", "hi", "hinglish"])
def test_a_stored_transcript_gets_exactly_one_version(world: World, language: str) -> None:
    meeting_id = world.meeting(language=language)
    [lease] = world.claim().values()

    result = world.run(meeting_id, lease)

    assert (result.state, result.version, result.first) == ("stored", 1, True)
    row = world.row(meeting_id)
    assert row["mom_state"] == "stored" and row["mom_at"] is not None
    assert (row["mom_lease"], row["mom_lease_until"], row["mom_error"]) == (None, None, None)
    assert row["recap_state"] == "pending"
    [stored] = world.versions(meeting_id)
    body = stored["body"]
    assert stored["language"] == language == body["outputLanguage"]
    assert body["templateId"] == "standard" and body["model"] == "fake-model"
    owners = [a["owner"] for a in body["actionItems"]]
    assert owners == ["Ravi Kumar", UNASSIGNED]
    assert body["actionItems"][0]["due"] == (date(2026, 11, 3)).isoformat()  # "kal" after Monday
    assert body["decisions"] and body["topics"] and body["risks"]
    devanagari = any("ऀ" <= c <= "ॿ" for c in body["summary"])
    assert devanagari is (language == "hi")
    assert world.claim() == {}


def test_two_concurrent_claimers_generate_once(world: World) -> None:
    meeting_id = world.meeting()

    async def race() -> list[dict]:
        async def claim() -> dict:
            return await asyncio.to_thread(world.claim)

        return await asyncio.gather(claim(), claim())

    first, second = asyncio.run(race())
    held = first or second
    assert len(first) + len(second) == 1
    transport = Gate(fake_route())

    assert world.run(meeting_id, held[meeting_id], client(transport)).state == "stored"
    assert transport.calls == 1
    assert len(world.versions(meeting_id)) == 1


def test_a_crashed_runs_lease_expires_and_the_meeting_is_completed_once(world: World) -> None:
    meeting_id = world.meeting()
    crashed = world.claim()[meeting_id]  # the worker died here: nothing ran
    assert world.claim() == {}  # the lease holds

    world.update(meeting_id, mom_lease_until=datetime.now(UTC) - timedelta(seconds=1))
    retaken = world.claim()[meeting_id]

    assert retaken != crashed
    assert world.run(meeting_id, crashed).state == "lost"  # the dead run's token writes nothing
    assert world.run(meeting_id, retaken).state == "stored"
    assert len(world.versions(meeting_id)) == 1


def test_a_slow_run_whose_lease_was_taken_over_writes_nothing(world: World) -> None:
    meeting_id = world.meeting()
    slow_lease = world.claim()[meeting_id]

    async def scenario():
        gate = asyncio.Event()
        slow = Gate(fake_route(), gate)
        slow_run = asyncio.create_task(
            run_one(world.engine, client(slow), world.org_id, meeting_id, slow_lease)
        )
        while slow.calls == 0:
            await asyncio.sleep(0.01)
        # The slow run outlived its lease; another worker takes the meeting over and finishes.
        await asyncio.to_thread(
            world.update, meeting_id, mom_lease_until=datetime.now(UTC) - timedelta(seconds=1)
        )
        fast_lease = (await asyncio.to_thread(world.claim))[meeting_id]
        fast = await run_one(world.engine, client(), world.org_id, meeting_id, fast_lease)
        gate.set()
        return fast, await slow_run

    fast, slow = asyncio.run(scenario())

    assert (fast.state, slow.state) == ("stored", "lost")
    assert [v["version"] for v in world.versions(meeting_id)] == [1]
    assert world.row(meeting_id)["mom_state"] == "stored"


def test_a_regenerate_written_during_a_run_survives_it_and_runs_next(world: World) -> None:
    meeting_id = world.meeting(language="en")
    lease = world.claim()[meeting_id]
    request = {"template": "client_call", "language": "hi"}

    async def scenario():
        gate = asyncio.Event()
        slow = Gate(fake_route(), gate)
        run = asyncio.create_task(
            run_one(world.engine, client(slow), world.org_id, meeting_id, lease)
        )
        while slow.calls == 0:
            await asyncio.sleep(0.01)
        await asyncio.to_thread(
            world.update, meeting_id, mom_state="pending", mom_request=json.dumps(request)
        )
        reclaimed = await asyncio.to_thread(world.claim)  # the lease still holds
        gate.set()
        return reclaimed, await run

    reclaimed, first = asyncio.run(scenario())

    assert reclaimed == {}
    assert (first.state, first.version) == ("pending", 1)
    row = world.row(meeting_id)
    assert (row["mom_state"], row["mom_request"], row["mom_lease"]) == ("pending", request, None)
    assert row["recap_state"] == "pending"  # the first version's recap

    second = world.run(meeting_id, world.claim()[meeting_id])

    assert (second.state, second.version, second.first) == ("stored", 2, False)
    versions = world.versions(meeting_id)
    assert [(v["version"], v["language"]) for v in versions] == [(1, "en"), (2, "hi")]
    assert versions[1]["body"]["templateId"] == "client_call"
    row = world.row(meeting_id)
    assert (row["mom_state"], row["mom_request"]) == ("stored", None)


def test_an_empty_transcript_is_skipped_without_a_model_call(world: World) -> None:
    meeting_id = world.meeting(lines=())
    transport = Gate(fake_route())

    result = world.run(meeting_id, world.claim()[meeting_id], client(transport))

    assert result.state == "skipped" and transport.calls == 0
    row = world.row(meeting_id)
    assert (row["mom_state"], row["recap_state"], row["mom_lease"]) == ("skipped", "skipped", None)
    assert world.versions(meeting_id) == []
    assert world.claim() == {}


def test_an_unavailable_model_backs_off_doubling_then_fails_with_its_code(world: World) -> None:
    meeting_id = world.meeting()
    now = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)

    first = world.run(meeting_id, world.claim()[meeting_id], unavailable(), clock=lambda: now)

    assert (first.state, first.error) == ("pending", "llm_unavailable")
    row = world.row(meeting_id)
    assert (row["mom_attempts"], row["mom_error"], row["mom_lease"]) == (1, "llm_unavailable", None)
    assert row["mom_next_at"] == now + timedelta(seconds=30)
    world.update(meeting_id, mom_next_at=datetime.now(UTC) - timedelta(seconds=1))
    world.run(meeting_id, world.claim()[meeting_id], unavailable(), clock=lambda: now)
    assert world.row(meeting_id)["mom_next_at"] == now + timedelta(seconds=60)
    world.update(meeting_id, mom_attempts=6, mom_next_at=datetime.now(UTC) - timedelta(seconds=1))
    world.run(meeting_id, world.claim()[meeting_id], unavailable(), clock=lambda: now)
    assert world.row(meeting_id)["mom_next_at"] == now + timedelta(minutes=30)  # the cap

    world.update(meeting_id, mom_next_at=datetime.now(UTC) - timedelta(seconds=1))
    last = world.run(meeting_id, world.claim()[meeting_id], unavailable(), clock=lambda: now)

    assert (last.state, last.error) == ("failed", "llm_unavailable")
    row = world.row(meeting_id)
    assert (row["mom_state"], row["mom_attempts"], row["mom_next_at"]) == (
        "failed", MAX_ATTEMPTS, None
    )
    assert row["recap_state"] == "none"
    assert world.versions(meeting_id) == [] and world.claim() == {}


def test_an_unknown_template_in_a_request_fails_at_once(world: World) -> None:
    meeting_id = world.meeting(mom_request=json.dumps({"template": "nope", "language": "en"}))

    result = world.run(meeting_id, world.claim()[meeting_id])

    assert (result.state, result.error) == ("failed", "mom_invalid_request")
    assert world.versions(meeting_id) == []


@pytest.mark.parametrize(
    "values",
    [
        {"deleted_at": datetime.now(UTC), "deletion_state": "pending"},
        {"deleted_at": datetime.now(UTC), "deletion_state": "failed"},
        {"source": "canary"},
        {"transcript_state": "pending"},
        {"transcript_state": "none"},
        {"transcript_state": "failed"},
        {"status": "recording"},
    ],
    ids=["deleted", "deletion-failed", "canary", "transcript-pending", "transcript-none",
         "transcript-failed", "still-recording"],
)
def test_meetings_that_owe_no_minutes_are_never_claimed(world: World, values: dict) -> None:
    meeting_id = world.meeting(**values)

    assert meeting_id not in world.claim()
    assert world.row(meeting_id)["mom_state"] == "none"


def test_a_meeting_deleted_during_the_run_gets_no_version(world: World) -> None:
    meeting_id = world.meeting()
    lease = world.claim()[meeting_id]

    async def scenario():
        gate = asyncio.Event()
        slow = Gate(fake_route(), gate)
        run = asyncio.create_task(
            run_one(world.engine, client(slow), world.org_id, meeting_id, lease)
        )
        while slow.calls == 0:
            await asyncio.sleep(0.01)
        await asyncio.to_thread(
            world.update, meeting_id, deleted_at=datetime.now(UTC), deletion_state="pending"
        )
        gate.set()
        return await run

    assert asyncio.run(scenario()).state == "lost"
    assert world.versions(meeting_id) == []


def test_a_run_without_the_current_lease_writes_nothing(world: World) -> None:
    meeting_id = world.meeting()
    world.claim()

    assert world.run(meeting_id, uuid.uuid4()).state == "lost"
    assert world.versions(meeting_id) == []
    assert world.row(meeting_id)["mom_attempts"] == 0


def test_the_meeting_view_shows_the_mom_state_and_error_code(world: World) -> None:
    from andai_core.db.models import Meeting
    from andai_core.meetings.access import Actor
    from andai_core.meetings.service import view

    meeting_id = world.meeting()
    world.run(meeting_id, world.claim()[meeting_id], unavailable())
    with transaction(world.engine, org_id=world.org_id, user_id=world.user_id) as tx:
        meeting = tx.get(Meeting, meeting_id)
        shown = view(tx, Actor(world.org_id, world.user_id, "owner"), meeting)

    assert (shown["mom_state"], shown["mom_error"]) == ("pending", "llm_unavailable")
