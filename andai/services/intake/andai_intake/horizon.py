"""The horizon top-up: scheduled meetings for the occurrences of recurring invites that come within
the horizon (D-58: `ANDAI_INTAKE_HORIZON_DAYS`, 14 days).

A series is scheduled only as far as the horizon when its invite arrives (`events.py`). Every
`TICK_S` (an hour) this runner asks `andai_invite_series_orgs()` (migration 0015) which orgs have an
active recurring event, then, as each org, takes every active series through the same
`events.apply` path as a message, with no message: occurrences that are now within the horizon get
their meeting. It is idempotent — the group lock and the one meeting per (event, occurrence) mean a
second run, or a run alongside the processor, adds nothing twice.

`bot.autoJoin` applies here too, for the series' team as it is now (D-57): `off` adds nothing, and
`only_when_invited` adds nothing to a series that reached the notetaker by an auto-forward. A
cancelled series adds nothing.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import Engine, select, text

from andai_core.db.models import InviteEvent
from andai_core.db.session import transaction
from andai_core.org.bot_settings import effective_auto_join
from andai_intake import events
from andai_intake.invite.recur import HORIZON
from andai_intake.processor import load_group, lock_group, may_create, utcnow, write_plan

TICK_S = 3600.0

log = logging.getLogger(__name__)


class HorizonRunner:
    def __init__(
        self,
        engine: Engine,
        *,
        language: str,
        horizon: timedelta = HORIZON,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.language = language
        self.horizon = horizon
        self.clock = clock

    def orgs(self) -> list[uuid.UUID]:
        with transaction(self.engine) as tx:
            return [
                uuid.UUID(str(v)) for v in tx.scalars(text("SELECT andai_invite_series_orgs()"))
            ]

    def top_up_org(self, org_id: uuid.UUID) -> int:
        """Add the meetings of `org_id`'s active series that are now due; returns how many."""
        now = self.clock()
        with transaction(self.engine, org_id=org_id) as tx:
            uids = list(
                tx.scalars(
                    select(InviteEvent.uid_norm)
                    .where(
                        InviteEvent.org_id == org_id,
                        InviteEvent.state == events.ACTIVE,
                        InviteEvent.recurring.is_(True),
                        InviteEvent.recurrence_id.is_(None),
                    )
                    .order_by(InviteEvent.uid_norm)
                )
            )
        created = 0
        for uid_norm in uids:
            with transaction(self.engine, org_id=org_id) as tx:
                lock_group(tx, org_id, uid_norm)
                state = load_group(tx, org_id, uid_norm)
                master = next((e for e in state.events if e.recurrence_id is None), None)
                if master is None or master.state != events.ACTIVE or not master.recurring:
                    continue
                mode = effective_auto_join(tx, org_id, master.team_id)
                allowed, _ = may_create(mode, master.auto_forwarded)
                plan = events.apply(state, (), None, now, self.horizon, may_create=allowed)
                if plan.writes:
                    write_plan(tx, org_id, plan, now=now, language=self.language)
                created += len(plan.creates)
        return created

    def run_once(self) -> int:
        return sum(self.top_up_org(org_id) for org_id in self.orgs())

    async def run(self, every: float = TICK_S) -> None:
        while True:
            try:
                created = await asyncio.to_thread(self.run_once)
                if created:
                    log.info("invite horizon: %d meeting(s) scheduled", created)
            except Exception as exc:  # the next tick retries; the error type only, no content
                log.warning("invite horizon top-up failed: %s", type(exc).__name__)
            await asyncio.sleep(every)
