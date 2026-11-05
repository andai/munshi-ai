"""How an invite reached the notetaker: as an attendee, as a forwarded copy, or by an auto-forward.

`is_forwarded_copy(attendees, notetaker_addresses)` — the notetaker is not among the event's
ATTENDEEs, so someone forwarded a copy (Outlook "Forward", Gmail forward, "Forward as iCalendar" or
"Forward as attachment"). The organizer may never send that copy an update or a cancellation
(INV-04), so the confirmation says so.

`auto_forwarded(headers)` — the message carries a header that a mailbox rule or server-side
forward adds. The documented list (`AUTO_FORWARD_HEADERS`):

* `Auto-Submitted: auto-forwarded` (RFC 5436 / RFC 3834 registry value);
* `X-MS-Exchange-Inbox-Rules-Loop` (Exchange Online / Outlook inbox rule);
* `X-Forwarded-For` and `X-Forwarded-To` (Gmail and Postfix-style forwarding).

The flags only describe the message; they are never trusted for sender verification, which is
`andai_intake.auth`'s job.
"""

from __future__ import annotations

from collections.abc import Iterable
from email.message import Message

AUTO_FORWARD_HEADERS = (
    "Auto-Submitted",
    "X-MS-Exchange-Inbox-Rules-Loop",
    "X-Forwarded-For",
    "X-Forwarded-To",
)


def is_forwarded_copy(attendees: Iterable[str], notetaker_addresses: Iterable[str]) -> bool:
    """True when none of `notetaker_addresses` is an ATTENDEE (both compared lower-case)."""
    ours = {a.strip().lower() for a in notetaker_addresses if a.strip()}
    return not any(a.strip().lower() in ours for a in attendees)


def auto_forwarded(headers: Message) -> bool:
    """True when `headers` carry one of `AUTO_FORWARD_HEADERS` (see the module docstring)."""
    for value in headers.get_all("Auto-Submitted") or ():
        if str(value).split(";", 1)[0].strip().lower() == "auto-forwarded":
            return True
    return any(headers.get(name) is not None for name in AUTO_FORWARD_HEADERS[1:])
