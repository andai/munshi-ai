"""Turn captured invite mail into fixtures that can be committed (D-24, D-55).

A captured message names real people, domains, meetings and mail servers. `Mapping` finds those
identifiers and rewrites each one to a stand-in derived from a secret key (HMAC-SHA256). The same
original always gets the same stand-in, so the create, update and cancel mails of one meeting still
match. `sanitise` rewrites one message. `resign` then signs the result with test keys that stub DNS
serves. Signing is the last step: a rewrite made after it would break the signature.

    python -m andai_intake.testing.sanitise --secret KEYFILE --capture ADDRESS \\
        [--terms FILE] [--terms-out FILE] --out DIR ORIGINAL.eml [...]

The command reads every original first (`Mapping.discover`), so an identifier found in one message
is rewritten in all of them. It then writes `DIR/<name>.eml` (sanitised, unsigned) and
`DIR/<name>.sign.json` (the envelope sender and the `SignStep`s), and prints counts only. KEYFILE
holds the mapping key; it is created on first use (mode 0600) and is never committed: without it
the stand-ins cannot be linked back to the originals. `--terms FILE` adds identifiers to rewrite
(the same file `leakscan` reads). `--terms-out FILE` writes every identifier found, for `leakscan`.

What is rewritten, wherever it appears: plain, in any case, percent-encoded once or twice, inside
base64 query values, and inside Safe Links, Proofpoint (v1, v2, v3) and Google redirect wrappers.
Wrappers are decoded, rewritten and encoded again in their own format, and their per-customer
tokens are mapped:

* addresses — a `--capture` address (the capture mailbox) becomes `NOTETAKER`, so the attendee check
  (`is_forwarded_copy`) still tells the truth. Other local parts are mapped letter for letter
  (letters stay letters, digits stay digits). Generic senders of a platform (`no-reply@zoom.us`) are
  kept;
* domains — a domain's organisational part (`ORG_SECOND_LEVEL` decides between `acme.com` and
  `acme.co.in`) becomes `<word><hex>.example.test`. Subdomain labels are kept. `PLATFORM_DOMAINS`
  (link hosts, wrapper hosts, mail providers) are kept. A tenant's label under a platform domain is
  mapped (`acme.onmicrosoft.com`, `acme.zoom.us`). `.test` names are kept;
* display names and ICS `CN=` values — word by word, to made-up words in the original's case.
  `STOPWORDS` are never mapped;
* ids — GUIDs (Teams `Tid`/`Oid`, tenant ids), Teams thread ids, Zoom and Teams meeting ids and
  passcodes (spaced forms too), Meet codes, Message-ID local parts, `Thread-Index`, Safe Links
  `data=` fields, UIDs. An Exchange Global Object ID UID keeps its class id, instance date,
  creation time and size, and only the data after them is mapped. The instance and its series
  therefore still share `ics.normalise_uid`.

What is kept: the MIME tree, content types, transfer encodings, charsets, header order, VTIMEZONEs,
TZIDs, METHOD, every date property (`PROTECTED_PROPS`), and the wrappers themselves. Attachments
that are neither text nor calendar get a placeholder payload, which the parser never reads.

What is removed: trace, authentication and vendor headers (`DROP_HEADERS`, `DROP_PREFIXES`).
`X-MS-Exchange-Inbox-Rules-Loop` is kept, rewritten, because it marks an auto-forward.

Re-signing: each original DKIM-Signature and ARC set becomes a `SignStep` with its domain mapped,
ordered as the hops added them (bottom header first). A sealer's ARC-Authentication-Results keep
only their method results and `header.from`/`header.d`/`header.i`/`smtp.mailfrom` values, which
are mapped as well. `resign(sanitised, kit)` signs DKIM and seals ARC in that order, with keys
generated at run time that `kit` publishes. Whether an original signature verified is not known
here; `authreport` reports that from the originals.

Limits: an identifier that appears only in free text, with no address, URL or label next to it, is
rewritten only when it is listed in `--terms`. `leakscan` is the check that nothing remained.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import codecs
import hashlib
import hmac
import html
import json
import os
import quopri
import re
import secrets
import string
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from email.header import decode_header
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.quoprimime import body_encode as qp_body_encode
from email.quoprimime import header_encode as qp_header_encode
from email.utils import getaddresses
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import authres
import dkim

from andai_intake.invite import links
from andai_intake.invite.ics import GOID_PREFIX
from andai_intake.replay import wire_bytes
from andai_intake.testing.sign import NOTETAKER, SELECTOR, Kit, rsa_key

TEST_SUFFIX = "example.test"
KEEP_SUFFIXES = (".test", ".localhost")
PLACEHOLDER = b"Attachment removed by the fixture sanitiser.\r\n"

# Kept as they are: hosts the link parser and wrappers need, and the big mail providers.
PLATFORM_DOMAINS = (
    "google.com",
    "googlemail.com",
    "gmail.com",
    "googleusercontent.com",
    "googleapis.com",
    "gstatic.com",
    "microsoft.com",
    "microsoft.us",
    "microsoftonline.com",
    "office.com",
    "office365.com",
    "office365.us",
    "outlook.com",
    "live.com",
    "hotmail.com",
    "skype.com",
    "lync.com",
    "aka.ms",
    "onmicrosoft.com",
    "sharepoint.com",
    "zoom.us",
    "zoom.com",
    "zoomgov.com",
    "proofpoint.com",
    "urldefense.com",
    "pphosted.com",
    "mimecast.com",
    "mimecastprotect.com",
    "icloud.com",
    "me.com",
    "apple.com",
    "w3.org",
)
# Platform domains whose next label names a tenant: mapped unless it matches the generic pattern.
_ZOOM_GENERIC = re.compile(
    r"\A(?:www|[a-z]{2}\d{2}web|[a-z]{2}web|api|app|applications|support|explore|events|"
    r"marketplace|success|blog|community|status|devsupport|help|zoom)\Z"
)
TENANT_PLATFORMS: dict[str, re.Pattern[str] | None] = {
    "mail.protection.outlook.com": None,
    "onmicrosoft.com": None,
    "sharepoint.com": None,
    "pphosted.com": None,
    "zoom.us": _ZOOM_GENERIC,
    "zoom.com": _ZOOM_GENERIC,
    "zoomgov.com": _ZOOM_GENERIC,
}
ORG_SECOND_LEVEL = frozenset(
    {"co", "com", "net", "org", "ac", "gov", "edu", "gen", "firm", "ind", "ltd", "plc", "nic",
     "res", "or", "ne", "go", "mil"}
)
_GENERIC_LOCAL = re.compile(
    r"\A(?:no-?reply|do-?not-?reply|donotreply|calendar-notification|calendar-server|"
    r"notifications?|mailer-daemon|postmaster)\Z",
    re.IGNORECASE,
)
STOPWORDS = frozenset(
    """
    the and for with from you your our are was not but all any can will this that have has
    html head body div span table font style class href color width height mailto http https www
    meet join meeting meetings teams zoom google microsoft outlook calendar mail text plain charset
    utf content type name value true false none null organizer organiser attendee required
    optional chair participant role partstat rsvp cutype individual accepted tentative declined
    needs action mobile phone dial location description summary invitation invite invited updated
    update canceled cancelled cancel event video call conference passcode password notetaker
    munshi capture team inc ltd llc pvt limited corp company group admin info support noreply reply
    office room help options more details user member guest external internal india standard time
    daylight utc gmt ist weekly daily sync series occurrence recurring
    """.split()
)

# Never rewritten in a calendar part: structure and every date or time value.
PROTECTED_PROPS = frozenset(
    {
        "BEGIN", "END", "VERSION", "METHOD", "CALSCALE", "DTSTART", "DTEND", "DTSTAMP", "DUE",
        "RECURRENCE-ID", "EXDATE", "RDATE", "RRULE", "EXRULE", "SEQUENCE", "TZID", "TZOFFSETFROM",
        "TZOFFSETTO", "TZNAME", "CREATED", "LAST-MODIFIED", "STATUS", "TRANSP", "CLASS",
        "PRIORITY", "TRIGGER", "DURATION", "ACTION", "REPEAT",
    }
)
DROP_HEADERS = frozenset(
    {
        "dkim-signature", "arc-seal", "arc-message-signature", "arc-authentication-results",
        "received", "authentication-results", "received-spf", "x-received", "x-originating-ip",
        "x-sender-ip", "msip_labels", "x-originatororg", "x-clientproxiedby",
        "x-incomingheadercount", "x-incomingtopheadermarker", "dkim-filter", "x-virus-scanned",
        "x-eopattributedmessage", "x-eoptenantattributedmessage", "x-auth-result",
    }
)
DROP_PREFIXES = (
    "x-ms-", "x-microsoft-", "x-google-", "x-gm-", "x-forefront-", "x-proofpoint-",
    "x-mimecast-", "x-spam-", "x-eop", "x-crosspremises", "x-originating", "x-barracuda-",
)
KEEP_HEADERS = frozenset({"x-ms-exchange-inbox-rules-loop"})
UNTOUCHED_HEADERS = frozenset(
    {"content-type", "content-transfer-encoding", "mime-version", "date"}
)
ADDRESS_HEADERS = frozenset(
    {"from", "to", "cc", "bcc", "reply-to", "sender", "resent-from", "resent-to", "resent-cc",
     "resent-sender", "delivered-to", "x-original-to", "return-path", "errors-to",
     "disposition-notification-to"}
)
MSGID_HEADERS = frozenset(
    {"message-id", "in-reply-to", "references", "content-id", "resent-message-id",
     "x-microsoft-original-message-id"}
)
MAX_DEPTH = 10

_EMAIL = re.compile(r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_GUID = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
_THREAD = re.compile(r"19:(?:meeting_)?([A-Za-z0-9_-]{16,})@thread\.(?:v2|tacv2|skype)", re.I)
_LABELLED = re.compile(
    r"\b(?:meeting\s*id|conference\s*id|webinar\s*id|passcode|password|pin|access\s*code)\b"
    r"\s*[:#]?\s*([0-9](?:[0-9 \u00a0]{4,}[0-9])|[A-Za-z0-9._!$*-]{4,})",
    re.IGNORECASE,
)
_NAME_PARAM = re.compile(r"(\bname\*?=)(\"[^\"]*\"|[^;\s]+)", re.IGNORECASE)
_MSGID = re.compile(r"<([^<>@\s]+)@([^<>\s]+)>")
_ENCODED_WORD = re.compile(r"=\?([^?\s]+)\?([BbQq])\?([^?\s]*)\?=")
_B64_VALUE = re.compile(r"\A[A-Za-z0-9+/_-]{16,}={0,2}\Z")
_URL = re.compile(r"https?://[^\s<>\"'`{}|\\^\[\]]+", re.I)
_V3_FULL = re.compile(
    r"\A(?P<head>https://urldefense(?:\.proofpoint)?\.com/v3/__)(?P<url>.+?)__;(?P<data>[^!]*)!"
    r"(?P<tail>.*)\Z",
    re.I | re.S,
)
_V3_RUN_CHARS = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
_PCT_HEX = re.compile(r"%[0-9A-Fa-f]{2}")
_DOMAIN_TERM = re.compile(r"\A[a-z0-9-]+(?:\.[a-z0-9-]+)+\Z")

# Left and right edges of a match. A percent escape (`%40`, `%2540`) or an ICS `\n` escape may sit
# right before an identifier even though its last character is a letter or digit.
_ESCAPED = r"|(?<=%[0-9A-Fa-f]{2})|(?<=%25[0-9A-Fa-f]{2})|(?<=\\[nN])"
_BEFORE = {
    "email": r"(?:(?<![A-Za-z0-9._+-])" + _ESCAPED + ")",
    "domain": r"(?:(?<![A-Za-z0-9-])" + _ESCAPED + ")",
}
_BEFORE_WORD = r"(?:(?<![A-Za-z0-9])" + _ESCAPED + ")"
_AFTER_DOMAIN = r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])"
_AFTER_DOMAIN_LOOSE = r"(?![A-Za-z-]|\.[A-Za-z0-9])"
_AFTER_WORD = r"(?![A-Za-z0-9])"
_GROUPS = ("email", "domain", "hex", "digits", "token", "name")
_FOLDED = ("email", "domain", "hex", "name")  # matched in any case


class SanitiseError(ValueError):
    """A message the sanitiser cannot rewrite safely."""


@dataclass(frozen=True)
class Term:
    group: str  # one of _GROUPS
    original: str
    replacement: str


@dataclass(frozen=True)
class SignStep:
    kind: str  # dkim | arc
    domain: str  # the signing domain (`d=`), mapped
    authserv: str | None = None  # arc: the sealer's authserv-id, mapped
    results: str | None = None  # arc: the results the sealer recorded, mapped


@dataclass(frozen=True)
class Sanitised:
    raw: bytes  # unsigned, CRLF
    steps: tuple[SignStep, ...]
    mail_from: str | None  # the envelope sender from Return-Path, mapped

    def sign_record(self) -> dict[str, object]:
        return {"mail_from": self.mail_from, "steps": [asdict(s) for s in self.steps]}


# ─── domains ─────────────────────────────────────────────────────────────────────────────────────


def _under(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def platform_of(host: str) -> str | None:
    """The `PLATFORM_DOMAINS` (or tenant platform) entry `host` is under, longest first."""
    host = host.lower().rstrip(".")
    for suffix in sorted((*TENANT_PLATFORMS, *PLATFORM_DOMAINS), key=len, reverse=True):
        if _under(host, suffix):
            return suffix
    return None


def tenant_label(host: str) -> tuple[str, str] | None:
    """`(label, platform)` when `host` names a tenant under a tenant platform."""
    host = host.lower().rstrip(".")
    for platform, generic in sorted(TENANT_PLATFORMS.items(), key=lambda i: -len(i[0])):
        if host != platform and host.endswith("." + platform):
            label = host[: -len(platform) - 1].rsplit(".", 1)[-1]
            if generic is not None and generic.match(label):
                return None
            return label, platform
    return None


def org_domain(host: str) -> str:
    """The organisational part of `host`: its last two labels, or three for `acme.co.in`."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ORG_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _kept(host: str) -> bool:
    return host.endswith(KEEP_SUFFIXES) or host in ("localhost",) or "." not in host


def _is_ip(host: str) -> bool:
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return True
    return ":" in host and bool(re.fullmatch(r"\[?[0-9A-Fa-f:.]+\]?", host))


# ─── MIME, without re-serialising ────────────────────────────────────────────────────────────────


def _split_entity(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """(header block with its final CRLF, separator, body)."""
    if raw.startswith(b"\r\n"):
        return b"", b"\r\n", raw[2:]
    end = raw.find(b"\r\n\r\n")
    if end == -1:
        return raw, b"", b""
    return raw[: end + 2], b"\r\n", raw[end + 4 :]


def _header_fields(head: bytes) -> list[tuple[str, bytes]]:
    """(name, raw value with its folding) per header field, in order."""
    fields: list[tuple[str, bytes]] = []
    for line in head.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and fields:
            name, value = fields[-1]
            fields[-1] = (name, value + b"\r\n" + line)
            continue
        name, sep, value = line.partition(b":")
        fields.append((name.decode("ascii", "replace") if sep else "", value if sep else line))
    return fields


def _unfold(value: str) -> str:
    return re.sub(r"\r?\n(?=[ \t])", "", value).strip()


def _decode_words(value: str) -> str:
    """`value` with RFC 2047 encoded words decoded (left as is when they cannot be)."""

    def one(match: re.Match[str]) -> str:
        try:
            (data, charset), *_ = decode_header(match.group(0))
            if isinstance(data, bytes):
                return data.decode(charset or "ascii", "replace")
        except (LookupError, ValueError, binascii.Error):
            pass
        return match.group(0)

    return _ENCODED_WORD.sub(one, value)


def _headers_message(head: bytes) -> Message:
    return BytesHeaderParser(policy=compat32).parsebytes(head + b"\r\n")


def _split_multipart(body: bytes, boundary: str) -> tuple[list[bytes], list[bytes]]:
    """`body` as (chunks, parts) with body == chunks[0] + parts[0] + chunks[1] + ... + chunks[n]."""
    delimiter = re.compile(
        rb"(?m)^--" + re.escape(boundary.encode("ascii", "replace")) + rb"(--)?[ \t]*\r?$"
    )
    matches = list(delimiter.finditer(body))
    if not matches:
        return [body], []
    chunks: list[bytes] = []
    parts: list[bytes] = []
    cursor = 0
    for index, match in enumerate(matches):
        if match.group(1):
            chunks.append(body[cursor:])
            return chunks, parts
        line_end = match.end() + 1 if body[match.end() : match.end() + 1] == b"\n" else match.end()
        chunks.append(body[cursor:line_end])
        following = matches[index + 1] if index + 1 < len(matches) else None
        if following is None:
            parts.append(body[line_end:])
            chunks.append(b"")
            return chunks, parts
        end = following.start()
        if body[end - 2 : end] == b"\r\n":
            end -= 2
        elif body[end - 1 : end] == b"\n":
            end -= 1
        end = max(end, line_end)
        parts.append(body[line_end:end])
        cursor = end
    chunks.append(body[cursor:])
    return chunks, parts


def _join_multipart(chunks: list[bytes], parts: list[bytes]) -> bytes:
    out = [chunks[0]]
    for part, chunk in zip(parts, chunks[1:], strict=True):
        out += [part, chunk]
    return b"".join(out)


def _leaf_kind(msg: Message) -> str | None:
    ctype = msg.get_content_type()
    filename = (msg.get_filename() or "").lower()
    if ctype in ("text/calendar", "application/ics") or filename.endswith(".ics"):
        return "calendar"
    if ctype == "text/html":
        return "html"
    if ctype.startswith("text/") or ctype == "message/delivery-status":
        return "text"
    return None


def _codec(msg: Message) -> str:
    try:
        return codecs.lookup(msg.get_content_charset() or "utf-8").name
    except LookupError:
        return "utf-8"


def _cte(msg: Message) -> str:
    return str(msg.get("Content-Transfer-Encoding") or "7bit").strip().lower()


def _decode_payload(body: bytes, cte: str) -> bytes:
    if cte == "base64":
        compact = b"".join(body.split())
        try:
            return base64.b64decode(compact + b"=" * (-len(compact) % 4))
        except (binascii.Error, ValueError):
            raise SanitiseError("unreadable base64 part") from None
    if cte == "quoted-printable":
        return quopri.decodestring(body)
    return body


def _encode_payload(data: bytes, cte: str, original: bytes) -> bytes:
    trailing = original.endswith(b"\r\n")
    if cte == "base64":
        first = original.split(b"\r\n", 1)[0].strip()
        width = len(first) if 4 <= len(first) <= 998 and len(first) % 4 == 0 else 76
        encoded = base64.b64encode(data)
        lines = [encoded[i : i + width] for i in range(0, len(encoded), width)]
        out = b"\r\n".join(lines)
    elif cte == "quoted-printable":
        text = data.decode("latin-1")
        out = qp_body_encode(text, maxlinelen=76, eol="\r\n").encode("ascii")
    else:
        out = re.sub(rb"\r?\n", b"\r\n", data)
    if trailing and not out.endswith(b"\r\n"):
        out += b"\r\n"
    return out


def _walk(raw: bytes, depth: int = 0) -> Iterator[tuple[str, str, str]]:
    """("header", name, decoded value) and ("body", kind, decoded text) of every entity."""
    head, _sep, body = _split_entity(raw)
    for name, value in _header_fields(head):
        text = _unfold(value.decode("utf-8", "surrogateescape"))
        yield "header", name.lower(), _decode_words(text)
    if depth > MAX_DEPTH:
        return
    msg = _headers_message(head)
    ctype = msg.get_content_type()
    boundary = msg.get_boundary()
    if ctype.startswith("multipart/") and boundary:
        for part in _split_multipart(body, boundary)[1]:
            yield from _walk(part, depth + 1)
    elif ctype == "message/rfc822":
        yield from _walk(body, depth + 1)
    else:
        kind = _leaf_kind(msg)
        if kind is not None:
            data = _decode_payload(body, _cte(msg))
            yield "body", kind, data.decode(_codec(msg), "surrogateescape")


# ─── calendar text ───────────────────────────────────────────────────────────────────────────────


def _ics_unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_unescape(text: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


def _fold(line: str, eol: str) -> str:
    data = line.encode("utf-8", "surrogateescape")
    if len(data) <= 75:
        return line
    chunks: list[bytes] = []
    start, limit = 0, 75
    while start < len(data):
        end = min(start + limit, len(data))
        while start < end < len(data) and (data[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(data[start:end])
        start, limit = end, 74
    return (eol + " ").join(c.decode("utf-8", "surrogateescape") for c in chunks)


def _prop_split(line: str) -> tuple[str, str]:
    """(name and parameters with the colon, value) of an unfolded content line."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            return line[: index + 1], line[index + 1 :]
    return line, ""


# ─── encodings ───────────────────────────────────────────────────────────────────────────────────


def _percent(value: str, depth: int, lower_hex: bool = False) -> str:
    for _ in range(depth):
        value = quote(value, safe="")
    if lower_hex:
        value = _PCT_HEX.sub(lambda m: m.group(0).lower(), value)
    return value


def _lower_hex(sample: str) -> bool:
    escapes = _PCT_HEX.findall(sample)
    return any(e != e.upper() for e in escapes) and not any(
        e != e.lower() for e in escapes
    )


def _requote(new: str, old: str) -> str:
    return _percent(new, 1, _lower_hex(old))


def _v2_decode(value: str) -> str:
    return unquote(value.replace("-", "%").replace("_", "/"))


def _v2_encode(url: str) -> str:
    out = []
    for char in url:
        if char.isascii() and (char.isalnum() or char == "."):
            out.append(char)
        elif char == "/":
            out.append("_")
        else:
            out.append("".join(f"-{b:02X}" for b in char.encode("utf-8")))
    return "".join(out)


def _v3_encode(url: str, starred: set[str]) -> tuple[str, str]:
    """(URL part, data) of a Proofpoint v3 link for `url`, starring every char in `starred`."""
    out: list[str] = []
    data: list[str] = []
    run: list[str] = []

    def flush() -> None:
        while run:
            take = run[: len(_V3_RUN_CHARS) + 1]
            del run[: len(take)]
            out.append("*" if len(take) == 1 else "**" + _V3_RUN_CHARS[len(take) - 2])
            data.extend(take)

    for char in url:
        if char in starred:
            run.append(char)
        else:
            flush()
            out.append(char)
    flush()
    encoded = base64.urlsafe_b64encode("".join(data).encode("utf-8")).decode("ascii")
    return "".join(out), encoded.rstrip("=")


def _case_like(sample: str, word: str) -> str:
    if sample.isupper():
        return word.upper()
    if sample.islower():
        return word.lower()
    return word[:1].upper() + word[1:].lower()


def _hexlike(value: str) -> bool:
    alnum = [c for c in value if c.isascii() and c.isalnum()]
    return (
        len(alnum) >= 16
        and all(c in string.hexdigits for c in alnum)
        and any(c.isalpha() for c in alnum)
    )


# ─── the mapping ─────────────────────────────────────────────────────────────────────────────────


class Mapping:
    """Identifiers and their stand-ins, derived from `secret`. Discover every message first, then
    rewrite: a stand-in is a pure function of the secret and the original, so the order of
    discovery never changes it."""

    def __init__(
        self,
        secret: bytes,
        capture: Iterable[str] = (),
        *,
        notetaker: str = NOTETAKER,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("the mapping secret needs at least 16 bytes")
        self._secret = secret
        self.capture = frozenset(a.strip().lower() for a in capture if a.strip())
        self.notetaker = notetaker.lower()
        self._terms: dict[tuple[str, str], Term] = {}
        self._compiled: dict[tuple[str, bool], re.Pattern[str]] | None = None
        self._lookup: dict[tuple[str, str], tuple[Term, int]] = {}

    # stand-ins ------------------------------------------------------------------------------

    def _stream(self, kind: str, value: str) -> Iterator[int]:
        counter = 0
        while True:
            message = f"{kind}\x00{value}\x00{counter}".encode("utf-8", "surrogateescape")
            yield from hmac.new(self._secret, message, hashlib.sha256).digest()
            counter += 1

    def _hex(self, kind: str, value: str, size: int) -> str:
        stream = self._stream(kind, value)
        return "".join("0123456789abcdef"[next(stream) % 16] for _ in range(size))

    def shape(self, kind: str, value: str) -> str:
        """`value` with each letter and digit replaced by one of its own class (hex stays hex)."""
        hexmode = _hexlike(value)
        upper = any(c.isupper() for c in value)
        salt = ""
        while True:
            stream = self._stream(kind + salt, value.lower() if hexmode else value)
            out: list[str] = []
            first = True
            for char in value:
                if not (char.isascii() and char.isalnum()):
                    out.append(char)
                    continue
                byte = next(stream)
                if hexmode:
                    digit = "0123456789abcdef"[byte % 16]
                    out.append(digit.upper() if upper else digit)
                elif char.isdigit():
                    out.append(str(1 + byte % 9) if first and char != "0" else str(byte % 10))
                elif char.islower():
                    out.append(string.ascii_lowercase[byte % 26])
                else:
                    out.append(string.ascii_uppercase[byte % 26])
                first = False
            result = "".join(out)
            if result.lower() != value.lower() or not any(c.isalnum() for c in value):
                return result
            salt += "'"

    def word(self, value: str) -> str:
        """A made-up, pronounceable word of about `value`'s length."""
        stream = self._stream("word", value.lower())
        consonants, vowels = "bdfghklmnprstvz", "aeiou"
        letters: list[str] = []
        while len(letters) < max(4, len(value)):
            letters.append(consonants[next(stream) % len(consonants)])
            letters.append(vowels[next(stream) % len(vowels)])
        made = "".join(letters[: max(4, len(value))]).capitalize()
        return made if made.lower() != value.lower() else made + "a"

    # registering ----------------------------------------------------------------------------

    def _add(self, group: str, original: str, replacement: str) -> None:
        key = (group, original.lower() if group in _FOLDED else original)
        if key not in self._terms and original != replacement:
            self._terms[key] = Term(group, original, replacement)
            self._compiled = None

    def map_host(self, host: str) -> str:
        """The stand-in for `host`; registers what it rewrites."""
        host = host.strip().rstrip(".").lower()
        if not host or _is_ip(host) or _kept(host):
            return host
        tenant = tenant_label(host)
        if tenant is not None:
            label, platform = tenant
            original = f"{label}.{platform}"
            replacement = f"tenant{self._hex('tenant', label, 6)}.{platform}"
            self._add("domain", original, replacement)
            return host[: -len(original)] + replacement
        if platform_of(host):
            return host
        org = org_domain(host)
        if org.endswith(TEST_SUFFIX):
            return host
        replacement = f"{self.word(org)[:6].lower()}{self._hex('domain', org, 4)}.{TEST_SUFFIX}"
        self._add("domain", org, replacement)
        return host[: -len(org)] + replacement

    def map_address(self, address: str) -> str:
        address = address.strip().lower()
        if address in self.capture:
            return self.notetaker
        local, at, domain = address.rpartition("@")
        if not at or not local or _kept(domain):
            return address
        new_domain = self.map_host(domain)
        keep_local = (platform_of(domain) or _kept(domain)) and _GENERIC_LOCAL.match(local)
        new_local = local if keep_local else self.shape("local", local)
        return f"{new_local}@{new_domain}"

    def add_email(self, address: str) -> None:
        address = address.strip().strip(".").lower()
        if "@" not in address:
            return
        replacement = self.map_address(address)
        self._add("email", address, replacement)

    def add_token(self, token: str) -> None:
        token = token.strip()
        compact = re.sub(r"[ \u00a0]", "", token)
        if compact.isdigit():
            if len(compact) >= 6:
                self._add("digits", compact, self.shape("token", compact))
            return
        if len(token) < 5 or token.lower() in STOPWORDS:
            return
        if _hexlike(token):
            self._add("hex", token.lower(), self.shape("token", token.lower()))
        else:
            self._add("token", token, self.shape("token", token))

    def add_name(self, text: str) -> None:
        for word in re.findall(r"[^\W\d_]{3,}", text):
            if word.lower() not in STOPWORDS:
                self._add("name", word.lower(), self.word(word))

    def add_term(self, term: str) -> None:
        """One identifier from a terms file, by its shape."""
        term = term.strip()
        if not term:
            return
        if _EMAIL.fullmatch(term):
            self.add_email(term)
        elif _DOMAIN_TERM.fullmatch(term.lower()):
            self.map_host(term)
        elif re.fullmatch(r"\+?[\d \u00a0().-]+", term):
            self.add_token(re.sub(r"\D", "", term))
        elif " " in term or term.isalpha():
            self.add_name(term)
        else:
            self.add_token(term)

    # discovering ----------------------------------------------------------------------------

    def discover(self, raw: bytes) -> None:
        """Register every identifier found in one captured message."""
        for where, kind, text in _walk(wire_bytes(raw)):
            if where == "header":
                self._discover_header(kind, text)
            elif kind == "calendar":
                unfolded = _ics_unfold(text)
                self._discover_calendar(unfolded)
                self._scan(unfolded)
                self._scan(_ics_unescape(unfolded))
            else:
                self._scan(text)
                if kind == "html":
                    self._scan(html.unescape(text))

    def _discover_header(self, name: str, text: str) -> None:
        if name in ADDRESS_HEADERS:
            for display, address in getaddresses([text]):
                if address:
                    self.add_email(address)
                if display and "@" not in display:
                    self.add_name(display)
        if name in MSGID_HEADERS:
            for local, host in _MSGID.findall(text):
                if len(local) >= 6:
                    self.add_token(local)
                self.map_host(host)
        if name in ("dkim-signature", "arc-seal", "arc-message-signature"):
            for domain in re.findall(r"(?:^|;)\s*d\s*=\s*([^;\s]+)", text):
                self.map_host(domain)
        if name == "arc-authentication-results":
            authserv = _INSTANCE.sub("", text).split(";", 1)[0].strip().split(" ", 1)[0]
            if "." in authserv:
                self.map_host(authserv)
        self._scan(text)

    def _discover_calendar(self, unfolded: str) -> None:
        for line in unfolded.splitlines():
            for match in re.finditer(r";CN=(\"[^\"]*\"|[^;:]*)", line, re.IGNORECASE):
                self.add_name(match.group(1).strip('"'))
            prefix, value = _prop_split(line)
            if prefix.upper().startswith("UID") and not self._goid(value.strip()):
                local = value.strip().rpartition("@")[0] or value.strip()
                for run in re.findall(r"[A-Za-z0-9]{6,}", local):
                    self.add_token(run)

    def _scan(self, text: str, depth: int = 0) -> None:
        views = {text}
        once = unquote(text)
        views.update((once, unquote(once)))
        for view in views:
            for address in _EMAIL.findall(view):
                self.add_email(address)
            for guid in _GUID.findall(view):
                self.add_token(guid)
            for token in _THREAD.findall(view):
                self.add_token(token)
            for value in _LABELLED.findall(view):
                if _passcode_like(value):
                    self.add_token(value)
            for url in links.urls_in(view):
                self._discover_url(url, depth)

    def _discover_url(self, url: str, depth: int) -> None:
        candidates = [url]
        unwrapped = links.unwrap(url)
        if unwrapped is not None and unwrapped[0] != url:
            candidates.append(unwrapped[0])
            if depth < 2:
                self._scan(unwrapped[0], depth + 1)
        for candidate in candidates:
            try:
                parts = urlsplit(candidate)
                host = (parts.hostname or "").lower()
            except ValueError:
                continue
            self.map_host(host)
            path = parts.path
            if _under(host, "zoom.us") or _under(host, "zoomgov.com"):
                for found in re.findall(r"/(?:j|w|s|wc(?:/join)?)/(\d{9,12})", path):
                    self.add_token(found)
                for found in re.findall(r"/my/([A-Za-z0-9._-]+)", path):
                    self.add_token(found)
            elif _under(host, "teams.microsoft.com") or _under(host, "teams.live.com"):
                for found in re.findall(r"/meet/(\d{9,})", path):
                    self.add_token(found)
            elif host == "meet.google.com":
                for found in re.findall(r"\A/([a-z]{3,}-[a-z]{4,}-[a-z]{3,}|lookup/[^/]+)", path):
                    self.add_token(found.rpartition("/")[2])
            for key, values in parse_qs(parts.query).items():
                for value in values:
                    if key.lower() in ("pwd", "p", "passcode", "password", "tk"):
                        self.add_token(value)
                    elif depth < 2 and _B64_VALUE.match(value):
                        decoded = _b64_decode(value)
                        if decoded is not None:
                            self._scan(decoded[0], depth + 1)

    # rewriting ------------------------------------------------------------------------------

    def _compile(self) -> dict[tuple[str, bool], re.Pattern[str]]:
        if self._compiled is not None:
            return self._compiled
        alternatives: dict[str, list[str]] = {g: [] for g in _GROUPS}
        self._lookup = {}
        for term in self._terms.values():
            if term.group == "digits":
                alternatives["digits"].append("[ \u00a0]?".join(term.original))
                self._lookup[("digits", term.original)] = (term, 0)
                continue
            for depth in (0, 1, 2):
                variant = _percent(term.original, depth)
                if depth and variant == term.original:
                    continue
                key = variant.lower() if term.group in _FOLDED else variant
                if (term.group, key) in self._lookup:
                    continue
                self._lookup[(term.group, key)] = (term, depth)
                alternatives[term.group].append(re.escape(variant))
        compiled: dict[tuple[str, bool], re.Pattern[str]] = {}
        for group in _GROUPS:
            if not alternatives[group]:
                continue
            body = "|".join(sorted(set(alternatives[group]), key=len, reverse=True))
            before = _BEFORE.get(group, _BEFORE_WORD)
            flags = re.IGNORECASE if group in _FOLDED else 0
            for loose in (False, True):
                if group in ("email", "domain"):
                    after = _AFTER_DOMAIN_LOOSE if loose else _AFTER_DOMAIN
                else:
                    after = _AFTER_WORD
                compiled[(group, loose)] = re.compile(f"{before}(?:{body}){after}", flags)
        self._compiled = compiled
        return compiled

    def _replace(self, group: str, match: re.Match[str]) -> str:
        text = match.group(0)
        if group == "digits":
            term, depth = self._lookup[("digits", re.sub(r"[ \u00a0]", "", text))]
            digits = iter(term.replacement)
            return "".join(next(digits) if c.isdigit() else c for c in text)
        key = text.lower() if group in _FOLDED else text
        term, depth = self._lookup[(group, key)]
        replacement = term.replacement
        if group == "name":
            replacement = _case_like(text, replacement)
        elif group == "hex":
            upper = any(c.isupper() for c in text)
            replacement = replacement.upper() if upper else replacement.lower()
        return _percent(replacement, depth, _lower_hex(text)) if depth else replacement

    def terms(self, text: str, *, loose: bool = False) -> str:
        """`text` with every registered identifier replaced, in every encoded form. `loose` lets
        an address or domain run straight into digits, as inside a decoded token
        (`19#ana@acme.com9f3c…`)."""
        for (group, is_loose), pattern in self._compile().items():
            if is_loose == loose:
                text = pattern.sub(lambda m, g=group: self._replace(g, m), text)
        return text

    def text(self, text: str) -> str:
        """Plain text or HTML: URLs rewritten structurally, then every identifier."""

        def one(match: re.Match[str]) -> str:
            url = match.group(0)
            stripped = url.rstrip(links._TRAILING)
            return self.url(stripped) + url[len(stripped) :]

        return self.terms(_URL.sub(one, text))

    def url(self, url: str) -> str:
        amp = "&amp;" in url
        plain = url.replace("&amp;", "&") if amp else url
        out = self._url(plain, 0)
        return out.replace("&", "&amp;") if amp else out

    def _url(self, url: str, depth: int) -> str:
        if depth > links.MAX_WRAPPERS:
            return self.terms(url)
        v3 = _V3_FULL.match(url)
        if v3 is not None:
            return self._v3(v3, depth)
        base, mark, rest = url.partition("?")
        query, hash_mark, fragment = rest.partition("#")
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return self.terms(url)
        if query:
            fields = query.split("&")
            for index, field in enumerate(fields):
                key, eq, value = field.partition("=")
                path = parts.path.lower()
                new = self._param(host, path, key.lower(), value, depth) if eq else None
                if new is not None:
                    fields[index] = f"{key}={new}"
            query = "&".join(fields)
        return self.terms(base + mark + query + hash_mark + fragment)

    def _param(self, host: str, path: str, key: str, value: str, depth: int) -> str | None:
        if any(_under(host, s.lstrip(".")) for s in links._SAFELINKS):
            if key == "url":
                return _requote(self._url(unquote(value), depth + 1), value)
            if key == "data":
                return self._safelinks_data(value)
            if key == "sdata":
                return _requote(self.shape("safelinks", unquote(value)), value)
        if host == "urldefense.proofpoint.com" and path == "/v2/url":
            if key == "u":
                return _v2_encode(self._url(_v2_decode(value), depth + 1))
            if key in ("c", "r", "m", "s"):
                return self.shape("proofpoint", value)
        if host == "urldefense.proofpoint.com" and path == "/v1/url":
            if key == "u":
                return _requote(self._url(unquote(value), depth + 1), value)
            if key in ("k", "r", "m", "s"):
                return self.shape("proofpoint", value)
        if host in ("google.com", "www.google.com") and path == "/url" and key in ("q", "url"):
            return _requote(self._url(unquote(value), depth + 1), value)
        return self._b64_param(value)

    def _b64_param(self, value: str) -> str | None:
        raw = unquote(value)
        if not _B64_VALUE.match(raw):
            return None
        decoded = _b64_decode(raw)
        if decoded is None:
            return None
        text, urlsafe = decoded
        new = self.terms(text, loose=True)
        if new == text:
            return None
        data = new.encode("latin-1", "replace")
        encoded = (base64.urlsafe_b64encode if urlsafe else base64.b64encode)(data).decode()
        if not raw.endswith("="):
            encoded = encoded.rstrip("=")
        return _requote(encoded, value) if raw != value else encoded

    def _safelinks_data(self, value: str) -> str:
        fields = unquote(value).split("|")
        out = []
        for field in fields:
            if re.fullmatch(r"[0-9a-fA-F]{32}|\d{12,}", field):
                out.append(self.shape("safelinks", field))
            elif len(field) >= 16 and _B64_VALUE.match(field):
                out.append(self._b64_param(field) or field)
            else:
                out.append(self.terms(field))
        return _requote("|".join(out), value)

    def _v3(self, match: re.Match[str], depth: int) -> str:
        try:
            inner = links._v3(match.group("url"), match.group("data"))
            data = match.group("data")
            original = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            return self.terms(match.group(0))
        starred = set(original) | {"%", "*"}
        url_part, new_data = _v3_encode(self._url(inner, depth + 1), starred)
        tail = re.sub(
            r"[A-Za-z0-9_-]{4,}", lambda t: self.shape("proofpoint", t.group(0)), match["tail"]
        )
        return f"{match.group('head')}{url_part}__;{new_data}!{tail}"

    @staticmethod
    def _goid(value: str) -> bool:
        return (
            len(value) >= 80
            and len(value) % 2 == 0
            and bool(re.fullmatch(r"[0-9A-Fa-f]+", value))
            and value.upper().startswith(GOID_PREFIX)
        )

    def uid(self, value: str) -> str:
        """A UID: an Exchange Global Object ID keeps its first 40 bytes (class id, instance date,
        creation time, reserved, size) and maps its data; any other UID maps its identifiers."""
        stripped = value.strip()
        if self._goid(stripped):
            return value.replace(stripped, stripped[:80] + self.shape("goid", stripped[80:]))
        return self.terms(value)

    def calendar(self, text: str) -> str:
        eol = "\r\n" if "\r\n" in text else "\n"
        out = []
        for raw_line in re.split(r"\r?\n(?![ \t])", text):
            line = _ics_unfold(raw_line)
            name = re.match(r"[A-Za-z0-9-]*", line).group(0).upper()  # type: ignore[union-attr]
            if name in PROTECTED_PROPS:
                new = line
            elif name == "UID":
                prefix, value = _prop_split(line)
                new = prefix + self.uid(value)
            else:
                new = self.text(line)
            out.append(raw_line if new == line else _fold(new, eol))
        return eol.join(out)

    def header(self, name: str, raw_value: str) -> str | None:
        """A header's new value, or None to keep the raw field as it is."""
        lname = name.lower()
        value = _unfold(raw_value)
        if lname == "content-type":
            new = _NAME_PARAM.sub(lambda m: m.group(1) + self.text(m.group(2)), value)
            return None if new == value else new
        if lname in UNTOUCHED_HEADERS:
            return None
        if lname == "thread-index":
            new = self.shape("thread-index", value)
        else:
            new = self.text(_ENCODED_WORD.sub(self._encoded_word, value))
        return None if new == value else new

    def _encoded_word(self, match: re.Match[str]) -> str:
        charset, encoding = match.group(1), match.group(2).upper()
        try:
            (data, _), *_ = decode_header(match.group(0))
            text = data.decode(charset, "strict") if isinstance(data, bytes) else data
        except (LookupError, ValueError, binascii.Error):
            return match.group(0)
        new = self.text(text)
        if new == text:
            return match.group(0)
        encoded = new.encode(charset, "replace")
        if encoding == "B":
            return f"=?{charset}?B?{base64.b64encode(encoded).decode('ascii')}?="
        return qp_header_encode(encoded, charset)

    # inventory ------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        result = dict.fromkeys(_GROUPS, 0)
        for term in self._terms.values():
            result[term.group] += 1
        return result

    def originals(self) -> list[str]:
        """Every identifier registered, for a leak scan's terms file. Never print these."""
        return sorted({t.original for t in self._terms.values()})


_INSTANCE = re.compile(r"\A\s*i\s*=\s*\d+\s*;\s*", re.IGNORECASE)


def _passcode_like(value: str) -> bool:
    compact = re.sub(r"[ \u00a0]", "", value)
    if compact.isdigit():
        return len(compact) >= 6
    if value.lower() in STOPWORDS or len(value) < 5:
        return False
    return any(c.isdigit() for c in value) or (
        any(c.isupper() for c in value) and any(c.islower() for c in value)
    )


def _b64_decode(value: str) -> tuple[str, bool] | None:
    """(text as latin-1, URL-safe alphabet) of a base64 value, or None."""
    urlsafe = "-" in value or "_" in value
    padded = value + "=" * (-len(value) % 4)
    try:
        if urlsafe:
            data = base64.urlsafe_b64decode(padded)
        else:
            data = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return data.decode("latin-1"), urlsafe


# ─── one message ─────────────────────────────────────────────────────────────────────────────────


def _dropped(name: str) -> bool:
    name = name.lower()
    if name in KEEP_HEADERS:
        return False
    return name in DROP_HEADERS or name.startswith(DROP_PREFIXES)


def _rewrite_headers(mapping: Mapping, head: bytes) -> bytes:
    out: list[bytes] = []
    for name, value in _header_fields(head):
        if not name:
            out.append(value)
            continue
        if _dropped(name):
            continue
        new = mapping.header(name, value.decode("utf-8", "surrogateescape"))
        if new is None:
            out.append(name.encode("ascii", "replace") + b":" + value)
        else:
            out.append(f"{name}: {new}".encode("utf-8", "surrogateescape"))
    return b"".join(line + b"\r\n" for line in out)


def _rewrite_entity(mapping: Mapping, raw: bytes, depth: int = 0) -> bytes:
    head, sep, body = _split_entity(raw)
    new_head = _rewrite_headers(mapping, head)
    if depth > MAX_DEPTH:
        raise SanitiseError("MIME nesting too deep")
    msg = _headers_message(head)
    ctype = msg.get_content_type()
    boundary = msg.get_boundary()
    if ctype.startswith("multipart/") and boundary:
        chunks, parts = _split_multipart(body, boundary)
        new_body = _join_multipart(chunks, [_rewrite_entity(mapping, p, depth + 1) for p in parts])
    elif ctype == "message/rfc822":
        new_body = _rewrite_entity(mapping, body, depth + 1)
    else:
        new_body = _rewrite_leaf(mapping, msg, body)
    return new_head + sep + new_body


def _rewrite_leaf(mapping: Mapping, msg: Message, body: bytes) -> bytes:
    cte = _cte(msg)
    kind = _leaf_kind(msg)
    if kind is None:
        return _encode_payload(PLACEHOLDER, cte, body) if body.strip() else body
    codec = _codec(msg)
    text = _decode_payload(body, cte).decode(codec, "surrogateescape")
    new = mapping.calendar(text) if kind == "calendar" else mapping.text(text)
    if new == text:
        return body
    return _encode_payload(new.encode(codec, "surrogateescape"), cte, body)


def _sign_steps(mapping: Mapping, head: bytes) -> tuple[SignStep, ...]:
    fields = [(n.lower(), _unfold(v.decode("utf-8", "surrogateescape")))
              for n, v in _header_fields(head)]
    aars: dict[int, str] = {}
    for name, value in fields:
        if name == "arc-authentication-results":
            match = re.match(r"\s*i\s*=\s*(\d+)", value)
            if match:
                aars[int(match.group(1))] = value
    dkims: list[tuple[int, SignStep]] = []
    seals: list[tuple[int, int, SignStep]] = []
    for position, (name, value) in enumerate(fields):
        tags = dict(
            (k.strip().lower(), v.strip())
            for k, _, v in (t.partition("=") for t in value.split(";"))
            if k.strip()
        )
        domain = tags.get("d")
        if not domain:
            continue
        if name == "dkim-signature":
            dkims.append((position, SignStep("dkim", mapping.map_host(domain))))
        elif name == "arc-seal" and tags.get("i", "").isdigit():
            instance = int(tags["i"])
            authserv, results = _aar_results(mapping, aars.get(instance, ""))
            sealer = mapping.map_host(domain)
            step = SignStep("arc", sealer, authserv or sealer, results)
            seals.append((position, instance, step))
    # Bottom header first: that is the order the hops added them. ARC sets go by instance.
    ordered = sorted(
        [(p, "dkim", s) for p, s in dkims] + [(p, "arc", s) for p, _, s in seals],
        key=lambda item: -item[0],
    )
    by_instance = iter(s for _, _, s in sorted(seals, key=lambda item: item[1]))
    return tuple(next(by_instance) if kind == "arc" else step for _, kind, step in ordered)


_AAR_PROPS = {("header", "from"), ("header", "d"), ("header", "i"), ("smtp", "mailfrom")}


def _aar_results(mapping: Mapping, value: str) -> tuple[str | None, str]:
    if not value:
        return None, "arc=none"
    body = _INSTANCE.sub("", value)
    try:
        parsed = authres.AuthenticationResultsHeader.parse_value(body)
    except Exception:  # authres raises its own SyntaxError and others on odd input
        return None, "arc=none"
    authserv = str(parsed.authserv_id or "").strip()
    items = []
    for result in parsed.results:
        method = str(getattr(result, "method", "")).lower()
        verdict = str(getattr(result, "result", "")).lower()
        if not re.fullmatch(r"[a-z0-9-]+", method) or not re.fullmatch(r"[a-z]+", verdict):
            continue
        item = f"{method}={verdict}"
        for prop in getattr(result, "properties", ()):
            if (prop.type, prop.name) in _AAR_PROPS and prop.value:
                item += f" {prop.type}.{prop.name}={mapping.terms(str(prop.value))}"
        items.append(item)
    return (mapping.map_host(authserv) if "." in authserv else None), "; ".join(items) or "arc=none"


def _return_path(mapping: Mapping, head: bytes) -> str | None:
    for name, value in _header_fields(head):
        if name.lower() == "return-path":
            addresses = [a for _, a in getaddresses([_unfold(value.decode("utf-8", "replace"))])]
            return mapping.map_address(addresses[0]) if addresses and addresses[0] else None
    return None


def sanitise(raw: bytes, mapping: Mapping) -> Sanitised:
    """One captured message rewritten through `mapping` (after `mapping.discover` saw it, and
    every other message of the corpus). Unsigned; see `resign`."""
    wire = wire_bytes(raw)
    mapping.discover(wire)
    head, _sep, _body = _split_entity(wire)
    steps = _sign_steps(mapping, head)
    mail_from = _return_path(mapping, head)
    return Sanitised(_rewrite_entity(mapping, wire), steps, mail_from)


def _seal(raw: bytes, kit: Kit, step: SignStep) -> bytes:
    key = rsa_key(step.domain)
    kit.publish_key(step.domain, key, SELECTOR)
    authserv = step.authserv or step.domain
    stamp = f"Authentication-Results: {authserv}; {step.results or 'arc=none'}\r\n".encode()
    arc_set = dkim.arc_sign(
        stamp + raw, SELECTOR.encode(), step.domain.encode(), key.private_pem, authserv.encode()
    )
    if not arc_set:
        raise SanitiseError("dkimpy produced no ARC set")
    # The stamp only feeds the seal: the ARC-Authentication-Results now carry it, and an AMS never
    # signs Authentication-Results, so leaving it out keeps a later hop with the same authserv-id
    # from merging two hops' results.
    return b"".join(arc_set) + raw


def resign(sanitised: Sanitised, kit: Kit) -> bytes:
    """Sign `sanitised` with DKIM and seal its ARC sets, in the original hops' order, with keys
    `kit` publishes. The From domain's SPF and DMARC records are the caller's to publish."""
    raw = sanitised.raw
    for step in sanitised.steps:
        if step.kind == "dkim":
            raw = kit.dkim(raw, step.domain)
        elif step.kind == "arc":
            raw = _seal(raw, kit, step)
        else:
            raise SanitiseError(f"unknown sign step {step.kind!r}")
    return raw


def steps_from_record(record: dict[str, object]) -> tuple[SignStep, ...]:
    steps = record.get("steps") or []
    if not isinstance(steps, list):
        raise SanitiseError("steps must be a list")
    return tuple(SignStep(**step) for step in steps)


# ─── the command ─────────────────────────────────────────────────────────────────────────────────


def load_secret(path: Path) -> bytes:
    """The mapping key in `path`, created (32 random bytes, mode 0600) when missing."""
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(secrets.token_bytes(32))
    return path.read_bytes()


def read_terms(path: Path) -> list[str]:
    lines = path.read_text("utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def run(
    files: list[Path],
    out_dir: Path,
    mapping: Mapping,
    *,
    write: Callable[[Path, bytes], None] | None = None,
) -> int:
    """Discover all `files`, then write each sanitised message and its sign record; returns the
    number written."""
    write = write or (lambda path, data: path.write_bytes(data))
    originals = [(path, path.read_bytes()) for path in files]
    for _, raw in originals:
        mapping.discover(raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    for path, raw in originals:
        result = sanitise(raw, mapping)
        write(out_dir / f"{path.stem}.eml", result.raw)
        record = json.dumps(result.sign_record(), indent=1, sort_keys=True) + "\n"
        write(out_dir / f"{path.stem}.sign.json", record.encode())
    return len(originals)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m andai_intake.testing.sanitise",
        description="Sanitise captured mail into unsigned fixtures plus sign records.",
    )
    parser.add_argument("--secret", required=True, type=Path, help="mapping key file (0600)")
    parser.add_argument(
        "--capture", action="append", default=[], help="capture mailbox address (repeatable)"
    )
    parser.add_argument("--terms", type=Path, help="extra identifiers to rewrite, one per line")
    parser.add_argument("--terms-out", type=Path, help="write every identifier found here")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if not args.capture:
        parser.error("name the capture mailbox with --capture")

    mapping = Mapping(load_secret(args.secret), args.capture)
    if args.terms:
        for term in read_terms(args.terms):
            mapping.add_term(term)
    try:
        written = run(args.files, args.out, mapping)
    except SanitiseError as exc:
        print(f"sanitise: stopped: {exc}", file=sys.stderr)
        return 1
    if args.terms_out:
        fd = os.open(args.terms_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("\n".join(mapping.originals()) + "\n")
    counts = ", ".join(f"{group} {n}" for group, n in mapping.counts().items())
    print(f"sanitised {written} message(s); identifiers mapped: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
