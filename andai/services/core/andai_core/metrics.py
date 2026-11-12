"""Process metrics shared by andai's services, and the rule every munshi metric follows.

Each service (andai-core, andai-worker, andai-intake) serves `GET /metrics` from its own
`CollectorRegistry` (`service_registry()`); a series counted here is per process and Prometheus sums
the processes. Database-derived gauges live in andai-worker only (`andai_worker.metrics`), so they
are scraped once. The full catalogue is `andai/docs/metrics.md`.

* `munshi_mail_sent_total{kind,outcome}` — mail handed to the SMTP relay (`mail.SmtpMailer`), by
  `kind` (`MAIL_KINDS`) and `outcome` (`sent`: the relay accepted it; `failed`: it did not).
* `munshi_asr_segment_lag_seconds` — for each transcript segment andai copies, the platform's write
  time of the segment (`created_at`) minus the segment's spoken end (`absolute_end_time`): how long
  speech-to-text and the platform's store took. Observed at ingest (`meetings/ingest.py`).
* `munshi_asr_segment_lag_missing_total` — segments that lacked one of the two times, so no lag was
  observed (counted, never read as zero).

**Labels never carry identity or content.** A label name is one of `LABEL_ALLOWLIST`; a label value
comes from a closed set in the code or a CHECK constraint (an enum), never from a user, a meeting,
an org, a team, an email, a link, an IP, a title or any free text. `LABEL_VALUE` is the shape every
value has; `andai_worker/tests/test_metrics_hygiene.py` scrapes all three services after seeding
identity-bearing data and fails on any violation.

`*_created` series are not exported (`disable_created_metrics`): they double every counter for no
use here.
"""

from __future__ import annotations

import re

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.registry import Collector

disable_created_metrics()

# Every label name a munshi metric may use (`le` is a histogram bucket bound).
LABEL_ALLOWLIST = frozenset(
    {
        "platform",
        "source",
        "outcome",
        "reason",
        "stage",
        "kind",
        "result",
        "status",
        "table",
        "key_id",
        "le",
    }
)
# The shape of every label value: short, lowercase, no spaces, no `@` or `/`.
LABEL_VALUE = re.compile(r"^[a-z0-9_.:+-]{0,40}$")

MAIL_KINDS = ("invite", "password_reset", "recap", "alert", "confirmation", "other")
MAIL_OUTCOMES = ("sent", "failed")

MAIL_SENT = Counter(
    "munshi_mail_sent",
    "Mail handed to the SMTP relay by kind and outcome (sent: accepted; failed: refused or"
    " unreachable).",
    ["kind", "outcome"],
    registry=None,
)
for _kind in MAIL_KINDS:
    for _outcome in MAIL_OUTCOMES:
        MAIL_SENT.labels(_kind, _outcome)

# Speech end → the platform's segment write, in seconds.
ASR_LAG_BUCKETS = (0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0, 30.0, 60.0)
ASR_LAG = Histogram(
    "munshi_asr_segment_lag_seconds",
    "Seconds from a transcript segment's spoken end to the platform's write of it, observed when"
    " andai copies the transcript.",
    buckets=ASR_LAG_BUCKETS,
    registry=None,
)
ASR_LAG_MISSING = Counter(
    "munshi_asr_segment_lag_missing",
    "Transcript segments copied without both a write time and a spoken end (no lag observed).",
    registry=None,
)


def count_mail(kind: str, outcome: str) -> None:
    """Count one mail; an unknown kind counts as `other`, an unknown outcome is a bug."""
    if outcome not in MAIL_OUTCOMES:
        raise ValueError(f"unknown mail outcome: {outcome!r}")
    MAIL_SENT.labels(kind if kind in MAIL_KINDS else "other", outcome).inc()


def service_registry(*collectors: Collector) -> CollectorRegistry:
    """A service's own registry: the mail counter, then `collectors`."""
    registry = CollectorRegistry(auto_describe=True)
    registry.register(MAIL_SENT)
    for collector in collectors:
        registry.register(collector)
    return registry


def render(*registries: CollectorRegistry) -> bytes:
    """The Prometheus text of `registries`, one after another."""
    return b"".join(generate_latest(registry) for registry in registries)


__all__ = [
    "ASR_LAG",
    "ASR_LAG_BUCKETS",
    "ASR_LAG_MISSING",
    "LABEL_ALLOWLIST",
    "LABEL_VALUE",
    "MAIL_KINDS",
    "MAIL_OUTCOMES",
    "MAIL_SENT",
    "count_mail",
    "render",
    "service_registry",
]
