"""Keep andai's meeting rows and consent records in step with the platform, viewer or no viewer.

Every 2 seconds, one runner per deployment (a Postgres advisory lock held on a dedicated
connection; a second andai-core replica waits for it):

1. `andai_open_meeting_orgs()` lists the orgs that have a meeting that is not terminal. It is the
   only cross-org read and returns org ids only (migration 0004); no HTTP route calls it.
2. For each org, a short transaction under that org's `app.org_id` reads its open meetings and
   unseals the org's platform key.
3. Outside any transaction: one `GET /bots` with that key, plus `GET /meetings/{id}` for an open
   meeting the list did not include or that failed (the list leaves out the bot's last error).
4. A second transaction under the org's context applies `status_map` to each meeting and writes
   status, failure reason, end reason and timestamps, and creates or updates the consent record
   (`consent.apply`). A row that reached a terminal state meanwhile (a user's stop) is left alone.

A `dispatching` row with no platform meeting id older than 60 seconds (andai-core stopped between
the insert and the dispatch) becomes `failed` / `internal_error`, so it never blocks its link.
A platform status or reason `status_map` does not know is logged with its raw value.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio.to_thread
from sqlalchemy import Connection, select, text

from andai_core.auth.deps import Services
from andai_core.db.models import TERMINAL_STATUSES, Meeting
from andai_core.db.session import transaction
from andai_core.meetings import consent
from andai_core.meetings.status_map import map_platform
from andai_core.platform import principals
from andai_core.platform.gateway_client import GatewayError

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 2.0
STALE_DISPATCH = timedelta(seconds=60)
# pg_try_advisory_lock key of the one reconciler ("munshi-r").
LOCK_KEY = int.from_bytes(b"munshi-r", "big") & 0x7FFF_FFFF_FFFF_FFFF


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:  # the platform writes UTC without an offset
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True)
class OpenMeeting:
    id: uuid.UUID
    status: str
    platform_meeting_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class Observation:
    meeting_id: uuid.UUID
    seen_status: str
    platform_meeting: dict | None  # None: the platform does not know it (404)


@dataclass
class TickResult:
    orgs: int = 0
    updated: int = 0
    failed_orgs: int = 0


class Reconciler:
    def __init__(self, services: Services, *, interval: float = INTERVAL_SECONDS) -> None:
        if services.gateway is None:
            raise ValueError("the reconciler needs the platform gateway")
        self.services = services
        self.gateway = services.gateway
        self.interval = interval
        self._lock_conn: Connection | None = None

    # ── single runner ──

    def try_lock(self) -> bool:
        """Take the deployment-wide reconciler lock on a dedicated connection; False when another
        runner holds it."""
        if self._lock_conn is not None:
            return True
        conn = self.services.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            held = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        except Exception:
            conn.close()
            raise
        if not held:
            conn.close()
            return False
        self._lock_conn = conn
        return True

    def release(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is None:
            return
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
        except Exception:  # noqa: BLE001 - closing the connection releases it anyway
            pass
        finally:
            conn.close()

    @property
    def holds_lock(self) -> bool:
        return self._lock_conn is not None

    # ── one pass ──

    def open_orgs(self) -> list[uuid.UUID]:
        with transaction(self.services.engine) as tx:
            return [uuid.UUID(str(v)) for v in tx.scalars(text("SELECT andai_open_meeting_orgs()"))]

    def _read(self, org_id: uuid.UUID) -> tuple[list[OpenMeeting], str | None]:
        with transaction(self.services.engine, org_id=org_id) as tx:
            rows = tx.execute(
                select(
                    Meeting.id, Meeting.status, Meeting.platform_meeting_id, Meeting.created_at
                ).where(Meeting.org_id == org_id, Meeting.status.not_in(TERMINAL_STATUSES))
            ).all()
            meetings = [OpenMeeting(*row) for row in rows]
            key = None
            if any(m.platform_meeting_id is not None for m in meetings):
                key = principals.open_key(tx, self.services.keyring, org_id)
        return meetings, key

    async def _observe(self, key: str, meetings: list[OpenMeeting]) -> list[Observation]:
        tracked = [m for m in meetings if m.platform_meeting_id is not None]
        if not tracked:
            return []
        listed = {
            m["id"]: m
            for m in await self.gateway.list_bots(key)
            if isinstance(m.get("id"), int)
        }
        observations = []
        for meeting in tracked:
            record = listed.get(meeting.platform_meeting_id)
            # The list leaves out the bot's last error; a failure is classified on the full record.
            if record is None or record.get("status") == "failed":
                try:
                    record = await self.gateway.get_meeting(key, meeting.platform_meeting_id)
                except GatewayError as exc:
                    if exc.status != 404:
                        raise
                    record = None
            observations.append(Observation(meeting.id, meeting.status, record))
        return observations

    def _write(
        self, org_id: uuid.UUID, meetings: list[OpenMeeting], observations: list[Observation]
    ) -> int:
        now = self.services.clock()
        updated = 0
        with transaction(self.services.engine, org_id=org_id) as tx:
            for stale in meetings:
                if (
                    stale.platform_meeting_id is None
                    and stale.status == "dispatching"
                    and now - stale.created_at >= STALE_DISPATCH
                ):
                    row = tx.get(Meeting, stale.id, with_for_update=True)
                    if row is not None and row.status == "dispatching" and row.platform_meeting_id is None:
                        log.warning("meeting %s: no dispatch answer after 60 s; failed", row.id)
                        row.status, row.failure_reason = "failed", "internal_error"
                        row.ended_at, row.updated_at = now, now
                        updated += 1
            for seen in observations:
                row = tx.get(Meeting, seen.meeting_id, with_for_update=True)
                if row is None or row.status in TERMINAL_STATUSES:
                    continue  # stopped by a user meanwhile: their decision stands
                if self._apply(tx, row, seen.platform_meeting, now):
                    updated += 1
        return updated

    def _apply(self, tx, row: Meeting, platform_meeting: dict | None, now: datetime) -> bool:
        if platform_meeting is None:
            log.warning("meeting %s: the platform has no record of it; failed", row.id)
            platform_meeting = {"status": "failed"}
            mapped_status, reason = "failed", "internal_error"
            mapped = None
        else:
            mapped = map_platform(platform_meeting)
            if mapped.unknown:
                log.warning("meeting %s: unknown platform value %s", row.id, mapped.unknown)
            mapped_status, reason = mapped.status, mapped.failure_reason
        before = (row.status, row.failure_reason, row.end_reason, row.started_at, row.ended_at)
        row.status, row.failure_reason = mapped_status, reason
        if mapped is not None:
            row.end_reason = mapped.end_reason
        started = _parse_time(platform_meeting.get("start_time"))
        if row.started_at is None and (started or (mapped is not None and mapped.admitted)):
            row.started_at = started or now
        if row.status in TERMINAL_STATUSES and row.ended_at is None:
            row.ended_at = _parse_time(platform_meeting.get("end_time")) or now
        changed = before != (
            row.status, row.failure_reason, row.end_reason, row.started_at, row.ended_at
        )
        if mapped is not None:
            record = consent.record_for(tx, row.id)
            consent_before = None if record is None else (
                record.disclosure_channel, record.disclosure_detail, record.removed_at
            )
            record = consent.apply(
                tx,
                row,
                platform_meeting,
                admitted=mapped.admitted,
                removed=mapped.removed,
                terminal=mapped.terminal,
                admitted_at=row.started_at,
                removed_at=row.ended_at,
                now=now,
            )
            if record is not None and consent_before != (
                record.disclosure_channel, record.disclosure_detail, record.removed_at
            ):
                changed = True
        if changed:
            row.updated_at = now
        return changed

    async def reconcile_org(self, org_id: uuid.UUID) -> int:
        meetings, key = await anyio.to_thread.run_sync(lambda: self._read(org_id))
        observations: list[Observation] = []
        if key is not None:
            observations = await self._observe(key, meetings)
        return await anyio.to_thread.run_sync(lambda: self._write(org_id, meetings, observations))

    async def tick(self) -> TickResult:
        result = TickResult()
        org_ids = await anyio.to_thread.run_sync(self.open_orgs)
        result.orgs = len(org_ids)
        for org_id in org_ids:
            try:
                result.updated += await self.reconcile_org(org_id)
            except GatewayError as exc:
                result.failed_orgs += 1
                log.warning("reconciler: org %s skipped this pass: %s", org_id, exc)
        return result

    async def run(self) -> None:
        """Reconcile every `interval` seconds while holding the lock; until cancelled."""
        try:
            while True:
                try:
                    if await anyio.to_thread.run_sync(self.try_lock):
                        await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                    log.exception("reconciler pass failed")
                    await anyio.to_thread.run_sync(self.release)
                await asyncio.sleep(self.interval)
        finally:
            await anyio.to_thread.run_sync(self.release)
