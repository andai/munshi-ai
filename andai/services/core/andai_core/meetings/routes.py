"""Meetings under /api/v1 (AUTH_MATRIX rows `/api/v1/meetings/...`).

* `GET /meetings?section=&filter=&limit=&cursor=` — the meetings the caller may read, as Live /
  Upcoming / Past sections, keyset-paginated (`listing.py`). Any member.
* `POST /meetings/join {join_url, language?, team_id?}` — send the bot to a Google Meet, Microsoft
  Teams or Zoom meeting. Any org member; a member names only a team they belong to. 201 with the new
  meeting; 200 with the meeting already live for that link when the caller may read it; 409
  `already_in_meeting` (no id) when they may not; 409 `removed_by_participant` within 12 hours of
  a removal (D-38); 422 for a link that is not a meeting link.
* `GET /meetings/{id}` — status, typed failure reason, admit instructions while the bot waits,
  consent summary, team and requester names, and the caller's `permissions`. Whoever may read it
  (`access.py`: requester, a lead of the meeting's team, admins and owners, and the users and team
  members it is shared with); 404 for anyone else.
* `DELETE /meetings/{id}` — delete the meeting's audio, transcript and minutes (`deletion.py`).
  Requester, admins and owners (not team leads or grantees: 404). 202 `{id, purge_by}`: the
  meeting is hidden from every read path at once and audited (`meeting.delete`); andai-worker
  purges it from the platform and andai within 15 minutes. 409 `meeting_live` while the bot is
  still in the meeting; a second delete is 404.
* `POST /meetings/{id}/stop` — take the bot out. Requester, team lead, admins and owners (not
  grantees: 404). Before admission the meeting ends `cancelled`; after it, the platform's own end
  moves it to `done`.
* `GET /meetings/{id}/transcript` — the transcript so far in andai's segment shape
  (`transcript.py`); the polling source when the live stream drops. Same access as reading.
* `GET /meetings/{id}/live` — server-sent events with the meeting's status and transcript as they
  change (`live.py`). Same access as reading; the browser never reaches the platform.
* `GET /meetings/{id}/mom` — the latest version of the meeting's minutes, a SPEC `MomDocument`
  whose evidence ids are the transcript's segment ids (`mom.py`). Same access as reading; 404
  `no_minutes` when there are none yet. There is no write route.
* `GET /meetings/{id}/shares`, `POST /meetings/{id}/shares {grantee_type, grantee_id}` and
  `DELETE /meetings/{id}/shares/{share_id}` — who the meeting is shared with, share it with a user
  or team of the org (201 new, 200 already shared), stop sharing (204). Requester, team lead,
  admins and owners; 404 for everyone else, grantees included (`shares.py`).

Refusals answer `{"detail", "code"}`.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from andai_core.auth.deps import Services, client_ip, get_services
from andai_core.auth.roles import Caller, require_role
from andai_core.db.session import transaction
from andai_core.meetings import deletion, listing, mom, shares
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


class ShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grantee_type: Literal["user", "team"]
    grantee_id: uuid.UUID


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


@router.get("/meetings")
def list_meetings(
    section: Literal["live", "upcoming", "past"] | None = None,
    filter: str | None = Query(default=None, max_length=64),  # noqa: A002 - the API's name
    limit: int = Query(default=listing.DEFAULT_LIMIT, ge=1, le=listing.MAX_LIMIT),
    cursor: str | None = None,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        return listing.list_meetings(
            tx, who, section=section, filter_=filter, limit=limit, cursor=cursor
        )


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


@router.delete("/meetings/{meeting_id}")
def delete_meeting(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> JSONResponse:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        accepted = deletion.request_delete(tx, who, meeting_id, now=services.clock(), ip=ip)
    return JSONResponse(accepted, status_code=202)


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


@router.get("/meetings/{meeting_id}/mom")
def minutes(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        return mom.read(tx, who, meeting_id)


@router.get("/meetings/{meeting_id}/shares")
def list_shares(
    meeting_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
) -> dict:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        return shares.list_shares(tx, who, meeting_id)


@router.post("/meetings/{meeting_id}/shares")
def grant_share(
    meeting_id: uuid.UUID,
    body: ShareBody,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> JSONResponse:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        share, created = shares.grant(
            tx, who, meeting_id, body.grantee_type, body.grantee_id, ip=ip
        )
    return JSONResponse(share, status_code=201 if created else 200)


@router.delete("/meetings/{meeting_id}/shares/{share_id}", status_code=204)
def revoke_share(
    meeting_id: uuid.UUID,
    share_id: uuid.UUID,
    caller: Caller = Depends(require_role(*EVERYONE)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> Response:
    who = actor(caller)
    with transaction(services.engine, org_id=who.org_id, user_id=who.user_id) as tx:
        shares.revoke(tx, who, meeting_id, share_id, ip=ip)
    return Response(status_code=204)
