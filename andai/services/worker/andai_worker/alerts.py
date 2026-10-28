"""Operator alerts by mail when bot joins start failing (BOT-04, D-34).

Every tick one runner per deployment (a Postgres advisory lock held on a dedicated connection, like
andai-core's reconciler) evaluates two incidents per platform:

* `canary_failures` — the latest 2 decided canary runs on the platform both failed. It closes when
  the latest canary run succeeds.
* `join_rate` — the 24 h join success rate of every source together (D-39: success = the bot
  reached `recording`; cancelled, denied and removed meetings are not counted) is below 95% with at
  least 5 counted joins. It closes when the rate is back at 95% or fewer than 5 joins remain.

An incident lives in one `alert_state` row. Opening it owes an `open` mail, closing it a
`recovery` mail (none if the opening mail never went out). The mail goes to `ANDAI_ALERT_EMAIL`
only; the owed mail stays in the row until the relay accepts it, so a relay outage delays a mail
but never loses or repeats it. A mail names the platform, the incident, the latest canary failure
reasons and the 24 h counts; never a meeting link, org or person.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio.to_thread
from sqlalchemy import Connection, Engine, select, text, update

from andai_core.db.models import PLATFORMS, AlertState
from andai_core.db.session import transaction
from andai_core.mail import Mail, Mailer, MailError
from andai_worker.canary import canary_history
from andai_worker.metrics import WINDOW, Rate, canary_summary, join_counts, rates

log = logging.getLogger(__name__)

TICK_S = 60.0
STREAK = 2
RATE_THRESHOLD = 0.95
MIN_JOINS = 5
# pg_try_advisory_lock key of the one alert evaluator ("munshi-a").
LOCK_KEY = int.from_bytes(b"munshi-a", "big") & 0x7FFF_FFFF_FFFF_FFFF

PLATFORM_NAMES = {"google_meet": "Google Meet", "teams": "Microsoft Teams", "zoom": "Zoom"}


@dataclass(frozen=True)
class Firing:
    canary: dict[str, bool]
    rate: dict[str, bool]


@dataclass(frozen=True)
class Notice:
    kind: str
    platform: str
    notice: str  # open | recovery


def _pct(rate: Rate | None) -> str:
    if rate is None or rate.ratio is None:
        return "no counted joins"
    return f"{rate.ratio:.1%} ({rate.successes} of {rate.counted} counted joins)"


class AlertEvaluator:
    def __init__(
        self,
        engine: Engine,
        mailer: Mailer,
        to: str,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.engine = engine
        self.mailer = mailer
        self.to = to
        self.clock = clock
        self._lock_conn: Connection | None = None

    # ── single runner ──

    def try_lock(self) -> bool:
        if self._lock_conn is not None:
            return True
        conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
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

    # ── evaluate ──

    def firing(self) -> Firing:
        summary = canary_summary(self.engine)
        by_platform = rates(join_counts(self.engine, self.clock() - WINDOW))
        canary = {p: summary[p].consecutive_failures >= STREAK for p in summary}
        rate = {}
        for platform in PLATFORMS:
            r = by_platform.get((platform, "all"))
            rate[platform] = (
                r is not None and r.counted >= MIN_JOINS and (r.ratio or 0.0) < RATE_THRESHOLD
            )
        return Firing(canary=canary, rate=rate)

    def evaluate(self) -> list[Notice]:
        """Open and close incidents; returns every mail still owed."""
        now = self.clock()
        fire = self.firing()
        wanted = {("canary_failures", p): on for p, on in fire.canary.items()}
        wanted.update({("join_rate", p): on for p, on in fire.rate.items()})
        with transaction(self.engine) as tx:
            rows = {
                (r.kind, r.platform): r
                for r in tx.scalars(select(AlertState).with_for_update()).all()
            }
            for (kind, platform), on in sorted(wanted.items()):
                row = rows.get((kind, platform))
                if on and (row is None or not row.open):
                    if row is None:
                        row = AlertState(kind=kind, platform=platform)
                        tx.add(row)
                        rows[(kind, platform)] = row
                    row.open, row.opened_at, row.closed_at = True, now, None
                    row.notice, row.updated_at = "open", now
                    log.warning("alert opened: %s on %s", kind, platform)
                elif not on and row is not None and row.open:
                    row.open, row.closed_at, row.updated_at = False, now, now
                    # Nothing to recover from if the opening mail never went out.
                    row.notice = "recovery" if row.notice is None else None
                    log.warning("alert closed: %s on %s", kind, platform)
            tx.flush()
            return [
                Notice(r.kind, r.platform, r.notice)
                for r in sorted(rows.values(), key=lambda r: (r.kind, r.platform))
                if r.notice is not None
            ]

    def _sent(self, notice: Notice) -> None:
        with transaction(self.engine) as tx:
            tx.execute(
                update(AlertState)
                .where(
                    AlertState.kind == notice.kind,
                    AlertState.platform == notice.platform,
                    AlertState.notice == notice.notice,
                )
                .values(notice=None, updated_at=self.clock())
            )

    # ── mail ──

    def compose(self, notice: Notice) -> Mail:
        name = PLATFORM_NAMES.get(notice.platform, notice.platform)
        rate = rates(join_counts(self.engine, self.clock() - WINDOW)).get((notice.platform, "all"))
        history = canary_history(self.engine, notice.platform)
        lines = [
            f"{started.astimezone(UTC):%Y-%m-%d %H:%M} UTC  "
            f"{outcome}{'' if reason is None else ' - ' + reason}"
            for started, outcome, reason in history
        ]
        canary_lines = "\n".join(f"  {line}" for line in lines) or "  (no canary runs yet)"
        if notice.kind == "canary_failures":
            what = f"{STREAK} or more canary joins in a row failed on {name}"
            back = f"the latest canary join on {name} succeeded"
        else:
            what = (
                f"the 24 h join success rate on {name} is below {RATE_THRESHOLD:.0%}"
                f" (with at least {MIN_JOINS} counted joins)"
            )
            back = f"the 24 h join success rate on {name} is back at {RATE_THRESHOLD:.0%} or above"
        if notice.notice == "open":
            subject = f"[munshi.ai] Bot joins failing on {name}"
            opening = f"munshi.ai alert: {what}."
        else:
            subject = f"[munshi.ai] Recovered: bot joins on {name}"
            opening = f"munshi.ai recovery: {back}."
        body = (
            f"{opening}\n\n"
            f"Platform: {name}\n"
            f"Incident: {notice.kind}\n"
            f"24 h join success rate (all sources): {_pct(rate)}\n\n"
            f"Latest canary runs (latest first):\n{canary_lines}\n\n"
            "The join metrics are at andai-worker /metrics. What to check is in the canaries runbook"
            " (andai/docs/runbooks/canaries.md); a bot-only fix ships as in"
            " andai/docs/runbooks/bot-hotfix.md.\n"
        )
        return Mail(to=self.to, subject=subject, text=body)

    async def notify(self, notices: list[Notice]) -> int:
        sent = 0
        for notice in notices:
            mail = await anyio.to_thread.run_sync(lambda: self.compose(notice))
            try:
                await self.mailer.send(mail)
            except MailError:
                log.warning("alert mail for %s on %s not sent; retried next pass",
                            notice.kind, notice.platform)
                continue
            await anyio.to_thread.run_sync(lambda: self._sent(notice))
            sent += 1
        return sent

    async def tick(self) -> int:
        if not await anyio.to_thread.run_sync(self.try_lock):
            return 0
        notices = await anyio.to_thread.run_sync(self.evaluate)
        return await self.notify(notices)

    async def run(self, every: float = TICK_S) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                    log.exception("alert pass failed")
                    await anyio.to_thread.run_sync(self.release)
                await asyncio.sleep(every)
        finally:
            await anyio.to_thread.run_sync(self.release)
