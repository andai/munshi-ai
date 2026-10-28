"""Join metrics in Prometheus text format, computed from andai's own rows on every scrape.

The worker connects as `andai_app` and reads meetings across orgs only through
`andai_join_stats(since)` (migration 0005), which returns `(platform, source, outcome, reason,
count)` aggregates and nothing else; canary runs come from the org-less `canary_runs` table.

Exported series (labels are enums only: no link, name, email, org or id ever becomes a label):

* `munshi_bot_joins_total{platform,source,outcome,reason}` — bot joins started in the last 24 h.
  `outcome` is `success` (the bot reached `recording`), `failure` (with its `JoinFailureReason`),
  `excluded` (cancelled by its user, denied by the host, removed by participants; D-39) or
  `pending` (still on its way in).
* `munshi_join_success_ratio_24h{platform,source}` — successes / (successes + failures) over 24 h,
  per source and for `source="all"` (what the alert uses); absent while nothing was counted.
* `munshi_canary_consecutive_failures{platform}` — canary failures in a row up to the latest run.
* `munshi_canary_last_run_timestamp_seconds{platform,outcome}` — start of the latest canary run
  with that outcome.

`join_counts`, `rates` and `canary_summary` are shared with the alert evaluator, so a mail and the
metrics never disagree about the numbers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from sqlalchemy import Engine, text

from andai_core.db.session import transaction

WINDOW = timedelta(hours=24)
ALL_SOURCES = "all"
# Canary runs read for the streak and the last-run times (about six a day per platform).
CANARY_HISTORY = 200


@dataclass(frozen=True)
class JoinCount:
    platform: str
    source: str
    outcome: str
    reason: str | None
    count: int


def join_counts(engine: Engine, since: datetime) -> list[JoinCount]:
    """`andai_join_stats(since)` as `andai_app` with no org context."""
    with transaction(engine) as tx:
        rows = tx.execute(
            text(
                "SELECT platform, source, outcome, reason, count FROM andai_join_stats(:since)"
            ),
            {"since": since},
        ).all()
    return [JoinCount(p, s, o, r, int(c)) for p, s, o, r, c in rows]


@dataclass(frozen=True)
class Rate:
    successes: int
    failures: int

    @property
    def counted(self) -> int:
        return self.successes + self.failures

    @property
    def ratio(self) -> float | None:
        return None if self.counted == 0 else self.successes / self.counted


def rates(counts: list[JoinCount]) -> dict[tuple[str, str], Rate]:
    """The D-39 success rate per (platform, source), and per (platform, "all")."""
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for c in counts:
        if c.outcome not in ("success", "failure"):
            continue
        slot = 0 if c.outcome == "success" else 1
        for key in ((c.platform, c.source), (c.platform, ALL_SOURCES)):
            tally[key][slot] += c.count
    return {key: Rate(s, f) for key, (s, f) in tally.items()}


@dataclass
class CanaryPlatform:
    consecutive_failures: int = 0
    # Failure reasons of the current streak, latest first, with their start times.
    streak: list[tuple[datetime, str]] = field(default_factory=list)
    last_run: dict[str, datetime] = field(default_factory=dict)
    latest_outcome: str | None = None


def canary_summary(engine: Engine) -> dict[str, CanaryPlatform]:
    """Per platform: the failure streak up to the latest decided canary run (a run is decided once
    its bot was admitted or failed), and the latest start time per outcome."""
    with transaction(engine) as tx:
        rows = tx.execute(
            text(
                "SELECT platform, outcome, reason, started_at FROM canary_runs"
                " WHERE outcome IS NOT NULL AND started_at IS NOT NULL"
                " ORDER BY started_at DESC LIMIT :n"
            ),
            {"n": CANARY_HISTORY},
        ).all()
    summary: dict[str, CanaryPlatform] = {}
    streak_open: dict[str, bool] = {}
    for platform, outcome, reason, started_at in rows:
        entry = summary.setdefault(platform, CanaryPlatform())
        if entry.latest_outcome is None:
            entry.latest_outcome = outcome
            streak_open[platform] = True
        entry.last_run.setdefault(outcome, started_at)
        if streak_open[platform]:
            if outcome == "failure":
                entry.consecutive_failures += 1
                entry.streak.append((started_at, reason or "internal_error"))
            else:
                streak_open[platform] = False
    return summary


class JoinCollector:
    """A custom collector: every scrape reads the current numbers from the database."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock

    def describe(self) -> list[Metric]:
        return []  # registering must not touch the database

    def collect(self) -> Iterator[Metric]:
        counts = join_counts(self.engine, self.clock() - WINDOW)

        joins = GaugeMetricFamily(
            "munshi_bot_joins_total",
            "Bot joins started in the last 24 hours by platform, source, outcome"
            " (success, failure, excluded, pending) and reason.",
            labels=["platform", "source", "outcome", "reason"],
        )
        for c in sorted(counts, key=lambda c: (c.platform, c.source, c.outcome, c.reason or "")):
            joins.add_metric([c.platform, c.source, c.outcome, c.reason or ""], c.count)
        yield joins

        ratio = GaugeMetricFamily(
            "munshi_join_success_ratio_24h",
            "Share of counted bot joins (success or failure, D-39) that reached recording in the"
            " last 24 hours; source=\"all\" is every source together.",
            labels=["platform", "source"],
        )
        for (platform, source), rate in sorted(rates(counts).items()):
            if rate.ratio is not None:
                ratio.add_metric([platform, source], rate.ratio)
        yield ratio

        summary = canary_summary(self.engine)
        streak = GaugeMetricFamily(
            "munshi_canary_consecutive_failures",
            "Canary joins that failed in a row up to the latest canary run.",
            labels=["platform"],
        )
        last = GaugeMetricFamily(
            "munshi_canary_last_run_timestamp_seconds",
            "Start time of the latest canary run with this outcome (Unix seconds).",
            labels=["platform", "outcome"],
        )
        for platform in sorted(summary):
            entry = summary[platform]
            streak.add_metric([platform], entry.consecutive_failures)
            for outcome in sorted(entry.last_run):
                last.add_metric([platform, outcome], entry.last_run[outcome].timestamp())
        yield streak
        yield last


def registry_for(engine: Engine, clock: Callable[[], datetime]) -> CollectorRegistry:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(JoinCollector(engine, clock))
    return registry


def render(registry: CollectorRegistry) -> bytes:
    return generate_latest(registry)
