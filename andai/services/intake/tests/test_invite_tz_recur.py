"""Time zones (Windows TZIDs, custom VTIMEZONEs, floating, all-day) and bounded recurrence
expansion (RRULE, EXDATE, RDATE, RECURRENCE-ID overrides, COUNT/UNTIL, caps)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from icalendar import Calendar
from icalendar.timezone import tzp

from andai_intake.invite import parse
from andai_intake.invite.recur import MAX_IN_WINDOW, occurrences
from andai_intake.invite.tz import WINDOWS_TO_OLSON, ZoneResolver, iana_name
from invitekit import CALENDARS, NOTETAKER, calendar, message, vevent


def one(ics: str, default_tz: str = "Asia/Kolkata"):
    (event,) = parse(message(attachment=ics), [NOTETAKER], default_tz=default_tz)
    return event


def timed(tzid: str, wall: str, **kwargs):
    return vevent(
        dtstart=f"DTSTART;TZID={tzid}:{wall}",
        dtend=f"DTEND;TZID={tzid}:{wall[:9]}{int(wall[9:11]) + 1:02d}{wall[11:]}",
        **kwargs,
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def vtimezone(tzid: str, offset: str) -> str:
    return "\r\n".join(
        [
            "BEGIN:VTIMEZONE",
            f"TZID:{tzid}",
            "BEGIN:STANDARD",
            "DTSTART:16010101T000000",
            f"TZOFFSETFROM:{offset}",
            f"TZOFFSETTO:{offset}",
            "END:STANDARD",
            "END:VTIMEZONE",
        ]
    )


# ─── Windows TZIDs, DST ─────────────────────────────────────────────────────────────────────────


def test_india_standard_time():
    event = one(calendar(timed("India Standard Time", "20261117T100000")))
    assert event.start == utc(2026, 11, 17, 4, 30)
    assert event.end == utc(2026, 11, 17, 5, 30)
    # CLDR names the zone by its older alias; both are the same IANA zone
    assert event.zone.name == WINDOWS_TO_OLSON["India Standard Time"] == "Asia/Calcutta"
    assert event.zone.source == "windows"
    assert event.floating is False and event.warnings == ()


@pytest.mark.parametrize(
    ("wall", "expected"),
    [
        ("20261030T090000", utc(2026, 10, 30, 16)),  # PDT, UTC-7
        ("20261102T090000", utc(2026, 11, 2, 17)),  # PST, UTC-8 (DST ended 2026-11-01)
    ],
)
def test_pacific_standard_time_across_dst(wall, expected):
    event = one(calendar(timed("Pacific Standard Time", wall)))
    assert event.start == expected
    assert event.zone.name == "America/Los_Angeles"


@pytest.mark.parametrize(
    ("wall", "expected"),
    [
        ("20261023T100000", utc(2026, 10, 23, 8)),  # CEST, UTC+2
        ("20261026T100000", utc(2026, 10, 26, 9)),  # CET, UTC+1 (DST ended 2026-10-25)
    ],
)
def test_w_europe_standard_time_across_dst(wall, expected):
    event = one(calendar(timed("W. Europe Standard Time", wall)))
    assert event.start == expected
    assert event.zone.name == "Europe/Berlin"


def test_weekly_series_keeps_wall_clock_across_dst():
    event = one(calendar(timed("Pacific Standard Time", "20261026T090000",
                               extra=["RRULE:FREQ=WEEKLY;COUNT=3"])))
    got = occurrences(event, now=utc(2026, 10, 25))
    assert [o.start for o in got.occurrences] == [
        utc(2026, 10, 26, 16),
        utc(2026, 11, 2, 17),
    ]  # 09:00 local both times; the third (11-09) is past the 14-day window


def test_iana_and_globally_unique_tzids():
    assert iana_name("Europe/Berlin") == ("Europe/Berlin", "iana")
    assert iana_name("/freeassociation.sourceforge.net/Tzfile/Europe/Berlin") == (
        "Europe/Berlin",
        "global",
    )
    assert iana_name('"India Standard Time"') == ("Asia/Calcutta", "windows")
    assert iana_name("Customized Time Zone") is None
    assert iana_name("../../etc/passwd") is None


# ─── custom VTIMEZONEs and icalendar's cache ────────────────────────────────────────────────────


def test_vtimezone_disagreeing_with_the_mapping_wins():
    ics = calendar(
        timed("India Standard Time", "20261117T100000"),
        vtimezones=[vtimezone("India Standard Time", "+0500")],
    )
    event = one(ics)
    assert event.start == utc(2026, 11, 17, 5)  # the VTIMEZONE's +05:00, not Kolkata's +05:30
    assert event.zone.source == "vtimezone" and event.zone.mismatch
    assert "vtimezone_mismatch" in event.warnings


def test_vtimezone_agreeing_with_the_mapping_uses_the_iana_zone():
    ics = calendar(
        timed("India Standard Time", "20261117T100000"),
        vtimezones=[vtimezone("India Standard Time", "+0530")],
    )
    event = one(ics)
    assert event.start == utc(2026, 11, 17, 4, 30)
    assert event.zone.name == "Asia/Calcutta" and not event.zone.mismatch


def test_customized_time_zone_is_never_shared_between_messages():
    first = one(
        calendar(
            timed("Customized Time Zone", "20261117T100000", uid="a@x.test"),
            vtimezones=[vtimezone("Customized Time Zone", "+0530")],
        )
    )
    # icalendar's process-wide cache keeps nothing after the parse
    assert tzp.timezone("Customized Time Zone") is None
    second = one(
        calendar(
            timed("Customized Time Zone", "20261117T100000", uid="b@x.test"),
            vtimezones=[vtimezone("Customized Time Zone", "-0800")],
        )
    )
    assert first.start == utc(2026, 11, 17, 4, 30)
    assert second.start == utc(2026, 11, 17, 18)
    assert first.zone.name != second.zone.name  # keyed by content hash
    assert first.zone.name.startswith("vtimezone:")
    # The cache-poisoning case itself: icalendar alone would decode B with A's zone.
    poisoned = Calendar.from_ical(
        calendar(
            timed("Customized Time Zone", "20261117T100000"),
            vtimezones=[vtimezone("Customized Time Zone", "+0530")],
        )
    )
    del poisoned
    second_again = one(
        calendar(
            timed("Customized Time Zone", "20261117T100000", uid="c@x.test"),
            vtimezones=[vtimezone("Customized Time Zone", "-0800")],
        )
    )
    assert second_again.start == utc(2026, 11, 17, 18)


def test_unknown_tzid_without_vtimezone_falls_back_to_the_default_zone():
    event = one(calendar(timed("Mars Standard Time", "20261117T100000")),
                default_tz="Europe/Berlin")
    assert event.start == utc(2026, 11, 17, 9)
    assert event.floating is True
    assert "unknown_tzid" in event.warnings


def test_floating_time_uses_the_default_zone_and_is_flagged():
    ics = calendar(vevent(dtstart="DTSTART:20261117T100000", dtend="DTEND:20261117T110000"))
    event = one(ics, default_tz="Asia/Kolkata")
    assert event.start == utc(2026, 11, 17, 4, 30)
    assert event.floating is True and "floating" in event.warnings
    berlin = one(ics, default_tz="Europe/Berlin")
    assert berlin.start == utc(2026, 11, 17, 9)


def test_all_day_event_has_no_start_and_no_occurrence():
    ics = calendar(
        vevent(dtstart="DTSTART;VALUE=DATE:20261117", dtend="DTEND;VALUE=DATE:20261118",
               extra=["RRULE:FREQ=DAILY"])
    )
    event = one(ics)
    assert event.all_day is True
    assert event.start is None and event.start_date.isoformat() == "2026-11-17"
    assert occurrences(event, now=utc(2026, 11, 16)).occurrences == ()


# ─── icalendar's BSD-2 calendars (tests/calendars, see README.md there) ─────────────────────────


def read(name: str) -> str:
    return (CALENDARS / name).read_text(encoding="utf-8")


def test_corpus_custom_tzid_with_full_vtimezone():
    event = one(read("america_new_york.ics"))
    assert event.start == utc(2014, 8, 29, 12)  # 08:00 EDT
    assert event.end == utc(2014, 8, 29, 14)
    assert event.zone.source == "vtimezone"


def test_corpus_vtimezone_matching_its_iana_name():
    event = one(read("timezoned.ics"))
    assert event.start == utc(2012, 2, 13, 9)  # 10:00 CET
    assert event.zone.name == "Europe/Vienna" and event.zone.source == "iana"


def test_corpus_outlook_style_tzid_defined_only_by_vtimezone():
    cal = Calendar.from_ical(read("issue_237_fail_to_parse_timezone_with_non_ascii_tzid.ics"))
    resolver = ZoneResolver(list(cal.walk("VTIMEZONE")), "Asia/Kolkata")
    zone = resolver.zone("(UTC-03:00) Brasília", datetime(2017, 5, 11, 13, 30))
    assert zone.source == "vtimezone"
    assert datetime(2017, 5, 11, 13, 30, tzinfo=zone.tzinfo).astimezone(UTC) == utc(
        2017, 5, 11, 16, 30
    )


def test_corpus_google_weekly_with_until_and_exdates():
    event = one(calendar(read("issue_112_missing_tzinfo_on_exdate.ics").strip(), method=None))
    assert event.rrule == "FREQ=WEEKLY;UNTIL=20131025T035959Z;BYDAY=FR,SA"
    assert len(event.exdates) == 2
    got = occurrences(event, now=utc(2013, 10, 3))
    # Fri 10-04, Sat 10-05; Fri 10-11 and Sat 10-12 are EXDATEs; 10-18/19 past the window
    assert [o.start for o in got.occurrences] == [utc(2013, 10, 4, 16), utc(2013, 10, 5, 16)]
    late = occurrences(event, now=utc(2013, 10, 17))
    # Fri 10-18 and Sat 10-19 are inside UNTIL; Fri 10-25 12:00 EDT is after UNTIL 03:59:59Z
    assert [o.start for o in late.occurrences] == [utc(2013, 10, 18, 16), utc(2013, 10, 19, 16)]


def test_corpus_single_instance_without_series():
    event = one(read("issue_1231_recurrence.ics"))
    assert event.recurrence_id == utc(2025, 4, 28, 16, 5)
    got = occurrences(event, now=utc(2025, 4, 28, 16))
    (only,) = got.occurrences
    assert only.recurrence_id == utc(2025, 4, 28, 16, 5) and only.overridden


# ─── recurrence ─────────────────────────────────────────────────────────────────────────────────


def series(rule: str, **kwargs):
    return vevent(
        dtstart="DTSTART;TZID=Asia/Kolkata:20261102T100000",
        dtend="DTEND;TZID=Asia/Kolkata:20261102T110000",
        extra=[f"RRULE:{rule}", *kwargs.pop("extra", [])],
        **kwargs,
    )


NOW = utc(2026, 11, 5)  # Thursday; the series is Mondays 10:00 IST (04:30Z) from 2026-11-02


def test_weekly_with_exdate_and_an_override_inside_the_window():
    base = series("FREQ=WEEKLY", extra=["EXDATE;TZID=Asia/Kolkata:20261116T100000"])
    moved = vevent(
        dtstart="DTSTART;TZID=Asia/Kolkata:20261110T150000",
        dtend="DTEND;TZID=Asia/Kolkata:20261110T160000",
        extra=["RECURRENCE-ID;TZID=Asia/Kolkata:20261109T100000"],
    )
    events = parse(message(attachment=calendar(base, moved)), [NOTETAKER])
    got = occurrences(events[0], now=NOW, overrides=events[1:])
    assert [(o.recurrence_id, o.start, o.overridden) for o in got.occurrences] == [
        (utc(2026, 11, 9, 4, 30), utc(2026, 11, 10, 9, 30), True),  # moved to Tue 15:00
        # 11-16 is an EXDATE
    ]
    assert got.capped is False
    later = occurrences(events[0], now=utc(2026, 11, 12), overrides=events[1:])
    assert [o.start for o in later.occurrences] == [utc(2026, 11, 23, 4, 30)]


def test_cancelled_override_removes_the_instance():
    base = series("FREQ=WEEKLY")
    cancelled = vevent(
        dtstart="DTSTART;TZID=Asia/Kolkata:20261109T100000",
        extra=["RECURRENCE-ID;TZID=Asia/Kolkata:20261109T100000", "STATUS:CANCELLED"],
    )
    events = parse(message(attachment=calendar(base, cancelled)), [NOTETAKER])
    got = occurrences(events[0], now=NOW, overrides=events[1:])
    assert [o.start for o in got.occurrences] == [utc(2026, 11, 16, 4, 30)]


def test_override_from_past_the_window_can_move_in():
    base = series("FREQ=WEEKLY")
    moved = vevent(
        dtstart="DTSTART;TZID=Asia/Kolkata:20261112T100000",
        dtend="DTEND;TZID=Asia/Kolkata:20261112T110000",
        extra=["RECURRENCE-ID;TZID=Asia/Kolkata:20261130T100000"],
    )
    events = parse(message(attachment=calendar(base, moved)), [NOTETAKER])
    got = occurrences(events[0], now=NOW, overrides=events[1:])
    assert utc(2026, 11, 12, 4, 30) in [o.start for o in got.occurrences]


def test_count_and_until():
    count = one(calendar(series("FREQ=WEEKLY;COUNT=2")))
    assert [o.start for o in occurrences(count, now=NOW).occurrences] == [utc(2026, 11, 9, 4, 30)]
    until = one(calendar(series("FREQ=DAILY;UNTIL=20261108T043000Z")))
    assert [o.start.day for o in occurrences(until, now=NOW).occurrences] == [5, 6, 7, 8]
    until_date = one(calendar(series("FREQ=DAILY;UNTIL=20261107")))
    assert [o.start.day for o in occurrences(until_date, now=NOW).occurrences] == [5, 6, 7]


def test_rdate_adds_an_instance():
    base = series("FREQ=WEEKLY;COUNT=1", extra=["RDATE;TZID=Asia/Kolkata:20261111T100000"])
    got = occurrences(one(calendar(base)), now=NOW)
    assert [o.start for o in got.occurrences] == [utc(2026, 11, 11, 4, 30)]


def test_running_occurrence_is_returned_and_ended_one_is_not():
    event = one(calendar(series("FREQ=DAILY")))
    running = occurrences(event, now=utc(2026, 11, 5, 5))  # 10:30 IST, during the 10:00 meeting
    assert running.occurrences[0].start == utc(2026, 11, 5, 4, 30)
    ended = occurrences(event, now=utc(2026, 11, 5, 5, 31))
    assert ended.occurrences[0].start == utc(2026, 11, 6, 4, 30)
    assert len(ended.occurrences) == 14


def test_one_off_inside_and_outside_the_window():
    event = one(calendar(vevent()))  # 2026-11-10 04:30Z
    assert len(occurrences(event, now=NOW).occurrences) == 1
    assert occurrences(event, now=utc(2026, 10, 1)).occurrences == ()
    assert occurrences(event, now=utc(2026, 11, 10, 5, 31)).occurrences == ()


def test_old_daily_series_still_reaches_today():
    old = vevent(
        dtstart="DTSTART;TZID=Asia/Kolkata:20200106T093000",
        dtend="DTEND;TZID=Asia/Kolkata:20200106T094500",
        extra=["RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR"],
    )
    got = occurrences(one(calendar(old)), now=NOW)
    assert got.capped is False
    assert got.occurrences[0].start == utc(2026, 11, 5, 4)
    assert len(got.occurrences) == 10


def test_pathological_rules_stop_at_the_caps():
    flood = one(calendar(series("FREQ=DAILY;BYHOUR=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,"
                                "18,19,20,21,22,23;BYMINUTE=0,5,10,15,20,25,30,35,40,45,50,55")))
    started = time.monotonic()
    got = occurrences(flood, now=NOW)
    assert time.monotonic() - started < 2
    assert got.capped is True
    assert len(got.occurrences) <= MAX_IN_WINDOW + 1

    # A DAILY rule that can turn barren is refused before dateutil walks to the year 9999.
    for rule in ("FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30", "FREQ=DAILY;BYSETPOS=2"):
        started = time.monotonic()
        refused = occurrences(one(calendar(series(rule))), now=NOW)
        assert time.monotonic() - started < 0.2
        assert refused.refused == "unsupported_daily_rule"
    # A barren WEEKLY rule runs to the end of dateutil's calendar, but within about half a second.
    barren = one(calendar(series("FREQ=WEEKLY;BYMONTH=2;BYMONTHDAY=30")))
    started = time.monotonic()
    got = occurrences(barren, now=NOW)
    assert time.monotonic() - started < 1.5
    assert got.occurrences == ()

    for rule in ("FREQ=SECONDLY", "FREQ=MINUTELY;INTERVAL=5", "FREQ=HOURLY"):
        refused = occurrences(one(calendar(series(rule))), now=NOW)
        assert refused.occurrences == () and refused.refused == "sub_daily_rule"


def test_ancient_or_unreadable_series_expand_to_nothing():
    ancient = vevent(
        dtstart="DTSTART:16010101T100000Z", dtend=None, extra=["RRULE:FREQ=DAILY"]
    )
    assert occurrences(one(calendar(ancient)), now=NOW).refused == "series_too_old"
    broken = one(calendar(series("FREQ=WEEKLY;BYDAY=XX")))
    assert occurrences(broken, now=NOW).refused == "unreadable_rule"


def test_horizon_is_a_parameter():
    event = one(calendar(series("FREQ=DAILY")))
    assert len(occurrences(event, now=NOW, horizon=timedelta(days=2)).occurrences) == 2
