"""Meetings under /api/v1 (AUTH_MATRIX rows `/api/v1/meetings/...`).

* `POST /meetings/join {join_url, language?, team_id?}` — send the bot to a Google Meet, Microsoft
  Teams or Zoom meeting. Any org member; a member names only a team they belong to. 201 with the new
  meeting; 200 with the meeting already live for that link when the caller may read it; 409
  `already_in_meeting` (no id) when they may not; 409 `removed_by_participant` within 12 hours of
  a removal (D-38); 422 for a link that is not a meeting link.
* `GET /meetings/{id}` — status, typed failure reason, admit instructions while the bot waits,
  consent summary. Requester, a lead of the meeting's team, admins and owners; 404 for anyone else.
* `POST /meetings/{id}/stop` — take the bot out. Same access as reading. Before admission the
  meeting ends `cancelled`; after it, the platform's own end moves it to `done`.
* `GET /meetings/{id}/transcript` — the transcript so far in andai's segment shape
  (`transcript.py`); the polling source when the live stream drops. Same access as reading.
* `GET /meetings/{id}/live` — server-sent events with the meeting's status and transcript as they
  change (`live.py`). Same access as reading; the browser never reaches the platform.

Refusals answer `{"detail", "code"}`.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from andai_core.auth.deps import Services, get_services
from andai_core.auth.roles import Caller, require_role
from andai_core.meetings.live import open_stream
from andai_core.meetings.service import Actor, MeetingError, Meetings
from andai_core.meetings.transcript import read_transcript

router = APIRouter(prefix="/api/v1")

EVERYONE = ("owner", "admin", "member")


class JoinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    join_url: str = Field(min_length=1, max_length=2048)
    language: Literal["en", "hi", "hinglish"] | None = None
    team_id: uuid.UUID | None = None


def meetings(services: Services) -> Meetings:
    if services.gateway is None:
        raise MeetingError(503, "platform_unavailable", "the meeting platform is not configured")
    return Meetings(
        services.engine, services.keyring, services.gateway, services.settings, services.clock
    )


def actor(caller: Caller) -> Actor:
    return Actor(org_id=caller.session.org_id, user_id=caller.session.user_id, role=caller.role)


async def meeting_error(_: Request, exc: MeetingError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status)


@router.post("/meetings/join")
async def join(
    body: JoinBody,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> JSONResponse:
    result = await meetings(services).join(
        actor(caller), join_url=body.join_url, language=body.language, team_id=body.team_id
    )
    return JSONResponse(result.meeting, status_code=201 if result.created else 200)


@router.get("/meetings/{meeting_id}")
def get_meeting(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    return meetings(services).get(actor(caller), meeting_id)


@router.post("/meetings/{meeting_id}/stop")
async def stop(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    return await meetings(services).stop(actor(caller), meeting_id)


@router.get("/meetings/{meeting_id}/transcript")
async def transcript(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    return await read_transcript(
        services.engine, services.keyring, meetings(services).gateway, actor(caller), meeting_id
    )


@router.get("/meetings/{meeting_id}/live")
async def live(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> StreamingResponse:
    return await open_stream(services, meetings(services).gateway, actor(caller), meeting_id)
