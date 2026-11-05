"""Time zones of one invite: TZID → a tzinfo, without icalendar's process-wide cache.

icalendar resolves a TZID while it parses and caches every custom VTIMEZONE process-wide under its
TZID, first definition wins. Two messages that both say "Customized Time Zone" with different
offsets would then share the first one. This module never uses those decoded zones: `ics.py` takes
the wall-clock value and the TZID parameter of each property and asks `ZoneResolver`, which holds
the VTIMEZONEs of one message only. `reset_icalendar_cache()` empties icalendar's cache after every
parse, so it cannot grow with the TZIDs senders invent.

How a TZID resolves (`ZoneResolver.zone`):

1. an IANA name (`Asia/Kolkata`); else a Windows name through icalendar's CLDR-generated
   `WINDOWS_TO_OLSON` (`India Standard Time` → `Asia/Calcutta`, an alias of `Asia/Kolkata`); else
   a "globally unique" TZID (`/vendor/Europe/Berlin`) by its trailing IANA name;
2. when the message embeds a VTIMEZONE for that TZID, its offsets are compared, day by day from
   `VALIDATE_DAYS_BEFORE` before the event to `VALIDATE_DAYS_AFTER` after it, with the zone from
   step 1. If they agree, the IANA zone is used. If they disagree, or step 1 found nothing
   (Outlook's "Customized Time Zone", "(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi"), the
   VTIMEZONE's own offsets are used. Those tzinfo objects are cached by the VTIMEZONE's content hash, never by TZID;
3. a TZID that neither step resolves falls back to the deployment's default zone
   (`ANDAI_DEFAULT_TIMEZONE`), flagged `assumed`.

A value without TZID and without `Z` is floating: it is read in the default zone and flagged
`floating`. A `VALUE=DATE` value is all-day and has no zone at all.
"""

from __future__ import annotations

import hashlib
import threading
import zoneinfo
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache

from icalendar.cal.timezone import Timezone
from icalendar.timezone import tzp
from icalendar.timezone.windows_to_olson import WINDOWS_TO_OLSON
from icalendar.timezone.zoneinfo import ZONEINFO

VALIDATE_DAYS_BEFORE = 30
VALIDATE_DAYS_AFTER = 400
VTIMEZONE_CACHE_SIZE = 64
VTIMEZONE_TEXT_MAX = 16 * 1024

_PROVIDER = ZONEINFO()
_vtz_cache: OrderedDict[bytes, tzinfo] = OrderedDict()
_lock = threading.Lock()


@dataclass(frozen=True)
class Zone:
    tzinfo: tzinfo
    name: str  # the IANA name, "UTC", or "vtimezone:<hash>" for a VTIMEZONE's own offsets
    source: str  # utc | iana | windows | global | vtimezone | default
    assumed: bool = False  # floating value or unknown TZID read in the default zone
    mismatch: bool = False  # the VTIMEZONE disagreed with the mapped IANA zone
    vtimezone: str | None = None  # the VTIMEZONE's own text when its offsets are used


UTC_ZONE = Zone(UTC, "UTC", "utc")


@lru_cache(maxsize=1)
def _available() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def iana_name(tzid: str) -> tuple[str, str] | None:
    """`(IANA name, source)` for a TZID, or None (see step 1 of the module docstring)."""
    cleaned = tzid.strip().strip('"').strip()
    bare = cleaned.strip("/")
    if bare in _available():
        return bare, "iana"
    if bare in WINDOWS_TO_OLSON:
        return WINDOWS_TO_OLSON[bare], "windows"
    if cleaned.startswith("/"):
        parts = bare.split("/")
        for start in range(1, len(parts)):
            candidate = "/".join(parts[start:])
            if candidate in _available():
                return candidate, "global"
    return None


def reset_icalendar_cache() -> None:
    """Empty icalendar's process-wide timezone cache (see the module docstring)."""
    tzp.use(_PROVIDER)


def _vtimezone_tz(component: Timezone) -> tuple[tzinfo, str]:
    digest = hashlib.sha256(component.to_ical()).digest()
    with _lock:
        cached = _vtz_cache.get(digest)
        if cached is not None:
            _vtz_cache.move_to_end(digest)
            return cached, digest.hex()[:12]
    built = tzp.create_timezone(component)  # never cached by icalendar
    with _lock:
        _vtz_cache[digest] = built
        while len(_vtz_cache) > VTIMEZONE_CACHE_SIZE:
            _vtz_cache.popitem(last=False)
    return built, digest.hex()[:12]


def _text(component: Timezone) -> str | None:
    """The VTIMEZONE as iCalendar text, kept so a stored series can be expanded again after the
    message is purged; None above `VTIMEZONE_TEXT_MAX`."""
    text = component.to_ical().decode("utf-8", "replace")
    return text if len(text) <= VTIMEZONE_TEXT_MAX else None


def vtimezone_tzinfo(text: str) -> tzinfo:
    """The tzinfo of a VTIMEZONE kept by `Zone.vtimezone` (cached by content hash like any other).
    The caller holds the parse lock and empties icalendar's cache afterwards (`ics.stored_zone`)."""
    component = Timezone.from_ical(text)
    if not isinstance(component, Timezone):
        raise ValueError("not a VTIMEZONE")
    return _vtimezone_tz(component)[0]


def _agree(a: tzinfo, b: tzinfo, near: datetime) -> bool:
    wall = near.replace(tzinfo=None)
    for day in range(-VALIDATE_DAYS_BEFORE, VALIDATE_DAYS_AFTER + 1):
        moment = wall + timedelta(days=day)
        if moment.replace(tzinfo=a).utcoffset() != moment.replace(tzinfo=b).utcoffset():
            return False
    return True


class ZoneResolver:
    """TZID → `Zone` for one message, from its own VTIMEZONEs and the IANA database."""

    def __init__(self, vtimezones: list[Timezone], default_tz: str) -> None:
        self._components: dict[str, Timezone] = {}
        for component in vtimezones:
            tzid = str(component.get("TZID", "")).strip()
            if tzid and tzid not in self._components:
                self._components[tzid] = component
        self._default_name = default_tz
        self._default = zoneinfo.ZoneInfo(default_tz)
        self._memo: dict[str, Zone] = {}

    def default(self) -> Zone:
        """The default zone for a floating value."""
        return Zone(self._default, self._default_name, "default", assumed=True)

    def zone(self, tzid: str | None, near: datetime | None) -> Zone:
        """The zone for a value with TZID `tzid` (None → floating) near wall time `near`."""
        if tzid is None:
            return self.default()
        if tzid in self._memo:
            return self._memo[tzid]
        mapped = iana_name(tzid)
        candidate = zoneinfo.ZoneInfo(mapped[0]) if mapped else None
        component = self._components.get(tzid.strip().strip('"').strip())
        result: Zone
        if component is not None:
            try:
                own, digest = _vtimezone_tz(component)
            except Exception:  # noqa: BLE001 - a broken VTIMEZONE falls back below
                own, digest = None, ""
            if own is not None and candidate is not None and mapped is not None:
                if near is None or _agree(candidate, own, near):
                    result = Zone(candidate, mapped[0], mapped[1])
                else:
                    result = Zone(
                        own, f"vtimezone:{digest}", "vtimezone", mismatch=True,
                        vtimezone=_text(component),
                    )
            elif own is not None:
                result = Zone(own, f"vtimezone:{digest}", "vtimezone", vtimezone=_text(component))
            elif candidate is not None and mapped is not None:
                result = Zone(candidate, mapped[0], mapped[1])
            else:
                result = Zone(self._default, self._default_name, "default", assumed=True)
        elif candidate is not None and mapped is not None:
            result = Zone(candidate, mapped[0], mapped[1])
        else:
            result = Zone(self._default, self._default_name, "default", assumed=True)
        self._memo[tzid] = result
        return result
