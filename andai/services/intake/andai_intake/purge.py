"""Retention of the notetaker mailbox (D-58): raw message bytes go after 7 days, the whole
`inbound_mail` row (the quarantine record with sender, trimmed subject, reason and check summary)
after 30. Every `TICK_S` seconds `andai_purge_inbound` (migration 0012) does both in one
transaction, against the database clock."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import Engine, text

from andai_core.db.session import transaction

TICK_S = 600.0

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Purged:
    raw_purged: int
    rows_deleted: int


def purge_once(engine: Engine, *, raw_days: int, row_days: int) -> Purged:
    with transaction(engine) as tx:
        row = tx.execute(
            text("SELECT raw_purged, rows_deleted FROM andai_purge_inbound(:raw, :rows)"),
            {"raw": raw_days, "rows": row_days},
        ).one()
    return Purged(raw_purged=row.raw_purged, rows_deleted=row.rows_deleted)


class PurgeRunner:
    def __init__(self, engine: Engine, *, raw_days: int, row_days: int) -> None:
        self.engine = engine
        self.raw_days = raw_days
        self.row_days = row_days

    async def tick(self) -> Purged:
        return await asyncio.to_thread(
            purge_once, self.engine, raw_days=self.raw_days, row_days=self.row_days
        )

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                purged = await self.tick()
                if purged.raw_purged or purged.rows_deleted:
                    log.info(
                        "inbound mail purge: %d raw bodies, %d rows",
                        purged.raw_purged,
                        purged.rows_deleted,
                    )
            except Exception as exc:  # the next tick retries; the error type only, no content
                log.warning("inbound mail purge failed: %s", type(exc).__name__)
            await asyncio.sleep(every)
