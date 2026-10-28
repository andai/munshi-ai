"""The live relay, `GET /api/v1/meetings/{id}/live`: one viewer's server-sent events, relayed from
one gateway WebSocket that andai-core holds with the org's key.

andai-core runs under uvicorn on a loopback port (the stream and the viewer's disconnect are real
HTTP); the gateway's `/ws` is a `websockets` server stub on another port that records the key and
the subscribe message and plays the frames a test pushes; the gateway's REST calls (`GET
/meetings/{id}`) go to the in-memory `FakeGateway`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
import pytest
import uvicorn
from sqlalchemy import Engine, text
from websockets.asyncio.server import ServerConnection, serve

from andai_core.auth.sessions import COOKIE_NAME
from andai_core.db.session import transaction
from andai_core.main import create_app
from andai_core.meetings import live
from andai_core.meetings.transcript import segment_id
from conftest import (
    Harness,
    RoleOrg,
    give_principal,
    make_harness,
    role_org,
    sign_in,
    zoom_link,
)

WAIT = 5.0
CLOSE = object()


# ─── the gateway's /ws, stubbed ───────────────────────────────────────────────────────────────────


@dataclass
class StubConnection:
    key: str | None
    queue: asyncio.Queue
    subscribe: dict | None = None
    subscribed: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)


class StubGateway:
    """A `websockets` server on a loopback port in its own thread: records each connection's key
    and subscribe message, answers `subscribed` (or refuses), then sends what tests push."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.connections: list[StubConnection] = []
        # (error code or None, close code): refuse the socket the way the gateway does.
        self.refuse: tuple[str | None, int] | None = None
        self.grant = True
        self.server = asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(WAIT)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    async def _start(self):
        return await serve(self._handler, "127.0.0.1", 0)

    async def _handler(self, ws: ServerConnection) -> None:
        conn = StubConnection(ws.request.headers.get("x-api-key"), asyncio.Queue())
        self.connections.append(conn)
        try:
            if self.refuse is not None:
                error, code = self.refuse
                if error is not None:
                    await ws.send(json.dumps({"type": "error", "error": error}))
                await ws.close(code)
                return
            conn.subscribe = json.loads(await ws.recv())
            meetings = conn.subscribe.get("meetings", []) if self.grant else []
            await ws.send(json.dumps({"type": "subscribed", "meetings": meetings}))
            conn.subscribed.set()
            gone = asyncio.ensure_future(ws.wait_closed())
            while True:
                item = asyncio.ensure_future(conn.queue.get())
                done, _ = await asyncio.wait({item, gone}, return_when=asyncio.FIRST_COMPLETED)
                if gone in done:
                    item.cancel()
                    return
                frame = item.result()
                if frame is CLOSE:
                    await ws.close(4503)
                    return
                await ws.send(frame if isinstance(frame, str) else json.dumps(frame))
        finally:
            conn.closed.set()

    def subscribed(self, nth: int = 1) -> StubConnection:
        """The `nth` connection (1-based) once it has subscribed."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if len(self.connections) >= nth and self.connections[nth - 1].subscribed.is_set():
                return self.connections[nth - 1]
            time.sleep(0.02)
        raise AssertionError("andai-core never subscribed")

    def push(self, conn: StubConnection, frame) -> None:
        self.loop.call_soon_threadsafe(conn.queue.put_nowait, frame)

    def stop(self) -> None:
        self.server.close()
        asyncio.run_coroutine_threadsafe(self.server.wait_closed(), self.loop).result(WAIT)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(WAIT)


# ─── andai-core over real HTTP ────────────────────────────────────────────────────────────────────


class AppServer:
    def __init__(self, harness: Harness) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        config = uvicorn.Config(
            create_app(harness.services), log_level="warning", timeout_graceful_shutdown=2
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + WAIT
        while not self.server.started:
            assert time.monotonic() < deadline, "andai-core did not start"
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(WAIT)


class Events:
    """The server-sent events of one response, read as they arrive."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.lines = response.iter_lines()
        self.comments: list[str] = []
        self.received: list[tuple[str, dict]] = []

    def next(self) -> tuple[str, dict] | None:
        """The next event, or None when the stream ended."""
        name, data = None, None
        for line in self.lines:
            if line.startswith(":"):
                self.comments.append(line)
            elif line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
            elif line == "" and name is not None:
                self.received.append((name, data))
                return name, data
        return None

    def comment(self) -> str:
        for line in self.lines:
            if line.startswith(":"):
                self.comments.append(line)
                return line
        raise AssertionError("the stream ended without a comment")


@dataclass
class World:
    harness: Harness
    engine: Engine
    org: RoleOrg
    stub: StubGateway
    app: AppServer
    meeting_id: str
    platform_id: int
    native_id: str
    key: str

    def session(self, role: str) -> str:
        client = sign_in(self.harness, self.org.by_role(role))
        return client.cookies.get(COOKIE_NAME)

    @contextmanager
    def stream(self, role: str = "lead", meeting_id: str | None = None) -> Iterator[Events]:
        headers = {"Cookie": f"{COOKIE_NAME}={self.session(role)}"}
        with httpx.Client(base_url=self.app.url, timeout=WAIT, headers=headers) as client:
            path = f"/api/v1/meetings/{meeting_id or self.meeting_id}/live"
            with client.stream("GET", path) as response:
                assert response.status_code == 200, response.read()
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["cache-control"] == "no-cache"
                assert response.headers["x-accel-buffering"] == "no"
                yield Events(response)

    def set_row(self, **values) -> None:
        assignments = ", ".join(f"{k} = :{k}" for k in values)
        with transaction(self.engine, org_id=self.org.owner.org_id) as tx:
            tx.execute(
                text(f"UPDATE meetings SET {assignments} WHERE id = :m"),
                {**values, "m": self.meeting_id},
            )


def make_world(app_engine: Engine, gateway_url: str | None = None) -> World:
    stub = StubGateway()
    harness = make_harness(app_engine, PLATFORM_GATEWAY_URL=gateway_url or stub.url)
    org = role_org(app_engine)
    key = give_principal(harness, app_engine, org.owner.org_id)
    sent = sign_in(harness, org.lead).post(
        "/api/v1/meetings/join", json={"join_url": zoom_link(), "team_id": str(org.team)}
    )
    assert sent.status_code == 201, sent.text
    platform_id = max(harness.gateway.meetings)
    native = harness.gateway.meetings[platform_id]["native_meeting_id"]
    return World(
        harness, app_engine, org, stub, AppServer(harness), sent.json()["id"], platform_id,
        native, key,
    )


@pytest.fixture
def world(app_engine: Engine) -> Iterator[World]:
    w = make_world(app_engine)
    try:
        yield w
    finally:
        w.app.stop()
        w.stub.stop()


def status_frame(platform_id: int, status: str) -> dict:
    """`bm:meeting:{id}:status`, as the platform publishes it."""
    return {
        "type": "meeting.status",
        "meeting": {"id": platform_id, "platform": "zoom", "native_id": "x"},
        "payload": {"status": status},
        "user_id": 7,
        "ts": "2026-10-28T06:00:00+00:00",
    }


def flat_status_frame(platform_id: int, status: str) -> dict:
    """The user-wide copy on `u:{user_id}:meetings`."""
    return {"type": "meeting.status", "meeting_id": platform_id, "native": "x", "status": status,
            "when": "2026-10-28T06:00:00+00:00"}


def raw_segment(n: int, *, completed: bool = True, text_: str | None = None) -> dict:
    return {
        "segment_id": f"conn-7:mock:{n}", "speaker": "Speaker A" if n % 2 == 0 else "",
        "text": text_ or f"mock utterance {n}", "start": float(n), "end": n + 0.8,
        "completed": completed, "language": "en",
    }


# ─── the relay ────────────────────────────────────────────────────────────────────────────────────


def test_status_and_transcript_reach_the_viewer_reshaped(world: World) -> None:
    gateway, pid = world.harness.gateway, world.platform_id
    sid = uuid.UUID(world.meeting_id)

    with world.stream("lead") as events:
        assert events.next() == ("status", {
            "status": "dispatching", "failure_reason": None, "end_reason": None,
            "admit_instructions": None, "at": world.harness.clock().isoformat(),
        })
        conn = world.stub.subscribed()
        assert conn.key == world.key
        assert conn.subscribe == {
            "action": "subscribe", "meetings": [{"platform": "zoom", "native_id": world.native_id}]
        }

        gateway.set(pid, status="awaiting_admission")
        world.stub.push(conn, status_frame(pid, "awaiting_admission"))
        name, data = events.next()
        assert (name, data["status"]) == ("status", "awaiting_admission")
        assert '"munshi.ai (recording)"' in data["admit_instructions"]

        # Another meeting of the org, on the principal's user-wide channel: not this viewer's.
        world.stub.push(conn, flat_status_frame(pid + 1, "active"))
        gateway.set(pid, status="active", start_time="2026-10-28T06:00:00")
        world.stub.push(conn, flat_status_frame(pid, "active"))
        assert events.next()[1]["status"] == "recording"
        world.stub.push(conn, status_frame(pid, "active"))  # same status again: nothing new

        world.stub.push(conn, {
            "type": "transcript", "meeting": {"id": pid}, "speaker": "Speaker A",
            "confirmed": [raw_segment(0)], "pending": [raw_segment(1, completed=False)],
        })
        name, first = events.next()
        assert name == "segment"
        assert first == {
            "id": segment_id(sid, "conn-7:mock:0"), "speaker": "Speaker A", "start": 0.0,
            "end": 0.8, "text": "mock utterance 0", "language": "en", "final": True,
            "absolute_start": "2026-10-28T06:00:00Z", "absolute_end": "2026-10-28T06:00:00.800000Z",
        }
        name, draft = events.next()
        assert (draft["speaker"], draft["final"]) == ("Unknown speaker", False)

        # The bot's own envelope repeats segment 0 unchanged: not sent again.
        world.stub.push(
            conn, {"type": "transcript", "meeting": {"id": pid}, "segment": raw_segment(0)}
        )
        # Another meeting's transcript: dropped.
        world.stub.push(conn, {"type": "transcript", "meeting": {"id": pid + 1},
                               "segment": raw_segment(9)})
        # Segment 1 settles, as a single-segment frame.
        world.stub.push(conn, {"type": "transcription_segment",
                               **raw_segment(1, text_="mock utterance one")})
        name, settled = events.next()
        assert (settled["id"], settled["final"], settled["text"]) == (
            draft["id"], True, "mock utterance one"
        )

        world.stub.push(conn, {"type": "transcript_retract", "meeting": {"id": pid},
                               "segment_ids": ["conn-7:mock:5"]})
        assert events.next() == ("retract", {"ids": [segment_id(sid, "conn-7:mock:5")]})

        gateway.set(pid, status="completed", completion_reason="stopped",
                    end_time="2026-10-28T06:05:00")
        world.stub.push(conn, status_frame(pid, "completed"))
        name, done = events.next()
        assert (done["status"], done["end_reason"], done["failure_reason"]) == (
            "done", "stopped", None
        )
        assert events.next() is None, "the stream ends after a terminal status"
        assert conn.closed.wait(WAIT), "the gateway socket closes with the stream"

    sent = json.dumps([d for _, d in events.received])
    for secret in (world.key, str(world.stub.port), "conn-7", world.native_id, "gateway"):
        assert secret not in sent
    assert all(
        str(pid) not in json.dumps({k: v for k, v in d.items() if k != "at"})
        for _, d in events.received
    )


def test_a_failure_carries_its_reason(world: World) -> None:
    gateway, pid = world.harness.gateway, world.platform_id

    with world.stream("lead") as events:
        events.next()
        conn = world.stub.subscribed()
        gateway.set(pid, status="failed", completion_reason="awaiting_admission_rejected",
                    failure_stage="awaiting_admission")
        world.stub.push(conn, status_frame(pid, "failed"))

        name, data = events.next()
        assert (data["status"], data["failure_reason"]) == ("failed", "admission_denied")
        assert events.next() is None


def test_a_change_before_the_subscription_is_caught_up(world: World) -> None:
    world.harness.gateway.set(world.platform_id, status="awaiting_admission")

    with world.stream("lead") as events:
        assert events.next()[1]["status"] == "dispatching"
        world.stub.subscribed()

        assert events.next()[1]["status"] == "awaiting_admission"


def test_an_unreadable_end_tells_the_viewer_to_poll(world: World) -> None:
    with world.stream("lead") as events:
        events.next()
        conn = world.stub.subscribed()
        world.harness.gateway.fail["get meeting"] = 500
        world.stub.push(conn, status_frame(world.platform_id, "completed"))

        assert events.next() == ("error", {"code": "live_unavailable",
                                           "detail": live.UNAVAILABLE_DETAIL})
        assert events.next() is None


def test_the_viewer_leaving_closes_the_gateway_socket(world: World) -> None:
    with world.stream("lead") as events:
        events.next()
        conn = world.stub.subscribed()
        assert not conn.closed.is_set()

    assert conn.closed.wait(WAIT), "the relay kept the gateway socket after the viewer left"


@pytest.mark.parametrize(
    "refusal",
    [("invalid_api_key", 4401), ("auth_unavailable", 4503), (None, 4401), (None, 4503)],
    ids=["4401 with frame", "4503 with frame", "4401 bare", "4503 bare"],
)
def test_gateway_refusals_become_an_error_event(world: World, refusal) -> None:
    world.stub.refuse = refusal

    with world.stream("lead") as events:
        assert events.next()[0] == "status"
        assert events.next() == ("error", {"code": "live_unavailable",
                                           "detail": live.UNAVAILABLE_DETAIL})
        assert events.next() is None


def test_an_error_frame_or_a_drop_mid_stream_ends_with_an_error_event(world: World) -> None:
    frames = ({"type": "error", "error": "authorization_service_error", "status": 503}, CLOSE)
    for nth, frame in enumerate(frames, start=1):
        with world.stream("lead") as events:
            events.next()
            conn = world.stub.subscribed(nth)
            world.stub.push(conn, frame)

            assert events.next()[0] == "error"
            assert events.next() is None


def test_a_subscription_the_platform_did_not_grant_is_an_error(world: World) -> None:
    world.stub.grant = False

    with world.stream("lead") as events:
        events.next()
        assert events.next()[0] == "error"
        assert events.next() is None


def test_an_unreachable_gateway_is_an_error(app_engine: Engine) -> None:
    w = make_world(app_engine, gateway_url="http://127.0.0.1:1")
    try:
        with w.stream("lead") as events:
            events.next()
            assert events.next()[0] == "error"
            assert events.next() is None
    finally:
        w.app.stop()
        w.stub.stop()


def test_keepalive_comments_while_nothing_happens(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "HEARTBEAT_SECONDS", 0.2)

    with world.stream("lead") as events:
        events.next()
        world.stub.subscribed()

        assert events.comment() == ": keepalive"


def test_an_ended_meeting_sends_its_status_and_never_reaches_the_platform(world: World) -> None:
    world.set_row(status="done", end_reason="stopped")

    with world.stream("lead") as events:
        assert events.next()[1]["status"] == "done"
        assert events.next() is None
    assert world.stub.connections == []


def test_waiting_for_the_platform_meeting(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "PLATFORM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(live, "PLATFORM_WAIT_SECONDS", 0.4)
    world.set_row(platform_meeting_id=None, native_meeting_id=None)

    with world.stream("lead") as events:
        assert events.next()[1]["status"] == "dispatching"
        assert events.next()[0] == "error", "gives up when the platform never answers"
    with world.stream("lead") as events:
        events.next()
        world.set_row(status="failed", failure_reason="internal_error")
        assert events.next()[1]["failure_reason"] == "internal_error"
        assert events.next() is None
    assert world.stub.connections == []


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_admins_may_watch(world: World, role: str) -> None:
    with world.stream(role) as events:
        assert events.next()[0] == "status"
        world.stub.subscribed()


def test_nobody_else_may_watch(world: World, app_engine: Engine) -> None:
    other = role_org(app_engine)
    path = f"/api/v1/meetings/{world.meeting_id}/live"

    member = sign_in(world.harness, world.org.member).get(path)
    stranger = sign_in(world.harness, other.owner).get(path)
    anonymous = world.harness.client().get(path)

    assert (member.status_code, member.json()["code"]) == (404, "no_such_meeting")
    assert (stranger.status_code, stranger.json()["code"]) == (404, "no_such_meeting")
    assert anonymous.status_code == 401
    assert world.stub.connections == []
