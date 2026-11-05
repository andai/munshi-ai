"""Plain-text confirmations: what the notetaker will do with an invite, told to its sender (INV-04).

One confirmation per processed message, built by the processor in the transaction that records the
message's result (`Notice`) and sent after that transaction commits (`Confirmer.send`): a meeting
that was rolled back never gets mail, and a mail the relay refuses is logged and dropped, never
followed by a second decision. It says the meeting's title, its time in the invite's own zone and
in UTC, the platform and what will happen:

* `scheduled` / `updated` — the notetaker joins `ANDAI_INTAKE_JOIN_LEAD_S` before the start (a
  series: how many occurrences are scheduled now and that later ones follow within the horizon);
* `joined_now` — the meeting has started, so the notetaker is joining now;
* `cancelled` — it will not join;
* `ignored` — nothing changed, and why;
* `quarantined` (a verified member's invite only) — it could not be scheduled, and why.

A forwarded copy (the notetaker is not an attendee) adds that later changes reach munshi.ai only if
the notetaker is added as an attendee.

**Who gets one (`may_confirm`).** Only the verified From address (`senderAuth` `dmarc_pass` or
`arc_trusted`) of a message that resolved to an active member: never the envelope MAIL FROM, never
in answer to a message with a null reverse-path (`<>`), an `Auto-Submitted` other than `no`, a
`List-Id`, `Precedence: bulk | list | junk`, or a mailbox-rule forward header
(`invite.forward.AUTO_FORWARD_HEADERS`, so a rule that forwards our own mail back cannot start a
loop), and never to a notetaker address (RFC 3834). Each
confirmation carries `Auto-Submitted: auto-replied`. At most `PER_HOUR` (30) per recipient in a
sliding hour (`limiter_key`, counted in Valkey like the intake limits); the rest are dropped.

The title is the invite's SUMMARY with control characters (CR and LF included) removed and white
space collapsed, at most 120 characters, so it can never add a header line. Addresses and content
are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from email.parser import BytesHeaderParser
from email.policy import compat32

import redis

from andai_core.auth.ratelimit import Limiter, ValkeyLimiter
from andai_core.mail import Mail, Mailer
from andai_intake.invite.forward import auto_forwarded

log = logging.getLogger(__name__)

PER_HOUR = 30
WINDOW_SECONDS = 3600
TITLE_MAX = 120
VERIFIED = ("dmarc_pass", "arc_trusted")
BULK = ("bulk", "list", "junk")
LISTED = 5  # occurrences named in one confirmation
PLATFORMS = {"google_meet": "Google Meet", "teams": "Microsoft Teams", "zoom": "Zoom"}
OUTCOMES = {
    "scheduled": "scheduled",
    "updated": "updated",
    "joined_now": "joining now",
    "cancelled": "cancelled",
    "ignored": "nothing changed",
    "quarantined": "could not schedule",
}
WHY = {
    # ignored
    "method_not_handled": "the message was not a meeting invitation or cancellation munshi.ai "
    "acts on",
    "not_event_owner": "only the person who first sent this meeting to munshi.ai, or its "
    "organiser, can change it",
    "stale": "a newer version of this invitation has already been received",
    "duplicate": "this invitation was already received",
    "ended": "the meeting has already ended",
    "all_day": "all-day events have no meeting to join",
    "unsupported_recurrence": "this meeting repeats in a way munshi.ai cannot schedule",
    "auto_join_off": "your organisation has turned off joining meetings from invitations",
    "auto_forwarded": "your organisation does not schedule meetings from automatically forwarded "
    "invitations",
    # quarantined
    "no_meeting_link": "the invitation has no Google Meet, Microsoft Teams or Zoom link",
    "parse_error": "the calendar invitation could not be read",
    "rate_limited": "too many invitations arrived from you in the last hour; send it again later",
}


@dataclass(frozen=True)
class Occurrence:
    start: datetime  # UTC
    skipped: bool = False


@dataclass(frozen=True)
class Notice:
    """One confirmation, decided with the message's result and sent after it commits."""

    to: str
    outcome: str
    reason: str | None
    title: str
    platform: str | None
    zone: tzinfo
    zone_name: str
    occurrences: tuple[Occurrence, ...] = ()
    recurring: bool = False
    forwarded_copy: bool = False


# ─── who ──────────────────────────────────────────────────────────────────────────────────────────


def may_confirm(
    raw: bytes,
    *,
    mail_from: str,
    sender: str | None,
    sender_auth: str | None,
    user_id: uuid.UUID | None,
    notetakers: frozenset[str],
) -> bool:
    """Whether the message's sender may get a confirmation (see the module docstring)."""
    if sender_auth not in VERIFIED or user_id is None or not sender:
        return False
    if sender.strip().lower() in notetakers:
        return False
    if mail_from.strip() in ("", "<>"):
        return False
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0] + b"\r\n\r\n"
    headers = BytesHeaderParser(policy=compat32).parsebytes(head)
    for value in headers.get_all("Auto-Submitted") or ():
        if str(value).split(";", 1)[0].strip().lower() != "no":
            return False
    if headers.get("List-Id") is not None or auto_forwarded(headers):
        return False
    return all(
        str(value).strip().lower() not in BULK for value in headers.get_all("Precedence") or ()
    )


def valkey_limiter(client: redis.Redis) -> ValkeyLimiter:
    """The per-recipient limit, counted in the same Valkey namespace as the intake limits."""
    return ValkeyLimiter(client, PER_HOUR, WINDOW_SECONDS, prefix="andai:limit:")


def limiter_key(recipient: str) -> str:
    digest = hashlib.sha256(f"intake-confirm\x00{recipient.strip().lower()}".encode()).hexdigest()
    return f"intake:confirm:{digest}"


# ─── what ─────────────────────────────────────────────────────────────────────────────────────────


def clean_title(value: str | None) -> str:
    """`value` safe for a Subject: no control characters, white space collapsed, ≤ 120."""
    if not value:
        return ""
    kept = "".join(" " if not c.isprintable() else c for c in value)
    return " ".join(kept.split())[:TITLE_MAX]


def zone_for(name: str | None, tz: tzinfo | None, default_tz: str) -> tuple[tzinfo, str]:
    """The zone a time is shown in: the invite's own, else the deployment default."""
    if tz is not None:
        return tz, name if name and not name.startswith("vtimezone:") else ""
    return zoneinfo.ZoneInfo(default_tz), default_tz


def when(start: datetime, zone: tzinfo, zone_name: str) -> str:
    local = start.astimezone(zone)
    label = zone_name or local.strftime("UTC%z")
    utc = start.astimezone(UTC)
    shown = f"{local:%a %d %b %Y, %H:%M} ({label})"
    if label == "UTC":
        return shown
    return f"{shown}, {utc:%H:%M} UTC" + ("" if utc.date() == local.date() else f" {utc:%d %b}")


def build(notice: Notice, *, lead_s: int, horizon_days: int, notetaker: str | None) -> Mail:
    """The plain-text mail for `notice`."""
    title = clean_title(notice.title) or "your meeting"
    platform = PLATFORMS.get(notice.platform or "", "")
    on = f" on {platform}" if platform else ""
    first = notice.occurrences[0] if notice.occurrences else None
    lines = [f"Meeting: {title}"]
    if platform:
        lines.append(f"Platform: {platform}")
    if first is not None:
        lines.append(f"Time: {when(first.start, notice.zone, notice.zone_name)}")
    lines.append("")
    lead = "two minutes" if lead_s == 120 else f"{lead_s} seconds"
    outcome, why = notice.outcome, WHY.get(notice.reason or "", "")
    if outcome in ("scheduled", "updated"):
        verb = "is scheduled" if outcome == "scheduled" else "was updated"
        if notice.occurrences and all(o.skipped for o in notice.occurrences):
            lines.append(f"The meeting {verb}. You skipped it, so the notetaker will not join.")
        else:
            lines.append(
                f"The meeting {verb}. The munshi.ai notetaker will join{on} {lead} before it "
                "starts, introduce itself as a recorder and take notes."
            )
        if notice.recurring:
            lines.append(
                f"This meeting repeats: {len(notice.occurrences)} occurrence(s) in the next "
                f"{horizon_days} days are scheduled, and later ones are added as they come "
                f"within {horizon_days} days."
            )
        if len(notice.occurrences) > 1:
            lines.append("")
            lines.extend(
                f"- {when(o.start, notice.zone, notice.zone_name)}"
                + (" (skipped)" if o.skipped else "")
                for o in notice.occurrences[:LISTED]
            )
            if len(notice.occurrences) > LISTED:
                lines.append(f"- and {len(notice.occurrences) - LISTED} more")
    elif outcome == "joined_now":
        lines.append(
            f"The meeting has already started, so the munshi.ai notetaker is joining{on} now. "
            "It will introduce itself as a recorder."
        )
    elif outcome == "cancelled":
        lines.append("The meeting was cancelled. The munshi.ai notetaker will not join it.")
    elif outcome == "ignored":
        lines.append("munshi.ai did not change anything" + (f": {why}." if why else "."))
    else:
        lines.append("munshi.ai could not schedule this meeting" + (f": {why}." if why else "."))
    if notice.forwarded_copy and outcome in ("scheduled", "updated", "joined_now"):
        address = f" ({notetaker})" if notetaker else ""
        lines += [
            "",
            "You forwarded a copy of this invitation. Later changes or a cancellation by the "
            "organiser will not reach munshi.ai unless the notetaker"
            f"{address} is added to the meeting as an attendee.",
        ]
    lines += [
        "",
        "You can see and skip your upcoming meetings in munshi.ai.",
        "",
        "This message was sent automatically; replies are not read.",
    ]
    return Mail(
        to=notice.to,
        subject=f"munshi.ai: {OUTCOMES.get(outcome, outcome)} — {title}",
        text="\n".join(lines) + "\n",
        headers=(("Auto-Submitted", "auto-replied"),),
    )


# ─── sending ──────────────────────────────────────────────────────────────────────────────────────


class Confirmer:
    def __init__(
        self,
        mailer: Mailer,
        limiter: Limiter,
        *,
        lead_s: int = 120,
        horizon_days: int = 14,
        notetakers: frozenset[str] = frozenset(),
    ) -> None:
        self.mailer = mailer
        self.limiter = limiter
        self.lead_s = lead_s
        self.horizon_days = horizon_days
        self.notetaker = sorted(notetakers)[0] if len(notetakers) == 1 else None

    async def send(self, notice: Notice) -> bool:
        """Send `notice` unless its recipient had `PER_HOUR` confirmations in the last hour.
        Returns whether the relay took it; a failure is logged (type only) and dropped."""
        key = limiter_key(notice.to)
        try:
            if await asyncio.to_thread(self.limiter.blocked, key) is not None:
                log.info("confirmation suppressed: %s per hour reached", PER_HOUR)
                return False
            await asyncio.to_thread(self.limiter.hit, key)
            mail = build(
                notice, lead_s=self.lead_s, horizon_days=self.horizon_days, notetaker=self.notetaker
            )
            await self.mailer.send(mail)
            return True
        except Exception as exc:  # noqa: BLE001 - the decision stands; the mail is dropped
            log.warning("confirmation not sent: %s", type(exc).__name__)
            return False
