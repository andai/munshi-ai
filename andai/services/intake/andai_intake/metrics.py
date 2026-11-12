"""andai-intake's metrics, served at `GET /metrics` (`main.py`) from the service's own registry.

* `munshi_intake_smtp_total{result}` — the SMTP receiver's verdicts (`smtp.py`), one per recipient
  refused and one per message: `accept` (stored, answered 250), `unknown_rcpt` (550, not a notetaker
  address), `too_many_rcpt` (452, past `MAX_RECIPIENTS`), `too_large` (552, a declared SIZE or DATA
  past the limit) and `store_failed` (451, not stored; the sender retries).
* `munshi_mail_sent_total{kind,outcome}` (`andai_core.metrics`) — the confirmations mailed to
  senders count under `kind="confirmation"`.

What came of each stored message (status, outcome, quarantine reason) is read from the database by
andai-worker (`munshi_intake_messages_24h`), so it is scraped once. Labels follow
`andai_core.metrics`: closed sets only, never an address, a subject or a peer.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter

from andai_core import metrics as core_metrics

SMTP_RESULTS = ("accept", "unknown_rcpt", "too_many_rcpt", "too_large", "store_failed")

SMTP_TOTAL = Counter(
    "munshi_intake_smtp",
    "SMTP receiver verdicts: accept, unknown_rcpt, too_many_rcpt, too_large, store_failed.",
    ["result"],
    registry=None,
)
for _result in SMTP_RESULTS:
    SMTP_TOTAL.labels(_result)


def count_smtp(result: str) -> None:
    if result not in SMTP_RESULTS:
        raise ValueError(f"unknown SMTP result: {result!r}")
    SMTP_TOTAL.labels(result).inc()


def registry() -> CollectorRegistry:
    """andai-intake's registry: mail delivery, then the receiver's verdicts."""
    return core_metrics.service_registry(SMTP_TOTAL)


render = core_metrics.render
