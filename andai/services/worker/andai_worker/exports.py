"""Data exports: build every copy of a person's data that was asked for, and delete expired bundles
(OPS-02, DPDP access request).

* `ExportRunner` — every `TICK_S` seconds it calls `andai_claim_exports(BATCH, now)` (migration
  0020), the only cross-org read it makes: open requests of any org that are due, set `running`
  and leased (`FOR UPDATE SKIP LOCKED`), so two workers never build one request at once. Each is
  built by andai-core's `dpdp.builder.run` under its org: read the person's data, stream each
  recording once for its size and SHA-256, seal the JSON part and mark the request `ready`; retries
  and `failed` are the builder's.
* `ExpiryRunner` — every `EXPIRY_TICK_S` seconds (hourly) `andai_expire_exports(now)` turns every
  ready request past its `expires_at` (`ANDAI_EXPORT_RETENTION_DAYS` after it was built) `expired`
  and deletes its sealed bundle, in every org.

A request that raises here is logged by id and the loop goes on; its lease runs out and it is
claimed again.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio.to_thread
from sqlalchemy import Engine, text

from andai_core.crypto import Keyring
from andai_core.db.session import transaction
from andai_core.dpdp import builder
from andai_core.platform.gateway_client import PlatformGateway
from andai_core.settings import Settings

log = logging.getLogger(__name__)

TICK_S = 10.0
EXPIRY_TICK_S = 3600.0
BATCH = 2


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class TickResult:
    claimed: int = 0
    ready: int = 0
    running: int = 0
    failed: int = 0


class ExportRunner:
    def __init__(
        self,
        engine: Engine,
        keyring: Keyring,
        gateway: PlatformGateway,
        settings: Settings,
        *,
        batch: int = BATCH,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.keyring = keyring
        self.gateway = gateway
        self.settings = settings
        self.batch = batch
        self.clock = clock

    def claim(self) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(org id, request id) of the requests this runner now holds a lease on."""
        with transaction(self.engine) as tx:  # andai_app with no org context
            rows = tx.execute(
                text("SELECT org_id, request_id FROM andai_claim_exports(:n, :at)"),
                {"n": self.batch, "at": self.clock()},
            ).all()
        return [(row.org_id, row.request_id) for row in rows]

    async def tick(self) -> TickResult:
        result = TickResult()
        for org_id, request_id in await anyio.to_thread.run_sync(self.claim):
            result.claimed += 1
            try:
                state = await builder.run(
                    self.engine,
                    self.keyring,
                    self.gateway,
                    org_id,
                    request_id,
                    settings=self.settings,
                    clock=self.clock,
                )
            except Exception:  # noqa: BLE001 - one request must not stop the others
                # Not even the failed attempt could be counted (the database is unreachable): the
                # lease runs out and the request is claimed again.
                log.exception("export %s: run failed", request_id)
                result.running += 1
                continue
            if state == "ready":
                result.ready += 1
            elif state == "failed":
                result.failed += 1
            elif state == "running":
                result.running += 1
        return result

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("export pass failed")
            await asyncio.sleep(every)


class ExpiryRunner:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = utcnow) -> None:
        self.engine = engine
        self.clock = clock

    def expire(self) -> int:
        """Expire every ready bundle past its time, in every org; the number expired."""
        with transaction(self.engine) as tx:
            return int(
                tx.execute(
                    text("SELECT andai_expire_exports(:at)"), {"at": self.clock()}
                ).scalar_one()
            )

    async def run(self, every: float = EXPIRY_TICK_S) -> None:
        while True:
            try:
                expired = await anyio.to_thread.run_sync(self.expire)
                if expired:
                    log.info("expired %d data export bundle(s)", expired)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                log.exception("export expiry pass failed")
            await asyncio.sleep(every)
