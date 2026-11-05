"""Builds synthetic invite mail for the `andai_intake.invite` tests: VEVENTs, VCALENDARs and MIME
messages shaped like the ones Google Calendar, Outlook and Zoom send. Not a test module."""

from __future__ import annotations

from collections.abc import Iterable
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP, default
from pathlib import Path

NOTETAKER = "notetaker@munshi.test"
ORGANIZER = "ana@acme.test"
CALENDARS = Path(__file__).resolve().parent / "calendars"


def vevent(
    uid: str = "evt-1@acme.test",
    *,
    dtstart: str = "DTSTART:20261110T043000Z",
    dtend: str | None = "DTEND:20261110T053000Z",
    summary: str = "Weekly sync",
    sequence: int | None = 0,
    dtstamp: str | None = "DTSTAMP:20261105T090000Z",
    organizer: str | None = f"ORGANIZER;CN=Ana:mailto:{ORGANIZER}",
    attendees: Iterable[str] = (f"mailto:{NOTETAKER}", "mailto:bo@acme.test"),
    extra: Iterable[str] = (),
) -> str:
    lines = ["BEGIN:VEVENT", f"UID:{uid}"]
    if dtstamp:
        lines.append(dtstamp)
    if dtstart:
        lines.append(dtstart)
    if dtend:
        lines.append(dtend)
    if sequence is not None:
        lines.append(f"SEQUENCE:{sequence}")
    lines.append(f"SUMMARY:{summary}")
    if organizer:
        lines.append(organizer)
    lines.extend(f"ATTENDEE;PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTICIPANT:{a}" for a in attendees)
    lines.extend(extra)
    lines.append("END:VEVENT")
    return "\r\n".join(lines)


def calendar(*events: str, method: str | None = "REQUEST", vtimezones: Iterable[str] = ()) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//munshi tests//EN"]
    if method:
        lines.append(f"METHOD:{method}")
    lines.extend(vtimezones)
    lines.extend(events)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def message(
    *,
    inline: str | None = None,
    attachment: str | None = None,
    body: str = "You have been invited.",
    html: str | None = None,
    forwarded: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "REQUEST",
    other_attachments: Iterable[tuple[str, str, bytes]] = (),
) -> bytes:
    """A multipart/mixed invite mail: body (+ html), inline calendar, `.ics`, forwarded message."""
    msg = EmailMessage()
    msg["From"] = ORGANIZER
    msg["To"] = NOTETAKER
    msg["Subject"] = "Invitation: Weekly sync"
    msg["Message-ID"] = "<m1@acme.test>"
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if inline is not None:
        msg.add_attachment(
            inline.encode(), maintype="text", subtype="calendar", disposition="inline",
            params={"method": method},
        )
        # EmailMessage marks every added part as an attachment; an inline calendar part has none.
        part = msg.get_payload()[-1]
        del part["Content-Disposition"]
    if attachment is not None:
        msg.add_attachment(
            attachment.encode(), maintype="application", subtype="ics", filename="invite.ics"
        )
    for maintype_subtype, filename, data in other_attachments:
        maintype, subtype = maintype_subtype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    if forwarded is not None:
        inner = message_from_bytes(forwarded, policy=default)
        msg.add_attachment(inner)
    return msg.as_bytes(policy=SMTP)
