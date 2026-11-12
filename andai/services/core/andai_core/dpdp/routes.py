"""Asking for, listing and downloading a copy of a person's data (OPS-02, DPDP access request).

* `POST /api/v1/me/exports` — any member asks for a copy of their own data in the caller's org
  (`source = self`).
* `POST /api/v1/org/users/{id}/exports` — owners and admins ask for a member's copy
  (`source = org_admin`); 404 `no_such_member` for someone who is not a member of the org.
  Both answer 202 with the request. At most one open (`pending`/`running`) request per person in
  an org (429 `export_in_progress`) and three per person per 24 hours (429 `export_rate_limited`);
  both are checked under a transaction-scoped advisory lock on (org, person). The request is
  audited `user.export.request` (target `user:<id>`); andai-worker builds it (`builder.py`).
* `GET /api/v1/me/exports` — the caller's copies in this org (asked by them or by an admin), newest
  first, the last `MY_LIMIT`.
* `GET /api/v1/org/exports?cursor=` — owners and admins: the org's admin-asked copies, newest
  first, 50 per page, with the member's display name; `canDownload` only on the caller's own.
* `GET /api/v1/exports/{id}/download` — the zip. Only the person whose data it is, or the owner or
  admin who asked for it (while they are still one), of the request's org; 404 for anyone else and
  for another org's id. 409 `export_not_ready` before it is built or after it failed; 410
  `export_expired` once its time has passed. The sealed JSON part is opened and written into a zip
  (deflated) in a `SpooledTemporaryFile` (16 MiB in memory, then disk), manifest first; each
  recording is streamed from the platform with the org's key into the zip (stored) and its SHA-256
  and size compared with the manifest's. A recording that changed or is gone stops the download
  with 502 `recording_changed`, a platform that cannot be reached with 502 `platform_unavailable`;
  both are audited `user.export.download_failed`. A complete zip is audited `user.export.download`
  and sent as `application/zip` with `Content-Disposition: attachment;
  filename="munshi-export-<date>.zip"` and `Cache-Control: no-store`.

Each request is listed as `{id, subjectId, subjectName?, source, state, error, createdAt, readyAt,
expiresAt, bytes, canDownload}`; no address, key or content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import anyio.to_thread
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Tx

from andai_core import audit
from andai_core.auth.deps import Services, client_ip, get_services, org_transaction
from andai_core.auth.roles import Caller, require_role
from andai_core.crypto import Keyring
from andai_core.db.models import (
    EXPORT_OPEN_STATES,
    ExportRequest,
    OrgMembership,
    User,
)
from andai_core.dpdp import builder
from andai_core.meetings.service import MeetingError
from andai_core.platform import principals
from andai_core.platform.gateway_client import GatewayError, PlatformGateway, RecordingTooLarge

ADMINS = ("owner", "admin")
MEMBERS = ("owner", "admin", "member")
PER_DAY = 3
DAY = timedelta(hours=24)
MY_LIMIT = 20
PAGE_SIZE = 50
MAX_CURSOR_CHARS = 128
SPOOL_BYTES = 16 * 1024 * 1024
READ_BYTES = 256 * 1024

EXPORT_IN_PROGRESS = MeetingError(
    429, "export_in_progress", "a copy of this person's data is already being made"
)
EXPORT_RATE_LIMITED = MeetingError(
    429, "export_rate_limited", "at most three copies of a person's data a day; try again later"
)
NO_SUCH_MEMBER = MeetingError(404, "no_such_member", "no such member")
NO_SUCH_EXPORT = MeetingError(404, "no_such_export", "no such export")
EXPORT_NOT_READY = MeetingError(409, "export_not_ready", "this copy is not ready to download")
EXPORT_EXPIRED = MeetingError(410, "export_expired", "this copy has expired; ask for a new one")
RECORDING_CHANGED = MeetingError(
    502, "recording_changed", "a recording changed since the copy was made; ask for a new one"
)
PLATFORM_UNAVAILABLE = MeetingError(
    502, "platform_unavailable", "the recordings cannot be read right now; try again shortly"
)
BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this list")


# ─── asking ───────────────────────────────────────────────────────────────────────────────────────


def _lock(tx: Tx, org_id: uuid.UUID, subject: uuid.UUID) -> None:
    tx.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"export:{org_id}:{subject}"},
    )


def request_export(
    tx: Tx,
    *,
    org_id: uuid.UUID,
    subject: uuid.UUID,
    requested_by: uuid.UUID,
    now: datetime,
    ip: str | None = None,
) -> ExportRequest:
    """Insert one pending request (the transaction's org) and audit it."""
    _lock(tx, org_id, subject)
    member = tx.scalar(
        select(OrgMembership.user_id).where(
            OrgMembership.org_id == org_id, OrgMembership.user_id == subject
        )
    )
    if member is None:
        raise NO_SUCH_MEMBER
    mine = select(ExportRequest).where(
        ExportRequest.org_id == org_id, ExportRequest.subject_user_id == subject
    )
    if tx.scalar(mine.where(ExportRequest.state.in_(EXPORT_OPEN_STATES)).limit(1)) is not None:
        raise EXPORT_IN_PROGRESS
    today = tx.scalar(
        select(func.count())
        .select_from(ExportRequest)
        .where(
            ExportRequest.org_id == org_id,
            ExportRequest.subject_user_id == subject,
            ExportRequest.created_at > now - DAY,
        )
    )
    if today >= PER_DAY:
        raise EXPORT_RATE_LIMITED
    request = ExportRequest(
        id=uuid.uuid4(),
        org_id=org_id,
        subject_user_id=subject,
        requested_by=requested_by,
        source="self" if requested_by == subject else "org_admin",
        state="pending",
        attempts=0,
        created_at=now,
    )
    try:
        with tx.begin_nested():
            tx.add(request)
            tx.flush()
    except IntegrityError:
        raise EXPORT_IN_PROGRESS from None
    audit.record(tx, org_id, requested_by, "user.export.request", f"user:{subject}", ip)
    return request


def may_download(request: ExportRequest, caller: Caller) -> bool:
    """The person whose data it is, or the owner or admin who asked for it."""
    me = caller.session.user_id
    if me == request.subject_user_id:
        return True
    return me == request.requested_by and caller.role in ADMINS


def view(request: ExportRequest, caller: Caller, name: str | None = None) -> dict:
    out = {
        "id": str(request.id),
        "subjectId": str(request.subject_user_id),
        "source": request.source,
        "state": request.state,
        "error": request.error,
        "createdAt": request.created_at.isoformat(),
        "readyAt": None if request.ready_at is None else request.ready_at.isoformat(),
        "expiresAt": None if request.expires_at is None else request.expires_at.isoformat(),
        "bytes": request.bundle_bytes,
        "canDownload": request.state == "ready" and may_download(request, caller),
    }
    if name is not None:
        out["subjectName"] = name
    return out


def encode_cursor(at: datetime, request_id: uuid.UUID) -> str:
    raw = f"{at.isoformat()}|{request_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise BAD_CURSOR
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, request_id = raw.split("|")
        position = (datetime.fromisoformat(at), uuid.UUID(request_id))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BAD_CURSOR from None
    if position[0].tzinfo is None:
        raise BAD_CURSOR
    return position


# ─── downloading ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class _Download:
    org_id: uuid.UUID
    request_id: uuid.UUID
    subject: uuid.UUID
    created_at: datetime
    ready_at: datetime
    ciphertext: str
    key_id: str
    recordings: list[builder.Recording]
    key: str | None


class _Changed(Exception):
    """A recording is not what the manifest says."""


async def write_zip(
    target,
    files: dict[str, bytes],
    recordings: list[builder.Recording],
    gateway: PlatformGateway | None,
    key: str | None,
    at: datetime,
) -> None:
    """Write the bundle into `target`: the JSON files (manifest first, deflated), then each
    recording streamed from the platform (stored) and checked against its size and SHA-256."""
    stamp = at.timetuple()[:6]
    archive = zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
    try:
        ordered = ["manifest.json", *(p for p in files if p != "manifest.json")]
        for path in ordered:
            info = zipfile.ZipInfo(path, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            await anyio.to_thread.run_sync(archive.writestr, info, files[path])
        for recording in recordings:
            if gateway is None or key is None:
                raise _Changed(recording.file)
            info = zipfile.ZipInfo(recording.file, date_time=stamp)
            info.compress_type = zipfile.ZIP_STORED
            info.file_size = recording.bytes
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "w", force_zip64=recording.bytes > 2**31) as dest:
                try:
                    async for chunk in gateway.stream_recording(
                        key,
                        recording.recording_id,
                        media_type=recording.kind,
                        max_bytes=recording.bytes,
                    ):
                        digest.update(chunk)
                        size += len(chunk)
                        await anyio.to_thread.run_sync(dest.write, chunk)
                except RecordingTooLarge:
                    raise _Changed(recording.file) from None
                except GatewayError as exc:
                    if exc.status == 404:  # the recording or its master is gone
                        raise _Changed(recording.file) from None
                    raise
            if size != recording.bytes or digest.hexdigest() != recording.sha256:
                raise _Changed(recording.file)
    finally:
        await anyio.to_thread.run_sync(archive.close)


def _chunks(spool) -> Iterator[bytes]:
    try:
        spool.seek(0)
        while chunk := spool.read(READ_BYTES):
            yield chunk
    finally:
        spool.close()


def _audit_download(
    services: Services, caller: Caller, download: _Download, ip: str | None, *, ok: bool
) -> None:
    target = f"user:{download.subject}"
    actor = caller.session.user_id
    with org_transaction(services, caller.session) as tx:
        if ok:
            audit.record(tx, download.org_id, actor, "user.export.download", target, ip)
        else:
            audit.record(tx, download.org_id, actor, "user.export.download_failed", target, ip)


# ─── routes ───────────────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1")


@router.post("/me/exports", status_code=202)
def post_my_export(
    caller: Caller = Depends(require_role(*MEMBERS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        request = request_export(
            tx,
            org_id=caller.session.org_id,
            subject=caller.session.user_id,
            requested_by=caller.session.user_id,
            now=services.clock(),
            ip=ip,
        )
        return view(request, caller)


@router.post("/org/users/{user_id}/exports", status_code=202)
def post_member_export(
    user_id: uuid.UUID,
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        request = request_export(
            tx,
            org_id=caller.session.org_id,
            subject=user_id,
            requested_by=caller.session.user_id,
            now=services.clock(),
            ip=ip,
        )
        name = tx.scalar(select(User.display_name).where(User.id == user_id))
        return view(request, caller, name)


@router.get("/me/exports")
def get_my_exports(
    caller: Caller = Depends(require_role(*MEMBERS)),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        rows = tx.scalars(
            select(ExportRequest)
            .where(
                ExportRequest.org_id == caller.session.org_id,
                ExportRequest.subject_user_id == caller.session.user_id,
            )
            .order_by(ExportRequest.created_at.desc(), ExportRequest.id.desc())
            .limit(MY_LIMIT)
        ).all()
        return {"items": [view(r, caller) for r in rows]}


@router.get("/org/exports")
def get_org_exports(
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_CHARS),
    caller: Caller = Depends(require_role(*ADMINS)),
    services: Services = Depends(get_services),
) -> dict:
    with org_transaction(services, caller.session) as tx:
        query = (
            select(ExportRequest, User.display_name)
            .join(User, User.id == ExportRequest.subject_user_id)
            .where(
                ExportRequest.org_id == caller.session.org_id,
                ExportRequest.source == "org_admin",
            )
        )
        if cursor is not None:
            at, request_id = decode_cursor(cursor)
            query = query.where(
                func.row(ExportRequest.created_at, ExportRequest.id) < func.row(at, request_id)
            )
        rows = tx.execute(
            query.order_by(ExportRequest.created_at.desc(), ExportRequest.id.desc()).limit(
                PAGE_SIZE + 1
            )
        ).all()
        page = rows[:PAGE_SIZE]
        nxt = (
            encode_cursor(page[-1][0].created_at, page[-1][0].id)
            if len(rows) > PAGE_SIZE
            else None
        )
        return {"items": [view(r, caller, name) for r, name in page], "next": nxt}


@router.get("/exports/{export_id}/download")
async def download(
    export_id: uuid.UUID,
    caller: Caller = Depends(require_role(*MEMBERS)),
    services: Services = Depends(get_services),
    ip: str | None = Depends(client_ip),
) -> StreamingResponse:
    now = services.clock()

    def load() -> _Download:
        with org_transaction(services, caller.session) as tx:
            request = tx.scalar(
                select(ExportRequest).where(
                    ExportRequest.id == export_id,
                    ExportRequest.org_id == caller.session.org_id,
                )
            )
            if request is None or not may_download(request, caller):
                raise NO_SUCH_EXPORT
            return _loaded(tx, services.keyring, caller, request, now)

    found = await anyio.to_thread.run_sync(load)
    files = await anyio.to_thread.run_sync(
        lambda: builder.open_part(
            services.keyring, found.org_id, found.request_id, found.ciphertext, found.key_id
        )
    )
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
    try:
        await write_zip(spool, files, found.recordings, services.gateway, found.key, found.ready_at)
    except (_Changed, GatewayError) as exc:
        spool.close()
        await anyio.to_thread.run_sync(
            lambda: _audit_download(services, caller, found, ip, ok=False)
        )
        raise RECORDING_CHANGED if isinstance(exc, _Changed) else PLATFORM_UNAVAILABLE from None
    except BaseException:
        spool.close()
        raise
    await anyio.to_thread.run_sync(
        lambda: _audit_download(services, caller, found, ip, ok=True)
    )
    size = spool.tell()
    name = f"munshi-export-{found.created_at.date().isoformat()}.zip"
    return StreamingResponse(
        _chunks(spool),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _loaded(
    tx: Tx, keyring: Keyring, caller: Caller, request: ExportRequest, now: datetime
) -> _Download:
    if request.state == "expired" or (
        request.state == "ready" and request.expires_at is not None and request.expires_at <= now
    ):
        raise EXPORT_EXPIRED
    if request.state != "ready" or request.bundle_ciphertext is None or request.key_id is None:
        raise EXPORT_NOT_READY
    recordings = [builder.Recording.from_stored(r) for r in request.recordings or []]
    key = principals.open_key(tx, keyring, caller.session.org_id) if recordings else None
    return _Download(
        org_id=caller.session.org_id,
        request_id=request.id,
        subject=request.subject_user_id,
        created_at=request.created_at,
        ready_at=request.ready_at or request.created_at,
        ciphertext=request.bundle_ciphertext,
        key_id=request.key_id,
        recordings=recordings,
        key=key,
    )
