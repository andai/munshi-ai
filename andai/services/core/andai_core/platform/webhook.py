"""The meeting platform's system webhook: `POST /internal/platform-events` (AUTH_MATRIX row
`/internal/platform-events`).

meeting-api posts its terminal events to one operator-configured URL (`MUNSHI_SYSTEM_WEBHOOK_URL`,
set by the compose overlay to this route on the stack network). The route lives outside `/api`, so
the portal's `/api/*` proxy never forwards to it; it carries no cookie and is authenticated by its
signature alone, which is why the Origin check exempts exactly this path (`auth/csrf.py`).

* **Signature.** `X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<ts>." + raw body)>` with
  `X-Webhook-Timestamp: <unix seconds>`, the platform's scheme (`webhooks/delivery.py`). The HMAC is
  over the exact bytes received, compared in constant time; the timestamp must be within 300 s of
  andai-core's clock. A missing, malformed or wrong signature, or a stale or future timestamp, is
  401 with nothing written. Without `ANDAI_PLATFORM_WEBHOOK_SECRET` every request is 503 (fail
  closed).
* **Dedup.** The envelope's `event_id` is stable across the platform's retries. The first delivery
  of an id is recorded in `platform_webhook_events` and answered 202; a redelivery is 200
  `duplicate` and changes nothing.
* **Nudge, not work.** For `meeting.completed` the route only records the event and calls
  `andai_platform_completed(<data.meeting.id>)`, which makes that meeting's transcript copy due now
  when it was waiting for a retry; andai-worker's claims do the work (`andai_claim_ingest`, then
  `andai_claim_mom`), so a lost webhook only costs latency. The record and the nudge are one
  transaction: when the nudge fails, no event row is left and the platform's retry is processed.
* **No existence oracle.** A meeting id andai does not know is answered exactly like a known one
  (202). Any other event type is 202 `ignored` and not recorded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime

import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as Tx

from andai_core.auth.deps import Services, get_services
from andai_core.db.session import transaction

log = logging.getLogger(__name__)

PATH = "/internal/platform-events"
SIGNATURE_HEADER = "x-webhook-signature"
TIMESTAMP_HEADER = "x-webhook-timestamp"
TOLERANCE_S = 300
MAX_BODY_BYTES = 1024 * 1024
COMPLETED = "meeting.completed"

router = APIRouter()


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The platform's signature header value for `body` sent at `timestamp`."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={digest.hexdigest()}"


def _answer(status: int, detail: str) -> JSONResponse:
    key = "detail" if status >= 400 else "status"
    return JSONResponse({key: detail}, status_code=status)


def verified(secret: str, headers, body: bytes, now: datetime) -> bool:
    """Whether the signature and timestamp headers authenticate `body` at `now`."""
    signature = headers.get(SIGNATURE_HEADER)
    timestamp = headers.get(TIMESTAMP_HEADER)
    if not signature or not timestamp or not timestamp.isascii() or not timestamp.isdigit():
        return False
    if abs(now.timestamp() - int(timestamp)) > TOLERANCE_S:
        return False
    expected = sign(secret, timestamp, body).encode()
    return hmac.compare_digest(expected, signature.encode("utf-8", "replace"))


def _platform_meeting_id(envelope: dict) -> int | None:
    data = envelope.get("data")
    meeting = data.get("meeting") if isinstance(data, dict) else None
    value = meeting.get("id") if isinstance(meeting, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def nudge(tx: Tx, platform_meeting_id: int) -> None:
    """Make the meeting's transcript copy due now (any org; returns nothing)."""
    tx.execute(text("SELECT andai_platform_completed(:p)"), {"p": platform_meeting_id})


def record(services: Services, event_id: str, event_type: str, platform_id: int | None) -> bool:
    """Record the event and nudge its meeting in one transaction; False for a redelivery."""
    with transaction(services.engine) as tx:  # andai_app with no org context
        inserted = tx.execute(
            text(
                "INSERT INTO platform_webhook_events (event_id, event_type, platform_meeting_id)"
                " VALUES (:e, :t, :p) ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
            ),
            {"e": event_id, "t": event_type, "p": platform_id},
        ).first()
        if inserted is None:
            return False
        if platform_id is not None:
            nudge(tx, platform_id)
    return True


@router.post(PATH)
async def platform_event(
    request: Request, services: Services = Depends(get_services)
) -> JSONResponse:
    secret_value = services.settings.platform_webhook_secret
    secret = secret_value.get_secret_value().strip() if secret_value is not None else ""
    if not secret:
        return _answer(503, "platform webhook not configured")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _answer(413, "payload too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _answer(413, "payload too large")
    if not verified(secret, request.headers, body, services.clock()):
        return _answer(401, "invalid signature")
    try:
        envelope = json.loads(body)
    except ValueError:
        return _answer(400, "the body is not JSON")
    if not isinstance(envelope, dict):
        return _answer(400, "the body is not an event")
    event_id, event_type = envelope.get("event_id"), envelope.get("event_type")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
        return _answer(400, "the event has no id")
    if event_type != COMPLETED:
        log.info("platform event %s (%s) ignored", event_id, str(event_type)[:40])
        return _answer(202, "ignored")
    platform_id = _platform_meeting_id(envelope)
    fresh = await anyio.to_thread.run_sync(
        lambda: record(services, event_id, event_type, platform_id)
    )
    if not fresh:
        log.info("platform event %s (%s) is a redelivery", event_id, event_type)
        return _answer(200, "duplicate")
    log.info("platform event %s (%s) accepted", event_id, event_type)
    return _answer(202, "accepted")


__all__ = ["PATH", "TOLERANCE_S", "nudge", "record", "router", "sign", "verified"]
