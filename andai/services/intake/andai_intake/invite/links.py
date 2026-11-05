"""The meeting join link of an invite: found, unwrapped and checked, offline (string parsing only).

Where it looks, in order (`SOURCES`): `X-GOOGLE-CONFERENCE`, `X-MICROSOFT-SKYPETEAMSMEETINGURL`,
`X-MICROSOFT-ONLINEMEETINGEXTERNALLINK`, `DESCRIPTION`, `X-ALT-DESC` (HTML), `LOCATION` (Zoom puts
the link there), then the mail's own text and HTML bodies. Within the text sources, a link shaped like a
join link (`JOIN_PATHS`: Meet `/abc-defg-hij`, Teams `/l/meetup-join/…` or `/meet/…`, Zoom `/j/…`,
`/w/…`, `/my/…`, `/wc/…`) is preferred over any other link to the same hosts (a Teams
"Meeting options" or a Zoom "Find your local number" link); the vendor properties always hold the
join link.

Unwrapping (`unwrap`), at most `MAX_WRAPPERS` levels, never fetching anything:

* Microsoft Defender Safe Links — `*.safelinks.protection.outlook.com/?url=<encoded>`;
* Proofpoint URL Defense v1 and v2 — `urldefense.proofpoint.com/v1|v2/url?u=<encoded>` (v2 writes
  `%` as `-` and `/` as `_`);
* Proofpoint URL Defense v3 — `urldefense.com/v3/__<url>__;<base64>!…`: each `*` in the URL takes
  the next character of the base64 data and `**X` takes a run of them. This decoder is a port of the
  public-domain (CC0-1.0) decoder at https://github.com/cardi/proofpoint-url-decoder; Proofpoint's
  own script is GPL-3.0 and is not used;
* Google redirects — `www.google.com/url?q=<encoded>`.

Mimecast's protected links (`*.mimecast.com`, `*.mimecastprotect.com`) are opaque tokens: such a
link counts as no link, and the search moves on to the next candidate.

Every unwrapped link must then be `https`, carry no user info, no port other than 443, no white
space, control character or backslash; its host is IDNA-encoded and must pass
`andai_core.meetings.service.link_platform()`, the same host allowlist the portal's paste-link
dispatch uses. The path and query are kept as sent (Teams `?p=` and Zoom `pwd=` passcodes); the
fragment is dropped.
"""

from __future__ import annotations

import base64
import binascii
import html
import re
import string
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from andai_core.meetings.service import MeetingError, link_platform

MAX_WRAPPERS = 3
MAX_URLS_PER_SOURCE = 200

VENDOR_PROPS = (
    "X-GOOGLE-CONFERENCE",
    "X-MICROSOFT-SKYPETEAMSMEETINGURL",
    "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK",
)
TEXT_PROPS = ("DESCRIPTION", "X-ALT-DESC", "LOCATION")
SOURCES = (*VENDOR_PROPS, *TEXT_PROPS, "body")

JOIN_PATHS = {
    "google_meet": re.compile(r"\A/(?:[a-z]{3,}-[a-z]{4,}-[a-z]{3,}|lookup/[^/]+)/?\Z", re.I),
    "teams": re.compile(r"\A/(?:l/meetup-join/|meet/)", re.I),
    "zoom": re.compile(r"\A/(?:j|w|my|wc)/", re.I),
}

_URL = re.compile(r"https?://[^\s<>\"'`{}|\\^\[\]]+", re.I)
_TRAILING = ".,;:!?)]}'\""
_SAFELINKS = (".safelinks.protection.outlook.com", ".safelinks.protection.office365.us")
_MIMECAST = (".mimecast.com", ".mimecastprotect.com")
_V3 = re.compile(
    r"\Ahttps://urldefense(?:\.proofpoint)?\.com/v3/__(?P<url>.+?)__;(?P<data>[^!]*)!", re.I
)
_V3_TOKEN = re.compile(r"\*(\*.)?")
_V3_RUNS = {
    c: n
    for n, c in enumerate(
        string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_", start=2
    )
}
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


@dataclass(frozen=True)
class JoinLink:
    url: str
    platform: str  # google_meet | teams | zoom
    source: str  # one of SOURCES
    wrappers: tuple[str, ...]  # unwrapped wrappers, outermost first


class _Opaque(Exception):
    """A wrapper whose target cannot be recovered offline (Mimecast)."""


def _under(host: str, suffixes: Iterable[str]) -> bool:
    return any(host.endswith(s) or host == s.lstrip(".") for s in suffixes)


def _v3(inner: str, data: str) -> str:
    chars = base64.urlsafe_b64decode(data + "==").decode("utf-8")
    url = unquote(inner)
    out: list[str] = []
    last = pos = 0
    for match in _V3_TOKEN.finditer(url):
        out.append(url[last : match.start()])
        token = match.group(0)
        size = 1 if token == "*" else _V3_RUNS[token[-1]]
        if pos + size > len(chars):
            raise ValueError("short v3 data")
        out.append(chars[pos : pos + size])
        pos += size
        last = match.end()
    out.append(url[last:])
    return "".join(out)


def _unwrap_once(url: str) -> tuple[str, str] | None:
    """`(inner URL, wrapper name)` when `url` is a known wrapper, else None."""
    v3 = _V3.match(url)
    if v3:
        return _v3(v3.group("url"), v3.group("data")), "proofpoint_v3"
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    query = parse_qs(parts.query)
    if _under(host, _MIMECAST):
        raise _Opaque(url)
    if _under(host, _SAFELINKS):
        return query["url"][0], "safelinks"
    if host == "urldefense.proofpoint.com" and parts.path.lower() == "/v1/url":
        return query["u"][0], "proofpoint_v1"
    if host == "urldefense.proofpoint.com" and parts.path.lower() == "/v2/url":
        return unquote(query["u"][0].replace("-", "%").replace("_", "/")), "proofpoint_v2"
    if host in ("google.com", "www.google.com") and parts.path == "/url":
        target = query.get("q") or query.get("url")
        if target:
            return target[0], "google"
        raise KeyError("q")
    return None


def unwrap(url: str) -> tuple[str, tuple[str, ...]] | None:
    """`url` with up to `MAX_WRAPPERS` wrappers removed, and their names. None when a wrapper is
    opaque or broken, or when wrappers remain past the limit."""
    wrappers: list[str] = []
    current = url
    for _ in range(MAX_WRAPPERS + 1):
        try:
            step = _unwrap_once(current)
        except (_Opaque, KeyError, IndexError, ValueError, binascii.Error, UnicodeError):
            return None
        if step is None:
            return current, tuple(wrappers)
        if len(wrappers) == MAX_WRAPPERS:
            return None
        current, name = step
        wrappers.append(name)
    return None


def accept(url: str) -> tuple[str, str] | None:
    """`(normalised URL, platform)` when `url` is an acceptable join link, else None."""
    if _BAD_CHARS.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc:
        return None
    if port not in (None, 443):
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    normalised = urlunsplit(("https", ascii_host, parts.path or "/", parts.query, ""))
    try:
        platform = link_platform(normalised)
    except MeetingError:
        return None
    return normalised, platform


def urls_in(text: str) -> list[str]:
    """http(s) URLs in plain text or HTML (entities decoded, trailing punctuation trimmed)."""
    found: list[str] = []
    for match in _URL.finditer(html.unescape(text)):
        url = match.group(0).rstrip(_TRAILING)
        if url:
            found.append(url)
        if len(found) >= MAX_URLS_PER_SOURCE:
            break
    return found


def _is_join_path(platform: str, url: str) -> bool:
    pattern = JOIN_PATHS.get(platform)
    return bool(pattern and pattern.match(urlsplit(url).path))


def join_link(props: dict[str, Sequence[str]], bodies: Sequence[str]) -> JoinLink | None:
    """The join link of one event: `props` maps property names (upper-case) to their text values,
    `bodies` are the mail's text bodies in order. See the module docstring."""
    fallback: JoinLink | None = None
    for source in SOURCES:
        texts = bodies if source == "body" else props.get(source, ())
        for text in texts:
            for raw in urls_in(text):
                unwrapped = unwrap(raw)
                if unwrapped is None:
                    continue
                checked = accept(unwrapped[0])
                if checked is None:
                    continue
                link = JoinLink(checked[0], checked[1], source, unwrapped[1])
                if source in VENDOR_PROPS or _is_join_path(link.platform, link.url):
                    return link
                if fallback is None:
                    fallback = link
    return fallback
