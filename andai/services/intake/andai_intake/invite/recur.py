"""`occurrences(event, now)`: the starts of an event that matter now, in UTC, bounded.

Expansion runs in the event's own zone and wall-clock time, so a 10:00 meeting stays at 10:00 across
a DST change, and only then converts to UTC. The window is `[now, now + horizon]` (D-58: 14 days):
an occurrence is returned when it has not ended by `now` (so one already running is returned, for
the join-at-once rule) and starts no later than `now + horizon`.

* A one-off event is its DTSTART. A single instance sent alone (a VEVENT with RECURRENCE-ID and no
  RRULE) is that instance, keyed by its RECURRENCE-ID; with STATUS:CANCELLED it is nothing. An
  all-day event (`VALUE=DATE`) never produces an occurrence.
* A series is DTSTART (always the first instance, RFC 5545 §3.8.5.3), RRULE (dateutil `rrule`),
  plus RDATE, minus EXDATE.
* `overrides` are the series' RECURRENCE-ID VEVENTs (from the same message or from stored state). An
  override replaces the instance whose original start equals its RECURRENCE-ID: it moves it (the
  new time is used, even into or out of the window) or, with STATUS:CANCELLED, removes it.
  RANGE=THISANDFUTURE is applied as an override of that one instance only.

Bounds. SECONDLY, MINUTELY and HOURLY rules are not meetings and expand to nothing (`refused`), as
does a series whose DTSTART is before `EARLIEST_SERIES`. The rule's UNTIL is clipped to the window
end. The walk stops at `MAX_IN_WINDOW` occurrences in the window, `MAX_STEPS` instances in all, or
`MAX_SECONDS` of wall time (`capped`). An event without DTEND or DURATION is taken to last
`ASSUMED_DURATION` for the "has it ended" test.

dateutil compares UNTIL only with instants that pass the rule's filters, so a rule that never
matches again (`FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30`) walks every period up to the year 9999 inside
one step, which no cap here can interrupt: about 2 s for DAILY, 0.5 s for WEEKLY, 0.2 s for MONTHLY
and 0.1 s for YEARLY. A DAILY rule with a filter that can make it barren (`DAILY_REFUSED_PARTS`) is
therefore refused (`unsupported_daily_rule`); meetings that recur daily filter by BYDAY only.
"""

from __future__ import annotations

import re
import time
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from dateutil.rrule import rrule, rrulestr

from andai_intake.invite.ics import ParsedEvent

HORIZON = timedelta(days=14)
MAX_IN_WINDOW = 1000
MAX_STEPS = 50_000
MAX_SECONDS = 0.5
ASSUMED_DURATION = timedelta(hours=1)
EARLIEST_SERIES = datetime(1970, 1, 1, tzinfo=UTC)
REFUSED_FREQS = frozenset({"SECONDLY", "MINUTELY", "HOURLY"})
DAILY_REFUSED_PARTS = frozenset({"BYMONTH", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYSETPOS"})

_UNTIL = re.compile(r"(?i)(?:^|;)UNTIL=([0-9TZ]+)")


@dataclass(frozen=True)
class Occurrence:
    recurrence_id: datetime  # the original start (UTC): the key of this instance
    start: datetime  # UTC, after any override
    end: datetime  # UTC
    overridden: bool


@dataclass(frozen=True)
class Expansion:
    occurrences: tuple[Occurrence, ...]
    capped: bool = False  # a bound stopped the walk; later instances may be missing
    refused: str | None = None  # why nothing was expanded (sub-daily rule, ancient series)


def _parts(rule: str) -> dict[str, str]:
    """The RRULE's NAME=VALUE parts, names upper-cased."""
    out: dict[str, str] = {}
    for item in rule.split(";"):
        name, _, value = item.partition("=")
        if name.strip():
            out[name.strip().upper()] = value.strip().upper()
    return out


def _duration(event: ParsedEvent) -> timedelta:
    if event.start is not None and event.end is not None:
        return event.end - event.start
    return ASSUMED_DURATION


def _rule_text(text: str, zone_info, window_end_local: datetime) -> tuple[str, datetime | None]:
    """The RRULE with UNTIL moved to local wall time (so a naive DTSTART can be used) and the
    clipped bound: min(UNTIL, window end)."""
    until_local: datetime | None = None
    match = _UNTIL.search(text)
    if match:
        value = match.group(1).upper()
        if len(value) == 8:  # a DATE: inclusive of that whole local day
            day = datetime.strptime(value, "%Y%m%d")
            until_local = day + timedelta(days=1) - timedelta(seconds=1)
        elif value.endswith("Z"):
            utc = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            until_local = utc.astimezone(zone_info).replace(tzinfo=None)
        else:
            until_local = datetime.strptime(value, "%Y%m%dT%H%M%S")
        text = _UNTIL.sub("", text).strip(";")
    bound = window_end_local if until_local is None else min(until_local, window_end_local)
    return text, bound


def occurrences(
    event: ParsedEvent,
    now: datetime,
    horizon: timedelta = HORIZON,
    overrides: Iterable[ParsedEvent] = (),
) -> Expansion:
    """The occurrences of `event` that have not ended and start by `now + horizon`."""
    if event.all_day or event.start is None or event.start_local is None or event.zone is None:
        return Expansion(())
    window_end = now + horizon
    length = _duration(event)
    tz = event.zone.tzinfo

    moved: dict[datetime, ParsedEvent] = {}
    for item in overrides:
        if isinstance(item.recurrence_id, datetime) and item.uid_norm == event.uid_norm:
            known = moved.get(item.recurrence_id)
            if known is None or (item.sequence, item.dtstamp or EARLIEST_SERIES) >= (
                known.sequence,
                known.dtstamp or EARLIEST_SERIES,
            ):
                moved[item.recurrence_id] = item

    def emit(original: datetime, out: dict[datetime, Occurrence]) -> None:
        override = moved.get(original)
        if override is None:
            start, end, overridden = original, original + length, False
        else:
            if (override.status or "") == "CANCELLED" or override.start is None:
                return
            start = override.start
            end = override.end or (start + _duration(override))
            overridden = True
        if end > now and start <= window_end:
            out[original] = Occurrence(original, start, end, overridden)

    out: dict[datetime, Occurrence] = {}
    excluded = set(event.exdates)
    excluded_days = set(event.exdate_days)

    def wanted(original: datetime) -> bool:
        if original in excluded:
            return False
        return original.astimezone(tz).date() not in excluded_days

    if not event.rrule and not event.rdates:
        # A one-off, or a single instance of a series sent alone (keyed by its RECURRENCE-ID).
        if (event.status or "") == "CANCELLED":
            return Expansion(())
        original = event.recurrence_id if isinstance(event.recurrence_id, datetime) else None
        end = event.start + length
        if end > now and event.start <= window_end:
            key = original or event.start
            out[key] = Occurrence(key, event.start, end, original is not None)
        return Expansion(_sorted(out))

    parts = _parts(event.rrule or "")
    freq = parts.get("FREQ", "")
    if freq in REFUSED_FREQS:
        return Expansion((), refused="sub_daily_rule")
    if freq == "DAILY" and DAILY_REFUSED_PARTS & parts.keys():
        return Expansion((), refused="unsupported_daily_rule")
    if event.start < EARLIEST_SERIES:
        return Expansion((), refused="series_too_old")

    window_end_local = window_end.astimezone(tz).replace(tzinfo=None)
    starts: set[datetime] = {event.start, *event.rdates}
    capped = False
    if event.rrule:
        text, bound = _rule_text(event.rrule, tz, window_end_local)
        dtstart = event.start_local.replace(tzinfo=None)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # COUNT and a clipped UNTIL
                rule = rrulestr(text, dtstart=dtstart, ignoretz=True)
                if isinstance(rule, rrule) and bound is not None:
                    rule = rule.replace(until=bound)
        except (ValueError, TypeError, OverflowError):
            return Expansion((), refused="unreadable_rule")
        began = time.monotonic()
        steps = in_window = 0
        for wall in rule:
            steps += 1
            instant = wall.replace(tzinfo=tz).astimezone(UTC)
            starts.add(instant)
            if instant + length > now:
                in_window += 1
            if instant > window_end:
                break
            if (
                in_window >= MAX_IN_WINDOW
                or steps >= MAX_STEPS
                or time.monotonic() - began > MAX_SECONDS
            ):
                capped = True
                break

    # An override whose original instance lies past the window can still move it into the window.
    starts.update(original for original in moved if original > window_end)
    for original in sorted(starts):
        if wanted(original):
            emit(original, out)
    return Expansion(_sorted(out), capped=capped)


def _sorted(out: dict[datetime, Occurrence]) -> tuple[Occurrence, ...]:
    return tuple(sorted(out.values(), key=lambda o: (o.start, o.recurrence_id)))
