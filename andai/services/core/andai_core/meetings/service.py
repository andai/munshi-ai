"""Sending the bot to a meeting, reading its state, and stopping it.

**Links.** andai checks only the host family of a pasted link (Google Meet, Microsoft Teams, Zoom
including vanity subdomains), so anything else is refused with 422 before any call goes out. The
platform parses the link; andai stores the `platform`, `native_meeting_id` and meeting `id` that
`POST /bots` answers and never derives them itself. `join_url_key` — the link's host and path,
lower-cased, query dropped — is what makes "the same link" in one org.

**One bot per link per org.** `join` first inserts a `dispatching` row and commits it. The partial
unique index on `(org_id, join_url_key)` over live rows means a second paste of a live link (even a
concurrent one) finds the first row instead of dispatching again: the caller gets that meeting if
they may read it, else 409 `already_in_meeting` with no id. Only then does andai call the platform,
with the org's key unsealed for that call. A structured platform 409 (the platform already runs a
bot for the meeting, e.g. the same Zoom meeting pasted under another hostname) attaches the new row
to that platform meeting, or answers the andai meeting that already holds it.

**No rejoin (D-38).** A link whose bot a participant removed in the last 12 hours is refused with
409 `removed_by_participant`; canary meetings are exempt. andai never re-dispatches on its own.

**Canaries.** Source `canary` is accepted only from the canary org (`orgs.is_canary`, created once
by `python -m andai_core canary-setup`), and that org sends nothing else, so canary bots never use
a customer org's bot cap or appear in its data. `canary_actor` is how andai-worker acts as it.

**Access (AUTH_MATRIX).** A meeting is readable and stoppable by its requester, by a lead of its
team, and by org admins and owners; for anyone else it does not exist (404). The pasted link is
returned with its passcode only to the requester and admins.

The Zoom passcode stays in the stored link and is never logged: every logged URL goes through
`redact_url`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anyio.to_thread
from sqlalchemy import Engine, exists, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.crypto import Keyring
from andai_core.db.models import (
    PLATFORMS,
    TERMINAL_STATUSES,
    ConsentRecord,
    Meeting,
    Org,
    OrgMembership,
    Team,
    TeamMembership,
)
from andai_core.db.session import transaction
from andai_core.meetings import consent
from andai_core.meetings.status_map import admit_instructions, map_platform
from andai_core.platform import principals
from andai_core.platform.gateway_client import DuplicateMeeting, GatewayError, PlatformGateway
from andai_core.settings import Settings

log = logging.getLogger(__name__)

ADMINS = ("owner", "admin")
LANGUAGES = ("en", "hi", "hinglish")
# The STT language sent for Hinglish until the speech pipeline settles on one (Phase 2).
HINGLISH_STT_LANGUAGE = "hi"
REMOVAL_WINDOW = timedelta(hours=12)
PASSCODE_PARAMS = frozenset({"pwd", "p", "password", "passcode"})
NOT_TERMINAL = Meeting.status.not_in(TERMINAL_STATUSES)

MEET_HOSTS = ("meet.google.com",)
TEAMS_HOSTS = ("teams.microsoft.com", "teams.live.com", "teams.microsoft.us")
ZOOM_HOSTS = ("zoom.us", "zoomgov.com")


class MeetingError(Exception):
    """A refusal with an HTTP status and a stable `code` for the API."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


NOT_A_MEETING_LINK = MeetingError(
    422, "not_a_meeting_link", "paste a Google Meet, Microsoft Teams or Zoom meeting link"
)
NO_SUCH_MEETING = MeetingError(404, "no_such_meeting", "no such meeting")
PLATFORM_REJECTED_LINK = MeetingError(
    422, "not_a_meeting_link", "the meeting platform did not accept this link"
)


# ─── links ────────────────────────────────────────────────────────────────────────────────────────


def _under(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def link_platform(url: str) -> str:
    """The platform family of a pasted link (`google_meet`, `teams`, `zoom`). Raises the 422
    refusal for anything else, including non-https links."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        raise NOT_A_MEETING_LINK from None
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        raise NOT_A_MEETING_LINK
    if _under(host, MEET_HOSTS):
        return "google_meet"
    if _under(host, TEAMS_HOSTS):
        return "teams"
    if _under(host, ZOOM_HOSTS):
        return "zoom"
    raise NOT_A_MEETING_LINK


def join_url_key(url: str) -> str:
    """The link's identity for dedup: host and path, lower-cased, no query, no trailing slash."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return f"{(parts.hostname or '').lower()}{path.lower()}"


def redact_url(url: str) -> str:
    """The link without passcode values (`pwd`, `p`, `password`, `passcode`)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable link>"
    query = [
        (k, "REDACTED" if k.lower() in PASSCODE_PARAMS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query, safe="REDACTED")))


# ─── access ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    org_id: uuid.UUID
    user_id: uuid.UUID
    role: str  # org role

    @property
    def is_admin(self) -> bool:
        return self.role in ADMINS


def _leads(tx: Tx, actor: Actor, team_id: uuid.UUID | None) -> bool:
    if team_id is None:
        return False
    return bool(
        tx.scalar(
            select(
                exists().where(
                    TeamMembership.org_id == actor.org_id,
                    TeamMembership.team_id == team_id,
                    TeamMembership.user_id == actor.user_id,
                    TeamMembership.role == "lead",
                )
            )
        )
    )


def can_read(tx: Tx, actor: Actor, meeting: Meeting) -> bool:
    return (
        actor.is_admin
        or meeting.requested_by == actor.user_id
        or _leads(tx, actor, meeting.team_id)
    )


def sees_passcode(actor: Actor, meeting: Meeting) -> bool:
    return actor.is_admin or meeting.requested_by == actor.user_id


def readable(tx: Tx, actor: Actor, meeting_id: uuid.UUID) -> Meeting:
    meeting = tx.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.org_id == actor.org_id)
    )
    if meeting is None or not can_read(tx, actor, meeting):
        raise NO_SUCH_MEETING
    return meeting


def view(tx: Tx, actor: Actor, meeting: Meeting) -> dict:
    """The meeting as the API returns it to `actor`."""
    snapshot = meeting.settings_snapshot or {}
    bot_name = str(snapshot.get("bot_name") or "")
    return {
        "id": str(meeting.id),
        "team_id": None if meeting.team_id is None else str(meeting.team_id),
        "requested_by": str(meeting.requested_by),
        "source": meeting.source,
        "platform": meeting.platform,
        "native_meeting_id": meeting.native_meeting_id,
        "join_url": meeting.join_url if sees_passcode(actor, meeting) else redact_url(meeting.join_url),
        "status": meeting.status,
        "failure_reason": meeting.failure_reason,
        "end_reason": meeting.end_reason,
        "admit_instructions": (
            admit_instructions(meeting.platform, bot_name)
            if meeting.status == "awaiting_admission"
            else None
        ),
        "language": snapshot.get("language"),
        "bot_name": bot_name,
        "consent": consent.summary(consent.record_for(tx, meeting.id)),
        "started_at": None if meeting.started_at is None else meeting.started_at.isoformat(),
        "ended_at": None if meeting.ended_at is None else meeting.ended_at.isoformat(),
        "created_at": meeting.created_at.isoformat(),
        "updated_at": meeting.updated_at.isoformat(),
    }


# ─── dispatch ─────────────────────────────────────────────────────────────────────────────────────


def settings_snapshot(settings: Settings, language: str) -> dict:
    """The org's fixed defaults plus the meeting's language, frozen at dispatch."""
    return {
        "bot_name": settings.bot_name,
        "language": language,
        "stt_language": HINGLISH_STT_LANGUAGE if language == "hinglish" else language,
        "lobby_timeout_s": settings.lobby_timeout_s,
        "alone_timeout_s": settings.alone_timeout_s,
        "disclosure_text": settings.disclosure_text,
        "privacy_notice_url": settings.privacy_notice_url,
    }


def dispatch_body(meeting: Meeting) -> dict:
    """The `POST /bots` body: the pasted link (the platform parses it), the snapshot's language,
    bot name, automatic-leave timeouts in milliseconds and the recording notice."""
    snapshot = meeting.settings_snapshot
    return {
        "meeting_url": meeting.join_url,
        "language": snapshot["stt_language"],
        "bot_name": snapshot["bot_name"],
        "automatic_leave": {
            "max_wait_for_admission": int(snapshot["lobby_timeout_s"]) * 1000,
            "max_time_left_alone": int(snapshot["alone_timeout_s"]) * 1000,
        },
        "disclosure": {
            "text": snapshot["disclosure_text"],
            "privacy_notice_url": snapshot["privacy_notice_url"],
        },
    }


@dataclass(frozen=True)
class JoinResult:
    created: bool
    meeting: dict


@dataclass(frozen=True)
class _Provisional:
    meeting_id: uuid.UUID
    body: dict
    key: str


def _check_team(tx: Tx, actor: Actor, team_id: uuid.UUID | None) -> None:
    if team_id is None:
        return
    team = tx.scalar(select(Team.id).where(Team.id == team_id, Team.org_id == actor.org_id))
    if team is None:
        raise MeetingError(404, "no_such_team", "no such team")
    if actor.is_admin:
        return
    member = tx.scalar(
        select(
            exists().where(
                TeamMembership.org_id == actor.org_id,
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == actor.user_id,
            )
        )
    )
    if not member:
        raise MeetingError(403, "not_in_team", "you can send the bot only for your own teams")


def recently_removed(tx: Tx, org_id: uuid.UUID, key: str, now: datetime) -> bool:
    """Whether a participant removed the bot from a meeting of this link in the last 12 hours."""
    return bool(
        tx.scalar(
            select(
                exists().where(
                    ConsentRecord.meeting_id == Meeting.id,
                    Meeting.org_id == org_id,
                    Meeting.join_url_key == key,
                    ConsentRecord.removed_at.is_not(None),
                    ConsentRecord.removed_at > now - REMOVAL_WINDOW,
                )
            )
        )
    )


def _live_by_key(tx: Tx, org_id: uuid.UUID, key: str) -> Meeting | None:
    return tx.scalar(
        select(Meeting).where(
            Meeting.org_id == org_id, Meeting.join_url_key == key, NOT_TERMINAL
        )
    )


def _existing_answer(tx: Tx, actor: Actor, meeting: Meeting) -> JoinResult:
    if not can_read(tx, actor, meeting):
        raise MeetingError(
            409, "already_in_meeting", "the bot is already in this meeting for your organisation"
        )
    return JoinResult(created=False, meeting=view(tx, actor, meeting))


class Meetings:
    """The meeting operations over the app's engine, keyring and gateway."""

    def __init__(
        self,
        engine: Engine,
        keyring: Keyring,
        gateway: PlatformGateway,
        settings: Settings,
        clock,
    ) -> None:
        self.engine = engine
        self.keyring = keyring
        self.gateway = gateway
        self.settings = settings
        self.clock = clock

    def _tx(self, actor: Actor):
        return transaction(self.engine, org_id=actor.org_id, user_id=actor.user_id)

    # ── join ──

    def _provision(
        self,
        actor: Actor,
        *,
        join_url: str,
        platform: str,
        language: str,
        team_id: uuid.UUID | None,
        source: str,
    ) -> _Provisional | JoinResult:
        key = join_url_key(join_url)
        now = self.clock()
        meeting_id = uuid.uuid4()
        try:
            with self._tx(actor) as tx:
                is_canary = bool(tx.scalar(select(Org.is_canary).where(Org.id == actor.org_id)))
                if (source == "canary") != is_canary:
                    raise MeetingError(
                        403,
                        "canary_org_only",
                        "canary meetings come only from the canary organisation, and it sends "
                        "nothing else",
                    )
                _check_team(tx, actor, team_id)
                if source != "canary" and recently_removed(tx, actor.org_id, key, now):
                    raise MeetingError(
                        409,
                        "removed_by_participant",
                        "participants removed the bot from this meeting; it will not rejoin "
                        "for 12 hours",
                    )
                secret = principals.open_key(tx, self.keyring, actor.org_id)
                if secret is None:
                    raise MeetingError(
                        503, "no_platform_principal", "this organisation cannot send bots yet"
                    )
                meeting = Meeting(
                    id=meeting_id,
                    org_id=actor.org_id,
                    team_id=team_id,
                    requested_by=actor.user_id,
                    source=source,
                    platform=platform,
                    join_url=join_url,
                    join_url_key=key,
                    status="dispatching",
                    settings_snapshot=settings_snapshot(self.settings, language),
                    created_at=now,
                    updated_at=now,
                )
                tx.add(meeting)
                tx.flush()
                body = dispatch_body(meeting)
        except IntegrityError:
            with self._tx(actor) as tx:
                existing = _live_by_key(tx, actor.org_id, key)
                if existing is None:  # ended between the two transactions: paste again
                    raise MeetingError(
                        409, "already_in_meeting", "the meeting just changed; try again"
                    ) from None
                return _existing_answer(tx, actor, existing)
        return _Provisional(meeting_id=meeting_id, body=body, key=secret)

    def _finish(
        self, actor: Actor, meeting_id: uuid.UUID, outcome: dict | GatewayError
    ) -> tuple[JoinResult | MeetingError, bool]:
        """Record the platform's answer on the provisional row and answer the caller (a refusal is
        returned, not raised, so the transaction commits). The flag says the platform started a bot
        that no live row holds any more (stopped meanwhile)."""
        now = self.clock()
        with self._tx(actor) as tx:
            meeting = tx.get(Meeting, meeting_id, with_for_update=True)
            if meeting is None:
                raise NO_SUCH_MEETING
            if meeting.status != "dispatching":
                # Stopped (or given up on) while the dispatch was in flight; that decision wins.
                return JoinResult(created=True, meeting=view(tx, actor, meeting)), isinstance(
                    outcome, dict
                )
            if isinstance(outcome, DuplicateMeeting):
                held = None
                if outcome.meeting_id is not None:
                    held = tx.scalar(
                        select(Meeting).where(
                            Meeting.org_id == actor.org_id,
                            Meeting.platform_meeting_id == outcome.meeting_id,
                            Meeting.id != meeting_id,
                            NOT_TERMINAL,
                        )
                    )
                if held is not None:
                    tx.delete(meeting)
                    tx.flush()
                    try:
                        return _existing_answer(tx, actor, held), False
                    except MeetingError as refusal:  # returned, so the delete commits
                        return refusal, False
                if outcome.meeting_id is None:
                    _fail(meeting, "internal_error", now)
                else:
                    self._place(
                        tx, meeting, outcome.meeting_id, outcome.platform,
                        outcome.native_meeting_id, now,
                    )
            elif isinstance(outcome, GatewayError):
                if outcome.status == 422:
                    tx.delete(meeting)
                    tx.flush()
                    return PLATFORM_REJECTED_LINK, False
                _fail(meeting, "capacity_exhausted" if outcome.status == 429 else "internal_error", now)
            else:
                self._attach(tx, meeting, outcome, now)
            audit.record(tx, actor.org_id, actor.user_id, "meeting.dispatch", f"meeting:{meeting.id}")
            tx.flush()
            return JoinResult(created=True, meeting=view(tx, actor, meeting)), False

    def _place(
        self,
        tx: Tx,
        meeting: Meeting,
        platform_id: int,
        platform: str | None,
        native_id: str | None,
        now: datetime,
    ) -> None:
        """Point `meeting` at the platform meeting `platform_id`. The platform runs one bot per
        native meeting, so any other live row of ours for the same native meeting points at a
        platform meeting that has ended: it is closed first, before the unique index sees two."""
        if platform in PLATFORMS:
            meeting.platform = platform
        if native_id is not None:
            stale = tx.scalars(
                select(Meeting).where(
                    Meeting.org_id == meeting.org_id,
                    Meeting.platform == meeting.platform,
                    Meeting.native_meeting_id == native_id,
                    Meeting.id != meeting.id,
                    NOT_TERMINAL,
                )
            ).all()
            for row in stale:
                log.warning("meeting %s: superseded by %s on the platform", row.id, meeting.id)
                _fail(row, "internal_error", now)
            tx.flush()
        meeting.platform_meeting_id = platform_id
        meeting.native_meeting_id = native_id
        meeting.updated_at = now

    def _attach(self, tx: Tx, meeting: Meeting, platform_meeting: dict, now: datetime) -> None:
        platform_id = platform_meeting.get("id")
        if not isinstance(platform_id, int) or isinstance(platform_id, bool):
            _fail(meeting, "internal_error", now)
            return
        platform = platform_meeting.get("platform")
        native = platform_meeting.get("native_meeting_id")
        self._place(
            tx, meeting, platform_id,
            platform if isinstance(platform, str) else None,
            native if isinstance(native, str) else None,
            now,
        )
        mapped = map_platform(platform_meeting)
        if mapped.unknown:
            log.warning("meeting %s: unknown platform value %s", meeting.id, mapped.unknown)
        meeting.status = mapped.status
        meeting.failure_reason = mapped.failure_reason
        tx.flush()

    async def join(
        self,
        actor: Actor,
        *,
        join_url: str,
        language: str | None,
        team_id: uuid.UUID | None,
        source: str = "portal_link",
    ) -> JoinResult:
        join_url = join_url.strip()
        platform = link_platform(join_url)
        language = language or self.settings.default_language
        if language not in LANGUAGES:
            raise MeetingError(422, "unsupported_language", "language must be en, hi or hinglish")
        provisional = await anyio.to_thread.run_sync(
            lambda: self._provision(
                actor,
                join_url=join_url,
                platform=platform,
                language=language,
                team_id=team_id,
                source=source,
            )
        )
        if isinstance(provisional, JoinResult):
            return provisional
        try:
            outcome: dict | GatewayError = await self.gateway.dispatch(
                provisional.key, provisional.body
            )
        except GatewayError as exc:
            log.warning(
                "meeting %s: dispatch to %s refused: %s",
                provisional.meeting_id, redact_url(join_url), exc,
            )
            outcome = exc
        result, orphaned = await anyio.to_thread.run_sync(
            lambda: self._finish(actor, provisional.meeting_id, outcome)
        )
        if orphaned and isinstance(outcome, dict):
            # A stop landed while the dispatch was in flight: take the new bot down again.
            await self._stop_on_platform(provisional.key, outcome)
        if isinstance(result, MeetingError):
            raise result
        return result

    # ── read ──

    def get(self, actor: Actor, meeting_id: uuid.UUID) -> dict:
        with self._tx(actor) as tx:
            return view(tx, actor, readable(tx, actor, meeting_id))

    # ── stop ──

    async def _stop_on_platform(self, key: str, platform_meeting: dict) -> None:
        platform = platform_meeting.get("platform")
        native = platform_meeting.get("native_meeting_id")
        if isinstance(platform, str) and isinstance(native, str):
            try:
                await self.gateway.stop(key, platform, native)
            except GatewayError as exc:
                if exc.status != 404:
                    log.warning("could not stop a bot dispatched after its stop: %s", exc)

    def _stop_target(self, actor: Actor, meeting_id: uuid.UUID) -> tuple[Meeting, str | None]:
        with self._tx(actor) as tx:
            meeting = readable(tx, actor, meeting_id)
            if meeting.status in TERMINAL_STATUSES:
                raise MeetingError(409, "not_running", "the bot is no longer in this meeting")
            key = None
            if meeting.platform_meeting_id is not None:
                key = principals.open_key(tx, self.keyring, actor.org_id)
            return meeting, key

    def _record_stop(self, actor: Actor, meeting_id: uuid.UUID, *, cancel: bool) -> dict:
        now = self.clock()
        with self._tx(actor) as tx:
            meeting = readable(tx, actor, meeting_id)
            if cancel and meeting.status not in TERMINAL_STATUSES:
                meeting.status = "cancelled"
                meeting.failure_reason = None
                meeting.ended_at = now
                meeting.updated_at = now
            audit.record(tx, actor.org_id, actor.user_id, "meeting.stop", f"meeting:{meeting.id}")
            tx.flush()
            return view(tx, actor, meeting)

    async def stop(self, actor: Actor, meeting_id: uuid.UUID) -> dict:
        meeting, key = await anyio.to_thread.run_sync(lambda: self._stop_target(actor, meeting_id))
        before_admission = meeting.status in ("scheduled", "dispatching", "awaiting_admission")
        if key is not None and meeting.native_meeting_id is not None:
            try:
                await self.gateway.stop(key, meeting.platform, meeting.native_meeting_id)
            except GatewayError as exc:
                if exc.status != 404:
                    raise MeetingError(
                        502, "platform_unavailable", "could not reach the meeting platform; try again"
                    ) from None
            if before_admission and meeting.platform_meeting_id is not None:
                # Admitted in the meantime? Then it ends as a recorded meeting, not a cancelled one.
                try:
                    current = await self.gateway.get_meeting(key, meeting.platform_meeting_id)
                    before_admission = not map_platform(current).admitted
                except GatewayError:
                    pass
        return await anyio.to_thread.run_sync(
            lambda: self._record_stop(actor, meeting_id, cancel=before_admission)
        )


# ─── the canary org ───────────────────────────────────────────────────────────────────────────────


def canary_org_id(engine: Engine) -> uuid.UUID | None:
    """The canary org's id, read with no org context through `andai_canary_org_id()` (0005)."""
    with transaction(engine) as tx:
        value = tx.scalar(text("SELECT andai_canary_org_id()"))
    return None if value is None else uuid.UUID(str(value))


def canary_actor(engine: Engine) -> Actor | None:
    """The canary org's owner as an `Actor`, or None before `canary-setup` ran."""
    org_id = canary_org_id(engine)
    if org_id is None:
        return None
    with transaction(engine, org_id=org_id) as tx:
        owner = tx.scalar(
            select(OrgMembership.user_id).where(
                OrgMembership.org_id == org_id, OrgMembership.role == "owner"
            )
        )
    return None if owner is None else Actor(org_id=org_id, user_id=owner, role="owner")


def _fail(meeting: Meeting, reason: str, now: datetime) -> None:
    meeting.status = "failed"
    meeting.failure_reason = reason
    meeting.ended_at = meeting.ended_at or now
    meeting.updated_at = now


__all__ = [
    "Actor",
    "canary_actor",
    "canary_org_id",
    "JoinResult",
    "MeetingError",
    "Meetings",
    "join_url_key",
    "link_platform",
    "recently_removed",
    "redact_url",
]
