"""A meeting's live status and transcript, relayed to one viewer as server-sent events.

`GET /api/v1/meetings/{id}/live` (AUTH_MATRIX: requester, a lead of the meeting's team, admins and
owners; 404 for anyone else, before any stream starts). The browser never reaches the platform:
after the access check andai-core opens one gateway WebSocket (`/ws`) for this viewer with the
org's key, subscribes to the meeting (`{platform, native_id}`) and re-shapes what arrives:

    event: status    {status, failure_reason, end_reason, admit_instructions, at}
    event: segment   a transcript segment in andai's shape (`transcript.py`)
    event: retract   {ids: [segment id, ...]}   (pending drafts the platform withdrew)
    event: error     {code: "live_unavailable", detail}
    : keepalive      (a comment every 15 seconds)

* The first event is always `status`, from andai's own row. A meeting that has ended gets that one
  event and the stream ends; nothing reaches the platform.
* While the platform has no meeting for it yet (the dispatch is in flight), andai's row is read
  every second for up to 60 seconds.
* Status: the gateway's `meeting.status` frames (per meeting, and the principal's user-wide copy)
  only say that something changed. Each one for this meeting is resolved by reading the platform
  meeting (`GET /meetings/{id}`) and mapping it through `status_map`, so a failure carries its
  `JoinFailureReason`. The same read runs once right after the subscription, to catch a change
  that happened before it. A status is sent only when it differs from the last one sent.
* Transcript: every transcript frame shape (`segments_of_frame`) and `transcript_retract`. A
  segment is sent again only when it changed (the bot and the collector both publish it).
* Frames that name another platform meeting are dropped: the principal's user-wide channel carries
  every meeting of the org, and a viewer may not be allowed to read the others.
* The stream ends after a terminal status, and the gateway socket closes with it; it also closes
  when the viewer goes away (Starlette cancels the response). Any gateway refusal or drop (close
  4401 bad key, 4503 auth down, an `error` frame, a subscription the platform did not grant, a
  failed connect) ends the stream with `error`: the viewer then polls `GET /meetings/{id}` and
  `GET /meetings/{id}/transcript`.

Nothing of the platform's own (its meeting and segment ids, the key, the gateway's address) is
sent to the viewer or logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import anyio.to_thread
from fastapi.responses import StreamingResponse
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from andai_core.auth.deps import Services
from andai_core.db.models import TERMINAL_STATUSES
from andai_core.meetings.service import Actor, MeetingError
from andai_core.meetings.status_map import Mapped, admit_instructions, map_platform
from andai_core.meetings.transcript import (
    Watched,
    normalise_segment,
    parse_time,
    segment_id,
    segments_of_frame,
    watched,
)
from andai_core.platform.gateway_client import (
    API_KEY_HEADER,
    GatewayError,
    PlatformGateway,
    live_url,
)

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15.0
PLATFORM_WAIT_SECONDS = 60.0
PLATFORM_POLL_SECONDS = 1.0
OPEN_TIMEOUT_SECONDS = 10.0
ENDED = frozenset({"completed", "failed"})
UNAVAILABLE_DETAIL = "live updates stopped; poll the meeting and its transcript"
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


KEEPALIVE = ": keepalive\n\n"
UNAVAILABLE = event("error", {"code": "live_unavailable", "detail": UNAVAILABLE_DETAIL})


def _platform_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class Relay:
    """One viewer's stream of one meeting."""

    def __init__(
        self, services: Services, gateway: PlatformGateway, actor: Actor, meeting: Watched
    ) -> None:
        self.services = services
        self.gateway = gateway
        self.actor = actor
        self.meeting = meeting
        self.base = meeting.started_at
        self.last: tuple | None = None
        self.sent: dict[str, tuple] = {}

    # ── events ──

    def _status(
        self, status: str, failure_reason: str | None, end_reason: str | None
    ) -> str | None:
        current = (status, failure_reason, end_reason)
        if current == self.last:
            return None
        self.last = current
        m = self.meeting
        return event(
            "status",
            {
                "status": status,
                "failure_reason": failure_reason,
                "end_reason": end_reason,
                "admit_instructions": (
                    admit_instructions(m.platform, m.bot_name)
                    if status == "awaiting_admission"
                    else None
                ),
                "at": self.services.clock().isoformat(),
            },
        )

    def _segments(self, frame: dict) -> list[str]:
        out = []
        for raw in segments_of_frame(frame):
            segment = normalise_segment(raw, meeting_id=self.meeting.id, base=self.base)
            if segment is None:
                continue
            fingerprint = tuple(segment[k] for k in sorted(segment))
            if self.sent.get(segment["id"]) == fingerprint:
                continue
            self.sent[segment["id"]] = fingerprint
            out.append(event("segment", segment))
        return out

    def _retract(self, frame: dict) -> list[str]:
        raw = frame.get("segment_ids")
        ids = [
            segment_id(self.meeting.id, str(s))
            for s in (raw if isinstance(raw, list) else [])
            if isinstance(s, str | int) and not isinstance(s, bool) and str(s)
        ]
        for sid in ids:
            self.sent.pop(sid, None)
        return [event("retract", {"ids": ids})] if ids else []

    # ── frames ──

    def _names_this_meeting(self, frame: dict, *, required: bool) -> bool:
        """Whether a frame is about this platform meeting (`meeting.id`, or the flat
        `meeting_id` of the user-wide copy). A frame naming none passes unless `required`."""
        ref = frame.get("meeting")
        named = ref.get("id") if isinstance(ref, dict) else frame.get("meeting_id")
        if named is None:
            return not required
        return _platform_id(named) == self.meeting.platform_meeting_id

    async def _resolve(self, raw_status: str | None) -> tuple[list[str], bool]:
        """Read the platform meeting and send its mapped status if it changed. `raw_status` is
        the frame's status, None for the catch-up read after subscribing."""
        m = self.meeting
        try:
            record = await self.gateway.get_meeting(m.key or "", m.platform_meeting_id or 0)
        except GatewayError as exc:
            if raw_status is None:
                return [], False  # the catch-up is best effort; frames still come
            if raw_status in ENDED:
                # Which end (done, failed and why) needs the record: the viewer polls instead.
                log.warning("meeting %s: live status read failed: %s", m.id, exc)
                return [UNAVAILABLE], True
            mapped: Mapped = map_platform({"status": raw_status})
        else:
            mapped = map_platform(record)
            self.base = parse_time(record.get("start_time")) or self.base
        chunk = self._status(mapped.status, mapped.failure_reason, mapped.end_reason)
        return ([chunk] if chunk else []), mapped.terminal

    async def _handle(self, frame: dict) -> tuple[list[str], bool]:
        """The events one gateway frame becomes, and whether the stream is over."""
        kind = frame.get("type")
        m = self.meeting
        if kind == "subscribed":
            refs = frame.get("meetings")
            granted = any(
                isinstance(r, dict)
                and r.get("platform") == m.platform
                and r.get("native_id") == m.native_meeting_id
                for r in (refs if isinstance(refs, list) else [])
            )
            if not granted:
                log.warning("meeting %s: the gateway did not grant the live subscription", m.id)
                return [UNAVAILABLE], True
            return await self._resolve(None)
        if kind == "error":
            code = frame.get("error")
            code = code if isinstance(code, str) else "?"
            log.warning("meeting %s: live relay refused: %s", m.id, code)
            return [UNAVAILABLE], True
        if kind == "meeting.status":
            if not self._names_this_meeting(frame, required=True):
                return [], False
            payload = frame.get("payload")
            raw = payload.get("status") if isinstance(payload, dict) else frame.get("status")
            return await self._resolve(raw if isinstance(raw, str) else "")
        if kind == "transcript_retract":
            if not self._names_this_meeting(frame, required=False):
                return [], False
            return self._retract(frame), False
        if kind in ("transcript", "transcription_segment"):
            if not self._names_this_meeting(frame, required=False):
                return [], False
            return self._segments(frame), False
        return [], False

    # ── the stream ──

    async def _reload(self) -> Watched | None:
        try:
            return await anyio.to_thread.run_sync(
                lambda: watched(
                    self.services.engine, self.services.keyring, self.actor, self.meeting.id
                )
            )
        except MeetingError:
            return None  # gone, or no longer readable

    def _ready(self) -> bool:
        m = self.meeting
        return None not in (m.platform_meeting_id, m.native_meeting_id, m.key)

    async def _relay(self, ws: ClientConnection) -> AsyncIterator[str]:
        m = self.meeting
        await ws.send(
            json.dumps(
                {
                    "action": "subscribe",
                    "meetings": [{"platform": m.platform, "native_id": m.native_meeting_id}],
                }
            )
        )
        beat = time.monotonic()
        while True:
            wait = HEARTBEAT_SECONDS - (time.monotonic() - beat)
            if wait <= 0:
                yield KEEPALIVE
                beat = time.monotonic()
                continue
            try:
                raw = await asyncio.wait_for(ws.recv(), wait)
            except TimeoutError:
                continue
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            chunks, done = await self._handle(frame)
            for chunk in chunks:
                yield chunk
            if done:
                return

    async def events(self) -> AsyncIterator[str]:
        m = self.meeting
        first = self._status(m.status, m.failure_reason, m.end_reason)
        if first:
            yield first
        if m.status in TERMINAL_STATUSES:
            return
        deadline = time.monotonic() + PLATFORM_WAIT_SECONDS
        while not self._ready():
            if time.monotonic() > deadline:
                yield UNAVAILABLE
                return
            await asyncio.sleep(PLATFORM_POLL_SECONDS)
            reloaded = await self._reload()
            if reloaded is None:
                yield UNAVAILABLE
                return
            self.meeting = m = reloaded
            self.base = m.started_at or self.base
            chunk = self._status(m.status, m.failure_reason, m.end_reason)
            if chunk:
                yield chunk
            if m.status in TERMINAL_STATUSES:
                return
        url = live_url(self.services.settings.platform_gateway_url)
        log.info("meeting %s: live relay opened", m.id)
        try:
            async with connect(
                url,
                additional_headers={API_KEY_HEADER: m.key or ""},
                open_timeout=OPEN_TIMEOUT_SECONDS,
                proxy=None,
            ) as ws:
                async for chunk in self._relay(ws):
                    yield chunk
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            log.warning("meeting %s: the gateway closed the live relay (%s)", m.id, code)
            yield UNAVAILABLE
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            log.warning("meeting %s: live relay could not connect: %s", m.id, type(exc).__name__)
            yield UNAVAILABLE
        finally:
            log.info("meeting %s: live relay closed", m.id)


async def open_stream(
    services: Services, gateway: PlatformGateway, actor: Actor, meeting_id: uuid.UUID
) -> StreamingResponse:
    """The viewer's event stream, after the access check (which raises the 404 refusal)."""
    meeting = await anyio.to_thread.run_sync(
        lambda: watched(services.engine, services.keyring, actor, meeting_id)
    )
    relay = Relay(services, gateway, actor, meeting)
    return StreamingResponse(relay.events(), media_type="text/event-stream", headers=HEADERS)


__all__ = ["HEARTBEAT_SECONDS", "Relay", "open_stream"]
