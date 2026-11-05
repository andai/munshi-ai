"""The calendar parts and text bodies of one stored message, read offline with the stdlib.

`extract(raw)` walks the MIME tree of the raw bytes and collects:

* every `text/calendar` and `application/ics` part, inline or attached, decoded to text;
* one level of `message/rfc822` ("Forward as attachment"): its calendar parts and bodies are
  collected too and marked `forwarded`; a message nested inside that one is ignored unread;
* the `text/plain` and `text/html` bodies that are not attachments, for the join-link fallback.

Every other part (zip, PDF, HTML or image attachments) is skipped without decoding its payload.
Identical calendars (the inline part and its `invite.ics` twin) are kept once.

Caps, each a `MimeError` (quarantine reason `parse_error`): more than `MAX_PARTS` parts, nesting
deeper than `MAX_DEPTH`, or a calendar part larger than `MAX_ICS_BYTES` once decoded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser

MAX_PARTS = 50
MAX_DEPTH = 5
MAX_ICS_BYTES = 1024 * 1024
MAX_BODY_BYTES = 256 * 1024
MAX_BODIES = 4

CALENDAR_TYPES = frozenset({"text/calendar", "application/ics"})
BODY_TYPES = ("text/plain", "text/html")


class MimeError(ValueError):
    """The message breaks a cap or cannot be read; the invite is quarantined `parse_error`."""


@dataclass(frozen=True)
class CalendarPart:
    text: str
    method_param: str | None  # the Content-Type `method=` parameter, upper-case
    attachment: bool
    forwarded: bool  # inside a `message/rfc822` part


@dataclass(frozen=True)
class Body:
    content_type: str  # text/plain or text/html
    text: str
    forwarded: bool


@dataclass
class Extracted:
    headers: EmailMessage
    calendars: list[CalendarPart] = field(default_factory=list)
    bodies: list[Body] = field(default_factory=list)
    forwarded_headers: list[Message] = field(default_factory=list)


def _decoded(part: Message, limit: int) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = b""
    if not isinstance(payload, bytes):  # pragma: no cover - multipart is never decoded here
        raise MimeError("unreadable part")
    if len(payload) > limit:
        raise MimeError("part too large")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:  # an unknown charset name
        return payload.decode("utf-8", errors="replace")


def _is_attachment(part: Message) -> bool:
    return (part.get_content_disposition() or "") == "attachment"


def _normal(text: str) -> bytes:
    return "\n".join(line.rstrip("\r") for line in text.strip().splitlines()).encode()


class _Walk:
    def __init__(self, out: Extracted) -> None:
        self.out = out
        self.parts = 0
        self.seen: set[bytes] = set()

    def visit(self, part: Message, depth: int, forwarded: bool) -> None:
        self.parts += 1
        if self.parts > MAX_PARTS:
            raise MimeError(f"more than {MAX_PARTS} MIME parts")
        if depth > MAX_DEPTH:
            raise MimeError(f"MIME nesting deeper than {MAX_DEPTH}")
        ctype = part.get_content_type()
        if part.is_multipart() and ctype != "message/rfc822":
            for child in part.get_payload():
                self.visit(child, depth + 1, forwarded)
            return
        if ctype == "message/rfc822":
            if forwarded:
                return  # a forward inside a forward is ignored unread
            inner = part.get_payload()
            if isinstance(inner, list):
                inner = inner[0] if inner else None
            if isinstance(inner, Message):
                self.out.forwarded_headers.append(inner)
                self.visit(inner, depth + 1, True)
            return
        if ctype in CALENDAR_TYPES:
            text = _decoded(part, MAX_ICS_BYTES)
            digest = hashlib.sha256(_normal(text)).digest()
            if digest in self.seen:
                return
            self.seen.add(digest)
            method = part.get_param("method")
            self.out.calendars.append(
                CalendarPart(
                    text=text,
                    method_param=str(method).strip().upper() if isinstance(method, str) else None,
                    attachment=_is_attachment(part),
                    forwarded=forwarded,
                )
            )
            return
        if ctype in BODY_TYPES and not _is_attachment(part):
            if len(self.out.bodies) >= MAX_BODIES:
                return
            payload = part.get_payload(decode=True) or b""
            if len(payload) > MAX_BODY_BYTES:
                return  # too large to scan for a link; the calendar parts still count
            self.out.bodies.append(Body(ctype, _decoded(part, MAX_BODY_BYTES), forwarded))
        # anything else (attachments of other types) is skipped without decoding


def extract(raw: bytes) -> Extracted:
    """Calendar parts and bodies of `raw` (see the module docstring). Raises `MimeError`."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:  # noqa: BLE001 - the stdlib parser is lenient; else bad input
        raise MimeError("unreadable message") from exc
    out = Extracted(headers=message)  # type: ignore[arg-type]
    _Walk(out).visit(message, 0, False)
    return out
