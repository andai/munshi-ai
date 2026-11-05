"""The org's quarantined notetaker mail, for its owners and admins (INV-03).

`GET /api/v1/org/intake/quarantine?cursor=` lists the caller's org's `inbound_mail` rows in status
`quarantined`, newest first (received time, then id), 25 per page:

    {"items": [{receivedAt, sender, subject, reason, senderAuth, check}], "next_cursor": str | null}

* `sender` — the From address the intake read (null when the message named none, or several);
* `subject` — the subject trimmed to 120 characters;
* `reason` — `sender_unverified`, `unknown_user`, `no_meeting_link`, `parse_error` or
  `rate_limited`;
* `senderAuth` — the sender check's verdict: `dmarc_pass`, `arc_trusted` or `fail`;
* `check` — one sentence an admin can read, built from the stored check summary (`check_summary`),
  or null when the message has none.

Nothing else of the message is returned: never its body, envelope or peer address. `next_cursor` is
an opaque token for the next page (null on the last); a cursor this route did not issue is 422
`bad_cursor`.

The rows are read as the app role in the caller's org context, so row-level security applies as
well as the org filter: a message nobody attributed to an org (`org_id IS NULL`) is never listed.
Members and leads get 403.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import literal, select, tuple_

from andai_core.auth.deps import Services, get_services, org_transaction
from andai_core.auth.roles import Caller, require_role
from andai_core.db.models import InboundMail
from andai_core.meetings.service import MeetingError

router = APIRouter(prefix="/api/v1/org")

PAGE_SIZE = 25
MAX_CURSOR_CHARS = 256
BAD_CURSOR = MeetingError(422, "bad_cursor", "this cursor is not valid for this list")

_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_TOKEN = re.compile(r"^[a-z_]{1,20}$")


# ─── cursor ───────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """Where a page starts: after the row with this received time and id."""

    received_at: datetime
    mail_id: uuid.UUID


def encode_cursor(position: Position) -> str:
    raw = json.dumps(
        {"t": position.received_at.isoformat(), "i": str(position.mail_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> Position:
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise BAD_CURSOR
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        position = Position(datetime.fromisoformat(raw["t"]), uuid.UUID(raw["i"]))
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise BAD_CURSOR from None
    if position.received_at.tzinfo is None:
        raise BAD_CURSOR
    return position


# ─── the check summary ────────────────────────────────────────────────────────────────────────────


def _domain(value: Any) -> str | None:
    return value if isinstance(value, str) and _DOMAIN.match(value) else None


def _token(value: Any) -> str:
    return value if isinstance(value, str) and _TOKEN.match(value) else "unknown"


def _dkim(results: Any) -> str:
    if not isinstance(results, list) or not results:
        return "none"
    found = {_token(r.get("result")) for r in results if isinstance(r, dict)}
    for result in ("pass", "fail", "temperror", "permerror"):
        if result in found:
            return result
    return "none"


_FROM_PROBLEMS = {
    "no_from": "The message has no From address.",
    "several_from": "The message names more than one sender.",
    "bad_from": "The From address could not be read.",
}


def check_summary(summary: Any) -> str | None:
    """One sentence on the sender check, from the `auth_summary` the intake stored."""
    if not isinstance(summary, dict) or not summary:
        return None
    verdict = summary.get("verdict")
    reason = summary.get("reason")
    domain = _domain(summary.get("from_domain")) or "the sender's domain"
    dmarc = summary.get("dmarc") if isinstance(summary.get("dmarc"), dict) else {}
    if verdict == "dmarc_pass":
        aligned = [
            name
            for name, ok in (("SPF", dmarc.get("spf_aligned")), ("DKIM", dmarc.get("dkim_aligned")))
            if ok
        ]
        how = f" ({' and '.join(aligned)} aligned)" if aligned else ""
        return f"DMARC passed for {domain}{how}."
    if verdict == "arc_trusted":
        arc = summary.get("arc") if isinstance(summary.get("arc"), dict) else {}
        sealer = _domain(arc.get("sealer")) or "an allowlisted forwarder"
        return f"DMARC did not pass for {domain}; accepted through the ARC seal of {sealer}."
    if reason in _FROM_PROBLEMS:
        return _FROM_PROBLEMS[reason]
    if reason == "no_dmarc_record":
        return f"{domain} publishes no DMARC record, so the sender cannot be verified."
    if reason == "temperror":
        return f"A DNS lookup failed or timed out while checking {domain}."
    if reason == "not_aligned":
        spf = summary.get("spf") if isinstance(summary.get("spf"), dict) else {}
        return (
            f"Neither SPF nor DKIM passed in alignment with {domain}"
            f" (SPF {_token(spf.get('result'))}, DKIM {_dkim(summary.get('dkim'))})."
        )
    return "The sender could not be verified."


# ─── the list ─────────────────────────────────────────────────────────────────────────────────────


def _iso(value: datetime) -> str:
    return value.isoformat()


@router.get("/intake/quarantine")
def list_quarantine(
    cursor: str | None = Query(default=None, max_length=MAX_CURSOR_CHARS),
    caller: Caller = Depends(require_role("owner", "admin")),
    services: Services = Depends(get_services),
) -> dict:
    after = None if cursor is None else decode_cursor(cursor)
    # Named columns only: the app role may not read `raw`, and nothing else of a message leaves.
    query = select(
        InboundMail.id,
        InboundMail.received_at,
        InboundMail.sender,
        InboundMail.subject_trim,
        InboundMail.quarantine_reason,
        InboundMail.sender_auth,
        InboundMail.auth_summary,
    ).where(
        InboundMail.org_id == caller.session.org_id,
        InboundMail.status == "quarantined",
    )
    if after is not None:
        last = tuple_(literal(after.received_at), literal(after.mail_id))
        query = query.where(tuple_(InboundMail.received_at, InboundMail.id) < last)
    query = query.order_by(InboundMail.received_at.desc(), InboundMail.id.desc())
    with org_transaction(services, caller.session) as tx:
        rows = tx.execute(query.limit(PAGE_SIZE + 1)).all()
    page = rows[:PAGE_SIZE]
    next_cursor = None
    if len(rows) > PAGE_SIZE:
        next_cursor = encode_cursor(Position(page[-1].received_at, page[-1].id))
    return {
        "items": [
            {
                "receivedAt": _iso(row.received_at),
                "sender": row.sender,
                "subject": row.subject_trim,
                "reason": row.quarantine_reason,
                "senderAuth": row.sender_auth,
                "check": check_summary(row.auth_summary),
            }
            for row in page
        ],
        "next_cursor": next_cursor,
    }
