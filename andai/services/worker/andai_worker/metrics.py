"""Join, deletion, minutes, erasure and sealed-secret metrics in Prometheus text format, computed
from andai's own rows on every scrape (the sealed-secret counts at most every 300 s).

The worker connects as `andai_app` and reads meetings across orgs only through
`andai_join_stats(since)` (migration 0005), which returns `(platform, source, outcome, reason,
count)` aggregates and nothing else, `andai_deletion_backlog()` (0009), which returns a count
and a time, and `andai_mom_stats(since)` (0010), which returns two counts and a list of durations;
canary runs come from the org-less `canary_runs` table.

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
* `munshi_deletions_pending` — deleted meetings not yet purged from the platform and andai
  (`pending`, or `failed` after the purge spent its attempts).
* `munshi_deletion_oldest_pending_seconds` — how long ago the oldest of them was deleted (0 when
  there is none); above 900 the 15-minute purge target (PORT-04) is missed.
* `munshi_mom_pending` — finished meetings with a stored transcript whose minutes are still owed
  (`none` or `pending`: not yet claimed, running, retrying or waiting for a regenerate).
* `munshi_mom_failed_total` — meetings whose minutes failed after every attempt (`failed`).
* `munshi_mom_latency_seconds` — histogram of meeting end → first minutes stored, over the meetings
  whose first minutes were stored in the last 24 h (SC2's p95 is read from it).
* `munshi_sealed_values{table,key_id}` — sealed secrets per table still wrapped by each KEK
  (`andai_core.sealed.counts_by_key`; a key id is a truncated hash, not the key). A key rotation
  retires the old key only once its count is 0 (andai/docs/runbooks/key-rotation.md). Read at most
  every 300 s: it visits each org's principal in its own transaction.
* `munshi_pipeline_pending{stage}`, `munshi_pipeline_oldest_pending_seconds{stage}` and
  `munshi_pipeline_failed_24h{stage}` — per stage (`PIPELINE_STAGES`: scheduled dispatch, transcript
  ingest, minutes, recap mail, deletion, intake) the work still owed, how long the oldest item has
  been due (0 when none) and the items that failed for good in the last 24 h, from
  `andai_pipeline_stats(at, lead_s)` (0018).
* `munshi_pipeline_stage_seconds{stage}` — histogram of how long each stage took
  (`DURATION_STAGES`), over the items it finished in the last 24 h (at most 5000 per stage), from
  `andai_pipeline_durations(since, lead_s)` (0018).
* `munshi_intake_messages_24h{status,outcome,reason}` — messages the notetaker mailbox received in
  the last 24 h by status, outcome and quarantine reason ('' for none), org-less ones included, from
  `andai_intake_stats(since)` (0018).
* `munshi_db_size_bytes` — the andai database's size on disk (`pg_database_size`).
* `munshi_erasures_pending`, `munshi_erasure_oldest_pending_seconds` and `munshi_erasures_failed` —
  user erasure requests still open, how long ago the oldest open one was made (0 when none; above
  86400 the 24-hour SLA is missed) and the requests that failed, from `andai_erasure_backlog(at)`
  (0019). Numbers only.

Process metrics (mail delivery, ASR lag) come from `andai_core.metrics` in the worker's own process
registry (`process_registry`); `/metrics` renders both registries.

`join_counts`, `rates` and `canary_summary` are shared with the alert evaluator, so a mail and the
metrics never disagree about the numbers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from prometheus_client import CollectorRegistry
from prometheus_client.core import GaugeMetricFamily, HistogramMetricFamily, Metric
from sqlalchemy import Engine, text

from andai_core import metrics as core_metrics
from andai_core.db.session import transaction
from andai_core.sealed import counts_by_key

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


@dataclass(frozen=True)
class DeletionBacklog:
    pending: int
    oldest_deleted_at: datetime | None


def deletion_backlog(engine: Engine) -> DeletionBacklog:
    """`andai_deletion_backlog()` as `andai_app` with no org context."""
    with transaction(engine) as tx:
        row = tx.execute(
            text("SELECT pending, oldest_deleted_at FROM andai_deletion_backlog()")
        ).one()
    return DeletionBacklog(int(row.pending), row.oldest_deleted_at)


class DeletionCollector:
    """Deleted meetings still waiting for their purge, read on every scrape."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        backlog = deletion_backlog(self.engine)
        yield GaugeMetricFamily(
            "munshi_deletions_pending",
            "Deleted meetings not yet purged from the platform and andai (pending or failed).",
            value=backlog.pending,
        )
        age = 0.0
        if backlog.oldest_deleted_at is not None:
            age = max(0.0, (self.clock() - backlog.oldest_deleted_at).total_seconds())
        yield GaugeMetricFamily(
            "munshi_deletion_oldest_pending_seconds",
            "Seconds since the oldest deleted, not yet purged meeting was deleted (0 when none).",
            value=age,
        )


# End → first minutes, in seconds (SC2: p95 at most 10 minutes).
MOM_LATENCY_BUCKETS = (30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0)


@dataclass(frozen=True)
class MomStats:
    pending: int
    failed: int
    latencies: list[float]


def mom_stats(engine: Engine, since: datetime) -> MomStats:
    """`andai_mom_stats(since)` as `andai_app` with no org context."""
    with transaction(engine) as tx:
        row = tx.execute(
            text("SELECT pending, failed, latencies FROM andai_mom_stats(:since)"),
            {"since": since},
        ).one()
    return MomStats(int(row.pending), int(row.failed), [float(v) for v in row.latencies or []])


class MomCollector:
    """Minutes still owed, failed, and how long they took, read on every scrape."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        stats = mom_stats(self.engine, self.clock() - WINDOW)
        yield GaugeMetricFamily(
            "munshi_mom_pending",
            "Finished meetings with a stored transcript whose minutes are still owed.",
            value=stats.pending,
        )
        yield GaugeMetricFamily(
            "munshi_mom_failed_total",
            "Meetings whose minutes failed after every attempt.",
            value=stats.failed,
        )
        buckets = [
            (str(bound), sum(1 for v in stats.latencies if v <= bound))
            for bound in MOM_LATENCY_BUCKETS
        ]
        buckets.append(("+Inf", len(stats.latencies)))
        yield HistogramMetricFamily(
            "munshi_mom_latency_seconds",
            "Seconds from meeting end to its first minutes stored, for minutes stored in the last"
            " 24 hours.",
            buckets=buckets,
            sum_value=sum(stats.latencies),
        )


SEALED_CACHE = timedelta(seconds=300)


class SealedCollector:
    """Sealed values per table and KEK id, read from the database at most every 300 s."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock
        self._counts: dict[tuple[str, str], int] | None = None
        self._read_at: datetime | None = None

    def describe(self) -> list[Metric]:
        return []

    def _current(self) -> dict[tuple[str, str], int]:
        now = self.clock()
        stale = self._read_at is None or not (
            timedelta(0) <= now - self._read_at < SEALED_CACHE
        )
        if self._counts is None or stale:
            self._counts = counts_by_key(self.engine)
            self._read_at = now
        return self._counts

    def collect(self) -> Iterator[Metric]:
        family = GaugeMetricFamily(
            "munshi_sealed_values",
            "Sealed values per table still wrapped by each key-encryption key (by key id).",
            labels=["table", "key_id"],
        )
        for (table, key_id), count in sorted(self._current().items()):
            family.add_metric([table, key_id], count)
        yield family


PIPELINE_STAGES = ("dispatch", "ingest", "minutes", "recap", "deletion", "intake")
DURATION_STAGES = ("dispatch", "ingest", "minutes", "recap")
STAGE_BUCKETS = (1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0, 1800.0, 3600.0)
# The worker's join lead when the caller names none (`ANDAI_INTAKE_JOIN_LEAD_S`'s default).
DEFAULT_LEAD_S = 120


@dataclass(frozen=True)
class StageStats:
    stage: str
    pending: int
    oldest_s: float
    failed_24h: int


def pipeline_stats(engine: Engine, at: datetime, lead_s: int) -> list[StageStats]:
    """`andai_pipeline_stats(at, lead_s)` as `andai_app` with no org context."""
    with transaction(engine) as tx:
        rows = tx.execute(
            text(
                "SELECT stage, pending, oldest_s, failed_24h"
                " FROM andai_pipeline_stats(:at, :lead)"
            ),
            {"at": at, "lead": lead_s},
        ).all()
    return [StageStats(s, int(p), float(o), int(f)) for s, p, o, f in rows if s in PIPELINE_STAGES]


def pipeline_durations(engine: Engine, since: datetime, lead_s: int) -> dict[str, list[float]]:
    """`andai_pipeline_durations(since, lead_s)` as `andai_app`: seconds per stage."""
    with transaction(engine) as tx:
        rows = tx.execute(
            text("SELECT stage, seconds FROM andai_pipeline_durations(:since, :lead)"),
            {"since": since, "lead": lead_s},
        ).all()
    found: dict[str, list[float]] = {stage: [] for stage in DURATION_STAGES}
    for stage, seconds in rows:
        if stage in found:
            found[stage].append(float(seconds))
    return found


class PipelineCollector:
    """Backlogs, failures and stage durations of every pipeline, read on every scrape."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime], lead_s: int) -> None:
        self.engine = engine
        self.clock = clock
        self.lead_s = lead_s

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        now = self.clock()
        stats = pipeline_stats(self.engine, now, self.lead_s)
        pending = GaugeMetricFamily(
            "munshi_pipeline_pending",
            "Work still owed per pipeline stage (scheduled dispatch, transcript ingest, minutes,"
            " recap mail, deletion, intake).",
            labels=["stage"],
        )
        oldest = GaugeMetricFamily(
            "munshi_pipeline_oldest_pending_seconds",
            "Seconds since the oldest pending item of the stage became due (0 when none).",
            labels=["stage"],
        )
        failed = GaugeMetricFamily(
            "munshi_pipeline_failed_24h",
            "Items of the stage that failed for good in the last 24 hours.",
            labels=["stage"],
        )
        for row in stats:
            pending.add_metric([row.stage], row.pending)
            oldest.add_metric([row.stage], row.oldest_s)
            failed.add_metric([row.stage], row.failed_24h)
        yield pending
        yield oldest
        yield failed

        durations = pipeline_durations(self.engine, now - WINDOW, self.lead_s)
        histogram = HistogramMetricFamily(
            "munshi_pipeline_stage_seconds",
            "Seconds each stage took, for the items it finished in the last 24 hours (dispatch:"
            " due to recording; ingest: end to transcript; minutes: transcript to first minutes;"
            " recap: first minutes to recap sent).",
            labels=["stage"],
        )
        for stage in DURATION_STAGES:
            values = durations[stage]
            buckets = [
                (str(bound), sum(1 for v in values if v <= bound)) for bound in STAGE_BUCKETS
            ]
            buckets.append(("+Inf", len(values)))
            histogram.add_metric([stage], buckets, sum(values))
        yield histogram


@dataclass(frozen=True)
class IntakeCount:
    status: str
    outcome: str
    reason: str
    count: int


def intake_stats(engine: Engine, since: datetime) -> list[IntakeCount]:
    """`andai_intake_stats(since)` as `andai_app` with no org context."""
    with transaction(engine) as tx:
        rows = tx.execute(
            text("SELECT status, outcome, reason, n FROM andai_intake_stats(:since)"),
            {"since": since},
        ).all()
    return [IntakeCount(s, o, r, int(n)) for s, o, r, n in rows]


class IntakeCollector:
    """Messages the notetaker mailbox received in the last 24 h, read on every scrape."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        family = GaugeMetricFamily(
            "munshi_intake_messages_24h",
            "Messages the notetaker mailbox received in the last 24 hours by status, outcome and"
            " quarantine reason.",
            labels=["status", "outcome", "reason"],
        )
        for c in intake_stats(self.engine, self.clock() - WINDOW):
            family.add_metric([c.status, c.outcome, c.reason], c.count)
        yield family


class DbSizeCollector:
    """The andai database's size on disk, read on every scrape."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        with transaction(self.engine) as tx:
            size = tx.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
        yield GaugeMetricFamily(
            "munshi_db_size_bytes", "Size of the andai database on disk in bytes.", value=size
        )


@dataclass(frozen=True)
class ErasureBacklog:
    pending: int
    oldest_s: float
    failed: int


def erasure_backlog(engine: Engine, at: datetime) -> ErasureBacklog:
    """`andai_erasure_backlog(at)` as `andai_app` with no org context."""
    with transaction(engine) as tx:
        row = tx.execute(
            text("SELECT pending, oldest_s, failed FROM andai_erasure_backlog(:at)"), {"at": at}
        ).one()
    return ErasureBacklog(int(row.pending), float(row.oldest_s), int(row.failed))


class ErasureCollector:
    """User erasure requests still open or failed, read on every scrape."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self.engine = engine
        self.clock = clock

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        backlog = erasure_backlog(self.engine, self.clock())
        yield GaugeMetricFamily(
            "munshi_erasures_pending",
            "User erasure requests not yet done (pending or running).",
            value=backlog.pending,
        )
        yield GaugeMetricFamily(
            "munshi_erasure_oldest_pending_seconds",
            "Seconds since the oldest open user erasure request was made (0 when none).",
            value=backlog.oldest_s,
        )
        yield GaugeMetricFamily(
            "munshi_erasures_failed",
            "User erasure requests that failed and need the operator.",
            value=backlog.failed,
        )


def registry_for(
    engine: Engine, clock: Callable[[], datetime], lead_s: int = DEFAULT_LEAD_S
) -> CollectorRegistry:
    """The database-derived collectors (read on every scrape)."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(JoinCollector(engine, clock))
    registry.register(DeletionCollector(engine, clock))
    registry.register(MomCollector(engine, clock))
    registry.register(SealedCollector(engine, clock))
    registry.register(PipelineCollector(engine, clock, lead_s))
    registry.register(IntakeCollector(engine, clock))
    registry.register(DbSizeCollector(engine))
    registry.register(ErasureCollector(engine, clock))
    return registry


def process_registry() -> CollectorRegistry:
    """This process's own counters: mail delivery and the ASR lag of the transcripts it copies."""
    return core_metrics.service_registry(core_metrics.ASR_LAG, core_metrics.ASR_LAG_MISSING)


render = core_metrics.render
