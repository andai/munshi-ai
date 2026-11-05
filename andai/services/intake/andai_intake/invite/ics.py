"""`parse(raw, notetaker_addresses)`: the invites in one stored message, offline and bounded.

Bytes in, `ParsedEvent`s out: no database, no network, no decision. Each VEVENT of the message's
calendar parts (`mime.extract`) appears once, even when the same calendar arrives inline, as an
`.ics` attachment and inside a forwarded message. Events are deduplicated on
`(uid_norm, recurrence_id, sequence, dtstamp)`, first part wins.

Per event: METHOD, the UID and its normalised form, RECURRENCE-ID (and RANGE), SEQUENCE (default 0),
DTSTAMP, SUMMARY (control characters removed, ≤ `SUMMARY_MAX` characters), ORGANIZER, the ATTENDEEs,
DTSTART/DTEND in UTC (time zones by `tz.ZoneResolver`), RRULE/RDATE/EXDATE, STATUS, the join link
(`links.join_link`), and the forward flags (`forward`).

UIDs: an Exchange Global Object ID (hex, prefix `GOID_PREFIX`) carries the instance date in bytes
16–19; they are zeroed so an instance and its series share `uid_norm`. Any other UID is only
stripped of surrounding white space.

METHOD comes from the VCALENDAR, else from the part's Content-Type `method=` parameter. REQUEST,
CANCEL, REPLY, COUNTER, REFRESH and ADD are recognised; anything else (PUBLISH included) is
`UNKNOWN`, with the original in `method_raw`.

Failures raise `InviteParseError` (quarantine reason `parse_error`): a MIME cap (`mime`), more than
`MAX_VEVENTS` VEVENTs in the message, a calendar icalendar cannot read, calendar parts that hold no
usable VEVENT, or more than `deadline_s` seconds of work. The deadline is checked between parts and
between events; one icalendar parse of one part (≤ 1 MiB, ≤ `MAX_VEVENTS` VEVENTs) runs to its end.
A VEVENT without a UID or with an unreadable DTSTART is skipped; the others are kept.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any

from icalendar import Calendar
from icalendar.cal.component import Component

from andai_intake.invite import forward, links, mime
from andai_intake.invite.tz import Zone, ZoneResolver, reset_icalendar_cache, vtimezone_tzinfo

MAX_VEVENTS = 200
MAX_ATTENDEES = 500
MAX_DATES = 1000
MAX_UID = 1024
SUMMARY_MAX = 200
DEADLINE_S = 5.0
DEFAULT_TZ = "Asia/Kolkata"

GOID_PREFIX = "040000008200E00074C5B7101A82E008"
_HEX = re.compile(r"\A[0-9A-Fa-f]+\Z")
_VEVENT_LINE = re.compile(rb"^BEGIN:VEVENT\s*$", re.IGNORECASE | re.MULTILINE)
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁩﻿]")

_parse_lock = threading.Lock()


class InviteParseError(ValueError):
    """The message cannot be parsed within the caps; quarantine it `parse_error`."""

    reason = "parse_error"


class Method(StrEnum):
    REQUEST = "REQUEST"
    CANCEL = "CANCEL"
    REPLY = "REPLY"
    COUNTER = "COUNTER"
    REFRESH = "REFRESH"
    ADD = "ADD"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Attendee:
    address: str
    name: str | None
    partstat: str | None
    role: str | None


@dataclass(frozen=True)
class ParsedEvent:
    uid: str
    uid_norm: str
    method: Method
    method_raw: str | None
    recurrence_id: datetime | date | None  # UTC, or a date for an all-day instance
    recurrence_range: str | None  # THISANDFUTURE when given
    sequence: int
    dtstamp: datetime | None  # UTC
    summary: str
    organizer: str | None
    organizer_name: str | None
    attendees: tuple[Attendee, ...]
    status: str | None
    all_day: bool
    start: datetime | None  # UTC
    end: datetime | None  # UTC; None when the event gives neither DTEND nor DURATION
    start_date: date | None  # the first day of an all-day event
    start_local: datetime | None  # DTSTART in `zone`, for recurrence expansion
    zone: Zone | None
    floating: bool  # DTSTART was floating or named an unknown TZID: read in the default zone
    rrule: str | None
    rdates: tuple[datetime, ...]  # UTC
    exdates: tuple[datetime, ...]  # UTC
    exdate_days: tuple[date, ...]  # EXDATE;VALUE=DATE on a timed event: that local day is excluded
    join_url: str | None
    platform: str | None
    join_source: str | None
    is_forwarded_copy: bool
    auto_forwarded: bool
    via_forward_attachment: bool  # came from inside a message/rfc822 part
    warnings: tuple[str, ...]

    @property
    def key(self) -> tuple[str, Any, int, datetime | None]:
        return (self.uid_norm, self.recurrence_id, self.sequence, self.dtstamp)


def normalise_uid(uid: str) -> str:
    """The UID used as an event key (see the module docstring)."""
    value = uid.strip()
    if len(value) >= 80 and len(value) % 2 == 0 and _HEX.match(value):
        upper = value.upper()
        if upper.startswith(GOID_PREFIX):
            return upper[:32] + "00000000" + upper[40:]
    return value


def clean_text(value: str, limit: int = SUMMARY_MAX) -> str:
    """`value` without control, zero-width or bidi characters, white space collapsed, ≤ `limit`."""
    return " ".join(_UNSAFE.sub(" ", value).split())[:limit]


def _address(value: Any) -> str | None:
    text = str(value).strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    text = text.strip().lower()
    if not text or "@" not in text or any(c.isspace() for c in text) or len(text) > 320:
        return None
    return text


def _param(prop: Any, name: str) -> str | None:
    params = getattr(prop, "params", None)
    if not params:
        return None
    value = params.get(name)
    if value is None:
        return None
    return str(value)


def _listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


class _Times:
    """Reads date and date-time properties as wall time + TZID, then resolves them itself."""

    def __init__(self, resolver: ZoneResolver) -> None:
        self.resolver = resolver
        self.warnings: set[str] = set()

    def moment(self, value: Any, tzid: str | None) -> tuple[datetime | date, Zone | None]:
        """`(aware datetime in its zone | date, zone)` for one decoded value."""
        if isinstance(value, tuple):  # a PERIOD: its start
            value = value[0]
        if not isinstance(value, datetime):
            if isinstance(value, date):
                return value, None
            raise ValueError("not a date or date-time")
        wall = value.replace(tzinfo=None)
        if tzid is not None:
            zone = self.resolver.zone(tzid, wall)
            if zone.assumed:
                self.warnings.add("unknown_tzid")
            if zone.mismatch:
                self.warnings.add("vtimezone_mismatch")
            return wall.replace(tzinfo=zone.tzinfo), zone
        if value.tzinfo is not None:
            return value.astimezone(UTC), None
        self.warnings.add("floating")
        zone = self.resolver.default()
        return wall.replace(tzinfo=zone.tzinfo), zone

    def prop(self, prop: Any) -> tuple[datetime | date, Zone | None]:
        return self.moment(prop.dt, _param(prop, "TZID"))

    def utc_list(self, props: Any, event_zone: Zone | None) -> tuple[list[datetime], list[date]]:
        """Every value of RDATE/EXDATE properties: UTC instants and bare dates."""
        instants: list[datetime] = []
        days: list[date] = []
        for prop in _listify(props):
            tzid = _param(prop, "TZID")
            for item in getattr(prop, "dts", []):
                if len(instants) + len(days) >= MAX_DATES:
                    self.warnings.add("dates_truncated")
                    return instants, days
                value = item.dt
                if isinstance(value, tuple):
                    value = value[0]
                if isinstance(value, datetime):
                    if tzid is None and value.tzinfo is None and event_zone is not None:
                        instants.append(value.replace(tzinfo=event_zone.tzinfo).astimezone(UTC))
                        continue
                    moment, _ = self.moment(value, tzid)
                    instants.append(moment.astimezone(UTC))  # type: ignore[union-attr]
                elif isinstance(value, date):
                    days.append(value)
        return instants, days


def _method(calendar: Component, part: mime.CalendarPart) -> tuple[Method, str | None]:
    raw = calendar.get("METHOD")
    text = str(raw).strip().upper() if raw is not None else part.method_param
    if not text:
        return Method.UNKNOWN, None
    text = text[:32]
    try:
        return Method(text), text
    except ValueError:
        return Method.UNKNOWN, text


def _event(
    event: Component,
    method: tuple[Method, str | None],
    part: mime.CalendarPart,
    resolver: ZoneResolver,
    notetaker: frozenset[str],
    auto: bool,
    bodies: list[str],
) -> ParsedEvent | None:
    uid = str(event.get("UID", "")).strip()
    if not uid or len(uid) > MAX_UID:
        return None
    times = _Times(resolver)
    try:
        start_prop = event.get("DTSTART")
        start_value, zone = times.prop(start_prop) if start_prop is not None else (None, None)
    except (ValueError, TypeError, AttributeError):
        return None

    all_day = isinstance(start_value, date) and not isinstance(start_value, datetime)
    start = start_value.astimezone(UTC) if isinstance(start_value, datetime) else None
    start_local = start_value if isinstance(start_value, datetime) else None
    if start_local is not None and zone is None:  # a UTC value: its zone is UTC
        zone = Zone(UTC, "UTC", "utc")

    end: datetime | None = None
    try:
        if event.get("DTEND") is not None and start is not None:
            end_value, _ = times.prop(event["DTEND"])
            if isinstance(end_value, datetime):
                end = end_value.astimezone(UTC)
        elif event.get("DURATION") is not None and start is not None:
            duration = event["DURATION"].dt
            if isinstance(duration, timedelta):
                end = start + duration
    except (ValueError, TypeError, AttributeError, OverflowError):
        times.warnings.add("unreadable_end")
    if end is not None and start is not None and end < start:
        times.warnings.add("end_before_start")
        end = None

    recurrence_id: datetime | date | None = None
    recurrence_range = None
    if event.get("RECURRENCE-ID") is not None:
        try:
            rid, _ = times.prop(event["RECURRENCE-ID"])
        except (ValueError, TypeError, AttributeError):
            return None
        recurrence_id = rid.astimezone(UTC) if isinstance(rid, datetime) else rid
        rng = _param(event["RECURRENCE-ID"], "RANGE")
        recurrence_range = rng.strip().upper() if rng else None

    try:
        sequence = max(0, int(str(event.get("SEQUENCE", 0)).strip()))
    except ValueError:
        sequence = 0
        times.warnings.add("bad_sequence")

    dtstamp = None
    if event.get("DTSTAMP") is not None:
        try:
            stamp, _ = times.prop(event["DTSTAMP"])
            if isinstance(stamp, datetime):
                dtstamp = stamp.astimezone(UTC)
        except (ValueError, TypeError, AttributeError):
            times.warnings.add("bad_dtstamp")

    organizer = organizer_name = None
    if event.get("ORGANIZER") is not None:
        org_prop = _listify(event["ORGANIZER"])[0]
        organizer = _address(org_prop)
        cn = _param(org_prop, "CN")
        organizer_name = clean_text(cn) if cn else None

    attendees: list[Attendee] = []
    attendee_addresses: list[str] = []
    for prop in _listify(event.get("ATTENDEE")):
        address = _address(prop)
        if address is None:
            continue
        attendee_addresses.append(address)
        if len(attendees) < MAX_ATTENDEES:
            cn = _param(prop, "CN")
            partstat = _param(prop, "PARTSTAT")
            role = _param(prop, "ROLE")
            attendees.append(
                Attendee(
                    address=address,
                    name=clean_text(cn) if cn else None,
                    partstat=partstat.upper()[:32] if partstat else None,
                    role=role.upper()[:32] if role else None,
                )
            )
        else:
            times.warnings.add("attendees_truncated")

    rrules = _listify(event.get("RRULE"))
    rrule = rrules[0].to_ical().decode() if rrules else None
    if len(rrules) > 1:
        times.warnings.add("extra_rrule_ignored")
    try:
        rdates, _rdays = times.utc_list(event.get("RDATE"), zone)
        exdates, exdays = times.utc_list(event.get("EXDATE"), zone)
    except (ValueError, TypeError, AttributeError):
        rdates, exdates, exdays = [], [], []
        times.warnings.add("unreadable_dates")

    status = event.get("STATUS")
    summary = clean_text(str(event.get("SUMMARY", "")))
    texts = {name: [str(v) for v in _listify(event.get(name))] for name in links.SOURCES[:-1]}
    link = links.join_link(texts, bodies)
    zone_flag = zone is not None and zone.assumed
    return ParsedEvent(
        uid=uid,
        uid_norm=normalise_uid(uid),
        method=method[0],
        method_raw=method[1],
        recurrence_id=recurrence_id,
        recurrence_range=recurrence_range,
        sequence=sequence,
        dtstamp=dtstamp,
        summary=summary,
        organizer=organizer,
        organizer_name=organizer_name,
        attendees=tuple(attendees),
        status=str(status).strip().upper()[:32] if status is not None else None,
        all_day=all_day,
        start=start,
        end=end,
        start_date=start_value if all_day else None,  # type: ignore[arg-type]
        start_local=start_local,
        zone=zone,
        floating=zone_flag,
        rrule=rrule,
        rdates=tuple(rdates),
        exdates=tuple(exdates),
        exdate_days=tuple(exdays),
        join_url=link.url if link else None,
        platform=link.platform if link else None,
        join_source=link.source if link else None,
        is_forwarded_copy=forward.is_forwarded_copy(attendee_addresses, notetaker),
        auto_forwarded=auto,
        via_forward_attachment=part.forwarded,
        warnings=tuple(sorted(times.warnings)),
    )


def _calendars(text: str) -> list[Component]:
    try:
        return list(Calendar.from_ical(text, multiple=True))
    except Exception as exc:  # noqa: BLE001 - icalendar raises many types on bad input
        raise InviteParseError("unreadable calendar") from exc


def stored_vtimezone(text: str) -> Any:
    """The tzinfo of a VTIMEZONE text kept on a `Zone` (a stored series expanded again after its
    message was purged). Raises ValueError for text that is not a readable VTIMEZONE."""
    with _parse_lock:
        try:
            return vtimezone_tzinfo(text)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - icalendar raises many types on bad input
            raise ValueError("unreadable VTIMEZONE") from exc
        finally:
            reset_icalendar_cache()


def parse(
    raw: bytes,
    notetaker_addresses: Iterable[str],
    *,
    default_tz: str = DEFAULT_TZ,
    deadline_s: float = DEADLINE_S,
) -> list[ParsedEvent]:
    """The invites in `raw` (see the module docstring). Raises `InviteParseError`."""
    started = time.monotonic()

    def check_time() -> None:
        if time.monotonic() - started > deadline_s:
            raise InviteParseError("parse time cap")

    try:
        extracted = mime.extract(raw)
    except mime.MimeError as exc:
        raise InviteParseError(str(exc)) from exc
    if not extracted.calendars:
        return []
    total = sum(len(_VEVENT_LINE.findall(p.text.encode())) for p in extracted.calendars)
    if total > MAX_VEVENTS:
        raise InviteParseError(f"more than {MAX_VEVENTS} VEVENTs")

    notetaker = frozenset(a.strip().lower() for a in notetaker_addresses if a.strip())
    auto = forward.auto_forwarded(extracted.headers)
    bodies = [b.text for b in extracted.bodies]
    events: list[ParsedEvent] = []
    seen: set[tuple[Any, ...]] = set()
    with _parse_lock:
        try:
            for part in extracted.calendars:
                check_time()
                for calendar in _calendars(part.text):
                    method = _method(calendar, part)
                    resolver = ZoneResolver(list(calendar.walk("VTIMEZONE")), default_tz)
                    for component in calendar.walk("VEVENT"):
                        check_time()
                        parsed = _event(
                            component, method, part, resolver, notetaker, auto, bodies
                        )
                        if parsed is None or parsed.key in seen:
                            continue
                        seen.add(parsed.key)
                        events.append(parsed)
        finally:
            reset_icalendar_cache()
    if not events:
        raise InviteParseError("no usable VEVENT")
    return events
